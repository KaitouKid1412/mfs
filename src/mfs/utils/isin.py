from __future__ import annotations

import re

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


def is_valid_isin(s: str | None) -> bool:
    if not s:
        return False
    return bool(_ISIN_RE.match(s.strip()))


def normalize_isin(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip().upper()
    return s if is_valid_isin(s) else None
