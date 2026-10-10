import pytest

from crisisgrid_core.graph import (
    GraphError,
    GraphValidationError,
    NodeEdit,
    canonicalize,
    compute_hashes,
    create_version,
    diff,
    downstream,
    hash_node,
    normalize_source,
    validate,
)
from crisisgrid_core.models import ExperimentGraph, GraphNode, Source

SRC = Source(kind="paper", ref="p1", quote="we train for 90 epochs")


def N(id, type, inputs=(), spec=None, provenance="stated"):
    return GraphNode(id=id, type=type, inputs=list(inputs), spec=spec or {}, provenance=provenance, sources=[SRC])


def make_graph(**kw):
    nodes = [
        N("data", "dataset", spec={"name": "cifar10"}),
        N("prep", "preprocess", ["data"], {"norm": True}),
        N("model", "model", ["prep"], {"arch": "resnet20"}),
        N("train", "train", ["model", "prep"], {"lr": 0.1, "epochs": 90}),
        N("eval", "eval", ["train"], {"metric": "top1"}),
        N("claim", "claim", ["eval"], {"id": "c1"}),
    ]
    return compute_hashes(ExperimentGraph(paper_id="p1", nodes=nodes, created_by="agent", env_digest="env1", **kw))


# ---- canonicalize / hashing ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ({"x": 1, "y": 2}, {"y": 2, "x": 1}),
    ({"lr": 1.0}, {"lr": 1}),
    ({"a": {"b": [1, 2]}, "c": 3}, {"c": 3, "a": {"b": (1, 2)}}),
    ({"s": {3, 1, 2}}, {"s": [1, 2, 3]}),
])
def test_canonicalize_equal(a, b):
    assert canonicalize(a) == canonicalize(b)


@pytest.mark.parametrize("a,b", [
    ({"lr": 0.1}, {"lr": 0.10001}),
    ({"x": [1, 2]}, {"x": [2, 1]}),
    ({"x": "1"}, {"x": 1}),
])
def test_canonicalize_different(a, b):
    assert canonicalize(a) != canonicalize(b)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), object()])
def test_canonicalize_rejects_unhashable_values(bad):
    with pytest.raises(GraphError):
        canonicalize({"x": bad})


def test_hashing_is_deterministic_and_order_independent_for_spec():
    g1 = make_graph()
    g2 = compute_hashes(g1.model_copy(deep=True))
    assert [n.content_hash for n in g1.nodes] == [n.content_hash for n in g2.nodes]
    assert all(len(n.content_hash) == 64 for n in g1.nodes)
    n = g1.node("train")
    shuffled = n.model_copy(update={"spec": dict(reversed(list(n.spec.items())))})
    assert hash_node(shuffled, [], "env1") == hash_node(n, [], "env1")


def test_env_digest_changes_every_hash():
    a = make_graph()
    b = compute_hashes(a.model_copy(update={"env_digest": "env2"}, deep=True))
    assert all(x.content_hash != y.content_hash for x, y in zip(a.nodes, b.nodes))


def test_upstream_change_propagates_to_hash():
    a = make_graph()
    b = create_version(a, {"prep": {"spec": {"norm": False}}}, created_by="human")
    changed = {n.id for n, m in zip(a.nodes, b.nodes) if n.content_hash != m.content_hash}
    assert changed == {"prep", "model", "train", "eval", "claim"}


def test_cell_source_binding_ignores_comments_and_whitespace_but_not_code():
    n = N("train", "train", ["model"])
    h = lambda s: hash_node(n, ["h"], None, s)  # noqa: E731
    assert h("x = 1\n") == h("# set x\nx   =   1  # trailing\n")
    assert h("x = 1") != h("x = 2")
    assert h("x = 1") != hash_node(n, ["h"], None, None)


def test_normalize_source_handles_magics():
    assert normalize_source("!pip install  numpy # c\n") == normalize_source("!pip install numpy")
    assert normalize_source("!pip install numpy") != normalize_source("!pip install scipy")


def test_cell_edit_invalidates_downstream_only():
    g = make_graph()
    a = compute_hashes(g, {"train": "lr = 0.1"})
    b = compute_hashes(g, {"train": "lr = 0.2"})
    d = diff(a, b)
    assert d.invalidated == ["train", "eval", "claim"]
    assert d.modified == []  # spec untouched; only the bound cell changed


# ---- validate -----------------------------------------------------------------------------------------------------

def codes(g):
    return {i.code for i in validate(g)}


