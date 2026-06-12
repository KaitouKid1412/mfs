import os
import sys
from pathlib import Path

import pytest

# Ensure src/ is importable
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("MFS_DATA_DIR", str(ROOT / "data"))

# ---------------------------------------------------------------------------
# local_data marker (F-8)
#
# Adapter value-pinning tests are calibrated against cached factsheets /
# Excels under data/raw, which is gitignored. The hook below auto-applies
# the ``local_data`` marker to every test in any module that declares a
# module-level Path constant resolving under <repo>/data/raw — so CI (and
# any fresh clone) can deselect them wholesale with ``-m 'not local_data'``
# while keeping the per-test ``pytest.skip`` guards as belt-and-braces.
#
# Detection is structural (module globals), NOT existence-based: the marker
# applies whether or not data/raw is present, so the deselected count is
# stable and can be pinned as a CI tripwire (see .github/workflows/ci.yml).
# ---------------------------------------------------------------------------

_DATA_RAW = (ROOT / "data" / "raw").resolve()


def _module_uses_local_data(module) -> bool:
    for value in vars(module).values():
        if isinstance(value, Path):
            try:
                resolved = Path(value).resolve()
            except OSError:  # pragma: no cover - defensive
                continue
            if resolved.is_relative_to(_DATA_RAW):
                return True
    return False


def pytest_collection_modifyitems(config, items):
    cache: dict[str, bool] = {}
    for item in items:
        module = getattr(item, "module", None)
        if module is None:
            continue
        name = module.__name__
        if name not in cache:
            cache[name] = _module_uses_local_data(module)
        if cache[name]:
            item.add_marker(pytest.mark.local_data)


# ---------------------------------------------------------------------------
# C7: mfs.compute.alignment lru-caches the invariant series (calendar /
# benchmark / risk-free) for the life of the process. Tests monkeypatch the
# underlying mfs.db.queries functions, so a cached frame from one test would
# leak into the next. Clear before every test; cheap no-op when the module
# was never imported.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_alignment_caches():
    mod = sys.modules.get("mfs.compute.alignment")
    if mod is not None:
        mod.clear_caches()
    yield
