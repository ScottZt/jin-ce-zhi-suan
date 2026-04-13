from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Protocol

from src.evolution.adapters.strategy_library_adapter import StrategyLibraryAdapter
from src.evolution.core.event_bus import EventBus
from src.evolution.core.evolution_profile import EvolutionProfile
from src.evolution.memory.strategy_memory import StrategyMemory


class StrategyLLM(Protocol):
    def generate(self, prompt: str, context: Dict[str, Any]) -> str:
        ...


class FallbackStrategyLLM:
    def __init__(self, primary: StrategyLLM, fallback: StrategyLLM):
        self.primary = primary
        self.fallback = fallback
        self.last_call_meta: Dict[str, Any] = {}

    def generate(self, prompt: str, context: Dict[str, Any]) -> str:
        try:
            code = self.primary.generate(prompt=prompt, context=context)
            self.last_call_meta = {
                "path": "primary",
                "provider": self.primary.__class__.__name__,
                "fallback_used": False,
            }
            return code
        except Exception as exc:
            code = self.fallback.generate(prompt=prompt, context=context)
            self.last_call_meta = {
                "path": "fallback",
                "provider": self.fallback.__class__.__name__,
                "fallback_used": True,
                "primary_provider": self.primary.__class__.__name__,
                "primary_error": str(exc),
            }
            return code


class MockStrategyLLM:
    def generate(self, prompt: str, context: Dict[str, Any]) -> str:
        _ = prompt
        header = self._build_header(context)
        body = self._build_on_bar_body(context)
        return f"""{header}

{body}
"""

    def _build_header(self, context: Dict[str, Any]) -> str:
        return f"""from src.strategies.implemented_strategies import BaseImplementedStrategy
import pandas as pd
from src.utils.indicators import Indicators

class {context["class_name"]}(BaseImplementedStrategy):
    def __init__(self):
        super().__init__("{context["strategy_id"]}", "{context["strategy_name"]}", trigger_timeframe="{context["trigger_tf"]}")
        self.history = {{}}
"""

    def _build_on_bar_body(self, context: Dict[str, Any]) -> str:
        return f"""    def on_bar(self, kline):
        code = kline["code"]
        if code not in self.history:
            self.history[code] = pd.DataFrame()
        self.history[code] = pd.concat([self.history[code], pd.DataFrame([kline])], ignore_index=True).tail(1500)
        df = self.history[code]
        if len(df) < {context["min_bars"]}:
            return None

        close = df["close"]
        ma_fast = Indicators.MA(close, {context["ma_fast"]})
        ma_slow = Indicators.MA(close, {context["ma_slow"]})
        rsi = Indicators.RSI(close, {context["rsi_period"]})
        if len(ma_fast) < 2 or len(ma_slow) < 2 or len(rsi) < 2:
            return None

        qty = int(self.positions.get(code, 0))
        curr_close = float(kline["close"])
        cross_up = float(ma_fast.iloc[-2]) <= float(ma_slow.iloc[-2]) and float(ma_fast.iloc[-1]) > float(ma_slow.iloc[-1])
        cross_down = float(ma_fast.iloc[-2]) >= float(ma_slow.iloc[-2]) and float(ma_fast.iloc[-1]) < float(ma_slow.iloc[-1])
        rsi_now = float(rsi.iloc[-1])

        if qty <= 0 and cross_up and rsi_now >= {context["rsi_buy"]}:
            buy_qty = int(self._qty())
            if buy_qty <= 0:
                return None
            return {{
                "strategy_id": self.id,
                "code": code,
                "dt": kline["dt"],
                "direction": "BUY",
                "price": curr_close,
                "qty": buy_qty,
                "stop_loss": curr_close * (1 - {context["stop_loss_pct"]}),
                "take_profit": curr_close * (1 + {context["take_profit_pct"]})
            }}

        if qty > 0 and (cross_down or rsi_now <= {context["rsi_sell"]}):
            return self.create_exit_signal(kline, qty, "Evolution Exit")
        return None"""


