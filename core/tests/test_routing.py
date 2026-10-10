import pytest

from crisisgrid_core.models import Claim, Gap, Source
from crisisgrid_core.papers import ArtifactInventory, DatasetRef, RepoInfo
from crisisgrid_core.routing import select_route

CLAIM = Claim(text="t", metric="m", dataset="d", split="test", reported_value=1.0, unit="score", tolerance=0.1,
              source=Source(kind="paper", ref="p"), provenance="stated")
REPO = RepoInfo(url="https://github.com/a/b", commit="abc")
PUBLIC = DatasetRef(name="d", access="public", url="http://x")


def inv(repo=REPO, datasets=(PUBLIC,)):
    return ArtifactInventory(repo=repo, datasets=list(datasets))


def blocking(name="X"):
    return Gap(field=f"R07:dataset_private:{name}", severity="blocking", description=f"{name} private", detected_by="rule")


@pytest.mark.parametrize("claims,gaps,inventory,case,tier,gates", [
    ([CLAIM], [], inv(), 2, 2, []),
    ([CLAIM], [], inv(repo=None), 3, 3, ["gap"]),
    ([CLAIM], [blocking()], inv(), 1, 1, ["data"]),
    ([CLAIM], [], inv(datasets=[]), 1, 1, ["data"]),
    ([CLAIM], [], inv(datasets=[DatasetRef(name="p", access="private")]), 1, 1, ["data"]),
    ([CLAIM], [], inv(datasets=[DatasetRef(name="u", access="unknown")]), 1, 1, ["data"]),
    ([CLAIM], [], inv(datasets=[DatasetRef(name="u", access="unknown", url="http://u")]), 2, 2, []),
    ([CLAIM], [], inv(datasets=[DatasetRef(name="g", access="gated")]), 2, 2, []),
    ([CLAIM], [blocking()], inv(repo=None), 1, 1, ["data"]),   # data is checked before code
])
def test_route_table(claims, gaps, inventory, case, tier, gates):
    d = select_route(claims, gaps, inventory)
    assert (d.case, d.tier, d.required_gates) == (case, tier, gates) and d.reasons


def test_no_claims_is_noted_but_does_not_change_the_route():
    d = select_route([], [], inv())
    assert d.case == 2 and any("no claims" in r for r in d.reasons)


def test_route_is_deterministic():
    a, b = select_route([CLAIM], [blocking()], inv()), select_route([CLAIM], [blocking()], inv())
    assert a == b
