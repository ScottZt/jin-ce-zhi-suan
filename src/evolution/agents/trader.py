from __future__ import annotations

from typing import Any, Dict, Optional

from src.evolution.adapters.backtest_adapter import BacktestAdapter
from src.evolution.core.event_bus import EventBus


class Trader:
    def __init__(self, bus: EventBus, backtest_adapter: Optional[BacktestAdapter] = None):
        self.bus = bus
        self.backtest_adapter = backtest_adapter or BacktestAdapter()
        self.bus.subscribe("StrategyApproved", self._on_strategy_approved)

    def _on_strategy_approved(self, data: Dict[str, Any]) -> None:
        payload = data if isinstance(data, dict) else {}
        strategy_code = str(payload.get("strategy_code", "") or "")
        iteration = int(payload.get("iteration", 0) or 0)
        profile = payload.get("profile", {}) if isinstance(payload.get("profile", {}), dict) else {}
        stock_codes = self._to_str_list(profile.get("target_stock_codes", []))
        timeframes = self._to_str_list(profile.get("timeframes", []))
        if not strategy_code.strip():
            self._publish_failed(iteration, strategy_code, payload)
            return
        try:
            metrics = self.backtest_adapter.run_backtest(
                strategy_code=strategy_code,
                stock_codes=stock_codes or None,
                timeframes=timeframes or None,
                progress_callback=lambda event: self._publish_progress(iteration, payload, event),
            )
            out = dict(payload)
            out.update({"iteration": iteration, "strategy_code": strategy_code, "metrics": metrics})
            self.bus.publish("BacktestFinished", out)
        except Exception:
            self._publish_failed(iteration, strategy_code, payload)

    def _publish_progress(self, iteration: int, payload: Dict[str, Any], event: Dict[str, Any]) -> None:
        event_payload = event if isinstance(event, dict) else {}
        out = dict(payload)
        out.update({
            "iteration": int(iteration or 0),
            "event": dict(event_payload),
        })
        self.bus.publish("BacktestProgress", out)

    def _publish_failed(self, iteration: int, strategy_code: str, payload: Dict[str, Any]) -> None:
        out = dict(payload)
        out.update({"iteration": iteration, "strategy_code": strategy_code, "metrics": self._empty_metrics()})
        self.bus.publish("BacktestFinished", out)

    def _empty_metrics(self) -> Dict[str, float]:
        return {
            "sharpe": 0.0,
            "drawdown": 0.0,
            "win_rate": 0.0,
            "total_return": 0.0,
            "profit_factor": 0.0,
        }

    def _to_str_list(self, value: Any) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            out = [str(x or "").strip() for x in value]
            return [x for x in out if x]
        text = str(value or "").strip()
        if not text:
            return []
        return [text]

