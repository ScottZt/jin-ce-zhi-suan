from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.evolution.adapters.strategy_library_adapter import StrategyLibraryAdapter
from src.evolution.core.event_bus import EventBus
from src.evolution.core.evolution_profile import EvolutionProfile


class StrategyLibraryCommitter:
    def __init__(self, bus: EventBus, adapter: Optional[StrategyLibraryAdapter] = None):
        self.bus = bus
        self.adapter = adapter or StrategyLibraryAdapter()
        self.bus.subscribe("StrategyScored", self._on_strategy_scored)

    def _on_strategy_scored(self, data: Dict[str, Any]) -> None:
        payload = data if isinstance(data, dict) else {}
        if str(payload.get("status", "")).strip().lower() != "ok":
            return
        profile_dict = payload.get("profile", {})
        profile = self._profile_from_dict(profile_dict)
        if not profile.persist_enabled:
            return
        score = self._to_float(payload.get("score", 0.0))
        if score < profile.persist_score_threshold:
            return
        strategy_code = str(payload.get("strategy_code", "") or "")
        if not strategy_code.strip():
            return
        parent_id = str(payload.get("parent_strategy_id", "") or "")
        parent_name = str(payload.get("parent_strategy_name", "") or "")
        metrics = payload.get("metrics", {})
        kline_type = str(payload.get("best_timeframe", "") or "").strip() or self._first_timeframe(profile.timeframes)
        stock_codes = self._to_str_list(payload.get("stock_codes", profile.target_stock_codes))
        committed = self.adapter.append_success_strategy(
            strategy_code=strategy_code,
            parent_strategy_id=parent_id,
            parent_strategy_name=parent_name,
            score=score,
            metrics=metrics if isinstance(metrics, dict) else {},
            kline_type=kline_type,
            stock_codes=stock_codes,
        )
        if not isinstance(committed, dict):
            return
        self.bus.publish(
            "StrategyCommitted",
            {
                "iteration": int(payload.get("iteration", 0) or 0),
                "status": "committed",
                "score": score,
                "strategy_id": committed.get("id"),
                "strategy_name": committed.get("name"),
                "parent_strategy_id": committed.get("parent_strategy_id"),
                "version": committed.get("version"),
            },
        )

    def _profile_from_dict(self, payload: Any) -> EvolutionProfile:
        data = payload if isinstance(payload, dict) else {}
        return EvolutionProfile(
            seed_strategy_id=str(data.get("seed_strategy_id", "") or ""),
            seed_strategy_ids=self._to_str_list(data.get("seed_strategy_ids", [])),
            seed_include_builtin=bool(data.get("seed_include_builtin", True)),
            seed_only_enabled=bool(data.get("seed_only_enabled", True)),
            target_stock_codes=self._to_str_list(data.get("target_stock_codes", [])),
            timeframes=self._to_str_list(data.get("timeframes", ["1min"])) or ["1min"],
            persist_enabled=bool(data.get("persist_enabled", True)),
            persist_score_threshold=self._to_float(data.get("persist_score_threshold", 0.2), default=0.2),
        )

    def _first_timeframe(self, values: List[str]) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return "1min"

    def _to_str_list(self, value: Any) -> List[str]:
        if isinstance(value, (list, tuple, set)):
            out = [str(x or "").strip() for x in value]
            return [x for x in out if x]
        text = str(value or "").strip()
        if not text:
            return []
        return [text]

    def _to_float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)
