async def test_content_extractor() -> dict:
    import asyncio
    import time

    from content_extractor import ContentExtractor

    results: dict = {
        "module": "content_extractor",
        "tests": [],
        "passed": 0,
        "failed": 0,
        "total": 0,
    }

    def _record(name: str, status: str, elapsed_ms: float, message: str = "") -> None:
        results["tests"].append(
            {"name": name, "status": status, "elapsed_ms": round(elapsed_ms, 2), "message": message}
        )
        results["total"] += 1
        if status == "passed":
            results["passed"] += 1
        elif status == "failed":
            results["failed"] += 1

    async def _fetch(url: str) -> str | None:
        def _sync() -> str | None:
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) test_content_extractor/1.0"
            try:
                import requests

                resp = requests.get(url, timeout=45, headers={"User-Agent": ua})
                resp.raise_for_status()
                return resp.text
            except Exception:
                pass
            try:
                from curl_cffi import requests as creq

                resp = creq.get(
                    url,
                    timeout=45,
                    impersonate="chrome",
                    headers={"User-Agent": ua},
                )
                resp.raise_for_status()
                return resp.text
            except Exception:
                return None

        return await asyncio.to_thread(_sync)

    t0 = time.perf_counter()
    html = await _fetch("https://en.wikipedia.org/wiki/Web_scraping")
    if not html:
        _record("body_extraction", "skipped", (time.perf_counter() - t0) * 1000, "fetch failed")
    else:
        try:
            ext = ContentExtractor()
            res = await asyncio.to_thread(ext.extract, html, "https://en.wikipedia.org/wiki/Web_scraping")
            text = (res.text or "").strip()
            title_ok = bool(res.title) and "web scraping" in res.title.lower()
            lang0 = (res.language or "").lower().split("-", 1)[0]
            ok = len(text) > 500 and title_ok and lang0 == "en"
            _record(
                "body_extraction",
                "passed" if ok else "failed",
                (time.perf_counter() - t0) * 1000,
                f"chars={len(text)} title={res.title!r} language={res.language!r}",
            )
        except Exception as exc:
            _record("body_extraction", "failed", (time.perf_counter() - t0) * 1000, repr(exc))

    t0 = time.perf_counter()
    url_sd = "https://www.imdb.com/title/tt0111161/"
    html_sd = await _fetch(url_sd)
    used = url_sd
    if not html_sd:
        url_fb = "https://en.wikipedia.org/wiki/The_Shawshank_Redemption"
        html_sd = await _fetch(url_fb)
        used = url_fb
    if not html_sd:
        _record("structured_data", "skipped", (time.perf_counter() - t0) * 1000, "fetch failed")
    else:
        try:
            ext = ContentExtractor(extract_tables=False)
            res = await asyncio.to_thread(ext.extract, html_sd, used)
            sd = res.structured_data or {}
            jld = sd.get("json-ld")
            og = sd.get("opengraph")
            has_jld = bool(jld) if not isinstance(jld, list) else len(jld) > 0
            has_og = bool(og) if not isinstance(og, list) else len(og) > 0
            ok = has_jld and has_og
            if not ok and used != "https://en.wikipedia.org/wiki/The_Shawshank_Redemption":
                url_fb = "https://en.wikipedia.org/wiki/The_Shawshank_Redemption"
                html_fb = await _fetch(url_fb)
                if html_fb:
                    used = url_fb
                    res = await asyncio.to_thread(ext.extract, html_fb, used)
                    sd = res.structured_data or {}
                    jld = sd.get("json-ld")
                    og = sd.get("opengraph")
                    has_jld = bool(jld) if not isinstance(jld, list) else len(jld) > 0
                    has_og = bool(og) if not isinstance(og, list) else len(og) > 0
                    ok = has_jld and has_og
            _record(
                "structured_data",
                "passed" if ok else "failed",
                (time.perf_counter() - t0) * 1000,
                f"url={used} json_ld={bool(jld)} opengraph={bool(og)}",
            )
        except Exception as exc:
            _record("structured_data", "failed", (time.perf_counter() - t0) * 1000, repr(exc))

    t0 = time.perf_counter()
    html_tbl = await _fetch("https://en.wikipedia.org/wiki/List_of_countries_and_dependencies_by_population")
    if not html_tbl:
        _record("tables", "skipped", (time.perf_counter() - t0) * 1000, "fetch failed")
    else:
        try:
            ext = ContentExtractor(extract_structured=False)
            res = await asyncio.to_thread(
                ext.extract,
                html_tbl,
                "https://en.wikipedia.org/wiki/List_of_countries_and_dependencies_by_population",
            )
            rows = [t.get("rows", 0) for t in (res.tables or [])]
            ok = any(int(r) > 10 for r in rows)
            _record(
                "tables",
                "passed" if ok else "failed",
                (time.perf_counter() - t0) * 1000,
                f"tables={len(res.tables or [])} max_rows={max(rows) if rows else 0}",
            )
        except Exception as exc:
            _record("tables", "failed", (time.perf_counter() - t0) * 1000, repr(exc))

    t0 = time.perf_counter()
    html_md = await _fetch("https://en.wikipedia.org/wiki/Web_scraping")
    if not html_md:
        _record("markdown_output", "skipped", (time.perf_counter() - t0) * 1000, "fetch failed")
    else:
        try:
            ext = ContentExtractor(output_format="markdown")
            res = await asyncio.to_thread(ext.extract, html_md, "https://en.wikipedia.org/wiki/Web_scraping")
            md = res.text_markdown or ""
            ok = any(ch in md for ch in ("#", "*", "["))
            _record(
                "markdown_output",
                "passed" if ok else "failed",
                (time.perf_counter() - t0) * 1000,
                f"sample_len={len(md)}",
            )
        except Exception as exc:
            _record("markdown_output", "failed", (time.perf_counter() - t0) * 1000, repr(exc))

    t0 = time.perf_counter()
    try:
        ext = ContentExtractor()
        await asyncio.to_thread(ext.extract, "", "https://example.com/")
        await asyncio.to_thread(ext.extract, "<html><unclosed><p>partial", "https://example.com/")
        _record("error_handling", "passed", (time.perf_counter() - t0) * 1000, "")
    except Exception as exc:
        _record("error_handling", "failed", (time.perf_counter() - t0) * 1000, repr(exc))

    return results
