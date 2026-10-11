"""Experiment graph endpoints: build from the parsed paper, edit into a new version, compare."""
from test_pipeline_api import NO_GAPS, PUBLIC, Stack, parsed


def test_graph_is_built_edited_and_compared(migrated_url):
    s = Stack(migrated_url, parsed(PUBLIC), NO_GAPS)
    pid = s.upload()
    assert s.client.get(f"/papers/{pid}/graph").json()["current"] is None  # nothing understood yet
    s.run(pid)
    g = s.client.get(f"/papers/{pid}/graph").json()
    cur = g["current"]
    assert cur["version"] == 1 and cur["created_by"] == "agent"
    types = {n["type"] for n in cur["graph"]["nodes"]}
    assert {"dataset", "model", "train", "eval", "claim"} <= types

    body = {"base": 1, "edits": {"train": {"learning_rate": 0.5}},
            "cells": [{"node_id": "train", "source": "x = 1"}], "note": "higher lr"}
    v2 = s.client.post(f"/papers/{pid}/graph/versions", json=body)
    assert v2.status_code == 201, v2.text
    assert v2.json()["version"] == 2 and v2.json()["parent_version"] == 1
    assert s.client.post(f"/papers/{pid}/graph/versions", json={"base": 2}).status_code == 422  # nothing changed

    c = s.client.get(f"/papers/{pid}/graph/compare?a=1&b=2").json()
    assert "train" in [m["node_id"] for m in c["diff"]["modified"]]
    assert c["code_changes"] and c["attribution"] == []
    node = next(n for n in s.client.get(f"/papers/{pid}/graph?version=2").json()["current"]["graph"]["nodes"] if n["id"] == "train")
    assert node["spec"]["learning_rate"] == 0.5 and node["provenance"] == "human"
    assert s.client.delete(f"/papers/{pid}").status_code == 204
