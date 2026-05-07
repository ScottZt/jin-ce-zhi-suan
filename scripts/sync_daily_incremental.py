#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Incremental Sync - Only sync new data since last update
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
BATCH_SIZE = 100

def get_last_sync_date(conn):
    """Get the last trade date from database"""
    try:
        result = conn.execute("SELECT MAX(trade_time) FROM dat_day").fetchone()
        if result and result[0]:
            # Convert to date, subtract 1 day for safety
            last_date = result[0]
            if isinstance(last_date, str):
                last_date = datetime.fromisoformat(last_date.replace(' ', 'T'))
            return last_date - timedelta(days=1)
    except:
        pass
    # Default: 30 days ago
    return datetime.now() - timedelta(days=30)

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

def print_progress_bar(current, total, start_time, total_new):
    """Print a simple progress bar on one line"""
    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0
    pct = current / total * 100
    
    # Simple bar: [=====>    ]
    bar_len = 20
    filled = int(bar_len * current / total)
    bar = '=' * filled + '>' + ' ' * (bar_len - filled - 1)
    
    # Print with \r to return to start of line
    sys.stdout.write(f"\r[{bar}] {pct:5.1f}% {current}/{total} | {speed:.1f}/s ETA:{eta/60:3.0f}min | New:{total_new}")
    sys.stdout.flush()
    """Sync only new data for a stock"""
    results = {'daily_new': 0, 'error': None}
    
    try:
        # Get daily data
        df_daily = tdx.fetch_kline_data(stock_code, start_time=start_date, end_time=end_date, interval='day')
        if df_daily is not None and len(df_daily) > 0:
            # Check which records already exist
            for _, row in df_daily.iterrows():
                trade_time = row.get('dt') or row.get('trade_time')
                
                # Check if record exists
                existing = conn.execute(
                    "SELECT 1 FROM dat_day WHERE code = ? AND trade_time = ?",
                    (stock_code, trade_time)
                ).fetchone()
                
                if not existing:
                    # Insert new record
                    conn.execute("""
                        INSERT INTO dat_day VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        stock_code,
                        trade_time,
                        float(row['open']),
                        float(row['high']),
                        float(row['low']),
                        float(row['close']),
                        float(row['vol']),
                        float(row.get('amount', 0))
                    ))
                    results['daily_new'] += 1
                    
    except Exception as e:
        results['error'] = str(e)
    
    return results

def main():
    print("="*70)
    print("Daily Incremental Sync - TongDaXin to DuckDB")
    print("="*70)
    print(f"\nStart time: {datetime.now()}")
    
    # Init TDX
    tdx = TdxProvider()
    print(f"TDX Mode: {tdx.describe_mode()}")
    print(f"TDX Dir: {tdx.tdxdir}")
    
    # Connect DuckDB
    os.makedirs(os.path.dirname(DUCKDB_PATH), exist_ok=True)
    conn = duckdb.connect(DUCKDB_PATH)
    
    # Ensure table exists
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
    
    # Get last sync date
    last_sync = get_last_sync_date(conn)
    end_date = datetime.now()
    
    print(f"\nLast sync date: {last_sync}")
    print(f"Sync range: {last_sync.date()} to {end_date.date()}")
    
    # Load stock list
    stocks = load_stock_list()
    total = len(stocks)
    print(f"Total stocks: {total}")
    
    # Statistics
    success_count = 0
    fail_count = 0
    total_new_records = 0
    start_time = time.time()
    
    print("\nStarting incremental sync...")
    print("-"*70)
    
    # Sync each stock
    for i, stock in enumerate(stocks, 1):
        results = sync_stock_incremental(tdx, conn, stock, last_sync, end_date)
        
        if results['error']:
            fail_count += 1
            print(f"[{i}/{total}] {stock} - ERROR: {results['error']}")
        else:
            success_count += 1
            total_new_records += results['daily_new']
            
            # Progress every 100 stocks or if new records found
            if i % 100 == 0 or i == total or results['daily_new'] > 0:
                elapsed = time.time() - start_time
                speed = i / elapsed if elapsed > 0 else 0
                eta = (total - i) / speed if speed > 0 else 0
                
                status = f"[{i}/{total}] {stock}"
                if results['daily_new'] > 0:
                    status += f" - NEW: {results['daily_new']} records"
                
                print(f"{status} | Progress: {i/total*100:.1f}% | ETA: {eta/60:.1f}min")
    
    # Final statistics
    print("-"*70)
    print(f"\nSync Complete!")
    print(f"Total time: {(time.time() - start_time)/60:.1f} minutes")
    print(f"Success: {success_count}, Failed: {fail_count}")
    print(f"New records added: {total_new_records}")
    
    # Database stats
    day_count = conn.execute("SELECT COUNT(*) FROM dat_day").fetchone()[0]
    day_stocks = conn.execute("SELECT COUNT(DISTINCT code) FROM dat_day").fetchone()[0]
    latest_date = conn.execute("SELECT MAX(trade_time) FROM dat_day").fetchone()[0]
    
    print(f"\nDatabase Status:")
    print(f"  Total records: {day_count}")
    print(f"  Total stocks: {day_stocks}")
    print(f"  Latest data: {latest_date}")
    
    conn.close()
    print(f"\nSaved to: {DUCKDB_PATH}")
    print(f"End time: {datetime.now()}")

if __name__ == "__main__":
    main()
