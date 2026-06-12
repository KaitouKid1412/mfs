"""B1: magic-bytes content validation before any download reaches the cache.

The motivating case is the trustmf.com WAF answering every PDF request with
HTTP 200 + a 487-byte React SPA HTML shell — without validation that shell
would be cached under a .pdf name and re-used forever. ``download_to``
validates BEFORE writing the tmp file, so a bad payload leaves no file.
"""

from __future__ import annotations

import pytest

from mfs.errors import IngestError
from mfs.io import http as io_http
from mfs.io.content_check import sniff_kind, validate_payload

# Shaped like the trustmf.com WAF response: a tiny React SPA shell (~487 B).
HTML_SHELL = (
    b'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="UTF-8" />\n'
    b'<link rel="icon" type="image/svg+xml" href="/favicon.ico" />\n'
    b'<meta name="viewport" content="width=device-width, initial-scale=1.0"/>'
    b"\n<title>TRUST Mutual Fund</title>\n"
    b'<script type="module" crossorigin src="/assets/index-9f1c2.js"></script>'
    b'\n<link rel="stylesheet" href="/assets/index-9f1c2.css">\n</head>\n'
    b'<body>\n<div id="root"></div>\n</body>\n</html>\n'
)

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
XLSX_BYTES = b"PK\x03\x04" + b"\x14\x00\x00\x00" + b"\x00" * 64
OLE2_BYTES = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64


# ---------------------------------------------------------------------------
# sniff_kind
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "data, kind",
    [
        (PDF_BYTES, "pdf"),
        (XLSX_BYTES, "xlsx_zip"),
        (OLE2_BYTES, "xls_ole2"),
        (HTML_SHELL, "html"),
        (b"  \n\t<!DOCTYPE HTML><html>", "html"),
        (b"<HTML><body>err</body></HTML>", "html"),
        (b"isin,weight\nINE040A01034,9.2\n", "csv_text"),
        (b'{"status":"Failure","statusCode":"400"}', "csv_text"),
        (b"", "unknown"),
        (b"\xff\xfe\x00\x9c binary garbage", "unknown"),
    ],
)
def test_sniff_kind(data, kind):
    assert sniff_kind(data) == kind


# ---------------------------------------------------------------------------
# validate_payload
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "data, expected",
    [
        (PDF_BYTES, "pdf"),
        (XLSX_BYTES, "excel"),
        (OLE2_BYTES, "excel"),
        (XLSX_BYTES, "xlsx_zip"),
        (OLE2_BYTES, "xls_ole2"),
    ],
)
def test_validate_payload_accepts(data, expected):
    validate_payload(data, expected, "https://example.test/x")  # no raise


@pytest.mark.parametrize(
    "data, expected",
    [
        (HTML_SHELL, "pdf"),     # the trustmf.com WAF case
        (HTML_SHELL, "excel"),
        (PDF_BYTES, "excel"),
        (XLSX_BYTES, "pdf"),
        (b"", "pdf"),
        (b"some,csv,text", "excel"),
    ],
)
def test_validate_payload_rejects(data, expected):
    with pytest.raises(IngestError):
        validate_payload(data, expected, "https://example.test/x")


def test_validate_payload_unknown_expectation_is_a_bug():
    with pytest.raises(ValueError):
        validate_payload(PDF_BYTES, "docx", "https://example.test/x")


def test_validate_payload_html_message_names_the_waf_shape():
    with pytest.raises(IngestError, match="HTML page"):
        validate_payload(HTML_SHELL, "pdf", "https://walled.test/f.pdf")


# ---------------------------------------------------------------------------
# download_to wiring: validate BEFORE anything touches disk
# ---------------------------------------------------------------------------

def test_download_to_html_shell_for_pdf_leaves_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(io_http, "fetch_bytes", lambda url, params=None: HTML_SHELL)
    out = tmp_path / "factsheets" / "trust" / "2026-04.pdf"
    with pytest.raises(IngestError):
        io_http.download_to("https://walled.test/f.pdf", out, expect="pdf")
    assert not out.exists()
    assert not out.with_suffix(out.suffix + ".tmp").exists()
    # The parent dir may have been created, but it must hold NO artifact.
    assert not any(out.parent.iterdir())


