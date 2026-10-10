from datetime import datetime, timezone

import pytest

from crisisgrid_core.audit_rules import RULES, AuditInput, merge_gaps, run_rules
from crisisgrid_core.models import Claim, Gap, Source
from crisisgrid_core.papers import (
    ArtifactInventory,
    Contradiction,
    DatasetRef,
    ExtractedValue,
    MetricDef,
    ParsedPaper,
    RepoInfo,
    enforce_sources,
    quote_in_text,
)

TEXT = """We train ResNet-20 on CIFAR-10 with a learning rate of 0.1 and a batch size of 128 for 160 epochs
using SGD. Our model achieves 91.25% top-1 accuracy on the test set. Training took two hours on a single
GPU. All images are normalized per channel."""


def src(q, kind="paper"):
    return Source(kind=kind, ref="p1", quote=q)


def claim(q="Our model achieves 91.25% top-1 accuracy on the test set", prov="stated"):
    return Claim(text="91.25 top1", metric="top1", dataset="cifar10", split="test", reported_value=91.25, unit="percent",
                 tolerance=0.5, source=src(q), provenance=prov)


# ---- quote verification -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("q", [
    "learning rate of 0.1",
    "LEARNING   RATE\nof 0.1",
    "a batch size of 128 for 160 epochs",
])
def test_quote_in_text_tolerates_whitespace_and_case(q):
    assert quote_in_text(q, TEXT)


@pytest.mark.parametrize("q", ["learning rate of 0.01", "", "   ", "momentum of 0.9"])
def test_quote_not_in_text(q):
    assert not quote_in_text(q, TEXT)


def test_quote_handles_hyphenation_and_unicode_dashes():
    assert quote_in_text("pre-trained weights", "we use pre-\ntrained weights")
    assert quote_in_text("top-1 accuracy", "top‑1 accuracy".replace("‑", "-"))
    assert quote_in_text("it's fine", "it’s fine")


def test_enforce_sources_keeps_verified_downgrades_unquoted_drops_fabricated():
    p = ParsedPaper(
        claims=[claim(), claim(q=""), claim(q="Our model achieves 99.9% accuracy")],
        hyperparameters={
            "learning_rate": ExtractedValue(value=0.1, source=src("a learning rate of 0.1")),
            "momentum": ExtractedValue(value=0.9, source=src("momentum of 0.9")),          # fabricated quote
            "weight_decay": ExtractedValue(value=1e-4),                                    # no quote at all
            "optimizer": ExtractedValue(value="sgd", provenance="human"),                   # human: untouched
        },
        seeds=ExtractedValue(value=0, source=src("seed 0")),
    )
    out = enforce_sources(p, TEXT)
    assert [c.provenance for c in out.claims] == ["stated", "inferred"]
    assert set(out.hyperparameters) == {"learning_rate", "weight_decay", "optimizer"}
    assert out.hyperparameters["learning_rate"].provenance == "stated"
    assert out.hyperparameters["weight_decay"].provenance == "inferred"
    assert out.hyperparameters["optimizer"].provenance == "human"
    assert out.seeds is None
    assert len(out.dropped) == 3 and any("momentum" in d for d in out.dropped)
    assert len(p.claims) == 3  # input untouched


def test_enforce_sources_contradictions_need_both_real_spans():
    good = Contradiction(field="epochs", a=ExtractedValue(value=160, source=src("for 160 epochs")),
                         b=ExtractedValue(value=200, source=src("batch size of 128")))
    bad = Contradiction(field="lr", a=ExtractedValue(value=0.1, source=src("learning rate of 0.1")),
                        b=ExtractedValue(value=0.2, source=src("learning rate of 0.2")))
    out = enforce_sources(ParsedPaper(contradictions=[good, bad]), TEXT)
    assert [c.field for c in out.contradictions] == ["epochs"] and len(out.dropped) == 1


def test_enforce_sources_drops_datasets_with_fabricated_quotes_only():
    p = ParsedPaper(datasets=[DatasetRef(name="CIFAR-10", source=src("on CIFAR-10")), DatasetRef(name="ImageNet", source=src("on ImageNet")),
                              DatasetRef(name="NoSource")])
    assert [d.name for d in enforce_sources(p, TEXT).datasets] == ["CIFAR-10", "NoSource"]


