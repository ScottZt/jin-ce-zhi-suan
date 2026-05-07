-- DuckDB 建表脚本
-- 用于金策智算量化系统

-- 1分钟K线表
CREATE TABLE IF NOT EXISTS dat_1mins (
    code VARCHAR NOT NULL,
    trade_time TIMESTAMP NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    vol DOUBLE,
    amount DOUBLE,
    PRIMARY KEY (code, trade_time)
);

-- 5分钟K线表
CREATE TABLE IF NOT EXISTS dat_5mins (
    code VARCHAR NOT NULL,
    trade_time TIMESTAMP NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    vol DOUBLE,
    amount DOUBLE,
    PRIMARY KEY (code, trade_time)
);

-- 日线表
CREATE TABLE IF NOT EXISTS dat_day (
    code VARCHAR NOT NULL,
    trade_time TIMESTAMP NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    vol DOUBLE,
    amount DOUBLE,
    PRIMARY KEY (code, trade_time)
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_1mins_code ON dat_1mins(code);
CREATE INDEX IF NOT EXISTS idx_1mins_time ON dat_1mins(trade_time);
CREATE INDEX IF NOT EXISTS idx_5mins_code ON dat_5mins(code);
CREATE INDEX IF NOT EXISTS idx_day_code ON dat_day(code);
