from paper_env.kernel.outputs import OutputRecorder
from paper_env.security import Scrubber


def rec(tmp_path, **kw):
    return OutputRecorder(Scrubber(scan_environ=False), tmp_path / "o.jsonl", kw.get("db", 10_000), kw.get("total", 1_000_000))


def test_progress_bar_overwrites_live_but_stores_only_final_line(tmp_path):
    r = rec(tmp_path)
    live = []
    for chunk in ("\r  0%|          |", "\r 50%|#####     |", "\r100%|##########|", "\n"):
        live += r.stream("stderr", chunk)
    assert [t for t, ow in live[:3]] == ["  0%|          |", " 50%|#####     |", "100%|##########|"]
    assert all(ow for _, ow in live[:3]) and live[3] == ("100%|##########|\n", False)
    outs, truncated, _ = r.finish()
    assert outs == [{"type": "stream", "name": "stderr", "text": "100%|##########|\n"}] and not truncated


def test_plain_lines_unchanged_and_partial_line_flushed_at_end(tmp_path):
    r = rec(tmp_path)
    assert r.stream("stdout", "a\nb") == [("a\n", False)]
    assert r.stream("stdout", "c\n") == [("bc\n", False)]
    r.stream("stdout", "tail")
    outs, *_ = r.finish()
    assert outs[0]["text"] == "a\nbc\ntail"


def test_secret_split_across_carriage_return_chunks_is_still_redacted(tmp_path):
    s = Scrubber(["topsecret-value"], scan_environ=False)
    r = OutputRecorder(s, tmp_path / "o", 10_000, 1_000_000)
    live = r.stream("stdout", "\rtopsecret-") + r.stream("stdout", "value\n")
    assert "topsecret" not in str(live) and "topsecret" not in str(r.finish()[0])
