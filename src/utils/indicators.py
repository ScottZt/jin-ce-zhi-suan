# src/utils/indicators.py
import pandas as pd
import numpy as np

class Indicators:
    """
    Technical Indicators Calculator & Data Resampler
    """
    
    @staticmethod
    def resample(df, rule):
        """
        Resample 1-min data to other timeframes.
        rule: '5min', '15min', '30min', '60min', 'D', 'W'
        """
        # Ensure dt is index
        if 'dt' in df.columns:
            df = df.set_index('dt')
        
        # Resample logic
        agg_dict = {
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'vol': 'sum',
            'amount': 'sum'
        }
        
        # Handle custom columns if exist
        if 'code' in df.columns:
            agg_dict['code'] = 'first'
            
        resampled = df.resample(rule).agg(agg_dict).dropna()
        
        # Reset index to keep dt as column
        return resampled.reset_index()

    @staticmethod
    def MA(series, window):
        return series.rolling(window=window).mean()

    @staticmethod
    def EMA(series, window):
        return series.ewm(span=window, adjust=False).mean()

    @staticmethod
    def MACD(close_series, fast=12, slow=26, signal=9):
        exp1 = Indicators.EMA(close_series, fast)
        exp2 = Indicators.EMA(close_series, slow)
        dif = exp1 - exp2
        dea = Indicators.EMA(dif, signal)
        macd = (dif - dea) * 2
        return dif, dea, macd

    @staticmethod
    def RSI(close_series, window=14):
        delta = close_series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.fillna(50)
        return rsi
        
    @staticmethod
    def KDJ(high, low, close, n=9, m1=3, m2=3):
        low_min = low.rolling(window=n).min()
        high_max = high.rolling(window=n).max()
        
        rsv = (close - low_min) / (high_max - low_min) * 100
        # Fix division by zero
        rsv = rsv.fillna(50)
        
        k = rsv.ewm(alpha=1/m1, adjust=False).mean()
        d = k.ewm(alpha=1/m2, adjust=False).mean()
        j = 3 * k - 2 * d
        return k, d, j

    @staticmethod
    def ATR(high, low, close, window=14):
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window=window).mean()

    @staticmethod
    def BollingerBands(close, window=20, num_std=2):
        ma = close.rolling(window=window).mean()
        std = close.rolling(window=window).std()
        upper = ma + (std * num_std)
        lower = ma - (std * num_std)
        return upper, ma, lower

    @staticmethod
    def sma(series, window=14, period=None, **kwargs):
        w = period if period is not None else window
        return Indicators.MA(series, int(w))

    @staticmethod
    def ema(series, window=14, period=None, **kwargs):
        w = period if period is not None else window
        return Indicators.EMA(series, int(w))

    @staticmethod
    def macd(close_series, fast=12, slow=26, signal=9, fastperiod=None, slowperiod=None, signalperiod=None, **kwargs):
        f = fastperiod if fastperiod is not None else fast
        s = slowperiod if slowperiod is not None else slow
        g = signalperiod if signalperiod is not None else signal
        return Indicators.MACD(close_series, fast=int(f), slow=int(s), signal=int(g))

    @staticmethod
    def rsi(close_series, window=14, period=None, timeperiod=None, **kwargs):
        w = period if period is not None else (timeperiod if timeperiod is not None else window)
        return Indicators.RSI(close_series, window=int(w))

    @staticmethod
    def kdj(high, low, close, n=9, m1=3, m2=3):
        return Indicators.KDJ(high, low, close, n=n, m1=m1, m2=m2)

    @staticmethod
    def atr(high, low, close, window=14):
        return Indicators.ATR(high, low, close, window=window)

    @staticmethod
    def bollinger_bands(close, window=20, num_std=2):
        return Indicators.BollingerBands(close, window=window, num_std=num_std)
