#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sync all stocks from TongDaXin to DuckDB - Using tqdm for progress bar
"""
import os
import sys
import csv
import time
from datetime import datetime, timedelta

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
    print("Initializing database...")
    
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
    print("Database initialized.\n")

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
    print(f"\nTDX Mode: {tdx.describe_mode()}")
    print(f"TDX Dir: {tdx.tdxdir}")
    
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
    print(f"Minute range: last {MINUTE_DAYS} days")
    print(f"\nStarting sync at {datetime.now().strftime('%H:%M:%S')}...")
    print("-"*70)
    
    # Statistics
    success_count = 0
    fail_count = 0
    total_daily = 0
    total_minute = 0
    start_time = time.time()
    
    # Simple progress display - update every N stocks
    update_interval = 50
    last_update = 0
    
    # Sync each stock
    for i, stock in enumerate(stocks, 1):
        results = sync_stock(tdx, conn, stock, start_date, end_date, sync_minute=True)
        
        if results['error']:
            fail_count += 1
        else:
            success_count += 1
            total_daily += results['daily']
            total_minute += results['minute']
        
        # Update display every N stocks
        if i - last_update >= update_interval or i == total:
            elapsed = time.time() - start_time
            speed = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / speed if speed > 0 else 0
            pct = i / total * 100
            
            # Format: [PROGRESS] 12.5% (687/5493) | 2.3/s | ETA: 35min | Daily: 325421
            bar_len = 20
            filled = int(bar_len * i / total)
            bar = '=' * filled + '>' + '-' * (bar_len - filled - 1)
            
            # Clear line and print (Windows compatible)
            print(f"[{bar}] {pct:5.1f}% ({i}/{total}) | {speed:.1f}/s | ETA:{eta/60:3.0f}min | D:{total_daily} M:{total_minute}")
            last_update = i
    
    print("-"*70)
    print(f"\nSync Complete!")
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
