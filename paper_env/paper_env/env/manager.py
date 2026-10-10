"""Environment lifecycle: create / install / pin / rebuild / lockfile.

Packages are NEVER installed by running pip inside a cell; everything goes through here.
Every mutating operation goes through the pre-execute hook gate and emits events.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from ..config import Settings
from ..domain import ActionKind, BlockedAction, EnvSpec, EventType
from ..events import EventBus
from ..hooks import Action, ActionBlocked, ActionGate
from ..repos import UnitOfWorkFactory
from .hardware import collect_hardware_info
from .procs import run_logged

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_*-]*$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_LOCAL_DIST = re.compile(r"\.(whl|tar\.gz|zip)$")


class InvalidSpec(ValueError):
    pass


@dataclass
class EnvOpResult:
    """Structured outcome of an env operation. ``status`` is one of
    success | failed | blocked."""

    op: str
    status: str
    success: bool = False
    failing_package: str | None = None
    conflict_details: str | None = None  # untrusted resolver text, scrubbed, never executed
    timed_out: bool = False
    error: str | None = None
    log_path: str | None = None
    lockfile_hash: str | None = None
    blocked_action_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LockfileInfo:
    text: str
    hash: str
    path: Path


def normalize_freeze(text: str) -> str:
    lines = sorted(l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#"))
    return "\n".join(lines) + "\n"


def lockfile_hash(freeze_text: str, python_full_version: str) -> str:
    h = hashlib.sha256()
    h.update(f"python=={python_full_version}\n".encode())
    h.update(normalize_freeze(freeze_text).encode())
    return h.hexdigest()


def validate_requirement(req: str) -> str:
    """Accept PEP 508 requirements or an existing local wheel/sdist path. Reject anything
    that could be parsed as a uv/pip option (leading '-') or is not a requirement."""
    req = req.strip()
    if not req or req.startswith("-"):
        raise InvalidSpec(f"invalid requirement {req!r}")
    if _LOCAL_DIST.search(req):
        if not Path(req).is_file():
            raise InvalidSpec(f"local distribution not found: {req}")
        return req
    try:
        Requirement(req)
    except InvalidRequirement as e:
        raise InvalidSpec(f"invalid requirement {req!r}: {e}") from e
    return req


def _req_name(req: str) -> str | None:
    try:
        return canonicalize_name(Requirement(req).name)
    except InvalidRequirement:
        return None  # local path: no name known


_CONFLICT = re.compile(r"(No solution found when resolving dependencies:.*)", re.S)
_FAIL_PKG_PATTERNS = [
    re.compile(r"Because there is no version of ([A-Za-z0-9._-]+)"),
    re.compile(r"Because ([A-Za-z0-9._-]+)[\s=<>!~]\S* (?:was not found|depends on)"),
    re.compile(r"Because ([A-Za-z0-9._-]+) was not found"),
    re.compile(r"Failed to (?:download|build|fetch)[^`\n]*`([A-Za-z0-9._-]+)"),
    re.compile(r"No matching distribution found for ([A-Za-z0-9._-]+)"),
    re.compile(r"error: .*?`([A-Za-z0-9._-]+)(?:==|>=|<=|\s|`)"),
]


def parse_failure(output: str) -> tuple[str | None, str | None]:
    """(failing_package, conflict_details) extracted best-effort from uv output.
    Returns None for what it cannot determine; it never guesses."""
    conflict = None
    m = _CONFLICT.search(output)
    if m:
        conflict = m.group(1).strip()[:8000]
    for p in _FAIL_PKG_PATTERNS:
        m = p.search(output)
        if m:
            return m.group(1), conflict
    return None, conflict


class EnvManager:
    def __init__(self, settings: Settings, uow_factory: UnitOfWorkFactory, bus: EventBus, gate: ActionGate):
        self.settings, self._uow, self.bus, self.gate = settings, uow_factory, bus, gate
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        for kind in (ActionKind.INSTALL_PACKAGE, ActionKind.PIN_VERSION, ActionKind.REBUILD_ENV):
            gate.register_executor(kind, self._execute_blocked)

    # ------------------------------------------------------------------ public API
    def create_env(self, paper_env_id: str) -> EnvOpResult:
        """Create the venv for the PaperEnv's stored EnvSpec, install, lock, hash."""
        return self._gated("create", paper_env_id, {"op": "create"}, ActionKind.REBUILD_ENV)

    def install_package(self, paper_env_id: str, name: str, version: str | None = None) -> EnvOpResult:
        self._check_name(name, version)
        return self._gated("install", paper_env_id, {"op": "install", "name": name, "version": version},
                           ActionKind.INSTALL_PACKAGE)

    def pin_version(self, paper_env_id: str, name: str, version: str) -> EnvOpResult:
        self._check_name(name, version)
        if not version:
            raise InvalidSpec("pin_version requires a version")
        return self._gated("pin", paper_env_id, {"op": "pin", "name": name, "version": version},
                           ActionKind.PIN_VERSION)

    def rebuild_env(self, paper_env_id: str) -> EnvOpResult:
        return self._gated("rebuild", paper_env_id, {"op": "rebuild"}, ActionKind.REBUILD_ENV)

    def get_lockfile(self, paper_env_id: str) -> LockfileInfo:
        path = self.settings.lockfile_path(paper_env_id)
        if not path.is_file():
            raise FileNotFoundError(f"no lockfile for {paper_env_id}: environment not built yet")
        text = path.read_text()
        with self._uow() as u:
            spec = u.paper_envs.get_spec(u.paper_envs.get(paper_env_id).env_spec_id)
        if not spec.lockfile_hash:
            raise FileNotFoundError(f"lockfile for {paper_env_id} exists but has no recorded hash")
        return LockfileInfo(text=text, hash=spec.lockfile_hash, path=path)

    def python_path(self, paper_env_id: str) -> Path:
        p = self.settings.venv_python(paper_env_id)
        if not p.exists():
            raise FileNotFoundError(f"environment for {paper_env_id} has not been created")
        return p

    def hardware_info(self, paper_env_id: str) -> dict[str, Any]:
        return collect_hardware_info(self.python_path(paper_env_id))

    def detect_drift(self, paper_env_id: str) -> bool:
        """Re-freeze the environment and compare with the recorded lockfile hash. Packages changed outside the Env
        manager (e.g. in the terminal) are recorded honestly: the lockfile and hash are updated (they are part of run
        identity) and an ``env_changed`` event flags the change as unmanaged. Returns True if drift was found."""
        with self._lock_for(paper_env_id):
            spec = self._load(paper_env_id)
            if not self.settings.venv_python(paper_env_id).exists():
                return False
            frozen = self._freeze(paper_env_id)
            if frozen is None or frozen[0] == spec.lockfile_hash:
                return False
            before = spec.lockfile_hash
            spec.lockfile_hash = frozen[0]
            self._save_spec(spec)
        self.bus.publish(EventType.ENV_CHANGED, {
            "op": "external_change", "before_lockfile_hash": before, "after_lockfile_hash": frozen[0],
            "unmanaged": True, "note": "packages changed outside the Env manager (terminal?); lockfile re-captured"},
            paper_env_id=paper_env_id)
        return True

    def cleanup_temp(self) -> list[str]:
        """Remove half-built environments (``*.staging`` / ``*.old``) left by a crash. Called at startup."""
        removed = []
        root = self.settings.data_dir / "envs"
        if root.is_dir():
            for d in root.iterdir():
                if d.is_dir() and d.name.endswith((".staging", ".old")):
                    shutil.rmtree(d, ignore_errors=True)
                    removed.append(d.name)
        return removed

    def delete_env_files(self, paper_env_id: str) -> None:
        shutil.rmtree(self.settings.env_dir(paper_env_id), ignore_errors=True)

    # ------------------------------------------------------------------ gating
    def _gated(self, op: str, paper_env_id: str, payload: dict, kind: ActionKind) -> EnvOpResult:
        try:
            self.gate.check(Action(kind, paper_env_id=paper_env_id, payload=payload))
        except ActionBlocked as b:
            return EnvOpResult(op=op, status="blocked", blocked_action_id=b.action_id, error=str(b))
        return self._run_op(op, paper_env_id, payload)

    def _execute_blocked(self, blocked: BlockedAction) -> EnvOpResult:
        p = blocked.payload
        return self._run_op(p["op"], blocked.paper_env_id, p)

    def _run_op(self, op: str, paper_env_id: str, p: dict) -> EnvOpResult:
        with self._lock_for(paper_env_id):
            if op == "create" or op == "rebuild":
                return self._build(op, paper_env_id)
            return self._install(op, paper_env_id, p["name"], p.get("version"))

    # ------------------------------------------------------------------ internals
    def _lock_for(self, paper_env_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(paper_env_id, threading.Lock())

    @staticmethod
    def _check_name(name: str, version: str | None) -> None:
        if not _NAME_RE.match(name or ""):
            raise InvalidSpec(f"invalid package name {name!r}")
        if version is not None and not _VERSION_RE.match(version):
            raise InvalidSpec(f"invalid version {version!r}")

    def _uv(self) -> str:
        exe = shutil.which(self.settings.uv_bin)
        if not exe:
            raise FileNotFoundError(f"uv executable {self.settings.uv_bin!r} not found")
        return exe

    def _log(self, paper_env_id: str, name: str) -> Path:
        return self.settings.logs_dir(paper_env_id) / f"{int(time.time() * 1000)}-{name}.log"

    def _effective_requirements(self, spec: EnvSpec) -> list[str]:
        """requirements with pins applied (a pin overrides any spec for the same package)."""
        pins = {canonicalize_name(k): (k, v) for k, v in spec.pins.items()}
        out, seen = [], set()
        for r in spec.requirements:
            n = _req_name(r)
            if n in pins:
                k, v = pins[n]
                out.append(f"{k}=={v}")
                seen.add(n)
            else:
                out.append(r)
        out += [f"{k}=={v}" for n, (k, v) in pins.items() if n not in seen]
        return out

    def _install_args(self, paper_env_id: str, reqs: list[str]) -> list[str]:
        return [self._uv(), "pip", "install", "--python", str(self.settings.venv_python(paper_env_id)),
                "--no-progress", "--color", "never", *reqs]

    def _freeze(self, paper_env_id: str) -> tuple[str, str] | None:
        """Write the lockfile; return (hash, text) or None on failure."""
        py = self.settings.venv_python(paper_env_id)
        r = run_logged([self._uv(), "pip", "freeze", "--python", str(py), "--color", "never"],
                       timeout=120, log_path=self._log(paper_env_id, "freeze"), scrubber=self.bus.scrubber,
                       merge_stderr=False)
        if r.returncode != 0:
            return None
        body = r.stdout
        info = collect_hardware_info(py).get("env_python") or {}
        full_ver = info.get("python_version")
        if not full_ver:
            return None  # cannot compute identity without the real interpreter version
        text = normalize_freeze(body)
        h = lockfile_hash(text, full_ver)
        self.settings.lockfile_path(paper_env_id).write_text(f"# python=={full_ver}\n{text}")
        return h, text

    def _emit_failed(self, paper_env_id: str, op: str, res: EnvOpResult) -> EnvOpResult:
        res.status, res.success = "failed", False
        self.bus.publish(EventType.ENV_INSTALL_FAILED, {
            "op": op, "failing_package": res.failing_package, "conflict_details": res.conflict_details,
            "timed_out": res.timed_out, "error": res.error, "log_path": res.log_path,
        }, paper_env_id=paper_env_id)
        return res

    def _finish_ok(self, paper_env_id: str, op: str, before: str | None, after: str, log: str,
                   extra: dict | None = None) -> EnvOpResult:
        self.bus.publish(EventType.ENV_CHANGED, {
            "op": op, "before_lockfile_hash": before, "after_lockfile_hash": after, **(extra or {}),
        }, paper_env_id=paper_env_id)
        return EnvOpResult(op=op, status="success", success=True, lockfile_hash=after, log_path=log,
                           extra=extra or {})

    def _load(self, paper_env_id: str) -> EnvSpec:
        with self._uow() as u:
            return u.paper_envs.get_spec(u.paper_envs.get(paper_env_id).env_spec_id)

    def _save_spec(self, spec: EnvSpec) -> None:
        with self._uow() as u:
            u.paper_envs.update_spec(spec)

    def _build(self, op: str, paper_env_id: str) -> EnvOpResult:
        spec = self._load(paper_env_id)
        before = spec.lockfile_hash
        try:
            for r in spec.requirements:
                validate_requirement(r)
        except InvalidSpec as e:
            return self._emit_failed(paper_env_id, op, EnvOpResult(op=op, status="failed", error=str(e)))
        env_dir = self.settings.env_dir(paper_env_id)
        # build into a sibling dir; swap only on success so a failed rebuild keeps the old env
        staging = env_dir.with_name(env_dir.name + ".staging")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        log = self._log(paper_env_id, op)
        venv = staging / "venv"
        r = run_logged([self._uv(), "venv", "--python", spec.python_version, str(venv), "--color", "never"],
                       timeout=self.settings.venv_timeout_s, log_path=staging / "venv.log",
                       scrubber=self.bus.scrubber)
        if r.returncode != 0:
            shutil.rmtree(staging, ignore_errors=True)
            res = EnvOpResult(op=op, status="failed", timed_out=r.timed_out,
                              error=f"venv creation failed (python {spec.python_version})",
                              conflict_details=r.output[-4000:])
            return self._emit_failed(paper_env_id, op, res)
        reqs = list(self.settings.kernel_packages) + self._effective_requirements(spec)
        if reqs:
            r = run_logged([self._uv(), "pip", "install", "--python", str(venv / "bin" / "python"),
                            "--no-progress", "--color", "never", *reqs],
                           timeout=self.settings.install_timeout_s, log_path=staging / "install.log",
                           scrubber=self.bus.scrubber)
            if r.returncode != 0:
                pkg, conflict = parse_failure(r.output)
                keep = self._log(paper_env_id, op)
                keep.parent.mkdir(parents=True, exist_ok=True)
                keep.write_text(r.output)
                shutil.rmtree(staging, ignore_errors=True)
                res = EnvOpResult(op=op, status="failed", failing_package=pkg, conflict_details=conflict,
                                  timed_out=r.timed_out, log_path=str(keep),
                                  error="install timed out" if r.timed_out else "install failed")
                return self._emit_failed(paper_env_id, op, res)
        # swap in (keep logs dir from the old env)
        old_logs = env_dir / "logs"
        backup = env_dir.with_name(env_dir.name + ".old")
        shutil.rmtree(backup, ignore_errors=True)
        if env_dir.exists():
            env_dir.rename(backup)
        staging.rename(env_dir)
        if (backup / "logs").exists():
            shutil.copytree(backup / "logs", old_logs, dirs_exist_ok=True)
        shutil.rmtree(backup, ignore_errors=True)
        log.parent.mkdir(parents=True, exist_ok=True)
        for part in ("venv", "install"):  # keep staging logs next to the other env logs
            src = env_dir / f"{part}.log"
            if src.exists():
                shutil.move(str(src), str(log.with_name(f"{log.stem}-{part}.log")))
        frozen = self._freeze(paper_env_id)
        if frozen is None:
            return self._emit_failed(paper_env_id, op, EnvOpResult(
                op=op, status="failed", error="could not capture lockfile / interpreter version"))
        h, _ = frozen
        spec.lockfile_hash = h
        self._save_spec(spec)
        return self._finish_ok(paper_env_id, op, before, h, str(log.with_name(f"{log.stem}-install.log")),
                               {"requirements": reqs})

    def _install(self, op: str, paper_env_id: str, name: str, version: str | None) -> EnvOpResult:
        spec = self._load(paper_env_id)
        before = spec.lockfile_hash
        py = self.settings.venv_python(paper_env_id)
        if not py.exists():
            return self._emit_failed(paper_env_id, op, EnvOpResult(
                op=op, status="failed", error="environment has not been created yet (call create_env)"))
        req = f"{name}=={version}" if version else name
        # Resolve the NEW spec as a whole: `uv pip install <one pkg>` alone would happily break
        # constraints of already-installed packages. A conflict fails before the env is touched.
        key = canonicalize_name(name)
        new_spec = spec.model_copy(deep=True)
        new_spec.requirements = [x for x in spec.requirements if _req_name(x) != key] + [req]
        if op == "pin" and version:
            new_spec.pins = {k: v for k, v in spec.pins.items() if canonicalize_name(k) != key}
            new_spec.pins[name] = version
        log = self._log(paper_env_id, op)
        reqs = list(self.settings.kernel_packages) + self._effective_requirements(new_spec)
        r = run_logged(self._install_args(paper_env_id, reqs), timeout=self.settings.install_timeout_s,
                       log_path=log, scrubber=self.bus.scrubber)
        if r.returncode != 0:
            pkg, conflict = parse_failure(r.output)
            res = EnvOpResult(op=op, status="failed", failing_package=pkg or name,
                              conflict_details=conflict, timed_out=r.timed_out, log_path=str(log),
                              error="install timed out" if r.timed_out else "install failed")
            return self._emit_failed(paper_env_id, op, res)
        spec = new_spec
        frozen = self._freeze(paper_env_id)
        if frozen is None:
            return self._emit_failed(paper_env_id, op, EnvOpResult(
                op=op, status="failed", error="installed, but could not capture lockfile"))
        spec.lockfile_hash = frozen[0]
        self._save_spec(spec)
        return self._finish_ok(paper_env_id, op, before, frozen[0], str(log), {"package": name, "version": version})
