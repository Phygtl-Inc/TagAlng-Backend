"""Community knowledge documents: file → Markdown → heading sections.

Pure text work, no database. Every upload (MD, DOCX, PDF) becomes Markdown here, so the
rest of the pipeline only ever sees one format. PDFs are read by Gemini in small page
batches (it keeps headings and tables, and reads scanned pages); pypdf's plain text is
the fallback when Vertex is unavailable or a batch fails.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

KIND_MD = "md"
KIND_DOCX = "docx"
KIND_PDF = "pdf"

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MIME_KINDS = {
    "text/markdown": KIND_MD,
    "text/x-markdown": KIND_MD,
    "text/plain": KIND_MD,
    "application/pdf": KIND_PDF,
    _DOCX_MIME: KIND_DOCX,
}
_EXT_KINDS = {".md": KIND_MD, ".markdown": KIND_MD, ".txt": KIND_MD, ".pdf": KIND_PDF, ".docx": KIND_DOCX}
KIND_MIME = {KIND_MD: "text/markdown", KIND_PDF: "application/pdf", KIND_DOCX: _DOCX_MIME}

# One section stays readable as a unit; longer ones split at paragraph breaks.
SECTION_MAX_CHARS = 6000
PDF_MAX_PAGES = 200
_PDF_PAGES_PER_CALL = 4


class ConvertError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def approx_tokens(text: str) -> int:
    """No tokenizer in the worker; ~4 characters a token is close enough for budgeting."""
    return (len(text or "") + 3) // 4


def detect_kind(filename: str | None, content_type: str | None) -> str | None:
    """Browsers send .md as '' or application/octet-stream, so the extension decides
    when the declared type is missing or generic."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _MIME_KINDS:
        return _MIME_KINDS[ct]
    name = (filename or "").strip().lower()
    for ext, kind in _EXT_KINDS.items():
        if name.endswith(ext):
            return kind
    return None


# ── to Markdown ─────────────────────────────────────────────────────────────────


def _clean(text: str) -> str:
    text = (text or "").replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _markdown_from_md(raw: bytes) -> str:
    return _clean(raw.decode("utf-8", errors="replace"))


# mammoth backslash-escapes Markdown punctuation ("3\.5", "a\_b"). Lana quotes these lines
# back and the quote check compares against them, so the escapes come out.
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")


def _markdown_from_docx(raw: bytes) -> str:
    import mammoth

    try:
        result = mammoth.convert_to_markdown(io.BytesIO(raw))
    except Exception as exc:
        raise ConvertError("docx_unreadable") from exc
    text = _MD_ESCAPE.sub(r"\1", result.value or "")
    # mammoth writes anchors for bookmarks: <a id="…"></a>
    text = re.sub(r'<a id="[^"]*"></a>', "", text)
    return _clean(text)


def _pdf_reader(raw: bytes):
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            reader.decrypt("")
        _ = len(reader.pages)
        return reader
    except Exception as exc:
        raise ConvertError("pdf_unreadable") from exc


def _pdf_slice(reader, start: int, stop: int) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for i in range(start, stop):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _pdf_text(reader, start: int, stop: int) -> str:
    parts: list[str] = []
    for i in range(start, stop):
        try:
            parts.append(reader.pages[i].extract_text() or "")
        except Exception:
            logger.warning("community_docs.pdf_text_page_failed page=%d", i, exc_info=True)
    return "\n\n".join(p.strip() for p in parts if p.strip())


_PDF_TRANSCRIBE = (
    "Transcribe these PDF pages into clean Markdown. Keep every word of the body text "
    "exactly as written, in its original language — do not summarise, translate, reword "
    "or add anything. Rejoin words and sentences that the page layout broke across lines or "
    "columns, so each paragraph reads as one continuous run of text. Mark real headings "
    "with #, ## or ### by their visual level; a short line that titles the text below it is "
    "a heading. Turn "
    "tables into Markdown tables. Drop running page headers, footers and page numbers. "
    "Describe an image only when it carries information the text does not, in one short "
    "italic line. Output only the Markdown."
)


def _pdf_batch_via_gemini(pdf_bytes: bytes) -> str:
    from app.orchestrator.llm import gemini_transcribe_pdf

    text = gemini_transcribe_pdf(pdf_bytes, _PDF_TRANSCRIBE)
    # A fenced reply ("```markdown … ```") is unwrapped.
    fenced = re.fullmatch(r"\s*```(?:markdown|md)?\s*\n(.*?)\n```\s*", text, flags=re.S)
    return fenced.group(1) if fenced else text


