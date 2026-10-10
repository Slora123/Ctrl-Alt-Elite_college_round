import pytest

from crisisgrid_core.attribution import attribute, confound_flags, plan_single_runs
from crisisgrid_core.graph import compute_hashes, create_version
from crisisgrid_core.ids import uuid7
from crisisgrid_core.models import ExperimentGraph, GraphNode, RunResult, Source
from crisisgrid_core.verifier import IneligibleRunError

SRC = Source(kind="paper", ref="p")


def N(id, type, inputs=(), spec=None):
    return GraphNode(id=id, type=type, inputs=list(inputs), spec=spec or {}, provenance="stated", sources=[SRC])


def base():
    nodes = [N("data", "dataset", spec={"name": "cifar10"}), N("prep", "preprocess", ["data"], {"normalize": True}),
             N("model", "model", ["prep"], {"arch": "resnet20", "depth": 20}),
             N("train", "train", ["model"], {"lr": 0.1, "weight_decay": 1e-4, "epochs": 90}),
             N("eval", "eval", ["train"], {"metric": "top1"})]
    return compute_hashes(ExperimentGraph(paper_id="p", nodes=nodes, created_by="agent", env_digest="e1"))


def runs(values, status="ok", **kw):
    return [RunResult(run_id=uuid7(), graph_id="g", seed=i, status=status, mode="clean", verification_eligible=True,
                      metrics={"top1": v}, **kw) for i, v in enumerate(values)]


A = [90.0, 90.2, 89.8, 90.1]


def edit(g, **edits):
    return create_version(g, {k: {"spec": v} for k, v in edits.items()}, created_by="human")


def test_one_edit_with_a_real_effect_is_attributed_directly():
    a = base()
    b = edit(a, train={"lr": 0.01})
    r = attribute(a, b, "top1", runs(A), runs([91.5, 91.7, 91.3, 91.6]))
    assert r.status == "attributed" and r.changed_nodes == ["train"] and r.partial is False
    assert r.delta == pytest.approx(1.5) and r.residual == 0.0
    [c] = r.contributions
    assert (c.node_id, c.significant, c.measured) == ("train", True, True)


def test_difference_inside_seed_noise_is_not_attributed():
    a = base()
    b = edit(a, train={"lr": 0.01})
    r = attribute(a, b, "top1", runs([90.0, 90.6, 89.4, 90.2]), runs([90.3, 89.6, 90.9, 89.8]))
    assert r.status == "no_attributable_effect" and r.contributions == [] and any("seed noise" in c for c in r.caveats)


def test_too_few_runs_means_the_noise_floor_is_unknown():
    a = base()
    b = edit(a, train={"lr": 0.01})
    r = attribute(a, b, "top1", runs([90.0]), runs([95.0]))
    assert r.status == "inconclusive" and r.delta == pytest.approx(5.0) and r.noise_se is None
    assert any("noise floor unknown" in c for c in r.caveats)


def test_deterministic_runs_zero_variance():
    a = base()
    b = edit(a, train={"lr": 0.01})
    assert attribute(a, b, "top1", runs([90.0, 90.0]), runs([91.0, 91.0])).status == "attributed"
    assert attribute(a, b, "top1", runs([90.0, 90.0]), runs([90.0, 90.0])).status == "no_attributable_effect"


def test_identical_versions_have_nothing_to_attribute():
    a = base()
    r = attribute(a, a.model_copy(deep=True), "top1", runs(A), runs(A))
    assert r.status == "inconclusive" and "nothing to attribute" in r.caveats[0]


def test_interactive_runs_are_rejected():
    a = base()
    b = edit(a, train={"lr": 0.01})
    bad = [RunResult(run_id="i", graph_id="g", seed=0, status="ok", mode="interactive", verification_eligible=False, metrics={"top1": 99.0})]
    with pytest.raises(IneligibleRunError):
        attribute(a, b, "top1", runs(A), bad)


def test_failed_and_infra_runs_are_excluded_with_a_caveat():
    a = base()
    b = edit(a, train={"lr": 0.01})
    rb = runs([91.5, 91.7, 91.3, 91.6]) + runs([0.0], status="infra_failed") + runs([0.0], status="failed")
    r = attribute(a, b, "top1", runs(A), rb)
    assert r.n_b == 4 and r.status == "attributed" and any("excluded" in c for c in r.caveats)


def test_metric_name_matching_is_forgiving():
    a = base()
    b = edit(a, train={"lr": 0.01})
    assert attribute(a, b, "Top-1", runs(A), runs([91.5, 91.7, 91.3, 91.6])).status == "attributed"


