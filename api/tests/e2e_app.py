"""A real CrisisGrid API (real auth, real database, real pipeline) with a SCRIPTED stand-in for the LLM and a stubbed GitHub
lookup, for browser end-to-end tests of the website. No network, no keys.

    python tests/e2e_app.py --port 8100 --state ./.e2e-state

The scripted model answers each agent by looking at its system prompt, with the extraction results of the demo paper
(`crisisgrid_api.demo`), so a PDF built by `crisisgrid_api.demo_pdf.make_pdf(crisisgrid_api.demo.PAPER)` analyses cleanly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config

from crisisgrid_api import demo
from crisisgrid_api.config import Settings
from crisisgrid_api.container import build_container
from crisisgrid_api.db.base import make_engine
from crisisgrid_api.gateway.gateway import Gateway, Provider
from crisisgrid_api.gateway.types import LLMResponse, Message
from crisisgrid_api.main import create_app


class RoutingClient:
    """Answers by agent, not by call order, so any number of uploads works."""

    def complete(self, model: str, messages: list[Message], *, temperature: float, max_tokens: int) -> LLMResponse:
        system = messages[0].content
        if "extract the experimental setup" in system:
            text = json.dumps(demo.PARSED)
        elif "audit an ML paper's methodology" in system:
            text = json.dumps({"gaps": []})
        else:  # diagnoser / report writer are not enabled in this harness
            text = json.dumps({"summary": "n/a", "limitations": []})
        return LLMResponse(text=text, model=model, prompt_tokens=10, completion_tokens=10)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--state", default="./.e2e-state")
    ap.add_argument("--no-llm", action="store_true", help="simulate a server with no LLM configured")
    a = ap.parse_args()

    state = Path(a.state).resolve()
    state.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CG_AUTH_SECRET", "e2e-secret-e2e-secret-e2e-secret-1234")
    os.environ.setdefault("CG_ENV", "dev")
    url = f"sqlite:///{(state / 'e2e.db').as_posix()}"
    os.environ["CG_DATABASE_URL"] = url
    os.environ.setdefault("CG_DATA_DIR", str(state))
    os.environ.setdefault("CG_PAPER_ENV", "0")  # paper_env is POSIX-only; the website pages under test do not need it

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(demo.__file__).parent / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    settings = Settings.from_env()
    engine = make_engine(url)
    gateway = None if a.no_llm else Gateway([Provider("scripted", "e2e-llm", RoutingClient())], sleep=lambda s: None)
    container = build_container(engine, settings, gateway=gateway, fetcher=demo._StubGitHub())
    app = create_app(settings, engine, container)
    print(f"e2e api on http://127.0.0.1:{a.port}  state={state}  llm={'off' if a.no_llm else 'scripted'}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
