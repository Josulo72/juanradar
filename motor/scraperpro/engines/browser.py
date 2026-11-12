"""Motor de navegador (Playwright/Chromium) con stealth, acciones, captura de red y perfiles.

Sustituye a fire-engine (CDP de pago) y supera al playwright-service del self-host de Firecrawl, que
solo devuelve HTML: aquí hay acciones, capturas, PDF, móvil, geolocalización, proxies por contexto,
perfiles persistentes y captura de las respuestas JSON (APIs internas de la web).
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from ..config import settings
from .base import FetchResult
from .proxies import playwright_proxy

# Huella coherente: se oculta webdriver, se rellenan plugins, idiomas, chrome.runtime, WebGL y permisos.
STEALTH_JS = r"""
(() => {
  const def = (o, p, v) => { try { Object.defineProperty(o, p, { get: () => v, configurable: true }); } catch (e) {} };
  def(Navigator.prototype, 'webdriver', undefined);
  if (!window.chrome) { window.chrome = { runtime: {}, loadTimes: () => ({}), csi: () => ({}), app: { isInstalled: false } }; }
  const langs = (window.__sp_langs || ['es-ES', 'es', 'en']);
  def(Navigator.prototype, 'languages', langs);
  def(Navigator.prototype, 'plugins', [
    { name: 'PDF Viewer', filename: 'internal-pdf-viewer' }, { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer' },
    { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer' }, { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer' },
    { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer' }]);
  def(Navigator.prototype, 'hardwareConcurrency', 8);
  def(Navigator.prototype, 'deviceMemory', 8);
  const q = window.navigator.permissions && window.navigator.permissions.query;
  if (q) { window.navigator.permissions.query = (p) => p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission }) : q.call(window.navigator.permissions, p); }
  const gp = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (p) {
    if (p === 37445) return 'Google Inc. (Intel)';
    if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';
    return gp.call(this, p);
  };
  try { delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array; delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise; } catch (e) {}
})();
"""

# Dominios de anuncios y rastreo más frecuentes (Firecrawl self-host bloquea 13; aquí una lista amplia + patrones)
AD_PATTERNS = re.compile(
    r"(doubleclick\.net|googlesyndication\.com|google-analytics\.com|googletagmanager\.com|googletagservices\.com|"
    r"adservice\.google\.|facebook\.net/.*/fbevents|connect\.facebook\.net|scorecardresearch\.com|quantserve\.com|"
    r"adnxs\.com|criteo\.(com|net)|taboola\.com|outbrain\.com|amazon-adsystem\.com|hotjar\.com|clarity\.ms|"
    r"segment\.(io|com)|mixpanel\.com|newrelic\.com|nr-data\.net|optimizely\.com|pubmatic\.com|rubiconproject\.com|"
    r"openx\.net|casalemedia\.com|moatads\.com|adsrvr\.org|yieldmo\.com|teads\.tv|smartadserver\.com|"
    r"tiktok\.com/i18n/pixel|snap\.licdn\.com|ads\.linkedin\.com|bat\.bing\.com|cookielaw\.org|onetrust\.com|"
    r"cookiebot\.com|consensu\.org|trustarc\.com|didomi\.io|quantcast\.com)",
    re.I,
)

COUNTRY = {
    "ES": ("es-ES", "Europe/Madrid", (40.4168, -3.7038)), "US": ("en-US", "America/New_York", (40.7128, -74.006)),
    "GB": ("en-GB", "Europe/London", (51.5072, -0.1276)), "FR": ("fr-FR", "Europe/Paris", (48.8566, 2.3522)),
    "DE": ("de-DE", "Europe/Berlin", (52.52, 13.405)), "IT": ("it-IT", "Europe/Rome", (41.9028, 12.4964)),
    "PT": ("pt-PT", "Europe/Lisbon", (38.7223, -9.1393)), "MX": ("es-MX", "America/Mexico_City", (19.4326, -99.1332)),
    "AR": ("es-AR", "America/Argentina/Buenos_Aires", (-34.6037, -58.3816)), "BR": ("pt-BR", "America/Sao_Paulo", (-23.5505, -46.6333)),
    "JP": ("ja-JP", "Asia/Tokyo", (35.6762, 139.6503)), "IN": ("en-IN", "Asia/Kolkata", (28.6139, 77.209)),
    "CA": ("en-CA", "America/Toronto", (43.6532, -79.3832)), "AU": ("en-AU", "Australia/Sydney", (-33.8688, 151.2093)),
    "NL": ("nl-NL", "Europe/Amsterdam", (52.3676, 4.9041)),
}

CONSENT_SELECTORS = [
    "#onetrust-accept-btn-handler", "button#didomi-notice-agree-button", "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "button[aria-label*='Accept' i]", "button[aria-label*='Aceptar' i]", "button:has-text('Aceptar todo')",
    "button:has-text('Aceptar todas')", "button:has-text('Accept all')", "button:has-text('Accept All')",
    "button:has-text('Aceptar')", "button:has-text('Acepto')", "button:has-text('Agree')", "button:has-text('I agree')",
    "button:has-text('Rechazar todo')", "button:has-text('Reject all')",
]


class BrowserManager:
    """Un Chromium compartido; cada petición usa un contexto aislado (proxy, idioma, móvil, perfil)."""

    def __init__(self) -> None:
        self._pw = None
        self._browsers: dict[str, Any] = {}
        self._lock: asyncio.Lock | None = None
        self._sem: asyncio.Semaphore | None = None
        self._loop = None

    def _ensure_loop_objects(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:  # nuevo bucle (tests, CLI): objetos asyncio nuevos
            self._loop = loop
            self._lock = asyncio.Lock()
            self._sem = asyncio.Semaphore(max(1, settings.browser_pool_size))
            self._browsers = {}
            self._pw = None

    @property
    def sem(self) -> asyncio.Semaphore:
        self._ensure_loop_objects()
        assert self._sem is not None
        return self._sem

    async def browser(self, proxied: bool = False, real_chrome: bool = False):
        """Instancias: 'direct' (sin proxy), 'proxied' (proxy por contexto) y sus variantes con el
        Chrome instalado en el sistema (huella más realista para el modo stealth)."""
        self._ensure_loop_objects()
        assert self._lock is not None
        key = ("proxied" if proxied else "direct") + (":chrome" if real_chrome else "")
        async with self._lock:
            b = self._browsers.get(key)
            if b is None or not b.is_connected():
                from playwright.async_api import async_playwright

                self._pw = self._pw or await async_playwright().start()
                kw: dict[str, Any] = dict(
                    headless=settings.headless,
                    args=["--disable-blink-features=AutomationControlled", "--no-default-browser-check",
                          "--disable-dev-shm-usage", "--disable-features=Translate"],
                )
                if proxied:
                    kw["proxy"] = {"server": "http://per-context"}
                b = None
                if real_chrome:
                    try:
                        b = await self._pw.chromium.launch(channel="chrome", **kw)
                    except Exception:
                        b = None  # sin Chrome instalado: Chromium del proyecto
                if b is None:
                    b = await self._pw.chromium.launch(**kw)
                self._browsers[key] = b
            return b

    async def close(self) -> None:
        if self._lock is None:
            return
        async with self._lock:
            for b in self._browsers.values():
                try:
                    await b.close()
                except Exception:
                    pass
            self._browsers = {}
            if self._pw:
                await self._pw.stop()
                self._pw = None

    async def new_context(self, *, proxy: str | None = None, mobile: bool = False, country: str | None = None,
                          languages: list[str] | None = None, headers: dict | None = None,
                          cookies: list[dict] | None = None, profile: str | None = None,
                          ignore_https_errors: bool = False, stealth: bool = True, real_chrome: bool = False):
        b = await self.browser(proxied=bool(proxy), real_chrome=real_chrome)
        loc, tz, geo = COUNTRY.get((country or "ES").upper(), COUNTRY["ES"])
        if languages:
            loc = languages[0]
        kw: dict[str, Any] = dict(
            locale=loc, timezone_id=tz, geolocation={"latitude": geo[0], "longitude": geo[1]},
            permissions=["geolocation"], ignore_https_errors=ignore_https_errors,
            extra_http_headers={k: v for k, v in (headers or {}).items() if k.lower() != "user-agent"},
        )
        if proxy:
            kw["proxy"] = playwright_proxy(proxy)
        if mobile and self._pw:
            dev = dict(self._pw.devices["Pixel 7"])  # Chromium: mejor un Android que un iPhone
            dev.pop("default_browser_type", None)
            kw.update(dev)
        else:
            kw.update(viewport={"width": 1366, "height": 900}, user_agent=(headers or {}).get("User-Agent", settings.user_agent),
                      device_scale_factor=1)
        state = profile_path(profile) if profile else None
        if state and state.exists():
            kw["storage_state"] = str(state)
        ctx = await b.new_context(**kw)
        if stealth:
            langs = json.dumps(languages or [loc, loc.split("-")[0], "en"])
            await ctx.add_init_script(f"window.__sp_langs = {langs};" + STEALTH_JS)
        if cookies:
            await ctx.add_cookies([_cookie(c) for c in cookies])
        return ctx


def profile_path(name: str) -> Path:
    safe = re.sub(r"[^\w.-]", "_", name)[:80]
    p = settings.data_dir / "profiles"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{safe}.json"


def _cookie(c: dict) -> dict:
    out = {k: v for k, v in c.items() if k in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite", "url")}
    if "domain" not in out and "url" not in out:
        raise ValueError("Cada cookie necesita 'domain' o 'url'")
    out.setdefault("path", "/") if "domain" in out else None
    return out


manager = BrowserManager()


async def fetch_browser(
    url: str,
    *,
    proxy: str | None = None,
    timeout_ms: int = 30000,
    wait_for_ms: int = 0,
    wait_for_selector: str | None = None,
    actions: list[dict] | None = None,
    screenshot: dict | None = None,
    mobile: bool = False,
    country: str | None = None,
    languages: list[str] | None = None,
    headers: dict | None = None,
    cookies: list[dict] | None = None,
    block_ads: bool = True,
    block_resources: list[str] | None = None,
    capture_network: bool = False,
    javascript: str | None = None,
    profile: str | None = None,
    save_profile: bool = False,
    ignore_https_errors: bool = False,
    stealth: bool = True,
    auto_scroll: bool = True,
    dismiss_consent: bool = True,
    real_chrome: bool = False,
    challenge_wait_ms: int = 0,
) -> FetchResult:
    t0 = time.perf_counter()
    network: list[dict] = []
    main_status = {"status": 0, "headers": {}, "ct": ""}
    async with manager.sem:
        try:
            ctx = await manager.new_context(proxy=proxy, mobile=mobile, country=country, languages=languages,
                                            headers=headers, cookies=cookies, profile=profile,
                                            ignore_https_errors=ignore_https_errors, stealth=stealth,
                                            real_chrome=real_chrome)
        except Exception as e:
            return FetchResult(url=url, final_url=url, status=0, engine="browser", error=f"contexto: {e}")
        page = await ctx.new_page()
        page.set_default_timeout(min(timeout_ms, 30000))
        blocked_types = set(block_resources or [])

        async def _route(route):
            req = route.request
            if block_ads and AD_PATTERNS.search(req.url):
                return await route.abort()
            if req.resource_type in blocked_types:
                return await route.abort()
            return await route.continue_()

        if block_ads or blocked_types:
            await page.route("**/*", _route)

        async def _on_response(resp):
            try:
                if resp.request.resource_type in ("xhr", "fetch"):
                    ct = resp.headers.get("content-type", "")
                    entry = {"url": resp.url, "method": resp.request.method, "status": resp.status, "contentType": ct}
                    if "json" in ct and len(network) < 200:
                        body = await resp.body()
                        if len(body) < 2_000_000:
                            try:
                                entry["json"] = json.loads(body)
                            except ValueError:
                                entry["text"] = body[:5000].decode("utf-8", "replace")
                    network.append(entry)
            except Exception:
                pass

        if capture_network:
            page.on("response", lambda r: asyncio.ensure_future(_on_response(r)))

        result = FetchResult(url=url, final_url=url, status=0, engine="browser")
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if resp:
                main_status["status"] = resp.status
                main_status["headers"] = await resp.all_headers()
                main_status["ct"] = main_status["headers"].get("content-type", "")
                if "pdf" in main_status["ct"] or "octet-stream" in main_status["ct"]:
                    body = await resp.body()
                    result.status, result.content, result.content_type = resp.status, body, main_status["ct"]
                    result.final_url = page.url
                    return result
            await smart_wait(page, timeout_ms=min(8000, timeout_ms // 2))
            if challenge_wait_ms:
                await _wait_challenge(page, challenge_wait_ms)
            if dismiss_consent:
                await _dismiss_consent(page)
            if wait_for_selector:
                await page.wait_for_selector(wait_for_selector, timeout=min(timeout_ms, 20000))
            if wait_for_ms:
                await page.wait_for_timeout(min(wait_for_ms, 60000))
            if auto_scroll and not actions:
                await _auto_scroll(page)
            if actions:
                result.actions = await run_actions(page, actions, timeout_ms=timeout_ms)
            if javascript:
                result.js_return = await page.evaluate(javascript)
            if screenshot is not None:
                png = await page.screenshot(full_page=bool(screenshot.get("fullPage")), type="png")
                result.screenshot = base64.b64encode(png).decode()
            result.html = await page.content()
            result.final_url = page.url
            result.status = main_status["status"] or 200
            result.headers = main_status["headers"]
            result.content_type = main_status["ct"] or "text/html"
            if save_profile and profile:
                await ctx.storage_state(path=str(profile_path(profile)))
        except Exception as e:
            result.error = f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"
            try:
                result.html = await page.content()
                result.final_url = page.url
                result.status = main_status["status"]
            except Exception:
                pass
        finally:
            result.network = network
            result.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            await ctx.close()
        return result


async def smart_wait(page, timeout_ms: int = 8000) -> None:
    """Espera inteligente: red inactiva + DOM estable (sin esperas fijas como Maxun)."""
    deadline = time.perf_counter() + timeout_ms / 1000
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    last = -1
    stable = 0
    while time.perf_counter() < deadline:
        try:
            n = await page.evaluate("document.body ? document.body.innerText.length : 0")
        except Exception:
            break
        if n == last and n > 0:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last = n
        await page.wait_for_timeout(300)


async def _wait_challenge(page, max_ms: int) -> None:
    """Retos JavaScript de 'comprobando tu navegador' (p. ej. Cloudflare) se resuelven solos en un
    navegador real tras unos segundos; aquí solo se espera y se mueve el ratón como un humano.
    No resuelve CAPTCHAs interactivos."""
    from .detect import detect_block

    deadline = time.perf_counter() + max_ms / 1000
    while time.perf_counter() < deadline:
        try:
            html = await page.content()
        except Exception:
            await page.wait_for_timeout(500)
            continue
        if not detect_block(200, html):
            return
        try:
            await page.mouse.move(200 + int(time.perf_counter() * 37) % 400, 300 + int(time.perf_counter() * 53) % 200, steps=8)
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=3000)
    except Exception:
        pass


async def _dismiss_consent(page) -> None:
    for sel in CONSENT_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click(timeout=1500)
                await page.wait_for_timeout(300)
                return
        except Exception:
            continue


async def _auto_scroll(page, max_steps: int = 12) -> None:
    """Desplaza para disparar la carga diferida (lazy load) y vuelve arriba."""
    try:
        prev = 0
        for _ in range(max_steps):
            h = await page.evaluate("document.body ? document.body.scrollHeight : 0")
            await page.evaluate("window.scrollBy(0, Math.max(window.innerHeight, 800))")
            await page.wait_for_timeout(250)
            y = await page.evaluate("window.scrollY + window.innerHeight")
            if y >= h and h == prev:
                break
            prev = h
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


async def run_actions(page, actions: list[dict], timeout_ms: int = 30000) -> dict:
    """Acciones compatibles con Firecrawl + extras (select, hover, goto, waitForNavigation, fill)."""
    out: dict[str, list] = {"screenshots": [], "scrapes": [], "javascriptReturns": [], "pdfs": []}
    for i, a in enumerate(actions[:100]):
        t = a.get("type")
        sel = a.get("selector")
        try:
            if t == "wait":
                if sel:
                    await page.wait_for_selector(sel, timeout=a.get("timeout", timeout_ms))
                else:
                    await page.wait_for_timeout(min(int(a.get("milliseconds", 1000)), 60000))
            elif t == "click":
                if a.get("all"):
                    for el in await page.query_selector_all(sel):
                        await el.click()
                else:
                    await _robust_click(page, sel, a.get("text"))
                await smart_wait(page, 4000)
            elif t in ("write", "type"):
                if sel:
                    await page.fill(sel, str(a.get("text", "")))
                else:
                    await page.keyboard.type(str(a.get("text", "")), delay=a.get("delay", 30))
            elif t == "fill":
                await page.fill(sel, str(a.get("value", a.get("text", ""))))
            elif t == "press":
                await page.keyboard.press(a.get("key", "Enter"))
                await smart_wait(page, 4000)
            elif t == "select":
                await page.select_option(sel, a.get("value"))
            elif t == "hover":
                await page.hover(sel)
            elif t == "scroll":
                d = -1 if a.get("direction") == "up" else 1
                amount = int(a.get("amount", 0)) or None
                if sel:
                    await page.eval_on_selector(sel, f"(el) => el.scrollBy(0, {d} * (el.clientHeight || 800))")
                else:
                    await page.evaluate(f"window.scrollBy(0, {d} * {amount or 'window.innerHeight'})")
                await page.wait_for_timeout(400)
            elif t == "goto":
                await page.goto(a["url"], wait_until="domcontentloaded")
                await smart_wait(page, 6000)
            elif t == "screenshot":
                png = await page.screenshot(full_page=bool(a.get("fullPage")), type="png")
                out["screenshots"].append(base64.b64encode(png).decode())
            elif t == "scrape":
                out["scrapes"].append({"url": page.url, "html": await page.content()})
            elif t == "executeJavascript":
                out["javascriptReturns"].append({"type": "result", "value": await page.evaluate(a.get("script", ""))})
            elif t == "pdf":
                pdf = await page.pdf(landscape=bool(a.get("landscape")), scale=float(a.get("scale", 1)),
                                     format=a.get("format", "A4"))
                out["pdfs"].append(base64.b64encode(pdf).decode())
            else:
                raise ValueError(f"acción desconocida: {t}")
        except Exception as e:
            if a.get("optional"):
                continue
            raise RuntimeError(f"acción {i} ({t}) falló: {str(e).splitlines()[0][:200]}") from e
    return out


async def _robust_click(page, selector: Optional[str], text: Optional[str] = None) -> None:
    """Click con autorreparación: selector → texto visible → rol accesible."""
    errors = []
    if selector:
        try:
            await page.click(selector, timeout=8000)
            return
        except Exception as e:
            errors.append(str(e).splitlines()[0])
    if text:
        for loc in (page.get_by_role("button", name=text), page.get_by_role("link", name=text), page.get_by_text(text, exact=False)):
            try:
                await loc.first.click(timeout=4000)
                return
            except Exception as e:
                errors.append(str(e).splitlines()[0])
    if selector:
        try:
            await page.click(selector, timeout=4000, force=True)
            return
        except Exception as e:
            errors.append(str(e).splitlines()[0])
    raise RuntimeError("; ".join(errors) or "sin selector ni texto")
