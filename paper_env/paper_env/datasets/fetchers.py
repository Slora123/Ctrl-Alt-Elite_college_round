"""Fetchers turn a manifest into files on disk. Credentials are supplied only at fetch time
via the SecretsProvider and are used for request headers only: never stored or returned."""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import quote, unquote, urlparse

import httpx

from ..domain import AccessType, DatasetFile, DatasetManifest, SourceType
from ..security import SecretsProvider
from .checksums import scan_dir
from .download import DownloadError, InsufficientDisk, download_file

ProgressFn = Callable[[str, int, int | None], None]  # (file path, bytes done, bytes total)


class FetchError(Exception):
    """Fetch failed; message is safe to show (no credentials)."""


class AuthRequired(FetchError):
    pass


class NotImplementedFetcher(FetchError):
    pass


@dataclass
class FetchContext:
    dest: Path
    secrets: SecretsProvider
    progress: ProgressFn
    min_free_bytes: int = 0
    client: httpx.Client | None = None
    retries: int = 5
    backoff: float = 0.5


@dataclass
class RemoteFile:
    path: str
    url: str | None
    size: int | None = None
    sha256: str | None = None  # known remote checksum, if the source publishes one


@dataclass
class Plan:
    files: list[RemoteFile]
    fingerprint: str | None  # ETag / commit sha of what we are about to fetch


@dataclass
class FetchOutcome:
    files: list[DatasetFile]
    fingerprint: str | None


class Fetcher(Protocol):
    def plan(self, manifest: DatasetManifest, ctx: FetchContext) -> Plan: ...
    def fetch(self, manifest: DatasetManifest, ctx: FetchContext, plan: Plan) -> FetchOutcome: ...
    def remote_fingerprint(self, manifest: DatasetManifest, ctx: FetchContext) -> str | None: ...


def _client(ctx: FetchContext) -> httpx.Client:
    return ctx.client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, connect=10.0))


def _bearer(manifest: DatasetManifest, secrets: SecretsProvider, key: str) -> dict[str, str]:
    """Header with a credential, only for gated datasets. Missing credentials -> explicit error."""
    if manifest.access_type != AccessType.GATED:
        return {}
    tok = secrets.get(key) or secrets.get(f"dataset:{manifest.dataset_id}")
    if not tok:
        raise AuthRequired(f"dataset {manifest.dataset_id} is gated and no credential '{key}' is configured")
    return {"Authorization": f"Bearer {tok}"}


def _download_all(client, plan: Plan, ctx: FetchContext, headers, base_url: str | None = None) -> list[DatasetFile]:
    out = []
    for rf in plan.files:
        assert rf.url
        dest = ctx.dest / rf.path
        try:
            d = download_file(client, rf.url, dest, headers=headers, expected_size=rf.size,
                              progress=lambda done, tot, p=rf.path: ctx.progress(p, done, tot),
                              retries=ctx.retries, backoff=ctx.backoff, min_free_bytes=ctx.min_free_bytes)
        except InsufficientDisk:
            raise
        except DownloadError as e:
            if e.status in (401, 403):
                raise AuthRequired(f"{e} (credentials missing or insufficient)") from e
            raise FetchError(str(e)) from e
        out.append(DatasetFile(path=rf.path, size=d.size, sha256=d.sha256))
    return out


class UrlFetcher:
    """source_uri is a file URL, or a base URL ending in '/' combined with manifest.files paths."""

    def _files(self, manifest: DatasetManifest) -> list[RemoteFile]:
        uri = manifest.source_uri
        if uri.endswith("/"):
            if not manifest.files:
                raise FetchError("base-URL dataset needs manifest.files to know what to download")
            return [RemoteFile(f.path, uri + quote(f.path), f.size or None, f.sha256 or None) for f in manifest.files]
        name = unquote(Path(urlparse(uri).path).name) or "download"
        known = manifest.files[0] if manifest.files else None
        return [RemoteFile(known.path if known else name, uri, (known.size or None) if known else None,
                           (known.sha256 or None) if known else None)]

    def remote_fingerprint(self, manifest, ctx):
        files = self._files(manifest)
        c = _client(ctx)
        parts = []
        for rf in files:
            r = c.head(rf.url, headers=_bearer(manifest, ctx.secrets, "url_token"))
            if r.status_code >= 400:
                raise FetchError(f"HEAD {r.status_code} for {rf.path}")
            parts.append(f"{rf.path}:{r.headers.get('etag') or r.headers.get('last-modified') or r.headers.get('content-length')}")
        return "|".join(parts)

    def plan(self, manifest, ctx):
        return Plan(self._files(manifest), None)

    def fetch(self, manifest, ctx, plan):
        c = _client(ctx)
        files = _download_all(c, plan, ctx, _bearer(manifest, ctx.secrets, "url_token"))
        try:
            fp = self.remote_fingerprint(manifest, ctx)
        except (FetchError, httpx.HTTPError):
            fp = None
        return FetchOutcome(files, fp)