# ---- audit rules --------------------------------------------------------------------------------------------------

FULL_HP = {k: ExtractedValue(value=1) for k in ("learning_rate", "batch_size", "epochs", "optimizer")}


def inp(text="", parsed=None, inventory=None):
    return AuditInput(parsed or ParsedPaper(), inventory or ArtifactInventory(), text)


def fields(gaps):
    return {g.field for g in gaps}


def clean_input(text="Images are normalized. "):
    parsed = ParsedPaper(
        hyperparameters=FULL_HP, seeds=ExtractedValue(value=0), hardware=ExtractedValue(value="1 GPU"),
        datasets=[DatasetRef(name="CIFAR-10", split="50k/10k", split_provenance="paper_official", access="public", url="http://x")],
        metrics=[MetricDef(name="top-1 accuracy", definition="top-1 accuracy")], reports_variance=True, n_runs_reported=5,
    )
    inv = ArtifactInventory(repo=RepoInfo(url="https://github.com/a/b", commit="abc", dependencies_pinned=True))
    return AuditInput(parsed, inv, text)


def test_clean_paper_has_no_gaps():
    assert run_rules(clean_input()) == []


def test_every_rule_is_registered_and_returns_gaps_tagged_with_its_id():
    assert list(RULES) == [f"R{i:02d}" for i in range(1, 17)]
    for rid, rule in RULES.items():
        for g in rule(inp("")):
            assert g.field.startswith(rid + ":") and g.detected_by == "rule"


def test_empty_paper_triggers_the_structural_rules():
    got = {g.field.split(":")[0] for g in run_rules(inp(""))}
    assert {"R01", "R02", "R04", "R05", "R06", "R10"} <= got


def test_r02_severity_by_number_missing():
    one = clean_input()
    one.parsed.hyperparameters.pop("epochs")
    [g] = RULES["R02"](one)
    assert g.severity == "minor" and "epochs" in g.description
    [g] = RULES["R02"](inp(""))
    assert g.severity == "major"


def test_r03_split_provenance():
    p = ParsedPaper(datasets=[DatasetRef(name="A"), DatasetRef(name="B", split_provenance="approximate"),
                              DatasetRef(name="C", split_provenance="paper_official")])
    sev = {g.field: g.severity for g in RULES["R03"](inp(parsed=p))}
    assert sev == {"R03:split:A": "major", "R03:split:B": "minor"}


@pytest.mark.parametrize("metric,definition,flagged", [
    ("accuracy", None, True), ("accuracy", "top-1 accuracy", False), ("f1", "macro-averaged F1", False), ("f1", None, True),
    ("bleu", None, False),
])
def test_r04_ambiguous_metrics(metric, definition, flagged):
    p = ParsedPaper(metrics=[MetricDef(name=metric, definition=definition)])
    assert bool(RULES["R04"](inp(parsed=p))) is flagged


def test_r04_ambiguity_resolved_by_paper_text():
    p = ParsedPaper(metrics=[MetricDef(name="accuracy")])
    assert RULES["R04"](inp("we report top-1 error", parsed=p)) == []


def test_r07_private_dataset_is_the_blocking_gap():
    p = ParsedPaper(datasets=[DatasetRef(name="Secret", access="private"), DatasetRef(name="Mystery", access="unknown"),
                              DatasetRef(name="Ok", access="public")])
    sev = {g.field: g.severity for g in RULES["R07"](inp(parsed=p))}
    assert sev == {"R07:dataset_private:Secret": "blocking", "R07:dataset_source:Mystery": "major"}


@pytest.mark.parametrize("pinned,expect", [(False, "major"), (None, "minor"), (True, None)])
def test_r08(pinned, expect):
    inv = ArtifactInventory(repo=RepoInfo(url="u", dependencies_pinned=pinned))
    got = RULES["R08"](inp(inventory=inv))
    assert (got[0].severity if got else None) == expect


