import hashlib
import os
import threading
from pathlib import Path

import httpx
import pytest

from paper_env.datasets import DatasetNotAvailable, DatasetStore
from paper_env.domain import *  # noqa: F403
from paper_env.hooks import Decision
from paper_env.repos import Conflict
from paper_env.security import EnvVarSecretsProvider
from tests.http_server import FileServer

A = b"alpha-data-" * 5000
B = b"beta-data-" * 7000
C = b"gamma-data-" * 3000


def sha(b): return hashlib.sha256(b).hexdigest()


@pytest.fixture
def srv():
    s = FileServer().start()
    s.files.update({"/a.bin": A, "/b.bin": B, "/c.bin": C})
    yield s
    s.stop()


@pytest.fixture
def mk(stack, srv):
    """Factory: DatasetStore bound to the shared stack (so hook/bus/uow are the stack's)."""
    stack.settings.min_free_disk_bytes = 0
    state = {"online": True}

    def make(secrets=None, **kw):
        ds = DatasetStore(stack.settings, stack.uow, stack.bus, stack.gate,
                          secrets or EnvVarSecretsProvider(environ={}),
                          network_check=lambda m: state["online"], client=httpx.Client(),
                          hf_endpoint=srv.base, backoff=0, **kw)
        return ds
    make.state = state
    return make


def url_ds(srv, name, content, path, *, checksum=True, **kw):
    files = [DatasetFile(path=path.strip("/"), size=len(content), sha256=sha(content) if checksum else "")] if checksum else []
    return DatasetManifest(dataset_id=f"{name}@v1", name=name, version="v1", source_type=SourceType.URL,
                           source_uri=srv.base + path, files=files, **kw)


# ------------------------------------------------------------ registry
def test_register_is_idempotent_and_versions_immutable(mk, srv):
    ds = mk()
    m = url_ds(srv, "a", A, "/a.bin")
    first = ds.register(m)
    assert first.status == DatasetStatus.REMOTE_ONLY and first.overall_checksum
    assert ds.register(m).dataset_id == "a@v1"
    changed = m.model_copy(update={"source_uri": srv.base + "/other.bin"})
    with pytest.raises(Conflict):
        ds.register(changed)


@pytest.mark.parametrize("uri", ["https://user:hunter2pass@host/x", "https://h/x?api_key=abcdef123456"])
def test_credentials_in_source_uri_rejected(mk, uri):
    with pytest.raises(ValueError, match="credentials"):
        mk().register(DatasetManifest(dataset_id="x@v1", name="x", version="v1", source_type=SourceType.URL, source_uri=uri))


def test_register_validates_checksums_and_paths(mk, srv):
    ds = mk()
    bad = url_ds(srv, "a", A, "/a.bin")
    bad.files[0].sha256 = "nothex"
    with pytest.raises(ValueError):
        ds.register(bad)
    evil = DatasetManifest(dataset_id="e@v1", name="e", version="v1", source_type=SourceType.URL, source_uri=srv.base + "/",
                           files=[DatasetFile(path="../../etc/passwd", size=1, sha256="a" * 64)])
    with pytest.raises(ValueError):
        ds.register(evil)


# ------------------------------------------------------------ fetch / verify
def test_fetch_with_expected_checksum_is_verified_and_read_only(mk, srv, stack):
    ds = mk()
    ds.register(url_ds(srv, "a", A, "/a.bin"))
    r = ds.fetch("a@v1")
    assert r.status == "verified" and ds.status("a@v1") == DatasetStatus.VERIFIED
    p = ds.resolve("a@v1")
    assert (p / "a.bin").read_bytes() == A
    with pytest.raises(PermissionError):
        open(p / "a.bin", "ab")
    with pytest.raises(PermissionError):
        open(p / "new.txt", "w")
    types = stack.event_types()
    assert types[0] == EventType.DATASET_FETCH_STARTED and EventType.DATASET_FETCH_PROGRESS in types
    assert types[-1] == EventType.DATASET_VERIFIED
    assert ds.fetch("a@v1").already_cached and len(srv.gets("/a.bin")) == 1


