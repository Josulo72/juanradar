"""Crawler con frontera persistente (reanudable), prioridad, robots.txt + crawl-delay y anti-trampas.

Mejoras frente a Firecrawl: respeta crawl-delay (allí está comentado), prioriza URLs (sitemap y
poca profundidad primero), limita páginas por patrón de URL (evita calendarios y filtros infinitos)
y la cola vive en SQLite: si se reinicia el servidor, el crawl continúa donde iba.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from urllib.parse import urlparse

from . import db, webhooks
from .config import settings
from .scrape import scrape_url, validate_url
from .transform.meta import extract_links
from .urls import SKIP_EXT, Robots, clean_url, dedupe_key, pattern_key, same_site, sitemap_urls


def _path_match(patterns: list[str] | None, url: str, full: bool) -> bool:
    target = url if full else (urlparse(url).path or "/")
    for p in patterns or []:
        try:
            if re.search(p, target):
                return True
        except re.error:
            if p in target:
                return True
    return False


class Crawl:
    def __init__(self, job_id: str, req: dict):
        self.job_id = job_id
        self.req = req
        self.base = validate_url(req["url"])
        self.limit = int(req.get("limit") or 100)
        self.max_depth = req.get("maxDiscoveryDepth", req.get("maxDepth"))
        self.ignore_q = bool(req.get("ignoreQueryParameters"))
        self.robots = Robots(self.base)
        self.respect_robots = settings.respect_robots if req.get("respectRobots") is None else bool(req["respectRobots"])
        self.per_pattern: Counter = Counter()
        self.max_per_pattern = req.get("maxPagesPerPattern") or 0
        self.delay = float(req.get("delay") or 0)
        self.concurrency = int(req.get("maxConcurrency") or (1 if self.delay else settings.workers))
        self.scrape_opts = dict(req.get("scrapeOptions") or {})
        self.scrape_opts.setdefault("formats", ["markdown"])
        self.webhook = req.get("webhook")
        bp = urlparse(self.base).path
        if "." in bp.rsplit("/", 1)[-1]:  # /catalogo/index.html -> /catalogo
            bp = bp.rsplit("/", 1)[0]
        self.base_path = bp.rstrip("/")
        self._last_fetch = 0.0
        self._delay_lock = asyncio.Lock()

    # ---------------------------------------------------------------- filtros
    def accept(self, url: str, depth: int) -> bool:
        if not url.startswith(("http://", "https://")) or SKIP_EXT.search(url):
            return False
        if self.max_depth is not None and depth > int(self.max_depth):
            return False
        if not self.req.get("allowExternalLinks") and not same_site(url, self.base, bool(self.req.get("allowSubdomains"))):
            return False
        path = urlparse(url).path
        if not self.req.get("crawlEntireDomain") and self.base_path and not path.startswith(self.base_path) and same_site(url, self.base, True):
            return False
        full = bool(self.req.get("regexOnFullURL"))
        if self.req.get("includePaths") and not _path_match(self.req["includePaths"], url, full):
            return False
        if self.req.get("excludePaths") and _path_match(self.req["excludePaths"], url, full):
            return False
        if self.respect_robots and not self.robots.allowed(url):
            return False
        return True

    def enqueue(self, url: str, depth: int, priority: float) -> bool:
        key = dedupe_key(url, self.ignore_q)
        cur = db.execute(
            "INSERT OR IGNORE INTO frontier(job_id,url_key,url,depth,priority) VALUES(?,?,?,?,?)",
            (self.job_id, key, clean_url(url, ignore_query=self.ignore_q), depth, priority),
        )
        return cur.rowcount > 0

    def _queued_total(self) -> int:
        row = db.one("SELECT COUNT(*) n FROM frontier WHERE job_id=?", (self.job_id,))
        return int(row["n"]) if row else 0

    # ---------------------------------------------------------------- arranque
    async def seed(self) -> None:
        if self.respect_robots:
            try:
                await self.robots.load()
            except Exception:
                pass
        if self.robots.crawl_delay and self.respect_robots:
            self.delay = max(self.delay, min(self.robots.crawl_delay, 30))
            self.concurrency = 1
        self.enqueue(self.base, 0, 100.0)
        if self.req.get("sitemap", "include") != "skip":
            try:
                sm = await sitemap_urls(self.base, self.robots.sitemaps, max_urls=self.limit * 5)
            except Exception:
                sm = []
            for item in sm:
                u = item["url"]
                if self.accept(u, 1):
                    self.enqueue(u, 1, 50.0 + 10 * (item.get("priority") or 0.5))
        db.update_job(self.job_id, status="running", total=min(self._queued_total(), self.limit))
        webhooks.emit(self.webhook, "crawl.started", {"id": self.job_id, "success": True})

    # ---------------------------------------------------------------- bucle
    def _next_batch(self, n: int) -> list[dict]:
        return db.query(
            "SELECT url, url_key, depth FROM frontier WHERE job_id=? AND state='pending' "
            "ORDER BY priority DESC, depth ASC LIMIT ?", (self.job_id, n),
        )

    def _done_count(self) -> int:
        row = db.one("SELECT COUNT(*) n FROM frontier WHERE job_id=? AND state='done'", (self.job_id,))
        return int(row["n"]) if row else 0

    async def _polite_wait(self) -> None:
        if not self.delay:
            return
        async with self._delay_lock:
            wait = self._last_fetch + self.delay - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_fetch = time.time()

    async def _process(self, item: dict) -> None:
        url, key, depth = item["url"], item["url_key"], item["depth"]
        pk = pattern_key(url)
        if self.max_per_pattern and self.per_pattern[pk] >= self.max_per_pattern:
            db.execute("UPDATE frontier SET state='skipped' WHERE job_id=? AND url_key=?", (self.job_id, key))
            return
        self.per_pattern[pk] += 1
        await self._polite_wait()
        opts = dict(self.scrape_opts)
        fmts = list(opts.get("formats") or ["markdown"])
        need_links = "links" not in [f if isinstance(f, str) else f.get("type") for f in fmts]
        if need_links:
            fmts = fmts + ["rawHtml"] if "rawHtml" not in fmts else fmts
        opts["formats"] = fmts
        try:
            doc = await scrape_url(url, opts)
        except Exception as e:
            doc = {"success": False, "metadata": {"sourceURL": url, "error": f"{type(e).__name__}: {e}"}}
        ok = bool(doc.get("success"))
        db.execute("UPDATE frontier SET state=?, attempts=attempts+1 WHERE job_id=? AND url_key=?",
                   ("done" if ok else "failed", self.job_id, key))
        final = doc.get("metadata", {}).get("url") or url
        # Las redirecciones fuera del sitio no cuentan
        if ok and not self.req.get("allowExternalLinks") and not same_site(final, self.base, bool(self.req.get("allowSubdomains"))):
            ok = False
        # Descubrimiento de enlaces
        html_for_links = doc.get("rawHtml") or doc.get("html") or ""
        links = doc.get("links") or (extract_links(html_for_links, final)[0] if html_for_links else [])
        if need_links:
            doc.pop("rawHtml", None)
        if self.max_depth is None or depth + 1 <= int(self.max_depth):
            for ln in links:
                if self.accept(ln, depth + 1):
                    self.enqueue(ln, depth + 1, 10.0 - depth)
        if ok:
            db.add_document(self.job_id, final, doc)
            db.bump_job(self.job_id, completed=1)
            webhooks.emit(self.webhook, "crawl.page", {"id": self.job_id, "success": True, "data": [doc]})
        else:
            db.bump_job(self.job_id, failed=1)

    async def run(self) -> None:
        job = db.get_job(self.job_id)
        if not job or job["status"] in ("cancelled",):
            return
        if not db.one("SELECT 1 FROM frontier WHERE job_id=? LIMIT 1", (self.job_id,)):
            await self.seed()
        else:  # reanudación tras reinicio
            db.execute("UPDATE frontier SET state='pending' WHERE job_id=? AND state='running'", (self.job_id,))
            if self.respect_robots:
                await self.robots.load()
            db.update_job(self.job_id, status="running")
        sem = asyncio.Semaphore(self.concurrency)
        running: set[asyncio.Task] = set()
        while True:
            job = db.get_job(self.job_id)
            if not job or job["status"] == "cancelled":
                for t in running:
                    t.cancel()
                return
            done = self._done_count()
            slots = self.limit - done - len(running)
            if slots > 0:
                batch = self._next_batch(min(slots, self.concurrency * 2))
                for item in batch:
                    db.execute("UPDATE frontier SET state='running' WHERE job_id=? AND url_key=?", (self.job_id, item["url_key"]))

                    async def _w(it=item):
                        async with sem:
                            await self._process(it)

                    t = asyncio.create_task(_w())
                    running.add(t)
                    t.add_done_callback(running.discard)
            if not running:
                break
            await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            total_known = min(self._queued_total(), self.limit)
            db.update_job(self.job_id, total=total_known)
        n = db.count_documents(self.job_id)
        db.update_job(self.job_id, status="completed", total=n, result={"pages": n})
        webhooks.emit(self.webhook, "crawl.completed", {"id": self.job_id, "success": True, "data": []})


async def run_crawl(job_id: str) -> None:
    job = db.get_job(job_id)
    if not job:
        return
    try:
        await Crawl(job_id, job["request"]).run()
    except Exception as e:
        db.update_job(job_id, status="failed", error=f"{type(e).__name__}: {e}")
        webhooks.emit(job["request"].get("webhook"), "crawl.failed", {"id": job_id, "success": False, "error": str(e)})
