# Checkmate 将死交易系统 - 趋势回调策略
from src.strategies.base_strategy import BaseStrategy
from src.strategies.implemented_strategies import BaseImplementedStrategy
from src.utils.indicators import Indicators
import pandas as pd
import numpy as np
from src.utils.runtime_params import get_value

class StrategyCM(BaseImplementedStrategy):
    """
    Checkmate 将死交易系统
    核心逻辑：趋势跟踪 + 回调入场 + 动态风控
    """
    def __init__(self):
        super().__init__("CM", "Checkmate将死趋势回调", trigger_timeframe="D")
        self.history = {}
        self.entry_bar_high = {}  # 开仓K线前3根高点
        self.entry_bar_low = {}   # 开仓K线前3根低点
        
    def on_bar(self, kline):
        code = kline['code']
        
        # 维护历史数据
        if code not in self.history:
            self.history[code] = pd.DataFrame()
        self.history[code] = pd.concat([self.history[code], pd.DataFrame([kline])], ignore_index=True).tail(200)
        
        df = self.history[code]
        if len(df) < 60:  # 至少需要60根K线计算EMA
            return None
            
        # 计算指标
        close = df['close']
        high = df['high']
        low = df['low']
        open_price = df['open']
        
        # EMA 双均线
        ema20 = Indicators.EMA(close, 20)
        ema50 = Indicators.EMA(close, 50)
        
        if len(ema20) < 2 or len(ema50) < 2:
            return None
            
        current_close = float(close.iloc[-1])
        current_open = float(open_price.iloc[-1])
        current_high = float(high.iloc[-1])
        current_low = float(low.iloc[-1])
        
        ema20_now = float(ema20.iloc[-1])
        ema20_prev = float(ema20.iloc[-2])
        ema50_now = float(ema50.iloc[-1])
        
        # 趋势判断
        bullish_trend = ema20_now > ema50_now and ema20_now > ema20_prev  # 多头趋势
        bearish_trend = ema20_now < ema50_now and ema20_now < ema20_prev  # 空头趋势
        
        # 获取配置参数
        pullback_pct = float(self._cfg("pullback_pct", 0.01))  # 回调幅度阈值
        trailing_stop_pct = float(self._cfg("trailing_stop_pct", 0.02))  # 追踪止损
        stop_loss_pct = float(self._cfg("stop_loss_pct", 0.03))  # 固定止损
        
        qty = int(self.positions.get(code, 0))
        
        # ===== 开仓逻辑 =====
        if qty == 0:
            # 多头入场：趋势向上 + 回调至EMA20附近 + 阳线确认
            if bullish_trend:
                # 价格回调至EMA20附近（1%范围内）
                pullback_zone = ema20_now * (1 - pullback_pct) <= current_close <= ema20_now * (1 + pullback_pct)
                # 阳线确认
                bullish_candle = current_close > current_open
                
                if pullback_zone and bullish_candle:
                    # 记录开仓前3根K线极值作为止损
                    if len(df) >= 4:
                        prev3_high = float(high.iloc[-4:-1].max())
                        prev3_low = float(low.iloc[-4:-1].min())
                        self.entry_bar_high[code] = prev3_high
                        self.entry_bar_low[code] = prev3_low
                    
                    buy_qty = self._qty()
                    if buy_qty > 0:
                        return {
                            'strategy_id': self.id,
                            'code': code,
                            'dt': kline['dt'],
                            'direction': 'BUY',
                            'price': current_close,
                            'qty': buy_qty,
                            'stop_loss': self.entry_bar_low.get(code, current_close * (1 - stop_loss_pct)),
                            'take_profit': None,
                            'reason': 'Checkmate_Bullish_Pullback'
                        }
            
            # 空头入场（可选，A股限制做空，可禁用）
            # if bearish_trend and ...
        
        # ===== 持仓管理 =====
        else:
            entry_price = self.entry_price.get(code, current_close)
            
            # 更新最高价（用于追踪止损）
            if code not in self.highest_high:
                self.highest_high[code] = entry_price
            self.highest_high[code] = max(self.highest_high.get(code, 0), current_high)
            
            # 计算浮动盈亏
            unrealized_pct = (current_close - entry_price) / entry_price if entry_price > 0 else 0
            
            # 动态止盈：有盈利后启动追踪止损
            if unrealized_pct > 0:
                trailing_stop_price = self.highest_high.get(code, current_close) * (1 - trailing_stop_pct)
                if current_close <= trailing_stop_price:
                    return self.create_exit_signal(kline, qty, f'Trailing_Stop_{trailing_stop_pct*100:.0f}%')
            
            # 固定止损：跌破开仓前3根K线低点
            stop_price = self.entry_bar_low.get(code, entry_price * (1 - stop_loss_pct))
            if current_close <= stop_price:
                return self.create_exit_signal(kline, qty, 'Fixed_Stop_Loss')
        
        return None
