"""The default hook allows everything, so the deny / needs_approval paths must be proven explicitly,
for EVERY action kind the harness can gate."""
import pytest
from pydantic import ValidationError

from paper_env.domain import (
    ActionKind, BlockedActionStatus, CellStatus, DatasetManifest, DecisionKind, EventType, RunStatus, SourceType,
)
from paper_env.events import json_schemas, validate_payload
from paper_env.hooks import Action, AllowAllHook, Decision


class Recording:
    """Hook that records what it was asked and answers with a fixed decision."""

    def __init__(self, decision):
        self.decision, self.seen = decision, []

    def check(self, action):
        self.seen.append(action)
        return self.decision


DENY = Decision.deny("policy: no")
ASK = Decision.needs_approval("needs a human")


def test_allow_all_is_default_and_never_blocks():
    assert AllowAllHook().check(Action(ActionKind.EXECUTE_CELL)).kind == DecisionKind.ALLOW


@pytest.mark.parametrize("decision,label", [(DENY, "deny"), (ASK, "needs_approval")])
def test_hook_sees_every_gated_action_kind_and_each_is_paused(kstack_factory, decision, label, tmp_path):
    hook = Recording(decision)
    st = kstack_factory(hook=hook)
    env = st.new_ready_env()
    (c,) = st.add_cells(env, "print('x')")
    st.datasets.register(DatasetManifest(dataset_id="d@v1", name="d", version="v1", source_type=SourceType.URL,
                                         source_uri="http://example.invalid/f"))
    results = {
        ActionKind.EXECUTE_CELL: st.runs.run_cell(c.id),
        ActionKind.INSTALL_PACKAGE: st.envs.install_package(env.id, "demo_c", "1.0"),
        ActionKind.PIN_VERSION: st.envs.pin_version(env.id, "demo_c", "1.0"),
        ActionKind.REBUILD_ENV: st.envs.rebuild_env(env.id),
        ActionKind.FETCH_DATASET: st.datasets.fetch("d@v1"),
    }
    assert {a.kind for a in hook.seen} == set(ActionKind)  # the hook was consulted for all five kinds
    assert results[ActionKind.EXECUTE_CELL].status == RunStatus.BLOCKED
    for kind in (ActionKind.INSTALL_PACKAGE, ActionKind.PIN_VERSION, ActionKind.REBUILD_ENV):
        assert results[kind].status == "blocked" and results[kind].blocked_action_id
    assert results[ActionKind.FETCH_DATASET].status == "blocked"
    pending = st.gate.list_pending(env.id) + [a for a in st.gate.list_pending() if a.paper_env_id is None]
    assert {a.kind for a in pending} == set(ActionKind) and all(a.decision == label for a in pending)
    assert all(a.status == BlockedActionStatus.PENDING for a in pending)
    # nothing ran
    assert st.cell(c.id).status == CellStatus.BLOCKED
    assert EventType.CELL_STARTED not in st.event_types() and EventType.ENV_CHANGED not in st.event_types()
    assert st.event_types().count(EventType.ACTION_BLOCKED) == 5


def test_blocked_payload_is_scrubbed_and_describes_the_action(kstack_factory):
    st = kstack_factory(hook=Recording(ASK))
    st.scrubber.add_secrets(["super-secret-source-token"])
    env = st.new_ready_env()
    (c,) = st.add_cells(env, "token = 'super-secret-source-token'\nprint(token)")
    run = st.runs.run_cell(c.id)
    blocked = st.gate.list_pending(env.id)[0]
    assert blocked.payload["cell_id"] == c.id and "super-secret-source-token" not in str(blocked.payload)
    assert "[REDACTED]" in blocked.payload["source"]
    ev = [e for e in st.captured if e.type == EventType.ACTION_BLOCKED][0]
    assert ev.payload["action_id"] == blocked.id and "super-secret-source-token" not in str(ev.payload)


