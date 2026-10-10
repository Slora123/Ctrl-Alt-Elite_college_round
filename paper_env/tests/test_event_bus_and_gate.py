import pytest

from paper_env.domain import ActionKind, BlockedActionStatus, DecisionKind, EventType
from paper_env.hooks import Action, ActionBlocked, Decision

RS = {"run_id": "r", "mode": "all", "seed": 0, "notebook_version": 1}


def test_publish_persists_scrubbed_and_notifies(stack):
    stack.scrubber.add_secrets(["very-secret-token"])
    got = []
    sub = stack.bus.subscribe(got.append, types=[EventType.ENV_CHANGED])
    stack.bus.publish(EventType.RUN_STARTED, RS, run_id="r")
    ev = stack.bus.publish(EventType.ENV_CHANGED, {"op": "install", "note": "oops very-secret-token"}, paper_env_id="p")
    assert [e.seq for e in got] == [ev.seq]
    assert "very-secret-token" not in str(stack.bus.history()) and "[REDACTED]" in ev.payload["note"]
    sub.close()
    stack.bus.publish(EventType.ENV_CHANGED, {"op": "install"})
    assert len(got) == 1


def test_bad_subscriber_does_not_break_publisher(stack):
    stack.bus.subscribe(lambda e: 1 / 0)
    ok = []
    stack.bus.subscribe(ok.append)
    stack.bus.publish(EventType.RUN_STARTED, RS)
    assert len(ok) == 1


class DenyAll:
    def check(self, action):
        return Decision.deny("policy says no")


class Approve:
    def check(self, action):
        return Decision.needs_approval("ask a human")


def test_default_hook_allows(stack):
    stack.gate.check(Action(ActionKind.INSTALL_PACKAGE, paper_env_id="p"))
    assert stack.bus.history() == []


@pytest.mark.parametrize("hook,kind", [(DenyAll(), "deny"), (Approve(), "needs_approval")])
def test_non_allow_blocks_persists_and_emits(stack_factory, hook, kind):
    st = stack_factory(hook=hook)
    with pytest.raises(ActionBlocked) as ei:
        st.gate.check(Action(ActionKind.FETCH_DATASET, paper_env_id="p", payload={"dataset_id": "d"}))
    with st.uow() as u:
        pending = u.blocked.list_pending("p")
    assert [a.id for a in pending] == [ei.value.action_id]
    assert pending[0].decision == kind and pending[0].payload == {"dataset_id": "d"}
    assert st.event_types() == [EventType.ACTION_BLOCKED]


def test_resume_blocked_deny_runs_nothing(stack_factory):
    st = stack_factory(hook=Approve())
    ran = []
    st.gate.register_executor(ActionKind.FETCH_DATASET, ran.append)
    with pytest.raises(ActionBlocked) as ei:
        st.gate.check(Action(ActionKind.FETCH_DATASET, paper_env_id="p"))
    assert st.gate.resume_blocked(ei.value.action_id, Decision.deny("no")) is None
    assert ran == []
    with st.uow() as u:
        assert u.blocked.get(ei.value.action_id).status == BlockedActionStatus.DENIED
    with pytest.raises(ValueError):  # cannot be resolved twice
        st.gate.resume_blocked(ei.value.action_id, Decision.allow())


def test_resume_blocked_allow_executes_without_rechecking_hook(stack_factory):
    st = stack_factory(hook=DenyAll())
    ran = []
    st.gate.register_executor(ActionKind.FETCH_DATASET, lambda b: ran.append(b.payload) or "done")
    with pytest.raises(ActionBlocked) as ei:
        st.gate.check(Action(ActionKind.FETCH_DATASET, payload={"k": 1}))
    assert st.gate.resume_blocked(ei.value.action_id, Decision.allow("approved")) == "done"
    assert ran == [{"k": 1}]
    assert st.event_types() == [EventType.ACTION_BLOCKED, EventType.ACTION_RESOLVED]
