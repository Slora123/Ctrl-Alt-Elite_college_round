"""Per-PaperEnv file workspace (the kernel's working directory). Upload / list / download / delete."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable

from ..config import Settings


class UnsafePath(ValueError):
    pass


class TooLarge(ValueError):
    pass


@dataclass
class FileInfo:
    path: str
    size: int
    mtime: float


class FileService:
    MAX_LISTED = 5000

    def __init__(self, settings: Settings):
        self.settings = settings

    def root(self, paper_env_id: str) -> Path:
        r = self.settings.files_dir(paper_env_id)
        r.mkdir(parents=True, exist_ok=True)
        return r

    def resolve(self, paper_env_id: str, rel: str) -> Path:
        """Map a user-supplied relative path into the workspace; reject anything that could escape it."""
        root = self.root(paper_env_id).resolve()
        if not rel or rel.startswith(("/", "\\")) or "\x00" in rel:
            raise UnsafePath(f"invalid path {rel!r}")
        parts = Path(rel).parts
        if any(p in ("..", "") for p in parts):
            raise UnsafePath(f"invalid path {rel!r}")
        target = (root / rel).resolve()
        if root != target and root not in target.parents:
            raise UnsafePath(f"path escapes the workspace: {rel!r}")
        return target

    def list(self, paper_env_id: str) -> list[FileInfo]:
        root = self.root(paper_env_id)
        out = []
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.is_symlink():
                st = p.stat()
                out.append(FileInfo(p.relative_to(root).as_posix(), st.st_size, st.st_mtime))
                if len(out) >= self.MAX_LISTED:
                    break
        return out

    def save(self, paper_env_id: str, rel: str, chunks: Iterable[bytes]) -> FileInfo:
        target = self.resolve(paper_env_id, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".uploading")
        size = 0
        try:
            with open(tmp, "wb") as f:
                for chunk in chunks:
                    size += len(chunk)
                    if size > self.settings.max_upload_bytes:
                        raise TooLarge(f"upload exceeds {self.settings.max_upload_bytes} bytes")
                    f.write(chunk)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        return FileInfo(rel, size, target.stat().st_mtime)

    def open_for_download(self, paper_env_id: str, rel: str) -> Path:
        p = self.resolve(paper_env_id, rel)
        if not p.is_file():
            raise FileNotFoundError(rel)
        return p

    def delete(self, paper_env_id: str, rel: str) -> None:
        self.open_for_download(paper_env_id, rel).unlink()
