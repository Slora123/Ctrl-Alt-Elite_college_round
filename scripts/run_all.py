#!/usr/bin/env python3
"""Start the whole CrisisGrid system with one command:

    python scripts/run_all.py [--no-auth] [--worker] [--terminal] [--llm-chain "groq/qwen/qwen3-32b" --llm-key ...]

  website (React, Vite)         http://localhost:5173      <- open this
  website API (Node, mock AI)   http://127.0.0.1:8000
  CrisisGrid API (FastAPI)      http://127.0.0.1:8100      (pipeline, auth, and the notebook environment under /paper-env)

First run installs dependencies (uv + npm) and builds the notebook UI. Everything shares ONE auth secret, generated once
into .cg/auth_secret. State (databases, environments, logs) lives in .cg/. Ctrl-C stops everything.
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".cg"  # overridable with --state-dir
WIN = sys.platform == "win32"
API_PORT, NODE_PORT, WEB_PORT = 8100, 8000, 5173  # overridable with --api-port / --node-port / --web-port


def sh(cmd: list[str], cwd: Path, env: dict | None = None, quiet: bool = False) -> None:
    r = subprocess.run(cmd, cwd=cwd, env={**os.environ, **(env or {})}, stdout=subprocess.DEVNULL if quiet else None)
    if r.returncode:
        sys.exit(f"command failed ({r.returncode}): {' '.join(cmd)}")


def need(tool: str, hint: str) -> str:
    p = shutil.which(tool)
    if not p:
        sys.exit(f"'{tool}' not found. {hint}")
    return p


def auth_secret() -> str:
    STATE.mkdir(exist_ok=True)
    f = STATE / "auth_secret"
    if not f.exists():
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_urlsafe(48))
    return f.read_text().strip()


def ensure_python() -> Path:
    """One uv workspace (pyproject.toml at the repo root): `uv sync` creates .venv with core, api and paper_env editable."""
    uv = need("uv", "Install uv: https://docs.astral.sh/uv/")
    py = ROOT / ".venv" / ("Scripts/python.exe" if WIN else "bin/python")
    stamp = ROOT / ".venv" / ".cg-synced"
    lock = ROOT / "uv.lock"
    if not py.exists() or not stamp.exists() or stamp.stat().st_mtime < lock.stat().st_mtime:
        print("[run_all] uv sync (installing the Python workspace)…")
        sh([uv, "sync"], ROOT)
        stamp.write_text("ok")
    return py


def ensure_node() -> None:
    npm = need("npm", "Install Node.js 20+.")
    if not (ROOT / "website" / "node_modules").exists():
        print("[run_all] npm install (website)…")
        sh([npm, "install", "--no-fund", "--no-audit"], ROOT / "website")
    fe = ROOT / "paper_env" / "frontend"
    if not (ROOT / "paper_env" / "paper_env" / "api" / "static" / "app" / "index.html").exists() and not WIN:
        print("[run_all] building the notebook UI (first run only)…")
        if not (fe / "node_modules").exists():
            sh([npm, "install", "--no-fund", "--no-audit"], fe)
        sh([npm, "run", "build"], fe)


def load_env_file() -> dict[str, str]:
    """KEY=VALUE lines from <state>/env (git-ignored, keep it chmod 600): provider keys such as AWS_BEARER_TOKEN_BEDROCK or
    CG_LLM_API_KEY go here instead of the command line, so they never end up in shell history or process listings."""
    f = STATE / "env"
    out: dict[str, str] = {}
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
        if f.stat().st_mode & 0o077 and not WIN:
            print(f"[run_all] warning: {f} is readable by other users; run: chmod 600 {f}")
    return out


def port_in_use(port: int) -> bool:
    import socket
    for fam, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(fam, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def tail(path: Path, n: int = 15) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return "(no log)"


def wait(url: str, name: str, proc: subprocess.Popen, log: Path, seconds: int = 90) -> None:
    """Ready only when OUR process is alive and answers. A health answer from some other process on the port (a previous run)
    must not count, and a child that already died is reported with its log instead of 'ready'."""
    for _ in range(seconds * 2):
        if proc.poll() is not None:
            sys.exit(f"[run_all] {name} exited with code {proc.returncode} while starting.\n--- {log.name} ---\n{tail(log)}")
        try:
            urllib.request.urlopen(url, timeout=2).read()
            print(f"[run_all] {name} ready  {url}")
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    sys.exit(f"{name} did not become ready at {url}\n--- {log.name} ---\n{tail(log)}")


def main() -> None:
    global STATE, API_PORT, NODE_PORT, WEB_PORT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-auth", action="store_true", help="run without logins (single-user local use)")
    ap.add_argument("--worker", action="store_true", help="also start a worker that runs jobs in paper_env kernels")
    ap.add_argument("--terminal", action="store_true", help="enable the notebook terminal (a real unsandboxed shell!)")
    ap.add_argument("--llm-chain", default="", help="comma-separated LiteLLM model ids, e.g. groq/qwen/qwen3-32b")
    ap.add_argument("--llm-key", default="", help="API key for the LLM provider")
    ap.add_argument("--state-dir", default="", help="where databases, environments and logs live (default: .cg)")
    ap.add_argument("--api-port", type=int, default=API_PORT)
    ap.add_argument("--node-port", type=int, default=NODE_PORT)
    ap.add_argument("--web-port", type=int, default=WEB_PORT)
    a = ap.parse_args()
    STATE = Path(a.state_dir).resolve() if a.state_dir else STATE
    API_PORT, NODE_PORT, WEB_PORT = a.api_port, a.node_port, a.web_port

    busy = [(n, p) for n, p in (("CrisisGrid API", API_PORT), ("website API", NODE_PORT), ("website", WEB_PORT)) if port_in_use(p)]
    if busy:  # typically a previous run_all that is still alive; starting anyway would "succeed" against the old servers
        sys.exit("[run_all] these ports are already in use: " + ", ".join(f"{n} :{p}" for n, p in busy) +
                 "\n  A previous run is probably still running. Stop it (Ctrl-C in its terminal, or: pkill -f run_all.py; "
                 "pkill -f vite; pkill -f server.js; pkill -f crisisgrid_api), or pick other ports with --api-port/--node-port/--web-port.")
    py = ensure_python()
    ensure_node()
    logs = STATE / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    secret = auth_secret()
    base = {
        "CG_ENV": "dev", "CG_AUTH_SECRET": secret, "CG_AUTH_ENABLED": "0" if a.no_auth else "1",
        "CG_DATABASE_URL": f"sqlite:///{(STATE / 'crisisgrid.db').as_posix()}", "CG_DATA_DIR": str(STATE),
        "CG_PAPER_ENV_DIR": str(STATE / "paper_env"), "CG_PAPER_ENV_TERMINAL": "1" if a.terminal else "0",
        "CG_LLM_CHAIN": a.llm_chain, "CG_LLM_API_KEY": a.llm_key, "CG_AUTH_URL": f"http://127.0.0.1:{API_PORT}",
        "PAPER_ENV_ENABLE_TERMINAL": "1" if a.terminal else "0",
        # the website's Vite proxy and the Node backend find their neighbours through these
        "CG_API": f"http://127.0.0.1:{API_PORT}", "CG_NODE_API": f"http://127.0.0.1:{NODE_PORT}",
    }
    env = {**os.environ, **load_env_file(), **base}
    procs: list[subprocess.Popen] = []

    def spawn(name: str, cmd: list[str], cwd: Path, extra: dict | None = None) -> subprocess.Popen:
        out = open(logs / f"{name}.log", "wb")
        p = subprocess.Popen(cmd, cwd=cwd, env={**env, **(extra or {})}, stdout=out, stderr=subprocess.STDOUT,
                             start_new_session=not WIN)
        procs.append(p)
        return p

    def stop(*_: object) -> None:
        print("\n[run_all] stopping…")
        for p in reversed(procs):
            try:
                os.killpg(p.pid, signal.SIGTERM) if not WIN else p.terminate()
            except (ProcessLookupError, PermissionError, OSError):
                pass
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    sh([str(py), "-m", "alembic", "upgrade", "head"], ROOT / "api", env=base, quiet=True)
    p = spawn("crisisgrid-api", [str(py), "-m", "uvicorn", "crisisgrid_api.main:app", "--host", "127.0.0.1", "--port", str(API_PORT)], ROOT / "api")
    wait(f"http://127.0.0.1:{API_PORT}/health", "CrisisGrid API", p, logs / "crisisgrid-api.log")
    p = spawn("website-api", [need("node", "Install Node.js 20+."), "server.js"], ROOT / "website" / "backend", {"PORT": str(NODE_PORT)})
    wait(f"http://127.0.0.1:{NODE_PORT}/api/health", "website API", p, logs / "website-api.log")
    p = spawn("website", [need("node", ""), str(ROOT / "website" / "node_modules" / "vite" / "bin" / "vite.js"), "--port", str(WEB_PORT), "--strictPort"],
              ROOT / "website")
    wait(f"http://localhost:{WEB_PORT}/", "website", p, logs / "website.log")

    if a.worker:  # dev: registration is open, so the worker registers itself (its token is printed in .cg/logs/worker.log)
        spawn("worker", [str(py), "-m", "crisisgrid_api.worker", "--api", f"http://127.0.0.1:{API_PORT}", "--name", "local-worker",
                         "--executor", "paper_env", "--paper-env-dir", str(STATE / "worker_paper_env")], ROOT / "api")

    print(f"""
[run_all] CrisisGrid is running.
   Open            http://localhost:{WEB_PORT}/        {'(no login: single-user mode)' if a.no_auth else '(create the first account: it becomes the administrator)'}
   Notebook UI     http://localhost:{WEB_PORT}/cg/paper-env/ui/
   API docs        http://127.0.0.1:{API_PORT}/docs
   Logs            {logs}
   LLM agents      {'enabled: ' + a.llm_chain if a.llm_chain else 'NOT configured (pass --llm-chain; keys go in ' + str(STATE / 'env') + '); reading, notebooks and static audit still work'}
Ctrl-C to stop.""")
    while True:
        for p in procs:
            if p.poll() is not None:
                name = Path(p.args[1] if len(p.args) > 1 else p.args[0]).name
                print(f"[run_all] a service ({' '.join(map(str, p.args[:3]))[:80]}) exited with code {p.returncode}; logs in {logs}")
                for lf in sorted(logs.glob("*.log")):
                    print(f"--- {lf.name} (last lines) ---\n{tail(lf, 6)}")
                stop()
        time.sleep(1)


if __name__ == "__main__":
    main()
