"""Intérprete de robots: DSL legible en orden natural (mejora del workflow inverso de Maxun).

Pasos disponibles (cada paso es un objeto con una clave):
  {"goto": "https://...{{param}}"}
  {"click": "selector", "text": "Texto visible (respaldo)", "optional": true}
  {"fill": {"selector": "#q", "value": "{{query}}"}}   {"type": {...}} (teclea con retardo)
  {"press": "Enter"}   {"select": {"selector": "...", "value": "..."}}   {"hover": "selector"}
  {"wait": 1500} | {"wait": "selector"} | {"wait": "networkidle"}
  {"scroll": {"times": 5, "until": "selector-opcional"}}
  {"extract": {"name": "items", "list": "article", "fields": {...}, "limit": 200, "paginate": {...}}}
  {"extractOne": {"name": "ficha", "fields": {...}}}
  {"if": {"exists": "sel" | "urlMatches": "rx" | "textContains": "txt"}, "then": [...], "else": [...]}
  {"foreach": {"from": "items", "field": "url", "limit": 20, "steps": [...]}}  (visita cada ficha)
  {"loop": {"times": 3, "steps": [...]}}   {"while": {"exists": "sel", "max": 20, "steps": [...]}}
  {"screenshot": {"name": "home", "fullPage": true}}   {"script": "return document.title"}
  {"login": {"url": "...", "user": {"selector": "#u", "env": "MI_USUARIO"},
             "password": {"selector": "#p", "env": "MI_CLAVE"}, "submit": "button[type=submit]",
             "successSelector": ".logout", "saveProfile": "mi-sitio"}}
Paginación: {"type": "next", "selector": "li.next a", "text": "Siguiente", "max": 10}
            {"type": "loadMore", "selector": "button.more", "max": 20}
            {"type": "scroll", "max": 30}
            {"type": "url", "template": "https://x.com/p/{page}", "start": 2, "max": 10}
            {"type": "links", "selector": ".pagination a"}  (recorre todos los enlaces de paginación)
Campos: como el formato "select"; además una lista de selectores = respaldo en orden (autorreparación).
Credenciales: SOLO por variable de entorno ({"env": "NOMBRE"}); nunca se guardan en el robot.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from urllib.parse import urljoin

from .. import db
from ..config import settings
from ..engines.browser import _robust_click, manager, profile_path, smart_wait
from ..engines.detect import detect_block
from ..engines.proxies import pool
from ..extract.select import detect_lists, extract_fields

_VAR = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


class RobotError(Exception):
    pass


def render(obj: Any, params: dict) -> Any:
    if isinstance(obj, str):
        def rep(m):
            v = params
            for part in m.group(1).split("."):
                v = v.get(part, "") if isinstance(v, dict) else ""
            return str(v)
        return _VAR.sub(rep, obj)
    if isinstance(obj, list):
        return [render(x, params) for x in obj]
    if isinstance(obj, dict):
        # no se renderizan los pasos anidados aquí (se renderizan al ejecutarlos con las vars de ese momento)
        return {k: (v if k == "steps" else render(v, params)) for k, v in obj.items()}
    return obj


class RobotRun:
    def __init__(self, definition: dict, params: dict | None = None, job_id: str | None = None):
        self.d = definition
        self.params = {**(definition.get("params") or {}), **(params or {})}
        self.job_id = job_id
        self.opts = definition.get("options") or {}
        self.data: dict[str, Any] = {}
        self.logs: list[dict] = []
        self.screenshots: list[str] = []
        self.network: list[dict] = []
        self.page = None
        self.ctx = None
        self.started = time.time()
        self.max_seconds = int(self.opts.get("maxSeconds", 900))

    def log(self, msg: str, **kw) -> None:
        entry = {"t": round(time.time() - self.started, 2), "msg": msg, **kw}
        self.logs.append(entry)
        if self.job_id:
            db.update_job(self.job_id, result={"progress": self.logs[-5:], "items": self._count()})

    def _count(self) -> dict:
        return {k: (len(v) if isinstance(v, list) else 1) for k, v in self.data.items()}

    # ---------------------------------------------------------------- ciclo de vida
    async def run(self) -> dict:
        proxy = pool.next() if self.opts.get("proxy") in ("rotate", "basic", "stealth") else None
        self.ctx = await manager.new_context(
            proxy=proxy, mobile=bool(self.opts.get("mobile")), country=self.opts.get("country"),
            languages=self.opts.get("languages"), headers=self.opts.get("headers"), cookies=self.opts.get("cookies"),
            profile=self.opts.get("profile"),
        )
        self.page = await self.ctx.new_page()
        self.page.set_default_timeout(int(self.opts.get("timeout", 30000)))
        if self.opts.get("captureNetwork"):
            self.page.on("response", self._on_response)
        try:
            if self.d.get("startUrl"):
                await self.step({"goto": self.d["startUrl"]}, "inicio")
            await self.steps(self.d.get("steps") or [], "steps")
            if self.opts.get("profile") and self.opts.get("saveProfile", True):
                await self.ctx.storage_state(path=str(profile_path(self.opts["profile"])))
            problems = self.validate()
            status = "completed" if not problems else "invalid"
            if problems:
                self.log("validación fallida", problems=problems)
                await self._failure_snapshot("validacion")
            return self.result(status, problems=problems)
        except Exception as e:
            self.log("error", error=str(e))
            await self._failure_snapshot("error")
            return self.result("failed", error=str(e))
        finally:
            try:
                await self.ctx.close()
            except Exception:
                pass

    def result(self, status: str, **extra) -> dict:
        return {"status": status, "data": self.data, "counts": self._count(), "screenshots": self.screenshots,
                "network": self.network if self.opts.get("captureNetwork") else None,
                "logs": self.logs, "elapsedSeconds": round(time.time() - self.started, 1), **extra}

    async def _on_response(self, resp) -> None:
        try:
            if resp.request.resource_type in ("xhr", "fetch") and "json" in resp.headers.get("content-type", ""):
                if len(self.network) < 300:
                    self.network.append({"url": resp.url, "status": resp.status, "json": json.loads(await resp.body())})
        except Exception:
            pass

    async def _failure_snapshot(self, label: str) -> None:
        """Ante un fallo guarda captura y HTML para poder diagnosticar (Maxun no lo hace)."""
        try:
            png = await self.page.screenshot(full_page=True)
            name = f"robot_{label}_{int(time.time())}.png"
            (settings.files_dir / name).write_bytes(png)
            html_name = name.replace(".png", ".html")
            (settings.files_dir / html_name).write_text(await self.page.content(), encoding="utf-8")
            self.screenshots.append(f"/files/{name}")
            self.log("instantánea de diagnóstico guardada", screenshot=f"/files/{name}", html=f"/files/{html_name}")
        except Exception:
            pass

    def validate(self) -> list[str]:
        v = self.d.get("validate") or self.d.get("validate_") or {}
        problems = []
        for name, n in (v.get("minItems") or {}).items():
            got = len(self.data.get(name) or [])
            if got < int(n):
                problems.append(f"'{name}': {got} elementos (mínimo {n})")
        for name, fields in (v.get("required") or {}).items():
            rows = self.data.get(name) or []
            rows = rows if isinstance(rows, list) else [rows]
            for f in fields:
                missing = sum(1 for r in rows if isinstance(r, dict) and r.get(f) in (None, "", []))
                if rows and missing / len(rows) > float(v.get("maxMissingRatio", 0.2)):
                    problems.append(f"'{name}.{f}': vacío en {missing}/{len(rows)} filas")
        return problems

    # ---------------------------------------------------------------- pasos
    async def steps(self, steps: list[dict], path: str) -> None:
        for i, st in enumerate(steps):
            if time.time() - self.started > self.max_seconds:
                raise RobotError(f"tiempo máximo ({self.max_seconds}s) superado")
            await self.step(st, f"{path}[{i}]")

    async def step(self, raw: dict, where: str) -> None:
        optional = bool(raw.get("optional"))
        st = render({k: v for k, v in raw.items() if k != "optional"}, self.params)
        kind = next(iter(st.keys() - {"text", "name", "timeout"}), None)
        if kind is None:
            return
        arg = st[kind]
        p = self.page
        try:
            if kind == "goto":
                await p.goto(arg, wait_until="domcontentloaded")
                await smart_wait(p, 8000)
                block = detect_block(200, await p.content())
                if block:
                    self.log("aviso: posible bloqueo antibot", tipo=block, url=p.url)
            elif kind == "click":
                await _robust_click(p, arg, st.get("text"))
                await smart_wait(p, 5000)
            elif kind in ("fill", "type"):
                sel, val = arg["selector"], str(self._value(arg))
                if kind == "fill":
                    await p.fill(sel, val)
                else:
                    await p.click(sel)
                    await p.keyboard.type(val, delay=40)
            elif kind == "press":
                await p.keyboard.press(arg)
                await smart_wait(p, 5000)
            elif kind == "select":
                await p.select_option(arg["selector"], arg.get("value"))
            elif kind == "hover":
                await p.hover(arg)
            elif kind == "wait":
                if isinstance(arg, (int, float)):
                    await p.wait_for_timeout(min(int(arg), 60000))
                elif arg == "networkidle":
                    await smart_wait(p, 15000)
                else:
                    await p.wait_for_selector(arg, timeout=int(st.get("timeout", 20000)))
            elif kind == "scroll":
                await self._scroll(int(arg.get("times", 5)) if isinstance(arg, dict) else int(arg),
                                   arg.get("until") if isinstance(arg, dict) else None)
            elif kind == "extract":
                await self._extract_list(arg)
            elif kind == "extractOne":
                html = await p.content()
                row = extract_fields(_root(html), arg["fields"], p.url)
                self._store(arg.get("name", "data"), row, single=True)
            elif kind == "if":
                ok = await self._cond(arg)
                branch = "then" if ok else "else"
                await self.steps(raw.get(branch) or [], f"{where}.{branch}")
            elif kind == "foreach":
                await self._foreach(raw["foreach"], where)
            elif kind == "loop":
                for n in range(int(arg.get("times", 1))):
                    self.params["loop"] = n
                    await self.steps(raw["loop"].get("steps") or [], f"{where}.loop{n}")
            elif kind == "while":
                for n in range(int(arg.get("max", 20))):
                    if not await self._cond(arg):
                        break
                    await self.steps(raw["while"].get("steps") or [], f"{where}.while{n}")
            elif kind == "screenshot":
                a = arg if isinstance(arg, dict) else {}
                png = await p.screenshot(full_page=bool(a.get("fullPage", True)))
                name = f"robot_{a.get('name', 'captura')}_{int(time.time() * 1000)}.png"
                (settings.files_dir / name).write_bytes(png)
                self.screenshots.append(f"/files/{name}")
            elif kind == "script":
                js = arg if arg.strip().startswith(("(", "()", "async")) else f"() => {{ {arg} }}"
                val = await p.evaluate(js)
                self._store(st.get("name", "script"), val, single=True)
            elif kind == "login":
                await self._login(arg)
            elif kind == "set":
                self.params.update(arg)
            else:
                raise RobotError(f"paso desconocido: {kind}")
            self.log(f"ok {kind}", where=where)
        except Exception as e:
            if optional:
                self.log(f"opcional omitido {kind}", where=where, error=str(e).splitlines()[0][:200])
                return
            raise RobotError(f"{where} ({kind}): {str(e).splitlines()[0][:300]}") from e

    def _value(self, arg: dict) -> str:
        if "env" in arg:
            v = os.environ.get(arg["env"])
            if v is None:
                raise RobotError(f"falta la variable de entorno {arg['env']}")
            return v
        return arg.get("value", arg.get("text", ""))

    async def _login(self, a: dict) -> None:
        p = self.page
        if a.get("url"):
            await p.goto(a["url"], wait_until="domcontentloaded")
            await smart_wait(p, 6000)
        if a.get("successSelector") and await p.query_selector(a["successSelector"]):
            self.log("sesión ya iniciada (perfil)")
            return
        await p.fill(a["user"]["selector"], self._value(a["user"]))
        await p.fill(a["password"]["selector"], self._value(a["password"]))
        await _robust_click(p, a.get("submit"), a.get("submitText"))
        await smart_wait(p, 10000)
        if a.get("successSelector"):
            await p.wait_for_selector(a["successSelector"], timeout=20000)
        if a.get("saveProfile"):
            await self.ctx.storage_state(path=str(profile_path(a["saveProfile"])))

    async def _cond(self, c: dict) -> bool:
        p = self.page
        if "exists" in c:
            el = await p.query_selector(c["exists"])
            return bool(el and await el.is_visible())
        if "notExists" in c:
            return await p.query_selector(c["notExists"]) is None
        if "urlMatches" in c:
            return re.search(c["urlMatches"], p.url) is not None
        if "textContains" in c:
            return c["textContains"].lower() in (await p.inner_text("body")).lower()
        return False

    async def _scroll(self, times: int, until: str | None = None) -> None:
        p = self.page
        last = 0
        for _ in range(times):
            await p.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await p.wait_for_timeout(700)
            await smart_wait(p, 3000)
            h = await p.evaluate("document.body.scrollHeight")
            if until and await p.query_selector(until):
                break
            if h == last:
                break
            last = h

    # ---------------------------------------------------------------- extracción con paginación
    async def _extract_list(self, a: dict) -> None:
        name = a.get("name", "items")
        limit = int(a.get("limit", 1000))
        dedupe_on = a.get("dedupeBy")
        pag = a.get("paginate") or {}
        seen_keys: set = set()
        visited_pages: set[str] = set()
        rows_total = 0
        page_n = int(pag.get("start", 2))
        pages = int(pag.get("max", 10)) + 1 if pag else 1
        for it in range(pages):
            html = await self.page.content()
            rows = self._rows(html, a)
            new = 0
            for r in rows:
                key = json.dumps(r.get(dedupe_on) if dedupe_on else r, sort_keys=True, ensure_ascii=False, default=str)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                self._store(name, r)
                new += 1
                rows_total += 1
                if rows_total >= limit:
                    break
            self.log(f"extraídos {new} de la página {it + 1}", name=name, url=self.page.url)
            visited_pages.add(self.page.url)
            if not pag or rows_total >= limit:
                break
            if it > 0 and new == 0 and pag.get("type") != "url":
                break  # sin novedades: fin de la paginación
            if not await self._next_page(pag, page_n, visited_pages):
                break
            page_n += 1

    def _rows(self, html: str, a: dict) -> list[dict]:
        root = _root(html)
        list_sel = a.get("list")
        sels = list_sel if isinstance(list_sel, list) else [list_sel] if list_sel else []
        items = []
        for s in sels:
            items = _css_or_xpath(root, s)
            if items:
                break
        if not items and a.get("autoHeal", True):
            # Autorreparación: la estructura cambió → buscar la lista que mejor cubre los campos pedidos
            cands = detect_lists(html, self.page.url)
            wanted = set(a.get("fields", {}).keys())
            best = max(cands, key=lambda c: len(wanted & set(c["fields"])) + c["count"] / 1000, default=None)
            if best:
                self.log("autorreparación: selector de lista sustituido", old=list_sel, new=best["listSelector"])
                items = _css_or_xpath(root, best["listSelector"])
        rows = []
        for el in items:
            row = extract_fields(el, a.get("fields", {}), self.page.url)
            if any(v not in (None, "", []) for v in row.values()):
                rows.append(row)
        return rows

    async def _next_page(self, pag: dict, page_n: int, visited: set[str]) -> bool:
        p = self.page
        t = pag.get("type", "next")
        try:
            if t == "url":
                url = pag["template"].replace("{page}", str(page_n))
                await p.goto(url, wait_until="domcontentloaded")
                await smart_wait(p, 8000)
                return True
            if t == "scroll":
                h = await p.evaluate("document.body.scrollHeight")
                await self._scroll(1)
                return (await p.evaluate("document.body.scrollHeight")) > h
            if t == "loadMore":
                await _robust_click(p, pag.get("selector"), pag.get("text"))
                await smart_wait(p, 6000)
                return True
            if t in ("next", "links"):
                href = None
                if pag.get("selector"):
                    el = await p.query_selector(pag["selector"])
                    if el:
                        href = await el.get_attribute("href")
                if not href:
                    from ..transform.meta import extract_links

                    _, info = extract_links(await p.content(), p.url)
                    cand = [u for u in info["pagination"] if u not in visited]
                    href = cand[0] if cand else None
                if href and not href.startswith("javascript"):
                    url = urljoin(p.url, href)
                    if url in visited:
                        return False
                    await p.goto(url, wait_until="domcontentloaded")
                    await smart_wait(p, 8000)
                    return True
                if pag.get("selector") or pag.get("text"):
                    await _robust_click(p, pag.get("selector"), pag.get("text"))
                    await smart_wait(p, 6000)
                    return p.url not in visited or True
        except Exception as e:
            self.log("fin de paginación", motivo=str(e).splitlines()[0][:150])
        return False

    async def _foreach(self, a: dict, where: str) -> None:
        src = self.data.get(a.get("from", "items")) or []
        field = a.get("field", "url")
        limit = int(a.get("limit", 50))
        merge = a.get("merge", True)
        for i, row in enumerate(list(src)[:limit]):
            url = row.get(field) if isinstance(row, dict) else row
            if not url:
                continue
            self.params["item"] = row
            await self.page.goto(urljoin(self.page.url, url), wait_until="domcontentloaded")
            await smart_wait(self.page, 8000)
            snapshot_keys = set(self.data)
            await self.steps(a.get("steps") or [], f"{where}.foreach{i}")
            if merge and isinstance(row, dict):
                # lo extraído con extractOne dentro del bucle se funde en la fila
                for k in set(self.data) - snapshot_keys:
                    val = self.data.pop(k)
                    row.update(val if isinstance(val, dict) else {k: val})

    def _store(self, name: str, value: Any, single: bool = False) -> None:
        if single:
            self.data[name] = value
        else:
            self.data.setdefault(name, []).append(value)


def _root(html: str):
    from ..transform.clean import parse

    return parse(html)


def _css_or_xpath(root, sel: str) -> list:
    try:
        return root.xpath(sel) if sel.startswith(("/", "(")) else root.cssselect(sel)
    except Exception:
        return []


async def run_robot(definition: dict, params: dict | None = None, job_id: str | None = None) -> dict:
    return await RobotRun(definition, params, job_id).run()
