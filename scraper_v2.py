"""Universal Scraper v2 — 360° 全方位爬蟲主控制器，整合 stealth fetch、內容提取、文檔處理、去重、韌性模組。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reuse from scraper.py
# ---------------------------------------------------------------------------
from scraper import (
    MediaItem,
    scrape_media,
    score_image_element,
    extract_og_images,
    extract_videos_from_page,
    find_main_content,
    is_in_main_content,
    is_in_non_content_area,
    is_ytdlp_supported,
    is_direct_media_url,
    extract_urls_from_text,
    generate_filename,
    download_file,
    YTDLP_DOMAINS,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    AUDIO_EXTENSIONS,
)

# ---------------------------------------------------------------------------
# Optional module imports — graceful degradation
# ---------------------------------------------------------------------------
try:
    from stealth_fetch import StealthFetcher, FetchResult
    _HAS_STEALTH = True
except ImportError:
    _HAS_STEALTH = False
    StealthFetcher = None  # type: ignore[assignment,misc]

    @dataclass
    class FetchResult:  # type: ignore[no-redef]
        url: str = ""
        html: str = ""
        status_code: int = 0
        level_used: int = 0
        elapsed_ms: float = 0
        error: str | None = None
        headers: dict = field(default_factory=dict)

        @property
        def ok(self) -> bool:
            return 200 <= self.status_code < 400

        @property
        def soup(self):
            from bs4 import BeautifulSoup
            return BeautifulSoup(self.html, "lxml") if self.html else None

try:
    from content_extractor import ContentExtractor, ExtractionResult
    _HAS_CONTENT = True
except ImportError:
    _HAS_CONTENT = False
    ContentExtractor = None  # type: ignore[assignment,misc]
    ExtractionResult = None  # type: ignore[assignment]

try:
    from document_extractor import DocumentExtractor, DocumentResult
    _HAS_DOCUMENT = True
except ImportError:
    _HAS_DOCUMENT = False
    DocumentExtractor = None  # type: ignore[assignment,misc]
    DocumentResult = None  # type: ignore[assignment]

try:
    from dedup import ContentDeduplicator, ContentHasher
    _HAS_DEDUP = True
except ImportError:
    _HAS_DEDUP = False
    ContentDeduplicator = None  # type: ignore[assignment,misc]
    ContentHasher = None  # type: ignore[assignment]

try:
    from resilience import (
        RateLimiter,
        RetryHandler,
        CircuitBreaker,
        ProxyPool,
        RobotsChecker,
        SitemapParser,
    )
    _HAS_RESILIENCE = True
except ImportError:
    _HAS_RESILIENCE = False
    RateLimiter = None  # type: ignore[assignment,misc]
    RetryHandler = None  # type: ignore[assignment,misc]
    CircuitBreaker = None  # type: ignore[assignment,misc]
    ProxyPool = None  # type: ignore[assignment,misc]
    RobotsChecker = None  # type: ignore[assignment,misc]
    SitemapParser = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Document format registry
# ---------------------------------------------------------------------------
_DOC_EXTENSIONS: set[str] = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls",
    ".pptx", ".ppt", ".odt", ".ods", ".odp",
    ".rtf", ".epub", ".csv", ".tsv", ".txt",
}


def _supported_doc_formats() -> set[str]:
    if _HAS_DOCUMENT and hasattr(DocumentExtractor, "supported_formats"):
        try:
            return set(DocumentExtractor.supported_formats())
        except Exception:
            pass
    return _DOC_EXTENSIONS

# ---------------------------------------------------------------------------
# ScrapeResult
# ---------------------------------------------------------------------------

@dataclass
class ScrapeResult:
    url: str
    fetch_result: FetchResult | None = None
    content: Any | None = None          # ExtractionResult
    media: list[MediaItem] = field(default_factory=list)
    document: Any | None = None         # DocumentResult
    is_duplicate: bool = False
    duplicate_of: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "url": self.url,
            "timestamp": self.timestamp,
            "is_duplicate": self.is_duplicate,
            "duplicate_of": self.duplicate_of,
        }
        if self.fetch_result is not None:
            d["fetch"] = {
                "status_code": self.fetch_result.status_code,
                "ok": self.fetch_result.ok,
                "elapsed_ms": self.fetch_result.elapsed_ms,
                "error": self.fetch_result.error,
            }
        if self.content is not None:
            d["content"] = {
                "title": getattr(self.content, "title", None),
                "word_count": getattr(self.content, "word_count", None),
                "language": getattr(self.content, "language", None),
            }
        if self.media:
            d["media"] = [m.to_dict() for m in self.media]
        if self.document is not None:
            d["document"] = {
                "format": getattr(self.document, "format", None),
                "page_count": getattr(self.document, "page_count", None),
            }
        return d

# ---------------------------------------------------------------------------
# UniversalScraper
# ---------------------------------------------------------------------------

class UniversalScraper:
    def __init__(
        self,
        *,
        proxy_pool: Any | None = None,
        captcha_api_key: str | None = None,
        max_level: int = 3,
        respect_robots: bool = True,
        dedup: bool = True,
        dedup_threshold: float = 0.8,
        rate_limit_delay: float = 1.0,
        max_concurrent: int = 10,
    ):
        self._max_concurrent = max_concurrent
        self._respect_robots = respect_robots

        # --- Stealth Fetcher ---
        if _HAS_STEALTH:
            self.fetcher = StealthFetcher(
                proxy_pool=proxy_pool,
                captcha_api_key=captcha_api_key,
                max_level=max_level,
            )
        else:
            self.fetcher = None
            logger.warning("stealth_fetch not available — falling back to scraper.fetch_page")

        # --- Content Extractor ---
        if _HAS_CONTENT:
            self.content_extractor = ContentExtractor(
                extract_tables=True,
                extract_structured=True,
                output_format="markdown",
            )
        else:
            self.content_extractor = None
            logger.warning("content_extractor not available — text extraction disabled")

        # --- Document Extractor ---
        if _HAS_DOCUMENT:
            self.doc_extractor = DocumentExtractor(use_ocr=False)
        else:
            self.doc_extractor = None
            logger.warning("document_extractor not available — document extraction disabled")

        # --- Deduplicator ---
        if _HAS_DEDUP and dedup:
            self.dedup = ContentDeduplicator(threshold=dedup_threshold)
        else:
            self.dedup = None

        # --- Resilience ---
        if _HAS_RESILIENCE:
            self.rate_limiter = RateLimiter()
            self.retry_handler = RetryHandler()
            self.circuit_breaker = CircuitBreaker()
            self.robots_checker = RobotsChecker()
        else:
            self.rate_limiter = None
            self.retry_handler = None
            self.circuit_breaker = None
            self.robots_checker = None
            logger.warning("resilience module not available — rate limiting / robots.txt disabled")

    # ------------------------------------------------------------------
    # URL routing
    # ------------------------------------------------------------------
    def _route_url(self, url: str) -> str:
        is_media, _ = is_direct_media_url(url)
        if is_media:
            return "direct_media"
        if is_ytdlp_supported(url):
            return "ytdlp"
        ext = os.path.splitext(urlparse(url).path)[1].lower()
        if ext in _supported_doc_formats():
            return "document"
        return "webpage"

    # ------------------------------------------------------------------
    # Core: scrape single URL
    # ------------------------------------------------------------------
    async def scrape(
        self,
        url: str,
        *,
        extract_text: bool = True,
        extract_media: bool = True,
        extract_tables: bool = True,
        extract_structured: bool = True,
        render_js: bool = False,
        check_sizes: bool = True,
    ) -> ScrapeResult:
        result = ScrapeResult(url=url)
        route = self._route_url(url)

        if route == "direct_media":
            _, media_type = is_direct_media_url(url)
            item = MediaItem(url, media_type or "image", score=100, source_url=url)
            item.filename = generate_filename(url)
            result.media = [item]
            return result

        if route == "ytdlp":
            item = MediaItem(url, "video", score=100, source_url=url, context="yt-dlp supported")
            result.media = [item]
            return result

        if route == "document":
            result = await self._handle_document(url, result)
            return result

        # --- webpage ---
        fetch_res = await self._fetch(url, render_js=render_js)
        result.fetch_result = fetch_res

        if fetch_res is None or not fetch_res.ok:
            return result

        # Dedup check on raw HTML
        if self.dedup and fetch_res.html:
            is_dup, dup_of = self.dedup.add_and_check(fetch_res.html, url)
            if is_dup:
                result.is_duplicate = True
                result.duplicate_of = dup_of
                return result

        # Text / structured extraction
        if extract_text and self.content_extractor:
            try:
                result.content = self.content_extractor.extract(fetch_res.html, url)
            except Exception as exc:
                logger.warning("Content extraction failed for %s: %s", url, exc)

        # Media extraction (reuse existing scraper logic)
        if extract_media:
            try:
                media_items = await asyncio.to_thread(
                    scrape_media, url, check_sizes
                )
                result.media = media_items
            except Exception as exc:
                logger.warning("Media extraction failed for %s: %s", url, exc)

        return result

    # ------------------------------------------------------------------
    # Batch scrape
    # ------------------------------------------------------------------
    async def scrape_many(
        self,
        urls: list[str],
        *,
        concurrency: int = 5,
        **kwargs: Any,
    ) -> list[ScrapeResult]:
        sem = asyncio.Semaphore(concurrency)

        async def _guarded(u: str) -> ScrapeResult:
            async with sem:
                if self.rate_limiter:
                    await self.rate_limiter.acquire(urlparse(u).netloc)
                return await self.scrape(u, **kwargs)

        return list(await asyncio.gather(*[_guarded(u) for u in urls]))

    # ------------------------------------------------------------------
    # Crawl (BFS)
    # ------------------------------------------------------------------
    async def crawl(
        self,
        start_url: str,
        *,
        max_depth: int = 2,
        max_pages: int = 50,
        same_domain: bool = True,
        use_sitemap: bool = True,
        **kwargs: Any,
    ) -> list[ScrapeResult]:
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(start_url, 0)])
        results: list[ScrapeResult] = []

        # 1. Sitemap discovery
        if use_sitemap and self.robots_checker:
            try:
                sitemaps = await self.robots_checker.get_sitemaps(start_url)
                for sm_url in sitemaps:
                    if SitemapParser is not None:
                        entries = await SitemapParser.parse(sm_url)
                        for entry in entries[:max_pages]:
                            entry_url = entry.url if hasattr(entry, "url") else str(entry)
                            if entry_url not in visited:
                                queue.append((entry_url, 1))
            except Exception as exc:
                logger.warning("Sitemap parsing failed: %s", exc)

        # 2. BFS
        while queue and len(results) < max_pages:
            url, depth = queue.popleft()
            if url in visited or depth > max_depth:
                continue

            # robots.txt
            if self._respect_robots and self.robots_checker:
                try:
                    allowed = await self.robots_checker.can_fetch(url)
                    if not allowed:
                        logger.info("Blocked by robots.txt: %s", url)
                        continue
                except Exception:
                    pass

            visited.add(url)

            # rate limit
            if self.rate_limiter:
                await self.rate_limiter.acquire(urlparse(url).netloc)

            result = await self.scrape(url, **kwargs)
            results.append(result)

            # extract links
            if depth < max_depth and result.fetch_result and result.fetch_result.ok:
                soup = result.fetch_result.soup
                if soup:
                    links = self._extract_links(soup, url, same_domain)
                    for link in links:
                        if link not in visited:
                            queue.append((link, depth + 1))

        return results

    # ------------------------------------------------------------------
    # Monitor
    # ------------------------------------------------------------------
    async def monitor(
        self,
        url: str,
        *,
        interval: int = 3600,
        callback: Callable[[str, ScrapeResult], Awaitable[None]] | Callable[[str, ScrapeResult], None] | None = None,
    ) -> None:
        if not (_HAS_DEDUP and ContentHasher):
            raise RuntimeError("dedup module required for monitoring")

        last_hash: str | None = None
        logger.info("Monitoring %s every %ds", url, interval)

        while True:
            try:
                result = await self.scrape(url)
                html = result.fetch_result.html if result.fetch_result else ""
                current_hash = ContentHasher.hash_content(html) if html else ""

                if last_hash is not None and current_hash != last_hash:
                    logger.info("Change detected on %s", url)
                    if callback is not None:
                        ret = callback(url, result)
                        if asyncio.iscoroutine(ret) or asyncio.isfuture(ret):
                            await ret

                last_hash = current_hash
            except Exception as exc:
                logger.error("Monitor cycle error for %s: %s", url, exc)

            await asyncio.sleep(interval)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _fetch(self, url: str, *, render_js: bool = False) -> FetchResult:
        if self.fetcher is not None:
            if self.retry_handler:
                return await self.retry_handler.execute(
                    self.fetcher.fetch, url, render_js=render_js
                )
            return await self.fetcher.fetch(url, render_js=render_js)

        # Fallback: sync fetch_page via thread
        from scraper import fetch_page
        resp = await asyncio.to_thread(fetch_page, url)
        if resp is None:
            return FetchResult(url=url, html="", status_code=0, error="All strategies failed")
        return FetchResult(
            url=url,
            html=resp.text,
            status_code=resp.status_code,
            headers=dict(resp.headers),
        )

    async def _handle_document(self, url: str, result: ScrapeResult) -> ScrapeResult:
        if self.doc_extractor is None:
            logger.warning("document_extractor not available — cannot process %s", url)
            return result

        import tempfile
        fetch_res = await self._fetch(url)
        result.fetch_result = fetch_res
        if fetch_res is None or not fetch_res.ok:
            return result

        ext = os.path.splitext(urlparse(url).path)[1].lower() or ".bin"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp_path = tmp.name
            if fetch_res.html:
                tmp.write(fetch_res.html.encode("utf-8", errors="replace"))
            elif hasattr(fetch_res, "_raw") and fetch_res._raw:
                tmp.write(fetch_res._raw)

        try:
            doc_result = await asyncio.to_thread(self.doc_extractor.extract, tmp_path)
            result.document = doc_result
        except Exception as exc:
            logger.warning("Document extraction failed for %s: %s", url, exc)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        return result

    @staticmethod
    def _extract_links(soup: Any, base_url: str, same_domain: bool) -> list[str]:
        base_domain = urlparse(base_url).netloc.lower()
        links: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            full = urljoin(base_url, href)
            parsed = urlparse(full)
            normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            if normalized in seen:
                continue
            if same_domain and parsed.netloc.lower() != base_domain:
                continue
            seen.add(normalized)
            links.append(full)
        return links


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------
__all__ = [
    "ScrapeResult",
    "UniversalScraper",
    "FetchResult",
    "MediaItem",
]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
async def _cli_main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Universal Scraper v2")
    parser.add_argument("urls", nargs="+", help="URL(s) to scrape")
    parser.add_argument("--crawl", action="store_true", help="Enable crawl mode on first URL")
    parser.add_argument("--depth", type=int, default=2, help="Max crawl depth")
    parser.add_argument("--max-pages", type=int, default=50, help="Max pages in crawl mode")
    parser.add_argument("--js", action="store_true", help="Render JavaScript")
    parser.add_argument("--no-media", action="store_true", help="Skip media extraction")
    parser.add_argument("--no-text", action="store_true", help="Skip text extraction")
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--output", "-o", help="Output JSON file")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    scraper = UniversalScraper()

    if args.crawl:
        results = await scraper.crawl(
            args.urls[0],
            max_depth=args.depth,
            max_pages=args.max_pages,
            extract_text=not args.no_text,
            extract_media=not args.no_media,
            render_js=args.js,
        )
    elif len(args.urls) == 1:
        results = [await scraper.scrape(
            args.urls[0],
            extract_text=not args.no_text,
            extract_media=not args.no_media,
            render_js=args.js,
        )]
    else:
        results = await scraper.scrape_many(
            args.urls,
            concurrency=args.concurrency,
            extract_text=not args.no_text,
            extract_media=not args.no_media,
            render_js=args.js,
        )

    output_data = [r.to_dict() for r in results]

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        print(f"Results written to {args.output}")
    else:
        print(json.dumps(output_data, ensure_ascii=False, indent=2))

    print(f"\n--- {len(results)} URL(s) processed ---")
    for r in results:
        status = "DUP" if r.is_duplicate else ("OK" if r.fetch_result and r.fetch_result.ok else "FAIL")
        media_count = len(r.media)
        title = getattr(r.content, "title", None) if r.content else None
        parts = [f"[{status}] {r.url}"]
        if title:
            parts.append(f"title={title!r}")
        if media_count:
            parts.append(f"media={media_count}")
        print("  ".join(parts))


if __name__ == "__main__":
    asyncio.run(_cli_main())
