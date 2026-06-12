"""Async harness for resilience module (Tester D). Imports resilience from E:\\YOUTUB or cwd."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path


def _ensure_resilience_on_path() -> None:
    for base in (Path(r"E:\YOUTUB"), Path(__file__).resolve().parent):
        if (base / "resilience.py").is_file():
            s = str(base)
            if s not in sys.path:
                sys.path.insert(0, s)
            return
    raise ImportError("resilience.py not found under E:\\YOUTUB or this directory")


async def test_resilience() -> dict:
    """
    Run all resilience module checks. Records elapsed_ms per case.
    Network cases (8–10) use try/except; failures are recorded, not re-raised.
    """
    _ensure_resilience_on_path()
    from resilience import (  # noqa: WPS433 (runtime import after path fix)
        CircuitBreaker,
        CircuitOpenError,
        ProxyConfig,
        ProxyPool,
        RateLimiter,
        RetryHandler,
        RobotsChecker,
        SitemapParser,
    )

    results: dict = {
        "module": "resilience",
        "tests": [],
        "passed": 0,
        "failed": 0,
        "total": 0,
    }

    def _append(
        name: str,
        ok: bool,
        elapsed_ms: float,
        *,
        detail: str | None = None,
        error: str | None = None,
    ) -> None:
        row = {
            "name": name,
            "passed": ok,
            "elapsed_ms": round(elapsed_ms, 3),
            "detail": detail,
            "error": error,
        }
        results["tests"].append(row)
        results["total"] += 1
        if ok:
            results["passed"] += 1
        else:
            results["failed"] += 1

    # --- 1. RateLimiter basic spacing ---
    t0 = time.perf_counter()
    try:
        lim = RateLimiter(default_delay=0.1, max_concurrent=10)
        d = "rate.example"
        await lim.acquire(d)
        await lim.acquire(d)
        await lim.acquire(d)
        elapsed = (time.perf_counter() - t0) * 1000
        ok = elapsed >= 200.0 * 0.85
        _append(
            "rate_limiter_basic",
            ok,
            elapsed,
            detail=f"elapsed_ms={elapsed:.1f} (expect >= ~200)",
        )
    except Exception as e:
        _append(
            "rate_limiter_basic",
            False,
            (time.perf_counter() - t0) * 1000,
            error=f"{type(e).__name__}: {e}",
        )

    # --- 2. RateLimiter adaptive (429 up, 200 down) ---
    t0 = time.perf_counter()
    try:
        lim2 = RateLimiter(default_delay=1.0, max_concurrent=10)
        dom = "adapt.example"
        lim2.update_delay(dom, 429)
        after_429 = lim2.stats.get(dom, {}).get("delay")
        lim2.update_delay(dom, 200)
        after_200 = lim2.stats.get(dom, {}).get("delay")
        ok = after_429 == 2.0 and after_200 == 1.8
        _append(
            "rate_limiter_adaptive",
            ok,
            (time.perf_counter() - t0) * 1000,
            detail=f"delay after 429={after_429}, after 200={after_200}",
        )
    except Exception as e:
        _append(
            "rate_limiter_adaptive",
            False,
            (time.perf_counter() - t0) * 1000,
            error=f"{type(e).__name__}: {e}",
        )

    # --- 3. RetryHandler succeeds on 2nd attempt ---
    t0 = time.perf_counter()
    try:
        attempts = 0

        async def flaky_ok():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise asyncio.TimeoutError("first fail")
            return "done"

        rh = RetryHandler(max_retries=5, base_delay=0.05, max_delay=1.0)
        out = await rh.execute(flaky_ok)
        elapsed = (time.perf_counter() - t0) * 1000
        ok = out == "done" and attempts == 2
        _append(
            "retry_handler_success",
            ok,
            elapsed,
            detail=f"attempts={attempts}, result={out!r}",
        )
    except Exception as e:
        _append(
            "retry_handler_success",
            False,
            (time.perf_counter() - t0) * 1000,
            error=f"{type(e).__name__}: {e}",
        )

    # --- 4. RetryHandler exhausts retries ---
    t0 = time.perf_counter()
    try:
        async def always_bad():
            raise OSError("always fail")

        rh2 = RetryHandler(max_retries=2, base_delay=0.05, max_delay=1.0)
        raised = False
        try:
            await rh2.execute(always_bad)
        except OSError:
            raised = True
        ok = raised
        _append(
            "retry_handler_all_fail",
            ok,
            (time.perf_counter() - t0) * 1000,
            detail="OSError raised after retries" if ok else "expected OSError",
        )
    except Exception as e:
        _append(
            "retry_handler_all_fail",
            False,
            (time.perf_counter() - t0) * 1000,
            error=f"{type(e).__name__}: {e}",
        )

    # --- 5. CircuitBreaker three states ---
    t0 = time.perf_counter()
    try:
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.5, success_threshold=2)

        async def boom():
            raise RuntimeError("fail")

        if cb.state != "closed":
            raise AssertionError(f"expected closed, got {cb.state}")

        for _ in range(3):
            try:
                await cb.call(boom)
            except RuntimeError:
                pass

        if cb.state != "open":
            raise AssertionError(f"expected open after 3 fails, got {cb.state}")

        async def noop():
            await asyncio.sleep(0)

        try:
            await cb.call(noop)
        except CircuitOpenError:
            pass
        else:
            raise AssertionError("expected CircuitOpenError while open before recovery")

        await asyncio.sleep(0.55)

        async def ok():
            return 1

        await cb.call(ok)
        if cb.state != "half_open":
            raise AssertionError(f"expected half_open after recovery + probe, got {cb.state}")

        ok_state = True
        _append(
            "circuit_breaker_states",
            ok_state,
            (time.perf_counter() - t0) * 1000,
            detail="closed -> open -> (wait) -> half_open after successful probe",
        )
    except Exception as e:
        _append(
            "circuit_breaker_states",
            False,
            (time.perf_counter() - t0) * 1000,
            error=f"{type(e).__name__}: {e}",
        )

    # --- 6. ProxyPool rotation ---
    t0 = time.perf_counter()
    try:
        urls = ["http://p1.example", "http://p2.example", "http://p3.example"]
        pool = ProxyPool([ProxyConfig(url=u) for u in urls])
        seen = [pool.get_rotating() for _ in range(3)]
        ok = set(seen) == set(urls)
        _append(
            "proxy_pool_rotating",
            ok,
            (time.perf_counter() - t0) * 1000,
            detail=f"sequence={seen}",
        )
    except Exception as e:
        _append(
            "proxy_pool_rotating",
            False,
            (time.perf_counter() - t0) * 1000,
            error=f"{type(e).__name__}: {e}",
        )

    # --- 7. ProxyPool mark_failed reduces available_count ---
    t0 = time.perf_counter()
    try:
        pool2 = ProxyPool([ProxyConfig(url="http://a.x"), ProxyConfig(url="http://b.x")])
        before = pool2.available_count
        pool2.mark_failed("http://a.x")
        after = pool2.available_count
        ok = before == 2 and after == 1
        _append(
            "proxy_pool_mark_failed",
            ok,
            (time.perf_counter() - t0) * 1000,
            detail=f"available {before} -> {after}",
        )
    except Exception as e:
        _append(
            "proxy_pool_mark_failed",
            False,
            (time.perf_counter() - t0) * 1000,
            error=f"{type(e).__name__}: {e}",
        )

    # --- 8. RobotsChecker Wikipedia can_fetch ---
    t0 = time.perf_counter()
    try:
        rc = RobotsChecker(
            user_agent="StealthScraper/1.0",
            cache_ttl=3600,
        )
        article = await rc.can_fetch("https://en.wikipedia.org/wiki/Web_scraping")
        ok = article is True
        _append(
            "robots_checker_wikipedia_fetch",
            ok,
            (time.perf_counter() - t0) * 1000,
            detail=f"article_allowed={article}",
        )
    except Exception as e:
        _append(
            "robots_checker_wikipedia_fetch",
            False,
            (time.perf_counter() - t0) * 1000,
            error=f"{type(e).__name__}: {e}",
        )

    # --- 9. RobotsChecker get_sitemaps Google ---
    t0 = time.perf_counter()
    try:
        rc2 = RobotsChecker(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
            cache_ttl=3600,
        )
        maps = await rc2.get_sitemaps("https://www.google.com/")
        ok = isinstance(maps, list) and len(maps) > 0
        _append(
            "robots_checker_google_sitemaps",
            ok,
            (time.perf_counter() - t0) * 1000,
            detail=f"count={len(maps) if isinstance(maps, list) else 'n/a'} sample={maps[:3]!r}",
        )
    except Exception as e:
        _append(
            "robots_checker_google_sitemaps",
            False,
            (time.perf_counter() - t0) * 1000,
            error=f"{type(e).__name__}: {e}",
        )

    # --- 10. SitemapParser sitemaps.org ---
    t0 = time.perf_counter()
    try:
        entries = await SitemapParser.parse("https://www.sitemaps.org/sitemap.xml")
        ok = len(entries) > 0
        _append(
            "sitemap_parser_sitemaps_org",
            ok,
            (time.perf_counter() - t0) * 1000,
            detail=f"entries={len(entries)}",
        )
    except Exception as e:
        _append(
            "sitemap_parser_sitemaps_org",
            False,
            (time.perf_counter() - t0) * 1000,
            error=f"{type(e).__name__}: {e}",
        )

    return results


if __name__ == "__main__":
    async def _main():
        r = await test_resilience()
        print(r)

    asyncio.run(_main())
