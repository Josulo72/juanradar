"""Map (descubrir URLs de un sitio) y Search (buscador web con varios proveedores).

Map: Firecrawl usa índice propio + búsqueda `site:` + sitemap, y NO sigue enlaces; en self-host solo
queda el sitemap. Aquí: sitemap + robots + enlaces de la portada a N niveles + ordenación por
relevancia con `search` (BM25 simple sobre URL y título).
Search: Firecrawl self-host solo tiene DuckDuckGo por scraping. Aquí: SearXNG propio (si se
configura SP_SEARXNG_URL) → DuckDuckGo HTML → Bing HTML, con filtros de dominio reales.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
from collections import Counter
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from .engines.http import fetch_http
from .scrape import scrape_url, validate_url
from .transform.clean import parse
from .transform.meta import extract_links
from .urls import SKIP_EXT, Robots, clean_url, dedupe_key, same_site, sitemap_urls


def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9áéíóúñü]+", unquote(s).lower()) if len(t) > 1]


def rank(items: list[dict], query: str) -> list[dict]:
    q = _tokens(query)
    if not q:
        return items
    docs = [_tokens(i["url"] + " " + (i.get("title") or "") + " " + (i.get("description") or "")) for i in items]
    n = len(docs) or 1
    df = Counter(t for d in docs for t in set(d))
    avg = sum(len(d) for d in docs) / n or 1
    scored = []
    for item, d in zip(items, docs):
        tf = Counter(d)
        s = 0.0
        for t in q:
            if t in tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(d) / avg))
            elif any(t in x for x in d):
                s += 0.3
        scored.append((s, item))
    scored.sort(key=lambda x: -x[0])
    return [dict(i, relevance=round(s, 3)) for s, i in scored]


async def map_site(req: dict) -> list[dict]:
    base = validate_url(req["url"])
    limit = int(req.get("limit") or 5000)
    subdomains = bool(req.get("includeSubdomains", True))
    ignore_q = bool(req.get("ignoreQueryParameters", True))
    seen: dict[str, dict] = {}

    def add(u: str, **extra) -> None:
        if not u.startswith(("http://", "https://")) or SKIP_EXT.search(u) or not same_site(u, base, subdomains):
            return
        k = dedupe_key(u, ignore_q)
        if k not in seen:
            seen[k] = {"url": clean_url(u, ignore_query=ignore_q), **{a: b for a, b in extra.items() if b}}

    robots = Robots(base)
    try:
        await robots.load()
    except Exception:
        pass
    mode = req.get("sitemap", "include")
    tasks = []
    if mode != "skip":
        tasks.append(sitemap_urls(base, robots.sitemaps, max_urls=limit * 2))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for res in results:
        if isinstance(res, list):
            for it in res:
                add(it["url"], lastmod=it.get("lastmod"))
    if mode != "only":
        frontier = [base]
        for _level in range(max(1, int(req.get("depth", 1)))):
            nxt: list[str] = []
            pages = await asyncio.gather(*[fetch_http(u, timeout_ms=15000) for u in frontier[:30]], return_exceptions=True)
            for u, r in zip(frontier, pages):
                if isinstance(r, Exception) or not getattr(r, "html", ""):
                    continue
                add(r.final_url or u, title=_title(r.html))
                links, _ = extract_links(r.html, r.final_url or u)
                for ln in links:
                    before = len(seen)
                    add(ln)
                    if len(seen) > before:
                        nxt.append(ln)
            frontier = nxt
            if not frontier or len(seen) >= limit:
                break
    items = list(seen.values())
    if req.get("search"):
        items = [i for i in rank(items, req["search"]) if i.get("relevance", 0) > 0] or items
    return items[:limit]


def _title(html_str: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html_str or "", re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:200] if m else None


# ---------------------------------------------------------------- search
async def _searxng(query: str, limit: int, lang: str | None) -> list[dict]:
    base = os.environ.get("SP_SEARXNG_URL")
    if not base:
        return []
    r = await fetch_http(f"{base.rstrip('/')}/search?q={quote_plus(query)}&format=json&language={lang or 'es'}",
                         timeout_ms=15000)
    import json

    try:
        data = json.loads(r.html)
    except ValueError:
        return []
    return [{"url": x["url"], "title": x.get("title"), "description": x.get("content")} for x in data.get("results", [])][:limit]


async def _ddg(query: str, limit: int, lang: str | None) -> list[dict]:
    region = {"es": "es-es", "en": "us-en", "fr": "fr-fr", "de": "de-de", "it": "it-it", "pt": "pt-pt"}.get((lang or "es")[:2], "wt-wt")
    r = await fetch_http(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}&kl={region}", timeout_ms=15000)
    if r.status != 200 or not r.html:
        return []
    root = parse(r.html)
    out = []
    for res in root.cssselect("div.result"):
        a = res.cssselect("a.result__a")
        if not a:
            continue
        href = a[0].get("href", "")
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        if "duckduckgo.com/y.js" in href or not href.startswith("http"):
            continue
        snip = res.cssselect(".result__snippet")
        out.append({"url": href, "title": a[0].text_content().strip(),
                    "description": snip[0].text_content().strip() if snip else None})
        if len(out) >= limit:
            break
    return out


async def _bing(query: str, limit: int, lang: str | None) -> list[dict]:
    r = await fetch_http(f"https://www.bing.com/search?q={quote_plus(query)}&setlang={lang or 'es'}&count={min(limit * 2, 50)}",
                         timeout_ms=15000)
    if r.status != 200 or not r.html:
        return []
    root = parse(r.html)
    out = []
    for li in root.cssselect("li.b_algo"):
        a = li.cssselect("h2 a")
        if not a or not a[0].get("href", "").startswith("http"):
            continue
        p = li.cssselect(".b_caption p, p")
        out.append({"url": a[0].get("href"), "title": a[0].text_content().strip(),
                    "description": p[0].text_content().strip() if p else None})
        if len(out) >= limit:
            break
    return out


async def search(req: dict) -> dict:
    query = req["query"]
    limit = max(1, min(int(req.get("limit") or 5), 50))
    lang = req.get("lang")
    inc = [d.lower() for d in req.get("includeDomains") or []]
    exc = [d.lower() for d in req.get("excludeDomains") or []]
    q = query
    if inc and len(inc) == 1:
        q += f" site:{inc[0]}"
    results: list[dict] = []
    used = None
    for name, prov in (("searxng", _searxng), ("duckduckgo", _ddg), ("bing", _bing)):
        try:
            results = await prov(q, limit * 3, lang)
        except Exception:
            results = []
        results = _filter_domains(results, inc, exc)
        if results:
            used = name
            break
    results = results[:limit]
    if req.get("scrapeOptions") and results:
        opts = dict(req["scrapeOptions"])
        docs = await asyncio.gather(*[scrape_url(r["url"], opts) for r in results], return_exceptions=True)
        for r, d in zip(results, docs):
            if isinstance(d, dict):
                r.update({k: v for k, v in d.items() if k not in ("success",)})
    return {"success": True, "provider": used, "data": results}


def _filter_domains(results: list[dict], inc: list[str], exc: list[str]) -> list[dict]:
    out = []
    for r in results:
        host = (urlparse(r["url"]).hostname or "").lower()
        if inc and not any(host == d or host.endswith("." + d) for d in inc):
            continue
        if exc and any(host == d or host.endswith("." + d) for d in exc):
            continue
        out.append(r)
    return out
