"""Webhooks firmados (HMAC-SHA256) con reintentos persistentes en BD.

Maxun: sin firma y reintentos en memoria (se pierden al reiniciar). Aquí sobreviven a reinicios.
Cabeceras: X-ScraperPro-Signature y, por compatibilidad, X-Firecrawl-Signature (sha256=<hex>).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from typing import Any

import httpx

from . import db
from .config import settings

MAX_ATTEMPTS = 6


def _spec(webhook: Any) -> dict | None:
    if not webhook:
        return None
    if isinstance(webhook, str):
        return {"url": webhook}
    return dict(webhook)


def emit(webhook: Any, event: str, data: dict) -> None:
    """Encola un evento. `webhook` = url o {url, headers, metadata, events}."""
    spec = _spec(webhook)
    if not spec or not spec.get("url"):
        return
    events = spec.get("events")
    short = event.split(".")[-1]
    if events and short not in events and event not in events:
        return
    payload = {"type": event, "timestamp": db.now_iso(), "metadata": spec.get("metadata") or {}, **data}
    db.execute(
        "INSERT INTO webhook_deliveries(id,url,event,payload,headers,next_attempt_ts,created_at) VALUES(?,?,?,?,?,?,?)",
        (db.new_id("wh"), spec["url"], event, db.dumps(payload), db.dumps(spec.get("headers") or {}), time.time(), db.now_iso()),
    )


def sign(body: bytes) -> str:
    secret = (settings.webhook_secret or "").encode()
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


async def deliver_pending(limit: int = 50) -> int:
    rows = db.query(
        "SELECT * FROM webhook_deliveries WHERE status='pending' AND next_attempt_ts<=? ORDER BY next_attempt_ts LIMIT ?",
        (time.time(), limit),
    )
    if not rows:
        return 0
    async with httpx.AsyncClient(timeout=15) as c:
        for w in rows:
            body = w["payload"].encode()
            headers = {"Content-Type": "application/json", **(db.loads(w["headers"], {}) or {})}
            if settings.webhook_secret:
                headers["X-ScraperPro-Signature"] = headers["X-Firecrawl-Signature"] = sign(body)
            attempts = w["attempts"] + 1
            try:
                r = await c.post(w["url"], content=body, headers=headers)
                if r.status_code < 300:
                    db.execute("UPDATE webhook_deliveries SET status='delivered', attempts=? WHERE id=?", (attempts, w["id"]))
                    continue
                err = f"HTTP {r.status_code}"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            if attempts >= MAX_ATTEMPTS:
                db.execute("UPDATE webhook_deliveries SET status='failed', attempts=?, last_error=? WHERE id=?",
                           (attempts, err, w["id"]))
            else:
                delay = min(5 * (2 ** (attempts - 1)), 3600)
                db.execute("UPDATE webhook_deliveries SET attempts=?, last_error=?, next_attempt_ts=? WHERE id=?",
                           (attempts, err, time.time() + delay, w["id"]))
    return len(rows)


async def dispatcher_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await deliver_pending()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass


def verify_signature(body: bytes, header: str) -> bool:
    """Para el receptor: comprueba la firma de un webhook recibido."""
    return hmac.compare_digest(sign(body), header or "")
