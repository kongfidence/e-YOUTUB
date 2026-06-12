"""3-level escalation stealth fetch engine: curl_cffi → Camoufox → nodriver."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

__all__ = ["StealthFetcher", "FetchResult"]

# ---------------------------------------------------------------------------
# Conditional imports — graceful degradation
# ---------------------------------------------------------------------------

_HAS_CURL_CFFI = False
try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    _HAS_CURL_CFFI = True
except ImportError:
    CurlAsyncSession = None  # type: ignore[assignment,misc]

_HAS_BROWSERFORGE = False
try:
    from browserforge.headers import HeaderGenerator
    _HAS_BROWSERFORGE = True
except ImportError:
    HeaderGenerator = None  # type: ignore[assignment,misc]

_HAS_CAMOUFOX = False
try:
    from camoufox.async_api import AsyncCamoufox
    _HAS_CAMOUFOX = True
except ImportError:
    AsyncCamoufox = None  # type: ignore[assignment,misc]

_HAS_NODRIVER = False
try:
    import nodriver
    _HAS_NODRIVER = True
except ImportError:
    nodriver = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# ProxyPool protocol (duck-typing)
# ---------------------------------------------------------------------------

@runtime_checkable
class ProxyPool(Protocol):
    def get_proxy(self) -> str | dict | None: ...


# ---------------------------------------------------------------------------
# CAPTCHA detection
# ---------------------------------------------------------------------------

_CAPTCHA_PATTERNS: list[str] = [
    "cf-browser-verification", "cf_chl_opt", "challenge-platform",
    "g-recaptcha", "h-captcha", "turnstile",
    "Just a moment", "Checking your browser",
    "Verify you are human", "Please verify",
    "cf-challenge-running", "ray ID",
]


def _is_captcha_page(html: str) -> bool:
    if not html:
        return False
    lower = html[:8192].lower()
    return any(p.lower() in lower for p in _CAPTCHA_PATTERNS)


# ---------------------------------------------------------------------------
# FetchResult
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    url: str
    html: str | None
    status_code: int
    level_used: int
    elapsed_ms: float
    error: str | None = None
    headers: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.html is not None and 200 <= self.status_code < 400 and not _is_captcha_page(self.html or "")

    @property
    def soup(self) -> BeautifulSoup:
        return BeautifulSoup(self.html or "", "lxml")


# ---------------------------------------------------------------------------
# Header generation helper
# ---------------------------------------------------------------------------

_header_gen: HeaderGenerator | None = None


def _generate_headers() -> dict[str, str]:
    global _header_gen
    if not _HAS_BROWSERFORGE:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        }
    if _header_gen is None:
        _header_gen = HeaderGenerator(browser="chrome", os="windows")
    raw = _header_gen.generate()
    if isinstance(raw, dict):
        return raw
    return dict(raw) if hasattr(raw, "__iter__") else {}


def _resolve_proxy(pool: ProxyPool | None) -> str | dict | None:
    if pool is None:
        return None
    try:
        return pool.get_proxy()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Impersonation targets for curl_cffi
# ---------------------------------------------------------------------------

_IMPERSONATE_TARGETS = ["chrome", "chrome110", "safari", "safari17_0", "chrome120"]


# ---------------------------------------------------------------------------
# StealthFetcher
# ---------------------------------------------------------------------------

class StealthFetcher:
    def __init__(
        self,
        proxy_pool: ProxyPool | None = None,
        captcha_api_key: str | None = None,
        max_level: int = 3,
    ):
        self.proxy_pool = proxy_pool
        self.captcha_api_key = captcha_api_key
        self.max_level = min(max(max_level, 1), 3)

        if self.max_level >= 1 and not _HAS_CURL_CFFI:
            logger.warning("curl_cffi not installed — Level 1 unavailable")
        if self.max_level >= 2 and not _HAS_CAMOUFOX:
            logger.warning("camoufox not installed — Level 2 unavailable")
        if self.max_level >= 3 and not _HAS_NODRIVER:
            logger.warning("nodriver not installed — Level 3 unavailable")

    # ---- public API -------------------------------------------------------

    async def fetch(
        self,
        url: str,
        *,
        render_js: bool = False,
        scroll_page: bool = False,
        wait_for_selector: str | None = None,
        timeout: int = 30,
    ) -> FetchResult:
        start_level = 2 if render_js else 1
        for level in range(start_level, self.max_level + 1):
            t0 = time.perf_counter()
            try:
                result = await self._dispatch(level, url, timeout=timeout,
                                              scroll_page=scroll_page,
                                              wait_for_selector=wait_for_selector)
                result.elapsed_ms = (time.perf_counter() - t0) * 1000
                if result.ok:
                    return result
                logger.info("L%d returned non-ok for %s (status=%d, captcha=%s) — escalating",
                            level, url, result.status_code, _is_captcha_page(result.html or ""))
            except Exception as exc:
                elapsed = (time.perf_counter() - t0) * 1000
                logger.warning("L%d failed for %s: %s", level, url, exc)
                if level == self.max_level:
                    return FetchResult(url=url, html=None, status_code=0,
                                       level_used=level, elapsed_ms=elapsed, error=str(exc))

        return FetchResult(url=url, html=None, status_code=0,
                           level_used=self.max_level, elapsed_ms=0, error="all levels exhausted")

    async def fetch_many(
        self,
        urls: list[str],
        *,
        concurrency: int = 5,
        **kwargs: Any,
    ) -> list[FetchResult]:
        sem = asyncio.Semaphore(concurrency)

        async def _bounded(u: str) -> FetchResult:
            async with sem:
                return await self.fetch(u, **kwargs)

        return list(await asyncio.gather(*[_bounded(u) for u in urls]))

    # ---- dispatch ---------------------------------------------------------

    async def _dispatch(
        self, level: int, url: str, *, timeout: int,
        scroll_page: bool, wait_for_selector: str | None,
    ) -> FetchResult:
        if level == 1:
            return await self._l1_curl_cffi(url, timeout=timeout)
        if level == 2:
            return await self._l2_camoufox(url, timeout=timeout,
                                           scroll_page=scroll_page,
                                           wait_for_selector=wait_for_selector)
        return await self._l3_nodriver(url, timeout=timeout,
                                       scroll_page=scroll_page,
                                       wait_for_selector=wait_for_selector)

    # ---- Level 1: curl_cffi -----------------------------------------------

    async def _l1_curl_cffi(self, url: str, *, timeout: int) -> FetchResult:
        if not _HAS_CURL_CFFI:
            raise RuntimeError("curl_cffi not installed")

        proxy = _resolve_proxy(self.proxy_pool)
        proxy_str: str | None = None
        if isinstance(proxy, dict):
            proxy_str = proxy.get("https") or proxy.get("http") or proxy.get("server")
        elif isinstance(proxy, str):
            proxy_str = proxy

        headers = _generate_headers()
        last_exc: Exception | None = None

        for target in _IMPERSONATE_TARGETS:
            try:
                async with CurlAsyncSession() as session:
                    resp = await session.get(
                        url,
                        headers=headers,
                        impersonate=target,
                        proxy=proxy_str,
                        timeout=timeout,
                        allow_redirects=True,
                    )
                    html = resp.text
                    return FetchResult(
                        url=url, html=html,
                        status_code=resp.status_code,
                        level_used=1, elapsed_ms=0,
                        headers=dict(resp.headers) if resp.headers else {},
                    )
            except Exception as exc:
                last_exc = exc
                continue

        raise last_exc or RuntimeError("curl_cffi: all impersonate targets failed")

    # ---- Level 2: Camoufox ------------------------------------------------

    async def _l2_camoufox(
        self, url: str, *, timeout: int,
        scroll_page: bool, wait_for_selector: str | None,
    ) -> FetchResult:
        if not _HAS_CAMOUFOX:
            raise RuntimeError("camoufox not installed")

        proxy_raw = _resolve_proxy(self.proxy_pool)
        proxy_dict: dict | None = None
        if isinstance(proxy_raw, dict):
            proxy_dict = proxy_raw
        elif isinstance(proxy_raw, str):
            proxy_dict = {"server": proxy_raw}

        kwargs: dict[str, Any] = {
            "headless": True,
            "os": "windows",
        }
        if proxy_dict:
            kwargs["proxy"] = proxy_dict
            kwargs["geoip"] = True

        async with AsyncCamoufox(**kwargs) as browser:
            page = await browser.new_page()
            try:
                resp = await page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
                status_code = resp.status if resp else 0

                if wait_for_selector:
                    await page.wait_for_selector(wait_for_selector, timeout=timeout * 1000)

                if scroll_page:
                    await self._playwright_scroll(page, timeout)

                html = await page.content()
                hdrs = await resp.all_headers() if resp else {}
            finally:
                await page.close()

        return FetchResult(
            url=url, html=html, status_code=status_code,
            level_used=2, elapsed_ms=0, headers=dict(hdrs),
        )

    @staticmethod
    async def _playwright_scroll(page: Any, timeout: int) -> None:
        max_scrolls = 30
        prev_height = 0
        for _ in range(max_scrolls):
            cur_height = await page.evaluate("document.body.scrollHeight")
            if cur_height == prev_height:
                break
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)
            prev_height = cur_height

    # ---- Level 3: nodriver + CAPTCHA solver --------------------------------

    async def _l3_nodriver(
        self, url: str, *, timeout: int,
        scroll_page: bool, wait_for_selector: str | None,
    ) -> FetchResult:
        if not _HAS_NODRIVER:
            raise RuntimeError("nodriver not installed")

        browser_args: list[str] = []
        proxy_raw = _resolve_proxy(self.proxy_pool)
        if isinstance(proxy_raw, dict):
            server = proxy_raw.get("server") or proxy_raw.get("https") or proxy_raw.get("http")
            if server:
                browser_args.append(f"--proxy-server={server}")
        elif isinstance(proxy_raw, str):
            browser_args.append(f"--proxy-server={proxy_raw}")

        browser = await nodriver.start(
            headless=True,
            browser_args=browser_args or None,
        )

        try:
            page = await browser.get(url)
            await asyncio.sleep(2)

            if wait_for_selector:
                for _ in range(timeout):
                    el = await page.query_selector(wait_for_selector)
                    if el:
                        break
                    await asyncio.sleep(1)

            if scroll_page:
                await self._nodriver_scroll(page, timeout)

            html = await page.get_content()
            status_code = 200

            if _is_captcha_page(html) and self.captcha_api_key:
                logger.info("CAPTCHA detected at %s — attempting solve", url)
                solved = await self._solve_captcha(url, html)
                if solved:
                    await asyncio.sleep(3)
                    html = await page.get_content()
                    if _is_captcha_page(html):
                        status_code = 403
                else:
                    status_code = 403
            elif _is_captcha_page(html):
                status_code = 403

        finally:
            try:
                browser.stop()
            except Exception:
                pass

        return FetchResult(
            url=url, html=html, status_code=status_code,
            level_used=3, elapsed_ms=0,
        )

    @staticmethod
    async def _nodriver_scroll(page: Any, timeout: int) -> None:
        max_scrolls = 30
        for _ in range(max_scrolls):
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1.5)
            except Exception:
                break

    # ---- CAPTCHA solver (Capsolver) ----------------------------------------

    async def _solve_captcha(self, url: str, html: str) -> bool:
        if not self.captcha_api_key:
            return False

        try:
            import capsolver
            capsolver.api_key = self.captcha_api_key
        except ImportError:
            logger.warning("capsolver package not installed — cannot solve CAPTCHA")
            return False

        task: dict[str, Any] | None = None
        lower = html[:16384].lower()

        if "turnstile" in lower or "cf-turnstile" in lower:
            site_key = self._extract_site_key(html, "cf-turnstile", "sitekey")
            if site_key:
                task = {
                    "type": "AntiTurnstileTaskProxyLess",
                    "websiteURL": url,
                    "websiteKey": site_key,
                }
        elif "g-recaptcha" in lower:
            site_key = self._extract_site_key(html, "g-recaptcha", "data-sitekey")
            if site_key:
                task = {
                    "type": "ReCaptchaV2TaskProxyLess",
                    "websiteURL": url,
                    "websiteKey": site_key,
                }
        elif "h-captcha" in lower:
            site_key = self._extract_site_key(html, "h-captcha", "data-sitekey")
            if site_key:
                task = {
                    "type": "HCaptchaTaskProxyLess",
                    "websiteURL": url,
                    "websiteKey": site_key,
                }

        if task is None:
            logger.info("Could not identify CAPTCHA type / site key for %s", url)
            return False

        try:
            solution = await asyncio.to_thread(capsolver.solve, task)
            logger.info("CAPTCHA solved for %s: type=%s", url, task["type"])
            return bool(solution)
        except Exception as exc:
            logger.warning("CAPTCHA solve failed for %s: %s", url, exc)
            return False

    @staticmethod
    def _extract_site_key(html: str, container_class: str, attr: str) -> str | None:
        import re
        pattern = rf'class\s*=\s*["\'][^"\']*{re.escape(container_class)}[^"\']*["\'][^>]*{re.escape(attr)}\s*=\s*["\']([^"\']+)["\']'
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return m.group(1)
        pattern2 = rf'{re.escape(attr)}\s*=\s*["\']([^"\']+)["\']'
        m2 = re.search(pattern2, html, re.IGNORECASE)
        return m2.group(1) if m2 else None


# ---------------------------------------------------------------------------
# Standalone convenience — use without instantiating StealthFetcher
# ---------------------------------------------------------------------------

async def fetch(url: str, *, render_js: bool = False, timeout: int = 30) -> FetchResult:
    """One-liner: fetch a URL with stealth. Returns FetchResult."""
    return await StealthFetcher().fetch(url, render_js=render_js, timeout=timeout)


async def fetch_many(urls: list[str], *, concurrency: int = 10, **kwargs) -> list[FetchResult]:
    """One-liner: fetch multiple URLs with stealth."""
    return await StealthFetcher().fetch_many(urls, concurrency=concurrency, **kwargs)
