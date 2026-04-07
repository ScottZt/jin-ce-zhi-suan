# src/utils/data_provider.py
import requests
import pandas as pd
from datetime import datetime, timedelta
import time
import os
from src.utils.config_loader import ConfigLoader
from src.utils.indicators import Indicators

class DataProvider:
    """
    Data Provider using external API for multi-timeframe K-line data.
    """
    def __init__(self, api_key=None, base_url=None):
        cfg = ConfigLoader.reload()
        self.api_key = api_key or cfg.get("data_provider.default_api_key", "")
        self.base_url = (base_url or cfg.get("data_provider.default_api_url", "")).rstrip("/")
        self.headers = {"X-API-Key": self.api_key} if self.api_key else {}
        self.last_error = ""
        self._cache_enabled = bool(cfg.get("data_provider.local_cache_enabled", True))
        cache_dir = str(cfg.get("data_provider.local_cache_dir", "data/history/cache") or "data/history/cache")
        base_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(base_dir))
        self._cache_dir = cache_dir if os.path.isabs(cache_dir) else os.path.join(project_root, cache_dir)
        os.makedirs(self._cache_dir, exist_ok=True)

    def _header_candidates(self):
        if not self.api_key:
            return [{}]
        return [
            {"X-API-Key": self.api_key, "Authorization": f"Bearer {self.api_key}"},
            {"X-API-Key": self.api_key},
            {"Authorization": f"Bearer {self.api_key}"},
            {"authorization": f"Bearer {self.api_key}"},
            {"x-api-key": self.api_key},
        ]

    def _request_get(self, path, params, timeout=15):
        last_msg = ""
        for h in self._header_candidates():
            try:
                response = requests.get(f"{self.base_url}{path}", headers=h, params=params, timeout=timeout)
                if response.status_code == 200:
                    return response
                detail = response.text[:240]
                if not last_msg:
                    last_msg = f"{path} {response.status_code} params={params} headers={list(h.keys())} detail={detail}"
            except Exception as e:
                if not last_msg:
                    last_msg = f"{path} request error params={params} err={e}"
        self.last_error = last_msg
        return None

    def _request_post(self, path, payload, timeout=20):
        last_msg = ""
        for h in self._header_candidates():
            headers = dict(h or {})
            headers["Content-Type"] = "application/json"
            try:
                response = requests.post(f"{self.base_url}{path}", headers=headers, json=payload, timeout=timeout)
                if response.status_code == 200:
                    return response
                detail = response.text[:240]
                if not last_msg:
                    last_msg = f"{path} {response.status_code} headers={list(headers.keys())} detail={detail}"
            except Exception as e:
                if not last_msg:
                    last_msg = f"{path} request error err={e}"
        self.last_error = last_msg
        return None

    def _extract_rows(self, payload):
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        for key in ["rows", "data", "items", "list", "result"]:
            val = payload.get(key)
            if isinstance(val, list):
                return val
        return []

    def _code_variants(self, code):
        c = str(code).upper()
        variants = [c]
        if c.startswith("SH") or c.startswith("SZ"):
            raw = c[2:]
            if len(raw) == 6:
                suffix = ".SH" if c.startswith("SH") else ".SZ"
                variants.append(f"{raw}{suffix}")
        if "." not in c and len(c) == 6 and c.isdigit():
            suffix = ".SH" if c.startswith("6") else ".SZ"
            variants.append(f"{c}{suffix}")
        seen = set()
        out = []
        for v in variants:
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out

    def _normalize_minutes_df(self, df):
        if df.empty:
            return df
        time_col = None
        for col in ["trade_time", "dt", "time", "datetime", "timestamp"]:
            if col in df.columns:
                time_col = col
                break
        if not time_col:
            return pd.DataFrame()
        if time_col != "dt":
            df = df.rename(columns={time_col: "dt"})
        if "ts_code" in df.columns and "code" not in df.columns:
            df = df.rename(columns={"ts_code": "code"})
        required_cols = ["code", "open", "high", "low", "close", "vol", "amount", "dt"]
        for c in required_cols:
            if c not in df.columns:
                return pd.DataFrame()
        df["dt"] = pd.to_datetime(df["dt"])
        for c in ["open", "high", "low", "close", "vol", "amount"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["dt", "open", "high", "low", "close"])
        df = df.drop_duplicates(subset=["dt"]).sort_values("dt").reset_index(drop=True)
        return df[["code", "dt", "open", "high", "low", "close", "vol", "amount"]]

    def _normalize_ohlcv_df(self, df):
        return self._normalize_minutes_df(df)

    def _cache_file_path(self, code, interval="1min"):
        safe_code = str(code).upper().replace(".", "_")
        return os.path.join(self._cache_dir, f"default_{safe_code}_{interval}.csv")

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
            df_save = self._normalize_minutes_df(df.copy())
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

    def check_connectivity(self, code):
        now = datetime.now()
        start_time = now - timedelta(days=3)
        params_list = []
        for c in self._code_variants(code):
            params_list.append({
                "code": c,
                "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                "limit": 1
            })
        for params in params_list:
            resp = self._request_get("/market/minutes", params, timeout=10)
            if resp is not None:
                return True, "ok"
        msg = self.last_error or "default 数据源连通性检查失败"
        return False, msg

    def _interval_table_name(self, interval):
        mapping = {
            "1min": "dat_1mins",
            "5min": "dat_5mins",
            "10min": "dat_10mins",
            "15min": "dat_15mins",
            "30min": "dat_30mins",
            "60min": "dat_60mins",
            "D": "dat_day"
        }
        return mapping.get(interval)

    def _fetch_daily_data(self, code, start_time, end_time):
        rows = []
        for c in self._code_variants(code):
            params = {
                "codes": c,
                "start_date": start_time.strftime("%Y-%m-%d"),
                "end_date": end_time.strftime("%Y-%m-%d"),
                "limit": 10000
            }
            response = self._request_get("/market/daily-bars/range", params, timeout=20)
            if response is None:
                continue
            rows = self._extract_rows(response.json())
            if rows:
                break
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        if "trade_date" in df.columns and "dt" not in df.columns:
            df = df.rename(columns={"trade_date": "dt"})
        return self._normalize_ohlcv_df(df)

    def _fetch_table_timeframe_data(self, code, start_time, end_time, interval):
        table = self._interval_table_name(interval)
        if not table:
            return pd.DataFrame()
        filters = [
            f"code:eq:{code}",
            f"trade_time:gte:{start_time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"trade_time:lte:{end_time.strftime('%Y-%m-%d %H:%M:%S')}"
        ]
        candidate_filters = [
            filters,
            [f"code:eq:{code.replace('.SH','').replace('.SZ','')}", f"trade_time:gte:{start_time.strftime('%Y-%m-%d %H:%M:%S')}", f"trade_time:lte:{end_time.strftime('%Y-%m-%d %H:%M:%S')}"]
        ]
        for fset in candidate_filters:
            all_rows = []
            offset = 0
            page_limit = 10000
            while True:
                params = {
                    "limit": page_limit,
                    "offset": offset,
                    "order_by": "trade_time",
                    "order_dir": "asc",
                    "filter": fset
                }
                response = self._request_get(f"/tables/{table}/rows", params, timeout=25)
                if response is None:
                    all_rows = []
                    break
                rows = self._extract_rows(response.json())
                if not rows:
                    break
                all_rows.extend(rows)
                if len(rows) < page_limit:
                    break
                offset += page_limit
            if not all_rows:
                continue
            df = pd.DataFrame(all_rows)
            if "trade_time" in df.columns and "dt" not in df.columns:
                df = df.rename(columns={"trade_time": "dt"})
            return self._normalize_ohlcv_df(df)
        return pd.DataFrame()

    def fetch_kline_data(self, code, start_time, end_time, interval="1min"):
        interval = str(interval)
        if interval == "1min":
            return self.fetch_minute_data(code, start_time, end_time)
        if interval == "D":
            df_d = self._fetch_daily_data(code, start_time, end_time)
            if not df_d.empty:
                return df_d
            df_1m = self.fetch_minute_data(code, start_time, end_time)
            return Indicators.resample(df_1m, "D") if not df_1m.empty else pd.DataFrame()
        df_tf = self._fetch_table_timeframe_data(code, start_time, end_time, interval)
        if not df_tf.empty:
            return df_tf
        df_1m = self.fetch_minute_data(code, start_time, end_time)
        if df_1m.empty:
            return pd.DataFrame()
        return Indicators.resample(df_1m, interval)

    def fetch_minute_data(self, code, start_time, end_time):
        """
        Fetch 1-minute data for a single stock within a time range.
        Handles pagination automatically.
        """
        cached_df, cache_hit = self._load_cached_minute_data(code, start_time, end_time)
        if cache_hit:
            return cached_df
        all_data = []
        current_start = start_time
        if not cached_df.empty:
            current_start = cached_df["dt"].max() + timedelta(minutes=1)
            if current_start > end_time:
                return cached_df
        limit = 20000 # Max limit per API spec
        
        print(f"Fetching data for {code} from {start_time} to {end_time}...")
        
        while current_start < end_time:
            # Format dates as ISO string or whatever the API expects.
            # API spec says date-time string. Let's try ISO format.
            # Example response showed "2011-08-17T15:00:00".
            
            # Request slightly more than needed to ensure coverage, but use limit
            candidate_params = []
            for c in self._code_variants(code):
                candidate_params.extend([
                    {
                        "code": c,
                        "start_time": current_start.isoformat(),
                        "end_time": end_time.isoformat(),
                        "limit": limit
                    },
                    {
                        "code": c,
                        "start_time": current_start.strftime("%Y-%m-%d %H:%M:%S"),
                        "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
                        "limit": limit
                    },
                    {
                        "code": c,
                        "start_time": current_start.strftime("%Y-%m-%d"),
                        "end_time": end_time.strftime("%Y-%m-%d"),
                        "limit": limit
                    }
                ])

            try:
                rows = []
                for params in candidate_params:
                    response = self._request_get("/market/minutes", params, timeout=45)
                    if response is None:
                        continue
                    data = response.json()
                    rows = self._extract_rows(data)
                    if rows:
                        break
                if not rows:
                    break
                    
                df_chunk = self._normalize_minutes_df(pd.DataFrame(rows))
                if df_chunk.empty:
                    break
                all_data.append(df_chunk)
                
                # Update current_start for next page
                # The API doesn't seem to have offset-based pagination for /minutes, but time-based?
                # Or maybe I just slide the window.
                # If I got 'limit' records, the last one's time is the new start.
                if len(rows) < limit:
                    break
                    
                last_time = pd.to_datetime(df_chunk["dt"].max())
                
                # If we are stuck at the same time, break to avoid infinite loop
                if last_time <= current_start:
                    # Move forward by 1 minute if stuck?
                    current_start = current_start + timedelta(minutes=1)
                else:
                    current_start = last_time + timedelta(minutes=1) # Start next from next minute
                    
                # Rate limit protection
                time.sleep(0.1) 
                
            except Exception as e:
                print(f"Error fetching data: {e}")
                self.last_error = str(e)
                break
                
        if not all_data:
            return cached_df if not cached_df.empty else pd.DataFrame()
        final_df = pd.concat(all_data, ignore_index=True)
        if not cached_df.empty:
            final_df = pd.concat([cached_df, final_df], ignore_index=True)
        final_df = self._normalize_minutes_df(final_df)
        self._save_minute_cache(code, final_df)
        return final_df

    def fetch_batch_data(self, codes, start_time, end_time):
        """
        Fetch data for multiple codes.
        """
        results = {}
        for code in codes:
            df = self.fetch_minute_data(code, start_time, end_time)
            if not df.empty:
                results[code] = df
        return results

    def get_latest_bar(self, code):
        """
        Get the latest available 1-minute bar for a stock.
        Returns a dict or None.
        """
        try:
            url = f"{self.base_url}/market/latest"
            candidate_params = []
            for c in self._code_variants(code):
                candidate_params.extend([{"codes": c}, {"code": c}])
            rows = []
            for params in candidate_params:
                response = self._request_get("/market/latest", params, timeout=8)
                if response is None:
                    continue
                data = response.json()
                rows = self._extract_rows(data)
                if rows:
                    break
            if rows:
                row_df = self._normalize_minutes_df(pd.DataFrame([rows[0]]))
                if not row_df.empty:
                    row = row_df.iloc[0]
                    return {
                        'code': row['code'],
                        'dt': row['dt'],
                        'open': float(row['open']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'close': float(row['close']),
                        'vol': float(row['vol']),
                        'amount': float(row['amount'])
                    }
        except Exception as e:
            print(f"Error fetching latest bar for {code}: {e}")
            self.last_error = str(e)
        return None

    def _normalize_minute_bar_row(self, bar):
        if not isinstance(bar, dict):
            return None
        dt_val = pd.to_datetime(bar.get("dt"), errors="coerce")
        if pd.isna(dt_val):
            return None
        code = str(bar.get("code", "") or "").strip().upper()
        if not code:
            return None
        row = {
            "code": code,
            "trade_time": dt_val.strftime("%Y-%m-%d %H:%M:%S"),
            "open": float(bar.get("open", bar.get("close", 0.0)) or 0.0),
            "high": float(bar.get("high", bar.get("close", 0.0)) or 0.0),
            "low": float(bar.get("low", bar.get("close", 0.0)) or 0.0),
            "close": float(bar.get("close", 0.0) or 0.0),
            "vol": float(bar.get("vol", bar.get("volume", 0.0)) or 0.0),
            "amount": float(bar.get("amount", bar.get("turnover", 0.0)) or 0.0),
        }
        return row

    def upsert_table_rows(self, table, rows, on_duplicate="update"):
        if not self.base_url:
            self.last_error = "default_api_url 未配置"
            return 0
        if not self.api_key:
            self.last_error = "default_api_key 未配置"
            return 0
        if not isinstance(rows, list) or len(rows) <= 0:
            return 0
        payload = {
            "on_duplicate": str(on_duplicate or "update"),
            "rows": rows
        }
        response = self._request_post(f"/tables/{str(table)}/rows", payload, timeout=30)
        if response is None:
            return 0
        try:
            data = response.json()
            rowcount = data.get("rowcount") if isinstance(data, dict) else None
            if isinstance(rowcount, int):
                self.last_error = ""
                return rowcount
        except Exception:
            pass
        self.last_error = ""
        return len(rows)

    def upsert_realtime_minute_bar(self, bar, on_duplicate="update"):
        row = self._normalize_minute_bar_row(bar)
        if row is None:
            self.last_error = "invalid realtime bar payload"
            return 0
        return int(self.upsert_table_rows("dat_1mins", [row], on_duplicate=on_duplicate) or 0)

    def push_data_to_remote(self, data_list):
        """
        Push daily archived data to remote API.
        This is a placeholder as the current API docs (OpenAPI) are read-only (GET methods).
        If there is a POST endpoint in future, implement here.
        For now, we just log.
        """
        # print(f"Pushing {len(data_list)} records to remote API...")
        # Example: requests.post(f"{self.base_url}/market/minutes", json=data_list, headers=self.headers)
        pass
