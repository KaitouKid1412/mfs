"""Base class for per-AMC monthly portfolio holdings adapters (Phase 3.C).

The original Phase 2.2 path extracted holdings from factsheet PDFs, where
ISINs were never printed. Phase 3.C switches to each AMC's own SEBI-mandated
monthly portfolio disclosure Excels — those DO carry ISINs, which unblocks
both Active Share (more reliable normalized-name match) and AUM Impact
Cost (ISIN→bhavcopy join now works).

There is NO centralized AMFI portal for portfolios. Each AMC publishes its
own under SEBI mandate, with per-AMC layout quirks.

Adapter contract per AMC:
  - discover URLs for one data month (one Excel per scheme).
  - download each (cached).
  - parse each Excel into ParsedHoldingRecord rows with ISIN populated.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable

from mfs.schemas import ParsedHoldingRecord


class HoldingsAdapter(ABC):
    """One adapter per AMC. Subclass and register via @register_adapter."""

    amc_slug: str
    source_label: str = ""

    @abstractmethod
    def discover_scheme_urls(self, ym: str) -> dict[str, str]:
        """Return {scheme_name_printed: absolute_excel_url} for data month ym.

        Implementations typically fetch the AMC's disclosures HTML page and
        scrape `.xlsx` links. The printed name is whatever the AMC's site
        shows — fuzzy-matched later to scheme_master.
        """

    @abstractmethod
    def parse_excel(
        self,
        excel_path: Path,
        scheme_name_printed: str,
        ym: str,
    ) -> Iterable[ParsedHoldingRecord]:
        """Yield holdings from one scheme's Excel.

        ISIN MUST be populated. Rows missing ISIN should be skipped at the
        adapter level — letting them through would silently undo the Phase
        3.C value proposition.
        """