def test_fetch_without_expected_checksum_is_cached_not_verified(mk, srv, stack):
    ds = mk()
    ds.register(url_ds(srv, "a", A, "/a.bin", checksum=False))
    r = ds.fetch("a@v1")
    assert r.status == "cached"
    m = ds.get("a@v1")
    assert m.status == DatasetStatus.CACHED and m.files[0].sha256 == sha(A) and m.overall_checksum
    assert EventType.DATASET_CACHED in stack.event_types() and EventType.DATASET_VERIFIED not in stack.event_types()
    assert ds.verify("a@v1").ok


def test_checksum_mismatch_marks_corrupted_and_never_auto_refreshes(mk, srv, stack):
    ds = mk()
    m = url_ds(srv, "a", A, "/a.bin")
    m.files[0].sha256 = sha(b"something else")
    ds.register(m)
    r = ds.fetch("a@v1")
    assert r.status == "failed" and r.error_code == "checksum_mismatch"
    assert ds.status("a@v1") == DatasetStatus.CORRUPTED
    failed = [e for e in stack.captured if e.type == EventType.DATASET_FETCH_FAILED][-1]
    assert failed.payload["status"] == "corrupted" and failed.payload["mismatches"]
    n = len(srv.gets("/a.bin"))
    again = ds.fetch("a@v1")
    assert again.status == "failed" and again.error_code == "corrupted" and "reset" in again.error
    assert len(srv.gets("/a.bin")) == n  # no silent re-download
    with pytest.raises(DatasetNotAvailable):
        ds.resolve("a@v1")


def test_upstream_change_detected_on_fetch(mk, srv, stack):
    ds = mk()
    ds.register(url_ds(srv, "a", A, "/a.bin"))
    ds.fetch("a@v1")
    assert ds.get("a@v1").remote_fingerprint
    ds.reset("a@v1")
    srv.files["/a.bin"] = A + b"changed upstream"
    r = ds.fetch("a@v1")
    assert r.status == "failed" and r.error_code == "upstream_changed"
    assert ds.status("a@v1") == DatasetStatus.UPSTREAM_CHANGED
    assert EventType.DATASET_UPSTREAM_CHANGED in stack.event_types()
    # explicit operator decision accepts the new upstream; checksums are re-recorded
    ds.reset("a@v1", accept_upstream=True)
    assert ds.fetch("a@v1").status == "cached"
    assert (ds.resolve("a@v1") / "a.bin").read_bytes() == A + b"changed upstream"


def test_check_upstream_flags_without_refreshing(mk, srv, stack):
    ds = mk()
    ds.register(url_ds(srv, "a", A, "/a.bin"))
    ds.fetch("a@v1")
    assert ds.check_upstream("a@v1").changed is False
    srv.files["/a.bin"] = b"new"
    res = ds.check_upstream("a@v1")
    assert res.changed is True and ds.status("a@v1") == DatasetStatus.UPSTREAM_CHANGED
    assert EventType.DATASET_UPSTREAM_CHANGED in stack.event_types()
    assert len(srv.gets("/a.bin")) == 1  # only HEADs after the first fetch


def test_verify_detects_tampering(mk, srv, stack):
    ds = mk()
    ds.register(url_ds(srv, "a", A, "/a.bin"))
    ds.fetch("a@v1")
    f = ds.resolve("a@v1") / "a.bin"
    os.chmod(f.parent, 0o755)
    os.chmod(f, 0o644)
    f.write_bytes(b"tampered")
    v = ds.verify("a@v1")
    assert not v.ok and v.status == DatasetStatus.CORRUPTED and "sha256 differs" in v.mismatches[0]
    assert any(e.type == EventType.DATASET_FETCH_FAILED and e.payload["status"] == "corrupted" for e in stack.captured)


