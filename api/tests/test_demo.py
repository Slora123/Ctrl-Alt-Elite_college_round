import io

from crisisgrid_api.demo import run_demo


def test_the_offline_demo_runs_end_to_end_and_tells_the_truth():
    out = io.StringIO()
    report = run_demo(out)
    text = out.getvalue()
    assert "REPRODUCED" in text and "3 job(s) done" in text and "run completed" in text
    assert "Verdicts (clean runs only)" in report and "lower-isolation worker" in report
    assert "momentum" not in report  # the invented hyperparameter was dropped by quote verification
    assert "R09:no_variance" in report  # the paper reports no variance and the audit says so
