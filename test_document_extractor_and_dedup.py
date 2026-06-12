from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

import requests

from dedup import ContentDeduplicator, ContentHasher
from document_extractor import DocumentExtractor


async def test_document_extractor() -> dict:
    results: dict = {
        "module": "document_extractor",
        "tests": [],
        "passed": 0,
        "failed": 0,
        "total": 0,
    }

    def _record(name: str, passed: bool, detail: str, elapsed_ms: float) -> None:
        results["tests"].append(
            {
                "name": name,
                "passed": passed,
                "detail": detail,
                "elapsed_ms": round(elapsed_ms, 3),
            }
        )
        results["total"] += 1
        if passed:
            results["passed"] += 1
        else:
            results["failed"] += 1

    # 1) PDF download + extract
    t0 = time.perf_counter()
    tmp_dir: str | None = None
    pdf_path: str | None = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="doc_ext_test_")
        url = "https://www.w3.org/WAI/WCAG21/Techniques/pdf/img/table-word.pdf"

        def _download() -> str:
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            p = Path(tmp_dir) / "table-word.pdf"
            p.write_bytes(r.content)
            return str(p)

        pdf_path = await asyncio.to_thread(_download)
        ex = DocumentExtractor()
        res = await asyncio.to_thread(ex.extract, pdf_path)
        text_ok = bool((res.text or "").strip()) or bool((res.text_markdown or "").strip())
        ok = text_ok and getattr(res, "page_count", 0) > 0
        _record(
            "pdf_download_and_extract",
            ok,
            f"text_nonempty={text_ok} page_count={getattr(res, 'page_count', None)}",
            (time.perf_counter() - t0) * 1000,
        )
    except Exception as e:
        _record(
            "pdf_download_and_extract",
            False,
            repr(e),
            (time.perf_counter() - t0) * 1000,
        )
    finally:
        if tmp_dir:
            await asyncio.to_thread(shutil.rmtree, tmp_dir, True)

    # 2) supported_formats contains .pdf
    t0 = time.perf_counter()
    try:
        fmt = await asyncio.to_thread(DocumentExtractor.supported_formats)
        ok = ".pdf" in fmt
        _record(
            "supported_formats_pdf",
            ok,
            f"formats_sample={sorted(list(fmt))[:12]}...",
            (time.perf_counter() - t0) * 1000,
        )
    except Exception as e:
        _record("supported_formats_pdf", False, repr(e), (time.perf_counter() - t0) * 1000)

    # 3) missing file — no crash; expect exception or empty-like result
    t0 = time.perf_counter()
    try:
        ex = DocumentExtractor()
        ghost = str(Path(tempfile.gettempdir()) / "___document_extractor_missing___99999.pdf")
        try:
            res = await asyncio.to_thread(ex.extract, ghost)
            text = (res.text or "") + (res.text_markdown or "")
            ok = len(text.strip()) == 0
            detail = "returned_empty_result" if ok else "non_empty_result"
        except Exception as e:
            ok = isinstance(e, (FileNotFoundError, OSError, ValueError, RuntimeError))
            detail = f"raised_{type(e).__name__}"
        _record("missing_file", ok, detail, (time.perf_counter() - t0) * 1000)
    except Exception as e:
        _record("missing_file", False, repr(e), (time.perf_counter() - t0) * 1000)

    # 4) PDF table extraction (same public PDF; expect list, prefer non-empty)
    t0 = time.perf_counter()
    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="doc_ext_tbl_")
        url = "https://www.w3.org/WAI/WCAG21/Techniques/pdf/img/table-word.pdf"

        def _dl2() -> str:
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            p = Path(tmp_dir) / "table-word.pdf"
            p.write_bytes(r.content)
            return str(p)

        pdf_path = await asyncio.to_thread(_dl2)
        ex = DocumentExtractor()
        tables = await asyncio.to_thread(ex.extract_tables_from_pdf, pdf_path)
        ok = isinstance(tables, list)
        _record(
            "extract_tables_from_pdf",
            ok,
            f"type=list count={len(tables) if isinstance(tables, list) else 'n/a'}",
            (time.perf_counter() - t0) * 1000,
        )
    except Exception as e:
        _record(
            "extract_tables_from_pdf",
            False,
            repr(e),
            (time.perf_counter() - t0) * 1000,
        )
    finally:
        if tmp_dir:
            await asyncio.to_thread(shutil.rmtree, tmp_dir, True)

    return results


