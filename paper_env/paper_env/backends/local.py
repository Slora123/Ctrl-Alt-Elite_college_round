"""Local compute backend: one ipykernel process per runtime, using the PaperEnv's own interpreter."""
from __future__ import annotations

import base64
import json
import logging
import os
import queue
import shutil
import signal
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from jupyter_client import KernelManager
from jupyter_client.kernelspec import KernelSpec, KernelSpecManager

from ..config import Settings
from ..domain import EnvSpec
from ..env.hardware import collect_hardware_info
from ..security import Scrubber
from .base import (
    BackendError, CheckpointRef, CommEvent, ExecDisplay, ExecError, ExecEvent, ExecFinished, ExecInfraFailure,
    ExecInputRequest, ExecOutput, RuntimeHandle,
)

log = logging.getLogger(__name__)
RT_SRC = Path(__file__).resolve().parent.parent / "kernel" / "runtime" / "_paper_env_rt.py"
INTERNAL_MIME = "application/vnd.paperenv.internal+json"


def classify_exit(rc: int | None) -> tuple[str, str]:
    """(kind, message) for a kernel process exit code. SIGKILL is how the OOM killer ends a process,
    but we cannot prove that, so the wording says 'possible'."""
    if rc is None:
        return "kernel_died", "kernel stopped responding"
    if rc == -signal.SIGKILL:
        return "oom_or_killed", "kernel was killed (SIGKILL) - possibly out of memory"
    if rc == -signal.SIGSEGV:
        return "segfault", "kernel crashed (SIGSEGV) - a native extension likely faulted"
    if rc < 0:
        return "kernel_died", f"kernel terminated by signal {-rc}"
    return "kernel_died", f"kernel process exited with code {rc}"


@dataclass
class _Runtime:
    km: KernelManager
    kc: Any
    proc: Any  # Popen of the kernel process
    pid: int
    workdir: Path
    lock: threading.Lock = field(default_factory=threading.Lock)  # one execution at a time
    interrupt_requested: bool = False
    dead: bool = False
    registry_file: Path | None = None
    waiters: dict[str, "queue.Queue"] = field(default_factory=dict)  # msg_id -> iopub messages for that execution
    comm_cbs: list = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)
    shell_lock: threading.Lock = field(default_factory=threading.Lock)
    reader: threading.Thread | None = None


