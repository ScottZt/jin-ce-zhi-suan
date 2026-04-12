from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from typing import Optional

# 兼容直接脚本运行：python src/evolution/runner/run_evolution.py
if __package__ in (None, ""):
    _PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)

from src.evolution.core.orchestrator import EvolutionOrchestrator


class EvolutionRunner:
    """
    循环执行策略进化任务。
    """

    def __init__(
        self,
        orchestrator: Optional[EvolutionOrchestrator] = None,
        sleep_seconds: float = 1.0,
        max_iterations: Optional[int] = None,
        max_consecutive_rejected: int = 30,
        rejected_backoff_seconds: float = 5.0,
    ):
        self.orchestrator = orchestrator or EvolutionOrchestrator()
        self.sleep_seconds = max(0.0, float(sleep_seconds))
        self.max_iterations = None if max_iterations is None else max(1, int(max_iterations))
        self.max_consecutive_rejected = max(1, int(max_consecutive_rejected))
        self.rejected_backoff_seconds = max(0.0, float(rejected_backoff_seconds))

    def run_forever(self) -> None:
        iteration = 0
        consecutive_rejected = 0
        while True:
            if self.max_iterations is not None and iteration >= self.max_iterations:
                break
            iteration += 1
            score = None
            status = "unknown"
            try:
                result = self.orchestrator.run_once(iteration=iteration)
                if isinstance(result, (int, float)):
                    score = float(result)
                    status = "ok"
                    consecutive_rejected = 0
                elif str(result).strip().lower() == "rejected":
                    status = "rejected"
                    consecutive_rejected += 1
                else:
                    status = "rejected"
                    consecutive_rejected += 1
            except Exception:
                status = "error"
                consecutive_rejected += 1

            self._log(iteration=iteration, score=score, status=status)
            if consecutive_rejected >= self.max_consecutive_rejected:
                self._log(
                    iteration=iteration,
                    score=score,
                    status=f"backoff({consecutive_rejected})",
                )
                if self.rejected_backoff_seconds > 0:
                    time.sleep(self.rejected_backoff_seconds)
                consecutive_rejected = 0
                continue

            if self.sleep_seconds > 0:
                time.sleep(self.sleep_seconds)

    def _log(self, iteration: int, score: Optional[float], status: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        score_text = "-" if score is None else f"{score:.6f}"
        print(f"[{ts}] iteration={iteration} score={score_text} status={status}", flush=True)


def main() -> None:
    runner = EvolutionRunner()
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        print("evolution stopped by user", flush=True)


if __name__ == "__main__":
    main()

