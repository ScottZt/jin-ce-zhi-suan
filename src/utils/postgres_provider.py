import re
import threading
import pandas as pd
from datetime import datetime, timedelta
from src.utils.config_loader import ConfigLoader
from src.utils.indicators import Indicators


class PostgresProvider:
    def __init__(self, host=None, port=None, user=None, password=None, database=None, schema=None):
        cfg = ConfigLoader.reload()
        self.host = str(host or cfg.get("data_provider.postgres_host", "127.0.0.1")).strip() or "127.0.0.1"
        self.port = int(port or cfg.get("data_provider.postgres_port", 5432) or 5432)
        self.user = str(user or cfg.get("data_provider.postgres_user", "")).strip()
        self.password = str(password or cfg.get("data_provider.postgres_password", "")).strip()
        self.database = str(database or cfg.get("data_provider.postgres_database", "")).strip()
        self.schema = str(schema or cfg.get("data_provider.postgres_schema", "public")).strip() or "public"
        self.pool_size = max(1, int(cfg.get("data_provider.postgres_pool_size", 6) or 6))
        self.pool_wait_timeout_sec = max(1.0, float(cfg.get("data_provider.postgres_pool_wait_timeout_sec", 10) or 10))
        self.page_size = max(1000, int(cfg.get("data_provider.postgres_query_page_size", 20000) or 20000))
        self.last_error = ""
        self._table_defaults = {
            "1min": "dat_1mins",
            "5min": "dat_5mins",
            "10min": "dat_10mins",
            "15min": "dat_15mins",
            "30min": "dat_30mins",
            "60min": "dat_60mins",
            "D": "dat_day",
        }
        self._pool_cond = threading.Condition()
        self._pool_idle = []
        self._pool_total = 0

    def _load_psycopg2(self):
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
            return psycopg2, RealDictCursor
        except Exception as e:
            self.last_error = f"psycopg2 未安装或导入失败: {e}"
            return None, None

    def _create_connection(self):
        psycopg2, _ = self._load_psycopg2()
        if psycopg2 is None:
            return None
        try:
            return psycopg2.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                dbname=self.database,
            )
        except Exception as e:
            self.last_error = f"PostgreSQL 连接失败: {e}"
            return None

    def _connection_alive(self, conn):
        if conn is None:
            return False
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            return True
        except Exception:
            return False

    def _acquire_connection(self):
        with self._pool_cond:
            while True:
                while self._pool_idle:
                    conn = self._pool_idle.pop()
                    if self._connection_alive(conn):
                        return conn
                    self._pool_total = max(0, self._pool_total - 1)
                    try:
                        conn.close()
                    except Exception:
                        pass
                if self._pool_total < self.pool_size:
                    self._pool_total += 1
                    break
                waited = self._pool_cond.wait(timeout=self.pool_wait_timeout_sec)
                if not waited:
                    self.last_error = f"PostgreSQL 连接池等待超时（{self.pool_wait_timeout_sec}s）"
                    return None
        conn = self._create_connection()
        if conn is not None:
            return conn
        with self._pool_cond:
            self._pool_total = max(0, self._pool_total - 1)
            self._pool_cond.notify()
        return None

    def _release_connection(self, conn, broken=False):
        if conn is None:
            return
        keep = (not broken) and self._connection_alive(conn)
        with self._pool_cond:
            if keep:
                self._pool_idle.append(conn)
            else:
                self._pool_total = max(0, self._pool_total - 1)
                try:
                    conn.close()
                except Exception:
                    pass
            self._pool_cond.notify()

    def _code_variants(self, code):
        c = str(code).upper().strip()
        variants = [c]
        if c.startswith("SH") or c.startswith("SZ"):
            raw = c[2:]
            if len(raw) == 6:
                suffix = ".SH" if c.startswith("SH") else ".SZ"
                variants.append(f"{raw}{suffix}")
        if "." not in c and len(c) == 6 and c.isdigit():
            suffix = ".SH" if c.startswith("6") else ".SZ"
            variants.append(f"{c}{suffix}")
        no_suffix = c.replace(".SH", "").replace(".SZ", "")
        if no_suffix and no_suffix != c:
            variants.append(no_suffix)
        out = []
        seen = set()
        for x in variants:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    def _normalize_df(self, df):
        if df is None or df.empty:
            return pd.DataFrame()
        if "trade_time" in df.columns and "dt" not in df.columns:
            df = df.rename(columns={"trade_time": "dt"})
        if "ts_code" in df.columns and "code" not in df.columns:
            df = df.rename(columns={"ts_code": "code"})
        required = ["code", "dt", "open", "high", "low", "close", "vol", "amount"]
        for c in required:
            if c not in df.columns:
                return pd.DataFrame()
        df["dt"] = pd.to_datetime(df["dt"], errors="coerce")
        for c in ["open", "high", "low", "close", "vol", "amount"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["dt", "open", "high", "low", "close"])
        df = df.sort_values("dt").drop_duplicates(subset=["dt"]).reset_index(drop=True)
        return df[["code", "dt", "open", "high", "low", "close", "vol", "amount"]]

    def _normalize_for_upsert(self, df):
        norm = self._normalize_df(df)
        if norm.empty:
            return pd.DataFrame()
        norm["code"] = norm["code"].astype(str).str.upper()
        norm["trade_time"] = pd.to_datetime(norm["dt"], errors="coerce")
        norm = norm.dropna(subset=["trade_time"])
        return norm[["code", "trade_time", "open", "high", "low", "close", "vol", "amount"]].copy()

    def _safe_name(self, name):
        t = str(name or "").strip()
        if not t:
            return ""
        if not re.match(r"^[A-Za-z0-9_]+$", t):
            return ""
        return t

    def _qualified_table(self, table):
        return f"\"{self.schema}\".\"{table}\""

    def _resolve_table_name(self, interval):
        cfg = ConfigLoader.reload()
        key_map = {
            "1min": "data_provider.postgres_table_1min",
            "5min": "data_provider.postgres_table_5min",
            "10min": "data_provider.postgres_table_10min",
            "15min": "data_provider.postgres_table_15min",
            "30min": "data_provider.postgres_table_30min",
            "60min": "data_provider.postgres_table_60min",
            "D": "data_provider.postgres_table_day",
        }
        cfg_name = self._safe_name(cfg.get(key_map.get(interval, ""), ""))
        if cfg_name:
            return cfg_name
        return self._table_defaults.get(interval, "")

    def _fetch_rows_paged(self, conn, table, code, start_time, end_time):
        offset = 0
        chunks = []
        sql = (
            f"SELECT code, trade_time AS dt, open, high, low, close, vol, amount "
            f"FROM {self._qualified_table(table)} "
            f"WHERE code = %s AND trade_time >= %s AND trade_time <= %s "
            f"ORDER BY trade_time ASC LIMIT %s OFFSET %s"
        )
        while True:
            with conn.cursor() as cursor:
                cursor.execute(sql, (code, start_time, end_time, self.page_size, offset))
                rows = cursor.fetchall() or []
            if not rows:
                break
            chunks.append(pd.DataFrame(rows))
            if len(rows) < self.page_size:
                break
            offset += self.page_size
        if not chunks:
            return pd.DataFrame()
        return pd.concat(chunks, ignore_index=True)

    def _query_range(self, code, start_time, end_time, interval):
        table = self._resolve_table_name(interval)
        if not table:
            self.last_error = f"未配置 {interval} 对应的 PostgreSQL 表名"
            return pd.DataFrame()
        conn = self._acquire_connection()
        if conn is None:
            return pd.DataFrame()
        broken = False
        try:
            for c in self._code_variants(code):
                raw_df = self._fetch_rows_paged(conn, table, c, start_time, end_time)
                if raw_df is not None and not raw_df.empty:
                    return self._normalize_df(raw_df)
            return pd.DataFrame()
        except Exception as e:
            broken = True
            self.last_error = f"PostgreSQL 查询失败: {e}"
            return pd.DataFrame()
        finally:
            self._release_connection(conn, broken=broken)

    def check_connectivity(self, code):
        conn = self._acquire_connection()
        if conn is None:
            return False, self.last_error or "PostgreSQL 连接失败"
        broken = False
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 AS ok")
                cursor.fetchone()
            table = self._resolve_table_name("1min")
            if not table:
                return True, "ok_no_data"
            now = datetime.now()
            start_time = now - timedelta(days=3)
            sql = (
                f"SELECT 1 AS ok FROM {self._qualified_table(table)} "
                f"WHERE code = %s AND trade_time >= %s AND trade_time <= %s LIMIT 1"
            )
            for c in self._code_variants(code):
                with conn.cursor() as cursor:
                    cursor.execute(sql, (c, start_time, now))
                    row = cursor.fetchone()
                if row:
                    return True, "ok"
            return True, "ok_no_data"
        except Exception as e:
            broken = True
            self.last_error = f"PostgreSQL 连通性检查失败: {e}"
            return False, self.last_error
        finally:
            self._release_connection(conn, broken=broken)

    def fetch_minute_data(self, code, start_time, end_time):
        return self._query_range(code, start_time, end_time, "1min")

    def fetch_daily_data(self, code, start_time, end_time):
        return self._query_range(code, start_time, end_time, "D")

    def fetch_kline_data_strict(self, code, start_time, end_time, interval="1min"):
        iv = str(interval or "1min")
        return self._query_range(code, start_time, end_time, iv)

    def fetch_kline_data(self, code, start_time, end_time, interval="1min"):
        iv = str(interval or "1min")
        if iv == "1min":
            return self.fetch_minute_data(code, start_time, end_time)
        if iv == "D":
            daily = self.fetch_daily_data(code, start_time, end_time)
            if not daily.empty:
                return daily
            minute = self.fetch_minute_data(code, start_time, end_time)
            return Indicators.resample(minute, "D") if not minute.empty else pd.DataFrame()
        tf_df = self._query_range(code, start_time, end_time, iv)
        if not tf_df.empty:
            return tf_df
        minute = self.fetch_minute_data(code, start_time, end_time)
        if minute.empty:
            return pd.DataFrame()
        return Indicators.resample(minute, iv)

    def get_latest_bar(self, code):
        table = self._resolve_table_name("1min")
        if not table:
            self.last_error = "未配置 postgres_table_1min"
            return None
        conn = self._acquire_connection()
        if conn is None:
            return None
        broken = False
        sql = (
            f"SELECT code, trade_time AS dt, open, high, low, close, vol, amount "
            f"FROM {self._qualified_table(table)} WHERE code = %s ORDER BY trade_time DESC LIMIT 1"
        )
        try:
            for c in self._code_variants(code):
                with conn.cursor() as cursor:
                    cursor.execute(sql, (c,))
                    row = cursor.fetchone()
                if row:
                    df = self._normalize_df(pd.DataFrame([row]))
                    if df.empty:
                        continue
                    r = df.iloc[-1]
                    return {
                        "code": str(r["code"]),
                        "dt": r["dt"],
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "vol": float(r["vol"]),
                        "amount": float(r["amount"]),
                    }
        except Exception as e:
            broken = True
            self.last_error = f"PostgreSQL latest 查询失败: {e}"
            return None
        finally:
            self._release_connection(conn, broken=broken)
        return None

    def upsert_kline_data(self, df, interval="1min", batch_size=2000):
        table = self._resolve_table_name(str(interval or "1min"))
        if not table:
            self.last_error = f"未配置 {interval} 对应的 PostgreSQL 表名"
            return 0
        norm = self._normalize_for_upsert(df)
        if norm.empty:
            return 0
        rows = [
            (
                str(r["code"]),
                pd.to_datetime(r["trade_time"]).to_pydatetime(),
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                float(r["vol"]),
                float(r["amount"]),
            )
            for _, r in norm.iterrows()
        ]
        conn = self._acquire_connection()
        if conn is None:
            return 0
        broken = False
        written = 0
        sql = (
            f"INSERT INTO {self._qualified_table(table)} (code, trade_time, open, high, low, close, vol, amount) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (code, trade_time) DO UPDATE SET "
            f"open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close, "
            f"vol=EXCLUDED.vol, amount=EXCLUDED.amount"
        )
        try:
            step = max(1, int(batch_size or 2000))
            for i in range(0, len(rows), step):
                chunk = rows[i:i + step]
                with conn.cursor() as cursor:
                    cursor.executemany(sql, chunk)
                conn.commit()
                written += len(chunk)
        except Exception as e:
            broken = True
            try:
                conn.rollback()
            except Exception:
                pass
            err_text = str(e)
            if "no unique or exclusion constraint matching the ON CONFLICT specification" in err_text:
                self.last_error = (
                    f"PostgreSQL 写入缓存失败: {e}。"
                    f"请为 {self._qualified_table(table)} 增加唯一约束 (code, trade_time)"
                )
            else:
                self.last_error = f"PostgreSQL 写入缓存失败: {e}"
            return 0
        finally:
            self._release_connection(conn, broken=broken)
        return written
