from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable, Deque, Dict, List, Optional

from src.evolution.core.orchestrator import EvolutionOrchestrator


class EvolutionRuntimeManager:
    def __init__(self, max_history: int = 500):
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._orchestrator = EvolutionOrchestrator()
        self._history: Deque[Dict[str, Any]] = deque(maxlen=max(50, int(max_history)))
        self._state: Dict[str, Any] = {
            "running": False,
            "iteration": 0,
            "last_status": "idle",
            "last_score": None,
            "last_error": "",
            "interval_seconds": 1.0,
            "max_iterations": None,
            "started_at": None,
            "finished_at": None,
            "profile": {},
            "active_event_type": "",
            "active_phase": "",
            "active_phase_label": "",
            "active_message": "",
            "active_progress_pct": 0,
            "active_data_status": "idle",
            "active_stock_code": "",
            "active_timeframe": "",
            "active_scenario_index": 0,
            "active_scenario_total": 0,
            "active_updated_at": None,
        }
        self._event_sink: Optional[Callable[[Dict[str, Any]], None]] = None
        self._orchestrator.set_runtime_event_sink(self._on_orchestrator_event)

    def set_event_sink(self, sink: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        self._event_sink = sink

    def start(
        self,
        interval_seconds: float = 1.0,
        max_iterations: Optional[int] = None,
        profile: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        snapshot = None
        with self._lock:
            if self._state["running"]:
                return self._snapshot_unlocked()
            self._stop_event.clear()
            self._state["running"] = True
            self._state["iteration"] = 0
            self._state["last_status"] = "starting"
            self._state["last_score"] = None
            self._state["last_error"] = ""
            self._state["interval_seconds"] = max(0.0, float(interval_seconds))
            self._state["max_iterations"] = None if max_iterations is None else max(1, int(max_iterations))
            self._state["started_at"] = datetime.now().isoformat(timespec="seconds")
            self._state["finished_at"] = None
            self._state["profile"] = profile if isinstance(profile, dict) else {}
            self._state["active_event_type"] = "start"
            self._state["active_phase"] = "start"
            self._state["active_phase_label"] = "任务启动中"
            self._state["active_message"] = "进化任务已启动，等待首轮执行"
            self._state["active_progress_pct"] = 0
            self._state["active_data_status"] = "checking"
            self._state["active_stock_code"] = ""
            self._state["active_timeframe"] = ""
            self._state["active_scenario_index"] = 0
            self._state["active_scenario_total"] = 0
            self._state["active_updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._thread = threading.Thread(target=self._run_loop, name="EvolutionRuntimeThread", daemon=True)
            self._thread.start()
            snapshot = self._snapshot_unlocked()
        self._emit_event({"kind": "state", "state": snapshot})
        return snapshot

    def stop(self) -> Dict[str, Any]:
        thread = None
        snapshot = None
        with self._lock:
            self._stop_event.set()
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        with self._lock:
            if self._state["running"]:
                self._state["running"] = False
                self._state["last_status"] = "stopped"
                self._state["finished_at"] = datetime.now().isoformat(timespec="seconds")
                self._state["active_event_type"] = "stopped"
                self._state["active_phase"] = "stopped"
                self._state["active_phase_label"] = "已停止"
                self._state["active_message"] = "进化任务已停止"
                self._state["active_data_status"] = "idle"
                self._state["active_updated_at"] = datetime.now().isoformat(timespec="seconds")
            snapshot = self._snapshot_unlocked()
        self._emit_event({"kind": "state", "state": snapshot})
        return snapshot

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()

    def history(self, limit: int = 100) -> List[Dict[str, Any]]:
        n = max(1, min(int(limit), 1000))
        with self._lock:
            rows = list(self._history)
        return rows[-n:]

    def top_strategies(self, k: int = 20) -> List[Dict[str, Any]]:
        return self._orchestrator.memory.get_top(k=max(1, min(int(k), 200)))

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                if not self._state["running"]:
                    break
                current_iter = int(self._state["iteration"]) + 1
                max_iter = self._state["max_iterations"]
            if max_iter is not None and current_iter > int(max_iter):
                break

            started = time.time()
            score: Optional[float] = None
            status = "rejected"
            error_text = ""
            detail: Dict[str, Any] = {}
            try:
                with self._lock:
                    profile = dict(self._state.get("profile", {}))
                result = self._orchestrator.run_once(iteration=current_iter, profile_override=profile)
                detail = self._orchestrator.get_last_result()
                if isinstance(result, (int, float)):
                    score = float(result)
                    status = "ok"
                else:
                    status = "rejected"
            except Exception as exc:
                status = "error"
                error_text = str(exc)
                detail = {"reason": f"runtime_exception:{error_text}"}
            reason = str(detail.get("reason", "") or "")
            if status != "error" and reason:
                error_text = reason
            metrics = detail.get("metrics", {}) if isinstance(detail.get("metrics"), dict) else {}

            record = {
                "iteration": current_iter,
                "status": status,
                "score": score,
                "reason": reason,
                "error": error_text,
                "cost_ms": int((time.time() - started) * 1000),
                "parent_strategy_id": str(detail.get("parent_strategy_id", "") or ""),
                "parent_strategy_name": str(detail.get("parent_strategy_name", "") or ""),
                "best_timeframe": str(detail.get("best_timeframe", "") or ""),
                "best_stock_code": str(detail.get("best_stock_code", "") or ""),
                "metrics": dict(metrics),
                "committed": bool(detail.get("committed", False)),
                "committed_strategy_id": str(detail.get("committed_strategy_id", "") or ""),
                "committed_strategy_name": str(detail.get("committed_strategy_name", "") or ""),
                "committed_version": detail.get("committed_version"),
                "time": datetime.now().isoformat(timespec="seconds"),
            }
            with self._lock:
                self._state["iteration"] = current_iter
                self._state["last_status"] = status
                self._state["last_score"] = score
                self._state["last_error"] = error_text
                self._history.append(record)
                interval = float(self._state["interval_seconds"])
            self._emit_event({"kind": "tick", "record": dict(record)})

            if interval > 0:
                self._stop_event.wait(timeout=interval)

        with self._lock:
            self._state["running"] = False
            if str(self._state.get("last_status", "")) == "starting":
                self._state["last_status"] = "stopped"
            self._state["finished_at"] = datetime.now().isoformat(timespec="seconds")
            self._thread = None
        self._emit_event({"kind": "state", "state": self.status()})

    def _on_orchestrator_event(self, event: Dict[str, Any]) -> None:
        payload = event if isinstance(event, dict) else {}
        event_type = str(payload.get("event_type", "") or "")
        body = payload.get("payload", {}) if isinstance(payload.get("payload"), dict) else {}
        event_type_lower = event_type.lower()
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._state["active_event_type"] = event_type_lower
            self._state["active_updated_at"] = now
            if event_type_lower == "start":
                iter_no = int(body.get("iteration", 0) or 0)
                if iter_no > 0:
                    self._state["iteration"] = iter_no
                self._state["last_status"] = "running"
                self._state["active_phase"] = "research"
                self._state["active_phase_label"] = "研究候选策略"
                self._state["active_message"] = f"迭代 {iter_no} 启动"
                self._state["active_progress_pct"] = 1
                self._state["active_data_status"] = "checking"
            elif event_type_lower == "strategygenerated":
                self._state["last_status"] = "running"
                self._state["active_phase"] = "research"
                self._state["active_phase_label"] = "候选策略已生成"
                self._state["active_message"] = "研究员已生成候选策略，等待审核"
                self._state["active_progress_pct"] = max(int(self._state.get("active_progress_pct", 0) or 0), 5)
            elif event_type_lower == "strategyapproved":
                self._state["last_status"] = "running"
                self._state["active_phase"] = "critic"
                self._state["active_phase_label"] = "审核通过"
                self._state["active_message"] = "风控审核通过，准备回测"
                self._state["active_progress_pct"] = max(int(self._state.get("active_progress_pct", 0) or 0), 10)
            elif event_type_lower == "backtestprogress":
                event_data = body.get("event", {}) if isinstance(body.get("event"), dict) else {}
                iter_no = int(body.get("iteration", 0) or 0)
                if iter_no > 0:
                    self._state["iteration"] = max(int(self._state.get("iteration", 0) or 0), iter_no)
                self._state["last_status"] = "running"
                self._state["active_phase"] = str(event_data.get("phase", "") or "backtest")
                self._state["active_phase_label"] = str(event_data.get("phase_label", "") or "回测中")
                msg = str(event_data.get("message", "") or "").strip()
                self._state["active_message"] = msg or "回测执行中"
                progress = event_data.get("progress_pct")
                try:
                    pct = int(progress)
                except Exception:
                    pct = int(self._state.get("active_progress_pct", 0) or 0)
                bounded_pct = max(0, min(100, pct))
                self._state["active_progress_pct"] = max(int(self._state.get("active_progress_pct", 0) or 0), bounded_pct)
                data_status = str(event_data.get("data_status", "") or "").strip().lower()
                if data_status:
                    self._state["active_data_status"] = data_status
                self._state["active_stock_code"] = str(event_data.get("stock_code", "") or "")
                self._state["active_timeframe"] = str(event_data.get("timeframe", "") or "")
                self._state["active_scenario_index"] = int(event_data.get("scenario_index", 0) or 0)
                self._state["active_scenario_total"] = int(event_data.get("scenario_total", 0) or 0)
            elif event_type_lower == "backtestfinished":
                self._state["last_status"] = "running"
                metrics = body.get("metrics", {}) if isinstance(body.get("metrics"), dict) else {}
                data_status = "ok"
                details = metrics.get("details", []) if isinstance(metrics.get("details"), list) else []
                if details:
                    any_bad = False
                    for row in details:
                        metric = row.get("metrics", {}) if isinstance(row, dict) else {}
                        if not bool(metric.get("_data_ok", False)):
                            any_bad = True
                            break
                    if any_bad:
                        data_status = "warning"
                self._state["active_phase"] = "backtest_done"
                self._state["active_phase_label"] = "回测完成"
                self._state["active_message"] = "回测完成，等待评分"
                self._state["active_progress_pct"] = max(int(self._state.get("active_progress_pct", 0) or 0), 95)
                self._state["active_data_status"] = data_status
            elif event_type_lower == "strategyscored":
                status = str(body.get("status", "") or "").strip().lower()
                if status:
                    self._state["last_status"] = status
                self._state["active_phase"] = "scoring"
                self._state["active_phase_label"] = "评分完成"
                self._state["active_message"] = f"评分结果: {status or '--'}"
                self._state["active_progress_pct"] = 100
                if status == "error":
                    self._state["active_data_status"] = "error"
                elif status == "ok":
                    self._state["active_data_status"] = "ok"
            elif event_type_lower == "strategyrejected":
                reason = str(body.get("reason", "") or "").strip()
                self._state["active_phase"] = "rejected"
                self._state["active_phase_label"] = "审核拒绝"
                self._state["active_message"] = reason or "候选策略被拒绝"
                self._state["active_progress_pct"] = 100
                self._state["active_data_status"] = "warning"
            elif event_type_lower == "strategycommitted":
                sid = str(body.get("strategy_id", "") or "").strip()
                self._state["active_phase"] = "committed"
                self._state["active_phase_label"] = "策略已入库"
                self._state["active_message"] = f"新增策略 {sid or '--'} 已入库"
                self._state["active_progress_pct"] = 100
                self._state["active_data_status"] = "ok"
            progress_snapshot = self._progress_snapshot_unlocked()
        self._emit_event({"kind": "progress", "progress": progress_snapshot})

    def _emit_event(self, payload: Dict[str, Any]) -> None:
        sink = self._event_sink
        if sink is None:
            return
        try:
            sink(payload if isinstance(payload, dict) else {})
        except Exception:
            pass

    def _snapshot_unlocked(self) -> Dict[str, Any]:
        return {
            "running": bool(self._state["running"]),
            "iteration": int(self._state["iteration"]),
            "last_status": str(self._state["last_status"]),
            "last_score": self._state["last_score"],
            "last_error": str(self._state["last_error"] or ""),
            "interval_seconds": float(self._state["interval_seconds"]),
            "max_iterations": self._state["max_iterations"],
            "started_at": self._state["started_at"],
            "finished_at": self._state["finished_at"],
            "history_count": len(self._history),
            "profile": dict(self._state.get("profile", {})),
            "activity": self._progress_snapshot_unlocked(),
        }

    def _progress_snapshot_unlocked(self) -> Dict[str, Any]:
        return {
            "iteration": int(self._state.get("iteration", 0) or 0),
            "event_type": str(self._state.get("active_event_type", "") or ""),
            "phase": str(self._state.get("active_phase", "") or ""),
            "phase_label": str(self._state.get("active_phase_label", "") or ""),
            "message": str(self._state.get("active_message", "") or ""),
            "progress_pct": int(self._state.get("active_progress_pct", 0) or 0),
            "data_status": str(self._state.get("active_data_status", "") or "idle"),
            "stock_code": str(self._state.get("active_stock_code", "") or ""),
            "timeframe": str(self._state.get("active_timeframe", "") or ""),
            "scenario_index": int(self._state.get("active_scenario_index", 0) or 0),
            "scenario_total": int(self._state.get("active_scenario_total", 0) or 0),
            "updated_at": self._state.get("active_updated_at"),
        }
