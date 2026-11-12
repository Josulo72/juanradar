"""Motor HTTP sin navegador con huella TLS/HTTP2 de Chrome real (curl_cffi).

Equivalente al `tlsclient` de fire-engine (solo en la nube de pago de Firecrawl). Si curl_cffi no
está disponible, cae a httpx.
"""
from __future__ import annotations

import re
import time

import httpx

from ..config import settings
from .base import FetchResult

try:
    from curl_cffi.requests import AsyncSession  # type: ignore
    HAS_CURL = True
except Exception:  # pragma: no cover
    HAS_CURL = False

IMPERSONATE = ["chrome131", "chrome124", "safari17_0", "edge101"]


def default_headers(lang: str) -> dict[str, str]:
    h = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": lang,
        "Upgrade-Insecure-Requests": "1",
    }
    if not HAS_CURL:
        h["User-Agent"] = settings.user_agent
    return h


async def fetch_http(
    url: str,
    *,
    headers: dict | None = None,
    cookies: list[dict] | None = None,
    proxy: str | None = None,
    timeout_ms: int = 20000,
    verify: bool = True,
    lang: str | None = None,
    impersonate_idx: int = 0,
) -> FetchResult:
    t0 = time.perf_counter()
    h = default_headers(lang or "es-ES,es;q=0.9,en;q=0.8")
    h.update(headers or {})
    jar = {c["name"]: c["value"] for c in (cookies or []) if "name" in c and "value" in c}
    timeout = max(timeout_ms, 1000) / 1000
    try:
        if HAS_CURL:
            imp = IMPERSONATE[impersonate_idx % len(IMPERSONATE)]
            async with AsyncSession(impersonate=imp, proxy=proxy, verify=verify, timeout=timeout) as s:
                r = await s.get(url, headers=h, cookies=jar, allow_redirects=True, max_redirects=10)
                content, status, final_url = r.content, r.status_code, str(r.url)
                rh = {k: v for k, v in r.headers.items()}
            engine = f"http:{imp}"
        else:
            async with httpx.AsyncClient(follow_redirects=True, proxy=proxy, verify=verify, timeout=timeout) as c:
                r = await c.get(url, headers=h, cookies=jar)
                content, status, final_url = r.content, r.status_code, str(r.url)
                rh = dict(r.headers)
            engine = "http:httpx"
    except Exception as e:  # red, DNS, TLS, timeout
        return FetchResult(url=url, final_url=url, status=0, engine="http", error=f"{type(e).__name__}: {e}",
                           elapsed_ms=int((time.perf_counter() - t0) * 1000))
    ct = next((v for k, v in rh.items() if k.lower() == "content-type"), "")
    html = ""
    if any(x in ct for x in ("html", "xml", "text", "json")) or not ct:
        html = decode(content, ct)
    return FetchResult(
        url=url, final_url=final_url, status=status, html=html, content=content, content_type=ct,
        headers=rh, engine=engine, elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )


def decode(content: bytes, ct: str) -> str:
    """Decodifica respetando el charset de cabecera o <meta> (Firecrawl asume UTF-8, TODO en su código)."""
    charset = None
    m = re.search(r"charset=([\w-]+)", ct or "", re.I)
    if m:
        charset = m.group(1)
    if not charset:
        m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', content[:4096], re.I)
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    for enc in filter(None, [charset, "utf-8", "cp1252", "latin-1"]):
        try:
            return content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", "replace")