class Researcher:
    def __init__(
        self,
        bus: EventBus,
        memory: StrategyMemory,
        llm_client: Optional[StrategyLLM] = None,
        strategy_library: Optional[StrategyLibraryAdapter] = None,
    ):
        self.bus = bus
        self.memory = memory
        self.llm_client = llm_client or MockStrategyLLM()
        self.strategy_library = strategy_library or StrategyLibraryAdapter()
        self._generated_fp = set()
        self.bus.subscribe("Start", self._on_start)

    def _on_start(self, data: Dict[str, Any]) -> None:
        payload = data if isinstance(data, dict) else {}
        iteration = int(payload.get("iteration", 0) or 0)
        profile = self._profile_from_payload(payload.get("profile"))
        try:
            candidate = self._generate_from_library(iteration=iteration, profile=profile)
            self.bus.publish(
                "StrategyGenerated",
                {
                    "iteration": iteration,
                    "strategy_code": candidate["strategy_code"],
                    "parent_strategy_id": candidate["parent_strategy_id"],
                    "parent_strategy_name": candidate["parent_strategy_name"],
                    "profile": profile.to_dict(),
                },
            )
        except Exception as exc:
            self.bus.publish("StrategyRejected", {"iteration": iteration, "reason": f"researcher_error:{exc}"})

    def _generate_from_library(self, iteration: int, profile: EvolutionProfile) -> Dict[str, str]:
        seed_meta = self.strategy_library.pick_seed(iteration=iteration, profile=profile)
        seed_code = str((seed_meta or {}).get("code", "") or "").strip()
        parent_id = str((seed_meta or {}).get("id", "") or "").strip()
        parent_name = str((seed_meta or {}).get("name", "") or "").strip()
        if not seed_code:
            seed_code = self._bootstrap_seed_code()
            parent_id = "BOOT"
            parent_name = "Bootstrap"
        seed_fp = self._fingerprint(seed_code)
        context = self._build_context(
            seed_code=seed_code,
            iteration=iteration,
            parent_strategy_id=parent_id,
            parent_strategy_name=parent_name,
            profile=profile,
        )
        prompt = f"seed_fp={seed_fp}, iteration={iteration}, parent={parent_id}, keep trend+risk shape."
        self.bus.publish(
            "LLMExecution",
            {
                "iteration": iteration,
                "stage": "start",
                "provider": self.llm_client.__class__.__name__,
                "prompt_chars": len(prompt),
                "seed_strategy_id": parent_id,
            },
        )
        candidate = ""
        call_meta: Dict[str, Any] = {}
        try:
            candidate = self.llm_client.generate(prompt=prompt, context=context)
            call_meta = self._extract_llm_meta()
        except Exception as exc:
            self.bus.publish(
                "LLMExecution",
                {
                    "iteration": iteration,
                    "stage": "error",
                    "provider": self.llm_client.__class__.__name__,
                    "error": str(exc),
                    "seed_strategy_id": parent_id,
                },
            )
            raise
        self.bus.publish(
            "LLMExecution",
            {
                "iteration": iteration,
                "stage": "done",
                "provider": str(call_meta.get("provider") or self.llm_client.__class__.__name__),
                "fallback_used": bool(call_meta.get("fallback_used", False)),
                "path": str(call_meta.get("path", "") or ""),
                "primary_provider": str(call_meta.get("primary_provider", "") or ""),
                "primary_error": str(call_meta.get("primary_error", "") or ""),
                "code_chars": len(str(candidate or "")),
                "seed_strategy_id": parent_id,
            },
        )
        validated = self._validate_candidate(candidate)
        strategy_code = self._ensure_not_duplicate(validated, {seed_fp})
        return {
            "strategy_code": strategy_code,
            "parent_strategy_id": parent_id,
            "parent_strategy_name": parent_name or parent_id,
        }

    def _extract_llm_meta(self) -> Dict[str, Any]:
        meta = getattr(self.llm_client, "last_call_meta", None)
        if isinstance(meta, dict):
            return dict(meta)
        return {
            "provider": self.llm_client.__class__.__name__,
            "fallback_used": False,
            "path": "direct",
        }

    def _build_context(
        self,
        seed_code: str,
        iteration: int,
        parent_strategy_id: str,
        parent_strategy_name: str,
        profile: EvolutionProfile,
    ) -> Dict[str, Any]:
        has_macd = "MACD" in seed_code.upper()
        has_rsi = "RSI" in seed_code.upper()
        variant = (max(1, int(iteration)) % 9) + 1
        timeframes = [str(x or "").strip() for x in profile.timeframes if str(x or "").strip()]
        trigger_tf = timeframes[(variant - 1) % len(timeframes)] if timeframes else ("30min" if has_macd else ("15min" if has_rsi else "1min"))
        return {
            "seed_code": seed_code,
            "class_name": f"EvolutionStrategyI{variant}_{self._sanitize_token(parent_strategy_id)}",
            "strategy_id": f"EVOL_{variant}",
            "strategy_name": f"Evolution Strategy {variant} from {parent_strategy_name or parent_strategy_id}",
            "trigger_tf": trigger_tf,
            "ma_fast": 8 + variant,
            "ma_slow": 20 + variant * 2,
            "rsi_period": 12 + (variant % 4),
            "rsi_buy": 53.0 + (variant % 4),
            "rsi_sell": 47.0 - (variant % 3),
            "stop_loss_pct": 0.02 + variant * 0.002,
            "take_profit_pct": 0.06 + variant * 0.003,
            "min_bars": 60 + variant * 3,
        }

    def _validate_candidate(self, strategy_code: str) -> str:
        code = str(strategy_code or "").strip()
        if not code:
            raise ValueError("空策略")
        if "BaseImplementedStrategy" not in code:
            raise ValueError("不兼容基类")
        if "def on_bar" not in code:
            raise ValueError("缺少 on_bar")
        return code

    def _ensure_not_duplicate(self, code: str, seed_fp: set[str]) -> str:
        fp = self._fingerprint(code)
        if fp in seed_fp or fp in self._generated_fp:
            bumped = re.sub(r"EvolutionStrategyI(\d+)", "EvolutionStrategyI999", code)
            fp2 = self._fingerprint(bumped)
            if fp2 in seed_fp or fp2 in self._generated_fp:
                raise ValueError("重复策略")
            self._generated_fp.add(fp2)
            return bumped
        self._generated_fp.add(fp)
        return code

    def _bootstrap_seed_code(self) -> str:
        return "from src.strategies.implemented_strategies import BaseImplementedStrategy\nclass Bootstrap(BaseImplementedStrategy):\n    def __init__(self):\n        super().__init__('BOOT', 'BOOT', trigger_timeframe='1min')\n    def on_bar(self, kline):\n        return None\n"

    def _fingerprint(self, strategy_code: str) -> str:
        normalized = re.sub(r"\s+", "", str(strategy_code or ""))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _profile_from_payload(self, payload: Any) -> EvolutionProfile:
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

    def _sanitize_token(self, text: str) -> str:
        value = re.sub(r"[^0-9a-zA-Z_]+", "", str(text or ""))
        return value or "seed"

