# src/utils/tushare_provider.py
import tushare as ts
import pandas as pd
from datetime import datetime, timedelta
import os
import time
from src.utils.config_loader import ConfigLoader
from src.utils.indicators import Indicators

class TushareProvider:
    """
    Tushare Pro Data Provider
    """
    def __init__(self, token=None):
        # Default to a placeholder token if none provided. User must replace this.
        self.token = token
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
        self.last_error = ""
        import tushare.pro.client as client
        client.DataApi._DataApi__http_url = "http://tushare.xyz"
        if self.token:
            ts.set_token(self.token)
            self.pro = ts.pro_api()
        else:
            self.pro = None
            print("⚠️ Warning: Tushare Token not provided. Please initialize with a valid token.")

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
            df = pd.read_csv(path)
            if "dt" in df.columns:
                df["dt"] = pd.to_datetime(df["dt"])
            df = self._normalize_minutes_df(df)
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

    def set_token(self, token):
        self.token = token
        import tushare.pro.client as client
        client.DataApi._DataApi__http_url = "http://tushare.xyz"
        ts.set_token(self.token)
        self.pro = ts.pro_api()

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
            # Optimized: Use Tushare Pro 'rt_min' if available (requires permissions)
            # This is cleaner than scraping.
            # Example: pro.rt_min(ts_code='600000.SH')
            
            # Normalize code (rt_min expects ts_code like 600000.SH)
            
            # Try rt_min first (Official Real-time Minute API)
            try:
                df = self.pro.rt_min(ts_code=code)
                if df is not None and not df.empty:
                    row = df.iloc[0]
                    time_val = str(row.get('time', '')).strip()
                    if not time_val:
                        raise ValueError("rt_min missing time")
                    if ("-" in time_val) and (":" in time_val):
                        dt = pd.to_datetime(time_val, errors='coerce')
                    else:
                        today = datetime.now().strftime("%Y-%m-%d")
                        dt = pd.to_datetime(f"{today} {time_val}", errors='coerce')
                    if pd.isna(dt):
                        raise ValueError(f"rt_min invalid time: {time_val}")
                    
                    payload = {
                        'code': str(row.get('ts_code', code)),
                        'dt': dt,
                        'open': float(row['open']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'close': float(row['close']),
                        'vol': float(row['vol']),
                        'amount': float(row['amount'])
                    }
                    self._append_rt_today_bar(code, payload)
                    self.last_error = ""
                    return payload
            except Exception as e_rt:
                self.last_error = f"rt_min_failed code={code} err={e_rt}"

            # Fallback to get_realtime_quotes (Scraping)
            df = ts.get_realtime_quotes(code)
            if df is None or df.empty:
                # Fallback to pro.daily for latest close (not real-time but better than nothing)
                end_date = datetime.now().strftime("%Y%m%d")
                start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
                df_daily = self.pro.daily(ts_code=code, start_date=start_date, end_date=end_date)
                
                if df_daily is not None and not df_daily.empty:
                    row = df_daily.iloc[0] # Latest
                    payload = {
                        'code': code,
                        'dt': pd.to_datetime(row['trade_date']),
                        'open': float(row['open']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'close': float(row['close']),
                        'vol': float(row['vol']) * 100, # Hand is unit? No, usually vol is lot or share. Tushare daily vol is "hand" (100 shares)? No, it says "Vol (Hand)". Let's assume hand.
                        'amount': float(row['amount']) * 1000 # Amount is usually in thousands?
                    }
                    return payload
                return None
                
            if len(df.index) <= 0:
                self.last_error = f"get_realtime_quotes_empty code={code}"
                return None
            row = df.iloc[0]
            
            # Normalize format
            # Tushare RT columns: name, open, pre_close, price, high, low, bid, ask, volume, amount, date, time
            
            # Combine date and time
            # Note: get_realtime_quotes returns different columns based on source
            # For tushare < 1.3, it uses sina.
            
            date_str = str(row.get('date', '') or '')
            time_str = str(row.get('time', '') or '')
            if not date_str or not time_str:
                self.last_error = f"get_realtime_quotes_missing_time_fields code={code}"
                return None
            dt_str = f"{date_str} {time_str}"
            dt = pd.to_datetime(dt_str, errors='coerce')
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
            self.last_error = f"get_latest_bar_failed code={code} err={e}"
            # print(f"Error fetching Tushare RT data: {e}")
            # Try fallback inside exception if get_realtime_quotes crashed (e.g. network issue or parsing issue)
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
                    print(f"DEBUG: Requesting Tushare data for {code} ({start_str} - {end_str})")
                    df_hist_remote = self.pro.stk_mins(ts_code=code, freq='1min', start_date=start_str, end_date=end_str)
                    df_hist_remote = self._normalize_minutes_df(df_hist_remote)
                    if df_hist_remote.empty:
                        self.last_error = f"stk_mins_empty code={code} range={start_str}->{end_str}"
                    hist_df = pd.concat([hist_cached, df_hist_remote], ignore_index=True) if not hist_cached.empty else df_hist_remote
                    hist_df = self._normalize_minutes_df(hist_df)
                except Exception as e:
                    self.last_error = f"fetch_minute_data_failed code={code} range={start_str}->{end_str} err={e}"
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
            return self._normalize_minutes_df(df)
        except Exception as e:
            self.last_error = f"_fetch_stk_mins_failed code={code} freq={freq} err={e}"
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
            self.last_error = f"fetch_daily_data_failed code={code} range={start_str}->{end_str} err={e}"
            return pd.DataFrame()
