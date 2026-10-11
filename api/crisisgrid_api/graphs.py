"""Experiment graphs for a paper: built deterministically from what the parser extracted (no LLM), with the run's notebook
cells bound to the node they implement. Versions are immutable; a human edit makes a new version, and two versions can be
compared (structural diff + attribution of a metric change, when both versions have clean runs)."""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from crisisgrid_core.attribution import attribute
from crisisgrid_core.graph import GraphError, GraphValidationError, compute_hashes, create_version, diff, validate
from crisisgrid_core.models import ExperimentGraph, GraphNode, RunResult, Source
from crisisgrid_core.papers import ParsedPaper

from .auth.models import User
from .db.graph_repo import SqlGraphRepo
from .orchestration.models import RunState

MODEL_KEYS = re.compile(r"layer|hidden|head|dim|d_model|dropout|width|depth|embed|vocab|kernel|channel|arch|model", re.I)
CELL_RULES = [  # first match wins; order matters (report_metric lives in eval cells that may also mention "model")
    ("eval", re.compile(r"report_metric|evaluat|accuracy|f1|bleu|test_loss|predict", re.I)),
    ("train", re.compile(r"\.fit\(|\bepoch|optimi[sz]er|\.backward\(|\btrain", re.I)),
    ("model", re.compile(r"\bclass \w+\(|nn\.Module|model\s*=|Classifier\(|Regressor\(|Sequential\(", re.I)),
    ("preprocess", re.compile(r"tokeni|normali|scale|split|transform|preprocess|train_test_split", re.I)),
    ("dataset", re.compile(r"load_|dataset|read_csv|FakeData|make_classification|fetch_", re.I)),
]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "x"


def _src(s: Source | None) -> list[Source]:
    return [s] if s is not None else []


def build_graph(paper_id: str, parsed: ParsedPaper, cells: list[dict[str, Any]] | None) -> tuple[ExperimentGraph, list[dict[str, Any]]]:
    nodes: list[GraphNode] = []
    data_ids: list[str] = []
    for d in parsed.datasets or []:
        nid = f"data:{_slug(d.name)}"
        if nid in data_ids:
            continue
        data_ids.append(nid)
        nodes.append(GraphNode(id=nid, type="dataset", spec={"name": d.name, "split": d.split, "access": d.access, "url": d.url},
                               provenance="stated" if d.source else "inferred", sources=_src(d.source)))
    if not data_ids:
        names = sorted({c.dataset for c in parsed.claims if c.dataset}) or ["unspecified"]
        for n in names:
            data_ids.append(f"data:{_slug(n)}")
            nodes.append(GraphNode(id=data_ids[-1], type="dataset", spec={"name": n}, provenance="inferred"))

    hps = parsed.hyperparameters or {}
    model_hp = {k: v for k, v in hps.items() if MODEL_KEYS.search(k)}
    train_hp = {k: v for k, v in hps.items() if k not in model_hp}

    def hp_node(nid: str, typ: str, hp: dict[str, Any], inputs: list[str], extra: dict[str, Any] | None = None) -> GraphNode:
        srcs = [v.source for v in hp.values() if v.source is not None][:6]
        stated = any(v.provenance == "stated" for v in hp.values()) and srcs
        return GraphNode(id=nid, type=typ, spec={**{k: v.value for k, v in hp.items()}, **(extra or {})}, inputs=inputs,  # type: ignore[arg-type]
                         provenance="stated" if stated else "inferred", sources=srcs if stated else [])

    splits = {d.name: d.split for d in parsed.datasets if d.split}
    nodes.append(GraphNode(id="preprocess", type="preprocess", spec={"splits": splits} if splits else {}, inputs=list(data_ids),
                           provenance="inferred"))
    nodes.append(hp_node("model", "model", model_hp, ["preprocess"]))
    extra = {"seeds": parsed.seeds.value} if parsed.seeds else {}
    if parsed.hardware:
        extra["hardware"] = parsed.hardware.value
    nodes.append(hp_node("train", "train", train_hp, ["model", "preprocess"], extra))
    msrc = [m.source for m in parsed.metrics if m.source][:6]
    nodes.append(GraphNode(id="eval", type="eval", spec={"metrics": {m.name: m.definition for m in parsed.metrics}}, inputs=["train"],
                           provenance="stated" if msrc else "inferred", sources=msrc))
    for i, c in enumerate(parsed.claims):
        nodes.append(GraphNode(id=f"claim:{i + 1}", type="claim", inputs=["eval"], provenance="stated", sources=[c.source],
                               spec={"text": c.text, "metric": c.metric, "dataset": c.dataset, "split": c.split,
                                     "reported_value": c.reported_value, "unit": c.unit, "claim_id": c.id}))

    bound = bind_cells(cells or [], data_ids[0])
    g = ExperimentGraph(paper_id=paper_id, nodes=nodes, created_by="agent", change_note="built from the paper's extracted setup")
    issues = validate(g)
    if issues:  # the builder is deterministic, so this is a bug, not bad input
        raise GraphValidationError(issues)
    return compute_hashes(g, cell_text(bound)), bound


