from __future__ import annotations

import hashlib
import re

from atlas.domain.enums import HermesDocumentContentType


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def detect_content_type(
    content_type_header: str | None,
    url: str | None = None,
) -> HermesDocumentContentType:
    ct = (content_type_header or "").lower()
    u = (url or "").lower().split("?")[0]

    if "text/html" in ct or u.endswith(".html") or u.endswith(".htm"):
        return HermesDocumentContentType.HTML
    if "application/pdf" in ct or u.endswith(".pdf"):
        return HermesDocumentContentType.PDF
    if ct.startswith("text/plain") or u.endswith(".txt"):
        return HermesDocumentContentType.TEXT
    if "application/json" in ct or u.endswith(".json"):
        return HermesDocumentContentType.JSON
    if "xml" in ct or u.endswith(".xml"):
        return HermesDocumentContentType.XML
    return HermesDocumentContentType.UNKNOWN


def _looks_binary(content: bytes) -> bool:
    """Heuristic: null bytes or high density of non-printable control chars → binary."""
    if not content:
        return False
    sample = content[:1024]
    if b"\x00" in sample:
        return True
    control_count = sum(1 for b in sample if b < 32 and b not in (9, 10, 13))
    return control_count / max(len(sample), 1) > 0.30


def detect_content_type_from_bytes(
    content: bytes,
    content_type_header: str | None,
    url: str | None = None,
) -> HermesDocumentContentType:
    result = detect_content_type(content_type_header, url)
    if result != HermesDocumentContentType.UNKNOWN:
        return result
    if _looks_binary(content):
        return HermesDocumentContentType.BINARY
    try:
        content[:1024].decode("utf-8")
    except UnicodeDecodeError:
        return HermesDocumentContentType.BINARY
    return HermesDocumentContentType.UNKNOWN


def make_text_preview(
    content: bytes,
    content_type: HermesDocumentContentType,
    max_chars: int = 2000,
) -> str | None:
    if content_type not in (
        HermesDocumentContentType.HTML,
        HermesDocumentContentType.TEXT,
        HermesDocumentContentType.JSON,
        HermesDocumentContentType.XML,
    ):
        return None
    text = content.decode("utf-8", errors="replace")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars] if text else None


def extract_html_title(content: bytes) -> str | None:
    text = content.decode("utf-8", errors="replace")
    m = re.search(r"<title[^>]*>([^<]{1,512})</title>", text, re.IGNORECASE)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip() or None
    return None
