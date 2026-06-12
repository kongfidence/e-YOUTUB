"""全方位網頁內容提取模組 — 正文、結構化數據、表格、metadata 一次過搞掂。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from io import StringIO
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graceful imports
# ---------------------------------------------------------------------------

try:
    import trafilatura
    from trafilatura.metadata import extract_metadata as _traf_extract_metadata
    _HAS_TRAFILATURA = True
except ImportError:
    _HAS_TRAFILATURA = False

try:
    import extruct
    _HAS_EXTRUCT = True
except ImportError:
    _HAS_EXTRUCT = False

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

try:
    from lxml import html as lxml_html
    _HAS_LXML = True
except ImportError:
    _HAS_LXML = False

__all__ = ["ExtractionResult", "ContentExtractor"]

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    url: str
    title: str | None = None
    author: str | None = None
    date: str | None = None
    text: str | None = None
    text_markdown: str | None = None
    structured_data: dict[str, Any] = field(default_factory=dict)
    tables: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    language: str | None = None
    word_count: int = 0

# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

_OUTPUT_MAP = {"text": "txt", "markdown": "markdown", "html": "html"}

class ContentExtractor:
    def __init__(
        self,
        *,
        extract_tables: bool = True,
        extract_structured: bool = True,
        output_format: str = "markdown",
    ):
        if output_format not in _OUTPUT_MAP:
            raise ValueError(f"output_format must be one of {list(_OUTPUT_MAP)}")
        self._extract_tables = extract_tables
        self._extract_structured = extract_structured
        self._output_format = output_format

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(self, html: str, url: str) -> ExtractionResult:
        """從 HTML 提取所有內容。"""
        result = ExtractionResult(url=url)

        self._fill_text(html, url, result)
        self._fill_metadata(html, url, result)

        if self._extract_structured:
            result.structured_data = self.extract_structured_data(html, url)

        if self._extract_tables:
            result.tables = self.extract_tables(html)

        if result.text:
            result.word_count = _count_words(result.text)

        return result

    def extract_text(self, html: str, url: str) -> str:
        """只提取正文（plain text）。"""
        text = self._trafilatura_text(html, url, out_format="txt")
        if text:
            return text
        return self._bs4_fallback_text(html)

    def extract_structured_data(self, html: str, url: str) -> dict[str, Any]:
        """提取 JSON-LD / Microdata / RDFa / OpenGraph / Microformat。"""
        if not _HAS_EXTRUCT:
            logger.warning("extruct not installed — structured data extraction skipped")
            return {}
        try:
            data: dict[str, Any] = extruct.extract(
                html,
                base_url=url,
                syntaxes=["json-ld", "microdata", "rdfa", "opengraph", "microformat"],
                uniform=True,
            )
            return {k: v for k, v in data.items() if v}
        except Exception:
            logger.debug("extruct extraction failed", exc_info=True)
            return {}

    def extract_tables(self, html: str) -> list[dict[str, Any]]:
        """提取所有 HTML 表格，每個表格轉為 list-of-dicts。"""
        if not _HAS_PANDAS:
            logger.warning("pandas not installed — table extraction skipped")
            return []
        try:
            dfs = pd.read_html(StringIO(html))
        except ValueError:
            return []
        except Exception:
            logger.debug("pandas.read_html failed", exc_info=True)
            return []

        tables: list[dict[str, Any]] = []
        for i, df in enumerate(dfs):
            if df.empty:
                continue
            records = df.where(df.notna(), None).to_dict(orient="records")
            tables.append({
                "index": i,
                "rows": len(df),
                "columns": list(df.columns),
                "data": records,
            })
        return tables

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fill_text(self, html: str, url: str, result: ExtractionResult) -> None:
        fmt = self._output_format
        if _HAS_TRAFILATURA:
            result.text = self._trafilatura_text(html, url, out_format="txt")
            if fmt == "markdown":
                result.text_markdown = self._trafilatura_text(html, url, out_format="markdown")
            elif fmt == "html":
                result.text_markdown = self._trafilatura_text(html, url, out_format="html")
            else:
                result.text_markdown = result.text
        if not result.text:
            result.text = self._bs4_fallback_text(html)
            result.text_markdown = result.text

    def _fill_metadata(self, html: str, url: str, result: ExtractionResult) -> None:
        if _HAS_TRAFILATURA:
            try:
                meta = _traf_extract_metadata(html, default_url=url)
                if meta:
                    result.title = result.title or meta.title
                    result.author = result.author or meta.author
                    result.date = result.date or meta.date
                    result.language = result.language or meta.language
                    result.metadata["sitename"] = meta.sitename
                    result.metadata["categories"] = meta.categories
                    result.metadata["tags"] = meta.tags
                    result.metadata["description"] = getattr(meta, "description", None)
                    result.metadata["license"] = getattr(meta, "license", None)
                    result.metadata = {k: v for k, v in result.metadata.items() if v}
            except Exception:
                logger.debug("trafilatura metadata extraction failed", exc_info=True)

        if _HAS_BS4 and not all([result.title, result.author, result.date]):
            self._bs4_fallback_metadata(html, result)

    # -- trafilatura wrappers --

    @staticmethod
    def _trafilatura_text(html: str, url: str, *, out_format: str = "txt") -> str | None:
        if not _HAS_TRAFILATURA:
            return None
        try:
            return trafilatura.extract(
                html,
                url=url,
                output_format=out_format,
                include_links=(out_format != "txt"),
                include_tables=True,
                include_images=False,
                include_comments=False,
                favor_precision=True,
                deduplicate=True,
            )
        except Exception:
            logger.debug("trafilatura extract failed", exc_info=True)
            return None

    # -- BeautifulSoup fallbacks --

    @staticmethod
    def _bs4_fallback_text(html: str) -> str:
        if not _HAS_BS4:
            return ""
        soup = BeautifulSoup(html, "lxml" if _HAS_LXML else "html.parser")
        for tag in soup.find_all(["script", "style", "noscript", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return re.sub(r"\n{3,}", "\n\n", text)

    @staticmethod
    def _bs4_fallback_metadata(html: str, result: ExtractionResult) -> None:
        soup = BeautifulSoup(html, "lxml" if _HAS_LXML else "html.parser")
        title_tag = soup.find("title")
        if title_tag:
            result.title = title_tag.get_text(strip=True)

        for meta in soup.find_all("meta"):
            name = (meta.get("name") or meta.get("property") or "").lower()
            content = meta.get("content", "")
            if not content:
                continue
            if name in ("author", "article:author"):
                result.author = result.author or content
            elif name in ("date", "article:published_time", "datePublished"):
                result.date = result.date or content
            elif name == "description":
                result.metadata["description"] = content
            elif name in ("og:title",):
                result.title = result.title or content

        html_tag = soup.find("html")
        if html_tag:
            result.language = result.language or html_tag.get("lang")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _count_words(text: str) -> int:
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]", text))
    latin = len(re.findall(r"[A-Za-z]+", text))
    return cjk + latin


# ---------------------------------------------------------------------------
# Standalone convenience — use without instantiating ContentExtractor
# ---------------------------------------------------------------------------

def extract(html: str, url: str = "", *, output_format: str = "markdown") -> ExtractionResult:
    """One-liner: extract text from HTML. Returns ExtractionResult."""
    return ContentExtractor(output_format=output_format).extract(html, url)


def extract_text(html: str, url: str = "") -> str:
    """One-liner: HTML → plain text string."""
    return ContentExtractor().extract_text(html, url)
