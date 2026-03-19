# src/ministries/li_bu_rites.py
import pandas as pd
import numpy as np


class LiBuRites:
    """
    礼部 (Rites): 生成每套策略独立业绩报表、排行榜
    """
    def _safe_float(self, value, default=0.0):
        try:
            v = float(value)
            if np.isnan(v) or np.isinf(v):
                return float(default)
            return v
        except Exception:
            return float(default)

    def _closed_trades(self, transactions):
        return [t for t in transactions if str(t.get("direction", "")).upper() == "SELL"]

    def _compute_streaks(self, closed_trades):
        max_win = 0
        max_loss = 0
        cur_win = 0
        cur_loss = 0
        for t in sorted(closed_trades, key=lambda x: x.get("dt")):
            pnl = self._safe_float(t.get("pnl", 0.0))
            if pnl > 0:
                cur_win += 1
                cur_loss = 0
            else:
                cur_loss += 1
                cur_win = 0
            if cur_win > max_win:
                max_win = cur_win
            if cur_loss > max_loss:
                max_loss = cur_loss
        return max_win, max_loss

    def _compute_monthly_profit_ratio(self, closed_trades):
        if not closed_trades:
            return 0.0
        rows = []
        for t in closed_trades:
            dt = pd.to_datetime(t.get("dt"))
            if pd.isna(dt):
                continue
            rows.append({"month": dt.strftime("%Y-%m"), "pnl": self._safe_float(t.get("pnl", 0.0))})
        if not rows:
            return 0.0
        df = pd.DataFrame(rows)
        monthly = df.groupby("month", as_index=False)["pnl"].sum()
        if monthly.empty:
            return 0.0
        return float((monthly["pnl"] > 0).sum() / len(monthly))

    def _compute_equity_curve(self, closed_trades, initial_capital, start_date=None, end_date=None):
        init_cap = self._safe_float(initial_capital)
        if init_cap <= 0:
            init_cap = 1.0
        if start_date is not None and end_date is not None:
            start = pd.to_datetime(start_date).normalize()
            end = pd.to_datetime(end_date).normalize()
        elif closed_trades:
            dts = [pd.to_datetime(t.get("dt")) for t in closed_trades if not pd.isna(pd.to_datetime(t.get("dt")))]
            if dts:
                start = min(dts).normalize()
                end = max(dts).normalize()
            else:
                now = pd.Timestamp.now().normalize()
                start = now
                end = now
        else:
            now = pd.Timestamp.now().normalize()
            start = now
            end = now
        if pd.isna(start) or pd.isna(end) or end < start:
            end = start
        all_days = pd.date_range(start=start, end=end, freq="D")
        if len(all_days) == 0:
            all_days = pd.DatetimeIndex([start])
        daily_pnl = pd.Series(0.0, index=all_days)
        for t in closed_trades:
            dt = pd.to_datetime(t.get("dt"))
            if pd.isna(dt):
                continue
            key = dt.normalize()
            if key not in daily_pnl.index:
                continue
            daily_pnl.loc[key] += self._safe_float(t.get("pnl", 0.0))
        equity = init_cap + daily_pnl.cumsum()
        equity = equity.clip(lower=1e-6)
        return equity

    def _compute_sharpe(self, equity, rf=0.02):
        if equity is None or len(equity) < 3:
            return 0.0
        rets = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        if rets.empty:
            return 0.0
        daily_rf = (1 + rf) ** (1 / 252) - 1
        excess = rets - daily_rf
        vol = excess.std(ddof=0)
        if vol <= 1e-12:
            return 0.0
        return float((excess.mean() / vol) * np.sqrt(252))

    def _compute_regime_consistency_ratio(self, equity):
        if equity is None or len(equity) < 15:
            return 0.0
        ret = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        if ret.empty:
            return 0.0
        bull = ret[ret > 0.002]
        bear = ret[ret < -0.002]
        side = ret[(ret >= -0.002) & (ret <= 0.002)]
        vals = []
        for seg in [bull, bear, side]:
            if len(seg) == 0:
                continue
            vals.append(1.0 if seg.mean() > 0 else 0.0)
        if not vals:
            return 0.0
        return float(np.mean(vals))

    def _score_piecewise(self, value, thresholds):
        v = self._safe_float(value)
        for cond, score in thresholds:
            if cond(v):
                return float(score)
        return 0.0

    def _grade_from_score(self, score):
        s = self._safe_float(score)
        if s >= 90:
            return "S", "可实盘、可加仓"
        if s >= 75:
            return "A", "可小资金实盘"
        if s >= 60:
            return "B", "继续优化"
        return "C", "放弃或重构"

    def _build_scorecard(self, metrics):
        annualized_roi = self._safe_float(metrics.get("annualized_roi", 0.0))
        total_return = self._safe_float(metrics.get("roi", 0.0))
        win_rate = self._safe_float(metrics.get("win_rate", 0.0))
        max_dd = self._safe_float(metrics.get("max_dd", 0.0))
        sharpe = self._safe_float(metrics.get("sharpe", 0.0))
        calmar = self._safe_float(metrics.get("calmar", 0.0))
        profit_ratio = self._safe_float(metrics.get("profit_factor", 0.0))
        max_loss_streak = int(metrics.get("max_loss_streak", 0) or 0)
        monthly_profit_ratio = self._safe_float(metrics.get("monthly_profit_ratio", 0.0))
        annual_trades = self._safe_float(metrics.get("annualized_trades", 0.0))
        avg_trade_amount_ratio = self._safe_float(metrics.get("avg_trade_amount_ratio", 0.0))
        monthly_stability = self._safe_float(metrics.get("monthly_stability_ratio", 0.0))
        regime_consistency = self._safe_float(metrics.get("regime_consistency_ratio", 0.0))

        score_ar = self._score_piecewise(annualized_roi, [
            (lambda x: x >= 0.30, 10),
            (lambda x: x >= 0.15, 7),
            (lambda x: x >= 0.08, 4),
            (lambda x: x > 0.00, 2),
            (lambda x: True, 0),
        ])
        score_tr = self._score_piecewise(total_return, [
            (lambda x: x >= 0.40, 10),
            (lambda x: x >= 0.20, 7),
            (lambda x: x >= 0.10, 4),
            (lambda x: x > 0.00, 2),
            (lambda x: True, 0),
        ])
        score_wr = self._score_piecewise(win_rate, [
            (lambda x: x >= 0.60, 10),
            (lambda x: x >= 0.50, 7),
            (lambda x: x >= 0.45, 4),
            (lambda x: True, 1),
        ])
        profitability = score_ar + score_tr + score_wr

        score_dd = self._score_piecewise(max_dd, [
            (lambda x: x <= 0.10, 15),
            (lambda x: x <= 0.15, 10),
            (lambda x: x <= 0.20, 5),
            (lambda x: True, 2),
        ])
        score_sharpe = self._score_piecewise(sharpe, [
            (lambda x: x >= 1.8, 10),
            (lambda x: x >= 1.2, 7),
            (lambda x: x >= 0.8, 4),
            (lambda x: True, 1),
        ])
        score_calmar = self._score_piecewise(calmar, [
            (lambda x: x >= 1.5, 10),
            (lambda x: x >= 1.0, 7),
            (lambda x: x >= 0.6, 4),
            (lambda x: True, 1),
        ])
        risk_control = score_dd + score_sharpe + score_calmar

        score_pl = self._score_piecewise(profit_ratio, [
            (lambda x: x >= 2.2, 10),
            (lambda x: x >= 1.8, 7),
            (lambda x: x >= 1.5, 4),
            (lambda x: True, 1),
        ])
        score_loss_streak = self._score_piecewise(max_loss_streak, [
            (lambda x: x <= 3, 5),
            (lambda x: x <= 5, 3),
            (lambda x: True, 1),
        ])
        score_monthly = self._score_piecewise(monthly_profit_ratio, [
            (lambda x: x >= 0.70, 5),
            (lambda x: x >= 0.55, 3),
            (lambda x: True, 1),
        ])
        pnl_quality = score_pl + score_loss_streak + score_monthly

        score_freq = self._score_piecewise(annual_trades, [
            (lambda x: 20 <= x <= 240, 5),
            (lambda x: 8 <= x < 20 or 240 < x <= 360, 3),
            (lambda x: True, 1),
        ])
        score_capacity = self._score_piecewise(avg_trade_amount_ratio, [
            (lambda x: x <= 0.08, 4),
            (lambda x: x <= 0.20, 3),
            (lambda x: x <= 0.35, 2),
            (lambda x: True, 1),
        ])
        score_stability = self._score_piecewise(monthly_stability, [
            (lambda x: x >= 0.65, 3),
            (lambda x: x >= 0.50, 2),
            (lambda x: True, 1),
        ])
        score_regime = self._score_piecewise(regime_consistency, [
            (lambda x: x >= 0.66, 3),
            (lambda x: x >= 0.33, 2),
            (lambda x: True, 1),
        ])
        practicality = score_freq + score_capacity + score_stability + score_regime

        total = float(profitability + risk_control + pnl_quality + practicality)
        grade, conclusion = self._grade_from_score(total)
        return {
            "total_score": round(total, 2),
            "grade": grade,
            "conclusion": conclusion,
            "dimensions": {
                "profitability": {"name": "收益能力", "score": round(profitability, 2), "max_score": 30},
                "risk_control": {"name": "风险控制", "score": round(risk_control, 2), "max_score": 35},
                "pnl_quality": {"name": "盈亏质量", "score": round(pnl_quality, 2), "max_score": 20},
                "practicality": {"name": "实战可行性", "score": round(practicality, 2), "max_score": 15},
            },
            "metrics": {
                "annualized_roi": annualized_roi,
                "total_return": total_return,
                "win_rate": win_rate,
                "max_drawdown": max_dd,
                "sharpe": sharpe,
                "calmar": calmar,
                "profit_loss_ratio": profit_ratio,
                "max_loss_streak": max_loss_streak,
                "monthly_profit_ratio": monthly_profit_ratio,
                "annualized_trades": annual_trades,
                "avg_trade_amount_ratio": avg_trade_amount_ratio,
                "monthly_stability_ratio": monthly_stability,
                "regime_consistency_ratio": regime_consistency
            }
        }

    def generate_report(self, strategy_id, hu_bu, xing_bu, initial_capital, start_date=None, end_date=None):
        transactions = [t for t in hu_bu.transactions if t["strategy_id"] == strategy_id]
        closed = self._closed_trades(transactions)
        total_trades = len(closed)
        wins = len([t for t in closed if self._safe_float(t.get("pnl", 0.0)) > 0])
        losses = len([t for t in closed if self._safe_float(t.get("pnl", 0.0)) <= 0])
        win_rate = wins / total_trades if total_trades > 0 else 0.0
        total_pnl = float(sum(self._safe_float(t.get("pnl", 0.0)) for t in closed))
        init_cap = self._safe_float(initial_capital, 1.0)
        roi = total_pnl / init_cap if init_cap else 0.0
        final_capital = init_cap + total_pnl

        equity = self._compute_equity_curve(closed, init_cap, start_date=start_date, end_date=end_date)
        max_value = equity.cummax()
        drawdown = (equity - max_value) / max_value.replace(0, np.nan)
        max_dd_pct = abs(self._safe_float(drawdown.min(), 0.0))

        avg_win = np.mean([self._safe_float(t.get("pnl", 0.0)) for t in closed if self._safe_float(t.get("pnl", 0.0)) > 0]) if wins > 0 else 0.0
        avg_loss_raw = np.mean([self._safe_float(t.get("pnl", 0.0)) for t in closed if self._safe_float(t.get("pnl", 0.0)) <= 0]) if losses > 0 else 0.0
        avg_loss = abs(avg_loss_raw)
        profit_factor = (abs(avg_win / avg_loss_raw) if avg_loss_raw != 0 else 0.0) if wins > 0 else 0.0

        rejections = xing_bu.get_rejection_count(strategy_id)
        violations = xing_bu.get_violation_count(strategy_id)
        circuit_breaks = xing_bu.get_circuit_break_count(strategy_id)

        if start_date is not None and end_date is not None:
            days = max((end_date - start_date).days, 1)
        elif closed:
            tx_start = min(t["dt"] for t in closed)
            tx_end = max(t["dt"] for t in closed)
            days = max((tx_end - tx_start).days, 1)
        else:
            days = 1
        if roi <= -1:
            annualized_roi = -1.0
        else:
            annualized_roi = (1 + roi) ** (252 / max(days, 1)) - 1
        if isinstance(annualized_roi, complex):
            annualized_roi = -1.0

        sharpe = self._compute_sharpe(equity, rf=0.02)
        calmar = (annualized_roi / max_dd_pct) if max_dd_pct > 0 else 0.0
        max_win_streak, max_loss_streak = self._compute_streaks(closed)
        monthly_profit_ratio = self._compute_monthly_profit_ratio(closed)
        annualized_trades = total_trades * 252 / max(days, 1)
        avg_trade_amount = np.mean([abs(self._safe_float(t.get("amount", 0.0))) for t in transactions if self._safe_float(t.get("amount", 0.0)) > 0]) if transactions else 0.0
        avg_trade_amount_ratio = (avg_trade_amount / init_cap) if init_cap > 0 else 0.0
        rets = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        positive_day_ratio = float((rets > 0).sum() / len(rets)) if len(rets) > 0 else 0.0
        regime_consistency_ratio = self._compute_regime_consistency_ratio(equity)
        monthly_equity = equity.resample("ME").last() if len(equity) > 0 else pd.Series(dtype=float)
        monthly_ret = monthly_equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        monthly_stability_ratio = float((monthly_ret > 0).sum() / len(monthly_ret)) if len(monthly_ret) > 0 else 0.0

        metrics = {
            "annualized_roi": annualized_roi,
            "roi": roi,
            "win_rate": win_rate,
            "max_dd": max_dd_pct,
            "sharpe": sharpe,
            "calmar": calmar,
            "profit_factor": profit_factor,
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
            "monthly_profit_ratio": monthly_profit_ratio,
            "annualized_trades": annualized_trades,
            "avg_trade_amount_ratio": avg_trade_amount_ratio,
            "monthly_stability_ratio": monthly_stability_ratio,
            "regime_consistency_ratio": regime_consistency_ratio
        }
        scorecard = self._build_scorecard(metrics)

        return {
            "strategy_id": strategy_id,
            "total_pnl": total_pnl,
            "roi": roi,
            "annualized_roi": annualized_roi,
            "max_dd": max_dd_pct,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "total_trades": total_trades,
            "rejections": rejections,
            "violations": violations,
            "circuit_breaks": circuit_breaks,
            "calmar": calmar,
            "sharpe": sharpe,
            "final_capital": final_capital,
            "avg_win": float(avg_win),
            "avg_loss": float(avg_loss),
            "max_win_streak": int(max_win_streak),
            "max_loss_streak": int(max_loss_streak),
            "monthly_profit_ratio": float(monthly_profit_ratio),
            "annualized_trades": float(annualized_trades),
            "avg_trade_amount_ratio": float(avg_trade_amount_ratio),
            "monthly_stability_ratio": float(monthly_stability_ratio),
            "regime_consistency_ratio": float(regime_consistency_ratio),
            "positive_day_ratio": float(positive_day_ratio),
            "scorecard": scorecard,
            "score_total": float(scorecard.get("total_score", 0.0)),
            "rating": str(scorecard.get("grade", "C"))
        }

    def generate_ranking(self, reports):
        df = pd.DataFrame(reports)
        if df.empty:
            return df
        if "score_total" not in df.columns:
            df["score_total"] = 0.0
        if "rating" not in df.columns:
            df["rating"] = "C"
        for col in ["calmar", "annualized_roi"]:
            if col not in df.columns:
                df[col] = 0.0
        df = df.sort_values(by=["score_total", "calmar", "annualized_roi"], ascending=[False, False, False]).reset_index(drop=True)
        df["rank"] = np.arange(1, len(df) + 1)
        return df

    def generate_backtest_report(self, strategy_id, transactions, initial_capital, start_date=None, end_date=None, summary_metrics=None):
        closed_trades = self._closed_trades(transactions)
        trade_count = len(closed_trades)
        win_trades = [t for t in closed_trades if t.get("pnl", 0) > 0]
        loss_trades = [t for t in closed_trades if t.get("pnl", 0) <= 0]

        win_num = len(win_trades)
        loss_num = len(loss_trades)
        win_rate = win_num / trade_count if trade_count > 0 else 0.0

        total_pnl = sum(t.get("pnl", 0.0) for t in closed_trades)
        init_cap = float(initial_capital)
        end_cap = init_cap + total_pnl
        total_return = (end_cap / init_cap - 1) if init_cap != 0 else 0.0

        if start_date is not None and end_date is not None:
            days = max((end_date - start_date).days, 1)
            start_txt = str(start_date)[:10]
            end_txt = str(end_date)[:10]
        elif closed_trades:
            trade_dates = [t["dt"] for t in closed_trades]
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
            equity.append(equity[-1] + t.get("pnl", 0.0))
        equity_series = pd.Series(equity)
        max_value = equity_series.cummax()
        drawdown = (equity_series - max_value) / max_value
        max_drawdown = drawdown.min() if not drawdown.empty else 0.0

        avg_win = float(np.mean([t.get("pnl", 0.0) for t in win_trades])) if win_trades else 0.0
        avg_loss = abs(float(np.mean([t.get("pnl", 0.0) for t in loss_trades]))) if loss_trades else 0.0
        profit_ratio = (avg_win / avg_loss) if avg_loss != 0 else 0.0
        max_win_streak, max_loss_streak = self._compute_streaks(closed_trades)
        monthly_profit_ratio = self._compute_monthly_profit_ratio(closed_trades)
        annualized_trades = trade_count * 252 / max(days, 1)
        avg_trade_amount = float(np.mean([abs(self._safe_float(t.get("amount", 0.0))) for t in transactions if self._safe_float(t.get("amount", 0.0)) > 0])) if transactions else 0.0
        avg_trade_amount_ratio = (avg_trade_amount / init_cap) if init_cap > 0 else 0.0
        equity_curve = self._compute_equity_curve(closed_trades, init_cap, start_date=start_date, end_date=end_date)
        sharpe = self._compute_sharpe(equity_curve, rf=0.02)
        dd_abs = abs(float(max_drawdown))
        calmar = (annual_return / dd_abs) if dd_abs > 0 else 0.0
        monthly_equity = equity_curve.resample("ME").last() if len(equity_curve) > 0 else pd.Series(dtype=float)
        monthly_ret = monthly_equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        monthly_stability_ratio = float((monthly_ret > 0).sum() / len(monthly_ret)) if len(monthly_ret) > 0 else 0.0
        regime_consistency_ratio = self._compute_regime_consistency_ratio(equity_curve)

        metrics = {
            "annualized_roi": self._safe_float(summary_metrics.get("annualized_roi")) if isinstance(summary_metrics, dict) else annual_return,
            "roi": self._safe_float(summary_metrics.get("roi")) if isinstance(summary_metrics, dict) else total_return,
            "win_rate": self._safe_float(summary_metrics.get("win_rate")) if isinstance(summary_metrics, dict) else win_rate,
            "max_dd": self._safe_float(summary_metrics.get("max_dd")) if isinstance(summary_metrics, dict) else dd_abs,
            "sharpe": self._safe_float(summary_metrics.get("sharpe")) if isinstance(summary_metrics, dict) else sharpe,
            "calmar": self._safe_float(summary_metrics.get("calmar")) if isinstance(summary_metrics, dict) else calmar,
            "profit_factor": self._safe_float(summary_metrics.get("profit_factor")) if isinstance(summary_metrics, dict) else profit_ratio,
            "max_win_streak": int(self._safe_float(summary_metrics.get("max_win_streak"), max_win_streak)) if isinstance(summary_metrics, dict) else max_win_streak,
            "max_loss_streak": int(self._safe_float(summary_metrics.get("max_loss_streak"), max_loss_streak)) if isinstance(summary_metrics, dict) else max_loss_streak,
            "monthly_profit_ratio": self._safe_float(summary_metrics.get("monthly_profit_ratio"), monthly_profit_ratio) if isinstance(summary_metrics, dict) else monthly_profit_ratio,
            "annualized_trades": self._safe_float(summary_metrics.get("annualized_trades"), annualized_trades) if isinstance(summary_metrics, dict) else annualized_trades,
            "avg_trade_amount_ratio": self._safe_float(summary_metrics.get("avg_trade_amount_ratio"), avg_trade_amount_ratio) if isinstance(summary_metrics, dict) else avg_trade_amount_ratio,
            "monthly_stability_ratio": self._safe_float(summary_metrics.get("monthly_stability_ratio"), monthly_stability_ratio) if isinstance(summary_metrics, dict) else monthly_stability_ratio,
            "regime_consistency_ratio": self._safe_float(summary_metrics.get("regime_consistency_ratio"), regime_consistency_ratio) if isinstance(summary_metrics, dict) else regime_consistency_ratio
        }
        scorecard = self._build_scorecard(metrics)

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
        print(f"评分卡总分：{scorecard['total_score']:.1f}/100 评级：{scorecard['grade']}（{scorecard['conclusion']}）")
        print("=" * 55 + "\n")
        trade_details = []
        force_close_count = 0
        for t in transactions:
            reason = str(t.get("reason", "") or "")
            if reason == "FORCE_CLOSE_END":
                force_close_count += 1
            trade_details.append({
                "dt": str(t.get("dt", "")),
                "direction": str(t.get("direction", "")),
                "price": float(t.get("price", 0.0) or 0.0),
                "quantity": int(t.get("quantity", 0) or 0),
                "amount": float(t.get("amount", 0.0) or 0.0),
                "cost": float(t.get("cost", 0.0) or 0.0),
                "pnl": float(t.get("pnl", 0.0) or 0.0),
                "reason": reason
            })

        report_notes = []
        if force_close_count > 0:
            report_notes.append(f"触发回测结束强制平仓 {force_close_count} 次（FORCE_CLOSE_END）")

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
            "max_win_streak": int(max_win_streak),
            "max_loss_streak": int(max_loss_streak),
            "monthly_profit_ratio": float(monthly_profit_ratio),
            "scorecard": scorecard,
            "trade_details": trade_details,
            "force_close_count": int(force_close_count),
            "report_notes": report_notes
        }
