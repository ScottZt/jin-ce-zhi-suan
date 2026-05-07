#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sync all stocks from TongDaXin to DuckDB - Using tqdm progress bar
"""
import os
import sys
import csv
import time
from datetime import datetime, timedelta
from tqdm import tqdm

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.utils.tdx_provider import TdxProvider
import duckdb

# Config
DUCKDB_PATH = "D:/duckdb/quantifydata.duckdb"
STOCK_LIST_FILE = os.path.join(PROJECT_ROOT, "data", "stock_list.csv")
MINUTE_DAYS = 30

def init_database(conn):
    """Initialize database tables"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dat_day (
            code VARCHAR, trade_time TIMESTAMP, open DOUBLE, high DOUBLE,
            low DOUBLE, close DOUBLE, vol DOUBLE, amount DOUBLE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dat_1mins (
            code VARCHAR, trade_time TIMESTAMP, open DOUBLE, high DOUBLE,
            low DOUBLE, close DOUBLE, vol DOUBLE, amount DOUBLE
        )
    """)
    conn.execute("DELETE FROM dat_day")
    conn.execute("DELETE FROM dat_1mins")

def load_stock_list():
    """Load all stock codes from CSV"""
    stocks = []
    try:
        with open(STOCK_LIST_FILE, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                code = row['code']
                if code.startswith('6'):
                    stocks.append(f"{code}.SH")
                else:
                    stocks.append(f"{code}.SZ")
    except Exception as e:
        print(f"Error loading stock list: {e}")
    return stocks

def sync_stock(tdx, conn, stock_code, start_date, end_date, sync_minute=True):
    """Sync single stock data"""
    results = {'daily': 0, 'minute': 0, 'error': None}
    
    try:
        # Sync daily data
        df_daily = tdx.fetch_kline_data(stock_code, start_time=start_date, end_time=end_date, interval='day')
        if df_daily is not None and len(df_daily) > 0:
            daily_data = []
            for _, row in df_daily.iterrows():
                daily_data.append((
                    stock_code,
                    row.get('dt') or row.get('trade_time'),
                    float(row['open']), float(row['high']), float(row['low']),
                    float(row['close']), float(row['vol']), float(row.get('amount', 0))
                ))
            conn.executemany("INSERT INTO dat_day VALUES (?, ?, ?, ?, ?, ?, ?, ?)", daily_data)
            results['daily'] = len(daily_data)
        
        # Sync minute data
        if sync_minute:
            minute_start = end_date - timedelta(days=MINUTE_DAYS)
            df_minute = tdx.fetch_kline_data(stock_code, start_time=minute_start, end_time=end_date, interval='1min')
            if df_minute is not None and len(df_minute) > 0:
                minute_data = []
                for _, row in df_minute.iterrows():
                    minute_data.append((
                        stock_code,
                        row.get('dt') or row.get('trade_time'),
                        float(row['open']), float(row['high']), float(row['low']),
                        float(row['close']), float(row['vol']), float(row.get('amount', 0))
                    ))
                conn.executemany("INSERT INTO dat_1mins VALUES (?, ?, ?, ?, ?, ?, ?, ?)", minute_data)
                results['minute'] = len(minute_data)
    except Exception as e:
        results['error'] = str(e)
    
    return results

def main():
    print("="*70)
    print("TongDaXin All Stocks Sync to DuckDB")
    print("="*70)
    
    # Init TDX
    tdx = TdxProvider()
    print(f"TDX Mode: {tdx.describe_mode()}")
    print(f"TDX Dir: {tdx.tdxdir}\n")
    
    # Connect DuckDB
    os.makedirs(os.path.dirname(DUCKDB_PATH), exist_ok=True)
    conn = duckdb.connect(DUCKDB_PATH)
    init_database(conn)
    
    # Load stocks
    stocks = load_stock_list()
    total = len(stocks)
    print(f"Loaded {total} stocks")
    
    # Date range
    end_date = datetime.now()
    start_date = end_date - timedelta(days=730)
    print(f"Daily range: {start_date.date()} to {end_date.date()}")
    print(f"Minute range: last {MINUTE_DAYS} days\n")
    print(f"Starting sync at {datetime.now().strftime('%H:%M:%S')}...")
    print("-"*70)
    
    # Statistics
    success_count = 0
    fail_count = 0
    total_daily = 0
    total_minute = 0
    start_time = time.time()
    
    # Use tqdm for single-line progress bar
    with tqdm(total=total, desc="Syncing", unit="stocks", 
              bar_format='{desc}: {percentage:5.1f}%|{bar:30}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}] | D:{postfix[0]} M:{postfix[1]}',
              postfix=[0, 0]) as pbar:
        
        for stock in stocks:
            results = sync_stock(tdx, conn, stock, start_date, end_date, sync_minute=True)
            
            if results['error']:
                fail_count += 1
            else:
                success_count += 1
                total_daily += results['daily']
                total_minute += results['minute']
            
            # Update progress bar
            pbar.postfix[0] = total_daily
            pbar.postfix[1] = total_minute
            pbar.update(1)
    
    print("\n" + "-"*70)
    print(f"Sync Complete!")
    print(f"Time: {(time.time() - start_time)/60:.1f} minutes")
    print(f"Success: {success_count}, Failed: {fail_count}")
    print(f"Total daily: {total_daily}, Total minute: {total_minute}")
    
    # Database stats
    day_count = conn.execute("SELECT COUNT(*) FROM dat_day").fetchone()[0]
    min_count = conn.execute("SELECT COUNT(*) FROM dat_1mins").fetchone()[0]
    print(f"\nDatabase: Daily={day_count}, Minute={min_count}")
    
    conn.close()
    print(f"\nSaved to: {DUCKDB_PATH}")

if __name__ == "__main__":
    main()
