"""Artifact finder: repo, datasets, weights, dependencies. Deterministic link extraction + a fetcher that pins the repo
commit at ingestion time. (The 'agent' part is classification we can do with rules; no LLM call is needed yet.)"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Protocol

import httpx

from crisisgrid_core.papers import ArtifactInventory, DatasetRef, ParsedPaper, RepoInfo

_TRAIL = ".,;:)]}>\"'"
_GITHUB = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)", re.I)
_HF_DATASET = re.compile(r"(?:https?://)?huggingface\.co/datasets/([\w.-]+(?:/[\w.-]+)?)", re.I)
_HF_MODEL = re.compile(r"(?:https?://)?huggingface\.co/(?!datasets/|spaces/|docs/|papers/)([\w.-]+/[\w.-]+)", re.I)
_ZENODO = re.compile(r"(?:https?://)?zenodo\.org/records?/(\d+)", re.I)
_KAGGLE = re.compile(r"(?:https?://)?(?:www\.)?kaggle\.com/(?:datasets|competitions)/([\w./-]+)", re.I)
_WEIGHTS = re.compile(r"https?://[^\s)\]>\"']+\.(?:pt|pth|ckpt|safetensors|bin|h5)\b", re.I)
_PRETRAINED = re.compile(r"pre-?trained|fine-?tun|initiali[sz]ed from|released (?:the )?(?:weights|checkpoints?)", re.I)


class RepoFetcher(Protocol):
    def fetch(self, url: str) -> RepoInfo | None: ...


def _clean(s: str) -> str:
    s = s.rstrip(_TRAIL)
    return s[:-4] if s.endswith(".git") else s


def extract_links(text: str) -> dict[str, list[str]]:
    flat = re.sub(r"-\s*\n\s*", "", text)  # urls broken across PDF lines
    seen: set[str] = set()

    def uniq(items: list[str]) -> list[str]:
        out = []
        for i in items:
            if i.lower() not in seen:
                seen.add(i.lower())
                out.append(i)
        return out

    repos = uniq([f"https://github.com/{_clean(o)}/{_clean(r)}" for o, r in _GITHUB.findall(flat)])
    return {
        "repos": repos,
        "hf_datasets": uniq([f"https://huggingface.co/datasets/{_clean(m)}" for m in _HF_DATASET.findall(flat)]),
        "hf_models": uniq([f"https://huggingface.co/{_clean(m)}" for m in _HF_MODEL.findall(flat)]),
        "zenodo": uniq([f"https://zenodo.org/records/{m}" for m in _ZENODO.findall(flat)]),
        "kaggle": uniq([f"https://www.kaggle.com/datasets/{_clean(m)}" for m in _KAGGLE.findall(flat)]),
        "weights": uniq([_clean(m) for m in _WEIGHTS.findall(flat)]),
    }


def find_artifacts(text: str, parsed: ParsedPaper, fetcher: RepoFetcher, *, paper_date: date | None = None) -> ArtifactInventory:
    links = extract_links(text)
    inv = ArtifactInventory(repo_candidates=links["repos"])

    for url in links["repos"]:
        info = fetcher.fetch(url)
        if info is not None:
            inv.repo = info
            break

    ds = {d.name.lower(): d.model_copy() for d in parsed.datasets}
    known_urls = {(d.url or "").lower().rstrip("/") for d in ds.values() if d.url}  # the parser may already have found this dataset
    for url in links["hf_datasets"] + links["zenodo"]:
        if url.lower() not in known_urls:
            ds.setdefault(url.lower(), DatasetRef(name=url.rsplit("/", 1)[-1], url=url, access="public"))
    for url in links["kaggle"]:
        if url.lower() not in known_urls:
            ds.setdefault(url.lower(), DatasetRef(name=url.rsplit("/", 1)[-1], url=url, access="gated"))  # kaggle needs an account/key
    inv.datasets = list(ds.values())
    inv.weights_urls = links["hf_models"] + links["weights"]

    if inv.repo is not None:
        c = inv.repo.default_branch_last_commit
        if c is not None and paper_date is not None:
            inv.branch_postdates_paper = c.date() > paper_date
        if inv.repo.commit:
            inv.snapshot_hash = hashlib.sha256(f"{inv.repo.url}@{inv.repo.commit}".encode()).hexdigest()

    if inv.repo is None:
        inv.missing_components.append("code")
    if not inv.datasets or all(d.access == "unknown" and not d.url for d in inv.datasets):
        inv.missing_components.append("dataset")
    if _PRETRAINED.search(text) and not inv.weights_urls:
        inv.missing_components.append("weights")
    if inv.repo is not None and inv.repo.dependencies_pinned is False:
        inv.missing_components.append("pinned_dependencies")
    return inv


class GitHubFetcher:
    """GitHub REST adapter. Network-dependent: unit-tested with a mock transport only, never against the live API."""

    def __init__(self, client: httpx.Client | None = None, token: str | None = None) -> None:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._c = client or httpx.Client(timeout=15, headers=headers, follow_redirects=True)

    def fetch(self, url: str) -> RepoInfo | None:
        m = _GITHUB.search(url)
        if not m:
            return None
        owner, repo = m.group(1), _clean(m.group(2))
        try:
            meta = self._c.get(f"https://api.github.com/repos/{owner}/{repo}")
            if meta.status_code != 200:
                return None
            branch = meta.json().get("default_branch")
            commit = self._c.get(f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}")
            sha, when = None, None
            if commit.status_code == 200:
                j = commit.json()
                sha = j.get("sha")
                ts = (j.get("commit", {}).get("committer") or {}).get("date")
                when = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
            readme = self._c.get(f"https://api.github.com/repos/{owner}/{repo}/readme")
            pinned = None
            if sha:
                req = self._c.get(f"https://raw.githubusercontent.com/{owner}/{repo}/{sha}/requirements.txt")
                if req.status_code == 200:
                    lines = [ln.strip() for ln in req.text.splitlines() if ln.strip() and not ln.strip().startswith(("#", "-"))]
                    pinned = bool(lines) and all("==" in ln for ln in lines)
            return RepoInfo(url=f"https://github.com/{owner}/{repo}", commit=sha, default_branch=branch,
                            default_branch_last_commit=when, dependencies_pinned=pinned, has_readme=readme.status_code == 200)
        except (httpx.HTTPError, ValueError):
            return None
