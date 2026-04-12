from __future__ import annotations

import asyncio
import re
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import src.core.backtest_cabinet as backtest_cabinet_module
from src.core.backtest_cabinet import BacktestCabinet
from src.evolution.core.strategy_loader import StrategyLoader
from src.utils.config_loader import ConfigLoader


class BacktestAdapter:
    def __init__(self, strategy_loader: Optional[StrategyLoader] = None):
        self.strategy_loader = strategy_loader or StrategyLoader()

    def run_backtest(
        self,
        strategy_code: str,
        stock_code: Optional[str] = None,
        stock_codes: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        initial_capital: Optional[float] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        try:
            asyncio.get_running_loop()
            return self._empty_metrics()
        except RuntimeError:
            return asyncio.run(
                self.run_backtest_async(
                    strategy_code=strategy_code,
                    stock_code=stock_code,
                    stock_codes=stock_codes,
                    timeframes=timeframes,
                    start_date=start_date,
                    end_date=end_date,
                    initial_capital=initial_capital,
                    progress_callback=progress_callback,
                )
            )

    async def run_backtest_async(
        self,
        strategy_code: str,
        stock_code: Optional[str] = None,
        stock_codes: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        initial_capital: Optional[float] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        target_stocks = self._resolve_stock_codes(stock_code=stock_code, stock_codes=stock_codes)
        target_timeframes = self._resolve_timeframes(timeframes=timeframes)
        init_capital = self._resolve_initial_capital(initial_capital)
        total_scenarios = max(1, len(target_stocks) * len(target_timeframes))
        self._emit_progress(
            progress_callback,
            {
                "stage": "scenarios_start",
                "phase": "prepare",
                "phase_label": "准备评估场景",
                "message": f"准备执行 {total_scenarios} 个评估场景",
                "scenario_index": 0,
                "scenario_total": total_scenarios,
                "progress_pct": 0,
                "data_status": "checking",
            },
        )
        scenarios: List[Dict[str, Any]] = []
        scenario_index = 0
        for code in target_stocks:
            for tf in target_timeframes:
                scenario_index += 1
                self._emit_progress(
                    progress_callback,
                    {
                        "stage": "scenario_start",
                        "phase": "data_fetch",
                        "phase_label": "场景数据获取",
                        "message": f"场景 {scenario_index}/{total_scenarios} 开始: {code} · {tf}",
                        "stock_code": code,
                        "timeframe": tf,
                        "scenario_index": scenario_index,
                        "scenario_total": total_scenarios,
                        "progress_pct": int(round(((scenario_index - 1) / total_scenarios) * 100)),
                        "data_status": "checking",
                    },
                )
                metrics = await self._run_single(
                    strategy_code=strategy_code,
                    stock_code=code,
                    timeframe=tf,
                    start_date=start_date,
                    end_date=end_date,
                    initial_capital=init_capital,
                    scenario_index=scenario_index,
                    scenario_total=total_scenarios,
                    progress_callback=progress_callback,
                )
                scenarios.append({"stock_code": code, "timeframe": tf, "metrics": metrics})
                data_ok = bool(metrics.get("_data_ok", False))
                progress_pct = int(round((scenario_index / total_scenarios) * 100))
                self._emit_progress(
                    progress_callback,
                    {
                        "stage": "scenario_done",
                        "phase": "scenario_done",
                        "phase_label": "场景回测完成",
                        "message": f"场景 {scenario_index}/{total_scenarios} 完成: {code} · {tf}",
                        "stock_code": code,
                        "timeframe": tf,
                        "scenario_index": scenario_index,
                        "scenario_total": total_scenarios,
                        "progress_pct": progress_pct,
                        "data_status": "ok" if data_ok else "warning",
                        "scenario_data_ok": data_ok,
                    },
                )
        self._emit_progress(
            progress_callback,
            {
                "stage": "scenarios_done",
                "phase": "aggregate",
                "phase_label": "汇总评分",
                "message": "全部评估场景已完成，正在汇总评分",
                "scenario_index": total_scenarios,
                "scenario_total": total_scenarios,
                "progress_pct": 100,
                "data_status": "ok",
            },
        )
        return self._aggregate_metrics(scenarios)

    async def _run_single(
        self,
        strategy_code: str,
        stock_code: str,
        timeframe: str,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        initial_capital: float,
        scenario_index: int,
        scenario_total: int,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        try:
            strategy = self.strategy_loader.load_from_code(strategy_code)
        except Exception:
            out = self._empty_metrics()
            out["_data_ok"] = False
            out["_data_message"] = "strategy_load_failed"
            return out
        self._apply_timeframe(strategy, timeframe)
        events: Dict[str, Any] = {"backtest_result": None, "backtest_failed": None}

        async def _event_callback(event_type: str, data: Dict[str, Any]) -> None:
            if event_type == "backtest_result":
                events["backtest_result"] = data
            if event_type == "backtest_failed":
                events["backtest_failed"] = data
            if event_type == "backtest_progress":
                payload = data if isinstance(data, dict) else {}
                self._emit_progress(
                    progress_callback,
                    {
                        "stage": "backtest_progress",
                        "phase": str(payload.get("phase", "") or "data_fetch"),
                        "phase_label": str(payload.get("phase_label", "") or ""),
                        "message": str(payload.get("msg", "") or payload.get("phase_label", "") or "回测执行中"),
                        "progress_pct": self._to_global_progress(
                            local_progress=self._to_int(payload.get("progress", 0)),
                            scenario_index=scenario_index,
                            scenario_total=scenario_total,
                        ),
                        "current_date": str(payload.get("current_date", "") or ""),
                        "stock_code": stock_code,
                        "timeframe": timeframe,
                        "scenario_index": scenario_index,
                        "scenario_total": scenario_total,
                        "data_status": "checking",
                    },
                )
            if event_type in {"system", "backtest_flow"}:
                payload = data if isinstance(data, dict) else {}
                msg = str(payload.get("msg", "") or "").strip()
                rows = self._extract_kline_rows(msg)
                if rows is not None:
                    self._emit_progress(
                        progress_callback,
                        {
                            "stage": "kline_rows",
                            "phase": "data_fetch",
                            "phase_label": "历史K线拉取完成",
                            "message": f"历史K线已就绪，共 {rows} 条",
                            "progress_pct": self._to_global_progress(
                                local_progress=18,
                                scenario_index=scenario_index,
                                scenario_total=scenario_total,
                            ),
                            "stock_code": stock_code,
                            "timeframe": timeframe,
                            "scenario_index": scenario_index,
                            "scenario_total": scenario_total,
                            "data_status": "ok",
                            "kline_rows": int(rows),
                        },
                    )
        try:
            with self._inject_single_strategy(strategy):
                cabinet = BacktestCabinet(
                    stock_code=stock_code,
                    strategy_id="all",
                    initial_capital=initial_capital,
                    event_callback=_event_callback,
                )
                await cabinet.run(start_date=start_date, end_date=end_date)
        except Exception:
            out = self._empty_metrics()
            out["_data_ok"] = False
            out["_data_message"] = "runtime_exception"
            return out
        if events.get("backtest_failed"):
            failed_msg = ""
            failed_payload = events.get("backtest_failed")
            if isinstance(failed_payload, dict):
                failed_msg = str(failed_payload.get("msg", "") or "")
            out = self._empty_metrics()
            out["_data_ok"] = False
            out["_data_message"] = failed_msg or "backtest_failed"
            return out
        result_payload = events.get("backtest_result")
        if not isinstance(result_payload, dict):
            out = self._empty_metrics()
            out["_data_ok"] = False
            out["_data_message"] = "missing_backtest_result"
            return out
        out = self._extract_metrics(result_payload)
        out["_data_ok"] = True
        out["_data_message"] = "ok"
        return out

    @contextmanager
    def _inject_single_strategy(self, strategy):
        original_create_strategies = backtest_cabinet_module.create_strategies

        def _create_strategies(apply_active_filter: bool = True):
            return [strategy]

        backtest_cabinet_module.create_strategies = _create_strategies
        try:
            yield
        finally:
            backtest_cabinet_module.create_strategies = original_create_strategies

    def _extract_metrics(self, backtest_result: Optional[Dict[str, Any]]) -> Dict[str, float]:
        if not isinstance(backtest_result, dict):
            return self._empty_metrics()
        ranking = backtest_result.get("ranking")
        if not isinstance(ranking, list) or not ranking:
            return self._empty_metrics()
        top = ranking[0] if isinstance(ranking[0], dict) else {}
        return {
            "sharpe": self._to_float(top.get("sharpe", 0.0)),
            "drawdown": self._to_float(top.get("max_dd", top.get("max_drawdown", 0.0))),
            "win_rate": self._to_float(top.get("win_rate", 0.0)),
            "total_return": self._to_float(top.get("roi", top.get("total_return", 0.0))),
            "profit_factor": self._to_float(top.get("profit_factor", 0.0)),
        }

    def _aggregate_metrics(self, scenarios: List[Dict[str, Any]]) -> Dict[str, Any]:
        valid = [x for x in scenarios if isinstance(x, dict) and isinstance(x.get("metrics"), dict)]
        if not valid:
            return self._empty_metrics()
        total = {
            "sharpe": 0.0,
            "drawdown": 0.0,
            "win_rate": 0.0,
            "total_return": 0.0,
            "profit_factor": 0.0,
        }
        best_score = None
        best_stock = ""
        best_timeframe = ""
        for row in valid:
            metrics = row.get("metrics", {})
            sharpe = self._to_float(metrics.get("sharpe", 0.0))
            drawdown = self._to_float(metrics.get("drawdown", 0.0))
            win_rate = self._to_float(metrics.get("win_rate", 0.0))
            total_return = self._to_float(metrics.get("total_return", 0.0))
            profit_factor = self._to_float(metrics.get("profit_factor", 0.0))
            total["sharpe"] += sharpe
            total["drawdown"] += drawdown
            total["win_rate"] += win_rate
            total["total_return"] += total_return
            total["profit_factor"] += profit_factor
            score = sharpe * 0.4 + win_rate * 0.2 + profit_factor * 0.2 - max(0.0, drawdown) * 0.2
            if best_score is None or score > best_score:
                best_score = score
                best_stock = str(row.get("stock_code", "") or "")
                best_timeframe = str(row.get("timeframe", "") or "")
        count = float(len(valid))
        merged = {k: v / count for k, v in total.items()}
        merged["scenario_count"] = count
        merged["best_stock_code"] = best_stock
        merged["best_timeframe"] = best_timeframe
        merged["details"] = valid
        return merged

    def _resolve_default_stock_code(self) -> str:
        cfg = ConfigLoader.reload()
        targets = cfg.get("targets", [])
        if isinstance(targets, list) and targets:
            first = str(targets[0] or "").strip()
            if first:
                return first
        return "000001.SZ"

    def _resolve_stock_codes(self, stock_code: Optional[str], stock_codes: Optional[List[str]]) -> List[str]:
        if stock_code and str(stock_code).strip():
            return [str(stock_code).strip()]
        values = [str(x or "").strip() for x in (stock_codes or [])]
        values = [x for x in values if x]
        if values:
            return values
        cfg = ConfigLoader.reload()
        targets = cfg.get("targets", [])
        if isinstance(targets, list):
            out = [str(x or "").strip() for x in targets if str(x or "").strip()]
            if out:
                return out
        return [self._resolve_default_stock_code()]

    def _resolve_timeframes(self, timeframes: Optional[List[str]]) -> List[str]:
        values = [str(x or "").strip() for x in (timeframes or [])]
        values = [x for x in values if x]
        if values:
            return values
        cfg = ConfigLoader.reload()
        cfg_values = cfg.get("evolution.evaluation.timeframes", [])
        if isinstance(cfg_values, list):
            out = [str(x or "").strip() for x in cfg_values if str(x or "").strip()]
            if out:
                return out
        return ["1min"]

    def _apply_timeframe(self, strategy: Any, timeframe: str) -> None:
        tf = str(timeframe or "").strip() or "1min"
        try:
            strategy.trigger_timeframe = tf
        except Exception:
            pass

    def _resolve_initial_capital(self, initial_capital: Optional[float]) -> float:
        if initial_capital is not None:
            return self._to_float(initial_capital, default=1_000_000.0)
        cfg = ConfigLoader.reload()
        return self._to_float(cfg.get("system.initial_capital", 1_000_000.0), default=1_000_000.0)

    def _empty_metrics(self) -> Dict[str, float]:
        return {
            "sharpe": 0.0,
            "drawdown": 0.0,
            "win_rate": 0.0,
            "total_return": 0.0,
            "profit_factor": 0.0,
        }

    def _to_float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    def _to_int(self, value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return int(default)

    def _emit_progress(self, callback: Optional[Callable[[Dict[str, Any]], None]], payload: Dict[str, Any]) -> None:
        if callback is None:
            return
        try:
            callback(payload if isinstance(payload, dict) else {})
        except Exception:
            pass

    def _extract_kline_rows(self, text: str) -> Optional[int]:
        msg = str(text or "").strip()
        if not msg:
            return None
        patterns = [
            r"已获取\s*([0-9]+)\s*条K线",
            r"共\s*([0-9]+)\s*条分钟K线",
            r"共\s*([0-9]+)\s*条K线",
        ]
        for p in patterns:
            m = re.search(p, msg)
            if not m:
                continue
            try:
                n = int(m.group(1))
                return n if n >= 0 else None
            except Exception:
                continue
        return None

    def _to_global_progress(self, local_progress: int, scenario_index: int, scenario_total: int) -> int:
        total = max(1, int(scenario_total or 1))
        idx = max(1, min(int(scenario_index or 1), total))
        local = max(0, min(100, int(local_progress or 0)))
        start_pct = ((idx - 1) / total) * 100.0
        end_pct = (idx / total) * 100.0
        pct = start_pct + ((end_pct - start_pct) * (local / 100.0))
        return int(max(0, min(100, round(pct))))

