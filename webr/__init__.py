# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""webR -- causality tracing for multi-agent AI systems.

    from webr import webR_node

    @webR_node
    async def planner(task: str) -> str:
        ...

Every call becomes a node and every caller/callee relationship becomes an edge, so that
when the system fails silently the web can say where.
"""

from __future__ import annotations

from ._ids import new_node_id, new_trace_id
from .buffer import DEFAULT_CAPACITY, DEFAULT_PINNED_CAPACITY, TraceBuffer
from .decorator import submit, webR_node
from .detectors import DEFAULT_DETECTORS, DEFAULT_SUSPECT_SIGNALS, Detector, Payloads
from .fingerprint import fingerprint
from .graph import (
    SCHEMA_VERSION,
    export_graph,
    graph_from_jsonl,
    load_jsonl,
    write_graph,
)
from .links import Link, clear_marks, link, mark, mark_count, origin
from .propagation import (
    ContextVarPropagator,
    NodeRef,
    Propagator,
    get_propagator,
    new_root,
    set_propagator,
)
from .records import EdgeKind, EdgeRecord, ErrorInfo, NodeRecord, NodeStatus
from .runtime import (
    configure,
    disable,
    enable,
    flush,
    get_buffer,
    get_writer,
    is_enabled,
    reset,
    set_buffer,
    set_capture,
    set_detectors,
    set_suspect_signals,
    start_writer,
    stop_writer,
)
from .writer import JsonlWriter

__version__ = "0.0.1"

__all__ = [
    "DEFAULT_CAPACITY",
    "DEFAULT_DETECTORS",
    "DEFAULT_PINNED_CAPACITY",
    "DEFAULT_SUSPECT_SIGNALS",
    "SCHEMA_VERSION",
    "ContextVarPropagator",
    "Detector",
    "EdgeKind",
    "EdgeRecord",
    "ErrorInfo",
    "JsonlWriter",
    "Link",
    "NodeRecord",
    "NodeRef",
    "NodeStatus",
    "Payloads",
    "Propagator",
    "TraceBuffer",
    "__version__",
    "clear_marks",
    "configure",
    "disable",
    "enable",
    "export_graph",
    "fingerprint",
    "flush",
    "get_buffer",
    "get_propagator",
    "get_writer",
    "graph_from_jsonl",
    "is_enabled",
    "link",
    "load_jsonl",
    "mark",
    "mark_count",
    "new_node_id",
    "new_root",
    "new_trace_id",
    "origin",
    "reset",
    "set_buffer",
    "set_capture",
    "set_detectors",
    "set_propagator",
    "set_suspect_signals",
    "start_writer",
    "stop_writer",
    "submit",
    "webR_node",
    "write_graph",
]
