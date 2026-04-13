# src/utils/tushare_provider.py
import tushare as ts
import pandas as pd
from datetime import datetime, timedelta, timezone
import os
import time
from src.utils.config_loader import ConfigLoader
from src.utils.indicators import Indicators
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

class TushareProvider:
    """
    Tushare Pro Data Provider
    """
    def __init__(self, token=None, event_callback=None):
        # Default to a placeholder token if none provided. User must replace this.
        self.token = token
        self.event_callback = event_callback
        self.last_error = ""
        cfg = ConfigLoader.reload()
        self._cache_enabled = bool(cfg.get("data_provider.local_cache_enabled", True))
        cache_dir = str(cfg.get("data_provider.local_cache_dir", "data/history/cache") or "data/history/cache")
        base_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(base_dir))
        self._cache_dir = cache_dir if os.path.isabs(cache_dir) else os.path.join(project_root, cache_dir)
        os.makedirs(self._cache_dir, exist_ok=True)
        self._replay_day = str(os.getenv("OPENCLAW_TUSHARE_REPLAY_DAY", "") or "").strip()
        self._replay_speed = float(os.getenv("OPENCLAW_TUSHARE_REPLAY_SPEED", "0") or 0.0)
        self._replay_enabled = bool(self._replay_day and self._replay_speed > 0)
        self._replay_state = {}
        self._rt_min_cache_ttl_sec = max(1.0, float(cfg.get("data_provider.tushare_rt_min_cache_ttl_sec", 8) or 8))
        self._rt_min_max_calls_per_min = max(1, int(cfg.get("data_provider.tushare_rt_min_max_calls_per_min", 50) or 50))
        self._rt_min_backoff_sec = max(5.0, float(cfg.get("data_provider.tushare_rt_min_backoff_sec", 25) or 25))
        self._rt_min_recent_calls = []
        self._rt_min_block_until = 0.0
        self._rt_min_cache = {}
        self._cn_tz = ZoneInfo("Asia/Shanghai") if ZoneInfo is not None else timezone(timedelta(hours=8))
        self.last_error = ""
        import tushare.pro.client as client
        client.DataApi._DataApi__http_url = "http://tushare.xyz"
        if self.token:
            ts.set_token(self.token)
            self.pro = ts.pro_api()
        else:
            self.pro = None
            self.last_error = "tushare_token 未配置"
            print("⚠️ Warning: Tushare Token not provided. Please initialize with a valid token.")

    def _emit_system_event(self, msg, code="", source="tushare"):
        if not self.event_callback:
            return
        payload = {
            "msg": str(msg or ""),
            "stock": str(code or ""),
            "source": str(source or "tushare")
        }
        try:
            loop = __import__("asyncio").get_running_loop()
            loop.create_task(self.event_callback("system", payload))
        except Exception:
            return

    def _is_live_console_trace_enabled(self):
        return bool(self.event_callback)

    def _summarize_df_result(self, df):
        if df is None or df.empty:
            return "rows=0"
        dt_min = ""
        dt_max = ""
        close_last = ""
        try:
            dt_min = str(pd.to_datetime(df["dt"], errors="coerce").min())
            dt_max = str(pd.to_datetime(df["dt"], errors="coerce").max())
        except Exception:
            dt_min = ""
            dt_max = ""
        try:
            close_last = float(df.iloc[-1].get("close", 0.0) or 0.0)
            close_last = f"{close_last:.4f}"
        except Exception:
            close_last = ""
        extra = []
        if dt_min:
            extra.append(f"start={dt_min}")
        if dt_max:
            extra.append(f"end={dt_max}")
        if close_last:
            extra.append(f"last_close={close_last}")
        extra_text = (" " + " ".join(extra)) if extra else ""
        return f"rows={len(df)}{extra_text}"

    def _trace_fetch(self, interface, code, start_time=None, end_time=None, result=None, err=""):
        if not self._is_live_console_trace_enabled():
            return
        range_text = ""
        if start_time is not None or end_time is not None:
            st = str(start_time) if start_time is not None else "-"
            et = str(end_time) if end_time is not None else "-"
            range_text = f" range={st}->{et}"
        if isinstance(result, pd.DataFrame):
            result_text = self._summarize_df_result(result)
        elif isinstance(result, dict):
            dt_text = str(result.get("dt", "") or "")
            close_text = str(result.get("close", "") or "")
            result_text = f"payload dt={dt_text} close={close_text}"
        else:
            result_text = str(result or "")
        status = "ok" if not err else f"fail err={err}"
        print(f"📡 Tushare拉取 interface={interface} code={code}{range_text} status={status} result={result_text}")

    def _is_rt_min_rate_limit_error(self, err):
        text = str(err or "").lower()
        if not text:
            return False
        return ("每分钟最多访问" in text) or ("rate limit" in text) or ("too many requests" in text) or ("频率" in text and "限制" in text)

    def _is_auth_error(self, err):
        raw = str(err or "").strip()
        if not raw:
            return False
        t = raw.lower()
        if "token" in t:
            return True
        if ("permission" in t) or ("unauthorized" in t) or ("forbidden" in t) or ("auth" in t):
            return True
        if ("权限" in raw) or ("认证" in raw) or ("授权" in raw):
            return True
        if ("积分" in raw) or ("点数" in raw):
            return True
        return False

    def _cleanup_rt_min_recent_calls(self, now_mono=None):
        now_v = float(now_mono if now_mono is not None else time.monotonic())
        self._rt_min_recent_calls = [x for x in self._rt_min_recent_calls if (now_v - float(x)) < 60.0]

    def _consume_rt_min_quota(self):
        now_mono = time.monotonic()
        self._cleanup_rt_min_recent_calls(now_mono=now_mono)
        if now_mono < float(self._rt_min_block_until or 0.0):
            wait_sec = max(0.0, float(self._rt_min_block_until) - now_mono)
            return False, f"rt_min_backoff_active remain={wait_sec:.1f}s"
        if len(self._rt_min_recent_calls) >= int(self._rt_min_max_calls_per_min):
            return False, f"rt_min_rate_guard active calls={len(self._rt_min_recent_calls)}/{int(self._rt_min_max_calls_per_min)}"
        self._rt_min_recent_calls.append(now_mono)
        return True, ""

    def _get_rt_min_cached_df(self, code, max_age_sec=None):
        code_u = str(code).upper()
        state = self._rt_min_cache.get(code_u)
        if not isinstance(state, dict):
            return pd.DataFrame()
        ts_mono = float(state.get("ts", 0.0) or 0.0)
        df = state.get("df")
        if not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame()
        ttl = float(max_age_sec if max_age_sec is not None else self._rt_min_cache_ttl_sec)
        if (time.monotonic() - ts_mono) > max(0.1, ttl):
            return pd.DataFrame()
        return df.copy()

    def _set_rt_min_cached_df(self, code, df):
        code_u = str(code).upper()
        norm = self._normalize_minutes_df(df)
        if norm.empty:
            return
        self._rt_min_cache[code_u] = {
            "ts": time.monotonic(),
            "df": norm.copy()
        }

    def _cache_file_path(self, code, interval="1min"):
        safe_code = str(code).upper().replace(".", "_")
        return os.path.join(self._cache_dir, f"tushare_{safe_code}_{interval}.csv")

    def _load_replay_df(self, code):
        code_u = str(code).upper()
        state = self._replay_state.setdefault(code_u, {})
        if "df" in state:
            return state["df"]
        if not self.pro:
            state["df"] = pd.DataFrame()
            return state["df"]
        start_str = f"{self._replay_day} 09:00:00"
        end_str = f"{self._replay_day} 15:30:00"
        try:
            df = self.pro.stk_mins(ts_code=code_u, freq='1min', start_date=start_str, end_date=end_str)
            df = self._normalize_minutes_df(df)
            if not df.empty:
                df = df[df["dt"].dt.strftime("%Y-%m-%d") == self._replay_day].copy()
                df = self._normalize_minutes_df(df)
            state["df"] = df
            return df
        except Exception as e:
            self.last_error = f"replay_load_failed code={code_u} day={self._replay_day} err={e}"
            state["df"] = pd.DataFrame()
            return state["df"]

    def _get_replay_bar(self, code):
        if not self._replay_enabled:
            return None
        code_u = str(code).upper()
        state = self._replay_state.setdefault(code_u, {})
        df = self._load_replay_df(code_u)
        if df is None or df.empty:
            self.last_error = f"replay_no_data code={code_u} day={self._replay_day}"
            return None
        if "start_real_ts" not in state:
            state["start_real_ts"] = time.time()
        elapsed = max(0.0, time.time() - float(state["start_real_ts"]))
        bars_per_sec = max(0.01, float(self._replay_speed) / 60.0)
        idx = int(elapsed * bars_per_sec)
        if idx >= len(df):
            idx = len(df) - 1
        row = df.iloc[idx]
        payload = {
            "code": str(row.get("code", code_u)),
            "dt": pd.to_datetime(row.get("dt")),
            "open": float(row.get("open", 0.0) or 0.0),
            "high": float(row.get("high", 0.0) or 0.0),
            "low": float(row.get("low", 0.0) or 0.0),
            "close": float(row.get("close", 0.0) or 0.0),
            "vol": float(row.get("vol", 0.0) or 0.0),
            "amount": float(row.get("amount", 0.0) or 0.0)
        }
        self._append_rt_today_bar(code_u, payload)
        if idx >= len(df) - 1:
            self.last_error = f"replay_finished code={code_u} day={self._replay_day}"
        else:
            self.last_error = ""
        return payload

    def _rt_today_cache_file_path(self, code):
        safe_code = str(code).upper().replace(".", "_")
        return os.path.join(self._cache_dir, f"tushare_{safe_code}_rt_today.csv")

    def _to_shanghai_naive_ts(self, value):
        ts_val = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts_val):
            return ts_val
        try:
            tz_obj = getattr(ts_val, "tz", None)
            if tz_obj is not None:
                try:
                    ts_val = ts_val.tz_convert(self._cn_tz)
                except Exception:
                    ts_val = ts_val.tz_localize(self._cn_tz)
                try:
                    ts_val = ts_val.tz_localize(None)
                except Exception:
                    pass
        except Exception:
            pass
        return ts_val

    def _to_naive_ts(self, value):
        return self._to_shanghai_naive_ts(value)

    def _normalize_intraday_market_ts(self, value):
        ts_val = self._to_shanghai_naive_ts(value)
        if pd.isna(ts_val):
            return ts_val
        if self._is_cn_trading_minutes(ts_val):
            return ts_val
        minus_8h = ts_val - pd.Timedelta(hours=8)
        if self._is_cn_trading_minutes(minus_8h):
            return minus_8h
        plus_8h = ts_val + pd.Timedelta(hours=8)
        if self._is_cn_trading_minutes(plus_8h):
            return plus_8h
        return ts_val

    def _normalize_minutes_df(self, df):
        if df is None or df.empty:
            return pd.DataFrame()
        work = df.copy()
        if "trade_time" in work.columns and "dt" not in work.columns:
            work = work.rename(columns={"trade_time": "dt"})
        if "ts_code" in work.columns and "code" not in work.columns:
            work = work.rename(columns={"ts_code": "code"})
        required_cols = ["code", "open", "high", "low", "close", "vol", "amount", "dt"]
        for c in required_cols:
            if c not in work.columns:
                return pd.DataFrame()
        work["dt"] = pd.to_datetime(work["dt"])
        for c in ["open", "high", "low", "close", "vol", "amount"]:
            work[c] = pd.to_numeric(work[c], errors="coerce")
        work = work.dropna(subset=["dt", "open", "high", "low", "close"])
        work = work.drop_duplicates(subset=["dt"]).sort_values("dt").reset_index(drop=True)
        return work[["code", "dt", "open", "high", "low", "close", "vol", "amount"]]

    def _load_cached_minute_data(self, code, start_time, end_time):
        if not self._cache_enabled:
            return pd.DataFrame(), False
        path = self._cache_file_path(code, "1min")
        if not os.path.exists(path):
            return pd.DataFrame(), False
        try:
            st = self._to_naive_ts(start_time)
            et = self._to_naive_ts(end_time)
            if pd.isna(st) or pd.isna(et):
                return pd.DataFrame(), False
            df = pd.read_csv(path)
            if "dt" in df.columns:
                df["dt"] = pd.to_datetime(df["dt"])
            df = self._normalize_minutes_df(df)
            if df.empty:
                return pd.DataFrame(), False
            df["dt"] = pd.to_datetime(df["dt"], errors="coerce").apply(self._normalize_intraday_market_ts)
            df = df.dropna(subset=["dt"])
            full_coverage = df["dt"].min() <= st and df["dt"].max() >= et
            df_range = df[(df["dt"] >= st) & (df["dt"] <= et)].copy()
            return df_range, bool(full_coverage and not df_range.empty)
        except Exception:
            return pd.DataFrame(), False

    def _save_minute_cache(self, code, df):
        if not self._cache_enabled or df is None or df.empty:
            return
        path = self._cache_file_path(code, "1min")
        try:
            df_save = self._normalize_minutes_df(df)
            if df_save.empty:
                return
            if os.path.exists(path):
                old_df = pd.read_csv(path)
                if "dt" in old_df.columns:
                    old_df["dt"] = pd.to_datetime(old_df["dt"])
                old_df = self._normalize_minutes_df(old_df)
                if not old_df.empty:
                    df_save = pd.concat([old_df, df_save], ignore_index=True)
                    df_save = self._normalize_minutes_df(df_save)
            df_save.to_csv(path, index=False, encoding="utf-8")
        except Exception:
            return

    def _load_rt_today_cache(self, code, day_text=None):
        path = self._rt_today_cache_file_path(code)
        if not os.path.exists(path):
            return pd.DataFrame()
        try:
            df = pd.read_csv(path)
            if "dt" in df.columns:
                df["dt"] = pd.to_datetime(df["dt"], errors="coerce")
            df = self._normalize_minutes_df(df)
            if df.empty:
                return pd.DataFrame()
            df["dt"] = pd.to_datetime(df["dt"], errors="coerce").apply(self._normalize_intraday_market_ts)
            df = df.dropna(subset=["dt"])
            if day_text:
                df = df[df["dt"].dt.strftime("%Y-%m-%d") == str(day_text)]
            return self._normalize_minutes_df(df)
        except Exception as e:
            self.last_error = f"load_rt_today_cache_failed code={code} err={e}"
            return pd.DataFrame()

    def _save_rt_today_cache(self, code, df):
        if df is None or df.empty:
            return
        path = self._rt_today_cache_file_path(code)
        try:
            work = self._normalize_minutes_df(df)
            if work.empty:
                return
            work["dt"] = pd.to_datetime(work["dt"], errors="coerce").apply(self._normalize_intraday_market_ts)
            work = work.dropna(subset=["dt"])
            latest_day = work["dt"].max().strftime("%Y-%m-%d")
            work = work[work["dt"].dt.strftime("%Y-%m-%d") == latest_day]
            work.to_csv(path, index=False, encoding="utf-8")
        except Exception as e:
            self.last_error = f"save_rt_today_cache_failed code={code} err={e}"

    def _append_rt_today_bar(self, code, bar):
        try:
            if not isinstance(bar, dict):
                return
            row = self._normalize_minutes_df(pd.DataFrame([{
                "code": str(bar.get("code", code)).upper(),
                "dt": bar.get("dt"),
                "open": bar.get("open"),
                "high": bar.get("high"),
                "low": bar.get("low"),
                "close": bar.get("close"),
                "vol": bar.get("vol"),
                "amount": bar.get("amount")
            }]))
            if row.empty:
                return
            day_text = row["dt"].max().strftime("%Y-%m-%d")
            old = self._load_rt_today_cache(code, day_text=day_text)
            merged = pd.concat([old, row], ignore_index=True) if not old.empty else row
            merged = self._normalize_minutes_df(merged)
            self._save_rt_today_cache(code, merged)
        except Exception as e:
            self.last_error = f"append_rt_today_bar_failed code={code} err={e}"

    def get_today_rt_cache_stats(self, code):
        day_text = self._replay_day if self._replay_enabled else datetime.now().strftime("%Y-%m-%d")
        df = self._load_rt_today_cache(code, day_text=day_text)
        if df.empty:
            return {"bars": 0, "last_dt": ""}
        return {
            "bars": int(len(df)),
            "last_dt": str(df["dt"].max())
        }

    def _is_cn_trading_minutes(self, dt_obj):
        dt = pd.to_datetime(dt_obj, errors="coerce")
        if pd.isna(dt):
            return False
        if int(dt.weekday()) >= 5:
            return False
        hm = int(dt.hour) * 60 + int(dt.minute)
        return (570 <= hm <= 690) or (780 <= hm <= 900)

    def _should_use_rt_min(self, start_time, end_time):
        now_ts = self._to_naive_ts(datetime.now())
        st = self._to_naive_ts(start_time)
        et = self._to_naive_ts(end_time)
        if pd.isna(st) or pd.isna(et):
            return False
        if (now_ts - et) > pd.Timedelta(minutes=30):
            return False
        if et.date() != now_ts.date():
            return False
        return self._is_cn_trading_minutes(et)

    def _fetch_rt_min(self, code, start_time=None, end_time=None):
        if not self.pro:
            return pd.DataFrame()
        cached = self._get_rt_min_cached_df(code)
        allowed, quota_msg = self._consume_rt_min_quota()
        if not allowed:
            if not cached.empty:
                self._trace_fetch("rt_min_cache", code, start_time=start_time, end_time=end_time, result=cached, err=quota_msg)
                return cached
            self.last_error = quota_msg
            self._trace_fetch("rt_min", code, start_time=start_time, end_time=end_time, result=pd.DataFrame(), err=quota_msg)
            return pd.DataFrame()
        try:
            df = self.pro.rt_min(ts_code=code)
        except Exception as e:
            err_text = str(e)
            if self._is_rt_min_rate_limit_error(err_text):
                self._rt_min_block_until = time.monotonic() + float(self._rt_min_backoff_sec)
                if not cached.empty:
                    self.last_error = f"rt_min_rate_limited use_cache_ttl={int(self._rt_min_cache_ttl_sec)}s"
                    self._trace_fetch("rt_min_cache", code, start_time=start_time, end_time=end_time, result=cached, err=err_text)
                    return cached
            if self._is_auth_error(err_text):
                self.last_error = err_text
            else:
                self.last_error = f"rt_min_failed code={code} err={err_text}"
            self._trace_fetch("rt_min", code, start_time=start_time, end_time=end_time, result=pd.DataFrame(), err=err_text)
            return pd.DataFrame()
        if df is None or df.empty:
            if not cached.empty:
                self._trace_fetch("rt_min_cache", code, start_time=start_time, end_time=end_time, result=cached, err="empty")
                return cached
            self._trace_fetch("rt_min", code, start_time=start_time, end_time=end_time, result=pd.DataFrame(), err="empty")
            return pd.DataFrame()
        work = df.copy()
        if "time" in work.columns and "dt" not in work.columns:
            today = datetime.now().strftime("%Y-%m-%d")
            work["dt"] = pd.to_datetime(today + " " + work["time"].astype(str), errors="coerce")
        elif "trade_time" in work.columns and "dt" not in work.columns:
            work["dt"] = pd.to_datetime(work["trade_time"], errors="coerce")
        if "ts_code" in work.columns and "code" not in work.columns:
            work["code"] = work["ts_code"]
        if "vol" not in work.columns and "volume" in work.columns:
            work["vol"] = work["volume"]
        if "amount" not in work.columns and "turnover" in work.columns:
            work["amount"] = work["turnover"]
        if "open" not in work.columns and "close" in work.columns:
            work["open"] = work["close"]
        if "high" not in work.columns and "close" in work.columns:
            work["high"] = work["close"]
        if "low" not in work.columns and "close" in work.columns:
            work["low"] = work["close"]
        work = self._normalize_minutes_df(work)
        if work.empty:
            return pd.DataFrame()
        st = self._to_naive_ts(start_time) if start_time is not None else None
        et = self._to_naive_ts(end_time) if end_time is not None else None
        work["dt"] = pd.to_datetime(work["dt"], errors="coerce").apply(self._normalize_intraday_market_ts)
        work = work.dropna(subset=["dt"])
        if st is not None and (not pd.isna(st)):
            work = work[work["dt"] >= st]
        if et is not None and (not pd.isna(et)):
            work = work[work["dt"] <= et]
        out = work.reset_index(drop=True)
        self._set_rt_min_cached_df(code, out)
        self._trace_fetch("rt_min", code, start_time=start_time, end_time=end_time, result=out)
        return out

    def set_token(self, token):
        self.token = token
        import tushare.pro.client as client
        client.DataApi._DataApi__http_url = "http://tushare.xyz"
        ts.set_token(self.token)
        self.pro = ts.pro_api()
        self.last_error = ""

    def get_latest_bar(self, code):
        """
        Get the latest real-time quote for a stock.
        Returns a dict in the standard format.
        """
        if self._replay_enabled:
            replay_bar = self._get_replay_bar(code)
            if replay_bar is not None:
                return replay_bar
        try:
            try:
                df = self._fetch_rt_min(code)
                if df is not None and not df.empty:
                    row = df.sort_values("dt").iloc[-1]
                    dt = self._to_naive_ts(row.get('dt'))
                    if pd.isna(dt):
                        raise ValueError("rt_min invalid dt")

                    payload = {
                        'code': str(row.get('code', code)),
                        'dt': dt,
                        'open': float(row.get('open', 0.0) or 0.0),
                        'high': float(row.get('high', 0.0) or 0.0),
                        'low': float(row.get('low', 0.0) or 0.0),
                        'close': float(row.get('close', 0.0) or 0.0),
                        'vol': float(row.get('vol', 0.0) or 0.0),
                        'amount': float(row.get('amount', 0.0) or 0.0)
                    }
                    self._append_rt_today_bar(code, payload)
                    self.last_error = ""
                    return payload
            except Exception as e_rt:
                err_text = str(e_rt)
                if self._is_auth_error(err_text):
                    self.last_error = err_text
                else:
                    self.last_error = f"rt_min_failed code={code} err={err_text}"
                self._trace_fetch("rt_min", code, result={}, err=str(e_rt))
            # ts.get_realtime_quotes expects code without .SZ or .SH, e.g. '300274'
            rt_code = str(code).split(".")[0]
            df = ts.get_realtime_quotes(rt_code)    
            if df is None or df.empty:
                end_date = datetime.now().strftime("%Y%m%d")
                start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
                df_daily = self.pro.daily(ts_code=code, start_date=start_date, end_date=end_date)
                if df_daily is not None and not df_daily.empty:
                    row = df_daily.iloc[0]
                    payload = {
                        'code': code,
                        'dt': pd.to_datetime(row['trade_date']),
                        'open': float(row['open']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'close': float(row['close']),
                        'vol': float(row['vol']) * 100,
                        'amount': float(row['amount']) * 1000
                    }
                    return payload
                return None
            if len(df.index) <= 0:
                self.last_error = f"get_realtime_quotes_empty code={code}"
                return None
            row = df.iloc[0]
            date_str = str(row.get('date', '') or '')
            time_str = str(row.get('time', '') or '')
            if not date_str or not time_str:
                self.last_error = f"get_realtime_quotes_missing_time_fields code={code}"
                return None
            dt_str = f"{date_str} {time_str}"
            dt = self._to_naive_ts(dt_str)
            if pd.isna(dt):
                self.last_error = f"get_realtime_quotes_invalid_dt code={code} raw={dt_str}"
                return None
            payload = {
                'code': code,
                'dt': dt,
                'open': float(row.get('open', 0.0) or 0.0),
                'high': float(row.get('high', 0.0) or 0.0),
                'low': float(row.get('low', 0.0) or 0.0),
                'close': float(row.get('price', row.get('close', 0.0)) or 0.0),
                'vol': float(row.get('volume', row.get('vol', 0.0)) or 0.0),
                'amount': float(row.get('amount', 0.0) or 0.0)
            }
            self._append_rt_today_bar(code, payload)
            self.last_error = ""
            return payload
        except Exception as e:
            err_text = str(e)
            if self._is_auth_error(err_text):
                self.last_error = err_text
            else:
                self.last_error = f"get_latest_bar_failed code={code} err={err_text}"
            try:
                end_date = datetime.now().strftime("%Y%m%d")
                start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
                df_daily = self.pro.daily(ts_code=code, start_date=start_date, end_date=end_date)
                if df_daily is not None and not df_daily.empty:
                    row = df_daily.iloc[0]
                    payload = {
                        'code': code,
                        'dt': pd.to_datetime(row['trade_date']),
                        'open': float(row['open']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'close': float(row['close']),
                        'vol': float(row['vol']) * 100,
                        'amount': float(row['amount']) * 1000
                    }
                    self.last_error = ""
                    return payload
            except Exception as e_daily:
                self.last_error = f"{self.last_error} | daily_fallback_failed err={e_daily}"
            return None

    def fetch_minute_data(self, code, start_time, end_time):
        """
        Fetch historical minute data via Tushare Pro (requires points/permission).
        Interface: pro.stk_mins or standard ts.pro_bar
        """
        if not self.pro:
            self.last_error = "tushare_token 未配置"
            return pd.DataFrame()
        start_time = self._to_naive_ts(start_time)
        end_time = self._to_naive_ts(end_time)
        if pd.isna(start_time) or pd.isna(end_time):
            self.last_error = "时间参数无效"
            return pd.DataFrame()
        start_time = pd.to_datetime(start_time, errors="coerce")
        end_time = pd.to_datetime(end_time, errors="coerce")
        if pd.isna(start_time) or pd.isna(end_time) or start_time > end_time:
            self.last_error = f"fetch_minute_data_invalid_range code={code} start={start_time} end={end_time}"
            return pd.DataFrame()
        today_text = datetime.now().strftime("%Y-%m-%d")
        today_start = pd.to_datetime(f"{today_text} 00:00:00")
        include_today = end_time >= today_start
        cached_df, cache_hit = self._load_cached_minute_data(code, start_time, end_time)
        if cache_hit and (not include_today):
            return cached_df
        hist_end = min(end_time, today_start - timedelta(seconds=1))
        hist_cached = pd.DataFrame()
        if not cached_df.empty:
            hist_cached = cached_df[cached_df["dt"] < today_start].copy()
        hist_df = hist_cached.copy()
        if hist_end >= start_time:
            fetch_start = start_time
            if not hist_cached.empty:
                fetch_start = max(fetch_start, hist_cached["dt"].max() + timedelta(minutes=1))
            if fetch_start <= hist_end:
                start_str = fetch_start.strftime("%Y-%m-%d %H:%M:%S")
                end_str = hist_end.strftime("%Y-%m-%d %H:%M:%S")
                try:
                    df_hist_remote = self.pro.stk_mins(ts_code=code, freq='1min', start_date=start_str, end_date=end_str)
                    df_hist_remote = self._normalize_minutes_df(df_hist_remote)
                    self._trace_fetch("stk_mins", code, start_time=start_str, end_time=end_str, result=df_hist_remote, err="empty" if df_hist_remote.empty else "")
                    if df_hist_remote.empty:
                        self.last_error = f"stk_mins_empty code={code} range={start_str}->{end_str}"
                    hist_df = pd.concat([hist_cached, df_hist_remote], ignore_index=True) if not hist_cached.empty else df_hist_remote
                    hist_df = self._normalize_minutes_df(hist_df)
                except Exception as e:
                    err_text = str(e)
                    if self._is_auth_error(err_text):
                        self.last_error = err_text
                    else:
                        self.last_error = f"fetch_minute_data_failed code={code} range={start_str}->{end_str} err={err_text}"
                    self._trace_fetch("stk_mins", code, start_time=start_str, end_time=end_str, result=pd.DataFrame(), err=str(e))
                    hist_df = hist_cached if not hist_cached.empty else pd.DataFrame()
        today_df = pd.DataFrame()
        if include_today:
            _ = self.get_latest_bar(code)
            today_df = self._load_rt_today_cache(code, day_text=today_text)
            if not today_df.empty:
                today_df = today_df[(today_df["dt"] >= start_time) & (today_df["dt"] <= end_time)].copy()
        parts = []
        if hist_df is not None and (not hist_df.empty):
            parts.append(hist_df)
        if today_df is not None and (not today_df.empty):
            parts.append(today_df)
        if not parts and (cached_df is not None) and (not cached_df.empty):
            parts.append(cached_df)
        if not parts:
            return pd.DataFrame()
        out = self._normalize_minutes_df(pd.concat(parts, ignore_index=True))
        out = out[(out["dt"] >= start_time) & (out["dt"] <= end_time)].copy()
        self._save_minute_cache(code, out)
        self.last_error = ""
        return out

    def _fetch_stk_mins(self, code, start_time, end_time, freq="1min"):
        if not self.pro:
            return pd.DataFrame()
        start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            df = self.pro.stk_mins(
                ts_code=code,
                freq=str(freq),
                start_date=start_str,
                end_date=end_str
            )
            out = self._normalize_minutes_df(df)
            self._trace_fetch("stk_mins", code, start_time=start_str, end_time=end_str, result=out, err="empty" if out.empty else "")
            return out
        except Exception as e:
            err_text = str(e)
            if self._is_auth_error(err_text):
                self.last_error = err_text
            else:
                self.last_error = f"_fetch_stk_mins_failed code={code} freq={freq} err={err_text}"
            self._trace_fetch("stk_mins", code, start_time=start_str, end_time=end_str, result=pd.DataFrame(), err=str(e))
            return pd.DataFrame()

    def fetch_kline_data(self, code, start_time, end_time, interval="1min"):
        tf = str(interval or "1min")
        if tf == "1min":
            return self.fetch_minute_data(code, start_time, end_time)
        if tf == "D":
            df_d = self.fetch_daily_data(code, start_time, end_time)
            if not df_d.empty:
                return df_d
            df_1m = self.fetch_minute_data(code, start_time, end_time)
            return Indicators.resample(df_1m, "D") if not df_1m.empty else pd.DataFrame()
        freq_map = {
            "5min": "5min",
            "10min": "10min",
            "15min": "15min",
            "30min": "30min",
            "60min": "60min"
        }
        if tf in freq_map:
            if self._should_use_rt_min(start_time, end_time):
                df_live_1m = self.fetch_minute_data(code, start_time, end_time)
                if not df_live_1m.empty:
                    df_live_tf = Indicators.resample(df_live_1m, tf)
                    if not df_live_tf.empty:
                        return df_live_tf
            df_tf = self._fetch_stk_mins(code, start_time, end_time, freq=freq_map[tf])
            if not df_tf.empty:
                return df_tf
        df_1m = self.fetch_minute_data(code, start_time, end_time)
        if df_1m.empty:
            return pd.DataFrame()
        return Indicators.resample(df_1m, tf)

    def fetch_daily_data(self, code, start_time, end_time):
        if not self.pro:
            return pd.DataFrame()
        start_str = start_time.strftime("%Y%m%d")
        end_str = end_time.strftime("%Y%m%d")
        try:
            df = self.pro.daily(ts_code=code, start_date=start_str, end_date=end_str)
            if df is None or df.empty:
                return pd.DataFrame()
            work = df.copy()
            if "trade_date" not in work.columns:
                return pd.DataFrame()
            work["dt"] = pd.to_datetime(work["trade_date"], format="%Y%m%d", errors="coerce")
            work["open"] = pd.to_numeric(work.get("open"), errors="coerce")
            work["high"] = pd.to_numeric(work.get("high"), errors="coerce")
            work["low"] = pd.to_numeric(work.get("low"), errors="coerce")
            work["close"] = pd.to_numeric(work.get("close"), errors="coerce")
            work["vol"] = pd.to_numeric(work.get("vol"), errors="coerce") * 100.0
            work["amount"] = pd.to_numeric(work.get("amount"), errors="coerce") * 1000.0
            work["code"] = code
            work = work.dropna(subset=["dt", "open", "high", "low", "close"])
            work = work.sort_values("dt").drop_duplicates(subset=["dt"]).reset_index(drop=True)
            return work[["code", "dt", "open", "high", "low", "close", "vol", "amount"]]
        except Exception as e:
            err_text = str(e)
            if self._is_auth_error(err_text):
                self.last_error = err_text
            else:
                self.last_error = f"fetch_daily_data_failed code={code} range={start_str}->{end_str} err={err_text}"
            return pd.DataFrame()

    def check_connectivity(self, code):
        if not self.pro:
            return False, "tushare_token 未配置"
        now = datetime.now()
        start_time = now - timedelta(days=3)
        try:
            _ = self.pro.stk_mins(
                ts_code=str(code).upper(),
                freq='1min',
                start_date=start_time.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=now.strftime("%Y-%m-%d %H:%M:%S")
            )
            self.last_error = ""
            return True, "ok"
        except Exception as e:
            err_text = str(e)
            self.last_error = err_text
            return False, err_text
