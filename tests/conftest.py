# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Shared test helpers."""

from __future__ import annotations

import pytest

import webrtrace
from webrtrace.buffer import TraceBuffer
from webrtrace.records import NodeRecord, NodeStatus, next_seq

TRACE = "0" * 32


@pytest.fixture
def buffer() -> TraceBuffer:
    """A fresh buffer for one test, with the process-wide state restored afterwards.

    Tracing state is global by design, so tests must not leak it into each other.
    """
    original = webrtrace.get_buffer()
    fresh = webrtrace.configure(capacity=1_000, pinned_capacity=100)
    _reset_process_state()
    try:
        yield fresh
    finally:
        webrtrace.set_buffer(original)
        _reset_process_state()


def _reset_process_state() -> None:
    # Every knob a test can turn must be restored here. `capture_text` was missing, and a
    # test that disabled it silently changed the behaviour of the next test -- the kind of
    # cross-test leak that makes a suite quietly stop testing what it claims to.
    webrtrace.enable()
    webrtrace.set_capture(True, full=False, text=True)
    webrtrace.set_detectors(*webrtrace.DEFAULT_DETECTORS)
    webrtrace.set_suspect_signals(*webrtrace.DEFAULT_SUSPECT_SIGNALS)
    webrtrace.set_redactor(None)
    webrtrace.clear_marks()


def by_name(buffer: TraceBuffer, name: str) -> NodeRecord:
    """The single recorded node with this name; fails loudly if that is not true."""
    matches = [r for r in buffer.records() if r.name == name]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one node named {name!r}, got {len(matches)}")
    return matches[0]


def all_named(buffer: TraceBuffer, name: str) -> list[NodeRecord]:
    """Every recorded node with this name, in invocation order."""
    return [r for r in buffer.records() if r.name == name]


def make_record(
    node_id: str,
    *,
    parent_id: str | None = None,
    status: NodeStatus = NodeStatus.OK,
    tainted: bool = False,
    name: str = "node",
    trace_id: str = TRACE,
) -> NodeRecord:
    """A minimal completed node, for buffer and serialization tests."""
    return NodeRecord(
        trace_id=trace_id,
        node_id=node_id,
        parent_id=parent_id,
        name=name,
        seq=next_seq(),
        status=status,
        started_unix_ns=0,
        duration_ns=1,
        tainted=tainted,
    )
