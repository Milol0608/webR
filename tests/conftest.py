# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Shared test helpers."""

from __future__ import annotations

from webr.records import NodeRecord, NodeStatus, next_seq

TRACE = "0" * 32


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
