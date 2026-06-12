"""文檔提取模組 — 支援 PDF/DOCX/PPTX/XLSX，自動偵測格式並提取文字、表格、圖片。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from docling.document_converter import DocumentConverter
    from docling.datamodel.base_models import InputFormat

    HAS_DOCLING = True
except ImportError:
    HAS_DOCLING = False

try:
    import pymupdf4llm
    import pymupdf

    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

try:
    import pdfplumber

    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

__all__ = ["DocumentResult", "DocumentExtractor"]

FORMAT_MAP: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "doc",
    ".pptx": "pptx",
    ".ppt": "ppt",
    ".xlsx": "xlsx",
    ".xls": "xls",
}


@dataclass
class DocumentResult:
    filepath: str
    text: str | None = None
    text_markdown: str | None = None
    tables: list[dict] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    page_count: int = 0
    metadata: dict = field(default_factory=dict)
    format: str = ""


class DocumentExtractor:
    """從 PDF/DOCX/PPTX/XLSX 提取結構化內容。

    優先使用 docling (AI layout analysis)，若未安裝則 fallback 到
    pymupdf4llm (快速 Markdown) + pdfplumber (表格)。
    """

    def __init__(self, *, use_ocr: bool = False, output_dir: str | None = None):
        self._use_ocr = use_ocr
        self._output_dir = output_dir
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        self._docling_converter: DocumentConverter | None = None
        if HAS_DOCLING:
            try:
                self._docling_converter = DocumentConverter()
            except Exception as exc:
                logger.warning("docling init failed, falling back: %s", exc)

    def extract(self, filepath: str) -> DocumentResult:
        """自動偵測格式並提取內容。"""
        ext = Path(filepath).suffix.lower()
        fmt = FORMAT_MAP.get(ext)
        if fmt is None:
            raise ValueError(f"Unsupported format: {ext}")

        if fmt == "pdf":
            return self.extract_pdf(filepath)

        if self._docling_converter is not None:
            return self._extract_via_docling(filepath, fmt)

        raise RuntimeError(
            f"No backend available for .{fmt} — install docling: pip install docling"
        )

    # ------------------------------------------------------------------
    # PDF
    # ------------------------------------------------------------------

    def extract_pdf(self, filepath: str) -> DocumentResult:
        if self._docling_converter is not None:
            return self._extract_via_docling(filepath, "pdf")
        if HAS_PYMUPDF:
            return self._extract_pdf_pymupdf(filepath)
        raise RuntimeError(
            "No PDF backend — install docling or pymupdf4llm: "
            "pip install docling  OR  pip install pymupdf4llm"
        )

    def extract_tables_from_pdf(self, filepath: str) -> list[dict]:
        if HAS_PDFPLUMBER:
            return self._tables_pdfplumber(filepath)
        if self._docling_converter is not None:
            result = self._extract_via_docling(filepath, "pdf")
            return result.tables
        return []

    # ------------------------------------------------------------------
    # docling backend
    # ------------------------------------------------------------------

    def _extract_via_docling(self, filepath: str, fmt: str) -> DocumentResult:
        assert self._docling_converter is not None
        conv = self._docling_converter.convert(filepath, raises_on_error=False)
        doc = conv.document

        text_md = doc.export_to_markdown()
        text_plain = doc.export_to_text() if hasattr(doc, "export_to_text") else None

        tables: list[dict] = []
        if hasattr(doc, "tables") and doc.tables:
            for i, tbl in enumerate(doc.tables):
                table_data: dict = {"index": i}
                if hasattr(tbl, "export_to_dataframe"):
                    try:
                        df = tbl.export_to_dataframe()
                        table_data["headers"] = list(df.columns)
                        table_data["rows"] = df.values.tolist()
                    except Exception:
                        pass
                elif hasattr(tbl, "export_to_markdown"):
                    table_data["markdown"] = tbl.export_to_markdown()
                tables.append(table_data)

        images: list[str] = []
        if self._output_dir and hasattr(doc, "pictures") and doc.pictures:
            for i, pic in enumerate(doc.pictures):
                if hasattr(pic, "image") and pic.image and hasattr(pic.image, "pil_image"):
                    try:
                        img = pic.image.pil_image
                        img_path = os.path.join(
                            self._output_dir,
                            f"{Path(filepath).stem}_img_{i}.png",
                        )
                        img.save(img_path)
                        images.append(img_path)
                    except Exception:
                        pass

        page_count = 0
        if hasattr(conv, "input") and hasattr(conv.input, "page_count"):
            page_count = conv.input.page_count

        metadata: dict = {}
        if hasattr(doc, "name"):
            metadata["title"] = doc.name

        return DocumentResult(
            filepath=filepath,
            text=text_plain,
            text_markdown=text_md,
            tables=tables,
            images=images,
            page_count=page_count,
            metadata=metadata,
            format=fmt,
        )

    # ------------------------------------------------------------------
    # pymupdf / pymupdf4llm backend (PDF only)
    # ------------------------------------------------------------------

    def _extract_pdf_pymupdf(self, filepath: str) -> DocumentResult:
        md = pymupdf4llm.to_markdown(
            filepath,
            force_ocr=self._use_ocr,
        )

        doc = pymupdf.open(filepath)
        page_count = len(doc)

        metadata: dict = {}
        raw_meta = doc.metadata or {}
        for key in ("title", "author", "subject", "creator", "creationDate", "modDate"):
            val = raw_meta.get(key)
            if val:
                metadata[key] = val

        plain_text_parts: list[str] = []
        for page in doc:
            plain_text_parts.append(page.get_text())
        plain_text = "\n".join(plain_text_parts)

        images: list[str] = []
        if self._output_dir:
            for page_num, page in enumerate(doc):
                for img_idx, img_info in enumerate(page.get_images(full=True)):
                    xref = img_info[0]
                    try:
                        pix = pymupdf.Pixmap(doc, xref)
                        if pix.width < 100 or pix.height < 100:
                            continue
                        if pix.n - pix.alpha > 3:
                            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                        img_path = os.path.join(
                            self._output_dir,
                            f"{Path(filepath).stem}_p{page_num}_img{img_idx}.png",
                        )
                        pix.save(img_path)
                        images.append(img_path)
                    except Exception:
                        pass

        doc.close()

        tables = self._tables_pdfplumber(filepath) if HAS_PDFPLUMBER else []

        return DocumentResult(
            filepath=filepath,
            text=plain_text,
            text_markdown=md,
            tables=tables,
            images=images,
            page_count=page_count,
            metadata=metadata,
            format="pdf",
        )

    # ------------------------------------------------------------------
    # pdfplumber tables
    # ------------------------------------------------------------------

    @staticmethod
    def _tables_pdfplumber(filepath: str) -> list[dict]:
        results: list[dict] = []
        try:
            with pdfplumber.open(filepath) as pdf:
                for page_num, page in enumerate(pdf.pages):
                    tables = page.extract_tables(
                        table_settings={
                            "snap_tolerance": 4,
                            "join_tolerance": 4,
                        }
                    )
                    for tbl_idx, table in enumerate(tables):
                        if not table or len(table) < 2:
                            continue
                        headers = [str(c).strip() if c else "" for c in table[0]]
                        rows = [
                            [str(c).strip() if c else "" for c in row]
                            for row in table[1:]
                        ]
                        results.append(
                            {
                                "page": page_num,
                                "index": tbl_idx,
                                "headers": headers,
                                "rows": rows,
                            }
                        )
        except Exception as exc:
            logger.warning("pdfplumber table extraction failed: %s", exc)
        return results

    # ------------------------------------------------------------------
    # util
    # ------------------------------------------------------------------

    @staticmethod
    def supported_formats() -> set[str]:
        return {".pdf", ".docx", ".pptx", ".xlsx", ".doc", ".ppt", ".xls"}


# ---------------------------------------------------------------------------
# Standalone convenience — use without instantiating DocumentExtractor
# ---------------------------------------------------------------------------

def extract(filepath: str, *, use_ocr: bool = False) -> "DocumentResult":
    """One-liner: extract text from a document file. Returns DocumentResult."""
    return DocumentExtractor(use_ocr=use_ocr).extract(filepath)
