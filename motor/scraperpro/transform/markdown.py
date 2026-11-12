"""Conversor HTML → markdown propio.

Corrige el fallo observado en la nube de Firecrawl: los elementos inline consecutivos se pegaban
("changedeep-thoughtsthinkingworld"). Aquí el espacio entre nodos se conserva y los inline con
pinta de "chip/etiqueta" se separan. Soporta tablas GFM, listas anidadas, código, citas, imágenes.
"""
from __future__ import annotations

import re

from lxml import html as lhtml

BLOCK = {
    "address", "article", "aside", "blockquote", "details", "dialog", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "li",
    "main", "nav", "ol", "p", "pre", "section", "table", "ul", "summary", "tr", "body", "html", "center",
}
SKIP = {"script", "style", "noscript", "template", "head", "svg", "button", "select", "option", "input", "textarea"}
_WS = re.compile(r"[ \t\r\n\f\v]+")


def html_to_markdown(html_str: str) -> str:
    if not html_str or not html_str.strip():
        return ""
    try:
        root = lhtml.fromstring(html_str)
    except Exception:
        return ""
    conv = _Converter()
    out = conv.block(root)
    return _post(out)


def _post(md: str) -> str:
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"^(Skip to (main )?content|Saltar al contenido( principal)?)\s*$", "", md, flags=re.I | re.M)
    return md.strip() + "\n" if md.strip() else ""


