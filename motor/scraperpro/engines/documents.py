"""Conversión de documentos (PDF, DOCX, XLSX) a markdown, en local y sin servicios de pago."""
from __future__ import annotations

import io
import re


def document_to_markdown(content: bytes, content_type: str = "", url: str = "", max_pages: int = 500) -> tuple[str, dict]:
    ct = (content_type or "").lower()
    low = url.lower().split("?")[0]
    if content[:4] == b"%PDF" or "pdf" in ct or low.endswith(".pdf"):
        return pdf_to_markdown(content, max_pages)
    if "wordprocessingml" in ct or low.endswith(".docx"):
        return docx_to_markdown(content)
    if "spreadsheetml" in ct or low.endswith(".xlsx"):
        return xlsx_to_markdown(content)
    return content.decode("utf-8", "replace"), {}


def pdf_to_markdown(content: bytes, max_pages: int = 500) -> tuple[str, dict]:
    import fitz  # PyMuPDF

    doc = fitz.open(stream=content, filetype="pdf")
    meta = {k: v for k, v in (doc.metadata or {}).items() if v}
    meta["numPages"] = doc.page_count
    parts: list[str] = []
    sizes: list[float] = []
    pages = list(doc)[:max_pages]
    for page in pages:  # primera pasada: tamaño de letra típico para detectar títulos
        for b in page.get_text("dict").get("blocks", []):
            for line in b.get("lines", []):
                for sp in line.get("spans", []):
                    if sp.get("text", "").strip():
                        sizes.append(sp.get("size", 0))
    body = sorted(sizes)[len(sizes) // 2] if sizes else 11
    for n, page in enumerate(pages, 1):
        tables = []
        try:
            for t in page.find_tables().tables:
                rows = t.extract()
                if rows and len(rows) > 1:
                    tables.append(_md_table(rows))
        except Exception:
            pass
        for b in page.get_text("dict").get("blocks", []):
            text = " ".join(sp.get("text", "") for line in b.get("lines", []) for sp in line.get("spans", [])).strip()
            if not text:
                continue
            size = max((sp.get("size", 0) for line in b.get("lines", []) for sp in line.get("spans", [])), default=body)
            if size >= body * 1.6 and len(text) < 150:
                parts.append(f"# {text}")
            elif size >= body * 1.25 and len(text) < 200:
                parts.append(f"## {text}")
            else:
                parts.append(text)
        parts.extend(tables)
        if n < len(pages):
            parts.append(f"\n<!-- página {n + 1} -->\n")
    md = "\n\n".join(parts)
    if len(re.sub(r"\s", "", md)) < 50 * max(1, len(pages)):
        meta["warning"] = "PDF con poco texto: probablemente escaneado (necesitaría OCR)"
    return md, meta


def docx_to_markdown(content: bytes) -> tuple[str, dict]:
    import docx  # python-docx

    d = docx.Document(io.BytesIO(content))
    out: list[str] = []
    for p in d.paragraphs:
        t = p.text.strip()
        if not t:
            continue
        style = (p.style.name or "").lower() if p.style is not None else ""
        if style.startswith("heading") or style.startswith("título") or style.startswith("titulo"):
            level = re.findall(r"\d", style)
            out.append("#" * min(int(level[0]) if level else 1, 6) + " " + t)
        elif "list" in style:
            out.append(f"- {t}")
        else:
            out.append(t)
    for table in d.tables:
        out.append(_md_table([[c.text.strip() for c in row.cells] for row in table.rows]))
    return "\n\n".join(out), {"paragraphs": len(d.paragraphs), "tables": len(d.tables)}


def xlsx_to_markdown(content: bytes) -> tuple[str, dict]:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        rows = [["" if v is None else str(v) for v in r] for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(c.strip() for c in r)][:2000]
        if rows:
            out.append(f"## {ws.title}\n\n" + _md_table(rows))
    return "\n\n".join(out), {"sheets": len(wb.worksheets)}


def _md_table(rows: list[list]) -> str:
    rows = [["" if c is None else str(c).replace("\n", " ").replace("|", "\\|").strip() for c in r] for r in rows]
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head, *body = rows
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)