def test_missing_cache_dir_downgrades_to_remote_only(mk, srv):
    ds = mk()
    ds.register(url_ds(srv, "a", A, "/a.bin"))
    ds.fetch("a@v1")
    from paper_env.datasets.checksums import rmtree_force
    rmtree_force(Path(ds.get("a@v1").cache_path))
    with pytest.raises(DatasetNotAvailable):
        ds.resolve("a@v1")
    assert ds.status("a@v1") == DatasetStatus.REMOTE_ONLY


def test_resolve_before_fetch_never_fetches_implicitly(mk, srv):
    ds = mk()
    ds.register(url_ds(srv, "a", A, "/a.bin"))
    with pytest.raises(DatasetNotAvailable, match="remote_only"):
        ds.resolve("a@v1")
    assert srv.requests == []


def test_http_error_is_failure_not_corruption(mk, srv):
    ds = mk()
    ds.register(DatasetManifest(dataset_id="n@v1", name="n", version="v1", source_type=SourceType.URL,
                                source_uri=srv.base + "/missing.bin"))
    r = ds.fetch("n@v1")
    assert r.status == "failed" and r.error_code == "fetch_error" and "404" in r.error
    assert ds.status("n@v1") == DatasetStatus.REMOTE_ONLY and len(srv.gets("/missing.bin")) == 1


def test_resumes_interrupted_fetch_and_emits_progress(mk, srv, stack):
    srv.cut_after["/a.bin"] = 20000
    ds = mk()
    ds.register(url_ds(srv, "a", A, "/a.bin"))
    assert ds.fetch("a@v1").status == "verified"
    assert srv.gets("/a.bin")[1]["range"] == "bytes=20000-"
    prog = [e for e in stack.captured if e.type == EventType.DATASET_FETCH_PROGRESS]
    assert prog and prog[-1].payload["bytes_done"] == len(A)


def test_base_url_multi_file_dataset(mk, srv):
    srv.files["/set/x.txt"] = b"xx"
    srv.files["/set/sub/y.txt"] = b"yyy"
    ds = mk()
    ds.register(DatasetManifest(
        dataset_id="multi@v1", name="multi", version="v1", source_type=SourceType.URL, source_uri=srv.base + "/set/",
        files=[DatasetFile(path="x.txt", size=2, sha256=sha(b"xx")), DatasetFile(path="sub/y.txt", size=3, sha256=sha(b"yyy"))]))
    assert ds.fetch("multi@v1").status == "verified"
    assert (ds.resolve("multi@v1") / "sub/y.txt").read_bytes() == b"yyy"