class HuggingFaceFetcher:
    """source_uri: ``hf://datasets/<org>/<name>`` (or just ``<org>/<name>``); manifest.version is the
    revision (branch/tag/commit). Uses the public HTTP API so no extra dependency is needed."""

    def __init__(self, endpoint: str = "https://huggingface.co"):
        self.endpoint = endpoint.rstrip("/")

    @staticmethod
    def repo_id(manifest: DatasetManifest) -> str:
        uri = manifest.source_uri
        for pre in ("hf://datasets/", "hf://"):
            if uri.startswith(pre):
                return uri[len(pre):].strip("/")
        return uri.strip("/")

    def _api(self, manifest, ctx):
        repo, rev = self.repo_id(manifest), quote(manifest.version, safe="")
        r = _client(ctx).get(f"{self.endpoint}/api/datasets/{repo}/revision/{rev}",
                             headers=_bearer(manifest, ctx.secrets, "hf_token"), params={"blobs": "true"})
        if r.status_code in (401, 403):
            raise AuthRequired(f"HuggingFace denied access to {repo} (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise FetchError(f"HuggingFace API HTTP {r.status_code} for {repo}@{manifest.version}")
        return r.json()

    def remote_fingerprint(self, manifest, ctx):
        return self._api(manifest, ctx).get("sha")

    def plan(self, manifest, ctx):
        data = self._api(manifest, ctx)
        repo, sha = self.repo_id(manifest), data.get("sha")
        wanted = {f.path: f for f in manifest.files}
        files = []
        for s in data.get("siblings", []):
            path = s["rfilename"]
            if wanted and path not in wanted:
                continue
            lfs = (s.get("lfs") or {}).get("sha256")
            url = f"{self.endpoint}/datasets/{repo}/resolve/{sha or quote(manifest.version, safe='')}/{quote(path)}"
            files.append(RemoteFile(path, url, s.get("size"), lfs))
        if wanted:
            missing = set(wanted) - {f.path for f in files}
            if missing:
                raise FetchError(f"files not present upstream at {manifest.version}: {sorted(missing)}")
        if not files:
            raise FetchError(f"no files listed for {repo}@{manifest.version}")
        return Plan(files, sha)

    def fetch(self, manifest, ctx, plan):
        headers = _bearer(manifest, ctx.secrets, "hf_token")
        files = _download_all(_client(ctx), plan, ctx, headers)
        return FetchOutcome(files, plan.fingerprint)


class LocalFetcher:
    """local_path / user_upload: copy from a path on this machine."""

    def _src(self, manifest) -> Path:
        p = Path(manifest.source_uri.removeprefix("file://"))
        if not p.exists():
            raise FetchError(f"local source not found: {p}")
        return p

    def remote_fingerprint(self, manifest, ctx):
        return None  # local sources have no upstream to compare; checksums cover integrity

    def plan(self, manifest, ctx):
        src = self._src(manifest)
        if src.is_file():
            return Plan([RemoteFile(src.name, None, src.stat().st_size)], None)
        return Plan([RemoteFile(f.path, None, f.size) for f in scan_dir(src)], None)

    def fetch(self, manifest, ctx, plan):
        src = self._src(manifest)
        ctx.dest.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            shutil.copy2(src, ctx.dest / src.name)
        else:
            shutil.copytree(src, ctx.dest, dirs_exist_ok=True)
        files = scan_dir(ctx.dest)
        for f in files:
            ctx.progress(f.path, f.size, f.size)
        return FetchOutcome(files, None)


class _StubFetcher:
    kind = ""

    def _no(self):
        raise NotImplementedFetcher(f"{self.kind} fetcher is not implemented yet")

    def plan(self, manifest, ctx): self._no()
    def fetch(self, manifest, ctx, plan): self._no()
    def remote_fingerprint(self, manifest, ctx): self._no()


class KaggleFetcher(_StubFetcher):
    kind = "Kaggle"


class S3Fetcher(_StubFetcher):
    kind = "S3"


def default_fetchers(hf_endpoint: str = "https://huggingface.co") -> dict[SourceType, Fetcher]:
    local = LocalFetcher()
    return {
        SourceType.URL: UrlFetcher(),
        SourceType.HUGGINGFACE: HuggingFaceFetcher(hf_endpoint),
        SourceType.LOCAL_PATH: local,
        SourceType.USER_UPLOAD: local,
        SourceType.KAGGLE: KaggleFetcher(),
        SourceType.S3: S3Fetcher(),
    }