class _Converter:
    def __init__(self) -> None:
        self.list_depth = 0

    # ---------------------------------------------------------------- bloques
    def block(self, el) -> str:
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag in SKIP:
            return ""
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = self.inline_of(el).strip()
            return f"\n\n{'#' * int(tag[1])} {text}\n\n" if text else ""
        if tag == "p":
            text = self.inline_of(el).strip()
            return f"\n\n{text}\n\n" if text else ""
        if tag == "hr":
            return "\n\n---\n\n"
        if tag == "pre":
            code = el.text_content()
            lang = ""
            c = el.find(".//code")
            if c is not None:
                m = re.search(r"(?:language|lang)-([\w+#-]+)", c.get("class", ""))
                lang = m.group(1) if m else ""
            fence = "````" if "```" in code else "```"
            return f"\n\n{fence}{lang}\n{code.rstrip()}\n{fence}\n\n"
        if tag == "blockquote":
            inner = self.children_blocks(el).strip()
            return "\n\n" + "\n".join("> " + ln if ln else ">" for ln in inner.splitlines()) + "\n\n" if inner else ""
        if tag in ("ul", "ol"):
            return self.list_(el, ordered=tag == "ol")
        if tag == "table":
            return self.table(el)
        if tag == "dl":
            parts = []
            for child in el:
                if child.tag == "dt":
                    parts.append(f"**{self.inline_of(child).strip()}**")
                elif child.tag == "dd":
                    parts.append(f": {self.inline_of(child).strip()}")
            return "\n\n" + "\n".join(parts) + "\n\n"
        if tag == "br":
            return "\n"
        if tag == "img":
            return self.inline(el)
        return self.children_blocks(el)

    def children_blocks(self, el) -> str:
        """Mezcla de hijos bloque e inline: los inline contiguos forman un párrafo."""
        out: list[str] = []
        buf: list[str] = []

        def flush() -> None:
            text = _join_inline(buf)
            text = re.sub(r" *\n *", "\n", text)
            if text.strip():
                out.append("\n\n" + text.strip() + "\n\n")
            buf.clear()

        if el.text:
            buf.append(_collapse(el.text))
        for child in el:
            ctag = child.tag if isinstance(child.tag, str) else ""
            if ctag in BLOCK or ctag in ("img",) and _is_big_image(child):
                flush()
                out.append(self.block(child))
            elif ctag in SKIP or not ctag:
                pass
            else:
                buf.append(self.inline(child))
            if child.tail:
                buf.append(_collapse(child.tail))
        flush()
        return "".join(out)

    # ---------------------------------------------------------------- inline
    def inline_of(self, el) -> str:
        parts = [_collapse(el.text or "")]
        for child in el:
            parts.append(self.inline(child))
            if child.tail:
                parts.append(_collapse(child.tail))
        return _join_inline(parts)

    def inline(self, el) -> str:
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag in SKIP or not tag:
            return ""
        if tag == "br":
            return "  \n"
        if tag == "img":
            src = el.get("src") or ""
            if not src or src.startswith("data:"):
                return ""
            alt = (el.get("alt") or "").replace("]", "\\]").strip()
            return f"![{alt}]({src})"
        if tag in BLOCK:
            # bloque dentro de inline (p.ej. <a><div>…</div></a>): se aplana con espacios
            inner = self.inline_of(el).strip()
            return f" {inner} " if inner else ""
        inner = self.inline_of(el)
        if tag == "a":
            href = (el.get("href") or "").strip()
            text = inner.strip()
            if not text:
                img = el.find(".//img")
                if img is not None:
                    text = self.inline(img)
            if not href or href.startswith("javascript:"):
                return text
            if not text:
                return ""
            title = el.get("title")
            t = f' "{title}"' if title and title not in text else ""
            return _pad(inner, f"[{text}]({href.replace(' ', '%20')}{t})")
        if tag in ("strong", "b"):
            return _pad(inner, f"**{inner.strip()}**") if inner.strip() else inner
        if tag in ("em", "i"):
            return _pad(inner, f"*{inner.strip()}*") if inner.strip() else inner
        if tag in ("del", "s", "strike"):
            return _pad(inner, f"~~{inner.strip()}~~") if inner.strip() else inner
        if tag == "code":
            t = el.text_content()
            tick = "``" if "`" in t else "`"
            return f"{tick}{t}{tick}" if t.strip() else ""
        if tag in ("sup",):
            return f"^{inner.strip()}"
        if tag in ("span", "small", "label", "time", "abbr", "cite", "q", "mark", "u", "font", "bdi"):
            # "chips": spans/enlaces hermanos sin espacio entre ellos se separan en _join_inline
            return inner
        return inner

    # ---------------------------------------------------------------- listas y tablas
    def list_(self, el, ordered: bool) -> str:
        self.list_depth += 1
        lines: list[str] = []
        indent = "  " * (self.list_depth - 1)
        n = int(el.get("start", "1") or 1) if ordered else 0
        for li in el:
            if li.tag != "li":
                continue
            bullet = f"{n}." if ordered else "-"
            n += 1
            nested = []
            inline_parts = []
            if li.text:
                inline_parts.append(_collapse(li.text))
            for child in li:
                if child.tag in ("ul", "ol"):
                    nested.append(self.list_(child, ordered=child.tag == "ol"))
                elif isinstance(child.tag, str) and child.tag in BLOCK and child.tag not in ("li",):
                    inline_parts.append(" " + self.block(child).strip() + " ")
                else:
                    inline_parts.append(self.inline(child))
                if child.tail:
                    inline_parts.append(_collapse(child.tail))
            text = _join_inline(inline_parts).strip()
            text = re.sub(r"\n+", " ", text)
            lines.append(f"{indent}{bullet} {text}".rstrip())
            for nl in nested:
                lines.append(nl.strip("\n"))
        self.list_depth -= 1
        body = "\n".join(lines)
        return f"\n\n{body}\n\n" if self.list_depth == 0 else f"\n{body}\n"

    def table(self, el) -> str:
        rows = []
        for tr in el.iter("tr"):
            cells = [c for c in tr if c.tag in ("td", "th")]
            if not cells:
                continue
            row = []
            for c in cells:
                txt = self.inline_of(c).strip().replace("\n", " ").replace("|", "\\|")
                row.append(txt)
                for _ in range(int(c.get("colspan", "1") or 1) - 1):
                    row.append("")
            rows.append(row)
        if not rows:
            return ""
        if len(rows) == 1 or max(len(r) for r in rows) == 1:
            return "\n\n" + "\n".join(" ".join(r) for r in rows) + "\n\n"
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        head, *body = rows
        out = ["| " + " | ".join(head) + " |", "|" + " --- |" * width]
        out += ["| " + " | ".join(r) + " |" for r in body]
        caption = el.find("caption")
        cap = f"**{caption.text_content().strip()}**\n\n" if caption is not None and caption.text_content().strip() else ""
        return "\n\n" + cap + "\n".join(out) + "\n\n"


def _collapse(s: str) -> str:
    return _WS.sub(" ", s)


def _pad(original: str, rendered: str) -> str:
    lead = " " if original[:1].isspace() else ""
    trail = " " if original[-1:].isspace() else ""
    return f"{lead}{rendered}{trail}"


def _join_inline(parts: list[str]) -> str:
    """Une piezas inline; si dos piezas con texto se tocan sin espacio y ambas son "palabras"
    completas de elementos distintos (chips/etiquetas), inserta un espacio."""
    out = ""
    for p in parts:
        if not p:
            continue
        if out and not out[-1].isspace() and not p[0].isspace():
            a, b = out[-1], p[0]
            if (a in ")]*_~`" and (b.isalnum() or b in "[!*")) or (a.isalnum() and b in "[!"):
                out += " "
        out += p
    return re.sub(r" {2,}", " ", out) if "  \n" not in out else out


def _is_big_image(img) -> bool:
    try:
        return int(img.get("width", "0")) >= 300
    except ValueError:
        return False
