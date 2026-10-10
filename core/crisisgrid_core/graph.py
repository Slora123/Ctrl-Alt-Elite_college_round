"""Experiment graph service (system_design.md 5.1): canonicalize, hash, validate, create_version, diff, downstream.

Pure functions over `ExperimentGraph`; persistence is someone else's job (GraphRepo).

Hashing rule: a node's content_hash covers only semantic content (type, spec, input hashes, env digest and, if a cell is
bound, the normalized cell source). Comments and whitespace never change a hash, so a cosmetic cell edit does not
invalidate downstream results while a real code edit does.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from typing import Any, Iterable

from pydantic import BaseModel, Field

from .ids import new_id
from .models import AUTHOR_TO_PROVENANCE, ExperimentGraph, GraphNode

__all__ = [
    "canonicalize", "normalize_source", "semantic_source_hash", "hash_node", "compute_hashes", "validate",
    "create_version", "diff", "NodeEdit", "downstream", "topological_order",
    "GraphError", "GraphValidationError", "ValidationIssue", "NodeChange", "GraphDiff",
]


class GraphError(ValueError):
    pass


class ValidationIssue(BaseModel):
    code: str
    node_id: str | None = None
    message: str


class GraphValidationError(GraphError):
    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        super().__init__("; ".join(f"{i.code}: {i.message}" for i in issues))


# ---- canonicalization ---------------------------------------------------------------------------------------------

def _plain(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise GraphError(f"non-finite float in spec: {value!r}")
        return int(value) if value.is_integer() else value  # 1.0 and 1 are the same configuration
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    raise GraphError(f"unsupported type in spec: {type(value).__name__}")


def canonicalize(spec: dict[str, Any]) -> str:
    """Deterministic JSON: sorted keys, no whitespace, integral floats folded to ints."""
    return json.dumps(_plain(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_COMMENT = re.compile(r"(?m)^\s*#.*$|\s+#.*$")


def normalize_source(source: str) -> str:
    """Code AST when it parses (ignores comments, whitespace, formatting); otherwise a comment/whitespace-stripped
    fallback (notebook magics such as `!pip` or `%matplotlib` are not valid Python)."""
    try:
        return ast.dump(ast.parse(source))
    except SyntaxError:
        lines = (re.sub(r"\s+", " ", _COMMENT.sub("", ln)).strip() for ln in source.splitlines())
        return "\n".join(ln for ln in lines if ln)


def semantic_source_hash(source: str) -> str:
    return hashlib.sha256(normalize_source(source).encode("utf-8")).hexdigest()


def hash_node(node: GraphNode, input_hashes: list[str], env_digest: str | None, cell_source: str | None = None) -> str:
    payload = {
        "type": node.type,
        "spec": json.loads(canonicalize(node.spec)),
        "inputs": input_hashes,
        "env": env_digest,
        "cell": semantic_source_hash(cell_source) if cell_source is not None else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# ---- structure ----------------------------------------------------------------------------------------------------

def topological_order(graph: ExperimentGraph) -> list[GraphNode]:
    """Inputs before dependents; ties keep graph order. Raises GraphError on cycles or dangling inputs."""
    by_id = {n.id: n for n in graph.nodes}
    state: dict[str, int] = {}
    order: list[GraphNode] = []

    def visit(nid: str, path: tuple[str, ...]) -> None:
        if state.get(nid) == 2:
            return
        if state.get(nid) == 1:
            raise GraphError("cycle: " + " -> ".join((*path, nid)))
        if nid not in by_id:
            raise GraphError(f"unknown input node: {nid}")
        state[nid] = 1
        for dep in by_id[nid].inputs:
            visit(dep, (*path, nid))
        state[nid] = 2
        order.append(by_id[nid])

    for n in graph.nodes:
        visit(n.id, ())
    return order


def validate(graph: ExperimentGraph) -> list[ValidationIssue]:
    """Structural checks. Returns every issue found (empty list means valid)."""
    issues: list[ValidationIssue] = []
    seen: set[str] = set()
    for n in graph.nodes:
        if n.id in seen:
            issues.append(ValidationIssue(code="duplicate_id", node_id=n.id, message=f"duplicate node id {n.id}"))
        seen.add(n.id)
    ids = {n.id for n in graph.nodes}
    consumers: dict[str, list[str]] = {i: [] for i in ids}
    for n in graph.nodes:
        for dep in n.inputs:
            if dep == n.id:
                issues.append(ValidationIssue(code="self_input", node_id=n.id, message=f"{n.id} lists itself as input"))
            elif dep not in ids:
                issues.append(ValidationIssue(code="missing_input", node_id=n.id, message=f"{n.id} reads unknown node {dep}"))
            else:
                consumers[dep].append(n.id)
        if len(set(n.inputs)) != len(n.inputs):
            issues.append(ValidationIssue(code="duplicate_input", node_id=n.id, message=f"{n.id} lists an input twice"))
        if n.type == "dataset" and n.inputs:
            issues.append(ValidationIssue(code="dataset_has_inputs", node_id=n.id, message="dataset nodes are sources"))
        if n.type != "dataset" and not n.inputs:
            issues.append(ValidationIssue(code="no_inputs", node_id=n.id, message=f"{n.type} node {n.id} has no inputs"))
        if n.provenance == "stated" and not n.sources:
            issues.append(ValidationIssue(code="stated_without_source", node_id=n.id,
                                          message="provenance 'stated' requires at least one Source"))
    for n in graph.nodes:
        if n.type == "claim" and consumers.get(n.id):
            issues.append(ValidationIssue(code="claim_not_sink", node_id=n.id, message="claim nodes must be sinks"))
    if not any(i.code in ("missing_input", "self_input") for i in issues):
        try:
            topological_order(graph)
        except GraphError as e:
            issues.append(ValidationIssue(code="cycle", message=str(e)))
    return issues


def compute_hashes(graph: ExperimentGraph, cell_sources: dict[str, str] | None = None) -> ExperimentGraph:
    """Return a copy with every node's content_hash filled in. `cell_sources` maps node_id -> bound cell source."""
    cell_sources = cell_sources or {}
    order = topological_order(graph)
    hashes: dict[str, str] = {}
    for n in order:
        hashes[n.id] = hash_node(n, [hashes[d] for d in n.inputs], graph.env_digest, cell_sources.get(n.id))
    return graph.model_copy(update={"nodes": [n.model_copy(update={"content_hash": hashes[n.id]}) for n in graph.nodes]},
                            deep=True)


