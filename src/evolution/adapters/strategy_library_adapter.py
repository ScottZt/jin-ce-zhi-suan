from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.evolution.core.evolution_profile import EvolutionProfile
from src.strategies import strategy_manager_repo as strategy_repo


class StrategyLibraryAdapter:
    def __init__(self, version_file: str = "data/evolution_strategy_versions.json"):
        self.version_file = Path(version_file)

    def list_seed_candidates(self, profile: EvolutionProfile) -> List[Dict[str, Any]]:
        rows = strategy_repo.list_all_strategy_meta()
        selected_ids = set(profile.seed_strategy_ids)
        if profile.seed_strategy_id:
            selected_ids.add(profile.seed_strategy_id)
        out: List[Dict[str, Any]] = []
        for row in rows:
            sid = str(row.get("id", "")).strip()
            code = str(row.get("code", "")).strip()
            if not sid or not code:
                continue
            if profile.seed_only_enabled and not bool(row.get("enabled", True)):
                continue
            if not profile.seed_include_builtin and bool(row.get("builtin", False)):
                continue
            if selected_ids and sid not in selected_ids:
                continue
            out.append(dict(row))
        return out

    def pick_seed(self, iteration: int, profile: EvolutionProfile) -> Optional[Dict[str, Any]]:
        candidates = self.list_seed_candidates(profile)
        if not candidates:
            return None
        if profile.seed_strategy_id:
            for row in candidates:
                if str(row.get("id", "")).strip() == profile.seed_strategy_id:
                    return row
        index = max(0, int(iteration) - 1) % len(candidates)
        return candidates[index]

    def append_success_strategy(
        self,
        strategy_code: str,
        parent_strategy_id: str,
        parent_strategy_name: str,
        score: float,
        metrics: Dict[str, Any],
        kline_type: str,
        stock_codes: List[str],
    ) -> Optional[Dict[str, Any]]:
        sid = strategy_repo.next_custom_strategy_id()
        parent_id = str(parent_strategy_id or "").strip() or "unknown"
        parent_name = str(parent_strategy_name or parent_id).strip() or parent_id
        version = self._next_version(parent_id)
        strategy_name = f"{parent_name}-EVOL-v{version}"
        rewritten = self._rewrite_strategy_identity(strategy_code, sid, strategy_name, kline_type)
        now = datetime.now().isoformat(timespec="seconds")
        metric_text = json.dumps(metrics if isinstance(metrics, dict) else {}, ensure_ascii=False)
        stock_text = ",".join([str(x).strip() for x in stock_codes if str(x).strip()])
        intent = {
            "source": "market",
            "strategy_type": "trend_following",
            "logic": f"由父策略{parent_id}进化生成，版本v{version}",
            "indicators": ["MA", "RSI"],
            "entry": "趋势确认后入场",
            "exit": "反向信号或风控触发后退出",
            "risk_profile": "balanced",
            "confidence": 0.66,
        }
        payload = {
            "id": sid,
            "name": strategy_name,
            "class_name": self._extract_class_name(rewritten),
            "code": rewritten,
            "kline_type": str(kline_type or "1min"),
            "template_text": f"Evolution parent={parent_id} version=v{version}",
            "analysis_text": f"Evolution成功入库；parent={parent_id}; version=v{version}; score={float(score):.6f}",
            "source": "market",
            "protect_level": "custom",
            "immutable": False,
            "depends_on": [parent_id],
            "raw_requirement_title": "策略进化新增版本",
            "raw_requirement": f"parent={parent_id}; parent_name={parent_name}; version=v{version}; stocks={stock_text}; metrics={metric_text}; created_at={now}",
            "strategy_intent": intent,
        }
        try:
            strategy_repo.add_custom_strategy(payload)
        except Exception:
            return None
        return {
            "id": sid,
            "name": strategy_name,
            "parent_strategy_id": parent_id,
            "version": version,
            "kline_type": str(kline_type or "1min"),
        }

    def _rewrite_strategy_identity(self, code: str, strategy_id: str, strategy_name: str, kline_type: str) -> str:
        source = str(code or "")
        pattern = r"(super\(\)\.__init__\(\s*[\"'])(.*?)([\"']\s*,\s*[\"'])(.*?)([\"']\s*,\s*trigger_timeframe\s*=\s*[\"'])(.*?)([\"'])"
        if re.search(pattern, source):
            return re.sub(
                pattern,
                rf"\g<1>{strategy_id}\g<3>{strategy_name}\g<5>{kline_type}\g<7>",
                source,
                count=1,
            )
        return source

    def _extract_class_name(self, code: str) -> str:
        m = re.search(r"class\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", str(code or ""))
        if m:
            return str(m.group(1))
        return "EvolutionGeneratedStrategy"

    def _next_version(self, parent_strategy_id: str) -> int:
        key = str(parent_strategy_id or "").strip() or "unknown"
        data = self._load_versions()
        current = int(data.get(key, 0) or 0) + 1
        data[key] = current
        self._save_versions(data)
        return current

    def _load_versions(self) -> Dict[str, int]:
        try:
            if not self.version_file.exists():
                return {}
            payload = json.loads(self.version_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {}
            out: Dict[str, int] = {}
            for key, value in payload.items():
                k = str(key or "").strip()
                if not k:
                    continue
                try:
                    out[k] = max(0, int(value))
                except Exception:
                    out[k] = 0
            return out
        except Exception:
            return {}

    def _save_versions(self, payload: Dict[str, int]) -> None:
        self.version_file.parent.mkdir(parents=True, exist_ok=True)
        self.version_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
