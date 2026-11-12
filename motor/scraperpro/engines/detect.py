"""Clasificadores: página de bloqueo/antibot y página que necesita JavaScript.

Mejora sobre Firecrawl: allí el éxito es "markdown no vacío", así que un CAPTCHA servido con 200
cuenta como éxito. Aquí se detecta y se escala de motor/proxy.
"""
from __future__ import annotations

import re

_BLOCK_MARKERS: list[tuple[str, str]] = [
    ("cloudflare", r"just a moment\.\.\.|cf-browser-verification|challenges\.cloudflare\.com|cf-chl-|attention required! \| cloudflare|_cf_chl_opt"),
    ("datadome", r"geo\.captcha-delivery\.com|datadome"),
    ("perimeterx", r"px-captcha|perimeterx|_pxhd"),
    ("akamai", r"akamai.{0,40}(bot|denied)"),
    ("imperva", r"incapsula|_incap_"),
    ("captcha", r"g-recaptcha|h-captcha|hcaptcha\.com|recaptcha/api|are you a robot|verify you are (a )?human|"
                r"prove you'?re not a robot|confirma que no eres un robot|unusual traffic"),
    ("access_denied", r"<title>\s*(access denied|403 forbidden|forbidden|error 1020|request blocked)\s*</title>"),
    ("rate_limited", r"too many requests|rate limit exceeded|demasiadas solicitudes"),
]
_BLOCK_RE = [(name, re.compile(rx, re.I)) for name, rx in _BLOCK_MARKERS]

_JS_REQUIRED_RE = re.compile(
    r"(please enable javascript|you need to enable javascript|this app works best with javascript|"
    r"habilita javascript|activa javascript|<noscript>[^<]{0,200}javascript)",
    re.I,
)
_SPA_ROOT_RE = re.compile(
    r'<div[^>]+id=["\'](root|app|__next|__nuxt|svelte|main-app)["\'][^>]*>\s*(</div>|<!--)', re.I
)
_TAG_RE = re.compile(r"<script\b.*?</script>|<style\b.*?</style>|<[^>]+>", re.I | re.S)


def visible_text_len(html: str) -> int:
    return len(re.sub(r"\s+", " ", _TAG_RE.sub(" ", html or "")).strip())


def detect_block(status: int, html: str, headers: dict | None = None) -> str | None:
    """Devuelve el tipo de bloqueo o None."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    sample = (html or "")[:60000]
    text_len = visible_text_len(sample)
    for name, rx in _BLOCK_RE:
        if rx.search(sample):
            # Una página real con mucho contenido puede mencionar "captcha" o cargar un script antibot
            if name in ("captcha", "rate_limited") and text_len > 3000:
                continue
            if name in ("datadome", "imperva", "akamai", "perimeterx") and text_len > 5000:
                continue
            return name
    if status in (401, 403, 407, 429, 503) and text_len < 2000:
        if "cf-ray" in headers or "cloudflare" in headers.get("server", "").lower():
            return "cloudflare"
        return "rate_limited" if status == 429 else f"http_{status}"
    return None


def needs_javascript(html: str) -> bool:
    """Heurística: la respuesta HTTP no trae el contenido y hay que renderizar."""
    if not html:
        return True
    text_len = visible_text_len(html)
    scripts = len(re.findall(r"<script\b", html, re.I))
    if _JS_REQUIRED_RE.search(html) and text_len < 1500:
        return True
    if _SPA_ROOT_RE.search(html) and text_len < 800:
        return True
    if scripts >= 8 and text_len < 400:
        return True
    if text_len < 250:
        return True
    # Datos incrustados en scripts que el JS pinta después (p. ej. "var data = [...]")
    inline_js = sum(len(m) for m in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.I | re.S))
    if text_len < 1500 and inline_js > 2 * text_len and re.search(r"(document\.write|innerHTML|appendChild|createElement|render\()", html):
        return True
    return False
