"""Notebook generator (the "Code rebuilder", minimal version): the paper's extracted setup -> a runnable, scaled-down
experiment as notebook cells, plus the assumptions it had to make.

What is guaranteed (checked in code, not trusted to the model): every cell parses, none uses magics, shell, package installs
or a denylisted module, the notebook reports at least one metric under a name that matches a claim, and requested packages
are restricted to an allowlist. What is NOT guaranteed: that the code is correct, or safe in the sandbox sense. It is shown to
a human at a gate before it runs, it runs on a lower-isolation worker, and its results are labelled directional/scaled-down.
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any

from pydantic import BaseModel, Field, model_validator

from crisisgrid_core.harness import HarnessGuard
from crisisgrid_core.papers import ParsedPaper

from ..gateway.gateway import Gateway
from ..gateway.prompts import SYSTEM_RULES, untrusted_block
from ..gateway.structured import run_structured
from ..gateway.types import Message

MAGIC = re.compile(r"^\s*[%!]")
INSTALL = re.compile(r"\b(pip3?|uv|conda|mamba|apt(?:-get)?)\b[^\n]*\binstall\b")
FORBIDDEN_MODULES = {"subprocess", "socket", "ctypes", "pty", "shutil", "multiprocessing", "importlib", "pickle", "marshal"}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "open_with_write"}
FORBIDDEN_ATTRS = {("os", "system"), ("os", "popen"), ("os", "remove"), ("os", "rmdir"), ("os", "kill"), ("os", "fork")}
# Packages the generated notebook may ask the environment to install. Anything else is dropped and reported: package names come
# from a model, so an open list would be a supply-chain hole.
ALLOWED_PACKAGES = {
    "numpy", "scipy", "pandas", "scikit-learn", "matplotlib", "seaborn", "tqdm", "pillow", "torch", "torchvision", "torchaudio",
    "tensorflow", "keras", "jax", "jaxlib", "flax", "optax", "transformers", "datasets", "tokenizers", "sentencepiece",
    "xgboost", "lightgbm", "catboost", "statsmodels", "networkx", "sympy", "opencv-python-headless", "einops", "scikit-image",
}
MAX_CELLS, MAX_CHARS = 14, 60_000


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


class GenCell(BaseModel):
    purpose: str = ""
    source: str


class GeneratedNotebook(BaseModel):
    cells: list[GenCell] = Field(min_length=1, max_length=MAX_CELLS)
    requirements: list[str] = Field(default_factory=list)  # pip requirement names, e.g. "numpy", "scikit-learn"
    assumptions: list[str] = Field(default_factory=list)  # everything the notebook assumes or reduces, in plain language
    scaled_down: bool = True
    dataset_note: str = ""

    @model_validator(mode="after")
    def _check(self, info: Any) -> "GeneratedNotebook":
        issues = code_issues([c.source for c in self.cells])
        metrics = (info.context or {}).get("metrics") if info is not None and getattr(info, "context", None) else None
        if not issues and metrics:
            reported = {_norm(m) for c in self.cells for m in re.findall(r"report_metric\(\s*['\"]([^'\"]+)['\"]", c.source)}
            wanted = {_norm(m) for m in metrics}
            if not reported:
                issues.append("the notebook never calls report_metric('<name>', value)")
            elif not any(r in w or w in r for r in reported for w in wanted):
                issues.append(f"report_metric names {sorted(reported)} do not match any claim metric {sorted(metrics)}; "
                              "use the paper's metric name")
        if issues:
            raise ValueError("generated notebook rejected: " + "; ".join(issues[:6]))
        return self


def code_issues(sources: list[str]) -> list[str]:
    """Static checks on generated cells. Best-effort policy, NOT a sandbox."""
    out: list[str] = []
    if sum(len(s) for s in sources) > MAX_CHARS:
        return [f"the notebook is longer than {MAX_CHARS} characters"]
    for i, src in enumerate(sources):
        if not src.strip():
            out.append(f"cell {i} is empty")
            continue
        for ln in src.splitlines():
            if MAGIC.match(ln):
                out.append(f"cell {i} uses an IPython magic or shell line ({ln.strip()[:40]!r})")
            elif INSTALL.search(ln):
                out.append(f"cell {i} installs packages; list them in 'requirements' instead")
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            out.append(f"cell {i} has a syntax error at line {e.lineno}: {e.msg}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                bad = [a.name for a in node.names if a.name.split(".")[0] in FORBIDDEN_MODULES]
            elif isinstance(node, ast.ImportFrom):
                bad = [node.module] if node.module and node.module.split(".")[0] in FORBIDDEN_MODULES else []
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                bad = [node.func.id + "()"]
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and (node.value.id, node.attr) in FORBIDDEN_ATTRS:
                bad = [f"{node.value.id}.{node.attr}"]
            else:
                bad = []
            out += [f"cell {i} uses {b}, which generated code may not use" for b in bad]
    return list(dict.fromkeys(out))


def filter_requirements(reqs: list[str]) -> tuple[list[str], list[str]]:
    """(kept, dropped). Only allowlisted package names, plain specifiers, no options or URLs."""
    kept, dropped = [], []
    for r in reqs:
        r = r.strip()
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*((?:[<>=!~]=?\s*[A-Za-z0-9.*+!_-]+\s*,?\s*)*)", r)
        name = m.group(1).lower().replace("_", "-") if m else ""
        if m and name in ALLOWED_PACKAGES:
            kept.append(name + (m.group(2).replace(" ", "") if m.group(2) else ""))
        else:
            dropped.append(r)
    return list(dict.fromkeys(kept)), dropped


INSTRUCTIONS = """You write a small, runnable Jupyter notebook that reproduces the experiment described by the extracted paper setup.
Hard rules:
- Output ONLY the JSON object of the schema. Cells are Python source strings. No markdown, no prose outside JSON.
- The notebook must finish in about 5 minutes on a CPU. Scale the experiment DOWN if needed (fewer epochs, a subset of the data,
  a smaller model) and say exactly what you reduced in "assumptions". Set "scaled_down": true whenever anything was reduced.
