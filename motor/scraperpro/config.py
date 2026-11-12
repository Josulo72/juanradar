"""Configuración central. Se lee de variables de entorno (prefijo SP_) o de motor\\.env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
# Navegadores dentro del proyecto (D:\ScraperPro\browsers), no en el perfil de usuario
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "browsers"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "si", "sí", "on")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("SP_DATA_DIR", str(ROOT / "data"))))
    host: str = field(default_factory=lambda: _env("SP_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("SP_PORT", 8787))
    auth_required: bool = field(default_factory=lambda: _bool("SP_AUTH", False))
    browser_pool_size: int = field(default_factory=lambda: _int("SP_BROWSER_POOL", 3))
    headless: bool = field(default_factory=lambda: _bool("SP_HEADLESS", True))
    default_timeout_ms: int = field(default_factory=lambda: _int("SP_TIMEOUT_MS", 30000))
    workers: int = field(default_factory=lambda: _int("SP_WORKERS", 4))
    per_domain_concurrency: int = field(default_factory=lambda: _int("SP_PER_DOMAIN", 2))
    proxies: list[str] = field(default_factory=lambda: [p.strip() for p in _env("SP_PROXIES").split(",") if p.strip()])
    llm_provider: str = field(default_factory=lambda: _env("SP_LLM_PROVIDER", ""))
    llm_model: str = field(default_factory=lambda: _env("SP_LLM_MODEL", ""))
    llm_base_url: str = field(default_factory=lambda: _env("SP_LLM_BASE_URL", ""))
    llm_api_key: str = field(default_factory=lambda: _env("SP_LLM_API_KEY", ""))
    webhook_secret: str = field(default_factory=lambda: _env("SP_WEBHOOK_SECRET", ""))
    user_agent: str = field(default_factory=lambda: _env(
        "SP_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36",
    ))
    respect_robots: bool = field(default_factory=lambda: _bool("SP_RESPECT_ROBOTS", True))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scraperpro.db"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.files_dir.mkdir(parents=True, exist_ok=True)
