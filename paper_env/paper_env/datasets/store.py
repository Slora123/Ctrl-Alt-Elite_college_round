"""Dataset registry + content-addressed cache. See README for the full contract."""
from __future__ import annotations

import fcntl
import re
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import httpx

from ..config import Settings
from ..domain import (
    AccessType, ActionKind, DatasetFile, DatasetManifest, DatasetSplit, DatasetStatus, EventType,
    SourceType, SplitProvenance, make_dataset_id,
)
from ..events import EventBus
from ..hooks import Action, ActionBlocked, ActionGate
from ..repos import Conflict, NotFound, UnitOfWorkFactory
from ..security import SecretsProvider
from .checksums import (
    dir_size, make_read_only, overall_checksum, rmtree_force, scan_dir, sha256_file,
)
from .download import InsufficientDisk
from .fetchers import AuthRequired, FetchContext, FetchError, Fetcher, NotImplementedFetcher, default_fetchers
from .network import default_network_check

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REFETCHABLE = {SourceType.HUGGINGFACE, SourceType.URL, SourceType.KAGGLE, SourceType.S3}
_USABLE = (DatasetStatus.CACHED, DatasetStatus.VERIFIED)


class DatasetNotAvailable(RuntimeError):
    def __init__(self, dataset_id: str, status: DatasetStatus, hint: str):
        super().__init__(f"dataset {dataset_id} is {status.value}: {hint}")
        self.dataset_id, self.status = dataset_id, status


@dataclass
class FetchResult:
    dataset_id: str
    status: str  # verified | cached | failed | blocked
    path: Path | None = None
    error: str | None = None
    error_code: str | None = None
    blocked_action_id: str | None = None
    already_cached: bool = False


@dataclass
class VerifyResult:
    dataset_id: str
    ok: bool
    status: DatasetStatus
    mismatches: list[str] = field(default_factory=list)


@dataclass
class UpstreamCheck:
    dataset_id: str
    changed: bool | None  # None = could not determine
    detail: str = ""


@dataclass
class PreflightProblem:
    dataset_id: str
    code: str  # unregistered | offline_remote_only | not_cached | corrupted | upstream_changed | downloading | blocked | fetch_failed
    message: str


@dataclass
class PreflightReport:
    ok: bool
    problems: list[PreflightProblem] = field(default_factory=list)
    approximate_split: bool = False
    dataset_manifests: list[dict] = field(default_factory=list)  # {dataset_id, overall_checksum, status}

    @property
    def message(self) -> str:
        return "; ".join(p.message for p in self.problems) if self.problems else "ok"


