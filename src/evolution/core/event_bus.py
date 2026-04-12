from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, DefaultDict, Dict, List


EventHandler = Callable[[Dict[str, Any]], None]


class EventBus:
    def __init__(self):
        self._handlers: DefaultDict[str, List[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type 不能为空")
        if not callable(handler):
            raise TypeError("handler 必须可调用")
        self._handlers[event_type].append(handler)

    def publish(self, event_type: str, data: Dict[str, Any]) -> None:
        payload = data if isinstance(data, dict) else {}
        for handler in list(self._handlers.get(event_type, [])):
            handler(payload)
