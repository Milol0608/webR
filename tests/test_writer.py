# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""JSONL streaming: durability, ordering, bounded queueing, and rotation."""

from __future__ import annotations

import json
import time

import pytest
from conftest import make_record

import webrtrace
from webrtrace.records import NodeStatus
from webrtrace.writer import JsonlWriter


@pytest.fixture
def writer_path(tmp_path):
    return tmp_path / "trace.jsonl"


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def wait_for_lines(path, count, timeout=5.0):
    """Poll until the file holds `count` lines, or fail with what it actually held.

    The writer is asynchronous, so tests must wait on an outcome rather than assume a
    timing. A generous deadline plus a real error message beats a busy loop that fails
    mysteriously on a loaded CI box.
    """
    deadline = time.monotonic() + timeout
    lines = []
    while time.monotonic() < deadline:
        lines = read_lines(path)
        if len(lines) >= count:
            return lines
        time.sleep(0.01)
    raise AssertionError(f"expected {count} lines within {timeout}s, got {len(lines)}")


def test_records_are_written_as_one_json_object_per_line(writer_path):
    writer = JsonlWriter(writer_path, flush_interval=0.05)
    try:
        for i in range(5):
            writer.submit(make_record(f"n{i}"))
        writer.flush()
    finally:
        writer.stop()

    lines = read_lines(writer_path)
    assert [line["node_id"] for line in lines] == ["n0", "n1", "n2", "n3", "n4"]
    assert writer.stats()["written"] == 5


def test_failed_nodes_are_flushed_immediately(writer_path):
    # The crash-survival property: when something goes wrong, it must already be on disk
    # rather than sitting in a queue waiting for the next interval.
    writer = JsonlWriter(writer_path, flush_interval=60.0)
    try:
        writer.submit(make_record("boom", status=NodeStatus.ERROR))
        # No flush() call, and the periodic interval is a minute away: the record can
        # only be on disk because the failure woke the writer immediately.
        assert [line["node_id"] for line in wait_for_lines(writer_path, 1)] == ["boom"]
    finally:
        writer.stop()


def test_stop_drains_everything_queued(writer_path):
    writer = JsonlWriter(writer_path, flush_interval=60.0)
    for i in range(50):
        writer.submit(make_record(f"n{i}"))
    writer.stop()

    assert len(read_lines(writer_path)) == 50


def test_stop_is_idempotent(writer_path):
    writer = JsonlWriter(writer_path)
    writer.submit(make_record("a"))
    writer.stop()
    writer.stop()

    assert len(read_lines(writer_path)) == 1


def test_queue_is_bounded_and_drops_are_counted(writer_path):
    # A writer that cannot keep up must not become an unbounded memory leak, and must
    # say so rather than silently implying the trace is complete.
    writer = JsonlWriter(writer_path, flush_interval=60.0, queue_capacity=10)
    try:
        for i in range(100):
            writer.submit(make_record(f"n{i}"))
        stats = writer.stats()
        assert stats["dropped"] == 90
        assert stats["pending"] <= 10
    finally:
        writer.stop()


def test_appending_to_an_existing_file_preserves_earlier_records(writer_path):
    first = JsonlWriter(writer_path)
    first.submit(make_record("a"))
    first.stop()

    second = JsonlWriter(writer_path)
    second.submit(make_record("b"))
    second.stop()

    assert [line["node_id"] for line in read_lines(writer_path)] == ["a", "b"]


def test_file_rotates_once_the_size_limit_is_passed(writer_path):
    writer = JsonlWriter(writer_path, flush_interval=60.0, rotate_bytes=200)
    try:
        for i in range(20):
            writer.submit(make_record(f"n{i}"))
            writer.flush()
    finally:
        writer.stop()

    rotated = sorted(writer_path.parent.glob("trace.jsonl*"))
    assert len(rotated) > 1
    assert writer.stats()["rotations"] > 0


def test_unserializable_attributes_do_not_crash_the_writer(writer_path):
    # A tracing library must never raise inside its own writer because a user attached
    # an exotic object to a node.
    class Exotic:
        def __repr__(self):
            return "<exotic>"

    record = make_record("a")
    record.attributes["thing"] = Exotic()

    writer = JsonlWriter(writer_path)
    try:
        writer.submit(record)
        writer.flush()
    finally:
        writer.stop()

    assert read_lines(writer_path)[0]["attributes"]["thing"] == "<exotic>"


# --- integration with the decorator -------------------------------------------------


def test_traced_calls_reach_the_jsonl_file(buffer, tmp_path):
    path = tmp_path / "run.jsonl"

    @webrtrace.webR_node(name="child")
    def child():
        return 1

    @webrtrace.webR_node(name="parent")
    def parent():
        return child()

    webrtrace.start_writer(path, flush_interval=0.05)
    try:
        parent()
        webrtrace.flush()
        lines = read_lines(path)
    finally:
        webrtrace.stop_writer()

    names = {line["name"] for line in lines}
    assert names == {"parent", "child"}


def test_evicted_nodes_still_survive_on_disk(tmp_path):
    # The point of streaming: the in-memory buffer is a bounded cache, and eviction from
    # it loses nothing permanently.
    path = tmp_path / "run.jsonl"
    small = webrtrace.configure(capacity=5, pinned_capacity=5)

    @webrtrace.webR_node(name="agent")
    def agent():
        return None

    webrtrace.start_writer(path, flush_interval=0.05)
    try:
        for _ in range(50):
            agent()
        webrtrace.flush()
        lines = read_lines(path)
    finally:
        webrtrace.stop_writer()
        webrtrace.configure()

    assert len(small.records()) == 5
    # The stream also carries an "open" marker per node (for hang detection), so count
    # terminal node records rather than raw lines.
    terminal = [line for line in lines if line.get("record", "node") == "node"]
    assert len(terminal) == 50


def test_starting_a_second_writer_stops_the_first(tmp_path):
    first_path, second_path = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    first = webrtrace.start_writer(first_path)
    try:
        second = webrtrace.start_writer(second_path)
        assert webrtrace.get_writer() is second
        assert first is not second
        # The displaced writer is closed, not left running against the same process.
        first.submit(make_record("late"))
        assert first.stats()["pending"] <= 1
    finally:
        webrtrace.stop_writer()
