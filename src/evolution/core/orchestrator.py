from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from src.evolution.agents.critic import Critic
from src.evolution.agents.library_committer import StrategyLibraryCommitter
from src.evolution.agents.researcher import FallbackStrategyLLM
from src.evolution.agents.researcher import MockStrategyLLM
from src.evolution.agents.researcher import Researcher
from src.evolution.agents.trader import Trader
from src.evolution.adapters.backtest_adapter import BacktestAdapter
from src.evolution.adapters.strategy_library_adapter import StrategyLibraryAdapter
from src.evolution.core.event_bus import EventBus
from src.evolution.core.evolution_profile import EvolutionProfile
from src.evolution.core.strategy_loader import StrategyLoader
from src.evolution.llm.client_factory import OpenAICompatibleStrategyLLM
from src.evolution.llm.client_factory import load_evolution_llm_config
from src.evolution.memory.strategy_memory import MemoryAgent
from src.evolution.memory.strategy_memory import StrategyMemory


class EvolutionOrchestrator:
    def __init__(self, memory: Optional[StrategyMemory] = None):
        self.bus = EventBus()
        self.memory = memory or StrategyMemory()
        loader = StrategyLoader()
        adapter = BacktestAdapter(strategy_loader=loader)
        strategy_library = StrategyLibraryAdapter()
        llm_client = self._build_researcher_llm()
        self.researcher = Researcher(
            bus=self.bus,
            memory=self.memory,
            llm_client=llm_client,
            strategy_library=strategy_library,
        )
        self.critic = Critic(bus=self.bus)
        self.trader = Trader(bus=self.bus, backtest_adapter=adapter)
        self.memory_agent = MemoryAgent(bus=self.bus, memory=self.memory)
        self.library_committer = StrategyLibraryCommitter(bus=self.bus, adapter=strategy_library)
        self._last_result: Dict[str, Any] = {"status": "rejected", "score": None, "reason": ""}
        self._last_generated: Dict[str, Any] = {}
        self._last_rejected: Dict[str, Any] = {}
        self._last_backtest: Dict[str, Any] = {}
        self._last_committed: Dict[str, Any] = {}
        self._last_llm: Dict[str, Any] = {}
        self._runtime_event_sink: Optional[Callable[[Dict[str, Any]], None]] = None
        self.bus.subscribe("StrategyGenerated", self._on_strategy_generated)
        self.bus.subscribe("StrategyRejected", self._on_strategy_rejected)
        self.bus.subscribe("BacktestFinished", self._on_backtest_finished)
        self.bus.subscribe("StrategyCommitted", self._on_strategy_committed)
        self.bus.subscribe("StrategyScored", self._on_strategy_scored)
        self.bus.subscribe("LLMExecution", self._on_llm_execution)
        self._subscribe_runtime_event("Start")
        self._subscribe_runtime_event("StrategyGenerated")
        self._subscribe_runtime_event("StrategyApproved")
        self._subscribe_runtime_event("StrategyRejected")
        self._subscribe_runtime_event("BacktestProgress")
        self._subscribe_runtime_event("BacktestFinished")
        self._subscribe_runtime_event("StrategyScored")
        self._subscribe_runtime_event("StrategyCommitted")
        self._subscribe_runtime_event("LLMExecution")

    def run_once(self, iteration: int, profile_override: Optional[Dict[str, Any]] = None) -> Any:
        profile = self._resolve_profile(profile_override)
        self._last_result = {"status": "rejected", "score": None, "reason": ""}
        self._last_generated = {}
        self._last_rejected = {}
        self._last_backtest = {}
        self._last_committed = {}
        self._last_llm = {}
        self.bus.publish("Start", {"iteration": int(iteration), "profile": profile.to_dict()})
        status = str(self._last_result.get("status", "rejected"))
        if status == "ok":
            return self._to_float(self._last_result.get("score", 0.0))
        return "rejected"

    def get_last_result(self) -> Dict[str, Any]:
        return dict(self._last_result)

    def set_runtime_event_sink(self, sink: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        self._runtime_event_sink = sink

    def _subscribe_runtime_event(self, event_type: str) -> None:
        name = str(event_type or "").strip()
        if not name:
            return

        def _handler(data: Dict[str, Any]) -> None:
            self._forward_runtime_event(name, data)

        self.bus.subscribe(name, _handler)

    def _forward_runtime_event(self, event_type: str, data: Dict[str, Any]) -> None:
        sink = self._runtime_event_sink
        if sink is None:
            return
        try:
            sink({
                "event_type": str(event_type or ""),
                "payload": data if isinstance(data, dict) else {},
            })
        except Exception:
            pass

    def _on_strategy_scored(self, data: Dict[str, Any]) -> None:
        payload = data if isinstance(data, dict) else {}
        metrics = payload.get("metrics", {}) if isinstance(payload.get("metrics"), dict) else {}
        committed = dict(self._last_committed) if isinstance(self._last_committed, dict) else {}
        self._last_result = {
            "status": str(payload.get("status", "rejected")),
            "score": payload.get("score"),
            "reason": str(payload.get("reason", "") or self._last_rejected.get("reason", "")),
            "iteration": int(payload.get("iteration", 0) or 0),
            "parent_strategy_id": str(payload.get("parent_strategy_id", "") or self._last_generated.get("parent_strategy_id", "")),
            "parent_strategy_name": str(payload.get("parent_strategy_name", "") or self._last_generated.get("parent_strategy_name", "")),
            "best_timeframe": str(payload.get("best_timeframe", "") or self._last_backtest.get("best_timeframe", "")),
            "best_stock_code": str(payload.get("best_stock_code", "") or self._last_backtest.get("best_stock_code", "")),
            "metrics": dict(metrics),
            "committed": bool(committed),
            "committed_strategy_id": str(committed.get("strategy_id", "") or ""),
            "committed_strategy_name": str(committed.get("strategy_name", "") or ""),
            "committed_version": committed.get("version"),
            "llm_provider": str(self._last_llm.get("provider", "") or ""),
            "llm_path": str(self._last_llm.get("path", "") or ""),
            "llm_stage": str(self._last_llm.get("stage", "") or ""),
            "llm_fallback_used": bool(self._last_llm.get("fallback_used", False)),
            "llm_primary_provider": str(self._last_llm.get("primary_provider", "") or ""),
            "llm_primary_error": str(self._last_llm.get("primary_error", "") or ""),
        }

    def _on_strategy_generated(self, data: Dict[str, Any]) -> None:
        payload = data if isinstance(data, dict) else {}
        self._last_generated = {
            "iteration": int(payload.get("iteration", 0) or 0),
            "parent_strategy_id": str(payload.get("parent_strategy_id", "") or ""),
            "parent_strategy_name": str(payload.get("parent_strategy_name", "") or ""),
        }

    def _on_strategy_rejected(self, data: Dict[str, Any]) -> None:
        payload = data if isinstance(data, dict) else {}
        self._last_rejected = {
            "iteration": int(payload.get("iteration", 0) or 0),
            "reason": str(payload.get("reason", "") or ""),
        }

    def _on_backtest_finished(self, data: Dict[str, Any]) -> None:
        payload = data if isinstance(data, dict) else {}
        self._last_backtest = {
            "iteration": int(payload.get("iteration", 0) or 0),
            "best_timeframe": str(payload.get("metrics", {}).get("best_timeframe", "") if isinstance(payload.get("metrics"), dict) else ""),
            "best_stock_code": str(payload.get("metrics", {}).get("best_stock_code", "") if isinstance(payload.get("metrics"), dict) else ""),
        }

    def _on_strategy_committed(self, data: Dict[str, Any]) -> None:
        payload = data if isinstance(data, dict) else {}
        self._last_committed = {
            "iteration": int(payload.get("iteration", 0) or 0),
            "strategy_id": str(payload.get("strategy_id", "") or ""),
            "strategy_name": str(payload.get("strategy_name", "") or ""),
            "version": payload.get("version"),
        }

    def _on_llm_execution(self, data: Dict[str, Any]) -> None:
        payload = data if isinstance(data, dict) else {}
        self._last_llm = {
            "iteration": int(payload.get("iteration", 0) or 0),
            "stage": str(payload.get("stage", "") or ""),
            "provider": str(payload.get("provider", "") or ""),
            "path": str(payload.get("path", "") or ""),
            "fallback_used": bool(payload.get("fallback_used", False)),
            "primary_provider": str(payload.get("primary_provider", "") or ""),
            "primary_error": str(payload.get("primary_error", "") or ""),
        }

    def _to_float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    def _build_researcher_llm(self):
        cfg = load_evolution_llm_config()
        mock = MockStrategyLLM()
        if not cfg.is_ready():
            return mock
        primary = OpenAICompatibleStrategyLLM(cfg)
        if cfg.fallback_to_mock:
            return FallbackStrategyLLM(primary=primary, fallback=mock)
        return primary

    def _resolve_profile(self, profile_override: Optional[Dict[str, Any]]) -> EvolutionProfile:
        base = EvolutionProfile.load()
        if not isinstance(profile_override, dict) or not profile_override:
            return base
        return base.merged(profile_override)

