"""Kernel-side runtime. Runs INSIDE the isolated env's interpreter; stdlib only (numpy/torch/pandas
are used only if the experiment already installed them).

Everything here is invoked by the host through *internal* executions and reports back through
structured display data (never by eval-ing anything the host received).
"""
import hashlib
import json
import random
import re
import sys

INTERNAL_MIME = "application/vnd.paperenv.internal+json"
_ADDR = re.compile(r" at 0x[0-9a-fA-F]+")
_MAX_HASH_BYTES = 64 * 1024 * 1024


def _emit(kind, payload):
    from IPython.display import publish_display_data
    publish_display_data({INTERNAL_MIME: {"kind": kind, **payload}})


def _user_ns():
    import IPython
    return IPython.get_ipython().user_ns


# --------------------------------------------------------------------------- datasets helper
class DatasetUnavailable(LookupError):
    pass


class _Datasets:
    """``datasets.resolve("cifar10@v1") -> Path`` (read-only). Reads a manifest snapshot written by the host
    for this run; it never fetches, never touches the network and never sees credentials, which keeps cell
    code portable across compute backends (a remote backend would provide the same snapshot)."""

    def _load(self):
        import os
        path = os.environ.get("PAPER_ENV_DATASETS_MANIFEST")
        if not path or not os.path.exists(path):
            raise DatasetUnavailable("no dataset manifest is available in this runtime")
        with open(path) as f:
            return json.load(f)

    def list(self):
        return sorted(self._load().get("datasets", {}))

    def info(self, dataset_id):
        entries = self._load().get("datasets", {})
        if dataset_id not in entries:
            raise DatasetUnavailable(
                f"dataset {dataset_id!r} is not available to this run (declared: {sorted(entries)}). "
                "Datasets must be registered and declared by the paper environment before running.")
        return dict(entries[dataset_id])

    def resolve(self, dataset_id):
        from pathlib import Path
        p = Path(self.info(dataset_id)["path"])
        if not p.exists():
            raise DatasetUnavailable(f"dataset {dataset_id!r} is listed but its files are missing at {p}")
        return p

    def derive(self, name, version, parent=None):
        """Directory for preprocessed data. Write into it, finish the cell, and the host registers it as a
        derived dataset linked to ``parent`` and to this cell's version."""
        import os
        from pathlib import Path
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or not re.fullmatch(r"[A-Za-z0-9._-]+", version):
            raise ValueError("name and version may only contain letters, digits, '.', '_' and '-'")
        run = self._load().get("run") or {}
        if not run.get("derived_dir"):
            raise DatasetUnavailable("no run directory is available for derived data")
        out = Path(run["derived_dir"]) / f"{name}@{version}"
        if out.exists():
            raise FileExistsError(f"{out.name} already exists; derived datasets are immutable, use a new version")
        out.mkdir(parents=True)
        with open(Path(run["derived_dir"]).parent / "derived_requests.jsonl", "a") as f:
            f.write(json.dumps({"name": name, "version": version, "parent": parent}) + "\n")
        return out

    def __repr__(self):
        try:
            return f"<datasets: {', '.join(self.list()) or 'none declared'}>"
        except Exception as e:
            return f"<datasets unavailable: {e}>"


datasets = _Datasets()


# --------------------------------------------------------------------------- seeding
def seed_all(seed):
    """Seed python, numpy and torch (if installed). Returns what was and wasn't seeded, explicitly."""
    import importlib.util
    report = {"seed": seed, "python": True, "numpy": None, "torch": None}
    random.seed(seed)
    if importlib.util.find_spec("numpy") is not None:
        import numpy as np
        np.random.seed(seed % (2 ** 32))
        report["numpy"] = True
    else:
        report["numpy"] = "not_installed"
    if importlib.util.find_spec("torch") is not None:
        import torch
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        report["torch"] = True
    else:
        report["torch"] = "not_installed"
    return report


def setup(seed, extra=None):
    _user_ns()["datasets"] = datasets
    report = seed_all(seed)
    report["python_version"] = sys.version.split()[0]
    report["hashseed_env"] = __import__("os").environ.get("PYTHONHASHSEED")
    for fn in (extra or []):
        fn()
    _emit("ready", {"report": report})


# --------------------------------------------------------------------------- state hashing
def _digest_bytes(b):
    return hashlib.sha256(b).hexdigest()


