"""Adapter base class for AMC factsheet scrapers.

Each AMC's monthly factsheet PDF carries three things we extract: portfolio
holdings (factsheet path — no ISIN; Phase 3.C provides a parallel
Excel-based path WITH ISIN), Portfolio Turnover Ratio (one number per
scheme), and AUM (one number per scheme). Subclass + register one adapter
per AMC.

Package name retained as ``mfs.ingest.managers`` for backward compatibility
with existing imports — the adapter no longer extracts manager-tenure data
(removed when the user opted to verify manager tenure manually for the
Stage 2 survivor set).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable

from mfs.schemas import ParsedAumRecord, ParsedHoldingRecord, ParsedPtrRecord


class ManagerAdapter(ABC):
    """One adapter per AMC. Subclass and register via @register_adapter.

    Required:
      - amc_slug:    short id (orchestrator fuzzy-matches scheme names per AMC).
      - source_label: human-readable AMC name for logs.
      - build_url(ym): return the PDF URL for data-month ym (YYYY-MM).

    Optional override methods:
      - parse_holdings(pdf_path, ym): yield ParsedHoldingRecord rows.
      - parse_ptr(pdf_path, ym):      yield ParsedPtrRecord rows.
      - parse_aum(pdf_path, ym):      yield ParsedAumRecord rows.

    All three parse methods receive the same local PDF path (the file already
    downloaded by ``fetch()``), so opening the PDF up to three times is the
    expected behavior. pdfplumber's caching of the PDF text layer makes this
    only marginally more expensive than a single open.
    """

    amc_slug: str
    source_label: str = ""

    @abstractmethod
    def build_url(self, ym: str) -> str:
        """Return the absolute PDF URL for data month ym='YYYY-MM'."""

    def fetch(self, ym: str) -> Path:
        """Download the factsheet for data month ym; return local Path.

        Override when the AMC needs referer chains, form posts, etc.
        Re-uses an existing cached PDF unconditionally — delete the file to
        force a re-fetch.
        """
        from mfs import paths
        from mfs.io.http import download_to

        out = paths.factsheet_raw(self.amc_slug, ym)
        if out.exists():
            return out
        url = self.build_url(ym)
        return download_to(url, out)

    def parse_holdings(self, pdf_path: Path, ym: str) -> Iterable[ParsedHoldingRecord]:
        """Extract one (scheme, isin, weight) row per portfolio line. Default
        yields nothing — adapters that support holdings override this."""
        return ()

    def parse_ptr(self, pdf_path: Path, ym: str) -> Iterable[ParsedPtrRecord]:
        """Extract one (scheme, ptr) per scheme. Default yields nothing."""
        return ()

    def parse_aum(self, pdf_path: Path, ym: str) -> Iterable[ParsedAumRecord]:
        """Extract one (scheme, AUM in Crore) per scheme. Default yields nothing."""
        return ()
