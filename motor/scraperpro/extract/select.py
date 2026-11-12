"""Extracción determinista por selectores (sin LLM, sin coste) y detección automática de listas.

Especificación de campo (cualquiera de estas formas):
  "h3 a"                       -> texto
  "h3 a@href"                  -> atributo (URLs se hacen absolutas)
  "p.star-rating@class"        -> atributo class (p. ej. "star-rating Three")
  {"selector": ".price", "type": "price"}
  {"selector": "li", "multiple": true}
  {"selector": ".rating", "attr": "class", "regex": "star-rating (\\w+)"}
  {"selector": ".item", "fields": {...}}  -> objeto/lista anidada
Selectores: CSS, o XPath si empiezan por "/" o "(".
Tipos: text, html, number, int, price, date, url, bool, json.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from lxml import html as lhtml

from ..transform.clean import parse

_WS = re.compile(r"\s+")


def _select(node, selector: str) -> list:
    selector = selector.strip()
    try:
        if selector.startswith(("/", "(", "./")):
            res = node.xpath(selector)
        else:
            res = node.cssselect(selector)
    except Exception:
        return []
    return [r for r in res if r is not None]


def _text(el) -> str:
    if isinstance(el, str):
        return _WS.sub(" ", el).strip()
    return _WS.sub(" ", el.text_content() or "").strip()


def _norm_spec(spec: Any) -> dict:
    if isinstance(spec, str):
        if "@" in spec and not spec.startswith(("/", "(")):
            sel, attr = spec.rsplit("@", 1)
            return {"selector": sel.strip(), "attr": attr.strip()}
        if spec.startswith(("/", "(")) and "/@" in spec:
            return {"selector": spec}
        return {"selector": spec}
    return dict(spec)


def extract_select(html_str: str, base_url: str, fields: dict, list_selector: str | None = None,
                   limit: int | None = None) -> Any:
    root = parse(html_str)
    if list_selector:
        items = _select(root, list_selector)
        rows = [extract_fields(it, fields, base_url) for it in items[: limit or None]]
        return [r for r in rows if any(v not in (None, "", []) for v in r.values())]
    return extract_fields(root, fields, base_url)


def extract_fields(node, fields: dict, base_url: str) -> dict:
    return {name: extract_value(node, spec, base_url) for name, spec in fields.items()}


def extract_value(node, spec: Any, base_url: str) -> Any:
    if isinstance(spec, list):  # selectores de respaldo en orden: el primero que dé valor
        for alt in spec:
            v = extract_value(node, alt, base_url)
            if v not in (None, "", []):
                return v
        return None
    s = _norm_spec(spec)
    sel = s.get("selector") or ""
    found = _select(node, sel) if sel else [node]
    if s.get("fields"):
        objs = [extract_fields(f, s["fields"], base_url) for f in found]
        return objs if s.get("multiple", True) else (objs[0] if objs else None)
    vals = [_value(f, s, base_url) for f in found]
    vals = [v for v in vals if v not in (None, "")]
    if s.get("multiple"):
        return vals
    if vals:
        return vals[0]
    return s.get("default")


def _value(el, s: dict, base_url: str) -> Any:
    attr = s.get("attr")
    typ = s.get("type", "text")
    if isinstance(el, str):  # XPath que devuelve texto/atributo
        raw = el
    elif attr:
        raw = el.get(attr)
        if raw is None and attr in ("text", "innerText"):
            raw = _text(el)
        if raw is None and attr == "html":
            raw = lhtml.tostring(el, encoding="unicode")
    elif typ == "html":
        raw = lhtml.tostring(el, encoding="unicode")
    else:
        raw = _text(el)
        if not raw and el.tag == "img":
            raw = el.get("alt") or el.get("src")
    if raw is None:
        return None
    if s.get("regex"):
        m = re.search(s["regex"], raw, re.I | re.S)
        if not m:
            return None
        raw = m.group(1) if m.groups() else m.group(0)
    if attr in ("href", "src", "action", "data-src", "poster") or typ == "url":
        raw = urljoin(base_url, raw.strip())
    if s.get("map"):
        raw = s["map"].get(raw, s["map"].get(str(raw).lower(), raw))
    return coerce(raw, typ)


# ---------------------------------------------------------------- normalizadores
_NUM_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
              "nine": 9, "ten": 10, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5}


def parse_number(s: Any) -> float | None:
    if isinstance(s, (int, float)):
        return float(s)
    if s is None:
        return None
    t = str(s).strip().lower()
    if t in _NUM_WORDS:
        return float(_NUM_WORDS[t])
    m = re.search(r"-?\d[\d.,\s  ]*", t)
    if not m:
        return None
    n = re.sub(r"[\s  ]", "", m.group(0)).rstrip(".,")
    if "," in n and "." in n:
        if n.rfind(",") > n.rfind("."):
            n = n.replace(".", "").replace(",", ".")  # 1.234,56
        else:
            n = n.replace(",", "")  # 1,234.56
    elif "," in n:
        parts = n.split(",")
        n = n.replace(",", ".") if len(parts[-1]) in (1, 2) and len(parts) == 2 else n.replace(",", "")
    elif n.count(".") > 1:
        n = n.replace(".", "")
    elif "." in n and len(n.split(".")[-1]) == 3 and len(n.split(".")[0]) <= 3 and not re.search(r"[€$£]", t):
        n = n.replace(".", "")  # 1.234 (miles) en formato europeo
    try:
        return float(n)
    except ValueError:
        return None


def parse_price(s: Any) -> dict | None:
    if s is None:
        return None
    t = str(s)
    amount = parse_number(t)
    if amount is None:
        return None
    cur = None
    for sym, code in (("€", "EUR"), ("£", "GBP"), ("US$", "USD"), ("$", "USD"), ("¥", "JPY"), ("₹", "INR"),
                      ("eur", "EUR"), ("usd", "USD"), ("gbp", "GBP"), ("mxn", "MXN")):
        if sym in t.lower() if sym.isalpha() else sym in t:
            cur = code
            break
    return {"amount": amount, "currency": cur, "raw": t.strip()}


_MONTHS = {"ene": 1, "jan": 1, "feb": 2, "mar": 3, "abr": 4, "apr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8,
           "aug": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12, "dec": 12}


def parse_date(s: Any) -> str | None:
    if not s:
        return None
    t = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y",
                "%d.%m.%Y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(t[:30], fmt).date().isoformat() if "%H" not in fmt else datetime.strptime(t, fmt).isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})\s+(?:de\s+)?([a-záéíóú]{3,})\.?\s+(?:de\s+)?(\d{4})", t.lower())
    if m and m.group(2)[:3] in _MONTHS:
        try:
            return datetime(int(m.group(3)), _MONTHS[m.group(2)[:3]], int(m.group(1))).date().isoformat()
        except ValueError:
            pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    return m.group(0) if m else t


def coerce(raw: Any, typ: str) -> Any:
    if typ in ("text", "html", "url", None):
        return raw.strip() if isinstance(raw, str) else raw
    if typ == "number":
        return parse_number(raw)
    if typ == "int":
        n = parse_number(raw)
        return int(n) if n is not None else None
    if typ == "price":
        return parse_price(raw)
    if typ == "date":
        return parse_date(raw)
    if typ == "bool":
        return str(raw).strip().lower() not in ("", "0", "false", "no", "none", "out of stock", "agotado", "sin stock")
    if typ == "json":
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None
    return raw


# ---------------------------------------------------------------- detección automática de listas
def detect_lists(html_str: str, base_url: str, max_lists: int = 5, min_items: int = 3) -> list[dict]:
    """Encuentra grupos de elementos repetidos (tarjetas, filas) y propone campos.

    Similar al "huella estructural" del grabador de Maxun, pero en servidor y sin grabar nada.
    """
    root = parse(html_str)
    groups: dict[tuple, list] = {}
    for el in root.iter():
        if not isinstance(el.tag, str) or el.tag in ("html", "head", "body", "script", "style", "option", "br", "span", "a", "b", "i", "strong", "em"):
            continue
        parent = el.getparent()
        if parent is None:
            continue
        sig = (id(parent), el.tag, _class_sig(el))
        groups.setdefault(sig, []).append(el)
    candidates = []
    for (pid, tag, cls), els in groups.items():
        if len(els) < min_items:
            continue
        texts = [_text(e) for e in els]
        filled = [t for t in texts if len(t) >= 3]
        if len(filled) < min_items:
            continue
        avg = sum(len(t) for t in filled) / len(filled)
        richness = sum(1 for e in els if len(list(e.iter())) >= 3) / len(els)
        if avg < 8 or richness < 0.5:
            continue
        uniq = len(set(texts)) / len(texts)
        score = len(els) * min(avg, 400) * richness * uniq
        parent = els[0].getparent()
        selector = _css_path(parent) + " > " + tag + ("." + ".".join(cls) if cls else "")
        candidates.append({"selector": selector, "count": len(els), "score": round(score, 1), "_els": els})
    candidates.sort(key=lambda c: -c["score"])
    out = []
    seen_sel = set()
    for c in candidates:
        if c["selector"] in seen_sel:
            continue
        seen_sel.add(c["selector"])
        fields = _propose_fields(c["_els"], base_url)
        if not fields:
            continue
        sample = [extract_fields(e, fields, base_url) for e in c["_els"][:3]]
        out.append({"listSelector": c["selector"], "count": c["count"], "score": c["score"], "fields": fields, "sample": sample})
        if len(out) >= max_lists:
            break
    return out


def _class_sig(el) -> tuple:
    cls = [c for c in (el.get("class") or "").split() if not re.search(r"\d{2,}|active|selected|odd|even|first|last|hover", c)]
    return tuple(sorted(cls)[:3])


def _css_path(el, max_depth: int = 4) -> str:
    parts = []
    cur = el
    while cur is not None and isinstance(cur.tag, str) and cur.tag not in ("html",) and len(parts) < max_depth:
        if cur.get("id") and not re.search(r"\d{3,}", cur.get("id")):
            parts.append(f"{cur.tag}#{cur.get('id')}")
            break
        cls = _class_sig(cur)
        parts.append(cur.tag + ("." + ".".join(cls) if cls else ""))
        if cur.tag == "body":
            break
        cur = cur.getparent()
    return " > ".join(reversed(parts))


def _propose_fields(items: list, base_url: str) -> dict:
    """Campos que aparecen en la mayoría de los elementos: títulos, enlaces, imágenes, precios, clases con valor."""
    counter: Counter = Counter()
    examples: dict[str, Any] = {}
    n = len(items)
    for it in items[:30]:
        seen_local = set()
        for el in it.iter():
            if not isinstance(el.tag, str) or el is it:
                continue
            rel = _relative_selector(it, el)
            if not rel or rel in seen_local:
                continue
            seen_local.add(rel)
            counter[rel] += 1
            examples.setdefault(rel, el)
    common = [sel for sel, c in counter.most_common(60) if c >= max(2, 0.6 * min(n, 30))]
    fields: dict[str, Any] = {}
    used_names: Counter = Counter()

    def add(name: str, spec: Any) -> None:
        used_names[name] += 1
        key = name if used_names[name] == 1 else f"{name}_{used_names[name]}"
        fields[key] = spec

    for sel in common:
        el = examples[sel]
        tag = el.tag
        txt = _text(el)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") or (tag == "a" and el.get("title")):
            add("title", f"{sel}@title" if el.get("title") and len(el.get("title")) > len(txt) else sel)
        if tag == "a" and el.get("href"):
            add("url", f"{sel}@href")
        elif tag == "img" and (el.get("src") or el.get("data-src")):
            add("image", f"{sel}@{'src' if el.get('src') else 'data-src'}")
            if el.get("alt"):
                add("image_alt", f"{sel}@alt")
        elif re.search(r"[€$£¥₹]\s?\d|\d[\d.,]*\s?(€|eur|usd)", txt, re.I) and len(txt) < 25 and not len(el):
            add("price", {"selector": sel, "type": "price"})
        elif el.get("class") and re.search(r"(rating|stars?|score|valoraci)", el.get("class"), re.I) and len(txt) < 5:
            add("rating", {"selector": sel, "attr": "class", "type": "number",
                           "regex": r"(?:rating|stars?|score)[-_ ]?(?:\w*[-_ ])?(\w+)\s*$"})
        elif tag in ("time",) or el.get("datetime"):
            add("date", {"selector": sel, "attr": "datetime"} if el.get("datetime") else {"selector": sel, "type": "date"})
        elif 3 <= len(txt) <= 400 and len(list(el)) == 0 and tag not in ("a", "button"):
            add(_guess_name(el), sel)
    # Elimina campos que devuelven exactamente lo mismo que otro anterior en los primeros elementos
    unique: dict[str, Any] = {}
    seen_vals: list = []
    for name, spec in fields.items():
        vals = [extract_value(it, spec, base_url) for it in items[:5]]
        if all(v in (None, "") for v in vals) or vals in seen_vals:
            continue
        seen_vals.append(vals)
        unique[name] = spec
    return dict(list(unique.items())[:15])


def _relative_selector(item, el) -> str | None:
    """Selector relativo estable: solo la PRIMERA clase (suele ser la estructural, p. ej. "star-rating"
    en "star-rating Three"; la segunda suele llevar el valor y cambia entre elementos)."""
    parts = []
    cur = el
    depth = 0
    while cur is not None and cur is not item and depth < 5:
        first = next((c for c in (cur.get("class") or "").split()
                      if not re.search(r"\d{2,}|active|selected|odd|even|first|last", c)), None)
        parts.append(cur.tag + (f".{first}" if first else ""))
        cur = cur.getparent()
        depth += 1
    if cur is not item:
        return None
    return " ".join(reversed(parts))


def _guess_name(el) -> str:
    cls = (el.get("class") or "").split()
    for c in cls + [el.get("itemprop") or ""]:
        c = re.sub(r"[^a-zA-Z_]", "_", c).strip("_").lower()
        if 2 < len(c) < 25:
            return c
    return {"p": "text", "span": "text", "div": "text", "li": "item", "td": "cell", "small": "note"}.get(el.tag, el.tag)
