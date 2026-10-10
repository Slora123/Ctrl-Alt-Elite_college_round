"""Compute backend interface. The runner talks to kernels ONLY through this Protocol, so AWS /
BYO-Jupyter adapters can be added later without touching runner code."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol, Union

from ..domain import EnvSpec


class BackendError(RuntimeError):
    """Provisioning / transport problem. Always an *infrastructure* failure."""


@dataclass
class RuntimeHandle:
    id: str
    backend: str
    paper_env_id: str
    workdir: str
    info: dict[str, Any] = field(default_factory=dict)  # backend-specific facts (pid, seeded libs, ...)


@dataclass
class CheckpointRef:
    id: str
    path: str  # backend-local location of the checkpoint payload
    saved: list[str] = field(default_factory=list)
    unserializable: list[dict[str, str]] = field(default_factory=list)  # [{name, reason}]
    rng_saved: bool = False


# ---- execute() stream ---------------------------------------------------------------
@dataclass
class ExecOutput:
    stream: str  # stdout | stderr
    text: str


@dataclass
class ExecDisplay:
    data: dict[str, Any]  # mime bundle
    metadata: dict[str, Any] = field(default_factory=dict)
    is_result: bool = False  # execute_result vs display_data


@dataclass
class ExecError:
    """An exception raised by the cell's code: an EXPERIMENT failure."""
    ename: str
    evalue: str
    traceback: list[str]


@dataclass
class ExecFinished:
    status: str  # ok | error | timeout | interrupted
    execution_count: int | None = None


@dataclass
class ExecInfraFailure:
    """The runtime itself failed (kernel death, OOM kill, transport): an INFRASTRUCTURE failure.
    In-memory state is gone; callers must not assume it survived."""
    kind: str  # kernel_died | oom_or_killed | segfault | killed_after_timeout | backend_error
    message: str
    exit_code: int | None = None


ExecEvent = Union[ExecOutput, ExecDisplay, ExecError, ExecFinished, ExecInfraFailure]


class ComputeBackend(Protocol):
    def provision(self, env_spec: EnvSpec, **opts: Any) -> RuntimeHandle: ...
    def execute(self, handle: RuntimeHandle, cell_code: str, timeout: float, **opts: Any) -> Iterator[ExecEvent]: ...
    def interrupt(self, handle: RuntimeHandle) -> None: ...
    def snapshot(self, handle: RuntimeHandle) -> CheckpointRef: ...
    def teardown(self, handle: RuntimeHandle) -> None: ...
    def capabilities(self) -> dict[str, Any]: ...  # gpu, network, max_runtime, cost_per_hr
