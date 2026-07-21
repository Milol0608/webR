# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Graph document assembly, from memory and from disk."""

from __future__ import annotations

import json

import pytest

import webr
from webr.graph import SCHEMA_VERSION, export_graph, graph_from_jsonl, load_jsonl, write_graph


def build_a_web():
    @webr.webR_node(name="extractor")
    def extractor():
        return 1

    @webr.webR_node(name="planner")
    def planner():
        return extractor()

    @webr.webR_node(name="orchestrator")
    def orchestrator():
        return planner()

    orchestrator()


def test_graph_has_nodes_edges_and_provenance(buffer):
    build_a_web()
    document = export_graph(buffer)

    assert document["schema"] == SCHEMA_VERSION
    assert document["webr_version"] == webr.__version__
    assert len(document["nodes"]) == 3
    assert len(document["edges"]) == 2
    assert len(document["traces"]) == 1
    assert document["stats"]["source"] == "buffer"


def test_edges_run_from_caller_to_callee(buffer):
    build_a_web()
    document = export_graph(buffer)

    by_id = {node["node_id"]: node["name"] for node in document["nodes"]}
    named_edges = {(by_id[e["src_id"]], by_id[e["dst_id"]]) for e in document["edges"]}
    assert named_edges == {("orchestrator", "planner"), ("planner", "extractor")}
    assert all(edge["kind"] == "invokes" for edge in document["edges"])


def test_roots_are_the_nodes_with_no_caller(buffer):
    build_a_web()
    document = export_graph(buffer)

    by_id = {node["node_id"]: node["name"] for node in document["nodes"]}
    assert [by_id[node_id] for node_id in document["roots"]] == ["orchestrator"]


def test_status_counts_are_reported(buffer):
    @webr.webR_node(name="boom")
    def boom():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        boom()
    build_a_web()

    stats = export_graph(buffer)["stats"]
    assert stats["by_status"] == {"error": 1, "ok": 3}


def test_dangling_edges_are_flagged_not_hidden(buffer):
    # An edge whose parent was evicted is still a real edge. The document reports it and
    # marks it, so a visualization can show the gap honestly.
    small = webr.configure(capacity=2, pinned_capacity=2)

    @webr.webR_node(name="child")
    def child():
        return 1

    @webr.webR_node(name="parent")
    def parent():
        return child()

    parent()

    @webr.webR_node(name="filler")
    def filler():
        return None

    for _ in range(10):
        filler()

    document = export_graph(small)
    dangling = [edge for edge in document["edges"] if edge.get("dangling")]
    assert document["stats"]["dangling_edges"] == len(dangling)
    assert document["stats"]["dropped"] > 0
    webr.configure()


def test_write_graph_produces_readable_json(buffer, tmp_path):
    build_a_web()
    destination = write_graph(tmp_path / "web.json", buffer)

    document = json.loads(destination.read_text(encoding="utf-8"))
    assert len(document["nodes"]) == 3


def test_graph_from_jsonl_round_trips_a_run(buffer, tmp_path):
    path = tmp_path / "run.jsonl"
    webr.start_writer(path, flush_interval=0.05)
    try:
        build_a_web()
        webr.flush()
    finally:
        webr.stop_writer()

    from_disk = graph_from_jsonl(path)
    from_memory = export_graph(buffer)

    assert from_disk["stats"]["source"] == "jsonl"
    assert len(from_disk["nodes"]) == len(from_memory["nodes"])
    assert len(from_disk["edges"]) == len(from_memory["edges"])


def test_graph_from_jsonl_reads_a_whole_directory_in_sequence_order(buffer, tmp_path):
    # Rotation splits a run across files; the reassembled web must not care.
    directory = tmp_path / "traces"
    webr.start_writer(directory / "run.jsonl", flush_interval=60.0, rotate_bytes=200)
    try:
        for _ in range(10):
            build_a_web()
        webr.flush()
    finally:
        webr.stop_writer()

    document = graph_from_jsonl(directory)
    assert document["stats"]["files_read"] > 1
    assert len(document["nodes"]) == 30
    seqs = [node["seq"] for node in document["nodes"]]
    assert seqs == sorted(seqs)


def test_partial_final_line_is_skipped_not_fatal(tmp_path):
    # A process killed mid-write leaves a truncated last line. A post-mortem tool that
    # refused to read the file because of it would be useless exactly when it matters.
    path = tmp_path / "run.jsonl"
    path.write_text(
        '{"node_id":"a","trace_id":"t","name":"x","status":"ok","seq":1,"parent_id":null}\n'
        '{"node_id":"b","trace_i',
        encoding="utf-8",
    )

    assert len(load_jsonl(path)) == 1
    assert len(graph_from_jsonl(path)["nodes"]) == 1
