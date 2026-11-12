"""Metadatos, datos estructurados (JSON-LD, microdatos), enlaces e imágenes.

Firecrawl (nube) devolvió solo 2 de ~45 enlaces en quotes.toscrape.com/js: aquí se recogen todos los
enlaces del documento completo (no del contenido limpio), clasificados, y se marca la paginación.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urldefrag, urljoin, urlparse

from .clean import absolutize, parse


def extract_metadata(html_str: str, url: str, status: int = 200, content_type: str = "") -> dict:
    root = parse(html_str)
    md: dict = {"sourceURL": url, "url": url, "statusCode": status}
    if content_type:
        md["contentType"] = content_type
    t = root.find(".//title")
    if t is not None and t.text_content().strip():
        md["title"] = re.sub(r"\s+", " ", t.text_content()).strip()
    h = root.find(".//html") if root.tag != "html" else root
    if h is not None and h.get("lang"):
        md["language"] = h.get("lang")
    for m in root.iter("meta"):
        key = m.get("property") or m.get("name") or m.get("itemprop") or m.get("http-equiv")
        val = m.get("content")
        if not key or val is None:
            continue
        key = key.strip()
        k = {"description": "description", "keywords": "keywords", "author": "author", "robots": "robots",
             "viewport": "viewport", "generator": "generator", "theme-color": "themeColor"}.get(key.lower(), key)
        if k in md:
            if isinstance(md[k], list):
                md[k].append(val)
            elif md[k] != val:
                md[k] = [md[k], val]
        else:
            md[k] = val
    for ln in root.iter("link"):
        rel = (ln.get("rel") or "").lower()
        href = ln.get("href")
        if not href:
            continue
        if rel == "canonical":
            md["canonical"] = urljoin(url, href)
        elif "icon" in rel and "favicon" not in md:
            md["favicon"] = urljoin(url, href)
        elif rel == "alternate" and ln.get("hreflang"):
            md.setdefault("alternates", {})[ln.get("hreflang")] = urljoin(url, href)
        elif rel in ("next", "prev"):
            md[f"{rel}Page"] = urljoin(url, href)
    sd = structured_data(root)
    if sd:
        md["structuredData"] = sd
    return md


def structured_data(root) -> list:
    out = []
    for s in root.iter("script"):
        if (s.get("type") or "").lower() == "application/ld+json":
            raw = (s.text or "").strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                try:
                    data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw.replace("\n", " ")))
                except ValueError:
                    continue
            out.extend(data if isinstance(data, list) else [data])
    # Microdatos (itemscope/itemprop) en un nivel
    for scope in root.xpath("//*[@itemscope and not(ancestor::*[@itemscope])]")[:50]:
        item = {"@type": (scope.get("itemtype") or "").rsplit("/", 1)[-1] or None}
        for prop in scope.xpath(".//*[@itemprop]")[:80]:
            name = prop.get("itemprop")
            val = prop.get("content") or prop.get("href") or prop.get("src") or re.sub(r"\s+", " ", prop.text_content()).strip()
            if name and val and name not in item:
                item[name] = val[:500]
        if len(item) > 1:
            out.append(item)
    return out


_PAG_RX = re.compile(r"(next|siguiente|sig\.|more|más|mas|load more|ver más|older|→|»|›)", re.I)


def extract_links(html_str: str, url: str) -> tuple[list[str], dict]:
    root = parse(html_str)
    absolutize(root, url)
    seen: set[str] = set()
    links: list[str] = []
    pagination: list[str] = []
    host = (urlparse(url).hostname or "").lower()
    internal = external = 0
    for a in root.iter("a", "area"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            continue
        href = urldefrag(href)[0]
        if not href.startswith(("http://", "https://")):
            continue
        if href not in seen:
            seen.add(href)
            links.append(href)
            if (urlparse(href).hostname or "").lower().removeprefix("www.") == host.removeprefix("www."):
                internal += 1
            else:
                external += 1
        txt = re.sub(r"\s+", " ", a.text_content() or "").strip()
        rel = (a.get("rel") or "").lower()
        cls = " ".join([a.get("class") or "", a.get("aria-label") or "", (a.getparent().get("class") or "") if a.getparent() is not None else ""])
        if "next" in rel or (len(txt) < 25 and _PAG_RX.search(txt)) or re.search(r"\bnext\b|pagination-next", cls, re.I):
            if href not in pagination:
                pagination.append(href)
    for ln in root.iter("link"):
        if (ln.get("rel") or "").lower() == "next" and ln.get("href"):
            nxt = urljoin(url, ln.get("href"))
            if nxt not in pagination:
                pagination.insert(0, nxt)
    return links, {"internal": internal, "external": external, "pagination": pagination}


def extract_images(html_str: str, url: str) -> list[str]:
    root = parse(html_str)
    absolutize(root, url)
    out: list[str] = []
    for img in root.iter("img"):
        src = img.get("src")
        if src and src.startswith("http") and src not in out:
            out.append(src)
    for m in root.iter("meta"):
        if (m.get("property") or "") in ("og:image", "twitter:image") and m.get("content"):
            u = urljoin(url, m.get("content"))
            if u not in out:
                out.append(u)
    return out
