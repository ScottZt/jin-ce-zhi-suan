# src/core/shangshu_sheng.py

from src.ministries.hu_bu_revenue import HuBuRevenue
from src.ministries.bing_bu_war import BingBuWar
from src.ministries.xing_bu_justice import XingBuJustice
from src.utils.constants import *

class ShangshuSheng:
    """
    尚书省 (Shangshu Sheng): 调度六部执行模拟成交、资金清算、持仓管理
    """
    def __init__(self, hu_bu: HuBuRevenue, bing_bu: BingBuWar, xing_bu: XingBuJustice):
        self.hu_bu = hu_bu
        self.bing_bu = bing_bu
        self.xing_bu = xing_bu
        self.positions = {} # Strategy ID -> {Stock Code -> Position Dict}
        
    def execute_order(self, strategy_id, signal, kline, hu_bu_account=None):
        """
        Execute an order (buy/sell).
        """
        direction = signal['direction']
        code = signal['code']
        qty = int(signal['qty'])
        hu_bu = hu_bu_account if hu_bu_account is not None else self.hu_bu
        
        # Simulate execution via War Ministry
        success, fill_price = self.bing_bu.match_order(signal, kline)
        
        if not success:
            self.xing_bu.record_rejection(strategy_id, 'EXEC_FAIL', "Execution failed", kline['dt'])
            return False

        if direction == 'BUY':
            cash_available = float(hu_bu.cash)
            if cash_available <= 0:
                self.xing_bu.record_rejection(strategy_id, 'EXEC_NO_CASH', "No available cash", kline['dt'])
                return False
            amount_probe = fill_price * qty
            cost_probe, _, _, _ = hu_bu.calculate_cost(amount_probe, direction, fill_price, qty)
            if amount_probe + cost_probe > cash_available:
                lo, hi = 0, qty
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    mid_amount = fill_price * mid
                    mid_cost, _, _, _ = hu_bu.calculate_cost(mid_amount, direction, fill_price, mid)
                    if mid_amount + mid_cost <= cash_available:
                        lo = mid
                    else:
                        hi = mid - 1
                qty = lo
                if qty <= 0:
                    self.xing_bu.record_rejection(strategy_id, 'EXEC_NO_CASH', "Insufficient cash after fee/slippage", kline['dt'])
                    return False
        amount = fill_price * qty
        cost, comm, stamp, transfer = hu_bu.calculate_cost(amount, direction, fill_price, qty)
        
        # Update Position
        if direction == 'BUY':
            if strategy_id not in self.positions:
                self.positions[strategy_id] = {}
            
            if code not in self.positions[strategy_id]:
                self.positions[strategy_id][code] = {
                    'qty': 0,
                    'avg_price': 0.0,
                    'market_value': 0.0,
                    'direction': 'BUY', # Default to Long
                    'stop_loss': signal.get('stop_loss'),
                    'take_profit': signal.get('take_profit')
                }
            
            pos = self.positions[strategy_id][code]
            new_qty = pos['qty'] + qty
            new_avg = (pos['avg_price'] * pos['qty'] + fill_price * qty + cost) / new_qty # Include cost in avg price? Usually cost is separate. Let's keep cost separate for PnL.
            # Standard avg price calculation: (old_val + new_val) / new_qty
            new_avg = (pos['avg_price'] * pos['qty'] + fill_price * qty) / new_qty
            
            pos['qty'] = new_qty
            pos['avg_price'] = new_avg
            pos['market_value'] = new_qty * fill_price # Mark to market immediately
            
            hu_bu.record_transaction(strategy_id, kline['dt'], 'BUY', fill_price, qty, cost)

        elif direction == 'SELL':
            if strategy_id not in self.positions or code not in self.positions[strategy_id]:
                 self.xing_bu.record_violation(strategy_id, 'SELL_NO_POS', f"Sell {code} without position", kline['dt'])
                 return False
            
            pos = self.positions[strategy_id][code]
            if pos['qty'] < qty:
                 self.xing_bu.record_violation(strategy_id, 'SELL_OVER_QTY', f"Sell {qty} > Holding {pos['qty']}", kline['dt'])
                 return False
            
            # Calculate Realized PnL
            # (Sell Price - Avg Buy Price) * Qty - Cost
            pnl = (fill_price - pos['avg_price']) * qty - cost
            
            pos['qty'] -= qty
            if pos['qty'] == 0:
                del self.positions[strategy_id][code]
            else:
                pos['market_value'] = pos['qty'] * fill_price

            hu_bu.record_transaction(strategy_id, kline['dt'], 'SELL', fill_price, qty, cost, pnl)
            
        return True

    def update_holdings_value(self, current_prices):
        """
        Update market value of all holdings based on current prices.
        """
        total_value = 0.0
        for strategy_id, stocks in self.positions.items():
            for code, pos in stocks.items():
                if code in current_prices:
                    price = current_prices[code]
                    pos['market_value'] = pos['qty'] * price
                    total_value += pos['market_value']
        return total_value

    def update_strategy_holdings_value(self, strategy_id, current_prices):
        total_value = 0.0
        stocks = self.positions.get(strategy_id, {})
        for code, pos in stocks.items():
            if code in current_prices:
                price = current_prices[code]
                pos['market_value'] = pos['qty'] * price
                total_value += pos['market_value']
        return total_value

    def check_stops(self, kline):
        """
        Check and trigger stop loss/take profit for all positions.
        """
        triggered_orders = []
        code = kline['code']
        
        for strategy_id, stocks in self.positions.items():
            if code in stocks:
                pos = stocks[code]
                triggered, type_, price = self.bing_bu.check_stop_orders(pos, kline)
                
                if triggered:
                    # Create a sell order immediately
                    order = {
                        'strategy_id': strategy_id,
                        'code': code,
                        'dt': kline['dt'], # Triggered at this time
                        'direction': 'SELL',
                        'qty': pos['qty'], # Close all for simplicity
                        'price': price, # Trigger price
                        'type': 'MARKET' # Execute immediately
                    }
                    triggered_orders.append(order)
                    self.xing_bu.record_circuit_break(strategy_id, f"{type_} triggered at {price}", kline['dt'])
        
        return triggered_orders