def test_download_to_pdf_payload_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(io_http, "fetch_bytes", lambda url, params=None: PDF_BYTES)
    out = tmp_path / "f.pdf"
    p = io_http.download_to("https://ok.test/f.pdf", out, expect="pdf")
    assert p == out and out.read_bytes() == PDF_BYTES


@pytest.mark.parametrize("payload", [XLSX_BYTES, OLE2_BYTES])
def test_download_to_excel_accepts_both_containers(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(io_http, "fetch_bytes", lambda url, params=None: payload)
    out = tmp_path / "f.xlsx"
    io_http.download_to("https://ok.test/f.xlsx", out, expect="excel")
    assert out.read_bytes() == payload


def test_download_to_without_expect_is_unchanged(tmp_path, monkeypatch):
    # Non-artifact paths (HTML listing pages etc.) keep working unvalidated.
    monkeypatch.setattr(io_http, "fetch_bytes", lambda url, params=None: HTML_SHELL)
    out = tmp_path / "page.html"
    io_http.download_to("https://ok.test/page", out)
    assert out.read_bytes() == HTML_SHELL


# ---------------------------------------------------------------------------
# Adapter fetch paths are wired with the right expectations
# ---------------------------------------------------------------------------

def test_manager_adapter_fetch_rejects_html_shell(tmp_path, monkeypatch):
    from mfs import paths
    from mfs.ingest.managers._base import ManagerAdapter

    class FakeAdapter(ManagerAdapter):
        amc_slug = "fake_amc_b1"

        def build_url(self, ym: str) -> str:
            return "https://walled.test/factsheet.pdf"

    out = tmp_path / "factsheets" / "fake_amc_b1" / "2026-04.pdf"
    monkeypatch.setattr(paths, "factsheet_raw", lambda slug, ym: out)
    monkeypatch.setattr(io_http, "fetch_bytes", lambda url, params=None: HTML_SHELL)
    with pytest.raises(IngestError):
        FakeAdapter().fetch("2026-04")
    assert not out.exists()


def test_manager_adapter_fetch_caches_real_pdf(tmp_path, monkeypatch):
    from mfs import paths
    from mfs.ingest.managers._base import ManagerAdapter

    class FakeAdapter(ManagerAdapter):
        amc_slug = "fake_amc_b1"

        def build_url(self, ym: str) -> str:
            return "https://ok.test/factsheet.pdf"

    out = tmp_path / "factsheets" / "fake_amc_b1" / "2026-04.pdf"
    monkeypatch.setattr(paths, "factsheet_raw", lambda slug, ym: out)
    monkeypatch.setattr(io_http, "fetch_bytes", lambda url, params=None: PDF_BYTES)
    assert FakeAdapter().fetch("2026-04") == out
    assert out.read_bytes() == PDF_BYTES


def test_generic_holdings_fetch_excel_rejects_html_shell(tmp_path, monkeypatch):
    from mfs.ingest.holdings import _generic

    class FakeHoldings(_generic.GenericHoldingsAdapter):
        amc_slug = "fake_holdings_b1"

        def discover_scheme_urls(self, ym: str) -> dict[str, str]:
            return {}

    out = tmp_path / "holdings" / "fake_holdings_b1" / "2026-04" / "X.xlsx"
    monkeypatch.setattr(
        _generic.paths, "holdings_excel_raw", lambda slug, ym, fn: out,
    )
    monkeypatch.setattr(io_http, "fetch_bytes", lambda url, params=None: HTML_SHELL)
    with pytest.raises(IngestError):
        FakeHoldings().fetch_excel("https://walled.test/X.xlsx", "X.xlsx", "2026-04")
    assert not out.exists()


def test_generic_holdings_fetch_excel_accepts_ole2_xls(tmp_path, monkeypatch):
    from mfs.ingest.holdings import _generic

    class FakeHoldings(_generic.GenericHoldingsAdapter):
        amc_slug = "fake_holdings_b1"

        def discover_scheme_urls(self, ym: str) -> dict[str, str]:
            return {}

    out = tmp_path / "holdings" / "fake_holdings_b1" / "2026-04" / "X.xls"
    monkeypatch.setattr(
        _generic.paths, "holdings_excel_raw", lambda slug, ym, fn: out,
    )
    monkeypatch.setattr(io_http, "fetch_bytes", lambda url, params=None: OLE2_BYTES)
    p = FakeHoldings().fetch_excel("https://ok.test/X.xls", "X.xls", "2026-04")
    assert p.read_bytes() == OLE2_BYTES