class LocalBackend:
    name = "local"

    def __init__(self, settings: Settings, scrubber: Scrubber | None = None):
        self.settings = settings
        self.scrubber = scrubber or Scrubber()
        self._rts: dict[str, _Runtime] = {}
        self._guard = threading.Lock()
        settings.kernels_dir().mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ lifecycle
    def provision(self, env_spec: EnvSpec, *, paper_env_id: str, seed: int, workdir: Path | None = None,
                  extra_env: dict[str, str] | None = None, startup_code: str = "", **_: Any) -> RuntimeHandle:
        python = self.settings.venv_python(paper_env_id)
        if not python.exists():
            raise BackendError(f"environment for {paper_env_id} has not been created (call create_env first)")
        hid = uuid.uuid4().hex[:12]
        workdir = Path(workdir) if workdir else self.settings.kernels_dir() / hid / "work"
        rt_dir = self.settings.kernels_dir() / hid / "rt"
        workdir.mkdir(parents=True, exist_ok=True)
        rt_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(RT_SRC, rt_dir / RT_SRC.name)

        argv = [str(python), "-m", "ipykernel_launcher", "-f", "{connection_file}"]

        class _Spec(KernelSpecManager):
            def get_kernel_spec(self, name):
                return KernelSpec(argv=argv, display_name="paper-env", language="python")

        env = self.scrubber.subprocess_env()
        env.update({"PYTHONHASHSEED": str(seed), "PYTHONUNBUFFERED": "1", "MPLBACKEND": "Agg",
                    "PAPER_ENV_RT_DIR": str(rt_dir), "PAPER_ENV_WORKDIR": str(workdir)})
        env.update(extra_env or {})
        km = KernelManager(kernel_spec_manager=_Spec(), kernel_name="paperenv")
        km.connection_file = str(rt_dir.parent / "connection.json")
        try:
            km.start_kernel(env=env, cwd=str(workdir))
            kc = km.client()
            kc.start_channels()
            kc.wait_for_ready(timeout=self.settings.kernel_start_timeout_s)
        except Exception as e:
            self._force_kill(km)
            raise BackendError(f"kernel failed to start: {type(e).__name__}: {self.scrubber.scrub(str(e))}"
                               " (is ipykernel installed in the environment?)") from e
        proc = km.provisioner.process
        rt = _Runtime(km=km, kc=kc, proc=proc, pid=proc.pid, workdir=workdir)
        handle = RuntimeHandle(id=hid, backend=self.name, paper_env_id=paper_env_id, workdir=str(workdir),
                               info={"pid": proc.pid, "python": str(python), "rt_dir": str(rt_dir), "seed": seed})
        reg = self.settings.kernels_dir() / f"{hid}.json"
        reg.write_text(json.dumps({"id": hid, "pid": proc.pid, "owner_pid": os.getpid(),
                                   "paper_env_id": paper_env_id, "started": time.time()}))
        rt.registry_file = reg
        rt.reader = threading.Thread(target=self._reader, args=(rt,), daemon=True, name=f"iopub-{hid}")
        rt.reader.start()
        with self._guard:
            self._rts[hid] = rt
        boot = (f"import sys; sys.path.insert(0, {str(rt_dir)!r}); import _paper_env_rt as _pe; "
                f"_pe.setup({seed}); {startup_code}")
        ready = None
        for ev in self.execute(handle, boot, timeout=self.settings.kernel_start_timeout_s, internal=True):
            if isinstance(ev, ExecDisplay) and INTERNAL_MIME in ev.data and ev.data[INTERNAL_MIME].get("kind") == "ready":
                ready = ev.data[INTERNAL_MIME]["report"]
            elif isinstance(ev, (ExecError, ExecInfraFailure)):
                self.teardown(handle)
                detail = ev.message if isinstance(ev, ExecInfraFailure) else f"{ev.ename}: {ev.evalue}"
                raise BackendError(f"kernel bootstrap failed: {self.scrubber.scrub(detail)}")
        if ready is None:
            self.teardown(handle)
            raise BackendError("kernel bootstrap produced no ready report")
        handle.info["seeded"] = ready
        return handle

    def teardown(self, handle: RuntimeHandle) -> None:
        with self._guard:
            rt = self._rts.pop(handle.id, None)
        if rt is not None:
            rt.stop.set()
            try:
                rt.kc.stop_channels()
            except Exception:
                pass
            self._force_kill(rt.km, rt.proc)
            if rt.registry_file:
                rt.registry_file.unlink(missing_ok=True)
        shutil.rmtree(self.settings.kernels_dir() / handle.id, ignore_errors=True)

    def shutdown_all(self) -> None:
        for hid in list(self._rts):
            self.teardown(RuntimeHandle(id=hid, backend=self.name, paper_env_id="", workdir=""))

    @staticmethod
    def _force_kill(km: KernelManager, proc: Any = None) -> None:
        try:
            km.shutdown_kernel(now=False, restart=False)  # polite: shutdown_request, then SIGTERM/KILL
        except Exception:
            pass
        proc = proc or getattr(getattr(km, "provisioner", None), "process", None)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except Exception:
                    pass

    # ------------------------------------------------------------------ orphans
    def cleanup_orphans(self) -> list[str]:
        """Kill kernels left behind by a crashed previous server. Called at startup."""
        killed = []
        for reg in self.settings.kernels_dir().glob("*.json"):
            try:
                info = json.loads(reg.read_text())
            except (OSError, ValueError):
                reg.unlink(missing_ok=True)
                continue
            owner, pid = info.get("owner_pid"), info.get("pid")
            if owner and _pid_alive(owner) and owner != os.getpid():
                continue  # belongs to another live server
            if owner == os.getpid() and info["id"] in self._rts:
                continue
            if pid and _pid_alive(pid) and _looks_like_kernel(pid):
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                killed.append(info["id"])
            reg.unlink(missing_ok=True)
            shutil.rmtree(self.settings.kernels_dir() / info["id"], ignore_errors=True)
        return killed

    # ------------------------------------------------------------------ iopub reader
    def _reader(self, rt: _Runtime) -> None:
        """The single consumer of the iopub channel. Execution messages go to the waiting execute() call;
        comm traffic (ipywidgets) always goes to the comm listeners, in arrival order, even when no cell runs."""
        while not rt.stop.is_set():
            try:
                msg = rt.kc.get_iopub_msg(timeout=0.2)
            except queue.Empty:
                continue
            except Exception:
                break
            mt = msg["msg_type"]
            if mt in ("comm_open", "comm_msg", "comm_close"):
                ev = CommEvent(mt, msg["content"], [base64.b64encode(bytes(b)).decode() for b in msg.get("buffers", [])],
                               msg.get("metadata", {}))
                for cb in list(rt.comm_cbs):
                    try:
                        cb(ev)
                    except Exception:
                        log.exception("comm listener failed")
                continue
            q = rt.waiters.get(msg["parent_header"].get("msg_id"))
            if q is not None:
                q.put(msg)

    def on_comm(self, handle: RuntimeHandle, callback: Any) -> None:
        rt = self._rts.get(handle.id)
        if rt is not None:
            rt.comm_cbs.append(callback)

    def comm_send(self, handle: RuntimeHandle, msg_type: str, content: dict[str, Any],
                  buffers: list[str] | None = None) -> None:
        rt = self._rts.get(handle.id)
        if rt is None or rt.dead:
            raise BackendError("runtime is not available")
        if msg_type not in ("comm_open", "comm_msg", "comm_close"):
            raise BackendError(f"unsupported comm message type {msg_type!r}")
        bufs = [base64.b64decode(b) for b in (buffers or [])]
        rt.kc.session.send(rt.kc.shell_channel.socket, msg_type, content, buffers=bufs)

    def provide_input(self, handle: RuntimeHandle, text: str) -> None:
        rt = self._rts.get(handle.id)
        if rt is None or rt.dead:
            raise BackendError("runtime is not available")
        rt.kc.input(text)

    def _shell_request(self, handle: RuntimeHandle, send, timeout: float = 3.0) -> dict[str, Any]:
        rt = self._rts.get(handle.id)
        if rt is None or rt.dead:
            raise BackendError("runtime is not available")
        with rt.shell_lock:
            msg_id = send(rt.kc)
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                try:
                    reply = rt.kc.get_shell_msg(timeout=0.2)
                except queue.Empty:
                    continue
                if reply["parent_header"].get("msg_id") == msg_id:
                    return reply["content"]
        return {"status": "timeout"}  # e.g. the kernel is busy running a cell

    def complete(self, handle: RuntimeHandle, code: str, cursor_pos: int) -> dict[str, Any]:
        c = self._shell_request(handle, lambda kc: kc.complete(code, cursor_pos))
        return {"matches": c.get("matches", []), "cursor_start": c.get("cursor_start", cursor_pos),
                "cursor_end": c.get("cursor_end", cursor_pos), "status": c.get("status", "ok"),
                "metadata": c.get("metadata", {})}

    def inspect(self, handle: RuntimeHandle, code: str, cursor_pos: int) -> dict[str, Any]:
        c = self._shell_request(handle, lambda kc: kc.inspect(code, cursor_pos, detail_level=0))
        return {"found": bool(c.get("found")), "data": c.get("data", {}), "status": c.get("status", "ok")}

    # ------------------------------------------------------------------ execution
    def execute(self, handle: RuntimeHandle, cell_code: str, timeout: float, *, internal: bool = False,
                **_: Any) -> Iterator[ExecEvent]:
        rt = self._rts.get(handle.id)
        if rt is None or rt.dead:
            yield ExecInfraFailure("backend_error", "runtime is not available (dead or torn down); "
                                   "in-memory state is lost - restart the kernel")
            return
        with rt.lock:
            yield from self._execute_locked(rt, cell_code, timeout, internal)

    def _dead_event(self, rt: _Runtime) -> ExecInfraFailure:
        rt.dead = True
        rc = rt.proc.poll()
        kind, msg = classify_exit(rc)
        return ExecInfraFailure(kind, msg, rc)

    def _execute_locked(self, rt: _Runtime, code: str, timeout: float, internal: bool) -> Iterator[ExecEvent]:
        rt.interrupt_requested = False
        try:
            msg_id = rt.kc.execute(code, store_history=not internal, allow_stdin=not internal, stop_on_error=False)
        except Exception as e:
            yield ExecInfraFailure("backend_error", f"could not submit code: {type(e).__name__}")
            return
        q: "queue.Queue" = queue.Queue()
        rt.waiters[msg_id] = q
        try:
            yield from self._consume(rt, msg_id, q, timeout)
        finally:
            rt.waiters.pop(msg_id, None)

    def _consume(self, rt: _Runtime, msg_id: str, q: "queue.Queue", timeout: float) -> Iterator[ExecEvent]:
        deadline = time.monotonic() + timeout
        timed_out = False
        saw_error = False
        while True:
            now = time.monotonic()
            if not timed_out and now >= deadline:
                timed_out = True
                self.interrupt_rt(rt)
                deadline = now + self.settings.interrupt_grace_s
            elif timed_out and now >= deadline:
                # kernel ignored the interrupt: kill it. State is gone and we say so.
                self._force_kill(rt.km, rt.proc)
                rt.dead = True
                yield ExecInfraFailure("killed_after_timeout", (
                    f"cell exceeded its {timeout:g}s timeout and the kernel did not respond to an interrupt; "
                    "it was killed and in-memory state is lost"), rt.proc.poll())
                return
            try:
                sin = rt.kc.get_stdin_msg(timeout=0)
                if sin["msg_type"] == "input_request":
                    yield ExecInputRequest(sin["content"].get("prompt", ""), bool(sin["content"].get("password")))
            except queue.Empty:
                pass
            try:
                msg = q.get(timeout=0.2)
            except queue.Empty:
                if rt.proc.poll() is not None:
                    yield self._dead_event(rt)
                    return
                continue
            mt, c = msg["msg_type"], msg["content"]
            if mt == "stream":
                yield ExecOutput(c["name"], c["text"])
            elif mt in ("display_data", "execute_result"):
                yield ExecDisplay(c["data"], c.get("metadata", {}), is_result=(mt == "execute_result"))
            elif mt == "error":
                saw_error = True
                if not (timed_out or rt.interrupt_requested) or c["ename"] != "KeyboardInterrupt":
                    yield ExecError(c["ename"], c["evalue"], c["traceback"])
            elif mt == "status" and c["execution_state"] == "idle":
                break
        if rt.proc.poll() is not None:
            yield self._dead_event(rt)
            return
        if timed_out:
            yield ExecFinished("timeout")
        elif rt.interrupt_requested and saw_error:
            yield ExecFinished("interrupted")
        else:
            yield ExecFinished("error" if saw_error else "ok")

    def interrupt(self, handle: RuntimeHandle) -> None:
        rt = self._rts.get(handle.id)
        if rt is not None and not rt.dead:
            rt.interrupt_requested = True
            self.interrupt_rt(rt)

    @staticmethod
    def interrupt_rt(rt: _Runtime) -> None:
        try:
            rt.km.interrupt_kernel()
        except Exception as e:  # kernel may already be gone
            log.warning("interrupt failed: %s", type(e).__name__)

    # ------------------------------------------------------------------ misc
    def is_alive(self, handle: RuntimeHandle) -> bool:
        rt = self._rts.get(handle.id)
        return bool(rt and not rt.dead and rt.proc.poll() is None)

    def _internal(self, handle: RuntimeHandle, code: str, kind: str, timeout: float = 600.0) -> dict[str, Any]:
        for ev in self.execute(handle, code, timeout, internal=True):
            if isinstance(ev, ExecDisplay) and INTERNAL_MIME in ev.data and ev.data[INTERNAL_MIME].get("kind") == kind:
                return ev.data[INTERNAL_MIME]
            if isinstance(ev, ExecError):
                raise BackendError(f"{kind} failed in the kernel: {ev.ename}: {self.scrubber.scrub(ev.evalue)}")
            if isinstance(ev, ExecInfraFailure):
                raise BackendError(f"{kind} failed: {ev.message}")
        raise BackendError(f"{kind} produced no result")

    def snapshot(self, handle: RuntimeHandle, *, dest: Path | None = None, names: list[str] | None = None,
                 **_: Any) -> CheckpointRef:
        """Serialize ``names`` and RNG state to ``dest``. Serialization runs inside the kernel; this process
        only receives the JSON report. Variables that cannot be serialized are reported, never skipped silently."""
        if dest is None:
            raise BackendError("snapshot needs a destination directory")
        dest = Path(dest)
        code = f"_pe.checkpoint_save({str(dest)!r}, __import__('json').loads({json.dumps(list(names or []))!r}))"
        rep = self._internal(handle, code, "checkpoint_saved")
        notes = list(rep.get("notes", []))
        if rep.get("rng_error"):
            notes.append(f"RNG state not saved: {rep['rng_error']}")
        ref = CheckpointRef(id=dest.name, path=str(dest), saved=[x["name"] for x in rep["saved"]],
                            unserializable=rep["unserializable"], rng_saved=bool(rep["rng_saved"]))
        ref.notes = notes  # type: ignore[attr-defined]
        return ref

    def restore(self, handle: RuntimeHandle, ref: CheckpointRef) -> dict[str, Any]:
        code = f"_pe.checkpoint_load({ref.path!r})"
        return self._internal(handle, code, "checkpoint_loaded")

    def capabilities(self) -> dict[str, Any]:
        hw = collect_hardware_info()
        return {"gpu": bool(hw.get("gpus")), "network": True,
                "max_runtime": self.settings.run_timeout_s, "cost_per_hr": 0.0, "backend": self.name}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _looks_like_kernel(pid: int) -> bool:
    """Guard against pid reuse: only kill processes whose command line is an ipykernel."""
    import subprocess
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "ipykernel_launcher" in out
