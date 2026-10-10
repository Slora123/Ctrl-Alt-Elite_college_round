"""Audit rules engine (system_design 5.4): deterministic methodology checks R01-R16, plus merge with agent gaps.

R12-R16 come from system_design.md. R01-R11 were "unchanged from v1", which was not available, so they are defined here
from what the other documents imply (see docs/decisions.md D-16). Text rules are conservative regex heuristics: they
raise a flag with the matching quote as evidence, they never decide a verdict. Only rules (never agents) may emit
`blocking`.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .models import Gap
from .papers import ArtifactInventory, ParsedPaper

__all__ = ["AuditInput", "RULES", "run_rules", "merge_gaps", "REQUIRED_HYPERPARAMETERS"]

REQUIRED_HYPERPARAMETERS = ("learning_rate", "batch_size", "epochs", "optimizer")


@dataclass
class AuditInput:
    parsed: ParsedPaper
    inventory: ArtifactInventory
    text: str

    @property
    def low(self) -> str:
        return " ".join(self.text.lower().split())


def _gap(rule: str, name: str, severity: str, description: str, fix: str | None = None) -> Gap:
    return Gap(field=f"{rule}:{name}", severity=severity, description=description, suggested_resolution=fix,  # type: ignore[arg-type]
               detected_by="rule")


def _snippet(rx: re.Pattern[str], low: str) -> str | None:
    m = rx.search(low)
    return None if not m else low[max(0, m.start() - 40): m.end() + 40].strip()


Rule = Callable[[AuditInput], list[Gap]]


def r01_seed(i: AuditInput) -> list[Gap]:
    if i.parsed.seeds is None:
        return [_gap("R01", "random_seed", "minor", "No random seed is stated.", "Fix and report seeds; clean runs use a fixed seed.")]
    return []


def r02_hyperparameters(i: AuditInput) -> list[Gap]:
    missing = [h for h in REQUIRED_HYPERPARAMETERS if h not in i.parsed.hyperparameters]
    if not missing:
        return []
    sev = "major" if len(missing) >= 2 else "minor"
    return [_gap("R02", "hyperparameters", sev, f"Missing hyperparameters: {', '.join(missing)}.",
                 "Take from the repo config or ask the user; do not guess.")]


def r03_split(i: AuditInput) -> list[Gap]:
    out = []
    for d in i.parsed.datasets:
        if d.split_provenance == "unspecified":
            out.append(_gap("R03", f"split:{d.name}", "major", f"Train/val/test split of '{d.name}' is not defined.",
                            "Use the official split if one exists; otherwise label results approximate_split."))
        elif d.split_provenance == "approximate":
            out.append(_gap("R03", f"split:{d.name}", "minor", f"Split of '{d.name}' is approximate, not the paper's own.",
                            "Verdict will be capped at partial."))
    return out


_AMBIGUOUS = {
    "accuracy": re.compile(r"top-?\s*[15]|top-?k|per-?class|balanced"),
    "f1": re.compile(r"micro|macro|weighted|binary"),
    "precision": re.compile(r"micro|macro|weighted|binary"),
    "recall": re.compile(r"micro|macro|weighted|binary"),
}


def r04_metric(i: AuditInput) -> list[Gap]:
    out = []
    if not i.parsed.metrics:
        out.append(_gap("R04", "metric_undefined", "major", "No evaluation metric is defined.", "Ask the user which metric the claim uses."))
    for m in i.parsed.metrics:
        key = m.name.lower().strip()
        for base, rx in _AMBIGUOUS.items():
            if key == base and not rx.search((m.definition or "").lower()) and not rx.search(i.low):
                out.append(_gap("R04", f"metric_ambiguous:{key}", "major",
                                f"Metric '{m.name}' is ambiguous (e.g. top-1 vs top-5, micro vs macro) and not defined.",
                                "Confirm the exact definition before comparing numbers."))
    return out


def r05_compute(i: AuditInput) -> list[Gap]:
    if i.parsed.hardware is None:
        return [_gap("R05", "compute", "minor", "Hardware / training time is not reported.", "Compute guard will estimate and label scaling.")]
    return []


def r06_code(i: AuditInput) -> list[Gap]:
    if i.inventory.repo is None:
        return [_gap("R06", "code_unavailable", "major", "No code repository was found for this paper.",
                     "Tier 3 (rebuild from the paper, labelled directional) or ask the user for the repo.")]
    return []


def r07_data(i: AuditInput) -> list[Gap]:
    out = []
    for d in [*i.parsed.datasets, *i.inventory.datasets]:
        if d.access == "private":
            out.append(_gap("R07", f"dataset_private:{d.name}", "blocking", f"Dataset '{d.name}' is private or unavailable.",
                            "Upload the data, substitute (labelled 'not a reproduction'), or stop at Tier 1."))
        elif d.access == "unknown" and not d.url:
            out.append(_gap("R07", f"dataset_source:{d.name}", "major", f"No source is known for dataset '{d.name}'.",
                            "Provide a download source."))
    return list({g.field: g for g in out}.values())


def r08_dependencies(i: AuditInput) -> list[Gap]:
    r = i.inventory.repo
    if r is None:
        return []
    if r.dependencies_pinned is False:
        return [_gap("R08", "deps_unpinned", "major", "Repo dependencies are not pinned.",
                     "Env builder resolves versions by the paper's publication date (labelled inferred).")]
    if r.dependencies_pinned is None:
        return [_gap("R08", "deps_unknown", "minor", "Could not determine whether dependencies are pinned.")]
    return []


def r09_variance(i: AuditInput) -> list[Gap]:
    p = i.parsed
    if p.reports_variance is False or (p.n_runs_reported is not None and p.n_runs_reported <= 1):
        return [_gap("R09", "no_variance", "minor", "Results come from a single run, with no variance reported.",
                     "Verdict tolerance will rely on our own seed variance.")]
    return []


_PREPROC = re.compile(r"preprocess|augmentation|normaliz|tokeniz|resiz|crop|standardiz")


def r10_preprocessing(i: AuditInput) -> list[Gap]:
    if not _PREPROC.search(i.low):
        return [_gap("R10", "preprocessing", "minor", "Preprocessing / augmentation is not described.")]
    return []


_PRETRAINED = re.compile(r"pre-?trained|fine-?tun|initiali[sz]ed from|checkpoint")


def r11_weights(i: AuditInput) -> list[Gap]:
    if _PRETRAINED.search(i.low) and not i.inventory.weights_urls:
        return [_gap("R11", "weights_source", "major", "The paper relies on pretrained weights but no source was found.",
                     "Locate the exact checkpoint or note that training from scratch is not equivalent.")]
    return []


_BEST_SEED = re.compile(r"best of \d+|best (?:run|seed|trial|result)s?\b|(?:report|show)(?:ed|s)? the best\b|over \d+ (?:runs|seeds|trials)[^.]{0,60}\bbest\b")
_TEST_SEL = re.compile(r"(?:select|choos|tun|pick|early[- ]stop|stopp)\w*[^.]{0,80}\b(?:on|using|by|with)\b[^.]{0,25}\btest (?:set|data|accuracy|split|performance)")
_BASELINE_COPY = re.compile(r"(?:baseline|competing|compared)[^.]{0,80}(?:taken|copied|quoted|reported) (?:from|in)|(?:taken|copied|quoted) from[^.]{0,60}(?:baseline|original paper|prior work)|results? (?:of|for) (?:the )?baselines? (?:are|is) (?:taken|from)")
_EXTERNAL = re.compile(r"\bgpt-?[34]|chatgpt|openai api|claude|gemini|human (?:evaluation|annotat\w+|raters?|judges?)|mechanical turk|mturk|crowd-?sourc|proprietary")


def r12_best_seed(i: AuditInput) -> list[Gap]:
    s = _snippet(_BEST_SEED, i.low)
    return [_gap("R12", "best_seed", "major", f"The paper appears to report the best of N runs: \"...{s}...\"",
                 "Compare against the mean and std across seeds.")] if s else []


def r13_test_selection(i: AuditInput) -> list[Gap]:
    s = _snippet(_TEST_SEL, i.low)
    return [_gap("R13", "test_selection", "major", f"Hyperparameters or a checkpoint may have been chosen on the test set: \"...{s}...\"",
                 "Use a validation split for selection; flag the claim as possibly inflated.")] if s else []


def r14_baseline_lineage(i: AuditInput) -> list[Gap]:
    s = _snippet(_BASELINE_COPY, i.low)
    return [_gap("R14", "baseline_lineage", "minor", f"Baseline numbers appear to be copied from other papers, not rerun: \"...{s}...\"")] if s else []


def r15_code_version(i: AuditInput) -> list[Gap]:
    out = []
    inv = i.inventory
    if inv.branch_postdates_paper:
        out.append(_gap("R15", "branch_postdates_paper", "minor", "The repo's default branch changed after the paper was published.",
                        "We pin the commit at ingestion, but it may not match the code the authors used."))
    if inv.repo is not None and inv.repo.commit is None:
        out.append(_gap("R15", "commit_unpinned", "minor", "The repo commit could not be pinned at ingestion."))
    return out


def r16_external_dependency(i: AuditInput) -> list[Gap]:
    s = _snippet(_EXTERNAL, i.low)
    return [_gap("R16", "external_dependency", "major", f"A proprietary API or human evaluation may be required: \"...{s}...\"",
                 "Mark non-executable parts; do not substitute silently.")] if s else []


RULES: dict[str, Rule] = {
    "R01": r01_seed, "R02": r02_hyperparameters, "R03": r03_split, "R04": r04_metric, "R05": r05_compute, "R06": r06_code,
    "R07": r07_data, "R08": r08_dependencies, "R09": r09_variance, "R10": r10_preprocessing, "R11": r11_weights,
    "R12": r12_best_seed, "R13": r13_test_selection, "R14": r14_baseline_lineage, "R15": r15_code_version,
    "R16": r16_external_dependency,
}


def run_rules(inp: AuditInput) -> list[Gap]:
    out: list[Gap] = []
    for rule in RULES.values():
        out.extend(rule(inp))
    return out


# ---- merge --------------------------------------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def merge_gaps(rule_gaps: list[Gap], agent_gaps: list[Gap]) -> list[Gap]:
    """Rules win. Agent gaps are capped at `major` (an agent never blocks alone) and dropped when they duplicate a rule
    gap (same field, or the same description once normalized). Agent fields are free text, so an agent gap whose field
    is a rule id ('R12') or starts with one is also treated as covered by that rule."""
    merged = list(rule_gaps)
    seen_fields = {_norm(g.field) for g in merged}
    seen_desc = {_norm(g.description) for g in merged}
    covered_rules = {g.field.split(":")[0].lower() for g in merged}
    for g in agent_gaps:
        f, d = _norm(g.field), _norm(g.description)
        rule_ref = re.match(r"\s*(r\d{2})\b", g.field.lower())
        if f in seen_fields or d in seen_desc or (rule_ref and rule_ref.group(1) in covered_rules):
            continue
        sev = "major" if g.severity == "blocking" else g.severity
        merged.append(g.model_copy(update={"severity": sev, "detected_by": "agent"}))
        seen_fields.add(f)
        seen_desc.add(d)
    order = {"blocking": 0, "major": 1, "minor": 2}
    return sorted(merged, key=lambda g: (order[g.severity], g.field))
