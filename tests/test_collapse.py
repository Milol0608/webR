# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""The per-agent aggregate view promised when ADR 0001 chose per-invocation nodes."""

from __future__ import annotations

import contextlib

import webrtrace
from webrtrace import webR_node
from webrtrace.collapse import collapse_by_agent
from webrtrace.render import render, render_summary


def build_fanout(worker_count: int = 5, failing: int | None = None):
    @webR_node(name="llm_call")
    def llm_call(i):
        return f"response {i}"

    @webR_node(name="worker")
    def worker(i):
        llm_call(i)
        if failing is not None and i == failing:
            raise RuntimeError("worker failed")
        return i

    @webR_node(name="orchestrator")
    def orchestrator():
        for i in range(worker_count):
            with contextlib.suppress(RuntimeError):
                worker(i)

    orchestrator()


def test_repeated_invocations_become_one_node(buffer):
    build_fanout(worker_count=5)
    collapsed = collapse_by_agent(webrtrace.export_graph(buffer))

    names = {node["name"]: node for node in collapsed["nodes"]}
    assert set(names) == {"orchestrator", "worker", "llm_call"}
    assert names["worker"]["calls"] == 5
    assert names["llm_call"]["calls"] == 5
    assert collapsed["stats"]["collapsed_from"] == 11  # 1 + 5 + 5


def test_durations_are_summed_and_the_worst_is_kept(buffer):
    build_fanout(worker_count=4)
    raw = webrtrace.export_graph(buffer)
    collapsed = collapse_by_agent(raw)

    workers = [n for n in raw["nodes"] if n["name"] == "worker"]
    node = next(n for n in collapsed["nodes"] if n["name"] == "worker")

    assert node["duration_ns"] == sum(w["duration_ns"] for w in workers)
    assert node["max_duration_ns"] == max(w["duration_ns"] for w in workers)


def test_a_single_failure_is_not_hidden_by_the_majority(buffer):
    # The whole risk of an aggregate view: 4 successes and 1 failure must not read as
    # "ok". The worst status wins.
    build_fanout(worker_count=5, failing=2)
    collapsed = collapse_by_agent(webrtrace.export_graph(buffer))

    worker = next(node for node in collapsed["nodes"] if node["name"] == "worker")
    assert worker["status"] == "error"
    assert worker["errors"] == 1
    assert worker["calls"] == 5


def test_suspect_nodes_are_counted_separately(buffer):
    @webR_node(name="agent", check=lambda out: out != "bad")
    def agent(value):
        return value

    for value in ("good", "bad", "good"):
        agent(value)

    collapsed = collapse_by_agent(webrtrace.export_graph(buffer))
    node = collapsed["nodes"][0]
    assert node["suspects"] == 1
    assert node["status"] == "suspect"


def test_structure_is_preserved(buffer):
    build_fanout(worker_count=3)
    collapsed = collapse_by_agent(webrtrace.export_graph(buffer))

    by_id = {node["node_id"]: node["name"] for node in collapsed["nodes"]}
    edges = {(by_id[e["src_id"]], by_id[e["dst_id"]]) for e in collapsed["edges"]}
    assert edges == {("orchestrator", "worker"), ("worker", "llm_call")}

    orchestrator = next(n for n in collapsed["nodes"] if n["name"] == "orchestrator")
    assert orchestrator["parent_id"] is None
    assert collapsed["roots"] == [orchestrator["node_id"]]


def test_edges_carry_how_many_invocations_they_represent(buffer):
    build_fanout(worker_count=5)
    collapsed = collapse_by_agent(webrtrace.export_graph(buffer))

    edge = next(e for e in collapsed["edges"] if e["dst_id"] != e["src_id"])
    assert edge["count"] >= 1
    assert sum(e["count"] for e in collapsed["edges"]) == 10  # 5 worker + 5 llm_call


def test_same_name_under_different_parents_stays_separate(buffer):
    # Merging by name alone would invent a relationship the run never had.
    @webR_node(name="shared")
    def shared():
        return 1

    @webR_node(name="branch_a")
    def branch_a():
        shared()

    @webR_node(name="branch_b")
    def branch_b():
        shared()

    branch_a()
    branch_b()

    collapsed = collapse_by_agent(webrtrace.export_graph(buffer))
    shared_nodes = [node for node in collapsed["nodes"] if node["name"] == "shared"]
    assert len(shared_nodes) == 2


def test_taint_survives_collapsing(buffer):
    @webR_node(name="bad", check=lambda out: False)
    def bad():
        return "wrong"

    @webR_node(name="root")
    def root():
        bad()

    root()
    collapsed = collapse_by_agent(webrtrace.export_graph(buffer))

    assert next(n for n in collapsed["nodes"] if n["name"] == "root")["tainted"] is True


def test_collapsing_an_empty_web_is_safe():
    collapsed = collapse_by_agent({"nodes": [], "edges": [], "stats": {}})
    assert collapsed["nodes"] == []
    assert collapsed["collapsed"] is True


def test_the_renderer_shows_counts_and_says_it_collapsed(buffer):
    build_fanout(worker_count=5, failing=1)
    collapsed = collapse_by_agent(webrtrace.export_graph(buffer))

    output = render(collapsed)
    assert "worker x5" in output
    assert "1 err" in output
    assert "collapsed from" in render_summary(collapsed)
    output.encode("ascii")


def test_original_node_ids_are_kept_for_drilling_back_down(buffer):
    build_fanout(worker_count=3)
    raw = webrtrace.export_graph(buffer)
    collapsed = collapse_by_agent(raw)

    node = next(n for n in collapsed["nodes"] if n["name"] == "worker")
    raw_ids = {n["node_id"] for n in raw["nodes"] if n["name"] == "worker"}
    assert set(node["node_ids"]) == raw_ids
