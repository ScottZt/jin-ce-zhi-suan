from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any, Dict, List, Optional, Protocol

from src.evolution.core.event_bus import EventBus


class MemoryBackend(Protocol):
    """可替换存储后端协议（内存/数据库均可实现该接口）。"""

    def save_record(self, record: Dict[str, Any]) -> None:
        ...

    def list_records(self) -> List[Dict[str, Any]]:
        ...


@dataclass
class InMemoryBackend:
    """初期内存存储实现。"""

    records: List[Dict[str, Any]] = field(default_factory=list)

    def save_record(self, record: Dict[str, Any]) -> None:
        self.records.append(dict(record))

    def list_records(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self.records]


class StrategyMemory:
    def __init__(self, backend: Optional[MemoryBackend] = None):
        self.backend = backend or InMemoryBackend()

    def save(self, strategy_code: str, score: float, metrics: Dict[str, Any]) -> Dict[str, Any]:
        strategy_meta = self._extract_strategy_meta(strategy_code)
        record = {
            "strategy_code": str(strategy_code or ""),
            "strategy_id": strategy_meta.get("strategy_id", ""),
            "strategy_name": strategy_meta.get("strategy_name", ""),
            "class_name": strategy_meta.get("class_name", ""),
            "score": self._to_float(score),
            "metrics": self._normalize_metrics(metrics),
            "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        }
        self.backend.save_record(record)
        return dict(record)

    def get_top(self, k: int = 5) -> List[Dict[str, Any]]:
        limit = max(0, int(k))
        if limit == 0:
            return []
        records = self.backend.list_records()
        records.sort(key=lambda item: self._to_float(item.get("score", 0.0)), reverse=True)
        return records[:limit]

    def _normalize_metrics(self, metrics: Dict[str, Any]) -> Dict[str, float]:
        data = metrics if isinstance(metrics, dict) else {}
        normalized: Dict[str, float] = {}
        for key, value in data.items():
            normalized[str(key)] = self._to_float(value)
        return normalized

    def _to_float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    def _extract_strategy_meta(self, strategy_code: str) -> Dict[str, str]:
        code = str(strategy_code or "")
        out = {
            "strategy_id": "",
            "strategy_name": "",
            "class_name": "",
        }
        if not code.strip():
            return out
        class_match = re.search(r"class\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", code)
        if class_match:
            out["class_name"] = str(class_match.group(1) or "").strip()
        super_match = re.search(
            r"super\(\)\.__init__\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]",
            code,
        )
        if super_match:
            out["strategy_id"] = str(super_match.group(1) or "").strip()
            out["strategy_name"] = str(super_match.group(2) or "").strip()
        return out


class MemoryAgent:
    SCORE_WEIGHTS = {
        "sharpe": 0.4,
        "win_rate": 0.2,
        "profit_factor": 0.2,
        "drawdown": 0.2,
    }

    def __init__(self, bus: EventBus, memory: Optional[StrategyMemory] = None):
        self.bus = bus
        self.memory = memory or StrategyMemory()
        self.bus.subscribe("BacktestFinished", self._on_backtest_finished)
        self.bus.subscribe("StrategyRejected", self._on_strategy_rejected)

    def _on_backtest_finished(self, data: Dict[str, Any]) -> None:
        payload = data if isinstance(data, dict) else {}
        strategy_code = str(payload.get("strategy_code", "") or "")
        iteration = int(payload.get("iteration", 0) or 0)
        metrics = payload.get("metrics", {})
        normalized = self._normalize_metrics(metrics)
        score = self._score(normalized)
        self.memory.save(strategy_code=strategy_code, score=score, metrics=normalized)
        out = dict(payload)
        out.update(
            {
                "iteration": iteration,
                "status": "ok",
                "score": score,
                "strategy_code": strategy_code,
                "metrics": normalized,
                "best_timeframe": str(metrics.get("best_timeframe", "") or ""),
                "best_stock_code": str(metrics.get("best_stock_code", "") or ""),
            }
        )
        self.bus.publish(
            "StrategyScored",
            out,
        )

    def _on_strategy_rejected(self, data: Dict[str, Any]) -> None:
        payload = data if isinstance(data, dict) else {}
        out = dict(payload)
        out.update({"iteration": int(payload.get("iteration", 0) or 0), "status": "rejected", "score": None, "metrics": {}})
        self.bus.publish(
            "StrategyScored",
            out,
        )

    def _normalize_metrics(self, metrics: Any) -> Dict[str, float]:
        data = metrics if isinstance(metrics, dict) else {}
        return {
            "sharpe": self._to_float(data.get("sharpe", 0.0)),
            "win_rate": self._to_float(data.get("win_rate", 0.0)),
            "drawdown": self._to_float(data.get("drawdown", 0.0)),
            "total_return": self._to_float(data.get("total_return", 0.0)),
            "profit_factor": self._to_float(data.get("profit_factor", 0.0)),
        }

    def _score(self, metrics: Dict[str, float]) -> float:
        sharpe = metrics["sharpe"]
        win_rate = metrics["win_rate"]
        profit_factor = metrics["profit_factor"]
        drawdown = max(0.0, metrics["drawdown"])
        return (
            sharpe * self.SCORE_WEIGHTS["sharpe"]
            + win_rate * self.SCORE_WEIGHTS["win_rate"]
            + profit_factor * self.SCORE_WEIGHTS["profit_factor"]
            - drawdown * self.SCORE_WEIGHTS["drawdown"]
        )

    def _to_float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

