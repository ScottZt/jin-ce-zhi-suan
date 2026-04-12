from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from src.utils.config_loader import ConfigLoader


@dataclass
class EvolutionProfile:
    seed_strategy_id: str = ""
    seed_strategy_ids: List[str] = field(default_factory=list)
    seed_include_builtin: bool = True
    seed_only_enabled: bool = True
    target_stock_codes: List[str] = field(default_factory=list)
    timeframes: List[str] = field(default_factory=lambda: ["1min"])
    persist_enabled: bool = True
    persist_score_threshold: float = 0.2

    @classmethod
    def load(cls) -> "EvolutionProfile":
        cfg = ConfigLoader.reload()
        seed_id = str(cfg.get("evolution.seed.strategy_id", "") or "").strip()
        seed_ids = cls._to_str_list(cfg.get("evolution.seed.strategy_ids", []))
        include_builtin = bool(cfg.get("evolution.seed.include_builtin", True))
        only_enabled = bool(cfg.get("evolution.seed.only_enabled", True))
        targets = cls._to_str_list(cfg.get("evolution.evaluation.stock_codes", []))
        if not targets:
            targets = cls._to_str_list(cfg.get("targets", []))
        timeframes = cls._to_str_list(cfg.get("evolution.evaluation.timeframes", ["1min"]))
        if not timeframes:
            timeframes = ["1min"]
        persist_enabled = bool(cfg.get("evolution.persist.enabled", True))
        score_threshold = cls._to_float(cfg.get("evolution.persist.score_threshold", 0.2), default=0.2)
        return cls(
            seed_strategy_id=seed_id,
            seed_strategy_ids=seed_ids,
            seed_include_builtin=include_builtin,
            seed_only_enabled=only_enabled,
            target_stock_codes=targets,
            timeframes=timeframes,
            persist_enabled=persist_enabled,
            persist_score_threshold=score_threshold,
        )

    @classmethod
    def from_dict(cls, payload: Any) -> "EvolutionProfile":
        data = payload if isinstance(payload, dict) else {}
        return cls(
            seed_strategy_id=str(data.get("seed_strategy_id", "") or "").strip(),
            seed_strategy_ids=cls._to_str_list(data.get("seed_strategy_ids", [])),
            seed_include_builtin=bool(data.get("seed_include_builtin", True)),
            seed_only_enabled=bool(data.get("seed_only_enabled", True)),
            target_stock_codes=cls._to_str_list(data.get("target_stock_codes", [])),
            timeframes=cls._to_str_list(data.get("timeframes", ["1min"])) or ["1min"],
            persist_enabled=bool(data.get("persist_enabled", True)),
            persist_score_threshold=cls._to_float(data.get("persist_score_threshold", 0.2), default=0.2),
        )

    def merged(self, override_payload: Any) -> "EvolutionProfile":
        override_data = override_payload if isinstance(override_payload, dict) else {}
        base = self.to_dict()
        out = dict(base)
        if "seed_strategy_id" in override_data:
            out["seed_strategy_id"] = str(override_data.get("seed_strategy_id", "") or "").strip()
        if "seed_strategy_ids" in override_data:
            out["seed_strategy_ids"] = self._to_str_list(override_data.get("seed_strategy_ids", []))
        if "seed_include_builtin" in override_data:
            out["seed_include_builtin"] = bool(override_data.get("seed_include_builtin", True))
        if "seed_only_enabled" in override_data:
            out["seed_only_enabled"] = bool(override_data.get("seed_only_enabled", True))
        if "target_stock_codes" in override_data:
            out["target_stock_codes"] = self._to_str_list(override_data.get("target_stock_codes", []))
        if "timeframes" in override_data:
            out["timeframes"] = self._to_str_list(override_data.get("timeframes", [])) or ["1min"]
        if "persist_enabled" in override_data:
            out["persist_enabled"] = bool(override_data.get("persist_enabled", True))
        if "persist_score_threshold" in override_data:
            out["persist_score_threshold"] = self._to_float(
                override_data.get("persist_score_threshold", out.get("persist_score_threshold", 0.2)),
                default=self._to_float(out.get("persist_score_threshold", 0.2), default=0.2),
            )
        return self.from_dict(out)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seed_strategy_id": self.seed_strategy_id,
            "seed_strategy_ids": list(self.seed_strategy_ids),
            "seed_include_builtin": bool(self.seed_include_builtin),
            "seed_only_enabled": bool(self.seed_only_enabled),
            "target_stock_codes": list(self.target_stock_codes),
            "timeframes": list(self.timeframes),
            "persist_enabled": bool(self.persist_enabled),
            "persist_score_threshold": float(self.persist_score_threshold),
        }

    @staticmethod
    def _to_str_list(value: Any) -> List[str]:
        if isinstance(value, (list, tuple, set)):
            out = [str(x or "").strip() for x in value]
            return [x for x in out if x]
        text = str(value or "").strip()
        if not text:
            return []
        if "," in text:
            parts = [str(x).strip() for x in text.split(",")]
            return [x for x in parts if x]
        return [text]

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)
