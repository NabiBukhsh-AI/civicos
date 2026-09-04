"""Document parsing and chunking.

Municipal documents are mostly PDFs of scanned notifications, budget tables and
bylaws. The chunker is structure-aware where it can be (headings, numbered
clauses, page boundaries) and falls back to sentence-aware splitting, because a
bylaw clause cut in half produces a citation that is worse than useless.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from civicos.core.config import get_settings
from civicos.core.errors import UnsupportedMediaError
from civicos.core.text import normalise_whitespace

logger = structlog.get_logger(__name__)

#: Headings we treat as section boundaries in municipal documents.
_HEADING = re.compile(
    r"^\s*(?:"
    # "SECTION 4", "CHAPTER II - Waste", "SCHEDULE 1: Fees"
    r"(?:CHAPTER|PART|SECTION|ARTICLE|SCHEDULE|ANNEXURE|APPENDIX|CLAUSE|RULE)"
    r"\s+[IVXLC\d]+(?:[.:)\-]?\s+\S[^\n]{0,90})?"
    # "4.2 Collection points"
    r"|\d+(?:\.\d+)*[.:)]?\s+[A-Z][^\n]{3,90}"
    # "GENERAL PROVISIONS"
    r"|[A-Z][A-Z \-&,]{6,90}"
    r")\s*$",
    re.MULTILINE,
)
_SENTENCE_END = re.compile(r"(?<=[.!?۔])\s+")
_PAGE_MARKER = "\n\n<<<PAGE:{number}>>>\n\n"
_PAGE_PATTERN = re.compile(r"<<<PAGE:(\d+)>>>")


@dataclass(slots=True)
class Chunk:
    content: str
    index: int
    page_number: int | None = None
    section_title: str | None = None
    token_estimate: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedDocument:
    text: str
    page_count: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def estimate_tokens(text: str) -> int:
    """Rough token count. Good enough for budgeting; never used for billing."""
    return max(1, len(text) // 4)


# ------------------------------------------------------------------ parsing --


def parse_document(data: bytes, content_type: str, filename: str = "") -> ParsedDocument:
    """Extract plain text from an uploaded document."""
    normalised = (content_type or "").split(";")[0].strip().lower()

    if normalised == "application/pdf" or filename.lower().endswith(".pdf"):
        return _parse_pdf(data)
    if normalised in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    } or filename.lower().endswith(".docx"):
        return _parse_docx(data)
    if normalised.startswith("text/") or filename.lower().endswith(
        (".txt", ".md", ".csv", ".json")
    ):
        return ParsedDocument(text=data.decode("utf-8", errors="replace"))

    raise UnsupportedMediaError(
        f"Cannot extract text from '{content_type or filename}'.",
        details={"supported": ["application/pdf", "docx", "text/*"]},
    )


def _parse_pdf(data: bytes) -> ParsedDocument:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise UnsupportedMediaError("PDF support requires 'pypdf'.") from exc

    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            content = page.extract_text() or ""
        except Exception as exc:  # a single corrupt page must not lose the file
            logger.warning("pdf_page_extract_failed", page=number, error=str(exc))
            content = ""
        if content.strip():
            # Page markers survive chunking so citations can name a page.
            parts.append(_PAGE_MARKER.format(number=number) + content)

    metadata: dict[str, Any] = {}
    if reader.metadata:
        metadata = {
            key.lstrip("/").lower(): str(value) for key, value in reader.metadata.items() if value
        }
    return ParsedDocument(text="".join(parts), page_count=len(reader.pages), metadata=metadata)


def _parse_docx(data: bytes) -> ParsedDocument:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise UnsupportedMediaError("DOCX support requires 'python-docx'.") from exc

    document = docx.Document(io.BytesIO(data))
    blocks = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                blocks.append(" | ".join(cells))
    return ParsedDocument(text="\n\n".join(blocks))


# ----------------------------------------------------------------- chunking --


def chunk_text(
    text: str,
    *,
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> list[Chunk]:
    """Split text into overlapping, structure-aware chunks."""
    settings = get_settings()
    chunk_size = chunk_size or settings.rag.chunk_size
    overlap = overlap if overlap is not None else settings.rag.chunk_overlap
    overlap = min(overlap, chunk_size // 2)

    if not text or not text.strip():
        return []

    chunks: list[Chunk] = []
    for section_title, page_number, body in _iter_sections(text):
        for piece in _split_body(body, chunk_size, overlap):
            chunks.append(
                Chunk(
                    content=piece,
                    index=len(chunks),
                    page_number=page_number,
                    section_title=section_title,
                    token_estimate=estimate_tokens(piece),
                )
            )
    return chunks


def _iter_sections(text: str) -> list[tuple[str | None, int | None, str]]:
    """Split on headings, tracking the page each section starts on."""
    sections: list[tuple[str | None, int | None, str]] = []
    current_page: int | None = None
    current_title: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append((current_title, current_page, body))
        buffer.clear()

    for line in text.splitlines():
        if page_match := _PAGE_PATTERN.search(line):
            current_page = int(page_match.group(1))
            continue
        if _HEADING.match(line):
            flush()
            current_title = normalise_whitespace(line)[:300]
            continue
        buffer.append(line)
    flush()

    if not sections:
        cleaned = _PAGE_PATTERN.sub("", text).strip()
        return [(None, None, cleaned)] if cleaned else []
    return sections


def _split_body(body: str, chunk_size: int, overlap: int) -> list[str]:
    """Sentence-aware packing so clauses stay intact where possible."""
    body = normalise_whitespace(body)
    if len(body) <= chunk_size:
        return [body] if body else []

    sentences = _SENTENCE_END.split(body)
    pieces: list[str] = []
    current: list[str] = []
    length = 0

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > chunk_size:
            # A single run-on sentence (common in scanned bylaws): hard-wrap it.
            if current:
                pieces.append(" ".join(current))
                current, length = [], 0
            pieces.extend(sentence[i : i + chunk_size] for i in range(0, len(sentence), chunk_size))
            continue
        if length + len(sentence) + 1 > chunk_size and current:
            pieces.append(" ".join(current))
            current, length = _carry_overlap(current, overlap)
        current.append(sentence)
        length += len(sentence) + 1

    if current:
        pieces.append(" ".join(current))
    return [piece for piece in pieces if piece.strip()]


def _carry_overlap(sentences: list[str], overlap: int) -> tuple[list[str], int]:
    """Keep the tail of the previous chunk so context spans the boundary."""
    if overlap <= 0:
        return [], 0
    carried: list[str] = []
    length = 0
    for sentence in reversed(sentences):
        if length + len(sentence) > overlap:
            break
        carried.insert(0, sentence)
        length += len(sentence) + 1
    return carried, length


def chunk_document(
    data: bytes, content_type: str, filename: str = "", **kwargs: Any
) -> tuple[ParsedDocument, list[Chunk]]:
    """Parse then chunk in one call - the ingest pipeline's entry point."""
    parsed = parse_document(data, content_type, filename)
    return parsed, chunk_text(parsed.text, **kwargs)
