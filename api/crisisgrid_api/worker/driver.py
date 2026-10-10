"""Clean-run driver. Runs in a FRESH python process: seeds the RNGs, executes the notebook's code cells top to bottom in one
shared namespace and writes result.json. Self-contained on purpose (it may run in a different interpreter than the worker).

    python driver.py spec.json result.json

Contract for cells: call `report_metric(name, value)` to publish a number. `get_ipython`-style magics (`%x`, `!x`) are not
supported; package installs inside a cell are refused (they belong to the Env manager).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import re
import sys
import time
import traceback

MAGIC = re.compile(r"^\s*[%!]")
INSTALL = re.compile(r"\b(pip3?|uv|conda|mamba|apt(?:-get)?)\b[^\n]*\binstall\b")


def seed_everything(seed: int) -> dict:
    random.seed(seed)
    seeded = {"python": True}
    try:
        import numpy as np  # type: ignore

        np.random.seed(seed)
        seeded["numpy"] = True
    except Exception:  # noqa: BLE001
        seeded["numpy"] = False
    try:
        import torch  # type: ignore

        torch.manual_seed(seed)
        seeded["torch"] = True
    except Exception:  # noqa: BLE001
        seeded["torch"] = False
    return seeded


def lockfile_digest() -> str:
    try:
        from importlib import metadata

        pkgs = sorted(f"{d.metadata['Name'].lower()}=={d.version}" for d in metadata.distributions() if d.metadata["Name"])
        return hashlib.sha256("\n".join(pkgs).encode()).hexdigest()
    except Exception:  # noqa: BLE001
        return ""


def main(spec_path: str, result_path: str) -> int:
    spec = json.load(open(spec_path, encoding="utf-8"))
    seed = int(spec.get("seed", 0))
    metrics: dict[str, float] = {}
    result: dict = {"status": "ok", "metrics": metrics, "error": None, "seeded": seed_everything(seed), "cells_run": 0,
                    "hardware_info": {"python": platform.python_version(), "platform": platform.platform(),
                                      "cpu_count": os.cpu_count()},
                    "lockfile_digest": lockfile_digest(), "seed": seed}

    def report_metric(name: str, value) -> None:
        metrics[str(name)] = float(value)

    ns: dict = {"__name__": "__main__", "report_metric": report_metric, "SEED": seed}
    started = time.time()
    for i, cell in enumerate(spec["cells"]):
        if cell.get("kind", "code") != "code":
            continue
        src = cell["source"]
        bad = next((ln for ln in src.splitlines() if MAGIC.match(ln) or INSTALL.search(ln)), None)
        if bad is not None:
            msg = f"cell {i} uses a magic or installs packages ({bad.strip()[:80]!r}); environment changes go through the Env manager"
            result.update(status="failed", error={"type": "UnsupportedCell", "cell_index": i, "message": msg})
            break
        try:
            exec(compile(src, f"<cell {i}>", "exec"), ns)  # noqa: S102  (this IS the sandboxed worker's job)
            result["cells_run"] = i + 1
        except BaseException as e:  # noqa: BLE001  (SystemExit / KeyboardInterrupt in user code are failures too)
            tb = traceback.format_exc()
            result.update(status="failed", error={"type": type(e).__name__, "message": str(e)[:2000], "cell_index": i,
                                                  "traceback": tb[-4000:]})
            break
    result["duration_s"] = round(time.time() - started, 3)
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
