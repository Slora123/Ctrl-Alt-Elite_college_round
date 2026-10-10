import threading
import time

import pytest

from paper_env.domain import EventType, RunStatus
from paper_env.kernel.runner import SessionError


def wait_for(pred, t=30):
    end = time.time() + t
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_complete_and_inspect_against_live_kernel(kstack):
    env = kstack.new_ready_env()
    (c,) = kstack.add_cells(env, "my_long_variable_name = {'a': 1}\ndef my_func(x, y=2):\n    'adds things'\n    return x + y")
    kstack.runs.run_cell(c.id)
    r = kstack.runner.complete(env.id, "my_long_va", 10)
    assert "my_long_variable_name" in r["matches"] and r["cursor_start"] == 0
    info = kstack.runner.inspect(env.id, "my_func", 7)
    assert info["found"] and "adds things" in info["data"]["text/plain"]


def test_completion_without_a_kernel_is_an_explicit_error(kstack):
    env = kstack.new_ready_env()
    with pytest.raises(SessionError):
        kstack.runner.complete(env.id, "x", 1)


def test_input_prompt_is_surfaced_and_answerable(kstack):
    env = kstack.new_ready_env()
    (c,) = kstack.add_cells(env, "name = input('who? ')\nprint('hello', name)")
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("run", kstack.runs.run_cell(c.id)))
    th.start()
    assert wait_for(lambda: EventType.INPUT_REQUESTED in kstack.event_types())
    ev = [e for e in kstack.captured if e.type == EventType.INPUT_REQUESTED][0]
    assert ev.payload["prompt"] == "who? " and ev.payload["password"] is False
    kstack.runner.provide_input(env.id, "ada")
    th.join(30)
    run = out["run"]
    assert run.status == RunStatus.SUCCESS
    texts = "".join(o.get("text", "") for o in kstack.cell_runs(run)[0].outputs)
    assert "hello ada" in texts


def test_unanswered_input_hits_the_cell_timeout_not_a_hang(kstack):
    env = kstack.new_ready_env()
    (c,) = kstack.add_cells(env, "input('never answered')")
    run = kstack.runs.run_cell(c.id, timeout=1.5)
    assert run.status == RunStatus.FAILED and kstack.cell_runs(run)[0].error.type == "CellTimeout"


def test_comm_round_trip_works_while_no_cell_is_running(kstack):
    """The widget protocol: kernel opens a comm, the frontend sends a message later, the kernel answers."""
    env = kstack.new_ready_env()
    code = (
        "import comm\n"
        "c = comm.create_comm(target_name='paperenv.test', data={'hello': 1})\n"
        "def on_msg(msg):\n"
        "    c.send({'echo': msg['content']['data']})\n"
        "c.on_msg(on_msg)\n"
        "print(c.comm_id)\n"
    )
    (cell,) = kstack.add_cells(env, code)
    run = kstack.runs.run_cell(cell.id)
    cid = kstack.cell_runs(run)[0].outputs[0]["text"].strip()
    comms = [e for e in kstack.captured if e.type == EventType.COMM_MESSAGE]
    assert comms[0].payload["msg_type"] == "comm_open" and comms[0].payload["content"]["comm_id"] == cid
    assert comms[0].seq is None  # ephemeral: not persisted
    assert not [e for e in kstack.bus.history() if e.type == EventType.COMM_MESSAGE]
    n = len(comms)
    kstack.runner.comm_send(env.id, "comm_msg", {"comm_id": cid, "data": {"ping": 42}})
    assert wait_for(lambda: len([e for e in kstack.captured if e.type == EventType.COMM_MESSAGE]) > n)
    reply = [e for e in kstack.captured if e.type == EventType.COMM_MESSAGE][-1].payload
    assert reply["msg_type"] == "comm_msg" and reply["content"]["data"] == {"echo": {"ping": 42}}


def test_comm_secrets_are_scrubbed(kstack):
    kstack.scrubber.add_secrets(["widget-secret-value"])
    env = kstack.new_ready_env()
    (cell,) = kstack.add_cells(env, "import comm\ncomm.create_comm(target_name='t', data={'v': 'widget-secret-value'})")
    kstack.runs.run_cell(cell.id)
    assert "widget-secret-value" not in str([e.payload for e in kstack.captured if e.type == EventType.COMM_MESSAGE])


def test_comm_rejects_unknown_message_types(kstack):
    env = kstack.new_ready_env()
    (cell,) = kstack.add_cells(env, "x = 1")
    kstack.runs.run_cell(cell.id)
    from paper_env.backends import BackendError
    with pytest.raises(BackendError):
        kstack.runner.comm_send(env.id, "execute_request", {"code": "import os"})
