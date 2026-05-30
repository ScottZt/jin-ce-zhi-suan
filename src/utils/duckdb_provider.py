import os
import re
import sys
from datetime import datetime, timedelta

import pandas as pd

from src.utils.config_loader import ConfigLoader
from src.utils.indicators import Indicators


class DuckDbProvider:
    def __init__(self, db_path=None):
        cfg = ConfigLoader.reload()
        self.db_path = str(db_path or cfg.get("data_provider.duckdb_path", "")).strip()
        self.page_size = max(1000, int(cfg.get("data_provider.duckdb_query_page_size", 20000) or 20000))
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

    def _load_duckdb(self):
        try:
            import duckdb
            return duckdb
        except Exception as e:
            self.last_error = f"duckdb 未安装或导入失败: {e}"
            return None

    def _resolve_db_path(self):
        raw = str(self.db_path or "").strip()
        # 打包运行时强制使用内置 DuckDB 相对路径（忽略前台配置项），保证部署后路径稳定。
        if getattr(sys, "frozen", False):
            packaged_rel = os.path.join("databases", "duckdb", "quantifydata.duckdb")
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                packaged_in_meipass = os.path.join(meipass, packaged_rel)
                if os.path.exists(packaged_in_meipass):
                    return packaged_in_meipass
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            packaged_in_exe_dir = os.path.join(exe_dir, packaged_rel)
            if os.path.exists(packaged_in_exe_dir):
                return packaged_in_exe_dir
            # 兜底返回相对路径，便于日志中明确展示期望路径。
            return packaged_rel
        if not raw:
            return ""
        if raw == ":memory:" or os.path.isabs(raw) and os.path.exists(raw):
            return raw
        # 优先尝试当前工作目录
        if os.path.exists(raw):
            return raw
        # 打包模式下尝试 sys._MEIPASS（_internal 目录）
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidate = os.path.join(meipass, raw)
            if os.path.exists(candidate):
                return candidate
        # 尝试 exe 所在目录
        if getattr(sys, "frozen", False):
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            candidate = os.path.join(exe_dir, raw)
            if os.path.exists(candidate):
                return candidate
        root, ext = os.path.splitext(raw)
        if (not ext) and os.path.exists(f"{raw}.duckdb"):
            return f"{raw}.duckdb"
        return raw

    def _connect(self, read_only=True):
        resolved_path = self._resolve_db_path()
        if not resolved_path:
            self.last_error = "DuckDB 路径未配置"
            return None
        if resolved_path != ":memory:" and not os.path.exists(resolved_path):
            self.last_error = f"DuckDB 文件不存在: {resolved_path}"
            return None
        duckdb = self._load_duckdb()
        if duckdb is None:
            return None
        try:
            return duckdb.connect(database=resolved_path, read_only=bool(read_only))
        except Exception as e:
            err_text = str(e or "")
            # DuckDB 同进程下若已存在读写连接，再打开只读连接会报配置冲突；此时回退到读写连接复用同一文件。
            if bool(read_only) and "different configuration than existing connections" in err_text.lower():
                try:
                    conn = duckdb.connect(database=resolved_path, read_only=False)
                    self.last_error = ""
                    return conn
                except Exception as retry_error:
                    self.last_error = f"DuckDB 连接失败: {retry_error}"
                    return None
            self.last_error = f"DuckDB 连接失败: {e}"
            return None

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

    def _normalize_ts_series(self, values):
        ts = pd.to_datetime(values, errors="coerce")
        try:
            tz_obj = getattr(ts.dt, "tz", None)
            if tz_obj is not None:
                ts = ts.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
        except Exception:
            pass
        return ts

    def _normalize_ts_value(self, value):
        # 查询边界和结果集都统一成无时区时间，避免 pandas 比较时出现 tz-aware/tz-naive 冲突。
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return ts
        try:
            if getattr(ts, "tzinfo", None) is not None:
                ts = ts.tz_convert("Asia/Shanghai").tz_localize(None)
        except Exception:
            try:
                if getattr(ts, "tzinfo", None) is not None:
                    ts = ts.tz_localize(None)
            except Exception:
                pass
        return ts

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
        df["dt"] = self._normalize_ts_series(df["dt"])
        for c in ["open", "high", "low", "close", "vol", "amount"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["dt", "open", "high", "low", "close"])
        dedup_keys = ["dt"]
        if "code" in df.columns:
            dedup_keys = ["code", "dt"]
        # 多股票批量写入时必须按 code+dt 去重，不能只按时间去重，否则同一时刻不同股票会被误丢弃。
        df = df.sort_values(dedup_keys).drop_duplicates(subset=dedup_keys).reset_index(drop=True)
        return df[["code", "dt", "open", "high", "low", "close", "vol", "amount"]]

    def _normalize_for_upsert(self, df):
        norm = self._normalize_df(df)
        if norm.empty:
            return pd.DataFrame()
        norm["code"] = norm["code"].astype(str).str.upper()
        norm["trade_time"] = pd.to_datetime(norm["dt"], errors="coerce")
        norm = norm.dropna(subset=["trade_time"])
        return norm[["code", "trade_time", "open", "high", "low", "close", "vol", "amount"]].copy()

    def _safe_table_name(self, name):
        t = str(name or "").strip()
        if not t:
            return ""
        if not re.match(r"^[A-Za-z0-9_]+$", t):
            return ""
        return t

    def _quoted_table(self, table):
        return f'"{table}"'

    def _resolve_table_name(self, interval):
        cfg = ConfigLoader.reload()
        key_map = {
            "1min": "data_provider.duckdb_table_1min",
            "5min": "data_provider.duckdb_table_5min",
            "10min": "data_provider.duckdb_table_10min",
            "15min": "data_provider.duckdb_table_15min",
            "30min": "data_provider.duckdb_table_30min",
            "60min": "data_provider.duckdb_table_60min",
            "D": "data_provider.duckdb_table_day",
        }
        cfg_name = self._safe_table_name(cfg.get(key_map.get(interval, ""), ""))
        if cfg_name:
            return cfg_name
        return self._table_defaults.get(interval, "")

    def _query_time_text(self, value):
        dt = pd.to_datetime(value, errors="coerce")
        if pd.isna(dt):
            return str(value or "")
        try:
            if getattr(dt, "tzinfo", None) is not None:
                dt = dt.tz_localize(None)
        except Exception:
            pass
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _query_date_text(self, value):
        dt = pd.to_datetime(value, errors="coerce")
        if pd.isna(dt):
            text = str(value or "").strip()
            return text[:10] if len(text) >= 10 else text
        return dt.strftime("%Y-%m-%d")

    def _trade_time_parse_expr(self):
        # 不同表的 trade_time 可能是 VARCHAR 或 TIMESTAMPTZ，这里统一归一到可比较的日期表达式。
        return (
            "CAST(COALESCE("
            "TRY_CAST(trade_time AS TIMESTAMP WITH TIME ZONE), "
            "TRY_STRPTIME(CAST(trade_time AS VARCHAR), '%Y-%m-%d %H:%M:%S%z')"
            ") AS DATE)"
        )

    def _fetch_rows_paged(self, conn, table, code, start_time, end_time):
        start_date = self._query_date_text(start_time)
        end_date = self._query_date_text(end_time)
        parse_expr = self._trade_time_parse_expr()
        sql = (
            f"SELECT code, trade_time AS dt, open, high, low, close, vol, amount "
            f"FROM {self._quoted_table(table)} "
            f"WHERE code = ? "
            f"AND {parse_expr} >= CAST(? AS DATE) "
            f"AND {parse_expr} <= CAST(? AS DATE)"
        )
        frame = conn.execute(sql, [code, start_date, end_date]).fetchdf()
        if frame is None or frame.empty:
            return pd.DataFrame()
        return frame.reset_index(drop=True)


    def _query_range(self, code, start_time, end_time, interval):
        table = self._resolve_table_name(interval)
        if not table:
            self.last_error = f"未配置 {interval} 对应的 DuckDB 表名"
            return pd.DataFrame()
        conn = self._connect(read_only=True)
        if conn is None:
            return pd.DataFrame()
        try:
            for c in self._code_variants(code):
                raw_df = self._fetch_rows_paged(conn, table, c, start_time, end_time)
                if raw_df is not None and not raw_df.empty:
                    df = self._normalize_df(raw_df)
                    if df.empty:
                        continue
                    start_dt = self._normalize_ts_value(start_time)
                    end_dt = self._normalize_ts_value(end_time)
                    if not pd.isna(start_dt):
                        df = df[df["dt"] >= start_dt]
                    if not pd.isna(end_dt):
                        df = df[df["dt"] <= end_dt]
                    return df.reset_index(drop=True)
            return pd.DataFrame()
        except Exception as e:
            self.last_error = f"DuckDB 查询失败: {e}"
            return pd.DataFrame()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def check_connectivity(self, code):
        conn = self._connect(read_only=True)
        if conn is None:
            return False, self.last_error or "DuckDB 连接失败"
        try:
            conn.execute("SELECT 1 AS ok").fetchone()
            table = self._resolve_table_name("1min")
            if not table:
                return True, "ok_no_data"
            sql = (
                f"SELECT 1 AS ok FROM {self._quoted_table(table)} "
                f"WHERE code = ? LIMIT 1"
            )
            for c in self._code_variants(code):
                row = conn.execute(sql, [c]).fetchone()
                if row:
                    return True, "ok"
            return True, "ok_no_data"
        except Exception as e:
            self.last_error = f"DuckDB 连通性检查失败: {e}"
            return False, self.last_error
        finally:
            try:
                conn.close()
            except Exception:
                pass

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
            self.last_error = "未配置 duckdb_table_1min"
            return None
        conn = self._connect(read_only=True)
        if conn is None:
            return None
        sql = (
            f"SELECT code, trade_time AS dt, open, high, low, close, vol, amount "
            f"FROM {self._quoted_table(table)} WHERE code = ? ORDER BY CAST(trade_time AS VARCHAR) DESC LIMIT 1"
        )
        try:
            for c in self._code_variants(code):
                row = conn.execute(sql, [c]).fetchdf()
                if row is None or row.empty:
                    continue
                df = self._normalize_df(row)
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
            self.last_error = f"DuckDB latest 查询失败: {e}"
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return None

    def _delete_then_insert_rows(self, conn, table, rows):
        delete_sql = (
            f"DELETE FROM {self._quoted_table(table)} "
            f"WHERE code = ? AND CAST(trade_time AS VARCHAR) = ?"
        )
        insert_sql = (
            f"INSERT INTO {self._quoted_table(table)} (code, trade_time, open, high, low, close, vol, amount) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        delete_params = [
            (str(row[0]), self._query_time_text(row[1]))
            for row in rows
        ]
        conn.executemany(delete_sql, delete_params)
        conn.executemany(insert_sql, rows)

    def upsert_kline_data_with_conn(self, conn, df, interval="1min", batch_size=2000):
        # 复用同一连接批量写入，供历史同步串行写线程持续刷盘。
        # DuckDB 原生批量路径：register DataFrame 为临时视图 + 直接 INSERT。
        # 同步服务已在 _process_code_sync 中对目标库做过缺失判定，
        # 传入数据都是目标库不存在的行，无需 upsert / DELETE 等大表操作。
        table = self._resolve_table_name(str(interval or "1min"))
        if not table:
            self.last_error = f"未配置 {interval} 对应的 DuckDB 表名"
            return 0
        norm = self._normalize_for_upsert(df)
        if norm.empty:
            self.last_error = ""
            return 0

        # batch 内去重：多只股票的数据合并后可能含同 (code, trade_time) 的行
        norm = norm.drop_duplicates(subset=['code', 'trade_time'], keep='last').reset_index(drop=True)
        if norm.empty:
            self.last_error = ""
            return 0

        view_name = f"_upsert_tmp_{table}"
        written = 0
        total = len(norm)
        step = max(1, int(batch_size or 2000))
        tbl = self._quoted_table(table)

        for i in range(0, total, step):
            chunk = norm.iloc[i:i + step]
            try:
                conn.register(view_name, chunk)
                conn.execute(
                    f"INSERT INTO {tbl} "
                    f"(code, trade_time, open, high, low, close, vol, amount) "
                    f"SELECT code, trade_time, open, high, low, close, vol, amount "
                    f"FROM {view_name}"
                )
                written += len(chunk)
            except Exception as e:
                self.last_error = f"DuckDB 写入缓存失败: {e}"
                return 0
            finally:
                try:
                    conn.unregister(view_name)
                except Exception:
                    pass

        self.last_error = ""
        return written

    def upsert_kline_data(self, df, interval="1min", batch_size=2000):
        conn = self._connect(read_only=False)
        if conn is None:
            return 0
        try:
            return int(self._upsert_with_fallback(conn, df, interval=interval, batch_size=batch_size) or 0)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _upsert_with_fallback(self, conn, df, interval="1min", batch_size=2000):
        # 非同步路径独立调用时的 upsert 逻辑，需要处理重复数据。
        # 同步串行写线程走 upsert_kline_data_with_conn 直插即可。
        table = self._resolve_table_name(str(interval or "1min"))
        if not table:
            self.last_error = f"未配置 {interval} 对应的 DuckDB 表名"
            return 0
        norm = self._normalize_for_upsert(df)
        if norm.empty:
            self.last_error = ""
            return 0
        norm = norm.drop_duplicates(subset=['code', 'trade_time'], keep='last').reset_index(drop=True)
        if norm.empty:
            self.last_error = ""
            return 0

        view_name = f"_upsert_tmp_{table}"
        written = 0
        total = len(norm)
        step = max(1, int(batch_size or 2000))
        tbl = self._quoted_table(table)

        for i in range(0, total, step):
            chunk = norm.iloc[i:i + step]
            try:
                conn.register(view_name, chunk)
                # 先尝试 ON CONFLICT upsert（小表和有索引表语义正确）
                conn.execute(
                    f"INSERT INTO {tbl} "
                    f"(code, trade_time, open, high, low, close, vol, amount) "
                    f"SELECT code, trade_time, open, high, low, close, vol, amount "
                    f"FROM {view_name} "
                    f"ON CONFLICT (code, trade_time) DO UPDATE SET "
                    f"open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, "
                    f"close=EXCLUDED.close, vol=EXCLUDED.vol, amount=EXCLUDED.amount"
                )
                written += len(chunk)
            except Exception:
                # ON CONFLICT 失败（无索引 / OOM 等），回退到 DELETE-then-INSERT
                try:
                    conn.execute(
                        f"DELETE FROM {tbl} "
                        f"WHERE EXISTS ("
                        f"  SELECT 1 FROM {view_name} v"
                        f"  WHERE {tbl}.code = v.code"
                        f"  AND CAST({tbl}.trade_time AS VARCHAR)"
                        f"      = CAST(v.trade_time AS VARCHAR)"
                        f")"
                    )
                    conn.execute(
                        f"INSERT INTO {tbl} "
                        f"(code, trade_time, open, high, low, close, vol, amount) "
                        f"SELECT code, trade_time, open, high, low, close, vol, amount "
                        f"FROM {view_name}"
                    )
                    written += len(chunk)
                except Exception:
                    # DELETE 也失败（大表 OOM / 类型不匹配），最终回退到直插。
                    # 同步路径已通过缺失判定过滤掉重复行，这里仅处理极端情况。
                    try:
                        conn.execute(
                            f"INSERT INTO {tbl} "
                            f"(code, trade_time, open, high, low, close, vol, amount) "
                            f"SELECT code, trade_time, open, high, low, close, vol, amount "
                            f"FROM {view_name}"
                        )
                        written += len(chunk)
                    except Exception as e3:
                        self.last_error = f"DuckDB 写入缓存失败: {e3}"
                        return 0
            finally:
                try:
                    conn.unregister(view_name)
                except Exception:
                    pass

        self.last_error = ""
        return written
