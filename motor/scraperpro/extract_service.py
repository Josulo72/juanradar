"""Extract multi-URL (equivalente a /extract de Firecrawl) con comodines y búsqueda web opcional.

Flujo: expandir URLs (`https://sitio.com/*` → map ordenado por relevancia con el prompt) →
scrape en paralelo → extracción por documento con el LLM (HTML simplificado, sin perder atributos)
→ fusión con deduplicado y trazabilidad de fuentes por campo.
"""
from __future__ import annotations

import asyncio
from typing import Any

from .discovery import map_site, search
from .extract import llm
from .scrape import scrape_url


async def expand_urls(urls: list[str], prompt: str | None, per_wildcard: int) -> list[str]:
    out: list[str] = []
    for u in urls:
        if u.endswith("/*"):
            items = await map_site({"url": u[:-2], "search": prompt or "", "limit": 2000, "depth": 1})
            out.extend(i["url"] for i in items[:per_wildcard])
        else:
            out.append(u)
    return list(dict.fromkeys(out))


async def run_extract(req: dict) -> dict:
    if not llm.is_configured():
        raise llm.LLMNotConfigured(
            "El endpoint /extract necesita un LLM. Para extraer sin LLM usa el formato 'select' en /v1/scrape o un robot."
        )
    prompt = req.get("prompt")
    schema = req.get("schema") or req.get("schema_")
    urls = await expand_urls(req.get("urls") or [], prompt, int(req.get("limitPerWildcard") or 20))
    if req.get("enableWebSearch") and prompt:
        res = await search({"query": prompt, "limit": 5})
        urls += [r["url"] for r in res.get("data", []) if r["url"] not in urls]
    if not urls:
        return {"success": False, "error": "Sin URLs que procesar", "data": None}
    opts = dict(req.get("scrapeOptions") or {})
    opts["formats"] = ["markdown", "html"]
    sem = asyncio.Semaphore(4)

    async def one(u: str):
        async with sem:
            doc = await scrape_url(u, opts)
            if not doc.get("success"):
                return u, None, doc.get("metadata", {}).get("error")
            data = await llm.extract_json(doc.get("html") or "", schema=schema, prompt=prompt, markdown=doc.get("markdown"))
            return u, data, None

    results = await asyncio.gather(*[one(u) for u in urls[:100]], return_exceptions=True)
    merged: Any = None
    sources: dict[str, list[str]] = {}
    errors = []
    for r in results:
        if isinstance(r, Exception):
            errors.append(str(r))
            continue
        u, data, err = r
        if err or data is None:
            errors.append(f"{u}: {err}")
            continue
        merged = llm.merge([merged, data]) if merged is not None else data
        if isinstance(data, dict):
            for k, v in data.items():
                if v not in (None, "", [], {}):
                    sources.setdefault(k, []).append(u)
    out = {"success": merged is not None, "data": merged, "urls": urls}
    if req.get("showSources", True):
        out["sources"] = sources
    if errors:
        out["warnings"] = errors[:50]
    return out
