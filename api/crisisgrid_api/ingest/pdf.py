"""PDF ingest: text per page, content hash for version pinning, and an explicit OCR-needed flag (never silently empty)."""
from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Callable
from datetime import date

from pydantic import BaseModel
from pypdf import PdfReader
from pypdf.errors import PyPdfError


class PdfError(ValueError):
    pass


class IngestedPaper(BaseModel):
    paper_hash: str  # sha256 of the original bytes: pins the exact paper version
    pages: list[str]
    metadata_date: date | None = None
    needs_ocr: bool = False
    ocr_used: bool = False

    @property
    def n_pages(self) -> int:
        return len(self.pages)

    @property
    def text(self) -> str:
        return "\n\n".join(self.pages)


_PDF_DATE = re.compile(r"D:(\d{4})(\d{2})?(\d{2})?")


def _pdf_date(raw: str | None) -> date | None:
    m = _PDF_DATE.match(raw or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1))
    except ValueError:
        return None


def ingest_pdf(data: bytes, *, ocr: Callable[[bytes], list[str]] | None = None, min_chars_per_page: int = 40) -> IngestedPaper:
    """Extract text. If the PDF looks scanned (almost no text) and `ocr` is given, use it; otherwise return
    `needs_ocr=True` so the pipeline can say so instead of analysing an empty paper."""
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages]
        raw_date = reader.metadata.get("/CreationDate") if reader.metadata else None
    except (PyPdfError, ValueError, OSError, KeyError) as e:
        raise PdfError(f"not a readable PDF: {e}") from e
    if not pages:
        raise PdfError("PDF has no pages")
    out = IngestedPaper(paper_hash=hashlib.sha256(data).hexdigest(), pages=pages,
                        metadata_date=_pdf_date(str(raw_date) if raw_date else None))
    if sum(len(p.strip()) for p in pages) < min_chars_per_page * len(pages):
        if ocr is not None:
            out.pages, out.ocr_used = ocr(data), True
        else:
            out.needs_ocr = True
    return out
