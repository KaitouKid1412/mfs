"""Adapter base class for AMC factsheet scrapers.

Each AMC's monthly factsheet PDF carries two things we extract: portfolio
holdings (factsheet path — no ISIN; Phase 3.C provides a parallel
Excel-based path WITH ISIN) and Portfolio Turnover Ratio (one number per
scheme). Subclass + register one adapter per AMC.

AUM extraction is intentionally not part of this interface — AMFI's quarterly
AAUM endpoint (``mfs.ingest.amfi_aum``) is the sole AUM source.

Package name retained as ``mfs.ingest.managers`` for backward compatibility
with existing imports — the adapter no longer extracts manager-tenure data
(removed when the user opted to verify manager tenure manually for the
Stage 2 survivor set).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable

from mfs.schemas import ParsedHoldingRecord, ParsedPtrRecord


class ManagerAdapter(ABC):
    """One adapter per AMC. Subclass and register via @register_adapter.

    Required:
      - amc_slug:    short id (orchestrator fuzzy-matches scheme names per AMC).
      - source_label: human-readable AMC name for logs.
      - build_url(ym): return the PDF URL for data-month ym (YYYY-MM).

    Optional override methods:
      - parse_holdings(pdf_path, ym): yield ParsedHoldingRecord rows.
      - parse_ptr(pdf_path, ym):      yield ParsedPtrRecord rows.

    Both parse methods receive the same local PDF path (the file already
    downloaded by ``fetch()``), so opening the PDF twice is the expected
    behavior. pdfplumber's caching of the PDF text layer makes this only
    marginally more expensive than a single open.
    """

    amc_slug: str
    source_label: str = ""

    @abstractmethod
    def build_url(self, ym: str) -> str:
        """Return the absolute PDF URL for data month ym='YYYY-MM'."""

    def fetch(self, ym: str) -> Path:
        """Download the factsheet for data month ym; return local Path.

        Override when the AMC needs referer chains, form posts, etc.
        Re-uses an existing cached PDF by mere existence; a forced run
        (``--full`` pipeline / ``--force`` CLI) deletes the data month's
        cached file in ``managers._run.run_for_amc`` BEFORE calling this,
        so force re-downloads (B9) — overrides must keep reading/writing
        the canonical ``paths.factsheet_raw`` location for that to hold.
        The payload's magic bytes are validated before caching
        (``expect='pdf'``): a WAF/SPA HTML shell served as 200 raises
        ``IngestError`` and is never written.
        """
        from mfs import paths
        from mfs.io.http import download_to

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out
        url = self.build_url(ym)
        return download_to(url, out, expect="pdf")

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        """Extract one (scheme, isin, weight) row per portfolio line. Default
        yields nothing — adapters that support holdings override this."""
        return ()

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        """Extract one (scheme, ptr) per scheme. Default yields nothing."""
        return ()

