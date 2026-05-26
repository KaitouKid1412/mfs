"""Confirm which schemes printed PTR and which didn't."""
import logging
import pdfplumber

from mfs.ingest.managers.whiteoak_capital import (
    WhiteoakCapitalAdapter, _scheme_name_from_page, _PTR_RE,
)

logging.getLogger("pdfminer").setLevel(logging.ERROR)

PDF = "data/raw/factsheets/whiteoak_capital/2026-04.pdf"

a = WhiteoakCapitalAdapter()
ptr = list(a.parse_ptr(PDF, "2026-04"))
aum = list(a.parse_aum(PDF, "2026-04"))
print(f"PTR rows: {len(ptr)}")
print(f"AUM rows: {len(aum)}")

ptr_names = {r.scheme_name_printed for r in ptr}
aum_names = {r.scheme_name_printed for r in aum}
print("\nin AUM but NOT in PTR:")
for n in sorted(aum_names - ptr_names):
    print("  ", n)

print("\nin PTR but NOT in AUM:")
for n in sorted(ptr_names - aum_names):
    print("  ", n)

print("\nAll PTR scheme names:")
for r in ptr:
    print(f"  {r.scheme_name_printed!r}: {r.ptr}")