- Use the stated hyperparameters wherever you can; for anything the setup does not state, pick a sensible value and list it in
  "assumptions". Never present a guess as stated.
- Two names are already defined in the kernel, do NOT import or define them: SEED (an int; seed EVERY random generator you use
  with it: random, numpy, torch, ...) and report_metric(name, value).
- For EACH claim, call report_metric("<the claim's metric name>", <float>) with the value your run measured. Use the paper's metric
  name. Fractions vs percent: report in the same unit as the claim.
- Data: if the paper's dataset is large, private or not downloadable in a few minutes, use a small public or synthetic stand-in
  (e.g. scikit-learn's built-in datasets, torchvision FakeData, or a tiny subset) and say so in "dataset_note" and "assumptions".
- No magics (%), no shell (!), no pip installs, no subprocess/socket/ctypes/eval/exec. List needed packages in "requirements"
  using plain names (numpy, scikit-learn, torch, ...).
- Keep it simple: 3 to 8 short cells (imports+setup, data, model, training, evaluation+report_metric)."""


class NotebookGeneratorAgent:
    def __init__(self, gateway: Gateway, guard: HarnessGuard) -> None:
        self.gateway, self.guard = gateway, guard

    @staticmethod
    def facts(parsed: ParsedPaper, gaps: list[Any] | None = None) -> dict[str, Any]:
        d = parsed.model_dump(mode="json")
        return {
            "title": d.get("title"),
            "claims": [{"text": c["text"], "metric": c["metric"], "dataset": c["dataset"], "split": c["split"],
                        "reported_value": c["reported_value"], "unit": c["unit"]} for c in d.get("claims", [])],
            "hyperparameters": {k: {"value": v["value"], "provenance": v["provenance"]} for k, v in (d.get("hyperparameters") or {}).items()},
            "datasets": [{"name": x["name"], "split": x.get("split"), "access": x.get("access")} for x in d.get("datasets", [])],
            "metrics": [{"name": m["name"], "definition": m.get("definition")} for m in d.get("metrics", [])],
            "seeds": (d.get("seeds") or {}).get("value"),
            "hardware": d.get("hardware"),
            "known_gaps": [g.get("description") for g in (gaps and [x.model_dump(mode="json") for x in gaps] or [])][:8],
        }

    def generate(self, parsed: ParsedPaper, gaps: list[Any] | None = None, *, run_id: str | None = None,
                 hint: str | None = None) -> tuple[GeneratedNotebook, list[str]]:
        """Returns the validated notebook and notes about what was dropped (e.g. non-allowlisted packages)."""
        facts = self.facts(parsed, gaps)
        msgs = [
            Message(role="system", content=SYSTEM_RULES + "\n" + INSTRUCTIONS),
            Message(role="user", content=untrusted_block("paper-setup", json.dumps(facts, indent=1))),
        ]
        if hint:
            msgs.append(Message(role="user", content=f"Reviewer hint (from the human operator; the rules above still apply): {hint}"))
        self.guard.start_stage("generate")
        nb = run_structured(self.gateway, self.guard, "generate", "notebook_gen", msgs, GeneratedNotebook, run_id=run_id,
                            context={"metrics": [c["metric"] for c in facts["claims"] if c.get("metric")]})
        kept, dropped = filter_requirements(nb.requirements)
        notes = [f"package '{d}' was requested but is not on the allowlist, so it will not be installed" for d in dropped]
        return nb.model_copy(update={"requirements": kept}), notes
