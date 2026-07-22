# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""The immutable records that make up a web.

Records are the only thing that crosses from the traced thread to the writer thread, so
they are frozen and slotted: frozen because a record handed off must never be mutated
behind the writer's back, slotted because there is one per instrumented call and dict
overhead per instance is not free.

Fields that later milestones populate (`io`, `signals`) are declared now with defaults so
that the on-disk schema does not change shape as features land.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from itertools import count
from typing import Any

_seq_counter = count()


def next_seq() -> int:
    """Monotonic per-process sequence number.

    Wall-clock timestamps are not reliable for ordering: they have coarse resolution on
    some platforms and can move backwards. `seq` gives a total order within a process,
    which is what reconstructing the web actually needs.
    """
    return next(_seq_counter)


class NodeStatus(str, Enum):
    """Terminal state of a node.

    `SUSPECT` is webR's reason to exist: the call returned normally, but a validator or
    detector believes the output is wrong. Nothing raised, so nothing else would notice.
    """

    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    SUSPECT = "suspect"


class EdgeKind(str, Enum):
    """How one node is connected to another.

    `INVOKES` is control flow and is inferred from propagation. `SENDS` is data
    dependency between nodes that never call each other, and must be declared.
    """

    INVOKES = "invokes"
    SENDS = "sends"


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """A captured exception. The traceback is a rendered string, never a frame object.

    Holding real frames would keep every local in the failing stack alive for as long as
    the record sits in the buffer, which is a memory leak with an innocent face.
    """

    type: str
    message: str
    traceback: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "message": self.message, "traceback": self.traceback}


@dataclass(frozen=True, slots=True)
class NodeRecord:
    """One completed invocation of an instrumented callable."""

    trace_id: str
    node_id: str
    parent_id: str | None
    name: str
    seq: int
    status: NodeStatus
    started_unix_ns: int
    duration_ns: int
    depth: int = 0
    error: ErrorInfo | None = None
    tainted: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)
    # Populated from milestone 4 onward.
    io: dict[str, Any] | None = None
    signals: dict[str, Any] | None = None

    @property
    def is_interesting(self) -> bool:
        """Whether this node must survive age-based eviction (see ADR 0001)."""
        return self.status in (NodeStatus.ERROR, NodeStatus.SUSPECT) or self.tainted

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping. Keys with no value are omitted to keep JSONL lines small."""
        out: dict[str, Any] = {
            # A JSONL stream carries both nodes and edges, so every line says which it
            # is. Inferring the type from which keys happen to be present would break the
            # moment either shape grew a field.
            "record": "node",
            "trace_id": self.trace_id,
            "node_id": self.node_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "seq": self.seq,
            "status": self.status.value,
            "started_unix_ns": self.started_unix_ns,
            "duration_ns": self.duration_ns,
            "depth": self.depth,
        }
        if self.error is not None:
            out["error"] = self.error.to_dict()
        if self.tainted:
            out["tainted"] = True
        if self.attributes:
            out["attributes"] = self.attributes
        if self.io is not None:
            out["io"] = self.io
        if self.signals is not None:
            out["signals"] = self.signals
        return out


@dataclass(frozen=True, slots=True)
class EdgeRecord:
    """A declared connection between two nodes.

    `INVOKES` edges are derivable from `NodeRecord.parent_id` and are not stored twice;
    this type carries the explicit `SENDS` edges added in milestone 5.
    """

    trace_id: str
    kind: EdgeKind
    src_id: str
    dst_id: str
    seq: int
    label: str | None = None

    #: Edges are never pinned on their own: an edge matters because of the nodes it
    #: joins, and those carry their own retention.
    is_interesting = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "record": "edge",
            "trace_id": self.trace_id,
            "kind": self.kind.value,
            "src_id": self.src_id,
            "dst_id": self.dst_id,
            "seq": self.seq,
        }
        if self.label is not None:
            out["label"] = self.label
        return out


@dataclass(frozen=True, slots=True)
class NodeOpen:
    """An 'I have started' marker, written to the durable stream when a node opens.

    It exists for one failure mode: a node that never returns -- a hang, or a process
    killed mid-call -- emits no `NodeRecord`, so without this it would be *absent* from
    the trace, and the trace would point at whatever did finish. The open marker means a
    hung node still appears, as `running`.

    It goes only to the JSONL writer, never to the bounded in-memory buffer: hang
    detection is a post-mortem question you ask of the durable file, and doubling the
    buffer's churn to answer a question it cannot answer would be pure cost. At export,
    an open marker with no matching terminal record becomes a `running` node; a terminal
    record supersedes its open marker by sharing `node_id` and `seq`.
    """

    trace_id: str
    node_id: str
    parent_id: str | None
    name: str
    seq: int
    started_unix_ns: int
    depth: int = 0
    # Never pinned or made urgent on its own; it is provisional until the node finishes.
    is_interesting = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "record": "open",
            "trace_id": self.trace_id,
            "node_id": self.node_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "seq": self.seq,
            "started_unix_ns": self.started_unix_ns,
            "depth": self.depth,
        }
        return out


def now_unix_ns() -> int:
    """Wall-clock timestamp, for correlating a web against external logs."""
    return time.time_ns()
