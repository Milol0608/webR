# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""webR -- causality tracing for multi-agent AI systems.

The public surface is intentionally small. Milestone 1 exposes the data model and the
propagation seam; `@webR_node` arrives in milestone 2.
"""

from __future__ import annotations

from ._ids import new_node_id, new_trace_id
from .buffer import DEFAULT_CAPACITY, DEFAULT_PINNED_CAPACITY, TraceBuffer
from .propagation import (
    ContextVarPropagator,
    NodeRef,
    Propagator,
    get_propagator,
    new_root,
    set_propagator,
)
from .records import EdgeKind, EdgeRecord, ErrorInfo, NodeRecord, NodeStatus

__version__ = "0.0.1"

__all__ = [
    "DEFAULT_CAPACITY",
    "DEFAULT_PINNED_CAPACITY",
    "ContextVarPropagator",
    "EdgeKind",
    "EdgeRecord",
    "ErrorInfo",
    "NodeRecord",
    "NodeRef",
    "NodeStatus",
    "Propagator",
    "TraceBuffer",
    "__version__",
    "get_propagator",
    "new_node_id",
    "new_root",
    "new_trace_id",
    "set_propagator",
]
