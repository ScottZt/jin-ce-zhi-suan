#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sync all stocks from TongDaXin to DuckDB
Includes: Daily and Minute(1min) data
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
BATCH_SIZE = 100  # Insert batch size
MINUTE_DAYS = 30  # Only sync last 30 days for minute data

def init_database(conn):
    """Initialize database tables"""
    print("Initializing database...")
    
    # Daily data table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dat_day (
            code VARCHAR,
            trade_time TIMESTAMP,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            vol DOUBLE,
            amount DOUBLE
        )
    """)
    
    # 1-minute data table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dat_1mins (
            code VARCHAR,
            trade_time TIMESTAMP,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            vol DOUBLE,
            amount DOUBLE
        )
    """)
    
    # Clear existing data
    conn.execute("DELETE FROM dat_day")
    conn.execute("DELETE FROM dat_1mins")
    
    print("Database initialized.")

def load_stock_list():
    """Load all stock codes from CSV"""
    stocks = []
    try:
        with open(STOCK_LIST_FILE, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                code = row['code']
                # Convert to standard format
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
                    float(row['open']),
                    float(row['high']),
                    float(row['low']),
                    float(row['close']),
                    float(row['vol']),
                    float(row.get('amount', 0))
                ))
            
            # Batch insert
            conn.executemany("""
                INSERT INTO dat_day VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, daily_data)
            results['daily'] = len(daily_data)
        
        # Sync minute data (only last N days)
        if sync_minute:
            minute_start = end_date - timedelta(days=MINUTE_DAYS)
            df_minute = tdx.fetch_kline_data(stock_code, start_time=minute_start, end_time=end_date, interval='1min')
            if df_minute is not None and len(df_minute) > 0:
                minute_data = []
                for _, row in df_minute.iterrows():
                    minute_data.append((
                        stock_code,
                        row.get('dt') or row.get('trade_time'),
                        float(row['open']),
                        float(row['high']),
                        float(row['low']),
                        float(row['close']),
                        float(row['vol']),
                        float(row.get('amount', 0))
                    ))
                
                conn.executemany("""
                    INSERT INTO dat_1mins VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, minute_data)
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
    
    # Init tables
    init_database(conn)
    
    # Load stock list
    stocks = load_stock_list()
    print(f"Loaded {len(stocks)} stocks from {STOCK_LIST_FILE}")
    
    # 获取已同步的股票列表
    synced_stocks = set()
    try:
        result = conn.execute("SELECT DISTINCT code FROM dat_day").fetchall()
        synced_stocks = {row[0] for row in result}
        print(f"已同步股票: {len(synced_stocks)} 只，将继续同步剩余股票")
    except:
        pass
    
    # Filter out already synced stocks
    stocks_to_sync = [s for s in stocks if s not in synced_stocks]
    total = len(stocks_to_sync)
    skipped = len(stocks) - total
    print(f"跳过已同步: {skipped} 只")
    print(f"待同步: {total} 只")
    
    # Date range (last 2 years for daily, last 30 days for minute)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=730)
    
    print(f"Daily data range: {start_date.date()} to {end_date.date()}")
    print(f"Minute data range: last {MINUTE_DAYS} days")
    print("\nStarting sync...")
    print("-"*70)
    
    # Statistics
    success_count = 0
    fail_count = 0
    total_daily = 0
    total_minute = 0
    start_time = time.time()
    last_print_time = 0
    
    # Sync each stock
    for i, stock in enumerate(stocks_to_sync, 1):
        results = sync_stock(tdx, conn, stock, start_date, end_date, sync_minute=True)
        
        if results['error']:
            fail_count += 1
        else:
            success_count += 1
            total_daily += results['daily']
            total_minute += results['minute']
        
        # Update progress every 5 seconds or every 100 stocks
        current_time = time.time()
        if current_time - last_print_time >= 5.0 or i % 100 == 0 or i == total:
            elapsed = current_time - start_time
            speed = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / speed if speed > 0 else 0
            progress_pct = i / total * 100
            
            # Progress bar (30 chars)
            bar_len = 30
            filled = int(bar_len * i / total)
            bar = '=' * filled + '-' * (bar_len - filled)
            
            # Print on new line every time (Windows CMD doesn't handle \r well)
            print(f"[{bar}] {progress_pct:5.1f}% | {i}/{total} | {speed:.1f}/s | ETA:{eta/60:3.0f}min | Daily:{total_daily} | 1min:{total_minute}")
            last_print_time = current_time
    
    # Final statistics
    print("-"*70)
    print("\nSync Complete!")
    print(f"Total time: {(time.time() - start_time)/60:.1f} minutes")
    print(f"Success: {success_count}, Failed: {fail_count}")
    print(f"Total daily records: {total_daily}")
    print(f"Total minute records: {total_minute}")
    
    # Database stats
    print("\nDatabase Statistics:")
    day_count = conn.execute("SELECT COUNT(*) FROM dat_day").fetchone()[0]
    min_count = conn.execute("SELECT COUNT(*) FROM dat_1mins").fetchone()[0]
    day_stocks = conn.execute("SELECT COUNT(DISTINCT code) FROM dat_day").fetchone()[0]
    min_stocks = conn.execute("SELECT COUNT(DISTINCT code) FROM dat_1mins").fetchone()[0]
    
    print(f"  Daily: {day_count} records, {day_stocks} stocks")
    print(f"  Minute: {min_count} records, {min_stocks} stocks")
    
    conn.close()
    print(f"\nSaved to: {DUCKDB_PATH}")

if __name__ == "__main__":
    main()
