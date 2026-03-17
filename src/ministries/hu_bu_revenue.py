# src/ministries/hu_bu_revenue.py
import pandas as pd
from src.utils.constants import *
from src.utils.runtime_params import get_value

class HuBuRevenue:
    """
    户部 (Revenue): 逐笔核算资金、净值、手续费、印花税、总成本
    """
    def __init__(self, initial_capital):
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.transactions = []
        self.daily_nav = [] # Date, NAV, Cash, Holdings Value
        self.total_commission = 0.0
        self.total_stamp_duty = 0.0
        self.total_transfer_fee = 0.0

    def calculate_cost(self, amount, direction, price, quantity):
        """
        Calculate transaction cost.
        Returns: (total_cost, commission, stamp_duty, transfer_fee)
        """
        # Commission: Max(5, amount * 0.00025)
        min_commission = float(get_value("trading_cost.min_commission", MIN_COMMISSION))
        commission_rate = float(get_value("trading_cost.commission_rate", COMMISSION_RATE))
        stamp_duty_rate = float(get_value("trading_cost.stamp_duty", STAMP_DUTY))
        transfer_fee_rate = float(get_value("trading_cost.transfer_fee", TRANSFER_FEE))
        commission = max(min_commission, amount * commission_rate)
        
        # Stamp Duty: 0.1% on SELL only
        stamp_duty = 0.0
        if direction == 'SELL':
            stamp_duty = amount * stamp_duty_rate
            
        # Transfer Fee: amount * 0.00001
        transfer_fee = amount * transfer_fee_rate
        
        total_cost = commission + stamp_duty + transfer_fee
        return total_cost, commission, stamp_duty, transfer_fee

    def record_transaction(self, strategy_id, dt, direction, price, quantity, cost, pnl=0.0):
        """
        Record a transaction.
        """
        amount = price * quantity
        
        self.transactions.append({
            'strategy_id': strategy_id,
            'dt': dt,
            'direction': direction,
            'price': price,
            'quantity': quantity,
            'amount': amount,
            'cost': cost,
            'pnl': pnl # PnL is realized PnL for closing trades
        })
        
        if direction == 'BUY':
            self.cash -= (amount + cost)
        elif direction == 'SELL':
            self.cash += (amount - cost) # Proceeds - Cost
            
        self.total_commission += cost # simplified tracking, needs breakdown if required
        
    def update_daily_nav(self, dt, holdings_value):
        nav = self.cash + holdings_value
        self.daily_nav.append({
            'dt': dt,
            'nav': nav,
            'cash': self.cash,
            'holdings': holdings_value
        })
        return nav

    def get_nav_history(self):
        return pd.DataFrame(self.daily_nav)
