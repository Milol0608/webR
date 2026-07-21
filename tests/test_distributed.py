# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Cross-process propagation: two processes, one web.

The important test here spawns a real subprocess. A same-process simulation would prove
nothing about the part that is actually hard -- that a trace context survives
serialization, a process boundary, and a separate JSONL file, and that the two halves
stitch back together at export.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from conftest import by_name

import webrtrace
from webrtrace import webR_node
from webrtrace.propagation import ContextVarPropagator, new_root

# --- the carrier ---------------------------------------------------------------------


def test_inject_outside_a_traced_call_is_empty(buffer):
    # Inventing a trace here would make the receiver the root of a trace that never had
    # a caller, which is worse than no link.
    assert webrtrace.inject() == {}


def test_inject_emits_a_w3c_traceparent(buffer):
    @webR_node(name="agent")
    def agent():
        return webrtrace.inject()

    carrier = agent()
    version, trace_id, node_id, flags = carrier["traceparent"].split("-")

    assert (version, flags) == ("00", "01")
    assert len(trace_id) == 32
    assert len(node_id) == 16
    assert trace_id == by_name(buffer, "agent").trace_id


def test_extract_round_trips_inject(buffer):
    propagator = ContextVarPropagator()
    root = new_root("producer")
    token = propagator.attach(root)
    try:
        carrier = propagator.inject()
    finally:
        propagator.detach(token)

    remote = propagator.extract(carrier)
    assert remote.trace_id == root.trace_id
    assert remote.node_id == root.node_id


@pytest.mark.parametrize(
    "carrier",
    [
        {},
        {"traceparent": ""},
        {"traceparent": "garbage"},
        {"traceparent": "00-tooshort-abc-01"},
        {"traceparent": f"00-{'0' * 32}-{'a' * 16}-01"},  # all-zero trace id is invalid
        {"traceparent": f"00-{'a' * 32}-{'0' * 16}-01"},  # all-zero span id is invalid
        {"traceparent": 12345},
    ],
    ids=["empty", "blank", "garbage", "malformed", "zero-trace", "zero-span", "not-a-str"],
)
def test_a_bad_carrier_costs_the_link_not_the_request(carrier):
    assert ContextVarPropagator().extract(carrier) is None


def test_headers_are_accepted_case_insensitively():
    propagator = ContextVarPropagator()
    valid = f"00-{'a' * 32}-{'b' * 16}-01"
    assert propagator.extract({"Traceparent": valid}) is not None


# --- adopting a remote parent --------------------------------------------------------


def test_remote_parent_makes_local_nodes_children_of_the_caller(buffer):
    carrier = {"traceparent": f"00-{'a' * 32}-{'b' * 16}-01"}

    @webR_node(name="handler")
    def handler():
        return 1

    with webrtrace.remote_parent(carrier):
        handler()

    record = by_name(buffer, "handler")
    assert record.trace_id == "a" * 32
    assert record.parent_id == "b" * 16
    assert record.depth == 1


def test_remote_parent_restores_the_previous_context(buffer):
    carrier = {"traceparent": f"00-{'a' * 32}-{'b' * 16}-01"}

    @webR_node(name="inner")
    def inner():
        return 1

    @webR_node(name="outer")
    def outer():
        with webrtrace.remote_parent(carrier):
            pass
        inner()  # must be a child of outer, not of the remote node

    outer()
    assert by_name(buffer, "inner").parent_id == by_name(buffer, "outer").node_id


def test_a_bad_carrier_simply_starts_a_new_trace(buffer):
    @webR_node(name="handler")
    def handler():
        return 1

    with webrtrace.remote_parent({"traceparent": "nonsense"}) as ref:
        handler()

    assert ref is None
    assert by_name(buffer, "handler").parent_id is None


def test_the_remote_node_is_not_recorded_locally(buffer):
    # The remote node belongs to the process that created it. Writing a local record for
    # it would duplicate the node when the two files are exported together.
    carrier = {"traceparent": f"00-{'a' * 32}-{'b' * 16}-01"}

    @webR_node(name="handler")
    def handler():
        return 1

    with webrtrace.remote_parent(carrier):
        handler()

    assert [r.name for r in buffer.records()] == ["handler"]


# --- the real thing ------------------------------------------------------------------

_CHILD = """
import sys, webrtrace
from webrtrace import webR_node

carrier = {"traceparent": sys.argv[1]}
webrtrace.start_writer(sys.argv[2])

@webR_node(name="child_worker")
def child_worker(prompt):
    return "done"

with webrtrace.remote_parent(carrier):
    child_worker("work from the parent process")

webrtrace.stop_writer()
"""


def test_a_real_subprocess_joins_the_parents_trace(buffer, tmp_path):
    traces = tmp_path / "traces"
    script = tmp_path / "child.py"
    script.write_text(textwrap.dedent(_CHILD), encoding="utf-8")

    webrtrace.start_writer(traces / "parent.jsonl", flush_interval=0.05)
    try:

        @webR_node(name="orchestrator")
        def orchestrator():
            carrier = webrtrace.inject()
            result = subprocess.run(
                [sys.executable, str(script), carrier["traceparent"], str(traces / "child.jsonl")],
                capture_output=True,
                text=True,
                timeout=90,
                cwd=str(tmp_path),
                env={**__import__("os").environ, "PYTHONPATH": str(_repo_root())},
            )
            assert result.returncode == 0, result.stderr
            return "spawned"

        orchestrator()
        webrtrace.flush()
    finally:
        webrtrace.stop_writer()

    # Both files, read together, must form one web.
    document = webrtrace.graph_from_jsonl(traces)
    names = {node["name"] for node in document["nodes"]}
    assert names == {"orchestrator", "child_worker"}
    assert len(document["traces"]) == 1, "the two processes must share one trace id"

    by_id = {node["node_id"]: node["name"] for node in document["nodes"]}
    edges = {(by_id[e["src_id"]], by_id[e["dst_id"]]) for e in document["edges"]}
    assert ("orchestrator", "child_worker") in edges
    assert document["stats"]["dangling_edges"] == 0


def _repo_root():
    from pathlib import Path

    return Path(webrtrace.__file__).resolve().parent.parent