def downstream(graph: ExperimentGraph, node_id: str) -> list[str]:
    """Transitive dependents of node_id (excluding itself), in topological order."""
    graph.node(node_id)  # KeyError if unknown
    hit = {node_id}
    out: list[str] = []
    for n in topological_order(graph):
        if n.id != node_id and any(d in hit for d in n.inputs):
            hit.add(n.id)
            out.append(n.id)
    return out


# ---- versioning ---------------------------------------------------------------------------------------------------

class NodeEdit(BaseModel):
    """Partial edit of an existing node: `spec` is merged key-by-key, `inputs` (if given) replaces the list."""

    spec: dict[str, Any] | None = None
    inputs: list[str] | None = None


def create_version(
    graph: ExperimentGraph,
    node_edits: dict[str, NodeEdit | dict[str, Any]] | None = None,
    *,
    created_by: str,
    change_note: str | None = None,
    add_nodes: Iterable[GraphNode] = (),
    remove_node_ids: Iterable[str] = (),
    env_digest: str | None = None,
    cell_sources: dict[str, str] | None = None,
) -> ExperimentGraph:
    """New immutable version (version+1, parent_version set, new id). Edited nodes take provenance from the author
    (human -> human, agent -> inferred). Raises GraphValidationError if the result is not a valid graph."""
    if created_by not in ("agent", "human"):
        raise GraphError("created_by must be 'agent' or 'human'")
    edits = {k: (v if isinstance(v, NodeEdit) else NodeEdit(**v)) for k, v in (node_edits or {}).items()}
    unknown = set(edits) - {n.id for n in graph.nodes}
    if unknown:
        raise GraphError(f"edits for unknown nodes: {sorted(unknown)}")
    removed = set(remove_node_ids)
    prov = AUTHOR_TO_PROVENANCE[created_by]

    nodes: list[GraphNode] = []
    for n in graph.nodes:
        if n.id in removed:
            continue
        n = n.model_copy(deep=True)
        e = edits.get(n.id)
        if e:
            if e.spec is not None:
                n.spec = {**n.spec, **e.spec}
            if e.inputs is not None:
                n.inputs = list(e.inputs)
            n.provenance = prov
        nodes.append(n)
    nodes.extend(a.model_copy(deep=True) for a in add_nodes)

    new = ExperimentGraph(
        id=new_id(), paper_id=graph.paper_id, version=graph.version + 1, parent_version=graph.version, nodes=nodes,
        env_digest=env_digest if env_digest is not None else graph.env_digest, created_by=created_by,  # type: ignore[arg-type]
        change_note=change_note,
    )
    issues = validate(new)
    if issues:
        raise GraphValidationError(issues)
    return compute_hashes(new, cell_sources)


class NodeChange(BaseModel):
    node_id: str
    fields: list[str] = Field(default_factory=list)  # which of type/spec/inputs/provenance changed
    spec_changes: dict[str, tuple[Any, Any]] = Field(default_factory=dict)  # key -> (old, new); None means absent


class GraphDiff(BaseModel):
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    modified: list[NodeChange] = Field(default_factory=list)  # the node itself was edited
    invalidated: list[str] = Field(default_factory=list)  # hash changed, directly or through an upstream change
    env_changed: bool = False

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.modified or self.invalidated or self.env_changed)


def diff(a: ExperimentGraph, b: ExperimentGraph) -> GraphDiff:
    """What changed from a to b. `invalidated` is the set of nodes whose results must be recomputed: everything whose
    content_hash differs (hashes are recomputed here if a graph carries none, so un-hashed graphs diff correctly)."""
    ha = {n.id: n.content_hash for n in (a if all(n.content_hash for n in a.nodes) else compute_hashes(a)).nodes}
    hb = {n.id: n.content_hash for n in (b if all(n.content_hash for n in b.nodes) else compute_hashes(b)).nodes}
    na = {n.id: n for n in a.nodes}
    nb = {n.id: n for n in b.nodes}
    out = GraphDiff(
        added=[i for i in nb if i not in na],
        removed=[i for i in na if i not in nb],
        env_changed=a.env_digest != b.env_digest,
    )
    for nid in nb:
        if nid not in na:
            continue
        x, y = na[nid], nb[nid]
        fields = [f for f in ("type", "inputs", "provenance") if getattr(x, f) != getattr(y, f)]
        spec_changes = {k: (x.spec.get(k), y.spec.get(k)) for k in sorted(set(x.spec) | set(y.spec))
                        if canonicalize({"v": x.spec.get(k)}) != canonicalize({"v": y.spec.get(k)})}
        if spec_changes:
            fields.append("spec")
        if fields:
            out.modified.append(NodeChange(node_id=nid, fields=fields, spec_changes=spec_changes))
        if ha[nid] != hb[nid]:
            out.invalidated.append(nid)
    return out