def test_install_blocked_then_approved_installs_and_denied_does_not(kstack_factory, wheels):
    class Toggle:
        def __init__(self): self.mode = ASK
        def check(self, a): return self.mode

    hook = Toggle()
    st = kstack_factory(hook=hook)
    hook.mode = Decision.allow()
    env = st.new_paper_env(alias="inst")
    assert st.envs.create_env(env.id).success
    hook.mode = ASK
    r = st.envs.install_package(env.id, "demo_c", "1.0")
    assert r.status == "blocked"
    done = st.gate.resume_blocked(r.blocked_action_id, Decision.allow("ok"))
    assert done.success and "demo_c==1.0" in st.spec(env).requirements
    r2 = st.envs.install_package(env.id, "demo_b", "1.0")
    assert st.gate.resume_blocked(r2.blocked_action_id, Decision.deny("no")) is None
    assert "demo_b==1.0" not in st.spec(env).requirements
    states = [a for a in st.event_types() if a == EventType.ACTION_RESOLVED]
    assert len(states) == 2


def test_blocked_run_all_style_plan_continues_after_approval(kstack_factory):
    class FirstAsk:
        n = 0
        def check(self, a):
            FirstAsk.n += 1
            return ASK if FirstAsk.n == 2 else Decision.allow()

    st = kstack_factory(hook=FirstAsk())
    env = st.new_ready_env()
    a, b, c = st.add_cells(env, "x = 1", "x += 1", "print(x)")
    run = st.runs._begin(env.id, __import__("paper_env.domain", fromlist=["RunMode"]).RunMode.ALL, None, fresh_kernel=True)
    run = st.runs._execute_plan(run, [a.id, b.id, c.id], stop_on_fail=True, cell_timeout=None)
    assert run.status == RunStatus.BLOCKED
    crs = st.cell_runs(run)
    assert [x.status for x in crs] == [CellStatus.SUCCESS, CellStatus.BLOCKED]
    out = st.gate.resume_blocked(crs[1].blocked_action_id, Decision.allow())
    assert out.status == RunStatus.SUCCESS
    final = st.cell_runs(run)
    assert [x.status for x in final] == [CellStatus.SUCCESS, CellStatus.SUCCESS, CellStatus.SUCCESS]
    assert final[2].outputs[0]["text"] == "2\n"


def test_crashing_hook_fails_closed(stack_factory):
    class Boom:
        def check(self, a): raise RuntimeError("guardrail bug")

    st = stack_factory(hook=Boom())
    r = st.envs.rebuild_env(st.new_paper_env().id)
    assert r.status == "blocked"
    with st.uow() as u:
        (a,) = u.blocked.list_pending()
    assert a.decision == "deny" and "failing closed" in a.reason


# ------------------------------------------------------------------ typed events
def test_every_event_type_has_a_schema_except_none_missing():
    schemas = json_schemas()
    assert set(schemas) == {t.value for t in EventType}


def test_malformed_payloads_are_rejected_at_publish(stack):
    with pytest.raises(ValidationError):
        stack.bus.publish(EventType.CELL_FAILED, {"cell_id": "c"})  # missing exception details
    with pytest.raises(ValidationError):
        stack.bus.publish(EventType.RUN_STARTED, {"run_id": "r", "mode": "bogus", "seed": 0, "notebook_version": 1})
    assert stack.bus.history() == []


def test_extra_fields_allowed_and_preserved():
    p = validate_payload(EventType.ENV_CHANGED, {"op": "install", "packages": ["a"]})
    assert p == {"op": "install", "packages": ["a"]}


def test_history_replay_and_filters_for_late_subscribers(stack):
    rs = {"run_id": "r1", "mode": "all", "seed": 0, "notebook_version": 1}
    e1 = stack.bus.publish(EventType.RUN_STARTED, rs, run_id="r1", paper_env_id="p")
    e2 = stack.bus.publish(EventType.RUN_FINISHED, {"run_id": "r1", "status": "success"}, run_id="r1", paper_env_id="p")
    stack.bus.publish(EventType.RUN_STARTED, {**rs, "run_id": "r2"}, run_id="r2", paper_env_id="q")
    assert [e.seq for e in stack.bus.history(after_seq=e1.seq, run_id="r1")] == [e2.seq]
    assert len(stack.bus.history(paper_env_id="p")) == 2
    q, sub = stack.bus.subscribe_queue(run_id="r3")
    stack.bus.publish(EventType.RUN_STARTED, {**rs, "run_id": "r3"}, run_id="r3")
    assert q.get_nowait().run_id == "r3" and q.empty()
    sub.close()


def test_events_survive_restart_of_the_bus(uow_factory):
    from paper_env.events import EventBus
    EventBus(uow_factory).publish(EventType.ENV_CHANGED, {"op": "install"})
    assert [e.type for e in EventBus(uow_factory).history()] == [EventType.ENV_CHANGED]