def test_concurrent_fetches_share_one_download(mk, srv):
    ds = mk()
    ds.register(url_ds(srv, "a", A, "/a.bin"))
    results = []
    ts = [threading.Thread(target=lambda: results.append(ds.fetch("a@v1"))) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(results) == 6 and all(r.status == "verified" for r in results)
    assert len(srv.gets("/a.bin")) == 1
    assert sum(not r.already_cached for r in results) == 1


def test_same_content_under_two_ids_shares_cache(mk, srv):
    ds = mk()
    ds.register(url_ds(srv, "a", A, "/a.bin"))
    ds.register(url_ds(srv, "a2", A, "/a.bin"))
    ds.fetch("a@v1")
    r = ds.fetch("a2@v1")
    assert r.status == "verified" and len(srv.gets("/a.bin")) == 1
    assert ds.get("a@v1").cache_path == ds.get("a2@v1").cache_path


# ------------------------------------------------------------ disk / eviction
def test_insufficient_disk_fails_before_caching(mk, srv, stack):
    stack.settings.min_free_disk_bytes = 10 ** 18
    ds = mk()
    ds.register(url_ds(srv, "a", A, "/a.bin"))
    r = ds.fetch("a@v1")
    assert r.status == "failed" and r.error_code == "insufficient_disk"
    assert ds.status("a@v1") == DatasetStatus.REMOTE_ONLY


def test_lru_evicts_oldest_but_never_in_use(mk, srv, stack):
    ds = mk()
    for n, c, p in (("a", A, "/a.bin"), ("b", B, "/b.bin"), ("c", C, "/c.bin")):
        ds.register(url_ds(srv, n, c, p))
    ds.fetch("a@v1")
    ds.fetch("b@v1")
    ds.resolve("a@v1")  # touch: a is now more recent than b
    stack.settings.cache_max_bytes = len(A) + len(B) + len(C) - 1
    ds.fetch("c@v1")
    assert ds.status("b@v1") == DatasetStatus.REMOTE_ONLY and ds.status("a@v1") == DatasetStatus.VERIFIED
    assert EventType.DATASET_EVICTED in stack.event_types()
    # now protect a with an active run: c must stay too, so a new fetch can't evict a
    ds.acquire("run-1", ["a@v1"])
    ds.register(url_ds(srv, "b2", B, "/b.bin"))
    stack.settings.cache_max_bytes = len(A) + len(C)
    ds.fetch("b2@v1")
    assert ds.status("a@v1") == DatasetStatus.VERIFIED  # in use -> kept
    assert ds.status("c@v1") == DatasetStatus.REMOTE_ONLY  # evicted instead
    ds.release("run-1")


def test_uploads_and_derived_are_never_evicted(mk, srv, stack, tmp_path):
    ds = mk()
    f = tmp_path / "mine.csv"
    f.write_text("a,b\n1,2\n")
    assert ds.register_upload("mine", "v1", f, author="me").status == "verified"
    ds.register(url_ds(srv, "a", A, "/a.bin"))
    stack.settings.cache_max_bytes = 1  # absurdly small
    ds.fetch("a@v1")
    assert ds.status("mine@v1") == DatasetStatus.VERIFIED


# ------------------------------------------------------------ upload / derived
def test_upload_copies_into_read_only_cache(mk, tmp_path):
    ds = mk()
    f = tmp_path / "mine.csv"
    f.write_text("a,b\n1,2\n")
    r = ds.register_upload("mine", "v1", f, license="CC0")
    m = ds.get("mine@v1")
    assert m.access_type == AccessType.USER_UPLOADED and m.source_type == SourceType.USER_UPLOAD
    assert m.files[0].sha256 == sha(b"a,b\n1,2\n")
    p = ds.resolve("mine@v1")
    assert p != f.parent and (p / "mine.csv").read_text() == "a,b\n1,2\n"
    with pytest.raises(PermissionError):
        open(p / "mine.csv", "w")
    f.write_text("changed")  # source edits don't affect the cache
    assert (p / "mine.csv").read_text() == "a,b\n1,2\n"


def test_derived_links_parent_cell_version_and_inherits_splits(mk, srv, stack):
    ds = mk()
    base = url_ds(srv, "a", A, "/a.bin", splits=[DatasetSplit(name="train", provenance=SplitProvenance.APPROXIMATE)])
    ds.register(base)
    ds.fetch("a@v1")
    out = stack.settings.runs_dir("run-9") / "derived" / "a_clean"
    out.mkdir(parents=True)
    (out / "x.npy").write_bytes(b"123")
    m = ds.register_derived(run_id="run-9", name="a_clean", version="r9", directory=out, parent_dataset_id="a@v1",
                            producing_cell_version_id="cv-1", author="agent")
    assert (m.parent_dataset_id, m.producing_cell_version_id, m.run_id) == ("a@v1", "cv-1", "run-9")
    assert m.status == DatasetStatus.VERIFIED and m.splits[0].provenance == SplitProvenance.APPROXIMATE
    assert ds.approximate_split(["a_clean@r9"])
    with pytest.raises(PermissionError):
        open(out / "x.npy", "wb")
    with pytest.raises(ValueError):  # must live under the run's directory
        ds.register_derived(run_id="run-9", name="z", version="1", directory=out.parent.parent.parent, parent_dataset_id=None,
                            producing_cell_version_id=None)
    with pytest.raises(Conflict):
        ds.register_derived(run_id="run-9", name="a_clean", version="r9", directory=out, parent_dataset_id="a@v1",
                            producing_cell_version_id="cv-1")


# ------------------------------------------------------------ preflight
def test_preflight_offline_remote_only_fails_early_with_clear_message(mk, srv):
    ds = mk()
    ds.register(url_ds(srv, "a", A, "/a.bin"))
    mk.state["online"] = False
    rep = ds.preflight(["a@v1"])
    assert not rep.ok and rep.problems[0].code == "offline_remote_only"
    assert "a@v1" in rep.message and "network is unavailable" in rep.message
    assert srv.requests == []


def test_preflight_cached_dataset_works_offline(mk, srv):
    ds = mk()
    ds.register(url_ds(srv, "a", A, "/a.bin"))
    ds.fetch("a@v1")
    mk.state["online"] = False
    n = len(srv.requests)
    rep = ds.preflight(["a@v1"], run_id="r1")
    assert rep.ok and len(srv.requests) == n
    assert rep.dataset_manifests[0]["overall_checksum"]


def test_preflight_fails_fast_without_partial_fetching(mk, srv):
    ds = mk()
    ds.register(url_ds(srv, "a", A, "/a.bin"))
    ds.register(url_ds(srv, "b", B, "/b.bin"))
    ds.fetch("a@v1")
    ds.reset("a@v1")
    mk.state["online"] = False
    n = len(srv.requests)
    rep = ds.preflight(["a@v1", "b@v1", "ghost@v9"])
    assert not rep.ok and {p.code for p in rep.problems} == {"offline_remote_only", "unregistered"}
    assert len(srv.requests) == n


def test_preflight_fetches_missing_when_online_and_flags_approximate_split(mk, srv):
    ds = mk()
    ds.register(url_ds(srv, "a", A, "/a.bin", splits=[DatasetSplit(name="val", provenance=SplitProvenance.APPROXIMATE)]))
    ds.register(url_ds(srv, "b", B, "/b.bin", splits=[DatasetSplit(name="val", provenance=SplitProvenance.PAPER_OFFICIAL)]))
    rep = ds.preflight(["b@v1"])
    assert rep.ok and not rep.approximate_split
    rep = ds.preflight(["a@v1", "b@v1"], run_id="r2")
    assert rep.ok and rep.approximate_split
    assert ds.status("a@v1") == DatasetStatus.VERIFIED


def test_preflight_rejects_corrupted(mk, srv):
    ds = mk()
    m = url_ds(srv, "a", A, "/a.bin")
    m.files[0].sha256 = sha(b"x")
    ds.register(m)
    ds.fetch("a@v1")
    rep = ds.preflight(["a@v1"])
    assert not rep.ok and rep.problems[0].code == "corrupted"


def test_local_path_dataset_needs_no_network(mk, tmp_path):
    d = tmp_path / "ld"
    d.mkdir()
    (d / "x.txt").write_text("hi")
    ds = mk()
    ds.register(DatasetManifest(dataset_id="loc@v1", name="loc", version="v1", source_type=SourceType.LOCAL_PATH,
                                source_uri=str(d)))
    mk.state["online"] = False
    rep = ds.preflight(["loc@v1"])
    assert rep.ok and (ds.resolve("loc@v1") / "x.txt").read_text() == "hi"


# ------------------------------------------------------------ gated / secrets
def hf_manifest(srv, gated=True, files=None):
    return DatasetManifest(dataset_id="hfd@main", name="hfd", version="main", source_type=SourceType.HUGGINGFACE,
                           source_uri="hf://datasets/org/hfd", access_type=AccessType.GATED if gated else AccessType.PUBLIC,
                           files=files or [])


def test_huggingface_public_fetch_uses_revision_sha(mk, srv, stack):
    srv.hf["org/hfd"] = {"sha": "c0ffee" * 6, "files": {"train.csv": A, "meta/info.json": b"{}"}, "lfs": True}
    ds = mk()
    ds.register(hf_manifest(srv, gated=False))
    r = ds.fetch("hfd@main")
    assert r.status == "cached"
    m = ds.get("hfd@main")
    assert m.remote_fingerprint == "c0ffee" * 6 and {f.path for f in m.files} == {"train.csv", "meta/info.json"}
    assert (ds.resolve("hfd@main") / "meta/info.json").read_bytes() == b"{}"
    assert any("/resolve/" + "c0ffee" * 6 in r["path"] for r in srv.requests)  # pinned to the commit


def test_hf_transfer_corruption_against_published_lfs_sha_is_detected(mk, srv):
    srv.hf["org/hfd"] = {"sha": "abc123", "files": {"train.csv": A}, "lfs": True, "lfs_override": {"train.csv": sha(b"other")}}
    ds = mk()
    ds.register(hf_manifest(srv, gated=False))
    r = ds.fetch("hfd@main")
    assert r.status == "failed" and r.error_code == "checksum_mismatch"
    assert ds.status("hfd@main") == DatasetStatus.CORRUPTED


def test_gated_requires_credentials_and_they_never_leak(mk, srv, stack, tmp_path):
    token = "hf_" + "Zq9" * 10
    srv.token = token
    srv.hf["org/hfd"] = {"sha": "abc123", "files": {"train.csv": A}}
    # no credential configured -> explicit failure, nothing guessed
    ds = mk()
    ds.register(hf_manifest(srv))
    r = ds.fetch("hfd@main")
    assert r.status == "failed" and r.error_code == "auth_required" and "hf_token" in r.error
    # wrong credential
    bad = mk(EnvVarSecretsProvider({"hf_token": "HF_T"}, environ={"HF_T": "wrong-token-value"}))
    assert bad.fetch("hfd@main").error_code == "auth_required"
    # right credential
    good = mk(EnvVarSecretsProvider({"hf_token": "HF_T"}, environ={"HF_T": token}))
    assert good.fetch("hfd@main").status == "cached"
    assert any(r["auth"] == f"Bearer {token}" for r in srv.requests)
    # the token appears nowhere we store or emit
    blob = str(stack.bus.history(limit=100000)) + str(good.list()) + str(ds.list())
    for f in Path(stack.settings.data_dir).rglob("*"):
        if f.is_file() and f.stat().st_size < 10 ** 6 and f.suffix in (".log", ".json", ".lock", ""):
            try:
                blob += f.read_text()
            except UnicodeDecodeError:
                pass
    assert token not in blob


def test_kaggle_and_s3_are_explicit_stubs(mk):
    ds = mk()
    for st in (SourceType.KAGGLE, SourceType.S3):
        ds.register(DatasetManifest(dataset_id=f"{st.value}@v1", name=st.value, version="v1", source_type=st, source_uri="x://y"))
        r = ds.fetch(f"{st.value}@v1")
        assert r.status == "failed" and r.error_code == "not_implemented" and "not implemented" in r.error


# ------------------------------------------------------------ hook
def test_fetch_blocked_by_hook_then_resumed(stack_factory, srv):
    class Ask:
        def check(self, a): return Decision.needs_approval("confirm download")

    st = stack_factory(hook=Ask())
    st.settings.min_free_disk_bytes = 0
    ds = DatasetStore(st.settings, st.uow, st.bus, st.gate, EnvVarSecretsProvider(environ={}),
                      network_check=lambda m: True, client=httpx.Client(), backoff=0)
    ds.register(url_ds(srv, "a", A, "/a.bin"))
    r = ds.fetch("a@v1")
    assert r.status == "blocked" and r.blocked_action_id and srv.requests == []
    assert ds.status("a@v1") == DatasetStatus.REMOTE_ONLY
    out = st.gate.resume_blocked(r.blocked_action_id, Decision.allow("ok"))
    assert out.status == "verified" and len(srv.gets("/a.bin")) == 1
    # deny path: nothing downloaded
    ds.register(url_ds(srv, "b", B, "/b.bin"))
    r2 = ds.fetch("b@v1")
    assert st.gate.resume_blocked(r2.blocked_action_id, Decision.deny("no")) is None
    assert srv.gets("/b.bin") == [] and ds.status("b@v1") == DatasetStatus.REMOTE_ONLY
