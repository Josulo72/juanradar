"""Selección de motor con escalado automático y aprendizaje por dominio.

Escalera: http (huella TLS de Chrome) → navegador → navegador stealth + proxy rotativo.
- Mejoras frente a Firecrawl:
  * detecta bloqueos servidos con 200 (CAPTCHA, Cloudflare) y escala también en ese caso;
  * recuerda por dominio qué escalón funcionó y empieza ahí la próxima vez (ahorra tiempo);
  * si todo falla, devuelve el mejor resultado parcial con el motivo, en lugar de solo un error.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from .. import db
from ..config import settings
from .base import FetchResult
from .browser import fetch_browser
from .detect import detect_block, needs_javascript
from .http import fetch_http
from .proxies import mask, pool

LADDER = ["http", "browser", "stealth"]


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _needs_browser(opts: dict) -> bool:
    return bool(
        opts.get("actions") or opts.get("waitFor") or opts.get("waitForSelector") or opts.get("mobile")
        or opts.get("captureNetwork") or opts.get("javascript") or opts.get("profile")
        or any(_fmt_name(f) == "screenshot" for f in opts.get("formats", []))
    )


def _fmt_name(f) -> str:
    return f if isinstance(f, str) else (f.get("type") or "")


def _screenshot_opts(opts: dict) -> dict | None:
    for f in opts.get("formats", []):
        if _fmt_name(f) == "screenshot":
            return f if isinstance(f, dict) else {"fullPage": False}
        if f == "screenshot@fullPage":
            return {"fullPage": True}
    return None


async def fetch(url: str, opts: dict) -> FetchResult:
    timeout = int(opts.get("timeout") or settings.default_timeout_ms)
    engine_pref = opts.get("engine", "auto")
    proxy_mode = opts.get("proxy", "auto")
    domain = _domain(url)
    profile = db.get_domain_profile(domain)

    if engine_pref != "auto":
        ladder = LADDER[LADDER.index(engine_pref):]
    else:
        start = "browser" if _needs_browser(opts) else profile.get("engine", "http")
        ladder = LADDER[LADDER.index(start):] if start in LADDER else LADDER
        if _needs_browser(opts) and ladder[0] == "http":
            ladder = ladder[1:]
    if any(_fmt_name(f) == "screenshot@fullPage" for f in opts.get("formats", [])) and "http" in ladder:
        ladder = [s for s in ladder if s != "http"]

    attempts: list[dict] = []
    best: FetchResult | None = None
    loc = opts.get("location") or {}
    for step in ladder:
        proxy = None
        if proxy_mode in ("basic", "rotate") or (proxy_mode == "stealth") or (step == "stealth" and proxy_mode == "auto"):
            proxy = pool.next()
        if proxy_mode == "none":
            proxy = None
        if step == "http":
            r = await fetch_http(url, headers=opts.get("headers"), cookies=opts.get("cookies"), proxy=proxy,
                                 timeout_ms=min(timeout, 20000), verify=not opts.get("skipTlsVerification"),
                                 lang=",".join((loc.get("languages") or [])) or None)
        else:
            r = await fetch_browser(
                url, proxy=proxy, timeout_ms=timeout, wait_for_ms=int(opts.get("waitFor") or 0),
                wait_for_selector=opts.get("waitForSelector"), actions=opts.get("actions"),
                screenshot=_screenshot_opts(opts), mobile=bool(opts.get("mobile")), country=loc.get("country"),
                languages=loc.get("languages"), headers=opts.get("headers"), cookies=opts.get("cookies"),
                block_ads=bool(opts.get("blockAds", True)), block_resources=opts.get("blockResources"),
                capture_network=bool(opts.get("captureNetwork")), javascript=opts.get("javascript"),
                profile=opts.get("profile"), save_profile=bool(opts.get("profile")),
                ignore_https_errors=bool(opts.get("skipTlsVerification")), stealth=True,
                auto_scroll=step == "stealth" or bool(opts.get("autoScroll", True)),
                real_chrome=step == "stealth",
                challenge_wait_ms=20000 if step == "stealth" else 6000,
            )
            if step == "stealth":
                r.engine = "browser:stealth" + ("+proxy" if proxy else "")
        r.proxy_used = mask(proxy)
        if not r.error and not r.is_document:
            r.blocked = detect_block(r.status, r.html, r.headers)
        attempts.append({"engine": r.engine, "status": r.status, "ms": r.elapsed_ms,
                         "blocked": r.blocked, "error": r.error, "proxy": r.proxy_used})
        if r.blocked and proxy:
            pool.penalize(proxy)
        if r.ok and not r.is_document and step == "http" and needs_javascript(r.html):
            r.blocked = "needs_javascript"
            attempts[-1]["blocked"] = "needs_javascript"
        if r.ok:
            r.attempts = attempts
            if engine_pref == "auto" and not _needs_browser(opts):
                _learn(domain, profile, step, True)
            return r
        if best is None or _score(r) > _score(best):
            best = r
        if r.status == 404 and not r.blocked:
            break  # una 404 real no mejora cambiando de motor
        await asyncio.sleep(0)
    assert best is not None
    best.attempts = attempts
    _learn(domain, profile, ladder[-1], False)
    if not best.error and best.blocked:
        best.error = f"bloqueado ({best.blocked}) tras {len(attempts)} intentos"
    return best


def _score(r: FetchResult) -> int:
    return (0 if r.error else 2) + (0 if r.blocked else 2) + min(len(r.html) // 5000, 3)


def _learn(domain: str, profile: dict, step: str, ok: bool) -> None:
    if not domain:
        return
    p = dict(profile)
    if ok:
        p["engine"] = step
        p["ok"] = int(p.get("ok", 0)) + 1
    else:
        p["fail"] = int(p.get("fail", 0)) + 1
    db.save_domain_profile(domain, p)
