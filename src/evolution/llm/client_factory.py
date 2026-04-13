from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

from src.utils.config_loader import ConfigLoader


class StrategyLLMClient(Protocol):
    def generate(self, prompt: str, context: Dict[str, Any]) -> str:
        ...


@dataclass
class EvolutionLLMConfig:
    enabled: bool = False
    provider: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.2
    max_tokens: int = 1200
    timeout_seconds: int = 30
    retry_times: int = 1
    fallback_to_mock: bool = True
    system_prompt: str = (
        "你是量化策略生成助手。只输出可执行Python策略代码，"
        "必须继承BaseImplementedStrategy，必须包含on_bar，不要输出解释。"
    )

    @classmethod
    def from_config(cls, cfg: ConfigLoader) -> "EvolutionLLMConfig":
        base_url = str(
            os.environ.get("EVOLUTION_LLM_BASE_URL", "")
            or cfg.get("evolution.llm.base_url", "")
            or ""
        ).strip()
        api_key = str(
            os.environ.get("EVOLUTION_LLM_API_KEY", "")
            or cfg.get("evolution.llm.api_key", "")
            or ""
        ).strip()
        model = str(
            os.environ.get("EVOLUTION_LLM_MODEL", "")
            or cfg.get("evolution.llm.model", "")
            or ""
        ).strip()
        enabled = bool(cfg.get("evolution.llm.enabled", False))
        return cls(
            enabled=enabled,
            provider=str(cfg.get("evolution.llm.provider", "openai_compatible") or "openai_compatible"),
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=float(cfg.get("evolution.llm.temperature", 0.2) or 0.2),
            max_tokens=max(128, int(cfg.get("evolution.llm.max_tokens", 1200) or 1200)),
            timeout_seconds=max(5, int(cfg.get("evolution.llm.timeout_seconds", 30) or 30)),
            retry_times=max(0, int(cfg.get("evolution.llm.retry_times", 1) or 1)),
            fallback_to_mock=bool(cfg.get("evolution.llm.fallback_to_mock", True)),
            system_prompt=str(cfg.get("evolution.llm.system_prompt", cls.system_prompt) or cls.system_prompt),
        )

    def is_ready(self) -> bool:
        return self.enabled and bool(self.base_url) and bool(self.model)


class OpenAICompatibleStrategyLLM:
    def __init__(self, cfg: EvolutionLLMConfig):
        self.cfg = cfg
        self.last_call_meta: Dict[str, Any] = {}

    def generate(self, prompt: str, context: Dict[str, Any]) -> str:
        req_body = self._build_request_body(prompt=prompt, context=context)
        endpoint = self._endpoint()
        last_error = None
        for _ in range(self.cfg.retry_times + 1):
            try:
                content = self._request_once(endpoint=endpoint, body=req_body)
                code = self._extract_code(content)
                if code.strip():
                    self.last_call_meta = {
                        "provider": "openai_compatible",
                        "model": self.cfg.model,
                        "endpoint": endpoint,
                        "fallback_used": False,
                        "path": "direct",
                    }
                    return code
                raise RuntimeError("LLM 返回内容为空")
            except Exception as exc:
                last_error = exc
        self.last_call_meta = {
            "provider": "openai_compatible",
            "model": self.cfg.model,
            "endpoint": endpoint,
            "fallback_used": False,
            "path": "direct",
            "error": str(last_error),
        }
        raise RuntimeError(f"LLM 调用失败: {last_error}")

    def _endpoint(self) -> str:
        base = self.cfg.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _build_request_body(self, prompt: str, context: Dict[str, Any]) -> Dict[str, Any]:
        seed_code = str(context.get("seed_code", "") or "")
        user_prompt = (
            f"{prompt}\n\n"
            "请基于以下种子策略做改写，保持策略风格但不要完全重复：\n"
            f"{seed_code}\n\n"
            f"目标上下文：{json.dumps(context, ensure_ascii=False)}\n"
            "只输出Python代码。"
        )
        return {
            "model": self.cfg.model,
            "temperature": float(self.cfg.temperature),
            "max_tokens": int(self.cfg.max_tokens),
            "messages": [
                {"role": "system", "content": self.cfg.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

    def _request_once(self, endpoint: str, body: Dict[str, Any]) -> str:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        req = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else str(exc)
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        choices = data.get("choices", [])
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("LLM 响应缺少 choices")
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = str(message.get("content", "") or "")
        if not content.strip():
            raise RuntimeError("LLM 响应内容为空")
        return content

    def _extract_code(self, text: str) -> str:
        content = str(text or "")
        m = re.search(r"```(?:python)?\s*([\s\S]*?)```", content, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
        return content.strip()


def load_evolution_llm_config() -> EvolutionLLMConfig:
    cfg = ConfigLoader.reload()
    return EvolutionLLMConfig.from_config(cfg)
