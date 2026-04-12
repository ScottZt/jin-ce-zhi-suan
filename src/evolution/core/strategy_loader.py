from __future__ import annotations

import ast
import builtins
from typing import Any, Dict, Mapping, Optional, Sequence, Type

import numpy as np
import pandas as pd

from src.strategies.implemented_strategies import BaseImplementedStrategy
from src.utils.indicators import Indicators
from src.utils.runtime_params import get_value


class StrategyLoadError(Exception):
    """Raised when strategy code cannot be safely loaded or instantiated."""


class StrategyLoader:
    DEFAULT_ALLOWED_IMPORTS = {
        "numpy",
        "pandas",
        "src.utils.indicators",
        "src.utils.runtime_params",
        "src.strategies.implemented_strategies",
    }

    SAFE_BUILTINS = {
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "float": float,
        "int": int,
        "len": len,
        "list": list,
        "max": max,
        "min": min,
        "range": range,
        "round": round,
        "set": set,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "super": super,
        "tuple": tuple,
        "zip": zip,
        "__build_class__": builtins.__build_class__,
        "object": object,
        "Exception": Exception,
    }

    def __init__(
        self,
        allowed_imports: Optional[Sequence[str]] = None,
        extra_globals: Optional[Mapping[str, Any]] = None,
        max_code_length: int = 200_000,
    ):
        self.allowed_imports = set(allowed_imports or self.DEFAULT_ALLOWED_IMPORTS)
        self.extra_globals = dict(extra_globals or {})
        self.max_code_length = int(max_code_length) if int(max_code_length) > 0 else 200_000

    def load_from_code(
        self,
        strategy_code: str,
        strategy_id: str = "EVOL",
        strategy_name: str = "EvolutionStrategy",
    ) -> BaseImplementedStrategy:
        if not isinstance(strategy_code, str) or not strategy_code.strip():
            raise StrategyLoadError("strategy_code 不能为空")
        if len(strategy_code) > self.max_code_length:
            raise StrategyLoadError(f"strategy_code 过长，超过限制: {self.max_code_length}")

        tree = self._parse_and_validate(strategy_code)

        runtime_globals: Dict[str, object] = {
            "__name__": "__strategy_runtime__",
            "__builtins__": self._build_runtime_builtins(),
            "BaseImplementedStrategy": BaseImplementedStrategy,
            "Indicators": Indicators,
            "pd": pd,
            "np": np,
            "get_value": get_value,
        }
        runtime_globals.update(self.extra_globals)
        runtime_locals: Dict[str, object] = {}

        try:
            code_obj = compile(tree, filename="<strategy_code>", mode="exec")
            exec(code_obj, runtime_globals, runtime_locals)
        except Exception as exc:
            raise StrategyLoadError(f"执行策略代码失败: {exc}") from exc

        namespace = dict(runtime_globals)
        namespace.update(runtime_locals)
        strategy_cls = self._pick_strategy_class(namespace)
        try:
            strategy = strategy_cls()
        except Exception as exc:
            raise StrategyLoadError(f"实例化策略失败: {exc}") from exc
        if not isinstance(strategy, BaseImplementedStrategy):
            raise StrategyLoadError("动态策略必须继承 BaseImplementedStrategy")

        if not str(getattr(strategy, "id", "")).strip():
            strategy.id = strategy_id
        if not str(getattr(strategy, "name", "")).strip():
            strategy.name = strategy_name
        return strategy

    def _parse_and_validate(self, strategy_code: str) -> ast.AST:
        try:
            tree = ast.parse(strategy_code, mode="exec")
        except SyntaxError as exc:
            raise StrategyLoadError(f"策略代码语法错误: {exc}") from exc

        forbidden_calls = {"eval", "exec", "__import__", "open", "compile", "input"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                raise StrategyLoadError("不允许使用 global/nonlocal")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not self._is_allowed_module(alias.name):
                        raise StrategyLoadError(f"不允许导入模块: {alias.name}")
            if isinstance(node, ast.ImportFrom):
                module_name = node.module or ""
                if not self._is_allowed_module(module_name):
                    raise StrategyLoadError(f"不允许导入模块: {module_name}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in forbidden_calls:
                    raise StrategyLoadError(f"不允许调用高风险函数: {node.func.id}")
        return tree

    def _is_allowed_module(self, module_name: str) -> bool:
        name = str(module_name or "").strip()
        if not name:
            return False
        for allowed in self.allowed_imports:
            allowed = str(allowed or "").strip()
            if not allowed:
                continue
            if name == allowed or name.startswith(allowed + "."):
                return True
        return False

    def _pick_strategy_class(self, namespace: Dict[str, object]) -> Type[BaseImplementedStrategy]:
        candidates = []
        for value in namespace.values():
            if not isinstance(value, type):
                continue
            if not issubclass(value, BaseImplementedStrategy):
                continue
            if value is BaseImplementedStrategy:
                continue
            candidates.append(value)

        if not candidates:
            raise StrategyLoadError("strategy_code 中未找到继承 BaseImplementedStrategy 的策略类")
        return candidates[-1]

    def _build_runtime_builtins(self) -> Dict[str, Any]:
        safe = dict(self.SAFE_BUILTINS)
        safe["__import__"] = self._safe_import
        return safe

    def _safe_import(self, name, globals=None, locals=None, fromlist=(), level=0):
        module_name = str(name or "").strip()
        if not self._is_allowed_module(module_name):
            raise StrategyLoadError(f"不允许导入模块: {module_name}")
        return builtins.__import__(name, globals, locals, fromlist, level)

