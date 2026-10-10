import pytest

from paper_env.repos.memory import memory_uow_factory
from paper_env.repos.sqlite import sqlite_uow_factory


@pytest.fixture(params=["memory", "sqlite"])
def uow_factory(request, tmp_path):
    if request.param == "memory":
        return memory_uow_factory()
    return sqlite_uow_factory(f"sqlite:///{tmp_path / 'test.db'}")


# ---------------------------------------------------------------- shared stack fixtures
import hashlib  # noqa: E402
import base64  # noqa: E402
import zipfile  # noqa: E402
from pathlib import Path  # noqa: E402

from paper_env.config import Settings  # noqa: E402
from paper_env.domain import EnvSpec, PaperEnv  # noqa: E402
from paper_env.env import EnvManager  # noqa: E402
from paper_env.events import EventBus  # noqa: E402
from paper_env.hooks import ActionGate, AllowAllHook  # noqa: E402
from paper_env.security import Scrubber  # noqa: E402


def make_wheel(dest: Path, name: str, version: str, requires: list[str] = ()) -> Path:
    """Build a minimal pure-python wheel so env tests need no network."""
    dest.mkdir(parents=True, exist_ok=True)
    dist = f"{name}-{version}"
    files = {
        f"{name}/__init__.py": f"__version__ = {version!r}\n",
        f"{dist}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                                      + "".join(f"Requires-Dist: {r}\n" for r in requires),
        f"{dist}.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = []
    for n, c in files.items():
        d = base64.urlsafe_b64encode(hashlib.sha256(c.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{n},sha256={d},{len(c)}")
    record.append(f"{dist}.dist-info/RECORD,,")
    path = dest / f"{dist}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as z:
        for n, c in files.items():
            z.writestr(n, c)
        z.writestr(f"{dist}.dist-info/RECORD", "\n".join(record) + "\n")
    return path


@pytest.fixture
def wheels(tmp_path, monkeypatch):
    """Local wheelhouse used as the only package index (offline, deterministic)."""
    d = tmp_path / "wheelhouse"
    make_wheel(d, "demo_a", "1.0", ["demo_b<2"])
    make_wheel(d, "demo_b", "1.0")
    make_wheel(d, "demo_b", "2.0")
    make_wheel(d, "demo_c", "1.0")
    monkeypatch.setenv("UV_FIND_LINKS", str(d))
    monkeypatch.setenv("UV_NO_INDEX", "1")
    return d


class Stack:
    """Wired services for tests."""

    def __init__(self, tmp_path, uow_factory, hook=None, kernel_packages=()):
        self.settings = Settings(data_dir=tmp_path / "data", kernel_packages=list(kernel_packages),
                                 install_timeout_s=120)
        self.uow = uow_factory
        self.scrubber = Scrubber(scan_environ=False)
        self.bus = EventBus(uow_factory, self.scrubber)
        self.hook = hook or AllowAllHook()
        self.gate = ActionGate(self.hook, uow_factory, self.bus)
        self.envs = EnvManager(self.settings, uow_factory, self.bus, self.gate)
        self.captured = []
        self.bus.subscribe(self.captured.append)

    def new_paper_env(self, alias="p1", **spec_kw) -> PaperEnv:
        spec = EnvSpec(**spec_kw)
        env = PaperEnv(alias=alias, paper_hash="h", env_spec_id=spec.id)
        with self.uow() as u:
            u.paper_envs.add(env, spec)
        return env

    def spec(self, env: PaperEnv) -> EnvSpec:
        with self.uow() as u:
            return u.paper_envs.get_spec(env.env_spec_id)

    def event_types(self):
        return [e.type for e in self.captured]


@pytest.fixture
def stack(tmp_path, uow_factory):
    return Stack(tmp_path, uow_factory)


@pytest.fixture
def stack_factory(tmp_path, uow_factory):
    def make(**kw):
        return Stack(tmp_path, uow_factory, **kw)
    return make


# ---------------------------------------------------------------- kernel stack
import shutil  # noqa: E402

from paper_env.backends import LocalBackend  # noqa: E402
from paper_env.kernel.runner import KernelRunner  # noqa: E402
from paper_env.repos.memory import memory_uow_factory  # noqa: E402
from paper_env.services.notebook import NotebookService  # noqa: E402
from paper_env.services.paper_envs import PaperEnvService  # noqa: E402
from paper_env.services.runs import RunService  # noqa: E402


@pytest.fixture(scope="session")
def kernel_template(tmp_path_factory):
    """One venv with ipykernel, built once per test session and copied per test."""
    root = tmp_path_factory.mktemp("kernel_template")
    st = Stack(root, memory_uow_factory(), kernel_packages=["ipykernel"])
    st.settings.install_timeout_s = 600
    env = st.new_paper_env(alias="template")
    res = st.envs.create_env(env.id)
    assert res.success, res
    return {"venv": st.settings.venv_dir(env.id), "lockfile_hash": res.lockfile_hash}


class KStack(Stack):
    """Stack plus notebook/backend/runner/run services and a pre-built env copied from the template."""

    def __init__(self, tmp_path, uow_factory, template, hook=None, **settings_kw):
        super().__init__(tmp_path, uow_factory, hook=hook, kernel_packages=["ipykernel"])
        for k, v in settings_kw.items():
            setattr(self.settings, k, v)
        self.settings.min_free_disk_bytes = 0
        self.backend = LocalBackend(self.settings, self.scrubber)
        self.notebook = NotebookService(uow_factory, self.bus)
        self.runner = KernelRunner(self.settings, uow_factory, self.bus, self.gate, self.backend, self.notebook)
        self.runs = RunService(self.settings, uow_factory, self.bus, self.gate, self.runner, self.notebook,
                               self.envs, self.backend)
        self.paper_envs = PaperEnvService(uow_factory, self.envs)
        self.template = template

    def new_ready_env(self, alias="p1", **spec_kw):
        env = self.new_paper_env(alias=alias, **spec_kw)
        shutil.copytree(self.template["venv"], self.settings.venv_dir(env.id), symlinks=True)
        spec = self.spec(env)
        spec.lockfile_hash = self.template["lockfile_hash"]
        with self.uow() as u:
            u.paper_envs.update_spec(spec)
        return env

    def add_cells(self, env, *sources, **kw):
        from paper_env.domain import Author
        return [self.notebook.add_cell(env.id, s, Author.HUMAN, **kw) for s in sources]

    def cell(self, cell_id):
        return self.notebook.get_cell(cell_id)

    def cell_runs(self, run):
        return self.runs.cell_runs(run.id)

    def close(self):
        self.runner.shutdown()
        self.backend.shutdown_all()


@pytest.fixture
def kstack(tmp_path, kernel_template):
    st = KStack(tmp_path, memory_uow_factory(), kernel_template)
    yield st
    st.close()


@pytest.fixture
def kstack_factory(tmp_path, kernel_template):
    made = []

    def make(uow_factory=None, hook=None, **kw):
        st = KStack(tmp_path, uow_factory or memory_uow_factory(), kernel_template, hook=hook, **kw)
        made.append(st)
        return st
    yield make
    for st in made:
        st.close()