async def test_dedup() -> dict:
    results: dict = {
        "module": "dedup",
        "tests": [],
        "passed": 0,
        "failed": 0,
        "total": 0,
    }

    def _record(name: str, passed: bool, detail: str, elapsed_ms: float) -> None:
        results["tests"].append(
            {
                "name": name,
                "passed": passed,
                "detail": detail,
                "elapsed_ms": round(elapsed_ms, 3),
            }
        )
        results["total"] += 1
        if passed:
            results["passed"] += 1
        else:
            results["failed"] += 1

    # 1) identical content twice
    t0 = time.perf_counter()
    try:
        d = ContentDeduplicator(threshold=0.8, num_perm=128, backend="memory")
        s = "Hello world test content"
        await asyncio.to_thread(d.add, s, "doc_a")
        dup, _matched = await asyncio.to_thread(d.is_duplicate, s, None)
        ok = dup is True
        _record("identical_twice", ok, f"is_duplicate={dup}", (time.perf_counter() - t0) * 1000)
    except Exception as e:
        _record("identical_twice", False, repr(e), (time.perf_counter() - t0) * 1000)

    # 2) similar but not identical (threshold 0.5 — char 5-grams are sensitive to word swaps)
    t0 = time.perf_counter()
    try:
        d = ContentDeduplicator(threshold=0.5, num_perm=128, backend="memory")
        base = (
            "The quick brown fox jumps over the lazy dog near the river bank. "
            "Machine learning models require large corpora for training purposes. " * 20
        )
        variant = base.replace("quick", "fast")
        await asyncio.to_thread(d.add, base, "doc_b1")
        dup, _matched = await asyncio.to_thread(d.is_duplicate, variant, None)
        ok = dup is True
        _record(
            "similar_not_identical",
            ok,
            f"is_duplicate={dup}",
            (time.perf_counter() - t0) * 1000,
        )
    except Exception as e:
        _record("similar_not_identical", False, repr(e), (time.perf_counter() - t0) * 1000)

    # 3) completely different
    t0 = time.perf_counter()
    try:
        d = ContentDeduplicator(threshold=0.8, num_perm=128, backend="memory")
        await asyncio.to_thread(d.add, "alpha beta gamma delta epsilon zeta eta theta", "doc_c1")
        dup, _matched = await asyncio.to_thread(
            d.is_duplicate,
            "qwertyuiop asdfghjkl zxcvbnm 1234567890 !@#$%^",
            None,
        )
        ok = dup is False
        _record(
            "completely_different",
            ok,
            f"is_duplicate={dup}",
            (time.perf_counter() - t0) * 1000,
        )
    except Exception as e:
        _record("completely_different", False, repr(e), (time.perf_counter() - t0) * 1000)

    # 4) ContentHasher hash_content + has_changed
    t0 = time.perf_counter()
    try:
        a = "same payload"
        b = "other payload"
        h1 = await asyncio.to_thread(ContentHasher.hash_content, a)
        h2 = await asyncio.to_thread(ContentHasher.hash_content, a)
        h3 = await asyncio.to_thread(ContentHasher.hash_content, b)
        same_ok = h1 == h2 and len(h1) > 0
        diff_ok = h1 != h3
        ch1 = await asyncio.to_thread(ContentHasher.has_changed, h1, h2)
        ch2 = await asyncio.to_thread(ContentHasher.has_changed, h1, h3)
        ok = same_ok and diff_ok and (ch1 is False) and (ch2 is True)
        _record(
            "content_hasher_hash_and_changed",
            ok,
            f"same_hash={same_ok} diff_hash={diff_ok} has_changed_same={ch1} has_changed_diff={ch2}",
            (time.perf_counter() - t0) * 1000,
        )
    except Exception as e:
        _record(
            "content_hasher_hash_and_changed",
            False,
            repr(e),
            (time.perf_counter() - t0) * 1000,
        )

    # 5) ETag header present / absent
    t0 = time.perf_counter()
    try:
        with_etag = {"ETag": '"abc123xyz"'}
        no_etag: dict = {}
        he = await asyncio.to_thread(ContentHasher.hash_etag, with_etag)
        hn = await asyncio.to_thread(ContentHasher.hash_etag, no_etag)
        hn_ok = hn is None or str(hn).strip() == ""
        ok = (he is not None and str(he).strip() != "") and hn_ok
        _record(
            "hash_etag_present_absent",
            ok,
            f"with_etag={he!r} without={hn!r}",
            (time.perf_counter() - t0) * 1000,
        )
    except Exception as e:
        _record("hash_etag_present_absent", False, repr(e), (time.perf_counter() - t0) * 1000)

    # 6) size + clear
    t0 = time.perf_counter()
    try:
        d = ContentDeduplicator(threshold=0.8, num_perm=128, backend="memory")
        await asyncio.to_thread(d.add, "one", "id1")
        await asyncio.to_thread(d.add, "two", "id2")
        await asyncio.to_thread(d.add, "three", "id3")
        sz = await asyncio.to_thread(lambda: d.size)
        await asyncio.to_thread(d.clear)
        sz0 = await asyncio.to_thread(lambda: d.size)
        ok = sz == 3 and sz0 == 0
        _record(
            "size_and_clear",
            ok,
            f"size_before_clear={sz} size_after={sz0}",
            (time.perf_counter() - t0) * 1000,
        )
    except Exception as e:
        _record("size_and_clear", False, repr(e), (time.perf_counter() - t0) * 1000)

    return results