def bind_cells(cells: list[dict[str, Any]], first_dataset: str) -> list[dict[str, Any]]:
    out = []
    for c in cells:
        src = str(c.get("source", ""))
        if c.get("kind", "code") != "code":
            continue
        node = c.get("node_id")
        if node is None:
            node = next((n for n, rx in CELL_RULES if rx.search(src)), None)
            node = first_dataset if node == "dataset" else node  # None = setup (imports, seeding)
        out.append({"node_id": node, "source": src})
    return out


def cell_text(cells: list[dict[str, Any]]) -> dict[str, str]:
    by: dict[str, list[str]] = {}
    for c in cells:
        if c.get("node_id"):
            by.setdefault(c["node_id"], []).append(c["source"])
    return {k: "\n\n".join(v) for k, v in by.items()}


class NewVersion(BaseModel):
    base: int
    edits: dict[str, dict[str, Any]] = Field(default_factory=dict)  # node_id -> spec keys to set
    cells: list[dict[str, Any]] | None = None  # full replacement [{node_id, source}]
    note: str | None = Field(default=None, max_length=500)


def build_graph_router(c: Any, repo: SqlGraphRepo, paper_for: Any, orch: Any) -> APIRouter:
    r = APIRouter()
    cur, writer = c.auth_deps.current_user, c.auth_deps.writer

    def runs_with_setup(paper_id: str) -> list[RunState]:
        return [s for s in c.runs.list(paper_id) if s.parsed is not None]

    def ensure(paper_id: str) -> list[dict[str, Any]]:
        versions = repo.list(paper_id)
        runs = runs_with_setup(paper_id)
        if not runs:
            return versions
        latest = runs[-1]
        if not versions:
            g, bound = build_graph(paper_id, latest.parsed, latest.notebook)
            repo.add(paper_id, 1, None, "agent", g.change_note, g.model_dump(mode="json"), bound)
            if latest.notebook:
                repo.add_run(paper_id, 1, latest.run_id)
            return repo.list(paper_id)
        v1 = versions[0]
        if not v1["cells"] and latest.notebook:  # the notebook arrived after the graph was first built
            g = ExperimentGraph.model_validate(v1["graph"])
            bound = bind_cells(latest.notebook, next(n.id for n in g.nodes if n.type == "dataset"))
            repo.set_cells(paper_id, 1, bound, compute_hashes(g, cell_text(bound)).model_dump(mode="json"))
            if latest.run_id not in v1["run_ids"]:
                repo.add_run(paper_id, 1, latest.run_id)
            versions = repo.list(paper_id)
        return versions

    def results(v: dict[str, Any]) -> tuple[list[RunResult], list[dict[str, Any]]]:
        out, runs = [], []
        for rid in v["run_ids"]:
            s = c.runs.get(rid)
            if s is None:
                continue
            ok = [x for x in s.run_results if x.status in ("ok", "scaled_down")]
            out += ok
            means: dict[str, list[float]] = {}
            for x in ok:
                for k, val in x.metrics.items():
                    means.setdefault(k, []).append(val)
            hw = next((x.hardware_info for x in ok if x.hardware_info), {})
            ran_on = hw.get("gpu") or (f"CPU ({hw.get('platform', '').split('-')[0]})" if hw else None)
            runs.append({"run_id": rid, "status": s.status, "node": s.node, "n_ok": len(ok), "ran_on": ran_on,
                         "metrics": {k: sum(vs) / len(vs) for k, vs in means.items()},
                         "verdicts": [x.model_dump(mode="json") for x in s.verdicts]})
        return out, runs

    def brief(v: dict[str, Any]) -> dict[str, Any]:
        return {k: v[k] for k in ("version", "parent_version", "created_by", "change_note", "created_at")} | {
            "n_nodes": len(v["graph"]["nodes"]), "n_cells": len(v["cells"]), "n_runs": len(v["run_ids"])}

    def get_v(paper_id: str, version: int) -> dict[str, Any]:
        v = repo.get(paper_id, version)
        if v is None:
            raise HTTPException(404, f"graph version {version} not found")
        return v

    @r.get("/papers/{paper_id}/graph")
    def graph(paper_id: str, version: int | None = None, user: User = Depends(cur)) -> dict[str, Any]:
        paper_for(user, paper_id)
        versions = ensure(paper_id)
        if not versions:
            return {"versions": [], "current": None, "note": "run the pipeline until the paper is understood to build its graph"}
        v = get_v(paper_id, version) if version else versions[-1]
        _, runs = results(v)
        return {"versions": [brief(x) for x in versions], "current": {**v, "runs": runs}}

    @r.post("/papers/{paper_id}/graph/versions", status_code=201)
    def new_version(paper_id: str, body: NewVersion, user: User = Depends(writer)) -> dict[str, Any]:
        paper_for(user, paper_id)
        ensure(paper_id)
        base = get_v(paper_id, body.base)
        g = ExperimentGraph.model_validate(base["graph"])
        latest = repo.list(paper_id)[-1]["version"]
        g = g.model_copy(update={"version": latest})  # new version number = latest + 1, parent = the base the human edited
        cells = body.cells if body.cells is not None else base["cells"]
        ids = {n.id for n in g.nodes}
        cells = [{"node_id": x.get("node_id") if x.get("node_id") in ids else None, "source": str(x.get("source", ""))}
                 for x in cells if str(x.get("source", "")).strip()]
        if not body.edits and cells == base["cells"]:
            raise HTTPException(422, "nothing changed")
        try:
            # a node whose bound code changed is an edit too, so its provenance becomes 'human'
            code_changed = {n for n in set(cell_text(cells)) ^ set(cell_text(base["cells"]))} | {
                n for n, t in cell_text(cells).items() if cell_text(base["cells"]).get(n) != t}
            edits = {n: {"spec": body.edits.get(n, {})} for n in (set(body.edits) | code_changed) if n in ids}
            ng = create_version(g, edits, created_by="human", change_note=body.note, cell_sources=cell_text(cells))
        except GraphValidationError as e:
            raise HTTPException(422, "; ".join(i.message for i in e.issues) if hasattr(e, "issues") else str(e)) from e
        except GraphError as e:
            raise HTTPException(422, str(e)) from e
        ng = ng.model_copy(update={"parent_version": body.base})
        row = repo.add(paper_id, ng.version, body.base, "human", body.note, ng.model_dump(mode="json"), cells)
        return brief(row)

    @r.get("/papers/{paper_id}/graph/compare")
    def compare(paper_id: str, a: int, b: int, user: User = Depends(cur)) -> dict[str, Any]:
        paper_for(user, paper_id)
        va, vb = get_v(paper_id, a), get_v(paper_id, b)
        ga, gb = ExperimentGraph.model_validate(va["graph"]), ExperimentGraph.model_validate(vb["graph"])
        d = diff(ga, gb)
        ra, runs_a = results(va)
        rb, runs_b = results(vb)
        ca, cb = cell_text(va["cells"]), cell_text(vb["cells"])
        code = [{"node_id": n, "before": ca.get(n, ""), "after": cb.get(n, "")} for n in sorted(set(ca) | set(cb)) if ca.get(n) != cb.get(n)]
        metrics = sorted({k for x in ra + rb for k in x.metrics})
        attr = [attribute(ga, gb, m, ra, rb).model_dump(mode="json") for m in metrics]
        return {"a": brief(va), "b": brief(vb), "diff": d.model_dump(mode="json"), "code_changes": code,
                "runs_a": runs_a, "runs_b": runs_b, "attribution": attr}

    @r.post("/papers/{paper_id}/graph/versions/{version}/run", status_code=202)
    def run_version(paper_id: str, version: int, bg: BackgroundTasks, user: User = Depends(writer)) -> dict[str, Any]:
        paper_for(user, paper_id)
        v = get_v(paper_id, version)
        if not v["cells"]:
            raise HTTPException(422, "this version has no notebook cells to run")
        o = orch()
        prev = runs_with_setup(paper_id)
        state = o.start(paper_id, notebook=[{"kind": "code", "source": x["source"]} for x in v["cells"]])
        if prev:  # carry the packages and the 'system-written' label over from the run that produced the notebook
            state.requirements, state.generated, state.scaled_down = prev[-1].requirements, prev[-1].generated, prev[-1].scaled_down
            c.runs.save(state)
        repo.add_run(paper_id, version, state.run_id)
        bg.add_task(o.advance, state.run_id)
        return {"run_id": state.run_id, "version": version}

    return r
