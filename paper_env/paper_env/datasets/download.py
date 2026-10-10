"""Resumable HTTP download: Range requests, If-Range validation, retry with backoff."""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import httpx

from .checksums import sha256_file

ProgressCb = Callable[[int, int | None], None]


class DownloadError(Exception):
    """Non-retryable failure (auth, 404, ...) or retries exhausted."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class InsufficientDisk(DownloadError):
    pass


@dataclass
class Downloaded:
    path: Path
    size: int
    sha256: str
    etag: str | None
    attempts: int


def _total_from(resp: httpx.Response, start: int) -> int | None:
    cr = resp.headers.get("content-range")
    if cr:
        m = re.match(r"bytes\s+\d+-\d+/(\d+|\*)", cr) or re.match(r"bytes\s+\*/(\d+)", cr)
        if m and m.group(1) != "*":
            return int(m.group(1))
    cl = resp.headers.get("content-length")
    if cl is not None and resp.headers.get("content-encoding") in (None, "identity"):
        return start + int(cl) if resp.status_code == 206 else int(cl)
    return None


def download_file(client: httpx.Client, url: str, dest: Path, *, headers: Mapping[str, str] | None = None,
                  expected_size: int | None = None, progress: ProgressCb | None = None,
                  retries: int = 5, backoff: float = 0.5, min_free_bytes: int = 0,
                  sleep: Callable[[float], None] = time.sleep) -> Downloaded:
    """Download ``url`` to ``dest`` (atomic rename on completion). Interrupted downloads
    resume from ``dest.part``; a changed remote (ETag) restarts cleanly."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    meta = dest.with_name(dest.name + ".part.json")
    headers = dict(headers or {})
    last_err: Exception | None = None
    for attempt in range(1, retries + 2):
        start = part.stat().st_size if part.exists() else 0
        etag = json.loads(meta.read_text()).get("etag") if meta.exists() else None
        req_headers = {**headers, "Accept-Encoding": "identity"}  # byte ranges must match stored bytes
        if start > 0:
            req_headers["Range"] = f"bytes={start}-"
            if etag:
                req_headers["If-Range"] = etag
        try:
            with client.stream("GET", url, headers=req_headers) as resp:
                if resp.status_code == 416:  # nothing left to send, or our offset is invalid
                    total = _total_from(resp, start)
                    if total is not None and total == start:
                        break
                    part.unlink(missing_ok=True)
                    meta.unlink(missing_ok=True)
                    continue
                if resp.status_code in (401, 403, 404, 410) or (400 <= resp.status_code < 500 and resp.status_code != 429):
                    raise DownloadError(f"HTTP {resp.status_code} for {_safe(url)}", resp.status_code)
                if resp.status_code >= 400:
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                resumed = resp.status_code == 206 and start > 0
                if not resumed:
                    start = 0  # server ignored Range or content changed: start over
                total = _total_from(resp, start) or expected_size
                if total is not None:
                    free = shutil.disk_usage(dest.parent).free
                    if free < (total - start) + min_free_bytes:
                        raise InsufficientDisk(f"need {total - start} more bytes (+{min_free_bytes} reserve), "
                                               f"only {free} free")
                new_etag = resp.headers.get("etag")
                meta.write_text(json.dumps({"etag": new_etag}))
                done = start
                with open(part, "ab" if resumed else "wb") as f:
                    for chunk in resp.iter_raw():
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                if total is not None and done != total:
                    raise httpx.ReadError(f"short body: got {done} of {total}")
                break
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last_err = e
            if attempt > retries:
                raise DownloadError(f"giving up on {_safe(url)} after {attempt} attempts: {type(e).__name__}") from e
            sleep(backoff * (2 ** (attempt - 1)))
            continue
    else:
        raise DownloadError(f"could not download {_safe(url)}: {last_err}")
    size = part.stat().st_size
    etag = json.loads(meta.read_text()).get("etag") if meta.exists() else None
    digest = sha256_file(part)
    part.replace(dest)
    meta.unlink(missing_ok=True)
    return Downloaded(dest, size, digest, etag, attempt)


def _safe(url: str) -> str:
    """URL for messages: drop query string and userinfo (may carry credentials)."""
    u = httpx.URL(url)
    return str(u.copy_with(query=None, userinfo=b"")) if u.query or u.userinfo else str(u)
