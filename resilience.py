"""Resilience layer: rate limiting, retry, circuit breaker, proxy pool, robots.txt, sitemap."""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from io import StringIO
from xml.etree import ElementTree

import aiohttp

__all__ = [
    "RateLimiter",
    "RetryHandler",
    "CircuitBreaker",
    "CircuitOpenError",
    "ProxyConfig",
    "ProxyPool",
    "RobotsChecker",
    "SitemapParser",
    "SitemapEntry",
]


class RateLimiter:
    """Domain-based adaptive rate limiter using asyncio primitives."""

    def __init__(
        self,
        *,
        default_delay: float = 1.0,
        max_concurrent: int = 10,
        domain_delays: dict[str, float] | None = None,
    ):
        self._default_delay = default_delay
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._domain_delays: dict[str, float] = dict(domain_delays or {})
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._domain_last: dict[str, float] = {}
        self._domain_request_count: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def _get_domain_lock(self, domain: str) -> asyncio.Lock:
        async with self._lock:
            if domain not in self._domain_locks:
                self._domain_locks[domain] = asyncio.Lock()
            return self._domain_locks[domain]

    def _delay_for(self, domain: str) -> float:
        return self._domain_delays.get(domain, self._default_delay)

    async def acquire(self, domain: str) -> None:
        """Wait until a request to *domain* is permitted."""
        await self._semaphore.acquire()
        domain_lock = await self._get_domain_lock(domain)
        async with domain_lock:
            now = time.monotonic()
            last = self._domain_last.get(domain, 0.0)
            delay = self._delay_for(domain)
            wait = delay - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._domain_last[domain] = time.monotonic()
            self._domain_request_count[domain] = self._domain_request_count.get(domain, 0) + 1
        self._semaphore.release()

    def update_delay(self, domain: str, status_code: int) -> None:
        """Adapt per-domain delay based on response status.

        429 → delay *= 2 (cap 60s)
        200 → delay *= 0.9 (floor 0.5s)
        """
        current = self._domain_delays.get(domain, self._default_delay)
        if status_code == 429:
            self._domain_delays[domain] = min(current * 2, 60.0)
        elif status_code == 200:
            self._domain_delays[domain] = max(current * 0.9, 0.5)

    @property
    def stats(self) -> dict:
        return {
            domain: {
                "delay": self._domain_delays.get(domain, self._default_delay),
                "requests": self._domain_request_count.get(domain, 0),
            }
            for domain in sorted(
                set(self._domain_request_count) | set(self._domain_delays)
            )
        }


