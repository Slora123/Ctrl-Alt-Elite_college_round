from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    """All tunables in one place; everything is overridable (tests shrink the timeouts)."""

    data_dir: Path = field(default_factory=lambda: Path("paper_env_data").resolve())
    uv_bin: str = "uv"
    # Packages every kernel env needs. Recorded in the lockfile like any other package.
    kernel_packages: list[str] = field(default_factory=lambda: ["ipykernel"])
    install_timeout_s: float = 900.0
    venv_timeout_s: float = 300.0
    cell_timeout_s: float = 600.0
    run_timeout_s: float = 3600.0
    max_output_bytes_db: int = 64 * 1024  # larger outputs are truncated in the DB, full text goes to a file
    max_output_bytes_total: int = 256 * 1024 * 1024  # hard cap per cell; beyond this output is dropped
    min_free_disk_bytes: int = 512 * 1024 * 1024
    cache_max_bytes: int = 50 * 1024 * 1024 * 1024
    checkpoint_after_each_cell: bool = False

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)

    # ---- layout ----
    def env_dir(self, paper_env_id: str) -> Path:
        return self.data_dir / "envs" / paper_env_id

    def venv_dir(self, paper_env_id: str) -> Path:
        return self.env_dir(paper_env_id) / "venv"

    def venv_python(self, paper_env_id: str) -> Path:
        return self.venv_dir(paper_env_id) / "bin" / "python"

    def logs_dir(self, paper_env_id: str) -> Path:
        return self.env_dir(paper_env_id) / "logs"

    def lockfile_path(self, paper_env_id: str) -> Path:
        return self.env_dir(paper_env_id) / "requirements.lock.txt"

    def artifacts_dir(self, paper_env_id: str) -> Path:
        return self.data_dir / "artifacts" / paper_env_id

    def cache_dir(self) -> Path:
        return self.data_dir / "datasets" / "cas"

    def runs_dir(self, run_id: str) -> Path:
        return self.data_dir / "runs" / run_id