def test_valid_graph():
    assert validate(make_graph()) == []


def test_validate_issues():
    g = make_graph()
    ns = [n.model_copy(deep=True) for n in g.nodes]
    cases = {
        "duplicate_id": lambda: ns + [ns[0]],
        "missing_input": lambda: [*ns[:2], N("x", "model", ["ghost"])],
        "self_input": lambda: [N("x", "model", ["x"])],
        "dataset_has_inputs": lambda: [N("a", "dataset"), N("b", "dataset", ["a"])],
        "no_inputs": lambda: [N("m", "model")],
        "claim_not_sink": lambda: [*ns, N("after", "eval", ["claim"])],
        "stated_without_source": lambda: [GraphNode(id="d", type="dataset", provenance="stated")],
        "cycle": lambda: [N("a", "model", ["b"]), N("b", "model", ["a"])],
        "duplicate_input": lambda: [N("d", "dataset"), N("m", "model", ["d", "d"])],
    }
    for code, build in cases.items():
        bad = ExperimentGraph(paper_id="p", nodes=build(), created_by="agent")
        assert code in codes(bad), code


# ---- downstream ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("node,expected", [
    ("data", ["prep", "model", "train", "eval", "claim"]),
    ("model", ["train", "eval", "claim"]),
    ("eval", ["claim"]),
    ("claim", []),
])
def test_downstream(node, expected):
    assert downstream(make_graph(), node) == expected


def test_downstream_unknown_node():
    with pytest.raises(KeyError):
        downstream(make_graph(), "nope")


# ---- create_version / diff ----------------------------------------------------------------------------------------

def test_create_version_bumps_and_sets_provenance():
    a = make_graph()
    b = create_version(a, {"train": NodeEdit(spec={"lr": 0.01})}, created_by="agent", change_note="lower lr")
    assert (b.version, b.parent_version, b.change_note) == (2, 1, "lower lr")
    assert b.id != a.id
    assert b.node("train").spec == {"lr": 0.01, "epochs": 90}
    assert b.node("train").provenance == "inferred"
    assert b.node("eval").provenance == "stated"
    assert a.node("train").spec["lr"] == 0.1  # original untouched
    h = create_version(a, {"train": {"spec": {"lr": 0.01}}}, created_by="human")
    assert h.node("train").provenance == "human"


def test_create_version_rejects_bad_edits():
    a = make_graph()
    with pytest.raises(GraphError):
        create_version(a, {"ghost": {"spec": {}}}, created_by="human")
    with pytest.raises(GraphValidationError):
        create_version(a, {"eval": {"inputs": ["eval"]}}, created_by="human")
    with pytest.raises(GraphValidationError):
        create_version(a, created_by="human", remove_node_ids=["prep"])  # dangling inputs
    with pytest.raises(GraphError):
        create_version(a, created_by="robot")


def test_add_and_remove_nodes():
    a = make_graph()
    b = create_version(a, created_by="human", add_nodes=[N("eval2", "eval", ["train"], provenance="human")])
    assert diff(a, b).added == ["eval2"]
    c = create_version(b, created_by="human", remove_node_ids=["eval2"])
    assert diff(b, c).removed == ["eval2"]
    assert diff(a, c).empty


def test_diff_spec_changes_and_invalidation():
    a = make_graph()
    b = create_version(a, {"train": {"spec": {"lr": 0.01, "wd": 1e-4}}}, created_by="human")
    d = diff(a, b)
    assert [c.node_id for c in d.modified] == ["train"]
    assert d.modified[0].fields == ["provenance", "spec"]
    assert d.modified[0].spec_changes == {"lr": (0.1, 0.01), "wd": (None, 1e-4)}
    assert d.invalidated == ["train", "eval", "claim"]
    assert not d.env_changed


def test_diff_identical_and_env_change():
    a = make_graph()
    assert diff(a, a.model_copy(deep=True)).empty
    b = create_version(a, created_by="human", env_digest="env2")
    d = diff(a, b)
    assert d.env_changed and len(d.invalidated) == 6


def test_diff_works_on_unhashed_graphs():
    a = make_graph()
    raw = lambda g: g.model_copy(update={"nodes": [n.model_copy(update={"content_hash": ""}) for n in g.nodes]})  # noqa: E731
    b = create_version(a, {"model": {"spec": {"arch": "resnet56"}}}, created_by="human")
    assert diff(raw(a), raw(b)).invalidated == ["model", "train", "eval", "claim"]
