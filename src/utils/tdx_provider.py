import os
import platform
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from src.utils.config_loader import ConfigLoader
from src.utils.indicators import Indicators


class TdxProvider:
    """
    纯 Mootdx 数据提供器（不依赖 pytdx）。
    统一输出字段: code, dt, open, high, low, close, vol, amount
    """

    def __init__(self, host=None, port=None, nodes=None, tdxdir=None):
        cfg = ConfigLoader.reload()
        self.last_error = ""
        self.mootdx_market = str(cfg.get("data_provider.tdx_market", "std") or "std").strip() or "std"
        self.host = str(host or cfg.get("data_provider.tdx_host", "119.147.212.81") or "119.147.212.81").strip() or "119.147.212.81"
        self.port = int(port or cfg.get("data_provider.tdx_port", 7709) or 7709)
        explicit_dir = str(tdxdir or "").strip()
        self.tdxdir = explicit_dir if explicit_dir else self._resolve_tdxdir(cfg)
        self._cache_enabled = bool(cfg.get("data_provider.local_cache_enabled", True))
        cache_dir = str(cfg.get("data_provider.local_cache_dir", "D:/jin-ce-zhi-suan/data/history/cache") or "D:/jin-ce-zhi-suan/data/history/cache")
        base_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(base_dir))
        self._cache_dir = cache_dir if os.path.isabs(cache_dir) else os.path.join(project_root, cache_dir)
        os.makedirs(self._cache_dir, exist_ok=True)
        self._reader = None
        self._quotes = None
        self.runtime_platform = str(platform.system() or "").strip().lower() or os.name
        self.provider_mode = "local_vipdoc" if self._has_valid_tdxdir() else "network_mirror"

    def _candidate_quote_servers(self):
        out = []
        seen = set()

        def _push(h, p):
            host = str(h or "").strip()
            if not host:
                return
            try:
                port = int(p or 7709)
            except Exception:
                port = 7709
            key = f"{host}:{port}"
            if key in seen:
                return
            seen.add(key)
            out.append((host, port))

        _push(self.host, self.port)
        cfg = ConfigLoader.reload()
        raw_nodes = str(cfg.get("data_provider.tdx_node_list", "") or "").strip()
        for item in [x.strip() for x in raw_nodes.split(",") if x.strip()]:
            if ":" in item:
                h, p = item.rsplit(":", 1)
                _push(h, p)
            else:
                _push(item, 7709)
        try:
            import mootdx.consts as consts  # type: ignore

            hosts = getattr(consts, "HQ_HOSTS", []) or []
            for row in hosts:
                if not isinstance(row, (list, tuple)) or len(row) < 3:
                    continue
                _push(row[1], row[2])
                if len(out) >= 8:
                    break
        except Exception:
            pass
        return out or [("119.147.212.81", 7709)]

    def _resolve_tdxdir(self, cfg):
        env_dir = str(os.environ.get("TDX_DIR", "") or "").strip()
        cfg_dir = str(cfg.get("data_provider.tdxdir", "") or cfg.get("data_provider.tdx_dir", "") or "").strip()
        raw = env_dir or cfg_dir
        if not raw:
            return ""
        return os.path.normpath(raw)

    def _has_valid_tdxdir(self):
        p = str(self.tdxdir or "").strip()
        if not p:
            return False
        try:
            return os.path.isdir(os.path.join(p, "vipdoc"))
        except Exception:
            return False

    def _cache_file_path(self, code, interval="1min"):
        safe_code = str(code).upper().replace(".", "_")
        return os.path.join(self._cache_dir, f"tdx_{safe_code}_{interval}.csv")

    def describe_mode(self):
        has_vipdoc = self._has_valid_tdxdir()
        return {
            "platform": self.runtime_platform,
            "provider_mode": self.provider_mode,
            "has_vipdoc": has_vipdoc,
            "tdxdir": str(self.tdxdir or "").strip(),
            "cache_dir": self._cache_dir,
        }

    def _normalize_symbol(self, code):
        c = str(code or "").strip().upper()
        if c.endswith(".SH") or c.endswith(".SZ"):
            return c
        if len(c) == 6 and c.isdigit():
            return f"{c}.SH" if c.startswith(("5", "6", "9")) else f"{c}.SZ"
        return c

    def _raw_symbol(self, code):
        sym = self._normalize_symbol(code)
        if "." in sym:
            return sym.split(".", 1)[0]
        return sym

    def _symbol_file_hints(self, code):
        sym = self._normalize_symbol(code)
        raw = self._raw_symbol(code)
        exch = "sh" if sym.endswith(".SH") else "sz"
        root = str(self.tdxdir or "").strip()
        day_path = os.path.join(root, "vipdoc", exch, "lday", f"{exch}{raw}.day")
        min1_path = os.path.join(root, "vipdoc", exch, "minline", f"{exch}{raw}.lc1")
        return day_path, min1_path

    def _import_mootdx(self):
        try:
            from mootdx.reader import Reader  # type: ignore
            from mootdx.quotes import Quotes  # type: ignore
            return Reader, Quotes
        except Exception as e:
            self.last_error = f"mootdx 未安装或导入失败: {e}"
            return None, None

    def _create_reader(self):
        Reader, _ = self._import_mootdx()
        if Reader is None:
            return None
        if not self._has_valid_tdxdir():
            self.last_error = "tdxdir 未配置或无效（需指向包含 vipdoc 的通达信目录）"
            return None
        kwargs = {"market": self.mootdx_market}
        kwargs["tdxdir"] = str(self.tdxdir)
        return Reader.factory(**kwargs)

    def _create_quotes(self, server=None, bestip=False):
        _, Quotes = self._import_mootdx()
        if Quotes is None:
            return None
        kwargs = {"market": self.mootdx_market, "timeout": 6}
        if bool(bestip):
            kwargs["bestip"] = True
        if server and isinstance(server, (list, tuple)) and len(server) >= 2:
            kwargs["server"] = (str(server[0]), int(server[1]))
        return Quotes.factory(**kwargs)

    def _ensure_reader(self):
        if self._reader is not None:
            return self._reader
        try:
            self._reader = self._create_reader()
        except Exception as e:
            self.last_error = f"mootdx Reader 初始化失败: {e}"
            self._reader = None
        return self._reader

    def _ensure_quotes(self):
        if self._quotes is not None:
            return self._quotes
        last_err = ""
        # 先走 bestip，保持与 tdxtest.py 一致的实时链路
        try:
            self._quotes = self._create_quotes(bestip=True)
            self.last_error = ""
            return self._quotes
        except Exception as e:
            last_err = str(e)
            self._quotes = None
        for server in self._candidate_quote_servers():
            try:
                self._quotes = self._create_quotes(server=server)
                self.host = str(server[0])
                self.port = int(server[1])
                self.last_error = ""
                return self._quotes
            except Exception as e:
                last_err = str(e)
                self._quotes = None
                continue
        self.last_error = f"mootdx Quotes 初始化失败: {last_err}" if last_err else "mootdx Quotes 初始化失败"
        return self._quotes

    def _normalize_ohlcv_df(self, df, code):
        if df is None or (hasattr(df, "empty") and bool(df.empty)):
            return pd.DataFrame()
        work = pd.DataFrame(df).copy()
        if work.empty:
            return pd.DataFrame()
        col_map = {
            "datetime": ["datetime", "dt", "trade_time", "date", "time", "日期", "时间"],
            "open": ["open", "OPEN", "开盘"],
            "high": ["high", "HIGH", "最高"],
            "low": ["low", "LOW", "最低"],
            "close": ["close", "CLOSE", "收盘", "price", "现价"],
            "vol": ["vol", "volume", "VOL", "成交量", "量"],
            "amount": ["amount", "turnover", "AMOUNT", "成交额", "额"],
            "code": ["code", "symbol", "ts_code", "股票代码"],
        }

        def _pick(cols):
            for c in cols:
                if c in work.columns:
                    return c
            return ""

        dt_col = _pick(col_map["datetime"])
        open_col = _pick(col_map["open"])
        high_col = _pick(col_map["high"])
        low_col = _pick(col_map["low"])
        close_col = _pick(col_map["close"])
        vol_col = _pick(col_map["vol"])
        amount_col = _pick(col_map["amount"])
        code_col = _pick(col_map["code"])

        if not dt_col and "date" in work.columns and "time" in work.columns:
            work["dt"] = pd.to_datetime(
                work["date"].astype(str).str.strip() + " " + work["time"].astype(str).str.strip(),
                errors="coerce",
            )
        elif dt_col:
            work["dt"] = pd.to_datetime(work[dt_col], errors="coerce")
        elif not work.index.empty:
            # mootdx Reader.daily 常把日期放在索引里
            idx_dt = pd.to_datetime(work.index, errors="coerce")
            if idx_dt.isna().all():
                return pd.DataFrame()
            work["dt"] = idx_dt
        else:
            return pd.DataFrame()

        work["open"] = pd.to_numeric(work[open_col], errors="coerce") if open_col else pd.NA
        work["high"] = pd.to_numeric(work[high_col], errors="coerce") if high_col else pd.NA
        work["low"] = pd.to_numeric(work[low_col], errors="coerce") if low_col else pd.NA
        work["close"] = pd.to_numeric(work[close_col], errors="coerce") if close_col else pd.NA
        work["vol"] = pd.to_numeric(work[vol_col], errors="coerce") if vol_col else 0.0
        work["amount"] = pd.to_numeric(work[amount_col], errors="coerce") if amount_col else 0.0
        if code_col:
            work["code"] = work[code_col].astype(str).str.upper()
        else:
            work["code"] = self._normalize_symbol(code)

        out = work[["code", "dt", "open", "high", "low", "close", "vol", "amount"]].copy()
        out = out.dropna(subset=["dt", "open", "high", "low", "close"])
        out = out.sort_values("dt").drop_duplicates(subset=["dt"]).reset_index(drop=True)
        return out

    def _load_cached_daily_data(self, code, start_time, end_time):
        if not self._cache_enabled:
            return pd.DataFrame(), False
        path = self._cache_file_path(code, "D")
        if not os.path.exists(path):
            return pd.DataFrame(), False
        try:
            df = pd.read_csv(path)
            df = self._normalize_ohlcv_df(df, code=code)
            if df.empty:
                return pd.DataFrame(), False
            full_coverage = df["dt"].min() <= start_time and df["dt"].max() >= end_time
            df_range = df[(df["dt"] >= start_time) & (df["dt"] <= end_time)].copy()
            return df_range, bool(full_coverage and not df_range.empty)
        except Exception:
            return pd.DataFrame(), False

    def _save_daily_cache(self, code, df):
        if not self._cache_enabled or df is None or df.empty:
            return
        path = self._cache_file_path(code, "D")
        try:
            df_save = self._normalize_ohlcv_df(df, code=code)
            if df_save.empty:
                return
            if os.path.exists(path):
                old_df = pd.read_csv(path)
                old_df = self._normalize_ohlcv_df(old_df, code=code)
                if not old_df.empty:
                    df_save = pd.concat([old_df, df_save], ignore_index=True)
                    df_save = self._normalize_ohlcv_df(df_save, code=code)
            df_save.to_csv(path, index=False, encoding="utf-8")
        except Exception:
            return

    def _load_cached_minute_data(self, code, start_time, end_time):
        if not self._cache_enabled:
            return pd.DataFrame(), False
        path = self._cache_file_path(code, "1min")
        if not os.path.exists(path):
            return pd.DataFrame(), False
        try:
            df = pd.read_csv(path)
            df = self._normalize_ohlcv_df(df, code=code)
            if df.empty:
                return pd.DataFrame(), False
            full_coverage = df["dt"].min() <= start_time and df["dt"].max() >= end_time
            df_range = df[(df["dt"] >= start_time) & (df["dt"] <= end_time)].copy()
            return df_range, bool(full_coverage and not df_range.empty)
        except Exception:
            return pd.DataFrame(), False

    def _save_minute_cache(self, code, df):
        if not self._cache_enabled or df is None or df.empty:
            return
        path = self._cache_file_path(code, "1min")
        try:
            df_save = self._normalize_ohlcv_df(df, code=code)
            if df_save.empty:
                return
            if os.path.exists(path):
                old_df = pd.read_csv(path)
                old_df = self._normalize_ohlcv_df(old_df, code=code)
                if not old_df.empty:
                    df_save = pd.concat([old_df, df_save], ignore_index=True)
                    df_save = self._normalize_ohlcv_df(df_save, code=code)
            df_save.to_csv(path, index=False, encoding="utf-8")
        except Exception:
            return

    def _reader_daily(self, raw_code):
        reader = self._ensure_reader()
        if reader is None:
            return pd.DataFrame()
        if hasattr(reader, "daily"):
            return reader.daily(symbol=raw_code)
        return pd.DataFrame()

    def _reader_minute(self, raw_code):
        reader = self._ensure_reader()
        if reader is None:
            return pd.DataFrame()
        for name in ["minute", "min", "mins", "minute_bars", "bars"]:
            if not hasattr(reader, name):
                continue
            fn = getattr(reader, name)
            try:
                return fn(symbol=raw_code)
            except TypeError:
                try:
                    return fn(raw_code)
                except Exception:
                    continue
            except Exception:
                continue
        return pd.DataFrame()

    def _quotes_bars(self, raw_code):
        quotes = self._ensure_quotes()
        if quotes is None:
            return pd.DataFrame()
        if not hasattr(quotes, "bars"):
            self.last_error = "mootdx Quotes 不支持 bars 接口"
            return pd.DataFrame()
        try:
            return quotes.bars(symbol=raw_code)
        except TypeError:
            return quotes.bars(raw_code)
        except Exception as e:
            self.last_error = f"mootdx Quotes.bars 失败: {e}"
            return pd.DataFrame()

    def _quotes_snapshot(self, raw_code):
        quotes = self._ensure_quotes()
        if quotes is None:
            return pd.DataFrame()
        if not hasattr(quotes, "quotes"):
            self.last_error = "mootdx Quotes 不支持 quotes 接口"
            return pd.DataFrame()
        variants = []
        code_raw = str(raw_code or "").strip()
        if code_raw:
            variants.append(code_raw)
        if len(code_raw) == 6 and code_raw.isdigit():
            variants.extend([f"sh{code_raw}", f"sz{code_raw}"])
        last_err = ""
        for sym in variants:
            try:
                df = quotes.quotes(symbol=sym)
            except TypeError:
                try:
                    df = quotes.quotes(sym)
                except Exception as e:
                    last_err = str(e)
                    continue
            except Exception as e:
                last_err = str(e)
                continue
            if df is not None and (not getattr(df, "empty", True)):
                self.last_error = ""
                return df
        if last_err:
            self.last_error = f"mootdx Quotes.quotes 失败: {last_err}"
        return pd.DataFrame()

    def _snapshot_time_to_dt(self, servertime):
        t = str(servertime or "").strip()
        if not t:
            return pd.Timestamp(datetime.now().replace(second=0, microsecond=0))
        if "." in t:
            t = t.split(".", 1)[0]
        if len(t) == 5:
            t = f"{t}:00"
        now = datetime.now()
        try:
            dt = pd.to_datetime(f"{now.strftime('%Y-%m-%d')} {t}", errors="coerce")
            if pd.isna(dt):
                return pd.Timestamp(now.replace(second=0, microsecond=0))
            return pd.Timestamp(dt).replace(second=0, microsecond=0)
        except Exception:
            return pd.Timestamp(now.replace(second=0, microsecond=0))

    def _snapshot_to_bar_df(self, snap_df, code):
        if snap_df is None or (hasattr(snap_df, "empty") and bool(snap_df.empty)):
            return pd.DataFrame()
        try:
            row = pd.DataFrame(snap_df).iloc[-1].to_dict()
        except Exception:
            return pd.DataFrame()
        close_v = pd.to_numeric(row.get("price", row.get("last_close", None)), errors="coerce")
        if pd.isna(close_v):
            return pd.DataFrame()
        open_v = pd.to_numeric(row.get("open", close_v), errors="coerce")
        high_v = pd.to_numeric(row.get("high", close_v), errors="coerce")
        low_v = pd.to_numeric(row.get("low", close_v), errors="coerce")
        vol_v = pd.to_numeric(row.get("vol", row.get("volume", 0.0)), errors="coerce")
        amt_v = pd.to_numeric(row.get("amount", 0.0), errors="coerce")
        out = pd.DataFrame(
            [
                {
                    "code": self._normalize_symbol(code),
                    "dt": self._snapshot_time_to_dt(row.get("servertime", "")),
                    "open": float(open_v if not pd.isna(open_v) else close_v),
                    "high": float(high_v if not pd.isna(high_v) else close_v),
                    "low": float(low_v if not pd.isna(low_v) else close_v),
                    "close": float(close_v),
                    "vol": float(vol_v if not pd.isna(vol_v) else 0.0),
                    "amount": float(amt_v if not pd.isna(amt_v) else 0.0),
                }
            ]
        )
        return self._normalize_ohlcv_df(out, code=code)

    def _snapshot_to_pseudo_current_minute_df(self, snap_df, code, anchor_dt):
        base = self._snapshot_to_bar_df(snap_df, code=code)
        if base is None or base.empty:
            return pd.DataFrame()
        dt_anchor = pd.to_datetime(anchor_dt, errors="coerce")
        if pd.isna(dt_anchor):
            dt_anchor = pd.Timestamp(datetime.now())
        dt_anchor = pd.Timestamp(dt_anchor).replace(second=0, microsecond=0)
        out = base.copy()
        out.loc[:, "dt"] = dt_anchor
        return self._normalize_ohlcv_df(out, code=code)

    def check_connectivity(self, code):
        raw_code = self._raw_symbol(code)
        snap = self._snapshot_to_bar_df(self._quotes_snapshot(raw_code), code=code)
        if not snap.empty:
            self.last_error = ""
            return True, "ok_rt"
        bars = self._quotes_bars(raw_code)
        df = self._normalize_ohlcv_df(bars, code=code)
        if not df.empty:
            self.last_error = ""
            return True, "ok"
        daily = self._reader_daily(raw_code)
        df_d = self._normalize_ohlcv_df(daily, code=code)
        if not df_d.empty:
            self.last_error = ""
            return True, "ok_local"
        cached_daily, _ = self._load_cached_daily_data(code, pd.Timestamp(datetime.now()) - pd.Timedelta(days=30), pd.Timestamp(datetime.now()))
        if not cached_daily.empty:
            self.last_error = ""
            return True, "ok_cache"
        # Quotes 可能因网络/节点不可用返回空；若本地 Reader 可初始化且目录有效，
        # 放行预检查，后续数据拉取阶段再按真实数据可得性判定。
        reader = self._ensure_reader()
        if reader is not None and self._has_valid_tdxdir():
            self.last_error = ""
            return True, "ok_reader"
        quotes = self._ensure_quotes()
        if quotes is not None:
            self.last_error = ""
            return True, "ok_network_mirror"
        if self.last_error:
            return False, self.last_error
        return False, "mootdx 连通性检查失败（bars/daily/cache均为空）"

    def fetch_minute_data(self, code, start_time, end_time):
        st = pd.to_datetime(start_time, errors="coerce")
        et = pd.to_datetime(end_time, errors="coerce")
        if pd.isna(st) or pd.isna(et) or st > et:
            self.last_error = "TDX时间参数无效"
            return pd.DataFrame()
        cached_df, cache_hit = self._load_cached_minute_data(code, st, et)
        if cache_hit:
            self.last_error = ""
            return cached_df
        raw_code = self._raw_symbol(code)

        df_snap = self._snapshot_to_bar_df(self._quotes_snapshot(raw_code), code=code)
        df_quote = self._normalize_ohlcv_df(self._quotes_bars(raw_code), code=code)
        df_reader = self._normalize_ohlcv_df(self._reader_minute(raw_code), code=code)
        parts = []
        if not cached_df.empty:
            parts.append(cached_df)
        if not df_reader.empty:
            parts.append(df_reader)
        if not df_snap.empty:
            parts.append(df_snap)
        if not df_quote.empty:
            parts.append(df_quote)
        if not parts:
            if self._has_valid_tdxdir():
                day_path, min1_path = self._symbol_file_hints(code)
                self.last_error = (
                    f"mootdx分钟线为空 code={self._normalize_symbol(code)}; "
                    f"本地文件检查 day_exists={os.path.exists(day_path)} lc1_exists={os.path.exists(min1_path)}; "
                    f"请在通达信客户端下载该标的历史数据后重试"
                )
            else:
                self.last_error = (
                    self.last_error
                    or f"mootdx分钟线为空 code={self._normalize_symbol(code)}；当前处于无vipdoc的网络镜像模式，请先扩大回测窗口触发本地缓存积累或检查节点连通性"
                )
            return pd.DataFrame()
        merged = pd.concat(parts, ignore_index=True)
        merged = self._normalize_ohlcv_df(merged, code=code)
        merged = merged[(merged["dt"] >= st) & (merged["dt"] <= et)].copy()
        if merged.empty:
            # lc1 缺失或快照时间不在请求窗口时，允许用 quotes 快照补一条“当前分钟伪1m”
            # 以避免实盘监控端无数据可展示。
            pseudo = self._snapshot_to_pseudo_current_minute_df(
                self._quotes_snapshot(raw_code),
                code=code,
                anchor_dt=min(pd.Timestamp(datetime.now()), et),
            )
            if pseudo is not None and (not pseudo.empty):
                pseudo = pseudo[(pseudo["dt"] >= st) & (pseudo["dt"] <= et)].copy()
                if not pseudo.empty:
                    self._save_minute_cache(code, pseudo)
                    self.last_error = ""
                    return pseudo
            if self._has_valid_tdxdir():
                day_path, min1_path = self._symbol_file_hints(code)
                self.last_error = (
                    f"mootdx分钟线为空 code={self._normalize_symbol(code)}; "
                    f"本地文件检查 day_exists={os.path.exists(day_path)} lc1_exists={os.path.exists(min1_path)}; "
                    f"请在通达信客户端下载该标的历史数据后重试"
                )
            else:
                self.last_error = self.last_error or f"mootdx分钟线为空 code={self._normalize_symbol(code)}"
            return pd.DataFrame()
        self._save_minute_cache(code, merged)
        self.last_error = ""
        return merged

    def fetch_kline_data(self, code, start_time, end_time, interval="1min"):
        iv = str(interval or "1min").strip()
        iv_low = iv.lower()
        if iv_low in {"d", "1d", "day", "daily"}:
            iv = "D"
        elif iv_low in {"1min", "5min", "10min", "15min", "30min", "60min"}:
            iv = iv_low
        else:
            iv = iv_low

        st = pd.to_datetime(start_time, errors="coerce")
        et = pd.to_datetime(end_time, errors="coerce")
        if pd.isna(st) or pd.isna(et) or st > et:
            self.last_error = "TDX时间参数无效"
            return pd.DataFrame()

        if iv == "1min":
            return self.fetch_minute_data(code, st, et)

        if iv == "D":
            cached_daily, cache_hit = self._load_cached_daily_data(code, st, et)
            if cache_hit:
                self.last_error = ""
                return cached_daily
            raw_code = self._raw_symbol(code)
            daily = self._normalize_ohlcv_df(self._reader_daily(raw_code), code=code)
            if not daily.empty:
                out = daily[(daily["dt"] >= st) & (daily["dt"] <= et)].copy()
                if not out.empty:
                    self._save_daily_cache(code, out)
                    self.last_error = ""
                    return out
            base = self.fetch_minute_data(code, st, et)
            if base.empty:
                return pd.DataFrame()
            out = Indicators.resample(base, "D")
            if not out.empty:
                self._save_daily_cache(code, out)
                self.last_error = ""
            return out

        base = self.fetch_minute_data(code, st, et)
        if base.empty:
            return pd.DataFrame()
        return Indicators.resample(base, iv)

    def get_latest_bar(self, code):
        raw_code = self._raw_symbol(code)
        quote_df = self._snapshot_to_bar_df(self._quotes_snapshot(raw_code), code=code)
        if quote_df.empty:
            quote_df = self._normalize_ohlcv_df(self._quotes_bars(raw_code), code=code)
        if quote_df.empty:
            now = datetime.now()
            quote_df = self.fetch_minute_data(code, now - pd.Timedelta(days=2), now)
        if quote_df.empty:
            return None
        row = quote_df.sort_values("dt").iloc[-1]
        self.last_error = ""
        return {
            "code": str(row["code"]),
            "dt": pd.to_datetime(row["dt"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "vol": float(row["vol"]),
            "amount": float(row["amount"]),
        }
