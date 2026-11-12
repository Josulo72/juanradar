"""API REST (FastAPI). Compatible con Firecrawl v1/v2 en scrape, batch, crawl, map, search y extract,
más robots, trabajos, programación, monitores, exportación y claves. Documentación en /docs."""
from __future__ import annotations

import csv
import io
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from .. import __version__, db
from ..config import settings
from ..discovery import map_site, search
from ..engines.browser import manager as browser_manager
from ..extract import llm
from ..jobs import check_monitor, manager
from ..models import (AutoRobotRequest, BatchScrapeRequest, CrawlRequest, ExtractRequest, MapRequest, MonitorRequest,
                      RobotDefinition, RunRobotRequest, ScheduleRequest, ScrapeRequest, SearchRequest)
from ..robots.auto import build_robot
from ..robots.runner import run_robot
from ..scrape import scrape_url

STATIC = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.conn()
    await manager.start()
    yield
    await manager.stop()
    await browser_manager.close()


app = FastAPI(title="ScraperPro", version=__version__, lifespan=lifespan,
              description="Scraper universal: Firecrawl (incl. funciones de pago) + Maxun, mejorados.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

PUBLIC = re.compile(r"^/($|ui|eco$|docs|openapi\.json|redoc|health|files/|static/|favicon)")


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if PUBLIC.match(path) or request.method == "OPTIONS":
        return await call_next(request)
    if settings.auth_required or db.has_api_keys():
        hdr = request.headers.get("authorization", "")
        key = hdr.removeprefix("Bearer ").strip() if hdr.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
        is_local = request.client and request.client.host in ("127.0.0.1", "::1")
        if path == "/v1/keys" and request.method == "POST" and is_local and not db.has_api_keys():
            return await call_next(request)  # primera clave: solo desde este equipo
        if not key or not db.verify_api_key(key):
            return JSONResponse({"success": False, "error": "API key no válida o ausente"}, status_code=401)
    return await call_next(request)


def _dump(model) -> dict:
    return model.model_dump(by_alias=True, exclude_none=True)


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


# ====================================================================== básicos
@app.get("/", include_in_schema=False)
async def ui():
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/files/{name}", include_in_schema=False)
async def files(name: str):
    p = (settings.files_dir / name).resolve()
    if p.parent != settings.files_dir.resolve() or not p.exists():
        raise HTTPException(404, "no existe")
    return FileResponse(p)


@app.get("/health")
async def health():
    return {"ok": True, "version": __version__, "llm": llm.is_configured(), "proxies": len(settings.proxies)}


# ====================================================================== scrape
@app.post("/v1/scrape")
@app.post("/v2/scrape")
async def scrape(req: ScrapeRequest):
    try:
        doc = await scrape_url(req.url, _dump(req))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not doc.get("success"):
        md = doc.get("metadata", {})
        return {"success": False, "error": md.get("error") or (f"bloqueado: {md['blocked']}" if md.get("blocked") else "fallo"),
                "data": doc}
    return {"success": True, "data": doc}


@app.post("/v1/batch/scrape")
@app.post("/v2/batch/scrape")
async def batch_scrape(req: BatchScrapeRequest, request: Request):
    data = _dump(req)
    valid, invalid = [], []
    for u in data["urls"]:
        (valid if re.match(r"^https?://[^\s/]+\.[^\s]+", u) else invalid).append(u)
    if invalid and not req.ignoreInvalidURLs:
        raise HTTPException(400, f"URLs no válidas: {invalid}")
    data["urls"] = valid
    jid = manager.submit("batch_scrape", data)
    return {"success": True, "id": jid, "url": f"{_base(request)}/v1/batch/scrape/{jid}", "invalidURLs": invalid}


def _status_payload(jid: str, offset: int, limit: int, request: Request, route: str) -> dict:
    job = db.get_job(jid)
    if not job:
        raise HTTPException(404, "trabajo no encontrado")
    total_docs = db.count_documents(jid)
    docs = db.get_documents(jid, offset, limit)
    nxt = offset + limit
    return {
        "success": True, "status": "scraping" if job["status"] == "running" else job["status"],
        "total": max(job["total"] or 0, total_docs), "completed": job["completed"], "failed": job["failed"],
        "creditsUsed": job["completed"], "expiresAt": None,
        "next": f"{_base(request)}/v1/{route}/{jid}?offset={nxt}&limit={limit}" if nxt < total_docs else None,
        "data": docs, "error": job.get("error"),
    }


@app.get("/v1/batch/scrape/{jid}")
@app.get("/v2/batch/scrape/{jid}")
async def batch_status(jid: str, request: Request, offset: int = 0, limit: int = Query(100, le=1000)):
    return _status_payload(jid, offset, limit, request, "batch/scrape")


@app.delete("/v1/batch/scrape/{jid}")
@app.delete("/v1/crawl/{jid}")
@app.delete("/v2/crawl/{jid}")
async def cancel_job(jid: str):
    return {"success": manager.cancel(jid), "status": "cancelled"}


# ====================================================================== crawl
@app.post("/v1/crawl")
@app.post("/v2/crawl")
async def crawl(req: CrawlRequest, request: Request):
    data = _dump(req)
    jid = manager.submit("crawl", data)
    return {"success": True, "id": jid, "url": f"{_base(request)}/v1/crawl/{jid}"}


@app.get("/v1/crawl/{jid}")
@app.get("/v2/crawl/{jid}")
async def crawl_status(jid: str, request: Request, offset: int = 0, limit: int = Query(100, le=1000)):
    return _status_payload(jid, offset, limit, request, "crawl")


@app.get("/v1/crawl/{jid}/errors")
async def crawl_errors(jid: str):
    rows = db.query("SELECT url, state, attempts FROM frontier WHERE job_id=? AND state IN ('failed','skipped')", (jid,))
    return {"errors": [r for r in rows if r["state"] == "failed"], "skipped": [r["url"] for r in rows if r["state"] == "skipped"]}


# ====================================================================== map / search / extract
@app.post("/v1/map")
@app.post("/v2/map")
async def map_(req: MapRequest):
    items = await map_site(_dump(req))
    return {"success": True, "links": items}


@app.post("/v1/search")
@app.post("/v2/search")
async def search_(req: SearchRequest):
    return await search(_dump(req))


@app.post("/v1/extract")
@app.post("/v2/extract")
async def extract(req: ExtractRequest, wait: bool = False):
    if not llm.is_configured():
        raise HTTPException(400, "Extract necesita un LLM configurado (ver /health). Alternativa sin LLM: formato 'select'.")
    jid = manager.submit("extract", _dump(req))
    if wait:
        job = await manager.wait(jid, 900)
        return {"success": job["status"] == "completed", "id": jid, **(job.get("result") or {})}
    return {"success": True, "id": jid}


@app.get("/v1/extract/{jid}")
@app.get("/v2/extract/{jid}")
async def extract_status(jid: str):
    job = db.get_job(jid)
    if not job:
        raise HTTPException(404, "no encontrado")
    return {"success": job["status"] != "failed", "status": job["status"], "error": job.get("error"), **(job.get("result") or {})}


# ====================================================================== robots
def _robot_row(r: dict) -> dict:
    return {"id": r["id"], "name": r["name"], "definition": db.loads(r["definition"], {}),
            "createdAt": r["created_at"], "updatedAt": r["updated_at"]}


@app.get("/v1/robots")
async def list_robots():
    return {"success": True, "data": [_robot_row(r) for r in db.query("SELECT * FROM robots ORDER BY updated_at DESC")]}


@app.post("/v1/robots")
async def create_robot(definition: RobotDefinition):
    rid = db.new_id("robot")
    d = _dump(definition)
    db.execute("INSERT INTO robots(id,name,definition,created_at,updated_at) VALUES(?,?,?,?,?)",
               (rid, definition.name, db.dumps(d), db.now_iso(), db.now_iso()))
    return {"success": True, "id": rid}


@app.get("/v1/robots/{rid}")
async def get_robot(rid: str):
    r = db.one("SELECT * FROM robots WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404, "robot no encontrado")
    return {"success": True, "data": _robot_row(r)}


@app.put("/v1/robots/{rid}")
async def update_robot(rid: str, definition: RobotDefinition):
    if not db.one("SELECT id FROM robots WHERE id=?", (rid,)):
        raise HTTPException(404, "robot no encontrado")
    db.execute("UPDATE robots SET name=?, definition=?, updated_at=? WHERE id=?",
               (definition.name, db.dumps(_dump(definition)), db.now_iso(), rid))
    return {"success": True}


@app.delete("/v1/robots/{rid}")
async def delete_robot(rid: str):
    db.execute("DELETE FROM robots WHERE id=?", (rid,))
    return {"success": True}


@app.post("/v1/robots/{rid}/run")
async def run_saved_robot(rid: str, body: RunRobotRequest = Body(default_factory=RunRobotRequest)):
    if not db.one("SELECT id FROM robots WHERE id=?", (rid,)):
        raise HTTPException(404, "robot no encontrado")
    jid = manager.submit("robot", {"robot_id": rid, "params": body.params})
    if body.wait:
        job = await manager.wait(jid, 900)
        return {"success": job["status"] == "completed", "id": jid, "result": job.get("result"), "error": job.get("error")}
    return {"success": True, "id": jid}


@app.post("/v1/robots/test")
async def test_robot(definition: RobotDefinition, params: Optional[str] = None):
    """Ejecuta una definición sin guardarla (para probar mientras se diseña)."""
    res = await run_robot(_dump(definition), json.loads(params) if params else None)
    return {"success": res["status"] == "completed", "result": res}


@app.post("/v1/robots/auto")
async def auto_robot(req: AutoRobotRequest, save: bool = False):
    try:
        out = await build_robot(req.url, req.prompt, req.useBrowser)
    except Exception as e:
        raise HTTPException(422, str(e))
    if save:
        rid = db.new_id("robot")
        db.execute("INSERT INTO robots(id,name,definition,created_at,updated_at) VALUES(?,?,?,?,?)",
                   (rid, out["robot"]["name"], db.dumps(out["robot"]), db.now_iso(), db.now_iso()))
        out["id"] = rid
    return {"success": True, **out}


# ====================================================================== trabajos y exportación
@app.get("/v1/jobs")
async def jobs(kind: Optional[str] = None, limit: int = Query(50, le=500)):
    return {"success": True, "data": db.list_jobs(kind, limit)}


@app.get("/v1/jobs/{jid}")
async def job(jid: str):
    j = db.get_job(jid)
    if not j:
        raise HTTPException(404, "no encontrado")
    j["documents"] = db.count_documents(jid)
    return {"success": True, "data": j}


@app.delete("/v1/jobs/{jid}")
async def cancel(jid: str):
    return {"success": manager.cancel(jid)}


def _rows_for_export(jid: str) -> list[dict]:
    job = db.get_job(jid)
    if not job:
        raise HTTPException(404, "no encontrado")
    if job["kind"] == "robot":
        data = (job.get("result") or {}).get("data") or {}
        rows: list[dict] = []
        for name, val in data.items():
            for r in (val if isinstance(val, list) else [val]):
                rows.append({"_lista": name, **(r if isinstance(r, dict) else {"valor": r})})
        return rows
    out = []
    for d in db.get_documents(jid, 0, 100000):
        md = d.get("metadata") or {}
        row = {"url": md.get("url"), "title": md.get("title"), "statusCode": md.get("statusCode"),
               "markdown": d.get("markdown")}
        for k in ("json", "select", "summary"):
            if d.get(k) is not None:
                row[k] = d[k]
        out.append(row)
    return out


def _flatten(r: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in r.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif isinstance(v, list):
            out[key] = json.dumps(v, ensure_ascii=False) if v and isinstance(v[0], (dict, list)) else " | ".join(map(str, v))
        else:
            out[key] = v
    return out


@app.get("/v1/jobs/{jid}/export")
async def export(jid: str, format: str = Query("json", pattern="^(json|ndjson|csv|xlsx)$")):
    rows = _rows_for_export(jid)
    if format == "json":
        return Response(json.dumps(rows, ensure_ascii=False, indent=2, default=str), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{jid}.json"'})
    if format == "ndjson":
        body = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)
        return Response(body, media_type="application/x-ndjson", headers={"Content-Disposition": f'attachment; filename="{jid}.ndjson"'})
    flat = [_flatten(r) for r in rows]
    cols: list[str] = []
    for r in flat:
        cols += [c for c in r if c not in cols]
    if format == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(flat)
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{jid}.csv"'})
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "datos"
    ws.append(cols)
    for r in flat:
        ws.append([str(r.get(c))[:32000] if r.get(c) is not None else None for c in cols])
    bio = io.BytesIO()
    wb.save(bio)
    return Response(bio.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{jid}.xlsx"'})


@app.get("/v1/jobs/{jid}/stream")
async def stream(jid: str):
    """Server-Sent Events con el progreso del trabajo."""
    import asyncio

    async def gen():
        last = None
        for _ in range(3600):
            j = db.get_job(jid)
            if not j:
                yield "event: error\ndata: no encontrado\n\n"
                return
            snap = {k: j[k] for k in ("status", "total", "completed", "failed")}
            if snap != last:
                yield f"data: {json.dumps(snap)}\n\n"
                last = snap
            if j["status"] in ("completed", "failed", "cancelled"):
                return
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ====================================================================== programación
@app.get("/v1/schedules")
async def schedules():
    rows = db.query("SELECT * FROM schedules ORDER BY created_at DESC")
    for r in rows:
        r["target"] = db.loads(r["target"], r["target"])
    return {"success": True, "data": rows}


@app.post("/v1/schedules")
async def create_schedule(req: ScheduleRequest):
    from apscheduler.triggers.cron import CronTrigger

    try:
        CronTrigger.from_crontab(req.cron, timezone=req.timezone)
    except ValueError as e:
        raise HTTPException(400, f"cron no válido: {e}")
    sid = db.new_id("sched")
    row = {"id": sid, "target_kind": req.target_kind, "target": db.dumps(req.target), "cron": req.cron,
           "timezone": req.timezone, "enabled": 1 if req.enabled else 0, "created_at": db.now_iso()}
    db.execute("INSERT INTO schedules(id,target_kind,target,cron,timezone,enabled,created_at) VALUES(?,?,?,?,?,?,?)",
               tuple(row.values()))
    nxt = manager.add_schedule(row) if req.enabled else None
    return {"success": True, "id": sid, "nextRunAt": nxt}


@app.delete("/v1/schedules/{sid}")
async def delete_schedule(sid: str):
    manager.remove_schedule(f"schedule:{sid}")
    db.execute("DELETE FROM schedules WHERE id=?", (sid,))
    return {"success": True}


# ====================================================================== monitores de cambios
@app.get("/v1/monitors")
async def monitors():
    rows = db.query("SELECT * FROM monitors ORDER BY created_at DESC")
    for r in rows:
        r["config"] = db.loads(r["config"], {})
    return {"success": True, "data": rows}


@app.post("/v1/monitors")
async def create_monitor(req: MonitorRequest):
    mid = db.new_id("mon")
    cfg = _dump(req)
    db.execute("INSERT INTO monitors(id,name,config,created_at) VALUES(?,?,?,?)", (mid, req.name, db.dumps(cfg), db.now_iso()))
    if req.cron:
        manager._add_cron(f"monitor:{mid}", req.cron, "Europe/Madrid", manager._fire_monitor, mid)
    first = await check_monitor(mid)  # línea base
    return {"success": True, "id": mid, "baseline": first}


@app.post("/v1/monitors/{mid}/check")
async def run_monitor(mid: str):
    return {"success": True, "data": await check_monitor(mid)}


@app.delete("/v1/monitors/{mid}")
async def delete_monitor(mid: str):
    manager.remove_schedule(f"monitor:{mid}")
    db.execute("DELETE FROM monitors WHERE id=?", (mid,))
    return {"success": True}


# ====================================================================== claves y estado
@app.post("/v1/keys")
async def create_key(name: str = Body("default", embed=True)):
    key = db.create_api_key(name)
    return {"success": True, "key": key, "note": "Guárdala ahora: no se vuelve a mostrar (se almacena solo su hash)."}


@app.get("/v1/llm")
async def llm_status():
    return {"success": True, **llm.describe()}


@app.post("/v1/llm/test")
async def llm_test():
    """Hace una llamada mínima al LLM configurado para comprobar URL, modelo y clave."""
    try:
        out = await llm.chat("Responde solo con JSON.", 'Devuelve {"ok": true}', max_tokens=20)
        return {"success": True, "respuesta": out[:200], **llm.describe()}
    except llm.LLMNotConfigured as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"El proveedor respondió con error: {type(e).__name__}: {str(e)[:300]}")


@app.get("/v1/stats")
async def stats():
    by = db.query("SELECT kind, status, COUNT(*) n FROM jobs GROUP BY kind, status")
    return {"success": True, "jobs": by, "robots": db.one("SELECT COUNT(*) n FROM robots")["n"],
            "schedules": db.one("SELECT COUNT(*) n FROM schedules")["n"], "monitors": db.one("SELECT COUNT(*) n FROM monitors")["n"],
            "domainsLearned": db.one("SELECT COUNT(*) n FROM domain_profiles")["n"],
            "webhooks": db.query("SELECT status, COUNT(*) n FROM webhook_deliveries GROUP BY status"),
            "llm": llm.is_configured()}
