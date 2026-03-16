# src/utils/tushare_provider.py
import tushare as ts
import pandas as pd
from datetime import datetime, timedelta

class TushareProvider:
    """
    Tushare Pro Data Provider
    """
    def __init__(self, token=None):
        # Default to a placeholder token if none provided. User must replace this.
        self.token = token
        import tushare.pro.client as client
        client.DataApi._DataApi__http_url = "http://tushare.xyz"
        if self.token:
            ts.set_token(self.token)
            self.pro = ts.pro_api()
        else:
            self.pro = None
            print("⚠️ Warning: Tushare Token not provided. Please initialize with a valid token.")

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
                    # Format: ts_code, freq, time, open, close, high, low, vol, amount
                    # Time is just HH:MM:SS, we need date.
                    today = datetime.now().strftime("%Y-%m-%d")
                    dt_str = f"{today} {row['time']}"
                    dt = pd.to_datetime(dt_str)
                    
                    return {
                        'code': row['ts_code'],
                        'dt': dt,
                        'open': float(row['open']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'close': float(row['close']),
                        'vol': float(row['vol']),
                        'amount': float(row['amount'])
                    }
            except Exception as e_rt:
                # print(f"DEBUG: rt_min failed ({e_rt}), falling back to get_realtime_quotes")
                pass

            # Fallback to get_realtime_quotes (Scraping)
            df = ts.get_realtime_quotes(code)
            if df is None or df.empty:
                # Fallback to pro.daily for latest close (not real-time but better than nothing)
                end_date = datetime.now().strftime("%Y%m%d")
                start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
                df_daily = self.pro.daily(ts_code=code, start_date=start_date, end_date=end_date)
                
                if df_daily is not None and not df_daily.empty:
                    row = df_daily.iloc[0] # Latest
                    return {
                        'code': code,
                        'dt': pd.to_datetime(row['trade_date']),
                        'open': float(row['open']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'close': float(row['close']),
                        'vol': float(row['vol']) * 100, # Hand is unit? No, usually vol is lot or share. Tushare daily vol is "hand" (100 shares)? No, it says "Vol (Hand)". Let's assume hand.
                        'amount': float(row['amount']) * 1000 # Amount is usually in thousands?
                    }
                return None
                
            row = df.iloc[0]
            
            # Normalize format
            # Tushare RT columns: name, open, pre_close, price, high, low, bid, ask, volume, amount, date, time
            
            # Combine date and time
            # Note: get_realtime_quotes returns different columns based on source
            # For tushare < 1.3, it uses sina.
            
            date_str = str(row['date'])
            time_str = str(row['time'])
            dt_str = f"{date_str} {time_str}"
            dt = pd.to_datetime(dt_str)
            
            return {
                'code': code,
                'dt': dt,
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['price']), # Current price
                'vol': float(row['volume']), # Check unit: usually shares?
                'amount': float(row['amount']) # Check unit: usually Yuan?
            }
        except Exception as e:
            # print(f"Error fetching Tushare RT data: {e}")
            # Try fallback inside exception if get_realtime_quotes crashed (e.g. network issue or parsing issue)
            try:
                 end_date = datetime.now().strftime("%Y%m%d")
                 start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
                 df_daily = self.pro.daily(ts_code=code, start_date=start_date, end_date=end_date)
                 if df_daily is not None and not df_daily.empty:
                    row = df_daily.iloc[0]
                    return {
                        'code': code,
                        'dt': pd.to_datetime(row['trade_date']),
                        'open': float(row['open']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'close': float(row['close']),
                        'vol': float(row['vol']) * 100,
                        'amount': float(row['amount']) * 1000
                    }
            except:
                pass
            return None

    def fetch_minute_data(self, code, start_time, end_time):
        """
        Fetch historical minute data via Tushare Pro (requires points/permission).
        Interface: pro.stk_mins or standard ts.pro_bar
        """
        if not self.pro:
            return pd.DataFrame()
            
        # Format dates: YYYY-MM-DD HH:MM:SS
        start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            print(f"DEBUG: Requesting Tushare data for {code} ({start_str} - {end_str})")
            
            # Use pro.stk_mins as it proved more reliable for the user's specific token/permission
            df = self.pro.stk_mins(ts_code=code, freq='1min', start_date=start_str, end_date=end_str)
            
            if df is None or df.empty:
                print(f"⚠️ Tushare stk_mins returned empty for {code}.")
                return pd.DataFrame()
                
            # Columns: ts_code, trade_time, open, close, high, low, vol, amount
            # Rename
            df = df.rename(columns={
                'ts_code': 'code',
                'trade_time': 'dt'
            })
            
            # Ensure datetime
            df['dt'] = pd.to_datetime(df['dt'])
            
            # Ensure numeric types
            numeric_cols = ['open', 'close', 'high', 'low', 'vol', 'amount']
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # Sort ascending (Tushare usually returns descending)
            df = df.sort_values('dt').reset_index(drop=True)
            
            return df
            
        except Exception as e:
            print(f"Error fetching Tushare history: {e}")
            return pd.DataFrame()