# ---- several edits ------------------------------------------------------------------------------------------------

def test_two_edits_are_split_with_one_at_a_time_runs_and_a_residual():
    a = base()
    b = edit(a, train={"lr": 0.01}, model={"depth": 32})
    total = runs([91.8, 91.9, 91.7, 91.8])                      # mean 91.8: +1.775 over the 90.025 baseline
    singles = {"train": runs([91.0, 91.1, 90.9, 91.0]),          # mean 91.0: +0.975
               "model": runs([90.8, 90.9, 90.7, 90.8])}          # mean 90.8: +0.775
    r = attribute(a, b, "top1", runs(A), total, singles)
    assert r.status == "attributed" and r.partial is False
    by = {c.node_id: c for c in r.contributions}
    assert by["train"].delta == pytest.approx(0.975) and by["model"].delta == pytest.approx(0.775)
    assert by["train"].significant and by["model"].significant
    assert r.residual == pytest.approx(0.025)  # 1.775 - 0.975 - 0.775: whatever the parts do not explain is reported, not spread
    assert any("architecture" in f for f in r.flags) and any("several concerns" in f for f in r.flags)


def test_budget_exhausted_gives_a_partial_result_with_the_unmeasured_edit_in_the_residual():
    a = base()
    b = edit(a, train={"lr": 0.01}, model={"depth": 32})
    r = attribute(a, b, "top1", runs(A), runs([91.8, 91.9, 91.7, 91.8]), {"train": runs([91.0, 91.1, 90.9, 91.0])})
    assert r.partial is True and r.status == "attributed"
    by = {c.node_id: c for c in r.contributions}
    assert by["model"].measured is False and by["model"].delta is None
    assert r.residual == pytest.approx(1.775 - 0.975) and any("budget" in c for c in r.caveats)


def test_several_edits_without_any_single_runs_is_inconclusive_not_guessed():
    a = base()
    b = edit(a, train={"lr": 0.01}, model={"depth": 32})
    r = attribute(a, b, "top1", runs(A), runs([91.8, 91.9, 91.7, 91.8]))
    assert r.status == "inconclusive" and r.partial and all(not c.measured for c in r.contributions)


def test_edits_that_individually_do_nothing_point_to_an_interaction():
    a = base()
    b = edit(a, train={"lr": 0.01}, model={"depth": 32})
    quiet = {"train": runs([90.0, 90.2, 89.8, 90.1]), "model": runs([90.1, 90.0, 90.2, 89.9])}
    r = attribute(a, b, "top1", runs(A), runs([91.8, 91.9, 91.7, 91.8]), quiet)
    assert r.status == "inconclusive" and any("interaction" in c for c in r.caveats)


# ---- confounds / planning -----------------------------------------------------------------------------------------

def test_environment_change_is_flagged_and_blocks_single_node_attribution():
    a = base()
    b = create_version(a, {"train": {"spec": {"lr": 0.01}}}, created_by="human", env_digest="e2")
    r = attribute(a, b, "top1", runs(A), runs([91.5, 91.7, 91.3, 91.6]))
    assert any("environment" in f for f in r.flags)
    assert r.status == "inconclusive"  # one edit + a new environment: cannot say which caused it


def test_added_nodes_are_a_structural_confound():
    a = base()
    b = create_version(a, created_by="human", add_nodes=[N("eval2", "eval", ["train"])])
    assert any("added/removed" in f for f in confound_flags(a, b))


def test_clean_single_edit_has_no_confound_flags():
    a = base()
    assert confound_flags(a, edit(a, train={"lr": 0.01})) == []


def test_node_types_mixed_edit_is_flagged():
    a = base()
    b = edit(a, prep={"normalize": False}, train={"epochs": 30})
    f = confound_flags(a, b)
    assert any("several kinds of node" in x for x in f) and any("data/preprocessing" in x for x in f)


@pytest.mark.parametrize("seeds,max_runs,expected,truncated", [
    (3, 100, ["model", "train"], False),
    (3, 6, ["model", "train"], False),
    (3, 5, ["model"], True),     # upstream edits are run first
    (3, 2, [], True),
    (1, 1, ["model"], True),
])
def test_plan_single_runs_respects_the_budget_and_runs_upstream_first(seeds, max_runs, expected, truncated):
    a = base()
    b = edit(a, train={"lr": 0.01}, model={"depth": 32})
    assert plan_single_runs(a, b, seeds, max_runs) == (expected, truncated)
