"""Seguimiento de cambios entre scrapes.

Firecrawl compara el markdown quitando espacios y ORDENANDO los caracteres: un texto reordenado
cuenta como "same". Aquí: hash normalizado + diff unificado por líneas + diff por campos del JSON,
con patrones a ignorar (fechas, contadores) para evitar falsos positivos.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from typing import Any

from .. import db


def _normalize(md: str, ignore: list[str] | None) -> str:
    t = md or ""
    for rx in ignore or []:
        try:
            t = re.sub(rx, "", t, flags=re.I)
        except re.error:
            continue
    return "\n".join(ln.strip() for ln in t.splitlines() if ln.strip())


def content_hash(md: str, ignore: list[str] | None = None) -> str:
    return hashlib.sha256(_normalize(md, ignore).encode()).hexdigest()


def json_diff(old: Any, new: Any, path: str = "") -> list[dict]:
    out: list[dict] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new)):
            p = f"{path}.{k}" if path else k
            if k not in old:
                out.append({"path": p, "change": "added", "new": new[k]})
            elif k not in new:
                out.append({"path": p, "change": "removed", "old": old[k]})
            else:
                out.extend(json_diff(old[k], new[k], p))
    elif isinstance(old, list) and isinstance(new, list):
        so = {json.dumps(x, sort_keys=True, ensure_ascii=False): x for x in old}
        sn = {json.dumps(x, sort_keys=True, ensure_ascii=False): x for x in new}
        for k in sn.keys() - so.keys():
            out.append({"path": f"{path}[]", "change": "added", "new": sn[k]})
        for k in so.keys() - sn.keys():
            out.append({"path": f"{path}[]", "change": "removed", "old": so[k]})
    elif old != new:
        out.append({"path": path or "$", "change": "changed", "old": old, "new": new})
    return out


def track(url: str, markdown: str, *, tag: str = "", data: Any = None, status: int = 200,
          modes: list[str] | None = None, ignore: list[str] | None = None, save: bool = True) -> dict:
    modes = modes or ["git-diff"]
    prev = db.one("SELECT * FROM snapshots WHERE url=? AND tag=? ORDER BY id DESC LIMIT 1", (url, tag))
    h = content_hash(markdown, ignore)
    result: dict[str, Any] = {"previousScrapeAt": prev["created_at"] if prev else None}
    if status == 404 or status == 410:
        result["changeStatus"] = "removed"
    elif not prev:
        result["changeStatus"] = "new"
    elif prev["content_hash"] == h and (data is None or db.loads(prev["json"]) == data):
        result["changeStatus"] = "same"
    else:
        result["changeStatus"] = "changed"
    result["visibility"] = "visible" if status < 400 else "hidden"
    if prev and result["changeStatus"] == "changed":
        if "git-diff" in modes:
            old_lines = _normalize(prev["markdown"] or "", ignore).splitlines()
            new_lines = _normalize(markdown, ignore).splitlines()
            diff = list(difflib.unified_diff(old_lines, new_lines, "anterior", "actual", lineterm="", n=2))
            added = [ln[1:] for ln in diff if ln.startswith("+") and not ln.startswith("+++")]
            removed = [ln[1:] for ln in diff if ln.startswith("-") and not ln.startswith("---")]
            result["diff"] = {"text": "\n".join(diff[:2000]), "added": added[:500], "removed": removed[:500]}
            ratio = difflib.SequenceMatcher(None, "\n".join(old_lines), "\n".join(new_lines)).ratio()
            result["similarity"] = round(ratio, 4)
        if "json" in modes and data is not None:
            result["json"] = json_diff(db.loads(prev["json"]), data)
    if save:
        db.execute(
            "INSERT INTO snapshots(url,tag,markdown,json,content_hash,created_at) VALUES(?,?,?,?,?,?)",
            (url, tag, markdown, db.dumps(data) if data is not None else None, h, db.now_iso()),
        )
    return result
