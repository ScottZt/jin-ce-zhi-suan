from __future__ import annotations

import ast
import re
from typing import Any, Callable, Dict, List

from src.evolution.core.event_bus import EventBus

class Critic:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self._rules: List[Callable[[str], bool]] = [
            self._check_not_empty_strategy,
            self._check_no_future_function,
            self._check_no_obvious_violations,
        ]
        self.bus.subscribe("StrategyGenerated", self._on_strategy_generated)

    def _on_strategy_generated(self, data: Dict[str, Any]) -> None:
        payload = data if isinstance(data, dict) else {}
        strategy_code = str(payload.get("strategy_code", "") or "")
        iteration = int(payload.get("iteration", 0) or 0)
        base_payload = dict(payload)
        base_payload["iteration"] = iteration
        base_payload["strategy_code"] = strategy_code
        if self.validate(strategy_code):
            self.bus.publish("StrategyApproved", base_payload)
            return
        base_payload["reason"] = "critic_rejected"
        self.bus.publish("StrategyRejected", base_payload)

    def validate(self, strategy_code: str) -> bool:
        if not isinstance(strategy_code, str):
            return False
        code = strategy_code.strip()
        if not code:
            return False
        for rule in self._rules:
            if not rule(code):
                return False
        return True

    def __call__(self, strategy_code: str) -> bool:
        return self.validate(strategy_code)

    def _check_not_empty_strategy(self, code: str) -> bool:
        if "class " not in code or "def on_bar" not in code:
            return False
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError:
            return False

        class_node = self._find_first_class(tree)
        if class_node is None:
            return False
        on_bar = self._find_method(class_node, "on_bar")
        if on_bar is None:
            return False

        effective_nodes = [n for n in on_bar.body if not isinstance(n, ast.Expr) or not self._is_docstring_expr(n)]
        if not effective_nodes:
            return False
        if len(effective_nodes) == 1:
            node = effective_nodes[0]
            if isinstance(node, ast.Pass):
                return False
            if isinstance(node, ast.Return) and node.value is None:
                return False
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and node.value.value is None:
                return False
        return True

    def _check_no_future_function(self, code: str) -> bool:
        suspicious_patterns = [
            r"\.shift\(\s*-\d+\s*\)",
            r"iloc\s*\[\s*[^]]*\+\s*\d+\s*\]",
            r"\blead\s*\(",
            r"forward[_\s-]*look",
            r"future[_\s-]*return",
            r"next[_\s-]*bar",
            r"tomorrow",
        ]
        lowered = code.lower()
        for pattern in suspicious_patterns:
            if re.search(pattern, lowered):
                return False
        return True

    def _check_no_obvious_violations(self, code: str) -> bool:
        blocked_calls = {
            "eval",
            "exec",
            "compile",
            "__import__",
            "open",
            "input",
        }
        blocked_imports = {
            "os",
            "subprocess",
            "socket",
            "requests",
            "urllib",
            "httpx",
            "shutil",
            "pathlib",
        }
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError:
            return False

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = str(alias.name or "").split(".")[0]
                    if root in blocked_imports:
                        return False
            if isinstance(node, ast.ImportFrom):
                root = str(node.module or "").split(".")[0]
                if root in blocked_imports:
                    return False
            if isinstance(node, ast.Call):
                name = self._call_name(node)
                if name in blocked_calls:
                    return False
        return True

    def _find_first_class(self, tree: ast.AST) -> ast.ClassDef | None:
        for node in getattr(tree, "body", []):
            if isinstance(node, ast.ClassDef):
                return node
        return None

    def _find_method(self, class_node: ast.ClassDef, method_name: str) -> ast.FunctionDef | None:
        for node in class_node.body:
            if isinstance(node, ast.FunctionDef) and node.name == method_name:
                return node
        return None

    def _is_docstring_expr(self, node: ast.Expr) -> bool:
        return isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)

    def _call_name(self, node: ast.Call) -> str:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return ""