def _markdown_from_pdf(raw: bytes) -> str:
    from app.orchestrator.llm import vertex_configured

    reader = _pdf_reader(raw)
    pages = len(reader.pages)
    if pages == 0:
        raise ConvertError("pdf_empty")
    if pages > PDF_MAX_PAGES:
        raise ConvertError("pdf_too_many_pages")
    use_gemini = vertex_configured()
    parts: list[str] = []
    for start in range(0, pages, _PDF_PAGES_PER_CALL):
        stop = min(start + _PDF_PAGES_PER_CALL, pages)
        text = ""
        if use_gemini:
            try:
                text = _pdf_batch_via_gemini(_pdf_slice(reader, start, stop))
            except Exception:
                logger.warning(
                    "community_docs.pdf_gemini_failed pages=%d-%d", start, stop, exc_info=True
                )
        if not text.strip():
            text = _pdf_text(reader, start, stop)
        parts.append(text.strip())
    return _clean("\n\n".join(p for p in parts if p))


def to_markdown(raw: bytes, kind: str) -> str:
    if not raw:
        raise ConvertError("empty_file")
    if kind == KIND_MD:
        md = _markdown_from_md(raw)
    elif kind == KIND_DOCX:
        md = _markdown_from_docx(raw)
    elif kind == KIND_PDF:
        md = _markdown_from_pdf(raw)
    else:
        raise ConvertError("type_not_allowed")
    if not md.strip():
        raise ConvertError("no_text_found")
    return md


# ── sections ────────────────────────────────────────────────────────────────────


@dataclass
class Section:
    ord: int
    heading_path: list[str]
    body: str
    covers: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def token_count(self) -> int:
        return approx_tokens(self.body)

    def as_row(self) -> dict[str, Any]:
        return {
            "ord": self.ord,
            "heading_path": self.heading_path,
            "body": self.body,
            "covers": self.covers,
            "token_count": self.token_count,
        }


_ATX = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _heading_text(raw: str) -> str:
    return re.sub(r"[*_`]+", "", raw).strip()


def _split_long(body: str, limit: int) -> list[str]:
    """Cut at paragraph breaks; a single paragraph longer than the limit is cut at line
    breaks, and only as a last resort mid-line."""
    if len(body) <= limit:
        return [body]
    out: list[str] = []
    cur = ""
    for para in body.split("\n\n"):
        pieces = [para]
        if len(para) > limit:
            pieces = []
            line_buf = ""
            for line in para.split("\n"):
                while len(line) > limit:
                    if line_buf:
                        pieces.append(line_buf)
                        line_buf = ""
                    pieces.append(line[:limit])
                    line = line[limit:]
                if line_buf and len(line_buf) + 1 + len(line) > limit:
                    pieces.append(line_buf)
                    line_buf = line
                else:
                    line_buf = f"{line_buf}\n{line}" if line_buf else line
            if line_buf:
                pieces.append(line_buf)
        for piece in pieces:
            if cur and len(cur) + 2 + len(piece) > limit:
                out.append(cur)
                cur = piece
            else:
                cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        out.append(cur)
    return out


def split_sections(markdown: str, title: str, *, max_chars: int = SECTION_MAX_CHARS) -> list[Section]:
    """One section per heading, with the full heading path (title › chapter › heading).

    Text before the first heading belongs to the document title. A heading with no text
    of its own is not a section — its name lives on in its children's paths. Headings
    inside fenced code are text, not structure.
    """
    stack: list[tuple[int, str]] = []
    blocks: list[tuple[list[str], list[str]]] = [([], [])]
    in_fence = False
    for line in (markdown or "").split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            blocks[-1][1].append(line)
            continue
        m = None if in_fence else _ATX.match(line)
        if m:
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, _heading_text(m.group(2))))
            blocks.append(([h for _, h in stack], []))
        else:
            blocks[-1][1].append(line)

    title = (title or "").strip() or "Document"
    sections: list[Section] = []
    for path, lines in blocks:
        body = "\n".join(lines).strip()
        if not body:
            continue
        full_path = [title, *path]
        parts = _split_long(body, max_chars)
        for i, part in enumerate(parts):
            p = list(full_path)
            if len(parts) > 1:
                p[-1] = f"{p[-1]} ({i + 1}/{len(parts)})"
            sections.append(Section(ord=len(sections), heading_path=p, body=part.strip()))
    return sections