class DatasetStore:
    def __init__(self, settings: Settings, uow_factory: UnitOfWorkFactory, bus: EventBus, gate: ActionGate,
                 secrets: SecretsProvider, fetchers: dict[SourceType, Fetcher] | None = None,
                 network_check: Callable[[DatasetManifest], bool] | None = None,
                 client: httpx.Client | None = None, hf_endpoint: str = "https://huggingface.co",
                 retries: int = 5, backoff: float = 0.5):
        self.settings, self._uow, self.bus, self.gate, self.secrets = settings, uow_factory, bus, gate, secrets
        self.fetchers = fetchers or default_fetchers(hf_endpoint)
        self.network_check = network_check or (lambda m: default_network_check(m, hf_endpoint))
        self.client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, connect=10.0))
        self.retries, self.backoff = retries, backoff
        self._key_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._mounts: dict[str, tuple[str, list[str]]] = {}
        self._derived_seen: dict[str, int] = {}
        bus.scrubber.add_secrets(secrets.known_values())
        gate.register_executor(ActionKind.FETCH_DATASET, lambda b: self._fetch_unchecked(b.payload["dataset_id"]))
        self.settings.cache_dir().mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ registry
    def register(self, manifest: DatasetManifest) -> DatasetManifest:
        """Register a dataset version. Versions are immutable: re-registering identical data is a no-op,
        different data under the same id raises Conflict."""
        self._validate(manifest)
        fresh = manifest.model_copy(update={"status": DatasetStatus.REMOTE_ONLY, "cache_path": None,
                                            "size_bytes": 0, "last_accessed_at": None})
        if fresh.files and all(f.sha256 for f in fresh.files) and not fresh.overall_checksum:
            fresh.overall_checksum = overall_checksum(fresh.files)
        with self._uow() as u:
            if u.datasets.exists(fresh.dataset_id):
                old = u.datasets.get(fresh.dataset_id)
                same = all(getattr(old, k) == getattr(fresh, k) for k in
                           ("name", "version", "source_type", "source_uri", "files", "overall_checksum",
                            "access_type", "splits", "parent_dataset_id", "producing_cell_version_id"))
                if same:
                    return old
                raise Conflict(f"dataset {fresh.dataset_id} already registered with different content; "
                               f"register a new version instead")
            u.datasets.upsert(fresh)
        return fresh

    def _validate(self, m: DatasetManifest) -> None:
        if not m.dataset_id or "/" in m.dataset_id or ".." in m.dataset_id:
            raise ValueError(f"invalid dataset_id {m.dataset_id!r}")
        if self.bus.scrubber.scrub(m.source_uri) != m.source_uri:
            raise ValueError("source_uri appears to contain credentials; configure them via the secrets provider")
        for f in m.files:
            if f.sha256 and not _HEX64.match(f.sha256):
                raise ValueError(f"invalid sha256 for {f.path}")
            p = Path(f.path)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError(f"unsafe file path {f.path!r}")
        if m.overall_checksum and m.files and all(f.sha256 for f in m.files) \
                and overall_checksum(m.files) != m.overall_checksum:
            raise ValueError("overall_checksum does not match the listed file checksums")

    def get(self, dataset_id: str) -> DatasetManifest:
        with self._uow() as u:
            return u.datasets.get(dataset_id)

    def list(self) -> list[DatasetManifest]:
        with self._uow() as u:
            return u.datasets.list()

    def status(self, dataset_id: str) -> DatasetStatus:
        return self.get(dataset_id).status

    def _update(self, dataset_id: str, **changes) -> DatasetManifest:
        with self._uow() as u:
            m = u.datasets.get(dataset_id).model_copy(update=changes)
            u.datasets.upsert(m)
        return m

    # ------------------------------------------------------------------ resolve / access
    def resolve(self, dataset_id: str) -> Path:
        """Read-only path of a usable dataset. Never fetches implicitly."""
        m = self.get(dataset_id)
        if m.status not in _USABLE:
            raise DatasetNotAvailable(dataset_id, m.status, "call fetch() (or run preflight) first")
        if not m.cache_path or not Path(m.cache_path).is_dir():
            self._update(dataset_id, status=DatasetStatus.REMOTE_ONLY, cache_path=None)
            raise DatasetNotAvailable(dataset_id, DatasetStatus.REMOTE_ONLY, "cached files are missing")
        self._update(dataset_id, last_accessed_at=datetime.now(timezone.utc))
        return Path(m.cache_path)

    def acquire(self, run_id: str, dataset_ids: Iterable[str]) -> None:
        """Mark datasets as in use by an active run (protects them from eviction)."""
        with self._uow() as u:
            for d in dataset_ids:
                u.datasets.mark_in_use(run_id, d)

    def release(self, run_id: str) -> None:
        with self._uow() as u:
            u.datasets.release_run(run_id)

    def approximate_split(self, dataset_ids: Iterable[str]) -> bool:
        with self._uow() as u:
            return any(s.provenance == SplitProvenance.APPROXIMATE
                       for d in dataset_ids for s in u.datasets.get(d).splits)

    # ------------------------------------------------------------------ fetch
    def fetch(self, dataset_id: str) -> FetchResult:
        self.get(dataset_id)  # NotFound surfaces to the caller
        try:
            self.gate.check(Action(ActionKind.FETCH_DATASET, payload={"dataset_id": dataset_id}))
        except ActionBlocked as b:
            return FetchResult(dataset_id, "blocked", error=str(b), error_code="blocked", blocked_action_id=b.action_id)
        return self._fetch_unchecked(dataset_id)

    def _key_for(self, m: DatasetManifest) -> str:
        if m.overall_checksum:
            return m.overall_checksum
        import hashlib
        return "pending-" + hashlib.sha256(m.dataset_id.encode()).hexdigest()[:24]

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._key_locks.setdefault(key, threading.Lock())

    @contextmanager
    def _key_lock(self, key: str):
        """Thread lock + flock so concurrent fetches (threads or processes) share one download."""
        lockdir = self.settings.cache_dir() / ".locks"
        lockdir.mkdir(parents=True, exist_ok=True)
        with self._lock_for(key):
            with open(lockdir / f"{key}.lock", "w") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def _ready_result(self, m: DatasetManifest) -> FetchResult | None:
        if m.status in _USABLE and m.cache_path and Path(m.cache_path).is_dir():
            self._update(m.dataset_id, last_accessed_at=datetime.now(timezone.utc))
            return FetchResult(m.dataset_id, m.status.value, Path(m.cache_path), already_cached=True)
        if m.status in (DatasetStatus.CORRUPTED, DatasetStatus.UPSTREAM_CHANGED):
            return FetchResult(m.dataset_id, "failed", error_code=m.status.value, error=(
                f"dataset is {m.status.value}; refusing to refresh automatically. "
                f"Inspect it, then call reset() explicitly."))
        return None

    def _fetch_unchecked(self, dataset_id: str) -> FetchResult:
        m = self.get(dataset_id)
        if (r := self._ready_result(m)):
            return r
        key = self._key_for(m)
        with self._key_lock(key):
            m = self.get(dataset_id)  # re-read: another fetch may have finished while we waited
            if (r := self._ready_result(m)):
                return r
            return self._download_locked(m, key)

    def _fail(self, m: DatasetManifest, code: str, msg: str, status: DatasetStatus = DatasetStatus.REMOTE_ONLY,
              extra: dict | None = None, event: EventType = EventType.DATASET_FETCH_FAILED) -> FetchResult:
        self._update(m.dataset_id, status=status)
        self.bus.publish(event, {"dataset_id": m.dataset_id, "code": code, "error": msg,
                                 "status": status.value, **(extra or {})})
        return FetchResult(m.dataset_id, "failed", error=self.bus.scrubber.scrub(msg), error_code=code)

    def _download_locked(self, m: DatasetManifest, key: str) -> FetchResult:
        cas = self.settings.cache_dir()
        # another dataset id may already have materialised this exact content
        if m.overall_checksum and (cas / m.overall_checksum).is_dir():
            return self._adopt(m, cas / m.overall_checksum)
        fetcher = self.fetchers.get(m.source_type)
        if fetcher is None:
            return self._fail(m, "no_fetcher", f"no fetcher for source type {m.source_type.value}")
        staging = cas / ".partial" / key
        self._update(m.dataset_id, status=DatasetStatus.DOWNLOADING)
        self.bus.publish(EventType.DATASET_FETCH_STARTED, {"dataset_id": m.dataset_id, "source_type": m.source_type.value})
        state = {"last": 0.0, "files_done": 0}

        def progress(path: str, done: int, total: int | None) -> None:
            now = time.monotonic()
            if now - state["last"] >= 0.2 or (total is not None and done >= total):
                state["last"] = now
                self.bus.publish(EventType.DATASET_FETCH_PROGRESS, {
                    "dataset_id": m.dataset_id, "file": path, "bytes_done": done, "bytes_total": total})

        ctx = FetchContext(dest=staging, secrets=self.secrets, progress=progress,
                           min_free_bytes=self.settings.min_free_disk_bytes, client=self.client,
                           retries=self.retries, backoff=self.backoff)
        try:
            plan = fetcher.plan(m, ctx)
            known = sum(f.size or 0 for f in plan.files)
            self._make_room(known)
            free = shutil.disk_usage(self.settings.cache_dir()).free
            if free < known + self.settings.min_free_disk_bytes:
                raise InsufficientDisk(f"need {known} bytes (+{self.settings.min_free_disk_bytes} reserve) "
                                       f"but only {free} free, even after evicting what is evictable")
            staging.mkdir(parents=True, exist_ok=True)
            outcome = fetcher.fetch(m, ctx, plan)
        except AuthRequired as e:
            return self._fail(m, "auth_required", str(e))
        except NotImplementedFetcher as e:
            return self._fail(m, "not_implemented", str(e))
        except InsufficientDisk as e:
            return self._fail(m, "insufficient_disk", str(e))
        except FetchError as e:
            return self._fail(m, "fetch_error", str(e))
        except (httpx.HTTPError, OSError) as e:
            return self._fail(m, "network_error", f"{type(e).__name__}: {e}")

        # ---- verify against expectations: manifest first, then source-published checksums
        expected = {f.path: f for f in m.files if f.sha256}
        remote_sha = {f.path: f.sha256 for f in plan.files if f.sha256}
        got = {f.path: f for f in outcome.files}
        mismatches = [f"{p}: expected sha256 {e.sha256[:12]}…, got {(got[p].sha256[:12] + '…') if p in got else 'MISSING'}"
                      for p, e in expected.items() if p not in got or got[p].sha256 != e.sha256 or
                      (e.size and got[p].size != e.size)]
        mismatches += [f"{p}: transfer corrupted (remote sha256 differs)" for p, s in remote_sha.items()
                       if p in got and got[p].sha256 != s and p not in expected]
        overall = overall_checksum(outcome.files)
        if m.overall_checksum and overall != m.overall_checksum and not mismatches:
            mismatches.append("overall_checksum mismatch")
        if mismatches:
            rmtree_force(staging)
            upstream = bool(m.remote_fingerprint and outcome.fingerprint and m.remote_fingerprint != outcome.fingerprint)
            if upstream:
                return self._fail(m, "upstream_changed", "upstream content changed since registration: " + "; ".join(mismatches),
                                  DatasetStatus.UPSTREAM_CHANGED, {"mismatches": mismatches,
                                  "previous": m.remote_fingerprint, "current": outcome.fingerprint},
                                  EventType.DATASET_UPSTREAM_CHANGED)
            return self._fail(m, "checksum_mismatch", "checksum mismatch: " + "; ".join(mismatches),
                              DatasetStatus.CORRUPTED, {"mismatches": mismatches})

        fully_verified = bool(m.files) and all(f.sha256 for f in m.files) and set(expected) == set(got)
        final = cas / overall
        if final.is_dir():
            rmtree_force(staging)  # identical content already cached under another id
        else:
            staging.rename(final)
            make_read_only(final)
        status = DatasetStatus.VERIFIED if fully_verified else DatasetStatus.CACHED
        self._update(m.dataset_id, status=status, files=outcome.files, overall_checksum=overall,
                     cache_path=str(final), size_bytes=sum(f.size for f in outcome.files),
                     remote_fingerprint=outcome.fingerprint or m.remote_fingerprint,
                     last_accessed_at=datetime.now(timezone.utc))
        if fully_verified:
            self.bus.publish(EventType.DATASET_VERIFIED, {"dataset_id": m.dataset_id, "overall_checksum": overall})
        else:
            self.bus.publish(EventType.DATASET_CACHED, {
                "dataset_id": m.dataset_id, "overall_checksum": overall,
                "note": "no complete expected checksums in the manifest; recorded on first fetch (not independently verified)"})
        return FetchResult(m.dataset_id, status.value, final)

    def _adopt(self, m: DatasetManifest, path: Path) -> FetchResult:
        files = scan_dir(path)
        expected = {f.path: f.sha256 for f in m.files if f.sha256}
        if any(expected.get(f.path, f.sha256) != f.sha256 for f in files) or not set(expected) <= {f.path for f in files}:
            return self._fail(m, "checksum_mismatch", "cached content does not match manifest", DatasetStatus.CORRUPTED)
        self._update(m.dataset_id, status=DatasetStatus.VERIFIED, files=files, cache_path=str(path),
                     size_bytes=sum(f.size for f in files), last_accessed_at=datetime.now(timezone.utc))
        self.bus.publish(EventType.DATASET_VERIFIED, {"dataset_id": m.dataset_id, "overall_checksum": m.overall_checksum,
                                                     "shared_cache": True})
        return FetchResult(m.dataset_id, "verified", path, already_cached=True)

    # ------------------------------------------------------------------ verify / upstream / reset
    def verify(self, dataset_id: str) -> VerifyResult:
        m = self.get(dataset_id)
        if m.status not in _USABLE or not m.cache_path or not Path(m.cache_path).is_dir():
            return VerifyResult(dataset_id, False, m.status, [f"not cached (status {m.status.value})"])
        root, mismatches = Path(m.cache_path), []
        actual = {f.path: f for f in scan_dir(root)}
        for f in m.files:
            a = actual.get(f.path)
            if a is None:
                mismatches.append(f"{f.path}: missing")
            elif f.sha256 and a.sha256 != f.sha256:
                mismatches.append(f"{f.path}: sha256 differs")
        mismatches += [f"{p}: unexpected extra file" for p in actual if p not in {f.path for f in m.files}]
        if mismatches:
            self._update(dataset_id, status=DatasetStatus.CORRUPTED)
            self.bus.publish(EventType.DATASET_FETCH_FAILED, {
                "dataset_id": dataset_id, "code": "checksum_mismatch", "status": "corrupted",
                "error": "cached files do not match the manifest", "mismatches": mismatches})
            return VerifyResult(dataset_id, False, DatasetStatus.CORRUPTED, mismatches)
        complete = bool(m.files) and all(f.sha256 for f in m.files)
        status = DatasetStatus.VERIFIED if complete else DatasetStatus.CACHED
        self._update(dataset_id, status=status)
        if complete:
            self.bus.publish(EventType.DATASET_VERIFIED, {"dataset_id": dataset_id, "overall_checksum": m.overall_checksum})
        return VerifyResult(dataset_id, True, status)

    def check_upstream(self, dataset_id: str) -> UpstreamCheck:
        """Compare the remote fingerprint with the recorded one. Flags, never refreshes."""
        m = self.get(dataset_id)
        fetcher = self.fetchers.get(m.source_type)
        ctx = FetchContext(dest=Path("."), secrets=self.secrets, progress=lambda *a: None, client=self.client)
        try:
            current = fetcher.remote_fingerprint(m, ctx) if fetcher else None
        except (FetchError, httpx.HTTPError, OSError) as e:
            return UpstreamCheck(dataset_id, None, f"could not check upstream: {type(e).__name__}")
        if current is None or m.remote_fingerprint is None:
            if current and m.remote_fingerprint is None:
                self._update(dataset_id, remote_fingerprint=current)
            return UpstreamCheck(dataset_id, None, "no recorded or remote fingerprint to compare")
        if current == m.remote_fingerprint:
            return UpstreamCheck(dataset_id, False)
        self._update(dataset_id, status=DatasetStatus.UPSTREAM_CHANGED)
        self.bus.publish(EventType.DATASET_UPSTREAM_CHANGED, {
            "dataset_id": dataset_id, "previous": m.remote_fingerprint, "current": current})
        return UpstreamCheck(dataset_id, True, f"{m.remote_fingerprint} -> {current}")

    def reset(self, dataset_id: str, accept_upstream: bool = False) -> DatasetManifest:
        """Explicit operator action after corruption / upstream change: drop the cached copy and return
        to remote_only. ``accept_upstream=True`` also forgets expected checksums so the new upstream
        content is recorded on next fetch. Nothing calls this automatically."""
        m = self.get(dataset_id)
        self._drop_cache(m)
        changes: dict = {"status": DatasetStatus.REMOTE_ONLY, "cache_path": None, "size_bytes": 0}
        if accept_upstream:
            changes.update(files=[], overall_checksum=None, remote_fingerprint=None)
        return self._update(dataset_id, **changes)

    def _drop_cache(self, m: DatasetManifest) -> None:
        if not m.cache_path:
            return
        with self._uow() as u:
            shared = [o for o in u.datasets.list() if o.cache_path == m.cache_path and o.dataset_id != m.dataset_id]
        if not shared:
            rmtree_force(Path(m.cache_path))

    # ------------------------------------------------------------------ eviction
    def cache_bytes(self) -> int:
        seen, total = set(), 0
        cas = str(self.settings.cache_dir())
        for m in self.list():
            if m.cache_path and m.cache_path.startswith(cas) and m.cache_path not in seen:
                seen.add(m.cache_path)
                total += m.size_bytes
        return total

    def _make_room(self, incoming: int) -> None:
        """Evict least-recently-used re-fetchable datasets until the cap and the free-disk reserve are
        satisfied. Datasets used by an active run, uploads and derived data are never evicted."""
        cas = self.settings.cache_dir()

        def need() -> bool:
            over_cap = self.cache_bytes() + incoming > self.settings.cache_max_bytes
            low_disk = shutil.disk_usage(cas).free < incoming + self.settings.min_free_disk_bytes
            return over_cap or low_disk

        if not need():
            return
        with self._uow() as u:
            in_use = u.datasets.in_use_dataset_ids()
            cands = [m for m in u.datasets.list()
                     if m.status in _USABLE and m.source_type in _REFETCHABLE and m.cache_path
                     and m.parent_dataset_id is None and m.run_id is None and m.dataset_id not in in_use]
        cands.sort(key=lambda m: m.last_accessed_at or datetime.min.replace(tzinfo=timezone.utc))
        for m in cands:
            if not need():
                break
            self._drop_cache(m)
            self._update(m.dataset_id, status=DatasetStatus.REMOTE_ONLY, cache_path=None, size_bytes=0)
            self.bus.publish(EventType.DATASET_EVICTED, {"dataset_id": m.dataset_id, "bytes": m.size_bytes})

    # ------------------------------------------------------------------ uploads & derived
    def register_upload(self, name: str, version: str, path: Path, *, author: str | None = None,
                        license: str | None = None, splits: list[DatasetSplit] | None = None) -> FetchResult:
        """Register a local file/dir the user provides and copy it into the cache (checksummed now)."""
        path = Path(path).resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        files = scan_dir(path) if path.is_dir() else [DatasetFile(path=path.name, size=path.stat().st_size,
                                                                   sha256=sha256_file(path))]
        m = self.register(DatasetManifest(
            dataset_id=make_dataset_id(name, version), name=name, version=version,
            source_type=SourceType.USER_UPLOAD, source_uri=str(path), files=files,
            access_type=AccessType.USER_UPLOADED, license=license, splits=splits or [], author=author))
        return self.fetch(m.dataset_id)

    def register_derived(self, *, run_id: str, name: str, version: str, directory: Path,
                         parent_dataset_id: str | None, producing_cell_version_id: str | None,
                         author: str | None = None, splits: list[DatasetSplit] | None = None,
                         license: str | None = None) -> DatasetManifest:
        """Register preprocessed data written under the run's own directory. It stays there (not copied
        into the shared cache, never evicted) and links to its parent and producing cell version."""
        root = Path(directory).resolve()
        allowed = self.settings.runs_dir(run_id).resolve()
        if allowed not in root.parents and root != allowed:
            raise ValueError(f"derived data must live under {allowed}")
        if not root.is_dir():
            raise FileNotFoundError(root)
        parent = self.get(parent_dataset_id) if parent_dataset_id else None
        files = scan_dir(root)
        make_read_only(root)
        manifest = DatasetManifest(
            dataset_id=make_dataset_id(name, version), name=name, version=version,
            source_type=SourceType.LOCAL_PATH, source_uri=str(root), files=files,
            overall_checksum=overall_checksum(files), license=license or (parent.license if parent else None),
            access_type=AccessType.USER_UPLOADED,
            splits=splits if splits is not None else (parent.splits if parent else []),
            status=DatasetStatus.VERIFIED, parent_dataset_id=parent_dataset_id,
            producing_cell_version_id=producing_cell_version_id, author=author, run_id=run_id,
            cache_path=str(root), size_bytes=sum(f.size for f in files),
            last_accessed_at=datetime.now(timezone.utc))
        with self._uow() as u:
            if u.datasets.exists(manifest.dataset_id):
                raise Conflict(f"dataset {manifest.dataset_id} already exists")
            u.datasets.upsert(manifest)
        self.bus.publish(EventType.DATASET_VERIFIED, {"dataset_id": manifest.dataset_id,
                                                     "derived_from": parent_dataset_id, "run_id": run_id},
                         run_id=run_id)
        return manifest

    # ------------------------------------------------------------------ kernel mount
    def prepare_mount(self, paper_env_id: str, run_id: str, dataset_ids: Iterable[str]) -> Path:
        """Write the read-only view a run's kernel sees: symlinks to the cache + a manifest snapshot.
        Returns the manifest path (the kernel finds it via PAPER_ENV_DATASETS_MANIFEST)."""
        ids = list(dict.fromkeys(dataset_ids))
        self._mounts[paper_env_id] = (run_id, ids)
        return self._write_mount(paper_env_id)

    def add_to_mount(self, paper_env_id: str, dataset_id: str) -> None:
        if paper_env_id in self._mounts:
            run_id, ids = self._mounts[paper_env_id]
            if dataset_id not in ids:
                ids.append(dataset_id)
            self._write_mount(paper_env_id)

    def mount_manifest_path(self, paper_env_id: str) -> Path:
        return self.settings.env_dir(paper_env_id) / "datasets_mount" / "manifest.json"

    def _write_mount(self, paper_env_id: str) -> Path:
        import json
        run_id, ids = self._mounts[paper_env_id]
        mount = self.settings.env_dir(paper_env_id) / "datasets_mount"
        mount.mkdir(parents=True, exist_ok=True)
        for p in mount.iterdir():
            if p.is_symlink():
                p.unlink()
        entries = {}
        for d in ids:
            try:
                m = self.get(d)
            except NotFound:
                continue
            if m.status not in _USABLE or not m.cache_path or not Path(m.cache_path).is_dir():
                continue
            link = mount / m.dataset_id.replace("/", "_")
            link.symlink_to(m.cache_path, target_is_directory=True)
            entries[m.dataset_id] = {
                "path": str(link), "name": m.name, "version": m.version, "status": m.status.value,
                "license": m.license, "access_type": m.access_type.value, "overall_checksum": m.overall_checksum,
                "parent_dataset_id": m.parent_dataset_id,
                "splits": [s.model_dump(mode="json") for s in m.splits],
                "files": [{"path": f.path, "size": f.size} for f in m.files]}  # no source_uri, no credentials
        derived = self.settings.runs_dir(run_id) / "derived"
        derived.mkdir(parents=True, exist_ok=True)
        snap = {"version": 1, "run": {"id": run_id, "derived_dir": str(derived)}, "datasets": entries}
        tmp = mount / "manifest.json.tmp"
        tmp.write_text(json.dumps(snap, indent=1))
        tmp.replace(mount / "manifest.json")
        return mount / "manifest.json"

    def collect_derived(self, paper_env_id: str, run_id: str, producing_cell_version_id: str,
                        author: str | None = None) -> tuple[list[DatasetManifest], list[str]]:
        """Register datasets that a cell created via ``datasets.derive``. The request file is plain JSON lines
        written by the kernel; each entry is validated and the directory is recomputed here, never trusted."""
        import json
        req_file = self.settings.runs_dir(run_id) / "derived_requests.jsonl"
        if not req_file.exists():
            return [], []
        lines = req_file.read_text().splitlines()
        start = self._derived_seen.get(run_id, 0)
        self._derived_seen[run_id] = len(lines)
        done: list[DatasetManifest] = []
        errors: list[str] = []
        for line in lines[start:]:
            try:
                req = json.loads(line)
                name, version, parent = req["name"], req["version"], req.get("parent")
                if not (re.fullmatch(r"[A-Za-z0-9._-]+", name) and re.fullmatch(r"[A-Za-z0-9._-]+", version)):
                    raise ValueError("invalid name/version")
                m = self.register_derived(
                    run_id=run_id, name=name, version=version,
                    directory=self.settings.runs_dir(run_id) / "derived" / f"{name}@{version}",
                    parent_dataset_id=parent, producing_cell_version_id=producing_cell_version_id, author=author)
                self.add_to_mount(paper_env_id, m.dataset_id)
                done.append(m)
            except (ValueError, KeyError, TypeError, FileNotFoundError, Conflict, NotFound) as e:
                errors.append(f"derived dataset request rejected: {type(e).__name__}: {e}")
        return done, errors

    # ------------------------------------------------------------------ preflight
    def preflight(self, dataset_ids: Iterable[str], *, fetch_missing: bool = True,
                  run_id: str | None = None) -> PreflightReport:
        """Make sure every declared dataset is usable BEFORE any cell executes. Classification happens first
        with no side effects, so an offline/corrupt/unregistered dataset fails fast without partial fetching."""
        ids = list(dict.fromkeys(dataset_ids))
        problems: list[PreflightProblem] = []
        to_fetch: list[str] = []
        manifests: list[DatasetManifest] = []
        for d in ids:
            try:
                m = self.get(d)
            except NotFound:
                problems.append(PreflightProblem(d, "unregistered", f"dataset {d} is not registered"))
                continue
            manifests.append(m)
            if m.status in _USABLE and not (m.cache_path and Path(m.cache_path).is_dir()):
                m = self._update(d, status=DatasetStatus.REMOTE_ONLY, cache_path=None)
            if m.status in _USABLE:
                continue
            if m.status == DatasetStatus.CORRUPTED:
                problems.append(PreflightProblem(d, "corrupted", f"dataset {d} is corrupted; run reset() deliberately"))
            elif m.status == DatasetStatus.UPSTREAM_CHANGED:
                problems.append(PreflightProblem(d, "upstream_changed", f"dataset {d} changed upstream; refusing to use it silently"))
            elif m.status == DatasetStatus.DOWNLOADING:
                problems.append(PreflightProblem(d, "downloading", f"dataset {d} is still downloading"))
            elif m.source_type not in (SourceType.LOCAL_PATH, SourceType.USER_UPLOAD) and not self.network_check(m):
                problems.append(PreflightProblem(d, "offline_remote_only", (
                    f"dataset {d} is remote_only (not cached) and the network is unavailable; "
                    f"fetch it while online before running")))
            else:
                to_fetch.append(d)
        if not problems and fetch_missing:
            for d in to_fetch:
                r = self.fetch(d)
                if r.status == "blocked":
                    problems.append(PreflightProblem(d, "blocked", f"fetching {d} awaits approval ({r.blocked_action_id})"))
                elif r.status == "failed":
                    problems.append(PreflightProblem(d, "fetch_failed", f"fetching {d} failed: {r.error}"))
        elif to_fetch and not fetch_missing:
            problems += [PreflightProblem(d, "not_cached", f"dataset {d} is not cached") for d in to_fetch]
        final = [self.get(d) for d in ids if self._exists(d)]
        report = PreflightReport(
            ok=not problems, problems=problems,
            approximate_split=any(s.provenance == SplitProvenance.APPROXIMATE for m in final for s in m.splits),
            dataset_manifests=[{"dataset_id": m.dataset_id, "overall_checksum": m.overall_checksum,
                                "status": m.status.value} for m in final])
        if run_id and report.ok:
            self.acquire(run_id, ids)
        return report

    def _exists(self, dataset_id: str) -> bool:
        with self._uow() as u:
            return u.datasets.exists(dataset_id)
