"""End-to-end demo: PaperEnv -> dataset -> 4 cells -> run_all -> break a cell -> fix via apply_edit -> run_clean.

Run:  uv run python demo.py

Needs network ONLY the first time (uv downloads ipykernel/dill into its cache). After that, with
`UV_OFFLINE=1 uv run python demo.py` it runs fully offline: the dataset is a local file that is
copied into the content-addressed cache and mounted read-only into the kernel.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from paper_env.config import Settings
from paper_env.container import build_app
from paper_env.domain import Author, DatasetSplit, EnvSpec, EventType, SplitProvenance

HERE = Path(__file__).resolve().parent
DATA = HERE / "demo_data"

CELLS = [
    dict(name="load", outputs=["rows"], inputs=[], source=(
        "import csv\n"
        "with open(datasets.resolve('toy-points@v1') / 'points.csv') as f:\n"
        "    rows = [(float(r['x']), float(r['y'])) for r in csv.DictReader(f)]\n"
        "print(f'loaded {len(rows)} rows')\n")),
    dict(name="stats", outputs=["mean_x", "mean_y"], inputs=["rows"], source=(
        "mean_x = sum(x for x, _ in rows) / len(rows)\n"
        "mean_y = sum(y for _, y in rows) / len(rows)\n"
        "print(f'mean=({mean_x:.3f}, {mean_y:.3f})')\n")),
    dict(name="center", outputs=["centered", "jitter"], inputs=["rows", "mean_x", "mean_y"], source=(
        "import random\n"
        "centered = [(x - mean_x, y - mean_y) for x, y in rows]\n"
        "jitter = [round(random.random(), 6) for _ in centered]\n"
        "print('first jitter draws:', jitter[:3])\n")),
    dict(name="fit", outputs=["slope"], inputs=["centered"], source=(
        "slope = sum(x * y for x, y in centered) / sum(x * x for x, _ in centered)\n"
        "print(f'slope={slope:.4f}')\n")),
]


def banner(title: str) -> None:
    print(f"\n{'=' * 8} {title} {'=' * 8}")


def table(app, env, names) -> None:
    for c in app.notebook.list_cells(env.id):
        kind = f"  [{c.last_failure_kind.value}]" if c.last_failure_kind and c.status.value == "failed" else ""
        print(f"  cell {c.position} {names[c.id]:<7} {c.status.value:<8}{kind}")


def run_summary(app, run, names) -> None:
    print(f"  run {run.id[:8]} mode={run.mode.value} status={run.status.value}"
          f"{' (' + run.failure_kind.value + ')' if run.failure_kind else ''}"
          f"{' - ' + run.message if run.message else ''}")
    for cr in app.runs.cell_runs(run.id):
        extra = ""
        if cr.error:
            extra = f"  {cr.error.type}: {cr.error.message} (line {cr.error.failing_lineno}: {cr.error.failing_line})"
        elif cr.outputs and cr.outputs[0].get("text"):
            extra = "  | " + cr.outputs[0]["text"].strip().splitlines()[0]
        print(f"    {names[cr.cell_id]:<7} {cr.status.value:<8}{extra}")


def main() -> None:
    shutil.rmtree(DATA, ignore_errors=True)
    app = build_app(Settings(data_dir=DATA))
    print("startup cleanup:", app.startup())
    try:
        banner("1. create PaperEnv + isolated uv environment")
        env = app.paper_envs.create("resnet-2015", paper_hash="sha256:demo", spec=EnvSpec(python_version="3.12", seed=42))
        built = app.envs.create_env(env.id)
        print(f"  alias=resnet-2015  env build: {built.status}  lockfile_hash={built.lockfile_hash[:16]}...")
        if not built.success:
            raise SystemExit(f"environment build failed: {built.error}\n{built.conflict_details}")

        banner("2. register a small dataset (local upload -> content-addressed cache, read-only)")
        src = DATA / "input" / "points.csv"
        src.parent.mkdir(parents=True)
        src.write_text("x,y\n" + "\n".join(f"{i},{2 * i + (i % 3) * 0.1:.2f}" for i in range(20)) + "\n")
        r = app.datasets.register_upload("toy-points", "v1", src, author="demo", license="CC0", splits=[
            DatasetSplit(name="train", definition="all rows", provenance=SplitProvenance.PAPER_OFFICIAL)])
        app.paper_envs.set_datasets(env.id, ["toy-points@v1"])
        print(f"  toy-points@v1 -> {r.status}, cached at {r.path}")

        banner("3. add 4 cells by hand")
        names = {}
        for spec in CELLS:
            c = app.notebook.add_cell(env.id, spec["source"], Author.HUMAN,
                                      declared_inputs=spec["inputs"], declared_outputs=spec["outputs"])
            names[c.id] = spec["name"]
        table(app, env, names)

        banner("4. run_all")
        run1 = app.runs.run_all(env.id)
        run_summary(app, run1, names)
        table(app, env, names)

        banner("5. break a cell through apply_edit (author=agent)")
        stats_id = next(i for i, n in names.items() if n == "stats")
        good = app.notebook.source_of(stats_id)
        v_bad = app.notebook.apply_edit(stats_id, good.replace("len(rows)", "len(rowz)", 1), Author.AGENT)
        print("  diff stored with the new CellVersion:\n" + "".join("    " + l for l in v_bad.diff.splitlines(True)))
        print("  statuses right after the edit (edited cell + everything downstream are stale):")
        table(app, env, names)

        banner("6. run_all again -> failure, remaining cells skipped (not failed)")
        run2 = app.runs.run_all(env.id)
        run_summary(app, run2, names)
        table(app, env, names)

        banner("7. fix it through apply_edit (revert to the previous good version)")
        v_fix = app.notebook.revert_to(app.notebook.versions(stats_id)[0].id, Author.HUMAN)
        print(f"  new version {v_fix.id[:8]} (author={v_fix.author.value}, parent={v_fix.parent_version_id[:8]})")
        print("  version history:", [(v.author.value, v.id[:6]) for v in app.notebook.versions(stats_id)])
        table(app, env, names)

        banner("8. run_clean (fresh kernel, fixed seed, the only verification-eligible mode)")
        run3 = app.runs.run_clean(env.id)
        run_summary(app, run3, names)
        table(app, env, names)

        banner("9. Run record")
        print(json.dumps(app.runs.get_run(run3.id).model_dump(mode="json"), indent=2))
        runs = app.runs.list_runs(env.id)
        print(f"\n  {len(runs)} runs recorded under notebook versions {[r.notebook_version for r in runs]} (none overwritten)")
        h = app.bus.history(limit=100000)
        counts: dict[str, int] = {}
        for e in h:
            counts[e.type.value] = counts.get(e.type.value, 0) + 1
        print("  persisted events:", dict(sorted(counts.items())))
        assert run3.verification_eligible and run3.status.value == "success"
    finally:
        app.shutdown()


if __name__ == "__main__":
    main()
