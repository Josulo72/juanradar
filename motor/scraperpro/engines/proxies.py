"""Rotación de proxies con salud por proxy (los que fallan se enfrían un tiempo)."""
from __future__ import annotations

import itertools
import threading
import time
from urllib.parse import urlparse

from ..config import settings


class ProxyPool:
    def __init__(self, proxies: list[str]):
        self.proxies = list(proxies)
        self._cycle = itertools.cycle(self.proxies) if self.proxies else None
        self._cooldown: dict[str, float] = {}
        self._lock = threading.Lock()

    def __bool__(self) -> bool:
        return bool(self.proxies)

    def next(self) -> str | None:
        if not self._cycle:
            return None
        with self._lock:
            now = time.time()
            for _ in range(len(self.proxies)):
                p = next(self._cycle)
                if self._cooldown.get(p, 0) <= now:
                    return p
            return min(self.proxies, key=lambda p: self._cooldown.get(p, 0))

    def penalize(self, proxy: str | None, seconds: float = 120) -> None:
        if proxy:
            with self._lock:
                self._cooldown[proxy] = time.time() + seconds


def playwright_proxy(proxy: str | None) -> dict | None:
    if not proxy:
        return None
    u = urlparse(proxy)
    out = {"server": f"{u.scheme}://{u.hostname}:{u.port}" if u.port else f"{u.scheme}://{u.hostname}"}
    if u.username:
        out["username"] = u.username
        out["password"] = u.password or ""
    return out


def mask(proxy: str | None) -> str:
    if not proxy:
        return "none"
    u = urlparse(proxy)
    return f"{u.scheme}://{u.hostname}:{u.port}"


pool = ProxyPool(settings.proxies)
