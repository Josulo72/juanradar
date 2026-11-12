"""Orquestador de un scrape: descarga (con escalado), transforma y genera los formatos pedidos.

Formatos: markdown, html, rawHtml, text, links, images, screenshot, screenshot@fullPage, summary,
json, select (determinista), lists (detección automática), attributes, changeTracking,
structuredData, network (APIs XHR/JSON capturadas), question, branding.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse

from . import db
from .engines.documents import document_to_markdown
from .engines.router import fetch
from .extract import llm
from .extract.select import detect_lists, extract_select
from .transform.changes import track
from .transform.clean import clean_html, parse
from .transform.markdown import html_to_markdown
from .transform.meta import extract_images, extract_links, extract_metadata, structured_data


def _fmt(f: Any) -> tuple[str, dict]:
    if isinstance(f, str):
        return f, {}
    return str(f.get("type", "")), f


def normalize_formats(opts: dict) -> list[tuple[str, dict]]:
    out = []
    for f in opts.get("formats") or ["markdown"]:
        name, spec = _fmt(f)
        if name == "json" and not spec and opts.get("jsonOptions"):
            spec = dict(opts["jsonOptions"])  # estilo Firecrawl v1
        if name == "changeTracking" and not spec and opts.get("changeTrackingOptions"):
            spec = dict(opts["changeTrackingOptions"])
        if name == "extract":  # alias antiguo de Firecrawl
            name, spec = "json", dict(opts.get("extract") or spec)
        out.append((name, spec))
    return out


def validate_url(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    if not p.hostname or "." not in p.hostname and p.hostname not in ("localhost",):
        raise ValueError(f"URL no válida: {url}")
    return url


def _cache_key(url: str, opts: dict) -> str:
    relevant = {k: opts.get(k) for k in ("formats", "onlyMainContent", "includeTags", "excludeTags", "actions",
                                          "mobile", "location", "waitFor", "headers")}
    return hashlib.sha256((url + json.dumps(relevant, sort_keys=True, default=str)).encode()).hexdigest()


async def scrape_url(url: str, opts: dict | None = None) -> dict:
    opts = dict(opts or {})
    url = validate_url(url)
    formats = normalize_formats(opts)
    names = [n for n, _ in formats]
    max_age = int(opts.get("maxAge") or 0)
    key = _cache_key(url, opts)
    if max_age > 0 and "changeTracking" not in names:
        cached = db.cache_get(key, max_age)
        if cached:
            cached.setdefault("metadata", {})["cacheState"] = "hit"
            return cached

    r = await fetch(url, opts)
    md: dict[str, Any] = {
        "sourceURL": url, "url": r.final_url or url, "statusCode": r.status, "engine": r.engine,
        "proxyUsed": r.proxy_used, "elapsedMs": r.elapsed_ms, "attempts": r.attempts, "cacheState": "miss",
    }
    doc: dict[str, Any] = {"metadata": md}
    warnings: list[str] = []
    if r.error and not r.html and not r.content:
        md["error"] = r.error
        doc["success"] = False
        return doc
    if r.error:
        warnings.append(r.error)

    # ------------------------------------------------ documentos (PDF/DOCX/XLSX)
    if r.is_document or re.search(r"\.(pdf|docx|xlsx)(\?|$)", url, re.I) and r.content and not r.html.lstrip().startswith("<"):
        if not opts.get("parsePDF", True) and "pdf" in (r.content_type or ""):
            import base64
            doc["rawBase64"] = base64.b64encode(r.content).decode()
        else:
            text, dmeta = document_to_markdown(r.content, r.content_type, url)
            md.update({"contentType": r.content_type, **{f"document_{k}": v for k, v in dmeta.items()}})
            if dmeta.get("warning"):
                warnings.append(dmeta["warning"])
            doc["markdown"] = text
            if "summary" in names and llm.is_configured():
                doc["summary"] = await llm.summarize(text)
            for name, spec in formats:
                if name == "json":
                    doc["json"] = await _json_format(spec, "", text)
        doc["success"] = True
        if warnings:
            md["warnings"] = warnings
        return doc

    raw_html = r.html or ""
    md.update({k: v for k, v in extract_metadata(raw_html, r.final_url or url, r.status, r.content_type).items()
               if k not in ("url", "statusCode", "sourceURL")})
    md["sourceURL"] = url
    cleaned = clean_html(
        raw_html, r.final_url or url, only_main=bool(opts.get("onlyMainContent", True)),
        include_tags=opts.get("includeTags"), exclude_tags=opts.get("excludeTags"),
        remove_base64_images=bool(opts.get("removeBase64Images", True)),
    )
    markdown = html_to_markdown(cleaned)
    if not markdown.strip() and opts.get("onlyMainContent", True):
        # como Firecrawl: si la limpieza deja la página vacía, se repite sin ella
        cleaned = clean_html(raw_html, r.final_url or url, only_main=False)
        markdown = html_to_markdown(cleaned)
        warnings.append("onlyMainContent dejó la página vacía; se devuelve la página completa")

    for name, spec in formats:
        try:
            if name == "markdown":
                doc["markdown"] = markdown
            elif name == "html":
                doc["html"] = cleaned
            elif name == "rawHtml":
                doc["rawHtml"] = raw_html
            elif name == "text":
                doc["text"] = re.sub(r"\n{3,}", "\n\n", parse(cleaned).text_content()).strip()
            elif name == "links":
                links, info = extract_links(raw_html, r.final_url or url)
                doc["links"] = links
                md["linksInfo"] = info
            elif name == "images":
                doc["images"] = extract_images(raw_html, r.final_url or url)
            elif name in ("screenshot", "screenshot@fullPage"):
                if r.screenshot:
                    doc["screenshot"] = _save_png(r.screenshot, url)
            elif name == "structuredData":
                doc["structuredData"] = structured_data(parse(raw_html))
            elif name == "select":
                doc.setdefault("select", {})
                result = extract_select(raw_html, r.final_url or url, spec.get("fields", {}), spec.get("list"), spec.get("limit"))
                if spec.get("name"):
                    doc["select"][spec["name"]] = result
                else:
                    doc["select"] = result
            elif name == "lists":
                doc["lists"] = detect_lists(raw_html, r.final_url or url, max_lists=int(spec.get("max", 5)))
            elif name == "attributes":
                out = []
                for s in spec.get("selectors", []):
                    vals = extract_select(raw_html, r.final_url or url,
                                          {"v": {"selector": s["selector"], "attr": s["attribute"], "multiple": True}})["v"]
                    out.append({"selector": s["selector"], "attribute": s["attribute"], "values": vals})
                doc["attributes"] = out
            elif name == "summary":
                doc["summary"] = await llm.summarize(markdown)
            elif name == "question":
                doc["answer"] = await llm.answer(markdown, spec.get("question") or spec.get("prompt") or "")
            elif name == "json":
                doc["json"] = await _json_format(spec, cleaned if opts.get("onlyMainContent", True) else raw_html, markdown)
            elif name == "network":
                doc["network"] = r.network
            elif name == "branding":
                doc["branding"] = _branding(raw_html, md)
            elif name == "changeTracking":
                data = None
                if "json" in (spec.get("modes") or []) and (spec.get("schema") or spec.get("prompt")):
                    data = await _json_format(spec, cleaned, markdown)
                doc["changeTracking"] = track(md["url"], markdown, tag=spec.get("tag") or "", data=data, status=r.status,
                                              modes=spec.get("modes"), ignore=spec.get("ignorePatterns"))
        except llm.LLMNotConfigured as e:
            warnings.append(f"{name}: {e}")
        except Exception as e:
            warnings.append(f"{name}: {type(e).__name__}: {e}")
    if r.actions:
        acts = dict(r.actions)
        acts["screenshots"] = [_save_png(s, url) for s in acts.get("screenshots", [])]
        doc["actions"] = acts
    if r.js_return is not None:
        doc["javascriptReturn"] = r.js_return
    if r.network and "network" not in doc and opts.get("captureNetwork"):
        doc["network"] = r.network
    md["quality"] = _quality(markdown, r)
    if r.blocked:
        md["blocked"] = r.blocked
    if warnings:
        md["warnings"] = warnings
    doc["success"] = r.ok
    if r.ok and opts.get("storeInCache", True):
        db.cache_put(key, url, doc)
    return doc


async def _json_format(spec: dict, html_content: str, markdown: str) -> Any:
    schema = spec.get("schema") or spec.get("schema_")
    prompt = spec.get("prompt")
    return await llm.extract_json(html_content, schema=schema, prompt=prompt, markdown=markdown,
                                  provider=spec.get("provider"), model=spec.get("model"))


def _save_png(b64: str, url: str) -> str:
    """Guarda la captura en data/files y devuelve su URL servida por la API."""
    import base64

    from .config import settings

    name = hashlib.sha1((url + b64[:200]).encode()).hexdigest()[:20] + ".png"
    path = settings.files_dir / name
    path.write_bytes(base64.b64decode(b64))
    return f"/files/{name}"


def _quality(markdown: str, r) -> dict:
    words = len(re.findall(r"\w+", markdown))
    score = 1.0
    reasons = []
    if r.blocked:
        score -= 0.6
        reasons.append(f"bloqueo: {r.blocked}")
    if words < 30:
        score -= 0.3
        reasons.append("muy poco texto")
    if r.error:
        score -= 0.2
        reasons.append("error parcial")
    return {"score": round(max(score, 0), 2), "words": words, "reasons": reasons}


def _branding(raw_html: str, md: dict) -> dict:
    """Identidad visual básica sin LLM: colores, fuentes, logo, favicon."""
    colors = re.findall(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)", raw_html)
    fonts = re.findall(r"font-family\s*:\s*([^;\"}]+)", raw_html, re.I)
    from collections import Counter

    root = parse(raw_html)
    logo = None
    for img in root.iter("img"):
        hint = " ".join([img.get("alt") or "", img.get("class") or "", img.get("id") or "", img.get("src") or ""]).lower()
        if "logo" in hint:
            logo = img.get("src")
            break
    return {
        "colors": [c for c, _ in Counter(c.lower() for c in colors).most_common(8)],
        "fonts": [f.strip().strip("'\"") for f, _ in Counter(fonts).most_common(5)],
        "logo": logo, "favicon": md.get("favicon"), "themeColor": md.get("themeColor"),
        "ogImage": md.get("og:image"),
    }
