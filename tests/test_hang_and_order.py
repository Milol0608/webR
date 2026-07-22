# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Batch 2: a hung node stays visible, and seq reflects invocation order."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time

import webrtrace
from webrtrace import webR_node
from webrtrace.graph import graph_from_jsonl


def test_seq_reflects_invocation_order_not_completion(buffer):
    # data-integrity #9: seq was assigned at completion, so concurrent siblings and even
    # a parent/child pair came out in the wrong order. It is now assigned at open.
    @webR_node(name="child")
    def child():
        return 1

    @webR_node(name="parent")
    def parent():
        return child()

    parent()

    parent_rec = next(r for r in buffer.records() if r.name == "parent")
    child_rec = next(r for r in buffer.records() if r.name == "child")
    # The parent is invoked before its child, so it must carry the lower seq.
    assert parent_rec.seq < child_rec.seq


def test_open_marker_only_reaches_disk_not_the_buffer(buffer, tmp_path):
    path = tmp_path / "run.jsonl"

    @webR_node(name="agent")
    def agent():
        return 1

    webrtrace.start_writer(path, flush_interval=0.05)
    try:
        agent()
        webrtrace.flush()
    finally:
        webrtrace.stop_writer()

    import json

    kinds = [json.loads(line)["record"] for line in path.read_text().splitlines() if line]
    assert "open" in kinds and "node" in kinds  # both on disk
    # But the in-memory buffer holds only the terminal record.
    assert len(buffer.records()) == 1


def test_a_completed_node_is_not_reported_as_running(buffer, tmp_path):
    path = tmp_path / "run.jsonl"

    @webR_node(name="agent")
    def agent():
        return 1

    webrtrace.start_writer(path, flush_interval=0.05)
    try:
        agent()
        webrtrace.flush()
    finally:
        webrtrace.stop_writer()

    document = graph_from_jsonl(path)
    assert document["stats"]["by_status"] == {"ok": 1}
    assert "running" not in document["stats"]


_HANGING_CHILD = """
import sys, time, webrtrace
from webrtrace import webR_node

webrtrace.start_writer(sys.argv[1], flush_interval=0.05)

@webR_node(name="cheap_helper")
def cheap_helper():
    return 1

@webR_node(name="vector_db_query")
def vector_db_query():
    cheap_helper()          # this finishes
    time.sleep(60)          # this never returns; the process will be killed

@webR_node(name="orchestrator")
def orchestrator():
    vector_db_query()

orchestrator()
"""


def test_a_killed_process_leaves_the_hung_node_visible(tmp_path):
    # break-it CRITICAL #1: without the open marker, a hung node emits no record and the
    # trace shows only what finished -- pointing at the wrong thing. With it, the hung
    # node appears as `running`.
    path = tmp_path / "hang.jsonl"
    script = tmp_path / "child.py"
    script.write_text(textwrap.dedent(_HANGING_CHILD), encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(script), str(path)],
        env={**__import__("os").environ, "PYTHONPATH": _repo_root()},
    )
    # Give it time to open the nodes and flush the markers, then kill it mid-hang.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            break
        time.sleep(0.1)
    time.sleep(0.5)
    proc.kill()
    proc.wait(timeout=10)

    document = graph_from_jsonl(path)
    names = {n["name"]: n["status"] for n in document["nodes"]}

    # cheap_helper finished (ok); the two nodes that were still on the stack when the
    # process died are visible as running rather than absent.
    assert names.get("cheap_helper") == "ok"
    assert names.get("vector_db_query") == "running"
    assert names.get("orchestrator") == "running"
    assert document["stats"]["running"] == 2


def _repo_root():
    from pathlib import Path

    return str(Path(webrtrace.__file__).resolve().parent.parent)
