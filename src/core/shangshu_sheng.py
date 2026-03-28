# src/core/shangshu_sheng.py

from src.ministries.hu_bu_revenue import HuBuRevenue
from src.ministries.bing_bu_war import BingBuWar
from src.ministries.xing_bu_justice import XingBuJustice
from src.utils.constants import *
import pandas as pd

class ShangshuSheng:
    """
    尚书省 (Shangshu Sheng): 调度六部执行模拟成交、资金清算、持仓管理
    """
    def __init__(self, hu_bu: HuBuRevenue, bing_bu: BingBuWar, xing_bu: XingBuJustice):
        self.hu_bu = hu_bu
        self.bing_bu = bing_bu
        self.xing_bu = xing_bu
        self.positions = {} # Strategy ID -> {Stock Code -> Position Dict}

    def _trade_day(self, dt_value):
        x = pd.to_datetime(dt_value, errors='coerce')
        if pd.isna(x):
            return ""
        return x.strftime("%Y-%m-%d")

    def _ensure_lots(self, pos):
        lots = pos.get('lots')
        if isinstance(lots, list):
            normalized = []
            for item in lots:
                if not isinstance(item, dict):
                    continue
                q = int(item.get('qty', 0) or 0)
                if q <= 0:
                    continue
                normalized.append({
                    'qty': q,
                    'buy_day': str(item.get('buy_day', '')).strip(),
                    'unit_cost': float(item.get('unit_cost', pos.get('avg_price', 0.0)) or 0.0)
                })
            pos['lots'] = normalized
            return pos['lots']
        legacy_qty = int(pos.get('qty', 0) or 0)
        if legacy_qty <= 0:
            pos['lots'] = []
            return pos['lots']
        pos['lots'] = [{
            'qty': legacy_qty,
            'buy_day': str(pos.get('last_buy_day', '')).strip(),
            'unit_cost': float(pos.get('avg_price', 0.0) or 0.0)
        }]
        return pos['lots']

    def _rebuild_position_from_lots(self, pos, mark_price):
        lots = self._ensure_lots(pos)
        total_qty = sum(int(x.get('qty', 0) or 0) for x in lots)
        if total_qty <= 0:
            pos['qty'] = 0
            pos['avg_price'] = 0.0
            pos['market_value'] = 0.0
            pos['last_buy_day'] = ''
            return
        total_cost = sum(float(x.get('unit_cost', 0.0) or 0.0) * int(x.get('qty', 0) or 0) for x in lots)
        pos['qty'] = total_qty
        pos['avg_price'] = total_cost / total_qty
        pos['market_value'] = float(mark_price) * total_qty
        buy_days = sorted([str(x.get('buy_day', '')).strip() for x in lots if str(x.get('buy_day', '')).strip()])
        pos['last_buy_day'] = buy_days[-1] if buy_days else ''

    def _sellable_qty_t1(self, pos, curr_day):
        lots = self._ensure_lots(pos)
        return sum(int(x.get('qty', 0) or 0) for x in lots if str(x.get('buy_day', '')).strip() != str(curr_day or '').strip())

    def _consume_lots_fifo(self, pos, sell_qty, curr_day):
        lots = self._ensure_lots(pos)
        need = int(sell_qty or 0)
        consumed = []
        for lot in lots:
            if need <= 0:
                break
            lot_day = str(lot.get('buy_day', '')).strip()
            if lot_day == str(curr_day or '').strip():
                continue
            can_take = min(int(lot.get('qty', 0) or 0), need)
            if can_take <= 0:
                continue
            lot['qty'] = int(lot.get('qty', 0) or 0) - can_take
            consumed.append({'qty': can_take, 'unit_cost': float(lot.get('unit_cost', 0.0) or 0.0)})
            need -= can_take
        if need > 0:
            return None
        pos['lots'] = [x for x in lots if int(x.get('qty', 0) or 0) > 0]
        return consumed
        
    def execute_order(self, strategy_id, signal, kline, hu_bu_account=None):
        """
        Execute an order (buy/sell).
        """
        direction = str(signal['direction']).upper()
        code = signal['code']
        qty = int(float(signal['qty']))
        hu_bu = hu_bu_account if hu_bu_account is not None else self.hu_bu
        lot_size = 100
        if direction not in {'BUY', 'SELL'}:
            self.xing_bu.record_rejection(strategy_id, 'EXEC_DIR_INVALID', f"Invalid direction: {direction}", kline['dt'])
            return False
        if qty <= 0:
            self.xing_bu.record_rejection(strategy_id, 'EXEC_QTY_INVALID', f"Invalid qty: {qty}", kline['dt'])
            return False
        if direction == 'BUY':
            qty = (qty // lot_size) * lot_size
            if qty < lot_size:
                self.xing_bu.record_rejection(strategy_id, 'EXEC_LOT_BLOCK', f"BUY qty must be >= {lot_size} and lot-sized", kline['dt'])
                return False
        
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
                lo, hi = 0, qty // lot_size
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    mid_qty = mid * lot_size
                    mid_amount = fill_price * mid_qty
                    mid_cost, _, _, _ = hu_bu.calculate_cost(mid_amount, direction, fill_price, mid_qty)
                    if mid_amount + mid_cost <= cash_available:
                        lo = mid
                    else:
                        hi = mid - 1
                qty = lo * lot_size
                if qty < lot_size:
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
                    'take_profit': signal.get('take_profit'),
                    'lots': []
                }
            
            pos = self.positions[strategy_id][code]
            lots = self._ensure_lots(pos)
            lots.append({
                'qty': qty,
                'buy_day': self._trade_day(kline.get('dt')),
                'unit_cost': float(fill_price)
            })
            self._rebuild_position_from_lots(pos, fill_price)
            
            hu_bu.record_transaction(strategy_id, kline['dt'], 'BUY', fill_price, qty, cost)

        elif direction == 'SELL':
            if strategy_id not in self.positions or code not in self.positions[strategy_id]:
                 self.xing_bu.record_violation(strategy_id, 'SELL_NO_POS', f"Sell {code} without position", kline['dt'])
                 return False
            
            pos = self.positions[strategy_id][code]
            pos_qty = int(pos.get('qty', 0) or 0)
            if qty > pos_qty:
                 self.xing_bu.record_violation(strategy_id, 'SELL_OVER_QTY', f"Sell {qty} > Holding {pos_qty}", kline['dt'])
                 return False
            if qty % lot_size != 0 and qty != pos_qty:
                self.xing_bu.record_rejection(strategy_id, 'EXEC_LOT_BLOCK', f"SELL qty must be lot-sized or equal to full position ({pos_qty})", kline['dt'])
                return False
            curr_day = self._trade_day(kline.get('dt'))
            sellable_qty = self._sellable_qty_t1(pos, curr_day)
            if qty > sellable_qty:
                self.xing_bu.record_rejection(strategy_id, 'EXEC_T1_BLOCK', f"T+1 block: {code} sellable {sellable_qty} < request {qty}", kline['dt'])
                return False
            consumed = self._consume_lots_fifo(pos, qty, curr_day)
            if consumed is None:
                self.xing_bu.record_rejection(strategy_id, 'EXEC_T1_BLOCK', f"T+1 block: {code} insufficient sellable lots", kline['dt'])
                return False
            cost_basis = sum(float(x.get('unit_cost', 0.0) or 0.0) * int(x.get('qty', 0) or 0) for x in consumed)
            
            # Calculate Realized PnL
            pnl = (fill_price * qty) - cost_basis - cost
            self._rebuild_position_from_lots(pos, fill_price)
            if int(pos.get('qty', 0) or 0) == 0:
                del self.positions[strategy_id][code]

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
                curr_day = self._trade_day(kline.get('dt'))
                sellable_qty = self._sellable_qty_t1(pos, curr_day)
                if sellable_qty <= 0:
                    continue
                triggered, type_, price = self.bing_bu.check_stop_orders(pos, kline)
                
                if triggered:
                    # Create a sell order immediately
                    order = {
                        'strategy_id': strategy_id,
                        'code': code,
                        'dt': kline['dt'], # Triggered at this time
                        'direction': 'SELL',
                        'qty': sellable_qty,
                        'price': price, # Trigger price
                        'type': 'MARKET' # Execute immediately
                    }
                    triggered_orders.append(order)
                    self.xing_bu.record_circuit_break(strategy_id, f"{type_} triggered at {price}", kline['dt'])
        
        return triggered_orders
