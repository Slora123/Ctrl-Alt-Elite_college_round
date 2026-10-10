"""Runtime profiles (the Colab "Change runtime type" menu) mapped onto compute backends.

Only the local backend exists; profiles that need other adapters are listed as unavailable with the reason, so the UI
can show them and the API refuses them explicitly instead of silently running somewhere else.
"""
from __future__ import annotations

import platform
from dataclasses import asdict, dataclass
from typing import Any

from .base import ComputeBackend


class RuntimeUnavailable(RuntimeError):
    pass


@dataclass
class RuntimeProfile:
    id: str
    label: str
    device: str  # cpu | gpu | tpu
    backend_id: str
    cost_per_hr: float = 0.0
    available: bool = True
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_local_gpu() -> tuple[bool, str]:
    """(available, description). NVIDIA via nvidia-smi, or Apple-silicon GPU (usable through torch/MPS)."""
    from ..env.hardware import collect_hardware_info

    hw = collect_hardware_info()
    if hw.get("gpus"):
        return True, ", ".join(g["name"] for g in hw["gpus"])
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return True, "Apple silicon GPU (via Metal/MPS in frameworks that support it)"
    return False, "no GPU detected on this machine"


class BackendRegistry:
    def __init__(self, default_id: str = "local-cpu"):
        self.default_id = default_id
        self._profiles: dict[str, RuntimeProfile] = {}
        self._backends: dict[str, ComputeBackend | None] = {}

    def register(self, profile: RuntimeProfile, backend: ComputeBackend | None) -> None:
        self._profiles[profile.id] = profile
        self._backends[profile.id] = backend if profile.available else None

    def profiles(self) -> list[RuntimeProfile]:
        return list(self._profiles.values())

    def profile(self, profile_id: str) -> RuntimeProfile:
        try:
            return self._profiles[profile_id]
        except KeyError:
            raise RuntimeUnavailable(f"unknown runtime {profile_id!r}") from None

    def backend_for(self, profile_id: str) -> ComputeBackend:
        p = self.profile(profile_id)
        b = self._backends.get(profile_id)
        if not p.available or b is None:
            raise RuntimeUnavailable(f"runtime {p.label!r} is not available: {p.reason or 'no backend'}")
        return b

    @property
    def default(self) -> ComputeBackend:
        return self.backend_for(self.default_id)

    def all_backends(self) -> list[ComputeBackend]:
        seen: list[ComputeBackend] = []
        for b in self._backends.values():
            if b is not None and not any(b is s for s in seen):
                seen.append(b)
        return seen


def as_registry(backend_or_registry: Any) -> BackendRegistry:
    """Accept a bare ComputeBackend (tests, simple setups) or a ready registry."""
    if isinstance(backend_or_registry, BackendRegistry):
        return backend_or_registry
    reg = BackendRegistry("local-cpu")
    reg.register(RuntimeProfile("local-cpu", "CPU (this machine)", "cpu", "local"), backend_or_registry)
    return reg


def default_registry(local: ComputeBackend) -> BackendRegistry:
    gpu_ok, gpu_desc = detect_local_gpu()
    reg = BackendRegistry("local-cpu")
    reg.register(RuntimeProfile("local-cpu", "CPU (this machine)", "cpu", "local"), local)
    reg.register(RuntimeProfile("local-gpu", "GPU (this machine)", "gpu", "local", available=gpu_ok,
                                reason=gpu_desc if not gpu_ok else ""), local)
    reg.register(RuntimeProfile("tpu", "TPU", "tpu", "tpu", available=False,
                                reason="no TPU backend is implemented"), None)
    reg.register(RuntimeProfile("aws-gpu", "GPU (AWS)", "gpu", "aws", cost_per_hr=1.0, available=False,
                                reason="the AWS adapter is not implemented yet"), None)
    return reg
