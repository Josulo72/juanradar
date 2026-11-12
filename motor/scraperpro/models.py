"""Esquemas de petición. Compatibles con la API de Firecrawl (v1/v2) y ampliados."""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


FormatSpec = Union[str, dict]


class Location(_Base):
    country: Optional[str] = None
    languages: Optional[list[str]] = None


class ScrapeOptions(_Base):
    formats: list[FormatSpec] = Field(default_factory=lambda: ["markdown"])
    onlyMainContent: bool = True
    includeTags: Optional[list[str]] = None
    excludeTags: Optional[list[str]] = None
    headers: Optional[dict[str, str]] = None
    cookies: Optional[list[dict]] = None
    waitFor: int = 0
    waitForSelector: Optional[str] = None
    timeout: Optional[int] = None
    mobile: bool = False
    actions: Optional[list[dict]] = None
    location: Optional[Location] = None
    skipTlsVerification: bool = False
    removeBase64Images: bool = True
    blockAds: bool = True
    blockResources: Optional[list[str]] = None  # image, font, media, stylesheet
    proxy: Literal["auto", "none", "basic", "stealth", "rotate"] = "auto"
    engine: Literal["auto", "http", "browser", "stealth"] = "auto"
    maxAge: int = 0
    storeInCache: bool = True
    parsePDF: bool = True
    captureNetwork: bool = False  # captura respuestas XHR/fetch JSON (APIs ocultas)
    profile: Optional[str] = None  # perfil persistente (cookies/localStorage) por nombre
    javascript: Optional[str] = None


class ScrapeRequest(ScrapeOptions):
    url: str


class BatchScrapeRequest(ScrapeOptions):
    urls: list[str]
    webhook: Optional[Union[str, dict]] = None
    maxConcurrency: Optional[int] = None
    ignoreInvalidURLs: bool = True


class CrawlRequest(_Base):
    url: str
    includePaths: Optional[list[str]] = None
    excludePaths: Optional[list[str]] = None
    maxDepth: Optional[int] = None
    maxDiscoveryDepth: Optional[int] = None
    limit: int = 100
    allowExternalLinks: bool = False
    allowSubdomains: bool = False
    crawlEntireDomain: bool = False
    ignoreQueryParameters: bool = False
    sitemap: Literal["include", "skip", "only"] = "include"
    regexOnFullURL: bool = False
    delay: Optional[float] = None
    maxConcurrency: Optional[int] = None
    respectRobots: Optional[bool] = None
    maxPagesPerPattern: Optional[int] = 200  # anti-trampas: calendarios, filtros infinitos
    scrapeOptions: ScrapeOptions = Field(default_factory=ScrapeOptions)
    webhook: Optional[Union[str, dict]] = None


class MapRequest(_Base):
    url: str
    search: Optional[str] = None
    includeSubdomains: bool = True
    ignoreQueryParameters: bool = True
    sitemap: Literal["include", "skip", "only"] = "include"
    limit: int = 5000
    depth: int = 1  # niveles de enlaces a explorar además del sitemap
    timeout: Optional[int] = None


class SearchRequest(_Base):
    query: str
    limit: int = 5
    lang: Optional[str] = None
    country: Optional[str] = None
    includeDomains: Optional[list[str]] = None
    excludeDomains: Optional[list[str]] = None
    scrapeOptions: Optional[ScrapeOptions] = None


class ExtractRequest(_Base):
    urls: list[str]
    prompt: Optional[str] = None
    schema_: Optional[dict] = Field(default=None, alias="schema")
    enableWebSearch: bool = False
    showSources: bool = True
    limitPerWildcard: int = 20
    scrapeOptions: Optional[ScrapeOptions] = None


class RobotDefinition(_Base):
    name: str
    description: Optional[str] = None
    startUrl: Optional[str] = None
    params: dict[str, Any] = Field(default_factory=dict)
    steps: list[dict]
    options: dict[str, Any] = Field(default_factory=dict)
    validate_: Optional[dict] = Field(default=None, alias="validate")
    webhook: Optional[Union[str, dict]] = None


class RunRobotRequest(_Base):
    params: dict[str, Any] = Field(default_factory=dict)
    wait: bool = False


class ScheduleRequest(_Base):
    target_kind: Literal["robot", "crawl", "scrape", "monitor", "batch_scrape"]
    target: Union[str, dict]
    cron: str
    timezone: str = "Europe/Madrid"
    enabled: bool = True


class MonitorRequest(_Base):
    name: str
    urls: list[str]
    cron: Optional[str] = "0 */6 * * *"
    mode: Literal["markdown", "json", "select"] = "markdown"
    scrapeOptions: ScrapeOptions = Field(default_factory=ScrapeOptions)
    webhook: Optional[Union[str, dict]] = None
    ignorePatterns: Optional[list[str]] = None  # regex de texto a ignorar (fechas, contadores)


class AutoRobotRequest(_Base):
    url: str
    prompt: Optional[str] = None
    useBrowser: bool = True
