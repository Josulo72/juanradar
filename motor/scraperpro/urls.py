"""Normalización de URLs, robots.txt y sitemaps."""
from __future__ import annotations

import gzip
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

from lxml import etree

from .engines.http import fetch_http

TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|_hsenc|_hsmi|igshid|yclid|ref_src|spm)$", re.I)


def normalize(url: str, *, ignore_query: bool = False) -> str:
    """URL canónica para deduplicar: host en minúsculas, sin fragmento, sin parámetros de rastreo,
    parámetros ordenados, sin index.html/barra final redundante."""
    p = urlparse(url.strip())
    scheme = (p.scheme or "https").lower()
    host = (p.hostname or "").lower()
    port = f":{p.port}" if p.port and not ((scheme == "http" and p.port == 80) or (scheme == "https" and p.port == 443)) else ""
    path = re.sub(r"/{2,}", "/", p.path or "/")
    path = re.sub(r"/(index|default)\.(html?|php|aspx?)$", "/", path, flags=re.I)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = ""
    if not ignore_query and p.query:
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)]
        query = urlencode(sorted(q))
    frag = p.fragment if p.fragment.startswith(("/", "!")) else ""  # rutas SPA con #/ o #!
    return urlunparse((scheme, host + port, path, "", query, frag))


def clean_url(url: str, *, ignore_query: bool = False) -> str:
    """URL descargable: sin fragmento ni parámetros de rastreo, pero con su ruta tal cual
    (normalize() altera la ruta y solo sirve como clave de deduplicado)."""
    p = urlparse(url.strip())
    query = ""
    if not ignore_query and p.query:
        query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)])
    frag = p.fragment if p.fragment.startswith(("/", "!")) else ""
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.params, query, frag))


def dedupe_key(url: str, ignore_query: bool = False) -> str:
    """Clave que iguala http/https y www/no-www (las "permutaciones" de Firecrawl)."""
    n = normalize(url, ignore_query=ignore_query)
    p = urlparse(n)
    host = (p.hostname or "").removeprefix("www.")
    return f"{host}{p.path}{'?' + p.query if p.query else ''}{'#' + p.fragment if p.fragment else ''}"


def pattern_key(url: str) -> str:
    """Patrón de URL (números e ids sustituidos) para limitar páginas por patrón (anti-trampas)."""
    p = urlparse(url)
    path = re.sub(r"\d+", "N", p.path)
    path = re.sub(r"[0-9a-f]{8,}", "H", path)
    keys = ",".join(sorted(k for k, _ in parse_qsl(p.query)))
    return f"{p.hostname}{path}?{keys}"


def same_site(url: str, base: str, allow_subdomains: bool) -> bool:
    a = (urlparse(url).hostname or "").lower().removeprefix("www.")
    b = (urlparse(base).hostname or "").lower().removeprefix("www.")
    if a == b:
        return True
    return allow_subdomains and (a.endswith("." + b) or b.endswith("." + a))


SKIP_EXT = re.compile(
    r"\.(jpg|jpeg|png|gif|webp|svg|ico|bmp|tiff?|mp3|mp4|avi|mov|wmv|flv|webm|ogg|wav|zip|rar|7z|gz|tar|exe|dmg|msi|"
    r"apk|iso|bin|css|js|woff2?|ttf|eot|otf|json|rss|atom)(\?|$)",
    re.I,
)


class Robots:
    def __init__(self, base_url: str, user_agent: str = "ScraperPro"):
        self.base = base_url
        self.ua = user_agent
        self.parser = RobotFileParser()
        self.sitemaps: list[str] = []
        self.crawl_delay: float | None = None
        self.loaded = False

    async def load(self) -> None:
        p = urlparse(self.base)
        robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
        r = await fetch_http(robots_url, timeout_ms=10000)
        text = r.html if r.status == 200 and r.html and "<html" not in r.html[:500].lower() else ""
        self.parser.parse(text.splitlines())
        self.sitemaps = [ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.lower().startswith("sitemap:")]
        try:
            d = self.parser.crawl_delay(self.ua) or self.parser.crawl_delay("*")
            self.crawl_delay = float(d) if d else None
        except Exception:
            self.crawl_delay = None
        self.loaded = True

    def allowed(self, url: str) -> bool:
        if not self.loaded:
            return True
        try:
            return self.parser.can_fetch(self.ua, url) and self.parser.can_fetch("*", url)
        except Exception:
            return True


async def sitemap_urls(base_url: str, extra: list[str] | None = None, max_sitemaps: int = 50, max_urls: int = 100_000) -> list[dict]:
    """Lee sitemaps (índices anidados, .gz, .txt). Devuelve [{url, lastmod, priority}]."""
    p = urlparse(base_url)
    root = f"{p.scheme}://{p.netloc}"
    queue = list(dict.fromkeys((extra or []) + [f"{root}/sitemap.xml", f"{root}/sitemap_index.xml"]))
    seen: set[str] = set()
    out: list[dict] = []
    while queue and len(seen) < max_sitemaps and len(out) < max_urls:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        r = await fetch_http(sm, timeout_ms=20000)
        if r.status != 200 or not r.content:
            continue
        data = r.content
        if data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except OSError:
                continue
        if sm.endswith(".txt"):
            out.extend({"url": ln.strip()} for ln in data.decode("utf-8", "replace").splitlines() if ln.strip().startswith("http"))
            continue
        try:
            tree = etree.fromstring(data, parser=etree.XMLParser(recover=True, huge_tree=True))
        except etree.XMLSyntaxError:
            continue
        if tree is None:
            continue
        for el in tree.iter():
            tag = etree.QName(el).localname if isinstance(el.tag, str) else ""
            if tag == "sitemap":
                loc = _child(el, "loc")
                if loc:
                    queue.append(urljoin(sm, loc))
            elif tag == "url":
                loc = _child(el, "loc")
                if loc:
                    pr = _child(el, "priority")
                    out.append({"url": urljoin(sm, loc), "lastmod": _child(el, "lastmod"),
                                "priority": float(pr) if pr and re.match(r"^[\d.]+$", pr) else None})
    return out[:max_urls]


def _child(el, name: str) -> str | None:
    for c in el:
        if isinstance(c.tag, str) and etree.QName(c).localname == name and c.text:
            return c.text.strip()
    return None
