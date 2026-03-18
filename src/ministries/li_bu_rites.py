# src/ministries/li_bu_rites.py
import pandas as pd
import numpy as np

class LiBuRites:
    """
    礼部 (Rites): 生成每套策略独立业绩报表、排行榜
    """
    def generate_report(self, strategy_id, hu_bu, xing_bu, initial_capital, start_date=None, end_date=None):
        """
        Generate performance report for a single strategy.
        """
        # Get transactions for this strategy
        transactions = [t for t in hu_bu.transactions if t['strategy_id'] == strategy_id]
        
        # Calculate basic metrics
        total_trades = len([t for t in transactions if t['direction'] == 'SELL']) # Count closed trades
        wins = len([t for t in transactions if t.get('pnl', 0) > 0])
        losses = len([t for t in transactions if t.get('pnl', 0) <= 0 and t['direction'] == 'SELL'])
        
        win_rate = wins / total_trades if total_trades > 0 else 0.0
        
        total_pnl = sum([t.get('pnl', 0) for t in transactions])
        final_capital = initial_capital + total_pnl # Simplified: Assuming allocated capital or proportional share
        
        roi = total_pnl / initial_capital
        
        # Drawdown calculation requires daily NAV for this strategy specifically
        # HuBu tracks total portfolio NAV. We need strategy-specific NAV or PnL curve.
        # Let's approximate using transaction history for now.
        
        cumulative_pnl = 0.0
        peak = 0.0
        max_dd = 0.0
        
        pnl_curve = []
        for t in transactions:
            if t['direction'] == 'SELL':
                cumulative_pnl += t['pnl']
                pnl_curve.append(cumulative_pnl)
                if cumulative_pnl > peak:
                    peak = cumulative_pnl
                dd = peak - cumulative_pnl
                if dd > max_dd:
                    max_dd = dd
                    
        # Max Drawdown % (relative to initial capital + peak profit)
        max_dd_pct = max_dd / initial_capital # Simplified
        
        avg_win = np.mean([t['pnl'] for t in transactions if t.get('pnl', 0) > 0]) if wins > 0 else 0.0
        avg_loss = np.mean([t['pnl'] for t in transactions if t.get('pnl', 0) <= 0 and t['direction'] == 'SELL']) if losses > 0 else 0.0
        profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
        
        rejections = xing_bu.get_rejection_count(strategy_id)
        violations = xing_bu.get_violation_count(strategy_id)
        circuit_breaks = xing_bu.get_circuit_break_count(strategy_id)

        # Annualized Return
        # Assuming we know the duration. But here we just have transactions.
        # We need start and end date of backtest.
        # Let's approximate using transaction dates range if available, else pass in duration.
        if start_date is not None and end_date is not None:
            days = max((end_date - start_date).days, 1)
            annualized_roi = (1 + roi) ** (252 / days) - 1
        elif transactions:
            tx_start = min(t['dt'] for t in transactions)
            tx_end = max(t['dt'] for t in transactions)
            days = max((tx_end - tx_start).days, 1)
            annualized_roi = (1 + roi) ** (252 / days) - 1
        else:
            annualized_roi = 0.0
        if isinstance(annualized_roi, complex):
            annualized_roi = -1.0
            
        # Sharpe Ratio
        # Need daily returns.
        # We can reconstruct daily PnL from transactions + holding change.
        # This is complex without daily NAV history.
        # Approximation: Use per-trade returns? No, Sharpe is time-based.
        # Let's use a simplified Sharpe based on average trade return / std dev of trade return * sqrt(trades per year)
        # This is "Trade Sharpe", not "Time Sharpe".
        # Better: Since we don't have daily NAV per strategy easily, let's use 0.0 placeholder or estimate.
        sharpe = 0.0
        
        calmar = (annualized_roi / max_dd_pct) if max_dd_pct > 0 else 0.0
        
        return {
            'strategy_id': strategy_id,
            'total_pnl': total_pnl,  # Added this field
            'roi': roi,
            'annualized_roi': annualized_roi,
            'max_dd': max_dd_pct,
            'win_rate': win_rate,
            'profit_factor': profit_factor,
            'total_trades': total_trades,
            'rejections': rejections,
            'violations': violations,
            'circuit_breaks': circuit_breaks,
            'calmar': calmar,
            'sharpe': sharpe
        }

    def generate_ranking(self, reports):
        """
        Generate ranking table.
        """
        df = pd.DataFrame(reports)
        if df.empty:
            return df
            
        # Rank by Calmar Ratio
        df['rank'] = df['calmar'].rank(ascending=False).astype(int)
        df = df.sort_values('rank')
        
        # Add Rating
        def get_rating(row):
            if row['violations'] > 0:
                return 'D'
            if row['roi'] > 0.2 and row['max_dd'] < 0.1:
                return 'A'
            if row['roi'] > 0:
                return 'B'
            return 'C' # Negative ROI
            
        df['rating'] = df.apply(get_rating, axis=1)
        
        return df

    def generate_backtest_report(self, strategy_id, transactions, initial_capital, start_date=None, end_date=None):
        closed_trades = [t for t in transactions if t.get('direction') == 'SELL']
        trade_count = len(closed_trades)
        win_trades = [t for t in closed_trades if t.get('pnl', 0) > 0]
        loss_trades = [t for t in closed_trades if t.get('pnl', 0) <= 0]

        win_num = len(win_trades)
        loss_num = len(loss_trades)
        win_rate = win_num / trade_count if trade_count > 0 else 0.0

        total_pnl = sum(t.get('pnl', 0.0) for t in closed_trades)
        init_cap = float(initial_capital)
        end_cap = init_cap + total_pnl
        total_return = (end_cap / init_cap - 1) if init_cap != 0 else 0.0

        if start_date is not None and end_date is not None:
            days = max((end_date - start_date).days, 1)
            start_txt = str(start_date)[:10]
            end_txt = str(end_date)[:10]
        elif closed_trades:
            trade_dates = [t['dt'] for t in closed_trades]
            sdt = min(trade_dates)
            edt = max(trade_dates)
            days = max((edt - sdt).days, 1)
            start_txt = str(sdt)[:10]
            end_txt = str(edt)[:10]
        else:
            days = 1
            start_txt = "--"
            end_txt = "--"

        annual_return = (1 + total_return) ** (252 / days) - 1 if days > 0 else 0.0

        equity = [init_cap]
        for t in closed_trades:
            equity.append(equity[-1] + t.get('pnl', 0.0))
        equity_series = pd.Series(equity)
        max_value = equity_series.cummax()
        drawdown = (equity_series - max_value) / max_value
        max_drawdown = drawdown.min() if not drawdown.empty else 0.0

        avg_win = float(np.mean([t.get('pnl', 0.0) for t in win_trades])) if win_trades else 0.0
        avg_loss = abs(float(np.mean([t.get('pnl', 0.0) for t in loss_trades]))) if loss_trades else 0.0
        profit_ratio = (avg_win / avg_loss) if avg_loss != 0 else 0.0

        print("\n" + "=" * 55)
        print("               📊 策略回测报告 📊")
        print("=" * 55)
        print(f"策略编号：{strategy_id}")
        print(f"回测周期：{start_txt} ~ {end_txt}")
        print(f"初始资金：{init_cap:.2f} 元")
        print(f"结束资金：{end_cap:.2f} 元")
        print(f"总收益：{total_return:.2%}")
        print(f"年化收益：{annual_return:.2%}")
        print(f"最大回撤：{max_drawdown:.2%}")
        print(f"总交易次数：{trade_count}")
        print(f"盈利次数：{win_num}  | 亏损次数：{loss_num}")
        print(f"胜率：{win_rate:.2%}")
        print(f"平均盈利：{avg_win:.2f}  | 平均亏损：{avg_loss:.2f}")
        print(f"盈亏比：{profit_ratio:.2f}")
        print("=" * 55 + "\n")
        trade_details = []
        for t in transactions:
            trade_details.append({
                "dt": str(t.get("dt", "")),
                "direction": str(t.get("direction", "")),
                "price": float(t.get("price", 0.0) or 0.0),
                "quantity": int(t.get("quantity", 0) or 0),
                "amount": float(t.get("amount", 0.0) or 0.0),
                "cost": float(t.get("cost", 0.0) or 0.0),
                "pnl": float(t.get("pnl", 0.0) or 0.0)
            })

        return {
            "strategy_id": strategy_id,
            "start_date": start_txt,
            "end_date": end_txt,
            "init_capital": init_cap,
            "end_capital": end_cap,
            "total_return": total_return,
            "annual_return": annual_return,
            "max_drawdown": float(max_drawdown),
            "trade_count": trade_count,
            "win_num": win_num,
            "loss_num": loss_num,
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_ratio": profit_ratio,
            "trade_details": trade_details
        }