def _summarize(name, obj):
    """Deterministic (kind, digest, weak) for one value. ``weak`` marks summaries that fall back
    to a repr we could not make fully address-independent."""
    t = type(obj)
    mod = t.__module__
    qual = f"{mod}.{t.__qualname__}"
    try:
        if mod.startswith("numpy") and hasattr(obj, "tobytes") and hasattr(obj, "dtype"):
            data = obj.tobytes()[:_MAX_HASH_BYTES]
            return "ndarray", _digest_bytes(repr((obj.shape, str(obj.dtype))).encode() + data), False
        if mod.startswith("torch") and hasattr(obj, "detach"):
            arr = obj.detach().cpu().numpy()
            data = arr.tobytes()[:_MAX_HASH_BYTES]
            return "tensor", _digest_bytes(repr((arr.shape, str(arr.dtype))).encode() + data), False
        if mod.startswith("pandas") and hasattr(obj, "shape") and hasattr(obj, "columns"):
            import pandas as pd
            h = pd.util.hash_pandas_object(obj, index=True).values.tobytes()
            return "dataframe", _digest_bytes(repr((obj.shape, list(map(str, obj.columns)))).encode() + h), False
        if isinstance(obj, (int, float, complex, bool, str, bytes, type(None))):
            return t.__name__, _digest_bytes(repr(obj).encode("utf-8", "replace")), False
        if isinstance(obj, (list, tuple, dict, set, frozenset)):
            r = repr(sorted(obj, key=repr) if isinstance(obj, (set, frozenset)) else obj)
            weak = bool(_ADDR.search(r))
            return t.__name__, _digest_bytes(_ADDR.sub("", r).encode("utf-8", "replace")), weak
        if callable(obj) and hasattr(obj, "__qualname__"):
            code = getattr(obj, "__code__", None)
            body = code.co_code if code is not None else b""
            return "callable", _digest_bytes(f"{qual}:{obj.__qualname__}".encode() + body), False
        r = repr(obj)
        return qual, _digest_bytes(_ADDR.sub("", r).encode("utf-8", "replace")), True
    except Exception as e:  # never let hashing break a run
        return "unhashable", _digest_bytes(f"{qual}:{type(e).__name__}".encode()), True


def _user_names(ns):
    skip = {"In", "Out", "get_ipython", "exit", "quit", "open", "datasets"}
    return sorted(k for k, v in ns.items()
                  if not k.startswith("_") and k not in skip
                  and not isinstance(v, type(sys)))  # modules are environment, not experiment state


def state_hash(declared_outputs):
    ns = _user_ns()
    details, weak = {}, []
    if declared_outputs:
        for n in sorted(declared_outputs):
            if n in ns:
                kind, d, w = _summarize(n, ns[n])
                details[n] = {"kind": kind, "digest": d}
                if w:
                    weak.append(n)
            else:
                details[n] = {"kind": "undefined", "digest": None}
        basis = "declared_outputs"
    else:
        for n in _user_names(ns):
            kind, d, w = _summarize(n, ns[n])
            details[n] = {"kind": kind, "digest": d}
            if w:
                weak.append(n)
        basis = "namespace_summary"
    h = hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()
    _emit("state_hash", {"state_hash": h, "basis": basis, "variables": details, "weak": weak})


# --------------------------------------------------------------------------- checkpoint / resume
# Serialization happens ONLY here, inside the kernel. The host never loads these files.
def _serializer():
    try:
        import dill
        return dill, "dill"
    except ImportError:
        import pickle
        return pickle, "pickle"


def _is_tensor_like(obj):
    t = type(obj)
    return t.__module__.startswith("torch") and hasattr(obj, "detach") and hasattr(obj, "cpu")


def _safetensors_ok(obj):
    import importlib.util
    if importlib.util.find_spec("safetensors") is None or importlib.util.find_spec("torch") is None:
        return False
    if _is_tensor_like(obj):
        return True
    return isinstance(obj, dict) and obj and all(isinstance(k, str) and _is_tensor_like(v) for k, v in obj.items())


def _rng_state():
    import importlib.util
    st = {"python": random.getstate()}
    if importlib.util.find_spec("numpy") is not None:
        import numpy as np
        st["numpy"] = np.random.get_state()
    if importlib.util.find_spec("torch") is not None:
        import torch
        st["torch"] = torch.get_rng_state()
        if torch.cuda.is_available():
            st["torch_cuda"] = torch.cuda.get_rng_state_all()
    return st


def _set_rng_state(st):
    restored = []
    if "python" in st:
        random.setstate(st["python"]); restored.append("python")
    if "numpy" in st:
        import numpy as np
        np.random.set_state(st["numpy"]); restored.append("numpy")
    if "torch" in st:
        import torch
        torch.set_rng_state(st["torch"]); restored.append("torch")
        if "torch_cuda" in st and torch.cuda.is_available():
            torch.cuda.set_rng_state_all(st["torch_cuda"]); restored.append("torch_cuda")
    return restored


