"""Generación automática de robots a partir de una URL (sin grabar nada).

1) Se carga la página (navegador si hace falta), 2) se detectan las listas repetidas y la
paginación, 3) opcionalmente un LLM elige la lista y renombra campos según el prompt.
El resultado es un robot DETERMINISTA: el LLM solo se usa al crearlo, no en cada ejecución
(mismo enfoque que el AI Extract v2 de Maxun, pero sin grabador y con autorreparación posterior).
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

from ..extract import llm
from ..extract.select import detect_lists
from ..scrape import scrape_url
from ..transform.meta import extract_links


async def build_robot(url: str, prompt: str | None = None, use_browser: bool = True) -> dict:
    opts = {"formats": ["rawHtml", "links"], "engine": "browser" if use_browser else "auto", "onlyMainContent": False}
    doc = await scrape_url(url, opts)
    html = doc.get("rawHtml") or ""
    if not html:
        raise RuntimeError(f"No se pudo cargar la página: {doc.get('metadata', {}).get('error')}")
    final = doc["metadata"].get("url", url)
    lists = detect_lists(html, final, max_lists=6)
    if not lists:
        raise RuntimeError("No se han encontrado listas repetidas en la página")
    chosen = lists[0]
    field_names: dict[str, str] = {}
    if prompt and llm.is_configured():
        menu = [{"i": i, "selector": c["listSelector"], "count": c["count"], "fields": list(c["fields"]),
                 "sample": c["sample"][:2]} for i, c in enumerate(lists)]
        raw = await llm.chat(
            "Eliges qué lista de una página web corresponde a lo que pide el usuario y das nombres claros a sus campos. "
            "Responde JSON: {\"i\": <índice>, \"rename\": {\"campo_actual\": \"nombre_nuevo\"}, \"drop\": [\"campo\"]}",
            f"Petición: {prompt}\n\nListas candidatas:\n{json.dumps(menu, ensure_ascii=False, default=str)[:30000]}",
        )
        try:
            pick = json.loads(raw[raw.find("{"):])
            chosen = lists[int(pick.get("i", 0))]
            field_names = pick.get("rename") or {}
            for d in pick.get("drop") or []:
                chosen["fields"].pop(d, None)
        except (ValueError, IndexError, KeyError):
            pass
    fields = {field_names.get(k, k): v for k, v in chosen["fields"].items()}
    _, info = extract_links(html, final)
    paginate = None
    if info["pagination"]:
        nxt = info["pagination"][0]
        paginate = {"type": "next", "max": 10}
        if _looks_like_page_param(nxt):
            paginate = {"type": "next", "max": 10}
    host = urlparse(final).hostname or "sitio"
    robot = {
        "name": f"Auto: {host}",
        "description": prompt or f"Lista detectada automáticamente ({chosen['count']} elementos por página)",
        "startUrl": final,
        "steps": [{"extract": {"name": "items", "list": chosen["listSelector"], "fields": fields, "limit": 1000,
                               **({"paginate": paginate} if paginate else {})}}],
        "options": {"timeout": 30000},
        "validate": {"minItems": {"items": 1}},
    }
    return {"robot": robot, "candidates": [{k: v for k, v in c.items() if k != "_els"} for c in lists],
            "pagination": info["pagination"][:5]}


def _looks_like_page_param(u: str) -> bool:
    return "page" in u.lower() or "pagina" in u.lower()
