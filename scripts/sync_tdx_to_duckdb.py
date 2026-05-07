#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TongDaXin Data Sync to DuckDB
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.utils.tdx_provider import TdxProvider
import duckdb
from datetime import datetime, timedelta

def main():
    print("="*60)
    print("TongDaXin Data Sync to DuckDB")
    print("="*60)
    
    # Init TDX
    tdx = TdxProvider()
    print("\nTDX Mode:", tdx.describe_mode())
    print("TDX Dir:", tdx.tdxdir)
    
    # DuckDB path (D drive)
    duckdb_path = "D:/jin-ce-zhi-suan/data/quantifydata.duckdb"
    print("DuckDB Path:", duckdb_path)
    
    # Connect DuckDB
    os.makedirs(os.path.dirname(duckdb_path), exist_ok=True)
    conn = duckdb.connect(duckdb_path)
    
    # Create tables
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
    
    conn.execute("DELETE FROM dat_day")
    
    # Stock list
    stocks = ['000001.SZ', '000858.SZ', '600036.SH', '600519.SH']
    
    # Date range (last 2 years)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=730)
    
    for stock in stocks:
        print("\nSync:", stock)
        
        try:
            # Get daily data
            df = tdx.fetch_kline_data(stock, start_time=start_date, end_time=end_date, interval='day')
            if df is not None and len(df) > 0:
                print("  Daily:", len(df), "records")
                
                for _, row in df.iterrows():
                    conn.execute("""
                        INSERT INTO dat_day VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        stock,
                        row.get('dt') or row.get('trade_time'),
                        float(row['open']),
                        float(row['high']),
                        float(row['low']),
                        float(row['close']),
                        float(row['vol']),
                        float(row.get('amount', 0))
                    ))
                print("  OK - Daily")
            else:
                print("  No daily data")
                
        except Exception as e:
            print("  Error:", str(e))
    
    # Statistics
    print("\n" + "="*60)
    print("Sync Complete:")
    day_count = conn.execute("SELECT COUNT(*) FROM dat_day").fetchone()[0]
    day_stocks = conn.execute("SELECT COUNT(DISTINCT code) FROM dat_day").fetchone()[0]
    
    print("Daily records:", day_count)
    print("Stocks:", day_stocks)
    
    # Show sample
    print("\nSample data:")
    sample = conn.execute("SELECT * FROM dat_day LIMIT 5").fetchall()
    for row in sample:
        print(row)
    
    conn.close()
    print("\nSaved to:", duckdb_path)

if __name__ == "__main__":
    main()
