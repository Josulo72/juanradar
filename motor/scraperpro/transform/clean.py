"""Limpieza del HTML y detección del contenido principal.

Firecrawl borra 48 selectores fijos (header, .top, .side, .menu…) aunque contengan el contenido real.
Aquí: 1) se prueba un contenedor principal semántico (main/article/role=main) si concentra el texto;
2) los candidatos a "ruido" solo se eliminan si su texto es pequeño respecto a la página o su
densidad de enlaces es alta. Nunca se borra un bloque que contenga la mayor parte del texto.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from lxml import etree, html as lhtml

ALWAYS_REMOVE = ["script", "style", "noscript", "template", "svg", "canvas", "object", "embed", "link", "meta"]

NOISE_SELECTORS = [
    "header", "footer", "nav", "aside", "[role=navigation]", "[role=banner]", "[role=contentinfo]",
    "[role=complementary]", "[role=dialog]", "[aria-modal=true]",
    ".header", "#header", ".footer", "#footer", ".navbar", ".nav", "#nav", ".navigation", ".menu", ".main-menu",
    ".sidebar", "#sidebar", ".side-bar", ".breadcrumb", ".breadcrumbs", "#breadcrumbs",
    ".modal", ".popup", ".overlay", "#modal", ".cookie", "#cookie", ".cookies", ".cookie-banner", "#cookie-banner",
    ".consent", "#consent", "[id*=onetrust]", "[class*=cookie-consent]", "[aria-label='Cookie consent' i]",
    ".ad", ".ads", ".advert", ".advertisement", "#ad", "[class*=sponsor]", ".social", ".social-media", ".social-links",
    ".share", "#share", ".sharing", ".newsletter", ".subscribe", ".lang-selector", ".language", "#language-selector",
    ".skip-link", ".skip-to-content", ".widget", ".related-posts", ".comments", "#comments", ".grecaptcha-badge",
]
FORCE_KEEP = ["main", "#main", "#content", ".content", "article", "[role=main]"]

_WS = re.compile(r"\s+")


def _text_len(el) -> int:
    try:
        return len(_WS.sub(" ", el.text_content() or "").strip())
    except Exception:
        return 0


def _link_density(el) -> float:
    total = _text_len(el)
    if not total:
        return 1.0
    link_text = sum(_text_len(a) for a in el.iter("a"))
    return link_text / total


def _css(root, selector: str) -> list:
    try:
        return root.cssselect(selector)
    except Exception:
        return []


def parse(html_str: str):
    if not html_str or not html_str.strip():
        return lhtml.fromstring("<html><body></body></html>")
    try:
        return lhtml.document_fromstring(html_str)
    except (etree.ParserError, ValueError):
        return lhtml.fromstring(f"<html><body>{html_str}</body></html>")


def absolutize(root, base_url: str) -> None:
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in ("href", "src", "poster", "action"):
            v = el.get(attr)
            if v and not v.startswith(("data:", "javascript:", "mailto:", "tel:", "#")):
                try:
                    el.set(attr, urljoin(base_url, v.strip()))
                except ValueError:
                    pass
        if el.tag == "img":
            best = _best_src(el)
            if best:
                el.set("src", urljoin(base_url, best))


def _best_src(img) -> str | None:
    """La imagen de mayor resolución de srcset/data-srcset/data-src (lazy-loading)."""
    for attr in ("srcset", "data-srcset"):
        v = img.get(attr)
        if v:
            cands = []
            for part in v.split(","):
                bits = part.strip().split()
                if not bits:
                    continue
                w = 0
                if len(bits) > 1:
                    m = re.match(r"(\d+(?:\.\d+)?)[wx]", bits[1])
                    w = float(m.group(1)) if m else 0
                cands.append((w, bits[0]))
            if cands:
                return max(cands)[1]
    for attr in ("data-src", "data-lazy-src", "data-original"):
        if img.get(attr):
            return img.get(attr)
    src = img.get("src")
    if src and src.startswith("data:") and len(src) < 200:
        return None
    return src


def clean_html(
    html_str: str,
    base_url: str,
    *,
    only_main: bool = True,
    include_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    remove_base64_images: bool = True,
) -> str:
    root = parse(html_str)
    for tag in ALWAYS_REMOVE:
        for el in root.iter(tag):
            _drop(el)
    for el in list(root.iter(etree.Comment)):
        _drop(el)
    for el in list(root.iter("iframe")):
        src = el.get("src", "")
        if not re.search(r"youtube|vimeo|maps\.google|google\.com/maps", src):
            _drop(el)
    for el in _css(root, "[hidden], [aria-hidden=true], [style*='display:none'], [style*='display: none']"):
        if _text_len(el) < 400:
            _drop(el)
    absolutize(root, base_url)
    if remove_base64_images:
        for img in list(root.iter("img")):
            if (img.get("src") or "").startswith("data:"):
                _drop(img)

    body = root.find("body")
    if body is None:
        body = root

    if include_tags:
        keep = []
        for sel in include_tags:
            keep.extend(_css(root, sel))
        new_body = lhtml.Element("body")
        for el in keep:
            new_body.append(_clone(el))
        body = new_body
    elif only_main:
        body = _main_content(body)

    for sel in exclude_tags or []:
        if sel.startswith("*") and sel.endswith("*") and len(sel) > 2:
            rx = re.compile(sel.strip("*"), re.I)
            for el in list(body.iter()):
                if isinstance(el.tag, str) and (rx.search(el.tag) or any(rx.search(f"{k}={v}") for k, v in el.attrib.items())):
                    _drop(el)
        else:
            for el in _css(body, sel):
                _drop(el)
    return lhtml.tostring(body, encoding="unicode", method="html")


def _main_content(body):
    total = _text_len(body) or 1
    # 1) Contenedor semántico que concentra el contenido
    for sel in ("main", "[role=main]", "article", "#content", "#main", ".main-content", ".post-content", ".entry-content"):
        cands = _css(body, sel)
        if len(cands) == 1 or (sel == "article" and len(cands) >= 1):
            best = max(cands, key=_text_len)
            if _text_len(best) >= 0.45 * total and _text_len(best) > 200:
                if sel == "article" and len(cands) > 1:
                    break  # varias tarjetas <article>: es un listado, no un artículo
                body = best
                break
    total = _text_len(body) or 1
    # 2) Ruido: se elimina solo si es pequeño o es casi todo enlaces
    for sel in NOISE_SELECTORS:
        for el in _css(body, sel):
            if el is body or el.getparent() is None:
                continue
            if any(el.xpath("self::main|self::article")) or _is_force_keep(el):
                continue
            tl = _text_len(el)
            if tl == 0 or tl < 0.30 * total or (_link_density(el) > 0.6 and tl < 0.6 * total):
                _drop(el)
    return body


def _is_force_keep(el) -> bool:
    eid = (el.get("id") or "").lower()
    cls = (el.get("class") or "").lower().split()
    return eid in ("main", "content") or "content" in cls or el.get("role") == "main"


def _drop(el) -> None:
    parent = el.getparent()
    if parent is None:
        return
    tail = el.tail
    if tail:
        prev = el.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(el)


def _clone(el):
    return lhtml.fromstring(lhtml.tostring(el, encoding="unicode"))
