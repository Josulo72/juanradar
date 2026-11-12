"""Capa LLM multi-proveedor (OpenAI, Anthropic, Ollama u OpenAI-compatible) sin SDKs.

Mejoras frente a Firecrawl (modelos fijos, texto truncado, ciego a atributos):
- el modelo y el proveedor se eligen por configuración o por petición;
- el contenido se trocea y los resultados parciales se fusionan (no se pierde el final de la página);
- se envía una vista "HTML simplificado" que conserva atributos con información (class, alt,
  title, datetime, aria-label, itemprop, data-*): así la valoración de books.toscrape (en la clase
  CSS "star-rating Three") deja de salir "Not Rated";
- la respuesta se valida contra el esquema y se reintenta una vez con el error.
Las claves se leen de OPENAI_API_KEY / ANTHROPIC_API_KEY y nunca se registran.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from lxml import html as lhtml

from ..config import settings

KEEP_ATTRS = {"class", "id", "href", "src", "alt", "title", "datetime", "aria-label", "itemprop", "content", "value",
              "rel", "name", "property", "role", "colspan"}


class LLMNotConfigured(RuntimeError):
    pass


# Proveedores predefinidos: (URL base, modelo por defecto, variable de entorno de su clave).
# Todos salvo Anthropic hablan el protocolo de OpenAI (/chat/completions). Los modelos por defecto
# pueden quedar anticuados: se cambian con SP_LLM_MODEL sin tocar código.
PRESETS: dict[str, tuple[str, str, str]] = {
    "anthropic": ("https://api.anthropic.com", "claude-haiku-5-5", "ANTHROPIC_API_KEY"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", "OPENAI_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "GROQ_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat", "DEEPSEEK_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "mistral-small-latest", "MISTRAL_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini", "OPENROUTER_API_KEY"),
    "together": ("https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo", "TOGETHER_API_KEY"),
    "xai": ("https://api.x.ai/v1", "grok-3-mini", "XAI_API_KEY"),
    "ollama": ("http://127.0.0.1:11434/v1", "llama3.1", ""),
    "lmstudio": ("http://127.0.0.1:1234/v1", "local-model", ""),
}


def provider_info(provider: str | None = None, model: str | None = None) -> tuple[str, str, str, str]:
    """Devuelve (protocolo, modelo, url_base, clave). Orden: SP_LLM_PROVIDER → primera clave de proveedor
    presente → SP_LLM_BASE_URL (cualquier API compatible con OpenAI)."""
    p = (provider or settings.llm_provider or "").lower().strip()
    if not p:
        p = next((name for name, (_, _, env) in PRESETS.items() if env and os.environ.get(env)), "")
        if not p and settings.llm_base_url:
            p = "custom"
    if not p:
        raise LLMNotConfigured(
            "No hay LLM configurado. En motor\\.env pon SP_LLM_PROVIDER (anthropic, openai, gemini, groq, "
            "deepseek, mistral, openrouter, together, xai, ollama, lmstudio o custom) y SP_LLM_API_KEY. "
            "La extracción por selectores (formato 'select') no lo necesita."
        )
    base, default_model, env = PRESETS.get(p, (settings.llm_base_url, "default", ""))
    base = settings.llm_base_url or base
    if not base:
        raise LLMNotConfigured(f"Proveedor '{p}' desconocido: define SP_LLM_BASE_URL con la URL de su API compatible con OpenAI.")
    key = settings.llm_api_key or (os.environ.get(env, "") if env else "") or "sin-clave"
    if key == "sin-clave" and p not in ("ollama", "lmstudio", "custom"):
        raise LLMNotConfigured(f"Falta la clave de '{p}': pon SP_LLM_API_KEY (o {env}) en motor\\.env.")
    return ("anthropic" if p == "anthropic" else "openai"), model or settings.llm_model or default_model, base, key


def describe() -> dict:
    """Estado del LLM sin exponer la clave."""
    try:
        proto, m, base, key = provider_info()
        name = (settings.llm_provider or next((n for n, (_, _, e) in PRESETS.items() if e and os.environ.get(e)), "custom"))
        return {"configured": True, "provider": name, "protocol": proto, "model": m, "baseUrl": base,
                "key": "definida" if key != "sin-clave" else "no necesaria"}
    except LLMNotConfigured as e:
        return {"configured": False, "error": str(e), "providers": list(PRESETS)}


def is_configured() -> bool:
    try:
        provider_info()
        return True
    except LLMNotConfigured:
        return False


async def chat(system: str, user: str, *, json_mode: bool = True, provider: str | None = None, model: str | None = None,
               max_tokens: int = 4096) -> str:
    p, m, base, key = provider_info(provider, model)
    async with httpx.AsyncClient(timeout=120) as c:
        if p == "anthropic":
            r = await c.post(f"{base}/v1/messages", headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                             json={"model": m, "max_tokens": max_tokens, "system": system,
                                   "messages": [{"role": "user", "content": user}]})
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json().get("content", []))
        body: dict[str, Any] = {"model": m, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                "temperature": 0}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = await c.post(f"{base.rstrip('/')}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
        if r.status_code == 400 and json_mode:  # proveedores sin response_format
            body.pop("response_format", None)
            r = await c.post(f"{base.rstrip('/')}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def simplified_html(html_str: str, max_chars: int = 400_000) -> str:
    """HTML mínimo con los atributos que llevan datos; sin scripts, estilos ni atributos de maquetación."""
    from ..transform.clean import parse

    root = parse(html_str)
    for el in list(root.iter("script", "style", "noscript", "svg", "template", "head", "link", "meta", "iframe")):
        p = el.getparent()
        if p is not None:
            p.remove(el)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for a in list(el.attrib):
            if a not in KEEP_ATTRS and not a.startswith("data-"):
                del el.attrib[a]
            elif a.startswith("data-") and len(el.attrib[a]) > 120:
                del el.attrib[a]
        if el.get("src", "").startswith("data:"):
            del el.attrib["src"]
    s = lhtml.tostring(root, encoding="unicode")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"<(div|span)>\s*</\1>", "", s)
    return s[:max_chars]


def chunk(text: str, size: int = 60_000, overlap: int = 1500) -> list[str]:
    if len(text) <= size:
        return [text]
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        i += size - overlap
    return out


def _parse_json(s: str) -> Any:
    s = s.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1)
    start = min([i for i in (s.find("{"), s.find("[")) if i >= 0], default=0)
    return json.loads(s[start:])


def validate(data: Any, schema: dict | None) -> list[str]:
    """Validación ligera de JSON Schema (type/required/properties/items)."""
    errs: list[str] = []
    if not schema:
        return errs

    def check(v, sch, path):
        t = sch.get("type")
        types = {"object": dict, "array": list, "string": str, "number": (int, float), "integer": int, "boolean": bool}
        if t and v is not None:
            tt = t if isinstance(t, list) else [t]
            if not any(isinstance(v, types.get(x, object)) for x in tt if x != "null"):
                errs.append(f"{path}: se esperaba {t}")
                return
        if isinstance(v, dict):
            for r in sch.get("required", []):
                if r not in v:
                    errs.append(f"{path}.{r}: falta")
            for k, sub in (sch.get("properties") or {}).items():
                if k in v:
                    check(v[k], sub, f"{path}.{k}")
        if isinstance(v, list) and isinstance(sch.get("items"), dict):
            for i, x in enumerate(v[:50]):
                check(x, sch["items"], f"{path}[{i}]")

    check(data, schema, "$")
    return errs[:20]


SYS_EXTRACT = (
    "Eres un extractor de datos web preciso. Devuelve SOLO JSON válido que cumpla el esquema. "
    "Usa únicamente información presente en el contenido; si un dato no aparece, usa null. "
    "Fíjate también en los atributos (class, alt, title, datetime, aria-label, itemprop, data-*): a menudo contienen "
    "el valor (p. ej. class=\"star-rating Three\" significa valoración 3). No inventes."
)


async def extract_json(content_html: str, *, schema: dict | None = None, prompt: str | None = None,
                       markdown: str | None = None, provider: str | None = None, model: str | None = None) -> Any:
    view = simplified_html(content_html) if content_html else (markdown or "")
    parts = chunk(view)
    results = []
    for i, part in enumerate(parts):
        user = ""
        if prompt:
            user += f"Instrucción: {prompt}\n\n"
        if schema:
            user += f"Esquema JSON:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
        else:
            user += "Devuelve un objeto JSON con los datos relevantes.\n\n"
        if len(parts) > 1:
            user += f"(Fragmento {i + 1} de {len(parts)}; extrae lo que haya en este fragmento.)\n\n"
        user += f"Contenido:\n{part}"
        raw = await chat(SYS_EXTRACT, user, provider=provider, model=model)
        try:
            data = _parse_json(raw)
        except ValueError:
            raw = await chat(SYS_EXTRACT, user + "\n\nTu respuesta anterior no era JSON válido. Devuelve solo JSON.",
                             provider=provider, model=model)
            data = _parse_json(raw)
        errs = validate(data, schema)
        if errs:
            raw = await chat(SYS_EXTRACT, user + f"\n\nCorrige estos errores de esquema: {errs}", provider=provider, model=model)
            try:
                data = _parse_json(raw)
            except ValueError:
                pass
        results.append(data)
    return merge(results)


def merge(results: list) -> Any:
    """Fusiona resultados de fragmentos: listas concatenadas sin duplicados, escalares el primero no nulo."""
    if not results:
        return None
    if len(results) == 1:
        return results[0]
    base = results[0]
    for r in results[1:]:
        base = _merge2(base, r)
    return base


def _merge2(a: Any, b: Any) -> Any:
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _merge2(out.get(k), v) if k in out else v
        return out
    if isinstance(a, list) and isinstance(b, list):
        seen = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in a}
        return a + [x for x in b if json.dumps(x, sort_keys=True, ensure_ascii=False) not in seen]
    return a if a not in (None, "", []) else b


async def summarize(markdown: str, *, lang: str = "es", provider: str | None = None, model: str | None = None) -> str:
    text = markdown[:80_000]
    return (await chat(
        f"Resume el contenido de una página web en 3-6 frases, en el idioma '{lang}' salvo que el contenido pida otro. "
        "Sin preámbulos.", text, json_mode=False, provider=provider, model=model)).strip()


async def answer(markdown: str, question: str, *, provider: str | None = None, model: str | None = None) -> str:
    return (await chat("Responde a la pregunta usando solo el contenido dado. Si no está, dilo.",
                       f"Pregunta: {question}\n\nContenido:\n{markdown[:80_000]}", json_mode=False,
                       provider=provider, model=model)).strip()
