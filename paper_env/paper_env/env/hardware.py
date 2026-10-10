"""Facts about the machine/interpreter that can affect results. Missing info is reported
explicitly (None / "not_detected"), never assumed."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

_PROBE = (
    "import json,sys,platform;"
    "print(json.dumps({'python_version':platform.python_version(),"
    "'python_implementation':platform.python_implementation(),'executable':sys.executable}))"
)


def _nvidia() -> dict[str, Any]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"cuda": "not_detected", "gpus": [], "driver_version": None}
    try:
        q = subprocess.run([exe, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=10)
        gpus, driver = [], None
        for line in q.stdout.strip().splitlines():
            name, drv, mem = [p.strip() for p in line.split(",")]
            gpus.append({"name": name, "memory": mem})
            driver = drv
        head = subprocess.run([exe], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"CUDA Version:\s*([\d.]+)", head)
        return {"cuda": m.group(1) if m else "unknown", "gpus": gpus, "driver_version": driver}
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"cuda": "probe_failed", "gpus": [], "driver_version": None, "error": str(e)}


def collect_hardware_info(python: Path | None = None) -> dict[str, Any]:
    """``python`` is the env interpreter whose version is recorded (not the host's)."""
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "system": platform.system(),
        "cpu_count": os.cpu_count(),
        "env_python": None,
    }
    if python is not None:
        try:
            out = subprocess.run([str(python), "-I", "-c", _PROBE], capture_output=True, text=True, timeout=30)
            info["env_python"] = json.loads(out.stdout) if out.returncode == 0 else {"error": out.stderr[-500:]}
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            info["env_python"] = {"error": str(e)}
    info.update(_nvidia())
    return info
