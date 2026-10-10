import hashlib
import httpx
import pytest

from paper_env.datasets.download import DownloadError, InsufficientDisk, download_file
from tests.http_server import FileServer

DATA = bytes(range(256)) * 400  # 102400 bytes


@pytest.fixture
def srv():
    s = FileServer().start()
    s.files["/f.bin"] = DATA
    yield s
    s.stop()


@pytest.fixture
def client():
    with httpx.Client() as c:
        yield c


def sha(b): return hashlib.sha256(b).hexdigest()


def test_plain_download(srv, client, tmp_path):
    d = download_file(client, srv.base + "/f.bin", tmp_path / "f.bin", backoff=0)
    assert d.sha256 == sha(DATA) and d.size == len(DATA) and (tmp_path / "f.bin").read_bytes() == DATA
    assert not (tmp_path / "f.bin.part").exists()


def test_dropped_connection_resumes_with_range(srv, client, tmp_path):
    srv.cut_after["/f.bin"] = 30000
    calls = []
    d = download_file(client, srv.base + "/f.bin", tmp_path / "f.bin", backoff=0, progress=lambda a, b: calls.append(a))
    assert d.sha256 == sha(DATA) and d.attempts == 2
    gets = srv.gets("/f.bin")
    assert gets[0]["range"] is None and gets[1]["range"] == "bytes=30000-"
    assert gets[1]["if_range"]  # ETag validated on resume
    assert calls[-1] == len(DATA)


def test_resume_from_leftover_part_file_after_crash(srv, client, tmp_path):
    srv.cut_after["/f.bin"] = 10000
    with pytest.raises(DownloadError):
        download_file(client, srv.base + "/f.bin", tmp_path / "f.bin", retries=0, backoff=0)
    assert (tmp_path / "f.bin.part").stat().st_size == 10000
    d = download_file(client, srv.base + "/f.bin", tmp_path / "f.bin", backoff=0)
    assert d.sha256 == sha(DATA) and srv.gets("/f.bin")[-1]["range"] == "bytes=10000-"


def test_server_without_range_support_restarts_cleanly(srv, client, tmp_path):
    srv.support_range = False
    srv.cut_after["/f.bin"] = 5000
    d = download_file(client, srv.base + "/f.bin", tmp_path / "f.bin", backoff=0)
    assert d.sha256 == sha(DATA)


def test_changed_upstream_between_attempts_restarts_not_splices(srv, client, tmp_path):
    srv.cut_after["/f.bin"] = 10000
    with pytest.raises(DownloadError):
        download_file(client, srv.base + "/f.bin", tmp_path / "f.bin", retries=0, backoff=0)
    new = b"Z" * 50000
    srv.files["/f.bin"] = new  # ETag changes -> If-Range mismatch -> full 200
    d = download_file(client, srv.base + "/f.bin", tmp_path / "f.bin", backoff=0)
    assert (tmp_path / "f.bin").read_bytes() == new and d.sha256 == sha(new)


def test_retries_5xx_then_succeeds_with_backoff(srv, client, tmp_path):
    srv.fail_status["/f.bin"] = [503, 503]
    sleeps = []
    d = download_file(client, srv.base + "/f.bin", tmp_path / "f.bin", backoff=0.5, sleep=sleeps.append)
    assert d.sha256 == sha(DATA) and sleeps == [0.5, 1.0]


def test_gives_up_after_retries(srv, client, tmp_path):
    srv.fail_status["/f.bin"] = [500] * 10
    with pytest.raises(DownloadError, match="giving up"):
        download_file(client, srv.base + "/f.bin", tmp_path / "f.bin", retries=2, backoff=0, sleep=lambda s: None)
    assert len(srv.gets("/f.bin")) == 3


@pytest.mark.parametrize("status", [401, 403, 404])
def test_4xx_not_retried(srv, client, tmp_path, status):
    srv.fail_status["/f.bin"] = [status]
    with pytest.raises(DownloadError) as ei:
        download_file(client, srv.base + "/f.bin", tmp_path / "f.bin", backoff=0)
    assert ei.value.status == status and len(srv.gets("/f.bin")) == 1


def test_insufficient_disk_checked_before_writing(srv, client, tmp_path):
    with pytest.raises(InsufficientDisk):
        download_file(client, srv.base + "/f.bin", tmp_path / "f.bin", min_free_bytes=10 ** 18)
    assert not (tmp_path / "f.bin.part").exists() or (tmp_path / "f.bin.part").stat().st_size == 0
