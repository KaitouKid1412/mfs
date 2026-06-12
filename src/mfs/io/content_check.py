"""Pre-cache payload content validation (Phase 6, B1).

Every download path that caches a binary artifact (factsheet PDF, monthly
portfolio Excel) must validate the payload's magic bytes BEFORE the bytes
ever touch the cache. The motivating failure is a WAF/SPA host answering a
PDF request with HTTP 200 + a tiny HTML React shell (the trustmf.com case:
487 bytes of ``<!doctype html>...<div id="root">`` served for every
``*.pdf`` path) — without this check the shell would be cached under a
``.pdf`` name and silently re-used forever.

``sniff_kind`` classifies a payload by magic bytes; ``validate_payload``
raises ``IngestError`` on mismatch so the fetch counts as a failure and
nothing is written. Stage-1 statement-date validation (post-parse) remains
the deeper net; this is the cheap pre-parse one.
"""

from __future__ import annotations

from mfs.errors import IngestError

# Magic-byte prefixes.
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"          # xlsx (and any OOXML/zip container)
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0"   # legacy .xls (OLE2 compound file)
_HTML_PREFIXES = (b"<!doctype", b"<html")

# expected-label -> set of acceptable sniffed kinds. 'excel' accepts both the
# modern zip container and the legacy OLE2 container because AMCs publish a
# mix of .xlsx and true .xls files.
_ACCEPTED: dict[str, frozenset[str]] = {
    "pdf": frozenset({"pdf"}),
    "excel": frozenset({"xlsx_zip", "xls_ole2"}),
    "xlsx_zip": frozenset({"xlsx_zip"}),
    "xls_ole2": frozenset({"xls_ole2"}),
}


def sniff_kind(data: bytes) -> str:
    """Classify a payload by magic bytes.

    Returns one of: 'pdf', 'xlsx_zip', 'xls_ole2', 'html', 'csv_text',
    'unknown'. 'html' detection is deliberately simple (lstripped, lowercased
    prefix) — it only needs to catch WAF/SPA shells served as 200, not parse
    arbitrary markup.
    """
    if not data:
        return "unknown"
    if data.startswith(_PDF_MAGIC):
        return "pdf"
    if data.startswith(_ZIP_MAGIC):
        return "xlsx_zip"
    if data.startswith(_OLE2_MAGIC):
        return "xls_ole2"
    head = data[:256].lstrip().lower()
    if head.startswith(_HTML_PREFIXES):
        return "html"
    try:
        data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return "unknown"
    return "csv_text"


def validate_payload(data: bytes, expected: str, url: str) -> None:
    """Raise ``IngestError`` unless ``data``'s sniffed kind satisfies
    ``expected`` ('pdf', 'excel', 'xlsx_zip', 'xls_ole2').

    An 'html' payload is ALWAYS rejected for binary expectations — a 200
    response carrying an HTML shell is a blocked/walled fetch, not a
    document. Callers must treat the raise as a fetch failure: never write
    the payload to the cache.
    """
    accepted = _ACCEPTED.get(expected)
    if accepted is None:
        raise ValueError(
            f"validate_payload: unknown expectation {expected!r} "
            f"(known: {sorted(_ACCEPTED)})"
        )
    kind = sniff_kind(data)
    if kind in accepted:
        return
    detail = (
        "host served an HTML page (WAF/SPA shell) instead of the document"
        if kind == "html"
        else f"payload sniffed as {kind!r}"
    )
    raise IngestError(
        f"content validation failed for {url}: expected {expected!r} but "
        f"{detail} ({len(data)} bytes). Refusing to cache it."
    )
