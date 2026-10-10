from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path

from ..domain import DatasetFile


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def overall_checksum(files: list[DatasetFile]) -> str:
    """Deterministic digest of a file set: sha256 over sorted 'sha256  path' lines."""
    lines = sorted(f"{f.sha256}  {f.path}" for f in files)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def scan_dir(root: Path) -> list[DatasetFile]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out.append(DatasetFile(path=p.relative_to(root).as_posix(), size=p.stat().st_size, sha256=sha256_file(p)))
    return out


def make_read_only(root: Path) -> None:
    for p in sorted(root.rglob("*"), reverse=True):
        if p.is_symlink():
            continue
        p.chmod(0o444 if p.is_file() else 0o555)
    root.chmod(0o555)


def make_writable(root: Path) -> None:
    if not root.exists():
        return
    root.chmod(root.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            q = Path(dirpath) / n
            if not q.is_symlink():
                q.chmod(q.stat().st_mode | stat.S_IWUSR | stat.S_IRUSR | (stat.S_IXUSR if q.is_dir() else 0))


def rmtree_force(root: Path) -> None:
    make_writable(root)
    shutil.rmtree(root, ignore_errors=True)


def dir_size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())
