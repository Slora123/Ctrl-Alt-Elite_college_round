"""Action hook (system_design 3.5): the single checkpoint before any side-effecting action. It never trusts an agent's own
claim about how risky a change is: code edits are classified here, from the diff.

  safe                 install/pin a missing package, fix a data path or an import, rerun        -> allow (logged)
  experiment_changing  hyperparameters, architecture, loss, split, preprocessing, seeds...        -> needs_approval for agents
  external             dataset fetch / download from a host                                       -> allow if allowlisted, else ask
  blocked              network outside the allowlist, host mounts                                  -> deny

Default for anything unrecognised is the cautious class (experiment_changing).
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from .harness import HarnessGuard

__all__ = ["Action", "Decision", "ActionHook", "classify_edit", "RiskClass", "DEFAULT_ALLOWLIST"]

RiskClass = Literal["safe", "experiment_changing", "external", "blocked"]
ActionKind = Literal["cell_run", "install", "pin", "dataset_fetch", "env_rebuild", "edit"]
Author = Literal["human", "generator", "agent"]

DEFAULT_ALLOWLIST = frozenset({"pypi.org", "files.pythonhosted.org", "huggingface.co", "cdn-lfs.huggingface.co", "github.com",
                               "raw.githubusercontent.com", "zenodo.org"})


class Action(BaseModel):
    kind: ActionKind
    author: Author
    target: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class Decision(BaseModel):
    decision: Literal["allow", "deny", "needs_approval"]
    reason: str
    risk_class: RiskClass


# ---- classifying a code edit ---------------------------------------------------------------------------------------

_IMPORT = re.compile(r"^\s*(?:import\s+[\w.,\s]+(?:\s+as\s+\w+)?|from\s+[\w.]+\s+import\s+[\w.*,\s()]+(?:\s+as\s+\w+)?)\s*(?:#.*)?$")
_COMMENT_OR_BLANK = re.compile(r"^\s*(?:#.*)?$")
_PATH_VAR = re.compile(r"(?i)^\s*\w*(?:path|dir|folder|root|file|filename|url|uri)\w*\s*=\s*(?:r|f)?(['\"])[^'\"\n]*\1\s*(?:#.*)?$")
_PATH_LITERAL_CALL = re.compile(r"^\s*(?:df|data|ds|raw)\w*\s*=\s*\w+(?:\.\w+)*\(\s*(?:r|f)?(['\"])[^'\"\n]+\1\s*\)\s*(?:#.*)?$")

# anything touching these is an experimental choice, however innocent the diff looks. Matching is token-based, so
# `split_70_30.csv`, `lr_schedule` and `batchSize` are caught; any number with a decimal point or 2+ digits counts too.
_EXP_WORDS = frozenset(
    "lr learning rate epoch epochs batch seed momentum weight decay dropout optimizer optimiser sgd adam adamw loss criterion "
    "scheduler warmup layer layers hidden width depth kernel heads activation normalize normalise augment augmentation "
    "transform transforms split shuffle threshold temperature beta gamma alpha epsilon eps stride padding".split())


def _experimental(line: str) -> bool:
    spaced = re.sub(r"([a-z])([A-Z])", lambda m: m.group(1) + " " + m.group(2), line)  # camelCase -> camel Case
    for tok in re.findall(r"[A-Za-z]+|\d+(?:\.\d+)?", spaced):
        if tok.lower() in _EXP_WORDS or (tok[0].isdigit() and ("." in tok or len(tok) >= 2)):
            return True
    return False


def _changed_lines(old: str, new: str) -> list[str]:
    out = []
    for ln in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if ln.startswith(("+++", "---", "@@")):
            continue
        if ln[:1] in "+-":
            out.append(ln[1:])
    return out


def classify_edit(old: str, new: str) -> tuple[RiskClass, str]:
    """safe only if EVERY changed line is an import, a comment/blank, or a path/URL assignment; otherwise the edit could
    change the experiment."""
    lines = _changed_lines(old, new)
    if not lines:
        return "safe", "no change"
    for ln in lines:
        if _COMMENT_OR_BLANK.match(ln) or _IMPORT.match(ln):
            continue
        if (_PATH_VAR.match(ln) or _PATH_LITERAL_CALL.match(ln)) and not _experimental(ln.split("#")[0]):
            continue
        return "experiment_changing", f"changed line is not an import, comment or path fix: {ln.strip()[:80]!r}"
    return "safe", "only imports, comments and paths changed"


def _host(url: str) -> str:
    return (urlparse(url if "//" in url else "//" + url).hostname or "").lower()


class ActionHook:
    def __init__(self, guard: HarnessGuard, allowlist: frozenset[str] | set[str] = DEFAULT_ALLOWLIST) -> None:
        self.guard, self.allowlist = guard, {h.lower() for h in allowlist}

    def _host_ok(self, host: str) -> bool:
        return any(host == h or host.endswith("." + h) for h in self.allowlist)

    def classify(self, a: Action) -> tuple[RiskClass, str]:
        p = a.payload
        if a.kind == "edit":
            return classify_edit(str(p.get("old", "")), str(p.get("new", "")))
        if a.kind in ("install", "pin"):
            name = str(p.get("package", ""))
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[\w,]+\])?(?:[=<>!~]=?[\w.*+!-]+)?", name):
                return "blocked", f"'{name}' is not a plain package requirement (no urls, paths or options)"
            return ("external", "package index outside the allowlist") if p.get("index") and not self._host_ok(_host(str(p["index"]))) \
                else ("safe", "install/pin a package from the allowlisted index")
        if a.kind == "dataset_fetch":
            host = _host(str(p.get("url", "")))
            if p.get("mount") or str(p.get("url", "")).startswith(("file:", "/")):
                return "blocked", "host mounts / local file URLs are never allowed"
            return ("safe", f"{host} is allowlisted") if self._host_ok(host) else ("external", f"new host: {host or '?'}")
        if a.kind == "env_rebuild":
            return "safe", "rebuild from the pinned spec"
        if a.kind == "cell_run":
            return "safe", "rerun"
        return "experiment_changing", "unknown action kind"

    def check(self, a: Action, stage: str = "repair") -> Decision:
        # 1. budget and loop checks come first: an action that keeps failing is not allowed to keep trying
        g = self.guard.check_budget(stage)
        if not g.ok:
            return Decision(decision="deny", reason=g.reason, risk_class="safe")
        risk, why = self.classify(a)
        # 2. the policy table
        if risk == "blocked":
            return Decision(decision="deny", reason=why, risk_class=risk)
        if risk == "safe":
            return Decision(decision="allow", reason=why, risk_class=risk)
        if risk == "experiment_changing":
            if a.author == "human":
                return Decision(decision="allow", reason="a human made this change", risk_class=risk)
            reason = f"{a.author}-authored change may alter the experiment: {why}"
            return Decision(decision="needs_approval", reason=reason, risk_class=risk)
        # external
        return Decision(decision="needs_approval", reason=why, risk_class=risk)
