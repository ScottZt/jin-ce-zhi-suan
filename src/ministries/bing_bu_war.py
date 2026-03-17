# src/ministries/bing_bu_war.py

from src.utils.constants import *
from src.utils.runtime_params import get_value

class BingBuWar:
    """
    兵部 (War): 模拟下单、撮合、止盈止损触发
    """
    def __init__(self):
        pass

    def match_order(self, order, kline):
        """
        Simulate order matching against a K-line.
        Returns: (success, fill_price, cost)
        """
        # Assume market order at open of next bar (as per rules: "Entry: Next 1-min open")
        # Or limit order if price within high/low.
        
        # For simplicity, we assume market orders execute at Open price of the current bar (which is the "next" bar relative to signal generation).
        # Slippage is applied.
        
        direction = order['direction'] # 'BUY' or 'SELL'
        price = kline['open']
        slippage = float(get_value("execution.slippage", SLIPPAGE))
        
        # Apply slippage
        if direction == 'BUY':
            fill_price = price * (1 + slippage)
        else:
            fill_price = price * (1 - slippage)
            
        # Check if price is within K-line range (it is, since we use Open)
        # But if it's a stop loss/take profit trigger, we check against High/Low.
        
        return True, fill_price

    def check_stop_orders(self, position, kline):
        """
        Check if stop loss or take profit is triggered.
        Returns: (triggered, type, price)
        """
        if not position:
            return False, None, 0.0

        # Long position logic
        if position['direction'] == 'BUY':
            # Stop Loss
            if kline['low'] <= position['stop_loss']:
                # Triggered at stop price or open if gap down, but rule says "Triggered immediately"
                # Conservative: min(open, stop_loss) if gap? 
                # Rule: "Triggered at high/low" -> We use the stop price itself if within range, else Open.
                fill_price = position['stop_loss'] 
                # If Open is already below stop loss (gap down), use Open.
                if kline['open'] < position['stop_loss']:
                    fill_price = kline['open']
                return True, 'STOP_LOSS', fill_price
            
            # Take Profit
            if position.get('take_profit') and kline['high'] >= position['take_profit']:
                 fill_price = position['take_profit']
                 if kline['open'] > position['take_profit']:
                     fill_price = kline['open']
                 return True, 'TAKE_PROFIT', fill_price

        return False, None, 0.0
