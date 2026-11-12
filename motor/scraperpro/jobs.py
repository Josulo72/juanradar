"""Gestor de trabajos asíncronos, programador cron y monitores de cambios.

Los trabajos viven en SQLite: al reiniciar, los que estaban en cola o en marcha se reanudan
(el crawl continúa desde su frontera). Maxun bloquea la petición HTTP hasta que el robot termina;
aquí todo es asíncrono (id + consulta o webhook) con opción de espera.
"""
from __future__ import annotations

import asyncio
import traceback
from datetime import datetime
from typing import Any

from . import db, webhooks
from .crawler import run_crawl
from .extract_service import run_extract
from .robots.runner import run_robot
from .scrape import scrape_url


class JobManager:
    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task] = {}
        self.stop_event = asyncio.Event()
        self.scheduler = None
        self._bg: list[asyncio.Task] = []

    # ---------------------------------------------------------------- arranque / parada
    async def start(self) -> None:
        self.stop_event = asyncio.Event()
        self._bg.append(asyncio.create_task(webhooks.dispatcher_loop(self.stop_event)))
        for j in db.query("SELECT id FROM jobs WHERE status IN ('queued','running')"):
            self.launch(j["id"])
        self._start_scheduler()

    async def stop(self) -> None:
        self.stop_event.set()
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        for t in list(self.tasks.values()) + self._bg:
            t.cancel()

    # ---------------------------------------------------------------- trabajos
    def submit(self, kind: str, request: dict, parent_id: str | None = None) -> str:
        jid = db.create_job(kind, request, parent_id)
        self.launch(jid)
        return jid

    def launch(self, jid: str) -> None:
        if jid in self.tasks and not self.tasks[jid].done():
            return
        t = asyncio.create_task(self._run(jid))
        self.tasks[jid] = t
        t.add_done_callback(lambda _t, j=jid: self.tasks.pop(j, None))

    async def wait(self, jid: str, timeout: float = 600) -> dict | None:
        t = self.tasks.get(jid)
        if t:
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout)
            except asyncio.TimeoutError:
                pass
        return db.get_job(jid)

    def cancel(self, jid: str) -> bool:
        job = db.get_job(jid)
        if not job or job["status"] in ("completed", "failed", "cancelled"):
            return False
        db.update_job(jid, status="cancelled")
        t = self.tasks.get(jid)
        if t and job["kind"] != "crawl":  # el crawl se detiene solo al ver el estado
            t.cancel()
        return True

    async def _run(self, jid: str) -> None:
        job = db.get_job(jid)
        if not job:
            return
        kind, req = job["kind"], job["request"]
        if job["status"] == "queued":
            db.update_job(jid, status="running")
        try:
            if kind == "crawl":
                await run_crawl(jid)
                return
            if kind == "batch_scrape":
                await self._batch(jid, req)
                return
            if kind == "scrape":
                doc = await scrape_url(req["url"], req)
                db.add_document(jid, req["url"], doc)
                db.update_job(jid, status="completed", result={"success": doc.get("success")}, total=1, completed=1)
                return
            if kind == "extract":
                res = await run_extract(req)
                db.update_job(jid, status="completed" if res.get("success") else "failed", result=res)
                webhooks.emit(req.get("webhook"), "extract.completed", {"id": jid, **res})
                return
            if kind == "robot":
                await self._robot(jid, req)
                return
            if kind == "monitor":
                res = await check_monitor(req["monitor_id"])
                db.update_job(jid, status="completed", result=res)
                return
            db.update_job(jid, status="failed", error=f"tipo de trabajo desconocido: {kind}")
        except asyncio.CancelledError:
            db.update_job(jid, status="cancelled")
            raise
        except Exception as e:
            db.update_job(jid, status="failed", error=f"{type(e).__name__}: {e}",
                          result={"traceback": traceback.format_exc()[-2000:]})

    async def _batch(self, jid: str, req: dict) -> None:
        urls = list(dict.fromkeys(req.get("urls") or []))
        done_urls = {r["url"] for r in db.query("SELECT url FROM documents WHERE job_id=?", (jid,))}
        todo = [u for u in urls if u not in done_urls]
        opts = {k: v for k, v in req.items() if k not in ("urls", "webhook", "maxConcurrency")}
        sem = asyncio.Semaphore(int(req.get("maxConcurrency") or 4))
        db.update_job(jid, total=len(urls))
        webhooks.emit(req.get("webhook"), "batch_scrape.started", {"id": jid, "success": True})

        async def one(u: str) -> None:
            async with sem:
                if (db.get_job(jid) or {}).get("status") == "cancelled":
                    return
                try:
                    doc = await scrape_url(u, opts)
                except Exception as e:
                    doc = {"success": False, "metadata": {"sourceURL": u, "error": str(e)}}
                db.add_document(jid, u, doc)
                db.bump_job(jid, completed=1 if doc.get("success") else 0, failed=0 if doc.get("success") else 1)
                webhooks.emit(req.get("webhook"), "batch_scrape.page", {"id": jid, "success": doc.get("success"), "data": [doc]})

        await asyncio.gather(*[one(u) for u in todo])
        if (db.get_job(jid) or {}).get("status") != "cancelled":
            db.update_job(jid, status="completed")
            webhooks.emit(req.get("webhook"), "batch_scrape.completed", {"id": jid, "success": True})

    async def _robot(self, jid: str, req: dict) -> None:
        row = db.one("SELECT * FROM robots WHERE id=?", (req["robot_id"],))
        if not row:
            db.update_job(jid, status="failed", error="robot no encontrado")
            return
        definition = db.loads(row["definition"], {})
        res = await run_robot(definition, req.get("params"), job_id=jid)
        status = "completed" if res["status"] == "completed" else "failed"
        db.update_job(jid, status=status, result=res, error=res.get("error") or (", ".join(res.get("problems") or []) or None))
        webhooks.emit(definition.get("webhook") or req.get("webhook"), f"robot.{res['status']}",
                      {"id": jid, "robotId": req["robot_id"], "data": res.get("data"), "counts": res.get("counts")})

    # ---------------------------------------------------------------- programador
    def _start_scheduler(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self.scheduler = AsyncIOScheduler()
        self.scheduler.start()
        for s in db.query("SELECT * FROM schedules WHERE enabled=1"):
            self.add_schedule(s)
        for m in db.query("SELECT * FROM monitors WHERE enabled=1"):
            cfg = db.loads(m["config"], {})
            if cfg.get("cron"):
                self._add_cron(f"monitor:{m['id']}", cfg["cron"], "Europe/Madrid", self._fire_monitor, m["id"])

    def _add_cron(self, job_key: str, cron: str, tz: str, func, *args) -> datetime | None:
        from apscheduler.triggers.cron import CronTrigger

        if not self.scheduler:
            return None
        trig = CronTrigger.from_crontab(cron, timezone=tz)
        j = self.scheduler.add_job(func, trig, args=list(args), id=job_key, replace_existing=True, misfire_grace_time=600)
        return j.next_run_time

    def add_schedule(self, s: dict) -> str | None:
        nxt = self._add_cron(f"schedule:{s['id']}", s["cron"], s.get("timezone") or "Europe/Madrid", self._fire_schedule, s["id"])
        if nxt:
            db.execute("UPDATE schedules SET next_run_at=? WHERE id=?", (nxt.isoformat(), s["id"]))
            return nxt.isoformat()
        return None

    def remove_schedule(self, key: str) -> None:
        if self.scheduler:
            try:
                self.scheduler.remove_job(key)
            except Exception:
                pass

    async def _fire_schedule(self, sid: str) -> None:
        s = db.one("SELECT * FROM schedules WHERE id=?", (sid,))
        if not s or not s["enabled"]:
            return
        target = db.loads(s["target"], s["target"])
        kind = s["target_kind"]
        if kind == "robot":
            rid = target if isinstance(target, str) else target.get("robot_id")
            params = {} if isinstance(target, str) else target.get("params", {})
            self.submit("robot", {"robot_id": rid, "params": params, "schedule_id": sid})
        elif kind == "monitor":
            self.submit("monitor", {"monitor_id": target if isinstance(target, str) else target["monitor_id"]})
        else:
            self.submit(kind, target if isinstance(target, dict) else {"url": target})
        db.execute("UPDATE schedules SET last_run_at=? WHERE id=?", (db.now_iso(), sid))

    async def _fire_monitor(self, mid: str) -> None:
        self.submit("monitor", {"monitor_id": mid})


async def check_monitor(mid: str) -> dict:
    m = db.one("SELECT * FROM monitors WHERE id=?", (mid,))
    if not m:
        raise ValueError("monitor no encontrado")
    cfg = db.loads(m["config"], {})
    opts = dict(cfg.get("scrapeOptions") or {})
    mode = cfg.get("mode", "markdown")
    ct: dict[str, Any] = {"type": "changeTracking", "modes": ["git-diff"], "tag": f"monitor:{mid}",
                          "ignorePatterns": cfg.get("ignorePatterns")}
    if mode == "json":
        ct["modes"].append("json")
        ct["schema"] = cfg.get("schema")
        ct["prompt"] = cfg.get("prompt")
    opts["formats"] = ["markdown", ct]
    results = []
    changed = 0
    for url in cfg.get("urls") or []:
        doc = await scrape_url(url, opts)
        info = doc.get("changeTracking") or {}
        results.append({"url": url, "status": info.get("changeStatus", "error"), "diff": info.get("diff"),
                        "json": info.get("json"), "error": doc.get("metadata", {}).get("error")})
        if info.get("changeStatus") in ("changed", "removed"):
            changed += 1
    summary = {"checkedAt": db.now_iso(), "changed": changed, "total": len(results), "results": results}
    db.execute("UPDATE monitors SET last_status=?, last_checked_at=? WHERE id=?",
               ("changed" if changed else "same", db.now_iso(), mid))
    if changed:
        webhooks.emit(cfg.get("webhook"), "monitor.changed", {"monitorId": mid, "name": m["name"], **summary})
    return summary


manager = JobManager()
