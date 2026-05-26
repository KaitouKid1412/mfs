"""One-shot downloader for Mahindra Manulife April 2026 per-scheme PDFs.

This is a helper script used during adapter calibration. The actual adapter
performs the same downloads via ``MahindraManulifeAdapter.fetch()``.
"""
import sys
from pathlib import Path

import httpx

BASE = "https://www.mahindramanulife.com/digital-factsheet/april-2026/PDF"
PARTS_DIR = Path(
    "/Users/suryavamseeayyagari/mfs/data/raw/factsheets/mahindra_manulife/2026-04_parts"
)
PARTS_DIR.mkdir(parents=True, exist_ok=True)

SCHEMES = [
    ("3_Mahindra-Manulife-ELSS-Tax-Saver-Fund.pdf", "03_ELSS-Tax-Saver.pdf"),
    ("4_Mahindra-Manulife-Multi-Cap-Fund.pdf", "04_Multi-Cap.pdf"),
    ("5_Mahindra-Manulife-Mid-Cap-Fund.pdf", "05_Mid-Cap.pdf"),
    ("6_Mahindra-Manulife-Consumption-Fund.pdf", "06_Consumption.pdf"),
    ("7_Mahindra-Manulife-Large-Cap-Fund.pdf", "07_Large-Cap.pdf"),
    ("8_Mahindra_Manulife_Large_Mid_Cap_Fund.pdf", "08_Large-Mid-Cap.pdf"),
    ("9_Mahindra-Manulife-Focused-Fund.pdf", "09_Focused.pdf"),
    ("10_Mahindra-Manulife-Flexi-Cap-Fund.pdf", "10_Flexi-Cap.pdf"),
    ("11_Mahindra-Manulife-Small-Cap-Fund.pdf", "11_Small-Cap.pdf"),
    ("13_Mahindra-Manulife-Aggressive-Hybrid-Fund.pdf", "13_Aggressive-Hybrid.pdf"),
    ("14_Mahindra-Manulife-Balanced-Advantage-Fund.pdf", "14_Balanced-Advantage.pdf"),
    ("15_Mahindra-Manulife-Arbitrage-Fund.pdf", "15_Arbitrage.pdf"),
    ("16_Mahindra-Manulife-Asia-Pacific-REITs-FOF.pdf", "16_Asia-Pacific-REITs-FOF.pdf"),
    ("17_Mahindra-Manulife-Liquid-Fund.pdf", "17_Liquid.pdf"),
    ("18_Mahindra-Manulife-Equity-Savings-Fund.pdf", "18a_Equity-Savings.pdf"),
    ("18_Mahindra-Manulife-Low-Duration-Fund.pdf", "18b_Low-Duration.pdf"),
    ("19_Mahindra-Manulife-Dynamic-Bond-Fund.pdf", "19_Dynamic-Bond.pdf"),
    ("20_Mahindra-Manulife-Overnight-Fund.pdf", "20_Overnight.pdf"),
    ("21_Mahindra-Manulife-Ultra-Short-Duration-Fund.pdf", "21_Ultra-Short-Duration.pdf"),
    ("22_Mahindra-Manulife-Short-Duration-Fund.pdf", "22_Short-Duration.pdf"),
    ("23_Mahindra-Manulife-Business-Cycle-Fund.pdf", "23_Business-Cycle.pdf"),
    ("24_Mahindra-Manulife-Multi-Asset-Allocation-Fund.pdf", "24_Multi-Asset-Allocation.pdf"),
    ("25_Mahindra-Manulife-Manufacturing-Fund.pdf", "25_Manufacturing.pdf"),
    ("26_Mahindra-Manulife-Value-Fund.pdf", "26_Value.pdf"),
    ("27_Mahindra-Manulife-Banking-Financial-Services-Fund.pdf",
     "27_Banking-Financial-Services.pdf"),
    ("Innovation-Opportunities-Fund.pdf", "28_Innovation-Opportunities.pdf"),
    ("Income-Plus-Arbitrage-Active_FOF.pdf", "29_Income-Plus-Arbitrage-Active-FOF.pdf"),
]


def main() -> int:
    client = httpx.Client(
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        },
        follow_redirects=True,
        timeout=30,
    )
    ok = fail = 0
    for fn, out in SCHEMES:
        url = f"{BASE}/{fn}"
        dst = PARTS_DIR / out
        if dst.exists() and dst.stat().st_size > 10000:
            ok += 1
            continue
        r = client.get(url)
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            print(f"FAIL {r.status_code} {url}")
            fail += 1
            continue
        dst.write_bytes(r.content)
        ok += 1
        print(f"OK {dst.name} ({len(r.content)} bytes)")
    print(f"\n{ok} ok, {fail} fail")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