class RetryHandler:
    """Exponential back-off with jitter."""

    def __init__(
        self,
        *,
        max_retries: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        retry_on: tuple[int, ...] = (429, 500, 502, 503, 504),
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.retry_on = retry_on

    async def execute(self, func, *args, **kwargs):
        """Call *func* with automatic retry on retryable failures."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                result = await func(*args, **kwargs)
                sc = getattr(result, "status_code", None) or getattr(result, "status", None)
                if sc is not None and sc in self.retry_on:
                    raise _RetryableStatusError(result.status)
                return result
            except _RetryableStatusError as exc:
                last_exc = exc
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, OSError) as exc:
                last_exc = exc

            if attempt < self.max_retries:
                jitter = random.uniform(0, self.base_delay)
                delay = min(self.base_delay * (2 ** attempt) + jitter, self.max_delay)
                await asyncio.sleep(delay)

        raise last_exc  # type: ignore[misc]


class _RetryableStatusError(Exception):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Retryable HTTP {status}")


class _CBState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when the circuit breaker is open."""


class CircuitBreaker:
    """Three-state circuit breaker: CLOSED → OPEN → HALF_OPEN → CLOSED."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        success_threshold: int = 2,
    ):
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._success_threshold = success_threshold

        self._state = _CBState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0.0
        self._lock = asyncio.Lock()

    async def call(self, func, *args, **kwargs):
        async with self._lock:
            if self._state == _CBState.OPEN:
                if time.monotonic() - self._last_failure_time >= self._recovery_timeout:
                    self._state = _CBState.HALF_OPEN
                    self._success_count = 0
                else:
                    raise CircuitOpenError(
                        f"Circuit open, retry after {self._recovery_timeout}s"
                    )

        try:
            result = await func(*args, **kwargs)
        except Exception:
            async with self._lock:
                self._failure_count += 1
                self._last_failure_time = time.monotonic()
                if self._state == _CBState.HALF_OPEN:
                    self._state = _CBState.OPEN
                elif self._failure_count >= self._failure_threshold:
                    self._state = _CBState.OPEN
            raise

        async with self._lock:
            if self._state == _CBState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self._success_threshold:
                    self._state = _CBState.CLOSED
                    self._failure_count = 0
            else:
                self._failure_count = 0
        return result

    @property
    def state(self) -> str:
        return self._state.value


@dataclass
class ProxyConfig:
    url: str
    proxy_type: str = "datacenter"  # 'residential' | 'datacenter'
    sticky: bool = False


@dataclass
class _ProxyState:
    config: ProxyConfig
    fail_count: int = 0
    success_count: int = 0
    total_requests: int = 0
    disabled_until: float = 0.0


class ProxyPool:
    """Rotating / sticky proxy management with health tracking."""

    def __init__(self, proxies: list[ProxyConfig] | None = None):
        self._proxies: dict[str, _ProxyState] = {}
        self._rotation_index = 0
        self._sticky_sessions: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()
        for p in (proxies or []):
            self._proxies[p.url] = _ProxyState(config=p)

    def _available(self) -> list[_ProxyState]:
        now = time.monotonic()
        return [
            ps for ps in self._proxies.values()
            if now >= ps.disabled_until
        ]

    def get_rotating(self) -> str | None:
        pool = self._available()
        if not pool:
            return None
        ps = pool[self._rotation_index % len(pool)]
        self._rotation_index += 1
        ps.total_requests += 1
        return ps.config.url

    def get_sticky(self, session_id: str | None = None, duration: int = 300) -> str | None:
        sid = session_id or uuid.uuid4().hex
        now = time.monotonic()

        if sid in self._sticky_sessions:
            url, expires = self._sticky_sessions[sid]
            if now < expires and url in self._proxies and now >= self._proxies[url].disabled_until:
                self._proxies[url].total_requests += 1
                return url

        stickies = [ps for ps in self._available() if ps.config.sticky]
        if not stickies:
            stickies = self._available()
        if not stickies:
            return None

        chosen = random.choice(stickies)
        self._sticky_sessions[sid] = (chosen.config.url, now + duration)
        chosen.total_requests += 1
        return chosen.config.url

    def mark_failed(self, proxy_url: str) -> None:
        ps = self._proxies.get(proxy_url)
        if not ps:
            return
        ps.fail_count += 1
        backoff = min(30 * (2 ** (ps.fail_count - 1)), 600)
        ps.disabled_until = time.monotonic() + backoff

    def mark_success(self, proxy_url: str) -> None:
        ps = self._proxies.get(proxy_url)
        if not ps:
            return
        ps.success_count += 1
        ps.fail_count = max(ps.fail_count - 1, 0)
        ps.disabled_until = 0.0

    def add(self, proxy: ProxyConfig) -> None:
        self._proxies[proxy.url] = _ProxyState(config=proxy)

    def remove(self, proxy_url: str) -> None:
        self._proxies.pop(proxy_url, None)
        to_del = [k for k, (u, _) in self._sticky_sessions.items() if u == proxy_url]
        for k in to_del:
            del self._sticky_sessions[k]

    def get_proxy(self) -> str | None:
        """Duck-typing compatible with StealthFetcher's ProxyPool protocol."""
        return self.get_rotating()

    @property
    def available_count(self) -> int:
        return len(self._available())

    @property
    def stats(self) -> dict:
        now = time.monotonic()
        return {
            url: {
                "type": ps.config.proxy_type,
                "sticky": ps.config.sticky,
                "success": ps.success_count,
                "fail": ps.fail_count,
                "total": ps.total_requests,
                "available": now >= ps.disabled_until,
            }
            for url, ps in self._proxies.items()
        }


class RobotsChecker:
    """robots.txt parser with async fetch and in-memory cache."""

    def __init__(self, *, user_agent: str = "StealthScraper/1.0", cache_ttl: int = 3600):
        self._user_agent = user_agent
        self._cache_ttl = cache_ttl
        self._cache: dict[str, tuple[RobotFileParser, float]] = {}
        self._lock = asyncio.Lock()

    async def _get_parser(self, url: str) -> RobotFileParser:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        robots_url = f"{origin}/robots.txt"

        async with self._lock:
            if origin in self._cache:
                parser, ts = self._cache[origin]
                if time.monotonic() - ts < self._cache_ttl:
                    return parser

        rp = RobotFileParser()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(robots_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        rp.parse(text.splitlines())
                    else:
                        rp.allow_all = True
        except Exception:
            rp.allow_all = True

        async with self._lock:
            self._cache[origin] = (rp, time.monotonic())
        return rp

    async def can_fetch(self, url: str) -> bool:
        rp = await self._get_parser(url)
        return rp.can_fetch(self._user_agent, url)

    async def get_crawl_delay(self, url: str) -> float | None:
        rp = await self._get_parser(url)
        delay = rp.crawl_delay(self._user_agent)
        if delay is not None:
            return float(delay)
        return None

    async def get_sitemaps(self, url: str) -> list[str]:
        rp = await self._get_parser(url)
        maps = rp.site_maps()
        return list(maps) if maps else []

    def clear_cache(self) -> None:
        self._cache.clear()


_SITEMAP_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
}


@dataclass
class SitemapEntry:
    url: str
    lastmod: str | None = None
    changefreq: str | None = None
    priority: float | None = None


class SitemapParser:
    """Async sitemap XML parser (supports sitemap index and urlset)."""

    @staticmethod
    async def parse(sitemap_url: str) -> list[SitemapEntry]:
        entries: list[SitemapEntry] = []

        async with aiohttp.ClientSession() as session:
            await SitemapParser._fetch_and_parse(session, sitemap_url, entries, depth=0)

        return entries

    @staticmethod
    async def _fetch_and_parse(
        session: aiohttp.ClientSession,
        url: str,
        entries: list[SitemapEntry],
        depth: int,
    ) -> None:
        if depth > 3:
            return

        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return
                data = await resp.read()
        except Exception:
            return

        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError:
            return

        tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

        if tag == "sitemapindex":
            sub_urls = [
                loc.text
                for loc in root.findall(".//sm:sitemap/sm:loc", _SITEMAP_NS)
                if loc.text
            ]
            tasks = [
                SitemapParser._fetch_and_parse(session, u, entries, depth + 1)
                for u in sub_urls
            ]
            await asyncio.gather(*tasks)
        else:
            for url_el in root.findall(".//sm:url", _SITEMAP_NS):
                loc = url_el.find("sm:loc", _SITEMAP_NS)
                if loc is None or not loc.text:
                    continue
                lastmod_el = url_el.find("sm:lastmod", _SITEMAP_NS)
                freq_el = url_el.find("sm:changefreq", _SITEMAP_NS)
                prio_el = url_el.find("sm:priority", _SITEMAP_NS)
                entries.append(
                    SitemapEntry(
                        url=loc.text,
                        lastmod=lastmod_el.text if lastmod_el is not None else None,
                        changefreq=freq_el.text if freq_el is not None else None,
                        priority=float(prio_el.text) if prio_el is not None and prio_el.text else None,
                    )
                )
