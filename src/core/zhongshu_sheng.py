# src/core/zhongshu_sheng.py

from src.utils.runtime_params import get_value

class ZhongshuSheng:
    """
    中书省 (Secretariat): 为每套策略独立生成买卖信号
    """
    def __init__(self, strategies):
        self.strategies = strategies # List of strategy instances

    def generate_signals(self, kline, runnable_strategy_ids=None, strategy_context=None):
        """
        Generate signals for all strategies for the current bar.
        """
        signals = []
        runnable = set(runnable_strategy_ids) if runnable_strategy_ids else None
        ctx = strategy_context if isinstance(strategy_context, dict) else {}
        for strategy in self.strategies:
            if runnable is not None and strategy.id not in runnable:
                continue
            if ctx and hasattr(strategy, "set_backtest_context"):
                strategy.set_backtest_context(**ctx)
            signal = strategy.on_bar(kline)
            if signal:
                if "qty" not in signal or signal.get("qty") is None:
                    signal["qty"] = self._resolve_fallback_qty(strategy)
                qty = int(float(signal.get("qty", 0)))
                if qty <= 0:
                    continue
                signal["qty"] = qty
                signals.append(signal)
        return signals

    def _resolve_fallback_qty(self, strategy):
        if hasattr(strategy, "_qty"):
            try:
                return int(float(strategy._qty()))
            except Exception:
                pass
        mode = str(get_value("strategy_params.common.order_qty_mode", "fixed")).strip().lower()
        if mode == "cash_pct":
            cash = float(getattr(strategy, "current_cash", 0.0) or 0.0)
            price = float(getattr(strategy, "last_price", 0.0) or 0.0)
            pct = float(get_value("strategy_params.common.order_cash_pct", 0.1))
            if pct > 1:
                pct = pct / 100.0
            pct = max(0.0, min(1.0, pct))
            if cash <= 0 or price <= 0 or pct <= 0:
                return 0
            raw_qty = int((cash * pct) // price)
            return int((raw_qty // 100) * 100)
        return int(float(get_value("strategy_params.common.order_qty", 1000)))

    def update_strategy_state(self, strategy_id, code, position_qty):
        """
        Update strategy state after execution.
        """
        for strategy in self.strategies:
            if strategy.id == strategy_id:
                strategy.update_position(code, position_qty)
                break
