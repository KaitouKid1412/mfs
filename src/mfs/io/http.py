from __future__ import annotations

from pathlib import Path

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from mfs.config import get_settings
from mfs.io.content_check import validate_payload
from mfs.utils.logging import get_logger

log = get_logger(__name__)


class TransientHttpError(Exception):
    pass


def _client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(
        timeout=s.http_timeout,
        headers={"User-Agent": s.user_agent},
        follow_redirects=True,
    )


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=20) + wait_random(0, 2),
    retry=retry_if_exception_type(TransientHttpError),
)
def fetch_bytes(url: str, params: dict | None = None) -> bytes:
    log.info("http.get", url=url, params=params)
    with _client() as c:
        try:
            r = c.get(url, params=params)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            raise TransientHttpError(str(e)) from e
        if r.status_code in (429, 500, 502, 503, 504):
            raise TransientHttpError(f"{r.status_code} for {url}")
        r.raise_for_status()
        return r.content


def download_to(
    url: str,
    out_path: Path,
    params: dict | None = None,
    *,
    expect: str | None = None,
) -> Path:
    """Download ``url`` to ``out_path`` atomically (tmp-file + rename).

    ``expect`` ('pdf' | 'excel' | 'xlsx_zip' | 'xls_ole2') validates the
    payload's magic bytes BEFORE anything is written: a mismatch (e.g. a WAF
    serving an HTML shell as 200 for a PDF path) raises ``IngestError`` and
    leaves no file — a bad payload must never reach the cache.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = fetch_bytes(url, params=params)
    if expect is not None:
        validate_payload(data, expect, url)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.rename(out_path)
    return out_path
