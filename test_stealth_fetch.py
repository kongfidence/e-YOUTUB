import json
import time


async def test_stealth_fetch() -> dict:
    results = {
        "module": "stealth_fetch",
        "tests": [],
        "passed": 0,
        "failed": 0,
        "total": 0,
    }

    def record(name: str, passed: bool, details: str, elapsed_ms: float) -> None:
        results["tests"].append(
            {
                "name": name,
                "passed": passed,
                "details": details,
                "elapsed_ms": round(elapsed_ms, 3),
            }
        )
        results["total"] += 1
        if passed:
            results["passed"] += 1
        else:
            results["failed"] += 1

    try:
        from stealth_fetch import StealthFetcher
    except ImportError as exc:
        t0 = time.perf_counter()
        record(
            "import",
            False,
            f"ImportError: {exc!r}",
            (time.perf_counter() - t0) * 1000,
        )
        return results

    t0 = time.perf_counter()
    try:
        fetcher = StealthFetcher()
        r = await fetcher.fetch("https://tls.browserleaks.com/json", timeout=45)
        elapsed = (time.perf_counter() - t0) * 1000
        ja3_ok = False
        chrome_ua = False
        if r.status_code == 200 and r.html:
            try:
                data = json.loads(r.html)
            except json.JSONDecodeError as je:
                record(
                    "tls_fingerprint",
                    False,
                    f"status={r.status_code} level={r.level_used} json_error={je!r}",
                    elapsed,
                )
            else:
                for key in ("ja3_hash", "ja3", "ja3_text"):
                    v = data.get(key)
                    if v is not None and str(v).strip():
                        ja3_ok = True
                        break
                ua = str(data.get("user_agent") or "")
                chrome_ua = "chrome" in ua.lower()
                passed = ja3_ok and r.status_code == 200
                record(
                    "tls_fingerprint",
                    passed,
                    f"status={r.status_code} level={r.level_used} ja3_ok={ja3_ok} chrome_ua={chrome_ua}",
                    elapsed,
                )
        else:
            record(
                "tls_fingerprint",
                False,
                f"status={r.status_code} level={r.level_used} html_len={len(r.html or '')}",
                elapsed,
            )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        record("tls_fingerprint", False, repr(exc), elapsed)

    t0 = time.perf_counter()
    try:
        fetcher = StealthFetcher()
        r = await fetcher.fetch("https://bot.sannysoft.com/", timeout=45)
        elapsed = (time.perf_counter() - t0) * 1000
        h = (r.html or "").lower()
        anti_ok = r.status_code == 200 and ("passed" in h or "failed" not in h)
        record(
            "anti_bot_sannysoft",
            anti_ok,
            f"status={r.status_code} level={r.level_used} len={len(r.html or '')}",
            elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        record("anti_bot_sannysoft", False, repr(exc), elapsed)

    t0 = time.perf_counter()
    try:
        fetcher = StealthFetcher()
        r = await fetcher.fetch("https://www.cloudflare.com/", timeout=45)
        elapsed = (time.perf_counter() - t0) * 1000
        cf_ok = r.status_code == 200 and r.html and "Cloudflare" in r.html
        record(
            "cloudflare_http2",
            cf_ok,
            f"status={r.status_code} level={r.level_used} len={len(r.html or '')}",
            elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        record("cloudflare_http2", False, repr(exc), elapsed)

    t0 = time.perf_counter()
    try:
        fetcher = StealthFetcher()
        urls = [
            "https://httpbin.org/get",
            "https://httpbin.org/headers",
            "https://httpbin.org/user-agent",
        ]
        out = await fetcher.fetch_many(urls, concurrency=3, timeout=45)
        elapsed = (time.perf_counter() - t0) * 1000
        ok = (
            len(out) == 3
            and all(x.status_code == 200 for x in out)
        )
        codes = [x.status_code for x in out]
        record(
            "fetch_many_concurrent",
            ok,
            f"count={len(out)} codes={codes}",
            elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        record("fetch_many_concurrent", False, repr(exc), elapsed)

    t0 = time.perf_counter()
    try:
        fetcher = StealthFetcher(max_level=1)
        r = await fetcher.fetch("https://httpbin.org/get", timeout=45)
        elapsed = (time.perf_counter() - t0) * 1000
        record(
            "max_level_1_fallback",
            True,
            f"no_uncaught status={r.status_code} level={r.level_used} err={r.error!r}",
            elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        record("max_level_1_fallback", False, repr(exc), elapsed)

    return results