def _import_bindings(ns, skip):
    """Names bound by ``import x [as y]`` / ``from m import f``: recorded by reference and restored by
    importing again (nothing is pickled). A binding is only recorded if re-resolving it yields the same object."""
    import importlib
    modules, objects = {}, {}
    for alias, val in list(ns.items()):
        if alias.startswith("_") or alias in skip or alias in ("In", "Out", "datasets", "get_ipython", "exit", "quit"):
            continue
        if isinstance(val, type(sys)):
            modules[alias] = val.__name__
        elif (isinstance(val, type) or callable(val)) and getattr(val, "__module__", None) not in (None, "__main__", "builtins") \
                and getattr(val, "__qualname__", None) and "<locals>" not in val.__qualname__:
            try:
                obj = importlib.import_module(val.__module__)
                for part in val.__qualname__.split("."):
                    obj = getattr(obj, part)
                if obj is val:
                    objects[alias] = [val.__module__, val.__qualname__]
            except Exception:
                pass
    return modules, objects


def checkpoint_save(dest, names):
    import os
    os.makedirs(dest, exist_ok=True)
    ns = _user_ns()
    ser, ser_name = _serializer()
    saved, failed, notes = [], [], []
    if ser_name != "dill":
        notes.append("dill is not installed in this environment; using pickle (locally defined functions/classes may not restore)")
    for n in names:
        if not n.isidentifier():
            failed.append({"name": n, "reason": "not a valid variable name"}); continue
        if n not in ns:
            failed.append({"name": n, "reason": "not defined in the kernel"}); continue
        obj, fname = ns[n], f"var_{n}"
        try:
            if _safetensors_ok(obj):
                from safetensors.torch import save_file
                tensors = {"__tensor__": obj} if _is_tensor_like(obj) else dict(obj)
                save_file({k: v.detach().cpu().contiguous() for k, v in tensors.items()}, os.path.join(dest, fname + ".safetensors"))
                saved.append({"name": n, "format": "safetensors", "file": fname + ".safetensors",
                              "single": _is_tensor_like(obj)})
            else:
                with open(os.path.join(dest, fname + ".bin"), "wb") as f:
                    ser.dump(obj, f)
                saved.append({"name": n, "format": ser_name, "file": fname + ".bin"})
        except Exception as e:
            for ext in (".bin", ".safetensors"):
                try:
                    os.remove(os.path.join(dest, fname + ext))
                except OSError:
                    pass
            failed.append({"name": n, "reason": f"{type(e).__name__}: {str(e)[:200]}"})
    rng_saved, rng_error = False, None
    try:
        with open(os.path.join(dest, "rng.bin"), "wb") as f:
            ser.dump(_rng_state(), f)
        rng_saved = True
    except Exception as e:
        rng_error = f"{type(e).__name__}: {str(e)[:200]}"
    modules, objects = _import_bindings(ns, set(names))
    manifest = {"saved": saved, "unserializable": failed, "rng_saved": rng_saved, "rng_error": rng_error, "notes": notes,
                "modules": modules, "objects": objects}
    with open(os.path.join(dest, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    _emit("checkpoint_saved", manifest)


def checkpoint_load(dest):
    import os
    ns = _user_ns()
    with open(os.path.join(dest, "manifest.json")) as f:
        manifest = json.load(f)
    ser, _ = _serializer()
    restored, failed = [], []
    for item in manifest["saved"]:
        n, path = item["name"], os.path.join(dest, item["file"])
        try:
            if item["format"] == "safetensors":
                from safetensors.torch import load_file
                data = load_file(path)
                ns[n] = data["__tensor__"] if item.get("single") else data
            else:
                with open(path, "rb") as f:
                    ns[n] = ser.load(f)
            restored.append(n)
        except Exception as e:
            failed.append({"name": n, "reason": f"{type(e).__name__}: {str(e)[:200]}"})
    import importlib
    reimported, import_failed = [], []
    for alias, mod in manifest.get("modules", {}).items():
        try:
            ns[alias] = importlib.import_module(mod); reimported.append(alias)
        except Exception as e:
            import_failed.append({"name": alias, "reason": f"import {mod}: {type(e).__name__}"})
    for alias, (mod, qual) in manifest.get("objects", {}).items():
        try:
            obj = importlib.import_module(mod)
            for part in qual.split("."):
                obj = getattr(obj, part)
            ns[alias] = obj; reimported.append(alias)
        except Exception as e:
            import_failed.append({"name": alias, "reason": f"from {mod} import {qual}: {type(e).__name__}"})
    rng_restored, rng_error = [], None
    if manifest.get("rng_saved"):
        try:
            with open(os.path.join(dest, "rng.bin"), "rb") as f:
                rng_restored = _set_rng_state(ser.load(f))
        except Exception as e:
            rng_error = f"{type(e).__name__}: {str(e)[:200]}"
    else:
        rng_error = manifest.get("rng_error") or "RNG state was not saved"
    _emit("checkpoint_loaded", {"restored": restored, "failed": failed, "rng_restored": rng_restored, "rng_error": rng_error,
                                "reimported": reimported, "import_failed": import_failed})
