import pytest

from crisisgrid_api.db.base import make_engine, make_session_factory
from crisisgrid_api.db.event_store import SqlEventStore
from crisisgrid_api.repos.interfaces import RunEvent
from crisisgrid_api.repos.memory import InMemoryEventStore


@pytest.fixture(params=["memory", "sql"])
def store(request, migrated_url):
    if request.param == "memory":
        return InMemoryEventStore()
    return SqlEventStore(make_session_factory(make_engine(migrated_url)))


def test_append_and_list_in_order_scoped_to_run(store):
    a = [store.append(RunEvent(run_id="r1", module="pipeline", kind=f"k{i}", payload={"i": i})) for i in range(3)]
    store.append(RunEvent(run_id="r2", module="pipeline", kind="other"))
    got = store.list("r1")
    assert [e.id for e in got] == [e.id for e in a]
    assert [e.payload["i"] for e in got] == [0, 1, 2]
    assert [e.kind for e in store.list("r2")] == ["other"]


def test_list_after_id_and_limit(store):
    evs = [store.append(RunEvent(run_id="r", module="m", kind=str(i))) for i in range(5)]
    assert [e.kind for e in store.list("r", after_id=evs[1].id)] == ["2", "3", "4"]
    assert [e.kind for e in store.list("r", limit=2)] == ["0", "1"]


def test_payload_and_timestamp_round_trip(store):
    e = store.append(RunEvent(run_id="r", module="m", kind="k", payload={"nested": {"a": [1, 2]}, "x": 1.5}))
    got = store.list("r")[0]
    assert got.payload == e.payload and got.module == "m" and got.ts.replace(tzinfo=None) == e.ts.replace(tzinfo=None)
