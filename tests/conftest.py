# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Shared test helpers."""

from __future__ import annotations

import pytest

import webr
from webr.buffer import TraceBuffer
from webr.records import NodeRecord, NodeStatus, next_seq

TRACE = "0" * 32


@pytest.fixture
def buffer() -> TraceBuffer:
    """A fresh buffer for one test, with the process-wide state restored afterwards.

    Tracing state is global by design, so tests must not leak it into each other.
    """
    original = webr.get_buffer()
    fresh = webr.configure(capacity=1_000, pinned_capacity=100)
    _reset_process_state()
    try:
        yield fresh
    finally:
        webr.set_buffer(original)
        _reset_process_state()


def _reset_process_state() -> None:
    webr.enable()
    webr.set_capture(True, full=False)
    webr.set_detectors(*webr.DEFAULT_DETECTORS)
    webr.set_suspect_signals(*webr.DEFAULT_SUSPECT_SIGNALS)


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
