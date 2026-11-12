"""Tipos comunes de los motores de descarga."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    html: str = ""
    content: bytes = b""
    content_type: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    engine: str = ""
    proxy_used: str = "none"
    elapsed_ms: int = 0
    screenshot: Optional[str] = None  # base64 PNG
    actions: dict[str, Any] = field(default_factory=dict)
    network: list[dict] = field(default_factory=list)
    js_return: Any = None
    blocked: Optional[str] = None
    error: Optional[str] = None
    attempts: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and self.blocked is None and (200 <= self.status < 400 or bool(self.html or self.content))

    @property
    def is_document(self) -> bool:
        ct = self.content_type.lower()
        return any(x in ct for x in ("pdf", "officedocument", "msword", "opendocument")) or self.content[:4] == b"%PDF"


class EngineError(Exception):
    pass
