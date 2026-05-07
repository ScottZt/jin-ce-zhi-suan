# src/utils/akshare_provider.py
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta
import os
from src.utils.config_loader import ConfigLoader

class AkshareProvider:
    """
    Akshare Data Provider (Open Source, Free)
    """
    def __init__(self):
        cfg = ConfigLoader.reload()
        self.last_error = ""
        self._cache_enabled = bool(cfg.get("data_provider.local_cache_enabled", True))
        cache_dir = str(cfg.get("data_provider.local_cache_dir", "D:/jin-ce-zhi-suan/data/history/cache") or "D:/jin-ce-zhi-suan/data/history/cache")
        base_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(base_dir))
        self._cache_dir = cache_dir if os.path.isabs(cache_dir) else os.path.join(project_root, cache_dir)
        os.makedirs(self._cache_dir, exist_ok=True)

    def _cache_file_path(self, code, interval="1min"):
        safe_code = str(code).upper().replace(".", "_")
        return os.path.join(self._cache_dir, f"akshare_{safe_code}_{interval}.csv")

    def _normalize_minutes_df(self, df):
        if df is None or df.empty:
            return pd.DataFrame()
        work = df.copy()
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

    def get_latest_bar(self, code):
        """
        Get the latest real-time quote for a stock using Akshare.
        Returns a dict in the standard format.
        """
        try:
            # Normalize code: 600036.SH -> 600036
            symbol = code.split('.')[0]
            
            import time
            max_retries = 3
            df = None
            
            for i in range(max_retries):
                try:
                    # Using stock_zh_a_spot_em can be slow/unstable if getting all stocks.
                    # stock_zh_a_hist_min_em is usually more reliable for single stock.
                    # But for "latest", we might just want the last 1min bar.
                    # Let's use stock_zh_a_spot_em ONLY IF filtered (not possible directly usually).
                    
                    # Better: stock_bid_ask_em (Buy/Sell levels) - very fast for single stock
                    # But we need OHLCV.
                    
                    # Let's stick to stock_zh_a_hist_min_em with a very short recent period?
                    # Or try stock_zh_a_tick_tx_js (tick data) -> Agg to 1min? Too complex.
                    
                    # Actually, for reliability, let's use stock_zh_a_hist_min_em for now.
                    # It might have 1 min delay but it's stable.
                    
                    # Get today's data?
                    now = datetime.now()
                    start_str = now.strftime("%Y-%m-%d 09:30:00")
                    end_str = now.strftime("%Y-%m-%d 15:00:00")
                    
                    # If before market, get yesterday?
                    # Simplify: just get last available data without date filter (returns recent)
                    # Akshare documentation says start_date/end_date are optional? 
                    # If not provided, it returns recent?
                    # Let's try providing recent range.
                    
                    # Actually, if we just want "latest price", stock_zh_a_spot_em is best but slow.
                    # Let's try stock_zh_a_spot_em again, maybe it works better now.
                    # Or 'stock_zh_a_hist_pre_min_em' (pre-market)?
                    
                    # Let's use `stock_zh_a_hist_min_em` with no date range (defaults to recent?)
                    # Or just recent few minutes.
                    
                    df = ak.stock_zh_a_hist_min_em(symbol=symbol, period='1', adjust='qfq')
                    self.last_error = ""
                    break
                except Exception as e:
                    self.last_error = f"get_latest_bar_retry_failed attempt={i+1}/{max_retries} code={code} err={e}"
                    time.sleep(1)
            
            if df is None or df.empty:
                self.last_error = f"get_latest_bar_empty code={code}"
                return None
                
            row = df.iloc[-1]
            
            # Columns: 时间, 开盘, 收盘, 最高, 最低, 成交量, 成交额, ...
            # Format: "2024-03-20 15:00:00"
            dt = pd.to_datetime(row['时间'])
            
            return {
                'code': code,
                'dt': dt,
                'open': float(row['开盘']),
                'high': float(row['最高']),
                'low': float(row['最低']),
                'close': float(row['收盘']),
                'vol': float(row['成交量']),
                'amount': float(row['成交额'])
            }
        except Exception as e:
            self.last_error = f"get_latest_bar_failed code={code} err={e}"
            print(f"Error fetching Akshare RT data: {e}")
            return None

    def fetch_minute_data(self, code, start_time, end_time):
        """
        Fetch historical minute data via Akshare (EastMoney).
        """
        cached_df, cache_hit = self._load_cached_minute_data(code, start_time, end_time)
        if cache_hit:
            return cached_df
        fetch_start = start_time
        if not cached_df.empty:
            fetch_start = cached_df["dt"].max() + timedelta(minutes=1)
            if fetch_start > end_time:
                return cached_df
        symbol = code.split('.')[0]
        # Akshare expects "YYYY-MM-DD HH:MM:SS" for minute data
        start_str = fetch_start.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")
        
        # Akshare stock_zh_a_hist_min_em seems to expect 'start_date' and 'end_date' in "YYYY-MM-DD HH:MM:SS"
        # However, checking recent docs/issues, sometimes it requires different formats or specific time ranges.
        # Let's verify parameter names. Yes, symbol, start_date, end_date, period, adjust.
        
        # IMPORTANT: Akshare EastMoney interface often fails if start/end range is too narrow or on holidays?
        # Let's try to ensure we are requesting a valid range.
        
        print(f"📡 Requesting Akshare data for {symbol} from {start_str} to {end_str}...")
        
        try:
            # Akshare stock_zh_a_hist_min_em
            # Note: Akshare requests might be blocked or need retry.
            # Adding a simple retry mechanism
            import time
            max_retries = 3
            df = None
            
            for i in range(max_retries):
                try:
                    df = ak.stock_zh_a_hist_min_em(
                        symbol=symbol, 
                        period='1', 
                        adjust='qfq', 
                        start_date=start_str, 
                        end_date=end_str
                    )
                    self.last_error = ""
                    break
                except Exception as e:
                    self.last_error = f"fetch_minute_data_retry_failed attempt={i+1}/{max_retries} code={code} range={start_str}->{end_str} err={e}"
                    print(f"Retry {i+1}/{max_retries} failed: {e}")
                    time.sleep(1)
            
            if df is None or df.empty:
                self.last_error = f"fetch_minute_data_empty code={code} range={start_str}->{end_str}"
                print(f"⚠️ Akshare returned empty data for {symbol}.")
                return cached_df if not cached_df.empty else pd.DataFrame()
                
            # Rename columns to standard format
            # Akshare columns: "时间", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "振幅", "涨跌幅", "涨跌额", "换手率"
            df = df.rename(columns={
                '时间': 'dt',
                '开盘': 'open',
                '收盘': 'close',
                '最高': 'high',
                '最低': 'low',
                '成交量': 'vol',
                '成交额': 'amount'
            })
            
            df['code'] = code
            df['dt'] = pd.to_datetime(df['dt'])
            
            # Filter exact range (API might return slightly different range)
            mask = (df['dt'] >= start_time) & (df['dt'] <= end_time)
            df = df.loc[mask]
            
            df = self._normalize_minutes_df(df[['code', 'dt', 'open', 'high', 'low', 'close', 'vol', 'amount']])
            if not cached_df.empty:
                df = pd.concat([cached_df, df], ignore_index=True)
                df = self._normalize_minutes_df(df)
            self._save_minute_cache(code, df)
            self.last_error = ""
            return df
            
        except Exception as e:
            self.last_error = f"fetch_minute_data_failed code={code} range={start_str}->{end_str} err={e}"
            print(f"❌ Error fetching Akshare history: {e}")
            return cached_df if not cached_df.empty else pd.DataFrame()
