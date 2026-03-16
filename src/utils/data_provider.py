# src/utils/data_provider.py
import requests
import pandas as pd
from datetime import datetime, timedelta
import time
from src.utils.config_loader import ConfigLoader

class DataProvider:
    """
    Data Provider using external API for 1-minute K-line data.
    """
    def __init__(self, api_key=None, base_url=None):
        cfg = ConfigLoader()
        self.api_key = api_key or cfg.get("data_provider.default_api_key", "")
        self.base_url = (base_url or cfg.get("data_provider.default_api_url", "")).rstrip("/")
        self.headers = {"X-API-Key": self.api_key} if self.api_key else {}
        self.last_error = ""

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

    def fetch_minute_data(self, code, start_time, end_time):
        """
        Fetch 1-minute data for a single stock within a time range.
        Handles pagination automatically.
        """
        all_data = []
        current_start = start_time
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
                    response = self._request_get("/market/minutes", params, timeout=15)
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
            return pd.DataFrame()
            
        final_df = pd.concat(all_data, ignore_index=True)
        final_df = self._normalize_minutes_df(final_df)
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
