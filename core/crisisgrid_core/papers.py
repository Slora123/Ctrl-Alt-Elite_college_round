"""Tier 1 data models: what the Paper parser extracts and the Artifact finder inventories, plus the rule that keeps
the parser honest (every `stated` fact needs a quote that really occurs in the paper)."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .models import Claim, Provenance, Source

__all__ = [
    "ExtractedValue", "DatasetRef", "MetricDef", "Contradiction", "ParsedPaper", "RepoInfo", "ArtifactInventory",
    "normalize_text", "quote_in_text", "enforce_sources",
]


class ExtractedValue(BaseModel):
    value: str | float | int | bool
    provenance: Provenance = "stated"
    source: Source | None = None


class DatasetRef(BaseModel):
    name: str
    split: str | None = None  # how the paper defines the split
    split_provenance: Literal["paper_official", "approximate", "unspecified"] = "unspecified"
    url: str | None = None
    access: Literal["public", "gated", "private", "unknown"] = "unknown"
    source: Source | None = None


class MetricDef(BaseModel):
    name: str
    definition: str | None = None
    source: Source | None = None


class Contradiction(BaseModel):
    """Two places in the paper disagree. Both spans are kept; the parser never resolves it silently."""

    field: str
    a: ExtractedValue
    b: ExtractedValue


class ParsedPaper(BaseModel):
    title: str = ""
    published: date | None = None
    claims: list[Claim] = Field(default_factory=list)
    hyperparameters: dict[str, ExtractedValue] = Field(default_factory=dict)  # keys: learning_rate, batch_size, ...
    datasets: list[DatasetRef] = Field(default_factory=list)
    metrics: list[MetricDef] = Field(default_factory=list)
    seeds: ExtractedValue | None = None
    n_runs_reported: int | None = None
    reports_variance: bool | None = None
    hardware: ExtractedValue | None = None
    contradictions: list[Contradiction] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)  # items removed because their quote could not be verified

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, d: object) -> object:
        """`null` means "the paper does not say": omit it instead of failing the whole extraction (and never invent a value)."""
        if not isinstance(d, dict):
            return d
        d = dict(d)
        hp = d.get("hyperparameters")
        if isinstance(hp, dict):
            d["hyperparameters"] = {k: v for k, v in hp.items() if not (v is None or (isinstance(v, dict) and v.get("value") is None))}
        for k in ("seeds", "hardware"):
            if isinstance(d.get(k), dict) and d[k].get("value") is None:
                d[k] = None
        return d


class RepoInfo(BaseModel):
    url: str
    commit: str | None = None  # pinned at ingestion
    default_branch: str | None = None
    default_branch_last_commit: datetime | None = None
    dependencies_pinned: bool | None = None
    has_readme: bool = False


class ArtifactInventory(BaseModel):
    repo: RepoInfo | None = None
    repo_candidates: list[str] = Field(default_factory=list)
    datasets: list[DatasetRef] = Field(default_factory=list)
    weights_urls: list[str] = Field(default_factory=list)
    missing_components: list[str] = Field(default_factory=list)
    branch_postdates_paper: bool | None = None
    snapshot_hash: str | None = None


# ---- quote verification -------------------------------------------------------------------------------------------

def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"-\s*\n\s*", "", s)  # de-hyphenate line breaks
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("−", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def _loose(s: str) -> str:
    # PDF extraction splits words at line ends (pre- / trained), so ignore hyphens and spaces when matching
    return normalize_text(s).replace("-", "").replace(" ", "")


def quote_in_text(quote: str, text: str) -> bool:
    q = _loose(quote)
    return bool(q) and q in _loose(text)


def _check(src: Source | None, text: str) -> Literal["verified", "missing", "fabricated"]:
    if src is None or not (src.quote or "").strip():
        return "missing"
    return "verified" if quote_in_text(src.quote or "", text) else "fabricated"


def enforce_sources(parsed: ParsedPaper, text: str) -> ParsedPaper:
    """No hallucinated facts. A `stated` value must carry a quote found verbatim in the paper:
    - quote present but not found in the paper -> the item is dropped (and listed in `dropped`)
    - no quote at all                          -> kept, but downgraded to provenance 'inferred'
    `human` provenance is never touched."""
    out = parsed.model_copy(deep=True)
    out.dropped = list(out.dropped)

    def judge(label: str, prov: Provenance, src: Source | None) -> Literal["keep", "inferred", "drop"]:
        if prov != "stated":
            return "keep"
        c = _check(src, text)
        if c == "verified":
            return "keep"
        if c == "missing":
            return "inferred"
        out.dropped.append(f"{label}: quote not found in paper")
        return "drop"

    claims = []
    for c in out.claims:
        j = judge(f"claim '{c.text[:60]}'", c.provenance, c.source)
        if j == "drop":
            continue
        claims.append(c.model_copy(update={"provenance": "inferred"}) if j == "inferred" else c)
    out.claims = claims

    def fix(label: str, v: ExtractedValue | None) -> ExtractedValue | None:
        if v is None:
            return None
        j = judge(label, v.provenance, v.source)
        if j == "drop":
            return None
        return v.model_copy(update={"provenance": "inferred"}) if j == "inferred" else v

    hp = {}
    for k, v in out.hyperparameters.items():
        nv = fix(f"hyperparameter {k}", v)
        if nv is not None:
            hp[k] = nv
    out.hyperparameters = hp
    out.seeds = fix("seeds", out.seeds)
    out.hardware = fix("hardware", out.hardware)

    # an item with no source is still a plausible extraction (just unverifiable); only a fabricated quote drops it
    out.datasets = [d for d in out.datasets if judge(f"dataset {d.name}", "stated", d.source) != "drop"]
    out.metrics = [m for m in out.metrics if judge(f"metric {m.name}", "stated", m.source) != "drop"]
    kept = []
    for ct in out.contradictions:
        # a contradiction is only meaningful if BOTH spans are real
        if all(_check(x.source, text) == "verified" for x in (ct.a, ct.b)):
            kept.append(ct)
        else:
            out.dropped.append(f"contradiction {ct.field}: span not found in paper")
    out.contradictions = kept
    return out
