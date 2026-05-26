"""Parse ITI's main.js bundle to extract per-endpoint postData call shapes."""
from __future__ import annotations

import re
import sys
from pathlib import Path

JS = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/iti_main.js").read_text()

# Find every F.f.XXXX usage with its payload
for m in re.finditer(r"\.postData\(F\.f\.([a-zA-Z_]+)\s*,\s*([^)]*)\)", JS):
    name, payload = m.group(1), m.group(2)
    print(f"{name:32s} -> {payload[:160]}")
