# src/strategies/my_strategy.py
from src.strategies.implemented_strategies import BaseImplementedStrategy
from src.utils.indicators import Indicators
import pandas as pd
import numpy as np

class MyStrategy(BaseImplementedStrategy):
    """
    自定义策略示例：双均线+MACD策略
    """
    def __init__(self):
        super().__init__("MY01", "双均线MACD策略", trigger_timeframe="D")
        self.history = {}
    
    def on_bar(self, kline):
        """
        每根K线调用一次
        """
        code = kline['code']
        self.update_holding_time(code)
        
        # 维护历史数据
        if code not in self.history:
            self.history[code] = pd.DataFrame()
        new_row = pd.DataFrame([kline])
        self.history[code] = pd.concat([self.history[code], new_row], ignore_index=True)
        
        # 保留最近200根K线
        if len(self.history[code]) > 200:
            self.history[code] = self.history[code].iloc[-200:]
        
        df = self.history[code]
        if len(df) < 60:
            return None
        
        # 计算指标
        df['ma5'] = Indicators.MA(df['close'], 5)
        df['ma20'] = Indicators.MA(df['close'], 20)
        
        # 获取最新值
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else last
        
        qty = self.positions.get(code, 0)
        
        # 买入条件：5日均线上穿20日均线
        if qty <= 0 and last['ma5'] > last['ma20'] and prev['ma5'] <= prev['ma20']:
            buy_qty = self._qty()
            if buy_qty > 0:
                return {
                    'strategy_id': self.id,
                    'code': code,
                    'dt': kline['dt'],
                    'direction': 'BUY',
                    'price': kline['close'],
                    'qty': buy_qty,
                    'stop_loss': kline['close'] * 0.95,  # 5%止损
                    'take_profit': kline['close'] * 1.10  # 10%止盈
                }
        
        # 卖出条件：5日均线下穿20日均线 或 达到止盈止损
        if qty > 0:
            # 均线死叉
            if last['ma5'] < last['ma20'] and prev['ma5'] >= prev['ma20']:
                return self.create_exit_signal(kline, qty, "MA Death Cross")
            
            # 检查最大持仓时间（10天）
            if self.check_max_holding_time(code, 10):
                return self.create_exit_signal(kline, qty, "Max Holding Time")
        
        return None
