import pytest

from crisisgrid_core.models import ExperimentGraph, GraphNode, Source, Verdict
from crisisgrid_core.report_check import (
    Deviation,
    allowed_numbers,
    attribution_guidance,
    check_numbers,
    fidelity_summary,
    numbers_in,
    render_deviations,
)

V = Verdict(claim_id="c", status="partial", reported=91.25, measured_mean=90.8, measured_std=0.31, delta=-0.45, n_seeds=3,
            evidence_run_ids=["a", "b", "c"], caveats=[])


@pytest.mark.parametrize("text,expected", [
    ("measured 90.8 vs 91.25", ["90.8", "91.25"]),
    ("accuracy was 91.25% (n=3)", ["91.25%", "3"]),
    ("lr 1e-4 and 1,200 steps", ["1e-4", "1,200"]),
    ("see `run 42` and https://x.org/v2/3", []),
    ("delta of -0.45", ["-0.45"]),
    ("no digits here", []),
    ("ResNet-20 uses 3x3 kernels", ["20", "3"]),  # strict on purpose: any number the writer asserts must be in the evidence
])
def test_numbers_in(text, expected):
    assert [lit for lit, _ in numbers_in(text)] == expected


def test_numbers_matching_the_evidence_pass_at_the_precision_stated():
    allowed = allowed_numbers([V])
    assert check_numbers("Mean 90.8 (std 0.31) over 3 seeds against the reported 91.25; delta -0.45.", allowed) == []
    assert check_numbers("Rounded: 91 and 91.3 and 90.80 and 0.3", allowed) == []     # 91.25->91.3 ok at 1dp; 0.31->0.3 ok
    assert check_numbers("The gap was 0.45", allowed) == []                           # |delta|


@pytest.mark.parametrize("text,bad", [
    ("Accuracy reached 92.4.", ["92.4"]),
    ("Over 5 seeds we saw 90.8.", ["5"]),
    ("It improved by 12%", ["12%"]),
    ("Mean 90.9", ["90.9"]),                 # near, but not a rounding of 90.8
    ("reproduced 91.25 and 99.9", ["99.9"]),
])
def test_invented_or_altered_numbers_are_rejected(text, bad):
    assert check_numbers(text, allowed_numbers([V])) == bad


def test_extra_allowed_numbers_cover_counts_the_pipeline_knows():
    assert check_numbers("3 gaps were found in 2 runs", allowed_numbers([V], extra=[2, 3])) == []


def test_unmeasured_verdicts_allow_only_what_exists():
    v = Verdict(claim_id="c", status="not_executed", reported=91.25)
    assert check_numbers("reported 91.25 but nothing measured", allowed_numbers([v])) == []
    assert check_numbers("measured 90.8", allowed_numbers([v])) == ["90.8"]


def test_deviation_list():
    assert "exactly what was provided" in render_deviations([])
    out = render_deviations([Deviation(node_id="train", author="agent", summary="pinned torch==2.1.0"),
                             Deviation(cell_id="c9", author="generator", summary="added data path fix")])
    assert out.startswith("2 agent-authored edit(s)") and "`train` (agent)" in out and "`c9` (generator)" in out


def g(provs):
    src = [Source(kind="paper", ref="p")]
    nodes = [GraphNode(id=f"n{i}", type="dataset" if i == 0 else "model", inputs=[] if i == 0 else ["n0"], provenance=p,
                       sources=src if p == "stated" else []) for i, p in enumerate(provs)]
    nodes.append(GraphNode(id="claim", type="claim", inputs=["n0"], provenance="stated", sources=src))
    return ExperimentGraph(paper_id="p", nodes=nodes, created_by="agent")


@pytest.mark.parametrize("provs,edits,level", [
    (["stated"] * 5, 0, "high"), (["stated"] * 4 + ["inferred"], 0, "high"), (["stated"] * 4 + ["inferred"], 1, "medium"),
    (["stated", "inferred"], 0, "medium"), (["inferred"] * 4 + ["stated"], 0, "low"), (["human"] * 4, 0, "high"),
])
def test_fidelity_levels(provs, edits, level):
    devs = [Deviation(summary="x")] * edits
    f = fidelity_summary(g(provs), devs)
    assert f.level == level and f.nodes == len(provs) and f.agent_edits == edits


def test_fidelity_ignores_claim_nodes_and_lists_inferred_ones():
    f = fidelity_summary(g(["stated", "inferred", "inferred"]))
    assert f.inferred_nodes == ["n1", "n2"] and f.nodes == 3


def test_a_failed_reproduction_is_not_blamed_on_the_paper_when_fidelity_is_poor():
    low = fidelity_summary(g(["inferred"] * 5))
    high = fidelity_summary(g(["stated"] * 5))
    assert "cannot be blamed on the paper" in attribution_guidance("not_reproduced", low)
    assert attribution_guidance("not_reproduced", high) is None
    assert attribution_guidance("reproduced", low) is None
