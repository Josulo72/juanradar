"""Línea de comandos.

  python -m scraperpro serve                      servidor API + panel (http://127.0.0.1:8787)
  python -m scraperpro scrape URL [-f markdown,links] [--engine auto|http|browser|stealth] [-o salida.json]
  python -m scraperpro crawl URL [--limit 50] [-o salida.ndjson]
  python -m scraperpro map URL [--search texto]
  python -m scraperpro search "consulta" [--limit 5]
  python -m scraperpro robot robot.json [--param clave=valor] [-o salida.json]
  python -m scraperpro auto URL [--prompt "qué quieres"] [-o robot.json]
  python -m scraperpro key [nombre]               crea una API key
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _out(data, path: str | None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if path:
        Path(path).write_text(text, encoding="utf-8")
        print(f"Guardado en {path}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)


async def _close() -> None:
    from .engines.browser import manager

    await manager.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="scraperpro", description="Scraper universal con API")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s = sub.add_parser("scrape")
    s.add_argument("url")
    s.add_argument("-f", "--formats", default="markdown")
    s.add_argument("--engine", default="auto")
    s.add_argument("--full", action="store_true", help="no limitar al contenido principal")
    s.add_argument("-o", "--output")
    s = sub.add_parser("crawl")
    s.add_argument("url")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--include", nargs="*")
    s.add_argument("--exclude", nargs="*")
    s.add_argument("-o", "--output")
    s = sub.add_parser("map")
    s.add_argument("url")
    s.add_argument("--search")
    s.add_argument("--depth", type=int, default=1)
    s.add_argument("-o", "--output")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("-o", "--output")
    s = sub.add_parser("robot")
    s.add_argument("file")
    s.add_argument("--param", action="append", default=[])
    s.add_argument("-o", "--output")
    s = sub.add_parser("auto")
    s.add_argument("url")
    s.add_argument("--prompt")
    s.add_argument("-o", "--output")
    s = sub.add_parser("key")
    s.add_argument("name", nargs="?", default="cli")
    a = ap.parse_args()

    if a.cmd == "serve":
        import uvicorn

        from .config import settings

        uvicorn.run("scraperpro.api.app:app", host=a.host or settings.host, port=a.port or settings.port, log_level="info")
        return
    if a.cmd == "key":
        from . import db

        print(db.create_api_key(a.name))
        return

    async def run():
        try:
            if a.cmd == "scrape":
                from .scrape import scrape_url

                return await scrape_url(a.url, {"formats": a.formats.split(","), "engine": a.engine, "onlyMainContent": not a.full})
            if a.cmd == "crawl":
                from . import db
                from .crawler import run_crawl

                req = {"url": a.url, "limit": a.limit, "includePaths": a.include, "excludePaths": a.exclude}
                jid = db.create_job("crawl", req)
                await run_crawl(jid)
                return db.get_documents(jid, 0, 100000)
            if a.cmd == "map":
                from .discovery import map_site

                return await map_site({"url": a.url, "search": a.search, "depth": a.depth})
            if a.cmd == "search":
                from .discovery import search

                return await search({"query": a.query, "limit": a.limit})
            if a.cmd == "robot":
                from .robots.runner import run_robot

                params = dict(p.split("=", 1) for p in a.param)
                return await run_robot(json.loads(Path(a.file).read_text(encoding="utf-8")), params)
            if a.cmd == "auto":
                from .robots.auto import build_robot

                return (await build_robot(a.url, a.prompt))["robot"]
        finally:
            await _close()

    _out(asyncio.run(run()), getattr(a, "output", None))


if __name__ == "__main__":
    main()
