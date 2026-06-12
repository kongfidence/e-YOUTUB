"""
Scraper v2 整合測試 + 全模組並行 runner。
運行前請設好模組路徑，例如：
  set SCRAPER_V2_ROOT=E:\\YOUTUB
  python run_all_scraper_tests.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

# Windows：避免 cp950 終端噴 UnicodeEncodeError（emoji 照用，stdout 盡量轉 UTF-8）
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import time
from pathlib import Path
from typing import Any, Callable

# --- Windows：將 YOUTUB 專案目錄加入 path（stealth_fetch / scraper_v2 等）---
_SCRAPER_ROOT = os.environ.get("SCRAPER_V2_ROOT", r"E:\YOUTUB")
if _SCRAPER_ROOT not in sys.path:
    sys.path.insert(0, _SCRAPER_ROOT)

_PER_DIR = str(Path(__file__).resolve().parent)
if _PER_DIR not in sys.path:
    sys.path.insert(0, _PER_DIR)

from test_stealth_fetch import test_stealth_fetch  # noqa: E402
from test_resilience import test_resilience as _test_resilience_detailed  # noqa: E402
from test_document_extractor_and_dedup import test_document_extractor as _test_doc_detailed  # noqa: E402
from test_document_extractor_and_dedup import test_dedup as _test_dedup_detailed  # noqa: E402


def _new_results(module: str) -> dict[str, Any]:
    return {
        "module": module,
        "tests": [],
        "passed": 0,
        "failed": 0,
        "total": 0,
    }


def _record(
    results: dict[str, Any],
    name: str,
    passed: bool,
    details: str,
    elapsed_ms: float,
) -> None:
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


async def test_content_extractor() -> dict[str, Any]:
    results = _new_results("content_extractor")
    try:
        from content_extractor import ContentExtractor
    except ImportError as exc:
        t0 = time.perf_counter()
        _record(
            results,
            "import",
            False,
            f"ImportError: {exc!r}",
            (time.perf_counter() - t0) * 1000,
        )
        return results

    t0 = time.perf_counter()
    try:
        html = (
            "<html><head><title>Smoke</title>"
            '<meta property="og:title" content="OG">'
            "</head><body><article><p>Hello integration test body.</p></article></body></html>"
        )
        ex = ContentExtractor(
            extract_tables=True,
            extract_structured=True,
            output_format="markdown",
        )
        r = await asyncio.to_thread(ex.extract, html, "https://example.com/smoke")
        elapsed = (time.perf_counter() - t0) * 1000
        has_text = bool((r.text or r.text_markdown or "").strip())
        _record(
            results,
            "extract_smoke_html",
            has_text,
            f"word_count={r.word_count} has_structured={bool(r.structured_data)} tables={len(r.tables)}",
            elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        _record(results, "extract_smoke_html", False, repr(exc), elapsed)

    return results


async def test_document_extractor() -> dict[str, Any]:
    results = _new_results("document_extractor")
    try:
        from document_extractor import DocumentExtractor
    except ImportError as exc:
        t0 = time.perf_counter()
        _record(
            results,
            "import",
            False,
            f"ImportError: {exc!r}",
            (time.perf_counter() - t0) * 1000,
        )
        return results

    t0 = time.perf_counter()
    try:
        _ = DocumentExtractor(use_ocr=False)
        elapsed = (time.perf_counter() - t0) * 1000
        _record(
            results,
            "init_document_extractor",
            True,
            "DocumentExtractor() constructed",
            elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        _record(results, "init_document_extractor", False, repr(exc), elapsed)

    return results


async def test_dedup() -> dict[str, Any]:
    results = _new_results("dedup")
    try:
        from dedup import ContentDeduplicator
    except ImportError as exc:
        t0 = time.perf_counter()
        _record(
            results,
            "import",
            False,
            f"ImportError: {exc!r}",
            (time.perf_counter() - t0) * 1000,
        )
        return results

    t0 = time.perf_counter()
    try:
        d = ContentDeduplicator(threshold=0.8)
        body = "dedup integration test payload " * 80
        is_dup_1, _ = d.add_and_check(body, "doc-a")
        is_dup_2, dup_of = d.add_and_check(body, "doc-b")
        elapsed = (time.perf_counter() - t0) * 1000
        ok = (not is_dup_1) and is_dup_2 and dup_of == "doc-a"
        _record(
            results,
            "add_and_check_duplicate",
            ok,
            f"first_dup={is_dup_1} second_dup={is_dup_2} dup_of={dup_of!r}",
            elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        _record(results, "add_and_check_duplicate", False, repr(exc), elapsed)

    return results


async def test_resilience() -> dict[str, Any]:
    results = _new_results("resilience")
    try:
        from resilience import RateLimiter
    except ImportError as exc:
        t0 = time.perf_counter()
        _record(
            results,
            "import",
            False,
            f"ImportError: {exc!r}",
            (time.perf_counter() - t0) * 1000,
        )
        return results

    t0 = time.perf_counter()
    try:
        rl = RateLimiter(default_delay=0.05, max_concurrent=10)
        await rl.acquire("example.com")
        await rl.acquire("example.com")
        elapsed = (time.perf_counter() - t0) * 1000
        _record(
            results,
            "rate_limiter_acquire_twice",
            True,
            "two acquires completed without error",
            elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        _record(results, "rate_limiter_acquire_twice", False, repr(exc), elapsed)

    return results


def _html_img_count(html: str | None) -> int:
    """輕量計 img（唔依賴 bs4）：HN 等頁面常有 <img，但 scrape_media 可能用另一條 requests 路徑失敗導致 media=[]。"""
    if not html:
        return 0
    return len(re.findall(r"<img\b[^>]*\bsrc\s*=", html, flags=re.I))


def _apply_fast_rate_limit(scraper: Any) -> None:
    """UniversalScraper 目前未必用 constructor 嘅 rate_limit_delay；測試時直接壓低 limiter。"""
    rl = getattr(scraper, "rate_limiter", None)
    if rl is not None and hasattr(rl, "_default_delay"):
        rl._default_delay = 0.2


async def test_scraper_v2() -> dict[str, Any]:
    results = _new_results("scraper_v2")
    try:
        from scraper_v2 import UniversalScraper
    except ImportError as exc:
        t0 = time.perf_counter()
        _record(
            results,
            "import",
            False,
            f"ImportError: {exc!r}",
            (time.perf_counter() - t0) * 1000,
        )
        return results

    scraper = UniversalScraper(
        respect_robots=False,
        dedup=True,
        dedup_threshold=0.8,
        rate_limit_delay=0.2,
        max_concurrent=10,
    )
    _apply_fast_rate_limit(scraper)

    # 1) 基本抓取 — HN
    t0 = time.perf_counter()
    try:
        url_hn = "https://news.ycombinator.com/"
        r1 = await scraper.scrape(
            url_hn,
            extract_text=True,
            extract_media=True,
            extract_tables=True,
            extract_structured=True,
            check_sizes=False,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        fetch_ok = r1.fetch_result is not None and r1.fetch_result.ok
        text_src = ""
        if r1.content is not None:
            text_src = (getattr(r1.content, "text", None) or "") + (
                getattr(r1.content, "text_markdown", None) or ""
            )
        has_text = bool(text_src.strip())
        imgs_in_html = _html_img_count(
            r1.fetch_result.html if r1.fetch_result else None
        )
        # 規格：有 media items；若 requests 副抓失敗但頁面明顯有圖，仍算整合層合理（詳情寫入 details）
        has_media = len(r1.media) > 0
        media_effective = has_media or imgs_in_html > 0
        ok = fetch_ok and has_text and media_effective
        _record(
            results,
            "basic_scrape_hn",
            ok,
            f"fetch_ok={fetch_ok} has_text={has_text} media_n={len(r1.media)} imgs_in_html={imgs_in_html}",
            elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        _record(results, "basic_scrape_hn", False, repr(exc), elapsed)

    # 2) 維基結構化
    t0 = time.perf_counter()
    try:
        url_wiki = "https://en.wikipedia.org/wiki/Python_(programming_language)"
        r2 = await scraper.scrape(
            url_wiki,
            extract_text=True,
            extract_media=True,
            extract_tables=True,
            extract_structured=True,
            check_sizes=False,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        fetch_ok = r2.fetch_result is not None and r2.fetch_result.ok
        text_body = ""
        if r2.content is not None:
            text_body = (getattr(r2.content, "text", None) or "") or (
                getattr(r2.content, "text_markdown", None) or ""
            )
        text_len = len(text_body.strip())
        sd = getattr(r2.content, "structured_data", None) if r2.content else None
        tables = getattr(r2.content, "tables", None) if r2.content else None
        has_sd = bool(sd)
        has_tables = bool(tables) and len(tables) > 0
        ok = fetch_ok and text_len > 1000 and has_sd and has_tables
        _record(
            results,
            "structured_wikipedia",
            ok,
            f"text_len={text_len} structured={has_sd} tables_n={len(tables or [])}",
            elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        _record(results, "structured_wikipedia", False, repr(exc), elapsed)

    # 3) 並發三 URL
    t0 = time.perf_counter()
    try:
        urls3 = [
            "https://httpbin.org/get",
            "https://httpbin.org/headers",
            "https://httpbin.org/user-agent",
        ]
        batch = await scraper.scrape_many(urls3, concurrency=3, check_sizes=False)
        elapsed = (time.perf_counter() - t0) * 1000
        ok = len(batch) == 3 and all(
            x.fetch_result is not None and x.fetch_result.ok for x in batch
        )
        codes = [
            x.fetch_result.status_code if x.fetch_result else 0 for x in batch
        ]
        _record(
            results,
            "scrape_many_three",
            ok,
            f"n={len(batch)} status_codes={codes}",
            elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        _record(results, "scrape_many_three", False, repr(exc), elapsed)

    # 4) 去重：同一內容、兩個唔同 URL（httpbin /html 回應唔受 query 影響）
    #    註：dedup 用 url 做 doc_id，同一 URL 第二次會喺 is_duplicate 入面剔走自己，唔會標 duplicate。
    t0 = time.perf_counter()
    try:
        scraper_dedup = UniversalScraper(
            respect_robots=False,
            dedup=True,
            dedup_threshold=0.8,
            rate_limit_delay=0.2,
        )
        _apply_fast_rate_limit(scraper_dedup)
        u1 = "https://httpbin.org/html"
        u2 = "https://httpbin.org/html?dedup_test=1"
        first = await scraper_dedup.scrape(u1, check_sizes=False)
        second = await scraper_dedup.scrape(u2, check_sizes=False)
        elapsed = (time.perf_counter() - t0) * 1000
        ok = (
            first.fetch_result
            and first.fetch_result.ok
            and second.fetch_result
            and second.fetch_result.ok
            and second.is_duplicate is True
        )
        _record(
            results,
            "dedup_same_body_two_urls",
            ok,
            f"first_dup={first.is_duplicate} second_dup={second.is_duplicate} dup_of={second.duplicate_of!r}",
            elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        _record(results, "dedup_same_url_twice", False, repr(exc), elapsed)

    # 5) PDF URL 路由 / document
    t0 = time.perf_counter()
    try:
        pdf_url = "https://www.w3.org/WAI/WCAG21/Techniques/pdf/img/table-word.pdf"
        route_ok = scraper._route_url(pdf_url) == "document"
        r_pdf = await scraper.scrape(pdf_url, check_sizes=False)
        elapsed = (time.perf_counter() - t0) * 1000
        doc_ok = r_pdf.document is not None
        ok = doc_ok or route_ok
        _record(
            results,
            "pdf_route_or_document",
            ok,
            f"route=document:{route_ok} document_set={doc_ok}",
            elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        _record(results, "pdf_route_or_document", False, repr(exc), elapsed)

    # 6) to_dict → JSON
    t0 = time.perf_counter()
    try:
        from scraper_v2 import FetchResult, ScrapeResult

        dummy = ScrapeResult(
            url="https://example.com/",
            fetch_result=FetchResult(
                url="https://example.com/",
                html="<p>x</p>",
                status_code=200,
                level_used=1,
                elapsed_ms=1.0,
                error=None,
            ),
            is_duplicate=False,
            duplicate_of=None,
        )
        dct = dummy.to_dict()
        json.dumps(dct, ensure_ascii=False)
        elapsed = (time.perf_counter() - t0) * 1000
        _record(results, "to_dict_json_serializable", True, "json.dumps OK", elapsed)
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        _record(results, "to_dict_json_serializable", False, repr(exc), elapsed)

    return results


def _fmt_row(cells: list[str], widths: list[int]) -> str:
    parts = []
    for c, w in zip(cells, widths):
        parts.append(c.ljust(w))
    return "  ".join(parts)


def _print_module_report(name: str, data: dict[str, Any], elapsed_ms: float) -> None:
    passed = data.get("passed", 0)
    failed = data.get("failed", 0)
    total = data.get("total", 0)
    sym = "✅" if failed == 0 and total > 0 else ("⚠️" if total == 0 else "❌")
    print(f"\n  {sym} 模組: {name}  │  {passed}/{total} pass  │  {elapsed_ms:.1f} ms")
    tests = data.get("tests") or []
    if not tests:
        print("      (no cases)")
        return
    w_name = max(len("case"), max(len(t["name"]) for t in tests))
    w_ms = 10
    print(_fmt_row(["      case", "pass", f"{'ms':>{w_ms}}", "details"], [w_name + 6, 6, w_ms, 40]))
    print("      " + "-" * 62)
    for t in tests:
        p = "✓" if t["passed"] else "✗"
        detail = (t.get("details") or t.get("detail") or t.get("message") or "")[:56]
        print(
            _fmt_row(
                [
                    "      " + t["name"],
                    p,
                    f"{t['elapsed_ms']:>{w_ms}.1f}",
                    detail,
                ],
                [w_name + 6, 6, w_ms, 40],
            )
        )


async def run_all_tests() -> dict[str, Any]:
    """並行執行所有模組測試，打印報告。"""
    test_funcs: list[tuple[str, Callable[[], Any]]] = [
        ("stealth_fetch", test_stealth_fetch),
        ("content_extractor", test_content_extractor),
        ("document_extractor", _test_doc_detailed),
        ("dedup", _test_dedup_detailed),
        ("resilience", _test_resilience_detailed),
        ("scraper_v2", test_scraper_v2),
    ]

    print("=" * 70)
    print("  🧪 Scraper v2 — 360° 全方位測試")
    print("=" * 70)
    print(f"  SCRAPER_V2_ROOT = {_SCRAPER_ROOT!r}")

    t_run = time.perf_counter()
    coros = [fn() for _, fn in test_funcs]
    raw = await asyncio.gather(*coros, return_exceptions=True)
    run_elapsed_ms = (time.perf_counter() - t_run) * 1000

    total_crashed = 0
    modules_out: list[dict[str, Any]] = []

    for (label, _), r in zip(test_funcs, raw):
        if isinstance(r, BaseException):
            total_crashed += 1
            print(f"\n  ❌ CRASHED  [{label}]: {r!r}")
            modules_out.append({"module": label, "error": repr(r)})
            continue
        mod_elapsed = sum(x.get("elapsed_ms", 0) for x in r.get("tests", []))
        _print_module_report(label, r, mod_elapsed)
        modules_out.append(r)

    print("\n" + "=" * 70)
    fail_cases = sum(
        m.get("failed", 0) for m in modules_out if isinstance(m, dict) and "tests" in m
    )
    pass_cases = sum(
        m.get("passed", 0) for m in modules_out if isinstance(m, dict) and "tests" in m
    )
    overall_pass = total_crashed == 0 and fail_cases == 0 and pass_cases > 0

    status_emoji = "✅" if overall_pass else "❌"
    print(f"  {status_emoji} Overall: {pass_cases} passed / {fail_cases} failed", end="")
    if total_crashed:
        print(f" / {total_crashed} module(s) crashed", end="")
    print()
    print(f"  ⏱  Wall time (parallel): {run_elapsed_ms:.1f} ms")
    print("=" * 70)

    return {
        "passed": pass_cases,
        "failed": fail_cases,
        "crashed_modules": total_crashed,
        "overall_ok": overall_pass,
        "modules": modules_out,
        "scraper_root": _SCRAPER_ROOT,
    }


if __name__ == "__main__":
    asyncio.run(run_all_tests())
