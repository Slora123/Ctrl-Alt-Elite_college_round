import pytest

from paper_env.domain import Author, CellStatus, EventType
from paper_env.services.notebook import CellBusy, NotebookService
from tests.test_repo_contract import make_env


@pytest.fixture
def nb(stack):
    env = stack.new_paper_env()
    return NotebookService(stack.uow, stack.bus), env, stack


def set_status(stack, cell, status):
    with stack.uow() as u:
        c = u.cells.get_cell(cell.id)
        c.status = status
        u.cells.update_cell(c)


def test_add_cell_creates_first_version_and_bumps_notebook_version(nb):
    svc, env, st = nb
    a = svc.add_cell(env.id, "x = 1", Author.GENERATOR, declared_outputs=["x"])
    b = svc.add_cell(env.id, "y = x", Author.HUMAN, declared_inputs=["x"])
    assert (a.position, b.position) == (0, 1) and a.status == CellStatus.PENDING
    assert [v.author for v in svc.versions(a.id)] == [Author.GENERATOR]
    assert svc.source_of(b.id) == "y = x"
    with st.uow() as u:
        assert u.paper_envs.get(env.id).current_notebook_version == 2


def test_insert_in_middle_shifts_positions_and_stales_later_cells(nb):
    svc, env, st = nb
    a, b = (svc.add_cell(env.id, s) for s in ("a=1", "b=2"))
    set_status(st, a, CellStatus.SUCCESS); set_status(st, b, CellStatus.SUCCESS)
    c = svc.add_cell(env.id, "c=3", position=1)
    cells = svc.list_cells(env.id)
    assert [x.id for x in cells] == [a.id, c.id, b.id]
    assert [x.status for x in cells] == [CellStatus.SUCCESS, CellStatus.PENDING, CellStatus.STALE]


def test_apply_edit_new_version_diff_author_event(nb):
    svc, env, st = nb
    c = svc.add_cell(env.id, "x = 1\n", Author.GENERATOR)
    v1 = svc.versions(c.id)[0]
    v2 = svc.apply_edit(c.id, "x = 2\n", Author.AGENT)
    assert v2.id != v1.id and v2.parent_version_id == v1.id and v2.author == Author.AGENT
    assert "-x = 1" in v2.diff and "+x = 2" in v2.diff
    assert svc.source_of(c.id) == "x = 2\n"
    assert svc.get_cell(c.id).current_version_id == v2.id
    assert [v.source for v in svc.versions(c.id)] == ["x = 1\n", "x = 2\n"]  # history intact
    assert svc.versions(c.id)[0].diff is None
    ev = [e for e in st.captured if e.type == EventType.CELL_EDITED][-1]
    assert ev.payload["change"] == "edited" and ev.payload["author"] == "agent" and ev.payload["diff"] == v2.diff


def test_apply_edit_marks_cell_and_downstream_stale_not_upstream_or_pending(nb):
    svc, env, st = nb
    cells = [svc.add_cell(env.id, f"v{i}={i}") for i in range(4)]
    for c in cells[:3]:
        set_status(st, c, CellStatus.SUCCESS)  # cell 3 never ran -> stays pending
    svc.apply_edit(cells[1].id, "v1 = 100", Author.HUMAN)
    got = [svc.get_cell(c.id).status for c in cells]
    assert got == [CellStatus.SUCCESS, CellStatus.STALE, CellStatus.STALE, CellStatus.PENDING]
    ev = [e for e in st.captured if e.type == EventType.CELL_EDITED][-1]
    assert ev.payload["stale_cells"] == [cells[1].id, cells[2].id]


def test_identical_edit_is_a_noop(nb):
    svc, env, st = nb
    c = svc.add_cell(env.id, "x = 1")
    set_status(st, c, CellStatus.SUCCESS)
    n_events = len(st.captured)
    v = svc.apply_edit(c.id, "x = 1", Author.AGENT)
    assert v.id == svc.versions(c.id)[0].id and len(svc.versions(c.id)) == 1
    assert svc.get_cell(c.id).status == CellStatus.SUCCESS and len(st.captured) == n_events


def test_cannot_edit_running_cell(nb):
    svc, env, st = nb
    c = svc.add_cell(env.id, "x = 1")
    set_status(st, c, CellStatus.RUNNING)
    with pytest.raises(CellBusy):
        svc.apply_edit(c.id, "x = 2", Author.HUMAN)


def test_revert_appends_new_version_with_old_source(nb):
    svc, env, st = nb
    c = svc.add_cell(env.id, "a = 1")
    v1 = svc.versions(c.id)[0]
    svc.apply_edit(c.id, "a = 2", Author.AGENT)
    svc.apply_edit(c.id, "a = 3", Author.AGENT)
    r = svc.revert_to(v1.id, Author.HUMAN)
    vs = svc.versions(c.id)
    assert len(vs) == 4 and r.source == "a = 1" and r.author == Author.HUMAN
    assert r.parent_version_id == vs[2].id and r.id != v1.id and vs[0].source == "a = 1"
    assert svc.source_of(c.id) == "a = 1"
    ev = [e for e in st.captured if e.type == EventType.CELL_EDITED][-1]
    assert ev.payload["change"] == "reverted" and ev.payload["reverted_to"] == v1.id


def test_input_warnings_follow_edits_and_declarations(nb):
    svc, env, _ = nb
    c = svc.add_cell(env.id, "y = x + 1")
    assert svc.get_cell(c.id).input_warnings == ["reads 'x' (line 1) which is not declared as an input"]
    svc.update_declarations(c.id, declared_inputs=["x"])
    assert svc.get_cell(c.id).input_warnings == []
    svc.apply_edit(c.id, "y = x + w", Author.HUMAN)
    assert "w" in svc.get_cell(c.id).input_warnings[0]


def test_delete_cell_repositions_and_stales(nb):
    svc, env, st = nb
    a, b, c = (svc.add_cell(env.id, s) for s in ("a=1", "b=2", "c=3"))
    for x in (a, b, c):
        set_status(st, x, CellStatus.SUCCESS)
    svc.delete_cell(b.id)
    cells = svc.list_cells(env.id)
    assert [(x.id, x.position) for x in cells] == [(a.id, 0), (c.id, 1)]
    assert cells[1].status == CellStatus.STALE and cells[0].status == CellStatus.SUCCESS


def test_cell_model_has_no_source_field_so_apply_edit_is_the_only_door():
    from paper_env.domain import Cell
    assert "source" not in Cell.model_fields
