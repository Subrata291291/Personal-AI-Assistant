"""Temporary, in-memory text extraction for user-selected Drive documents.

Nothing in this module writes extracted text to disk or to the database.
The caller must keep the returned text only for the duration of one request.
"""

import io
import re
import zipfile
from dataclasses import dataclass
from typing import Literal
from xml.etree import ElementTree

from pypdf import PdfReader

ExtractionKind = Literal["pdf", "docx", "google_doc"]

PDF_MIME_TYPE = "application/pdf"
DOCX_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"

SUPPORTED_CONTRACT_MIME_TYPES = frozenset({
    PDF_MIME_TYPE,
    DOCX_MIME_TYPE,
    GOOGLE_DOC_MIME_TYPE,
})

MAX_PDF_PAGES = 500
MAX_DOCX_ENTRIES = 5000
MAX_DOCX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_WORD_NS}}}"


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    extraction: ExtractionKind
    page_count: int | None
    truncated: bool


def _normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\x00", "")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def extract_pdf_text(
    data: bytes,
    *,
    max_chars: int,
) -> ExtractedDocument:
    if not data:
        raise ValueError("The selected PDF file is empty.")

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise ValueError(
                    "The selected PDF is encrypted and cannot be read."
                ) from exc
            if reader.is_encrypted:
                raise ValueError(
                    "The selected PDF is encrypted and cannot be read."
                )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(
            "The selected file could not be parsed as a PDF."
        ) from exc

    page_count = len(reader.pages)
    if page_count > MAX_PDF_PAGES:
        raise ValueError(
            f"The selected PDF has more than {MAX_PDF_PAGES} pages."
        )

    sections: list[str] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        page_text = _normalize_text(page_text)
        if page_text:
            sections.append(f"--- Page {page_number} ---\n{page_text}")

    text = "\n\n".join(sections)
    text, truncated = _truncate(text, max_chars)
    return ExtractedDocument(
        text=text,
        extraction="pdf",
        page_count=page_count,
        truncated=truncated,
    )


def extract_docx_text(
    data: bytes,
    *,
    max_chars: int,
) -> ExtractedDocument:
    if not data:
        raise ValueError("The selected DOCX file is empty.")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DOCX_ENTRIES:
                raise ValueError("The selected DOCX file is too complex.")
            if sum(entry.file_size for entry in entries) > (
                MAX_DOCX_UNCOMPRESSED_BYTES
            ):
                raise ValueError("The selected DOCX file is too large.")
            if "word/document.xml" not in archive.namelist():
                raise ValueError(
                    "The selected file is not a valid DOCX document."
                )
            document_xml = archive.read("word/document.xml")
    except zipfile.BadZipFile as exc:
        raise ValueError(
            "The selected file could not be parsed as a DOCX document."
        ) from exc

    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError as exc:
        raise ValueError(
            "The selected DOCX document content could not be read."
        ) from exc

    paragraphs: list[str] = []
    for paragraph in root.iter(f"{_W}p"):
        text_parts = [
            node.text or ""
            for node in paragraph.iter(f"{_W}t")
        ]
        paragraph_text = _normalize_text("".join(text_parts))
        if not paragraph_text:
            continue
        style = paragraph.find(f"{_W}pPr/{_W}pStyle")
        style_name = style.get(f"{_W}val", "") if style is not None else ""
        if style_name.lower().startswith("heading"):
            paragraph_text = f"## {paragraph_text}"
        paragraphs.append(paragraph_text)

    text = "\n\n".join(paragraphs)
    text, truncated = _truncate(text, max_chars)
    return ExtractedDocument(
        text=text,
        extraction="docx",
        page_count=None,
        truncated=truncated,
    )


def extract_google_doc_text(
    data: bytes,
    *,
    max_chars: int,
) -> ExtractedDocument:
    text = _normalize_text(data.decode("utf-8", errors="replace"))
    text, truncated = _truncate(text, max_chars)
    return ExtractedDocument(
        text=text,
        extraction="google_doc",
        page_count=None,
        truncated=truncated,
    )
