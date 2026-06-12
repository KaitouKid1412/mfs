"""One-time tool: extract specific pages from a cached AMC factsheet PDF
into a small fixture PDF committed under tests/fixtures/factsheets/.

Rationale (F-8, docs/phase6/stage2_full_remediation.md): full factsheets
are 2-4MB copyrighted marketing documents; committing ~28 of them creates
80-100MB of repo bloat and redistribution exposure. Instead we commit
1-2 page excerpts that contain one pinned scheme each, so CI exercises
the real parse path (tests/test_managers_fixture_extracts.py) without
shipping whole documents.

The extract is a TEXT-EXTRACTION fixture, not a visually faithful copy:
embedded font programs are stripped and raster images are replaced with
1x1 gray stubs. pdfplumber/pdfminer text + word extraction only needs
the glyph widths and ToUnicode CMaps, which are preserved, so adapter
``parse_ptr`` output is byte-identical to parsing the full factsheet
page. This also shrinks the HDFC page from ~6MB to ~40KB and reduces
the amount of copyrighted material redistributed to plain text excerpts.

Usage:
    uv run python tools/make_fixture_pdf.py \
        --src data/raw/factsheets/hdfc/2026-04.pdf \
        --pages 7 \
        --out tests/fixtures/factsheets/hdfc/2026-04_extract.pdf

Pages are 1-based. Requires the ``pypdf`` dev dependency.
"""

from __future__ import annotations

import argparse
import re
import warnings
import zlib
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    DictionaryObject,
    IndirectObject,
    NameObject,
    NumberObject,
    StreamObject,
)

_DO_RE = re.compile(rb"/(\S+)\s+Do\b")

# 1x1 8-bit gray pixel, Flate-encoded — stub for stripped raster images.
_STUB_IMAGE_DATA = zlib.compress(b"\x00")


def _prune_unused_xobjects(page) -> None:
    """Drop /XObject resources the page content never invokes.

    Some factsheets (HDFC) attach the *entire document's* template
    XObjects to every page's /Resources; a one-page extract would then
    carry ~6MB of dead templates. We scan the content stream for
    ``/Name Do`` invocations (following into invoked Form XObjects that
    lack their own /Resources, which inherit the page's) and keep only
    the names actually used.
    """
    contents = page.get_contents()
    if contents is None:
        return
    resources = page.get("/Resources")
    if resources is None:
        return
    xobjects = resources.get_object().get("/XObject")
    if xobjects is None:
        return
    xobjects = xobjects.get_object()

    used: set[bytes] = set()
    queue = [contents.get_data()]
    while queue:
        data = queue.pop()
        for name in _DO_RE.findall(data):
            if name in used:
                continue
            used.add(name)
            obj = xobjects.get("/" + name.decode("latin-1"))
            if obj is None:
                continue
            obj = obj.get_object()
            # A Form XObject without its own /Resources inherits the
            # page's — scan its content too so we keep what it invokes.
            if obj.get("/Subtype") == "/Form" and "/Resources" not in obj:
                queue.append(obj.get_data())

    for key in [k for k in xobjects if k.lstrip("/").encode("latin-1") not in used]:
        del xobjects[key]


def _walk(obj, seen: set, fn) -> None:
    if isinstance(obj, IndirectObject):
        key = (obj.idnum, obj.generation)
        if key in seen:
            return
        seen.add(key)
        _walk(obj.get_object(), seen, fn)
        return
    fn(obj)
    if isinstance(obj, dict):
        for v in obj.values():
            _walk(v, seen, fn)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, seen, fn)


def _strip_render_only_data(writer: PdfWriter) -> None:
    """Remove data only needed for visual rendering, not text extraction:
    embedded font programs (glyph widths + ToUnicode stay) and raster
    image payloads (replaced by a 1x1 gray stub)."""

    def strip(obj) -> None:
        if not isinstance(obj, DictionaryObject):
            return
        for k in ("/FontFile", "/FontFile2", "/FontFile3"):
            if k in obj:
                del obj[k]
        if obj.get("/Subtype") == "/Image" and isinstance(obj, StreamObject):
            obj._data = _STUB_IMAGE_DATA
            obj[NameObject("/Filter")] = NameObject("/FlateDecode")
            obj[NameObject("/Width")] = NumberObject(1)
            obj[NameObject("/Height")] = NumberObject(1)
            obj[NameObject("/BitsPerComponent")] = NumberObject(8)
            obj[NameObject("/ColorSpace")] = NameObject("/DeviceGray")
            for k in ("/DecodeParms", "/SMask", "/Decode", "/Mask", "/Intent", "/Length1"):
                if k in obj:
                    del obj[k]

    _walk(writer._root_object, set(), strip)


def _compress(writer: PdfWriter) -> None:
    with warnings.catch_warnings():
        # pypdf 6 renamed the kwargs; pinning the old names keeps >=5 working.
        warnings.simplefilter("ignore", DeprecationWarning)
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    for page in writer.pages:
        page.compress_content_streams()


def extract_pages(src: Path, pages: list[int], out: Path) -> None:
    # Pass 1: pull the requested pages and prune /XObject entries the
    # page content never invokes.
    reader = PdfReader(src)
    n = len(reader.pages)
    writer = PdfWriter()
    for p in pages:
        if not 1 <= p <= n:
            raise SystemExit(f"page {p} out of range 1..{n} for {src}")
        writer.add_page(reader.pages[p - 1])
    for page in writer.pages:
        _prune_unused_xobjects(page)
    _compress(writer)
    buf = BytesIO()
    writer.write(buf)

    # Pass 2: re-read the extract (so only objects reachable from the
    # kept pages exist), strip render-only data, and drop the orphans
    # that stripping created.
    buf.seek(0)
    writer2 = PdfWriter()
    writer2.append(PdfReader(buf))
    _strip_render_only_data(writer2)
    _compress(writer2)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as fh:
        writer2.write(fh)
    print(f"wrote {out} ({out.stat().st_size:,} bytes, pages {pages} of {src})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, required=True, help="source factsheet PDF")
    ap.add_argument(
        "--pages",
        type=int,
        nargs="+",
        required=True,
        help="1-based page numbers to extract, in order",
    )
    ap.add_argument("--out", type=Path, required=True, help="output fixture PDF path")
    args = ap.parse_args()
    extract_pages(args.src, args.pages, args.out)


if __name__ == "__main__":
    main()