def test_r09_single_run():
    assert RULES["R09"](inp(parsed=ParsedPaper(n_runs_reported=1)))
    assert RULES["R09"](inp(parsed=ParsedPaper(reports_variance=False)))
    assert not RULES["R09"](inp(parsed=ParsedPaper(n_runs_reported=5, reports_variance=True)))


def test_r11_pretrained_without_weights():
    assert RULES["R11"](inp("we fine-tune a pretrained model"))
    assert not RULES["R11"](inp("we fine-tune a pretrained model", inventory=ArtifactInventory(weights_urls=["https://hf.co/m"])))
    assert not RULES["R11"](inp("we train from scratch"))


@pytest.mark.parametrize("rule,text", [
    ("R12", "We report the best of 5 runs for every table."),
    ("R12", "For each method we show the best seed."),
    ("R13", "The learning rate was tuned on the test set accuracy."),
    ("R13", "We selected the checkpoint using the test set."),
    ("R14", "Baseline numbers are taken from the original paper."),
    ("R14", "The results of the baselines are taken from prior work."),
    ("R16", "Responses are scored by GPT-4."),
    ("R16", "We rely on human evaluation by crowd-sourced annotators."),
])
def test_text_rules_fire(rule, text):
    gaps = RULES[rule](inp(text))
    assert gaps and gaps[0].severity in ("major", "minor") and "..." in gaps[0].description


@pytest.mark.parametrize("rule,text", [
    ("R12", "We report the mean over 5 seeds with standard deviation."),
    ("R13", "Hyperparameters were tuned on a held-out validation set; the test set was used once."),
    ("R14", "We rerun all baselines with the same code."),
    ("R16", "We use a ResNet and the official evaluation script."),
])
def test_text_rules_do_not_fire_on_clean_text(rule, text):
    assert RULES[rule](inp(text)) == []


def test_r15():
    old = ArtifactInventory(repo=RepoInfo(url="u", commit=None), branch_postdates_paper=True)
    assert len(RULES["R15"](inp(inventory=old))) == 2
    ok = ArtifactInventory(repo=RepoInfo(url="u", commit="abc"), branch_postdates_paper=False)
    assert RULES["R15"](inp(inventory=ok)) == []


def test_rules_never_emit_blocking_except_r07():
    text = "best of 5 runs tuned on the test set GPT-4 pretrained taken from the original paper baselines"
    for g in run_rules(inp(text)):
        assert g.severity != "blocking"


# ---- merge --------------------------------------------------------------------------------------------------------

def g(field, desc="d", sev="major", by="agent"):
    return Gap(field=field, severity=sev, description=desc, detected_by=by)


def test_merge_caps_agent_severity_and_dedupes_against_rules():
    rule_gaps = [g("R01:random_seed", "No random seed is stated.", "minor", "rule"), g("R07:dataset_private:X", "private", "blocking", "rule")]
    agent = [
        g("random_seed", "other words", "major"),                 # same field as a rule gap after normalization? no: 'randomseed' != 'r01random seed'
        g("R01", "agent says seed missing", "major"),             # references rule id R01 -> covered
        g("No random seed is stated", "No random seed is stated.", "minor"),  # same description
        g("label_leakage", "Test labels leak into training.", "blocking"),
        g("label_leakage", "Test labels leak into training.", "blocking"),     # duplicate agent gap
    ]
    out = merge_gaps(rule_gaps, agent)
    by_field = {x.field: x for x in out}
    assert by_field["label_leakage"].severity == "major" and by_field["label_leakage"].detected_by == "agent"
    assert [x.field for x in out].count("label_leakage") == 1
    assert "R01" not in by_field
    assert out[0].severity == "blocking" and out[0].field == "R07:dataset_private:X"   # sorted by severity


def test_merge_never_lets_an_agent_block():
    out = merge_gaps([], [g("x", "y", "blocking")])
    assert out[0].severity == "major"


def test_repo_info_dates_are_tz_aware_friendly():
    RepoInfo(url="u", default_branch_last_commit=datetime(2024, 1, 1, tzinfo=timezone.utc))
