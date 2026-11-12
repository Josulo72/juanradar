"""Persistencia en SQLite (WAL). Tablas con columnas indexables + JSON para el resto."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import settings

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
  request TEXT NOT NULL, result TEXT, error TEXT,
  total INTEGER DEFAULT 0, completed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
  parent_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, kind);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, url TEXT NOT NULL,
  data TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_documents_job ON documents(job_id, id);
CREATE TABLE IF NOT EXISTS frontier (
  job_id TEXT NOT NULL, url_key TEXT NOT NULL, url TEXT NOT NULL, depth INTEGER NOT NULL,
  priority REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER DEFAULT 0,
  PRIMARY KEY (job_id, url_key)
);
CREATE INDEX IF NOT EXISTS ix_frontier_pending ON frontier(job_id, state, priority DESC);
CREATE TABLE IF NOT EXISTS cache (
  key TEXT PRIMARY KEY, url TEXT NOT NULL, data TEXT NOT NULL, created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, tag TEXT NOT NULL DEFAULT '',
  markdown TEXT, json TEXT, content_hash TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snapshots_url ON snapshots(url, tag, id DESC);
CREATE TABLE IF NOT EXISTS robots (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, definition TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY, target_kind TEXT NOT NULL, target TEXT NOT NULL, cron TEXT NOT NULL,
  timezone TEXT NOT NULL DEFAULT 'Europe/Madrid', enabled INTEGER NOT NULL DEFAULT 1,
  last_run_at TEXT, next_run_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monitors (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, config TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  last_status TEXT, last_checked_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id TEXT PRIMARY KEY, url TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL,
  headers TEXT, attempts INTEGER DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
  next_attempt_ts REAL NOT NULL, last_error TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_webhooks_pending ON webhook_deliveries(status, next_attempt_ts);
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL,
  created_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS domain_profiles (
  domain TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(settings.db_path, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.executescript(SCHEMA)
        return _conn


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    with _lock:
        return conn().execute(sql, tuple(params))


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, tuple(params)).fetchall()]


def one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(s: str | None, default: Any = None) -> Any:
    if s is None:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- jobs
def create_job(kind: str, request: dict, parent_id: str | None = None, status: str = "queued") -> str:
    jid = new_id(kind.replace("_", "")[:6])
    ts = now_iso()
    execute(
        "INSERT INTO jobs(id,kind,status,request,parent_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (jid, kind, status, dumps(request), parent_id, ts, ts),
    )
    return jid


def update_job(jid: str, **fields: Any) -> None:
    if not fields:
        return
    if "result" in fields and not isinstance(fields["result"], (str, type(None))):
        fields["result"] = dumps(fields["result"])
    if fields.get("status") in ("completed", "failed", "cancelled"):
        fields.setdefault("finished_at", now_iso())
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), jid))


def bump_job(jid: str, completed: int = 0, failed: int = 0, total: int = 0) -> None:
    execute(
        "UPDATE jobs SET completed=completed+?, failed=failed+?, total=total+?, updated_at=? WHERE id=?",
        (completed, failed, total, now_iso(), jid),
    )


def get_job(jid: str) -> dict | None:
    row = one("SELECT * FROM jobs WHERE id=?", (jid,))
    if row:
        row["request"] = loads(row["request"], {})
        row["result"] = loads(row["result"])
    return row


def list_jobs(kind: str | None = None, limit: int = 50) -> list[dict]:
    if kind:
        rows = query("SELECT * FROM jobs WHERE kind=? ORDER BY created_at DESC LIMIT ?", (kind, limit))
    else:
        rows = query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
    for r in rows:
        r["request"] = loads(r["request"], {})
        r["result"] = None
    return rows


def add_document(job_id: str, url: str, data: dict) -> None:
    execute("INSERT INTO documents(job_id,url,data,created_at) VALUES(?,?,?,?)", (job_id, url, dumps(data), now_iso()))


def get_documents(job_id: str, offset: int = 0, limit: int = 100) -> list[dict]:
    rows = query("SELECT data FROM documents WHERE job_id=? ORDER BY id LIMIT ? OFFSET ?", (job_id, limit, offset))
    return [loads(r["data"], {}) for r in rows]


def count_documents(job_id: str) -> int:
    row = one("SELECT COUNT(*) AS n FROM documents WHERE job_id=?", (job_id,))
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------- cache
def cache_get(key: str, max_age_ms: int) -> dict | None:
    if max_age_ms <= 0:
        return None
    row = one("SELECT data, created_ts FROM cache WHERE key=?", (key,))
    if not row or (time.time() - row["created_ts"]) * 1000 > max_age_ms:
        return None
    return loads(row["data"])


def cache_put(key: str, url: str, data: dict) -> None:
    execute(
        "INSERT OR REPLACE INTO cache(key,url,data,created_ts) VALUES(?,?,?,?)",
        (key, url, dumps(data), time.time()),
    )


# ---------------------------------------------------------------- api keys
def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_api_key(name: str) -> str:
    key = "sp-" + secrets.token_urlsafe(32)
    execute(
        "INSERT INTO api_keys(id,name,key_hash,prefix,created_at) VALUES(?,?,?,?,?)",
        (new_id("key"), name, _hash_key(key), key[:10], now_iso()),
    )
    return key


def verify_api_key(key: str) -> bool:
    return one("SELECT id FROM api_keys WHERE key_hash=? AND revoked=0", (_hash_key(key),)) is not None


def has_api_keys() -> bool:
    return one("SELECT id FROM api_keys WHERE revoked=0") is not None


# ---------------------------------------------------------------- perfiles por dominio (aprendizaje)
def get_domain_profile(domain: str) -> dict:
    row = one("SELECT data FROM domain_profiles WHERE domain=?", (domain,))
    return loads(row["data"], {}) if row else {}


def save_domain_profile(domain: str, data: dict) -> None:
    execute(
        "INSERT OR REPLACE INTO domain_profiles(domain,data,updated_at) VALUES(?,?,?)",
        (domain, dumps(data), now_iso()),
    )
