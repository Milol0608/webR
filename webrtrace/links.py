# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Explicit data-dependency edges.

Call edges are free -- if A calls B, propagation sees it. But the edge that actually
breaks a multi-agent system is usually invisible to the call stack::

    plan = planner()          # planner produces a plan
    ...                       # 200 lines later, a different task
    result = executor(plan)   # executor consumes it

Nothing connects `planner` to `executor` structurally, yet a bad plan is exactly how
these systems fail. `SENDS` edges record that relationship.

They are **explicit**, per ADR 0001. Inferring them automatically would mean silently
tagging arbitrary objects, and Python forbids attributes on `str` -- the type agents pass
most. A detector that fails silently on the common case has no business inside a tool
built to catch silent failure.

Two ways to declare one:

**Marking**, when producer and consumer share a process::

    plan = build()
    webrtrace.mark(plan)        # in the producer
    ...
    webrtrace.link(plan)        # in the consumer -> edge producer -> consumer

**Tokens**, when they do not -- a queue, a socket, another machine::

    token = webrtrace.origin()  # serializable; send it alongside the payload
    webrtrace.link(token)       # on the far side

Marking is keyed on object *identity*, and the registry holds a strong reference. That is
deliberate: a live reference makes `id()` stable, closing the recycled-address hole that
would otherwise attribute an edge to whatever object happened to land at that address
next. The registry is bounded, so the retention is too.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, TypeVar

from . import runtime
from .propagation import get_propagator
from .records import EdgeKind, EdgeRecord, next_seq

T = TypeVar("T")

#: How many marked objects are remembered. Each entry keeps its object alive, so this is
#: the knob that bounds how much memory marking can hold.
MAX_MARKS = 2_048


@dataclass(frozen=True, slots=True)
class Link:
    """A serializable reference to the node that produced a value."""

    trace_id: str
    node_id: str
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"trace_id": self.trace_id, "node_id": self.node_id, "label": self.label}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Link:
        return cls(
            trace_id=payload["trace_id"],
            node_id=payload["node_id"],
            label=payload.get("label"),
        )


_lock = threading.Lock()
_marks: OrderedDict[int, tuple[Any, Link]] = OrderedDict()


def origin(label: str | None = None) -> Link | None:
    """A token naming the currently executing node, or None outside a traced call.

    Send it wherever the payload goes -- a queue message, an HTTP header, a job record --
    and `link` it on the far side.
    """
    current = get_propagator().current()
    if current is None:
        return None
    return Link(trace_id=current.trace_id, node_id=current.node_id, label=label)


def mark(value: T, label: str | None = None) -> T:
    """Remember that this value came from the current node. Returns it unchanged.

    Returning the value makes it usable inline (`return webrtrace.mark(plan)`) without ever
    altering what the function produces.
    """
    current = get_propagator().current()
    if current is None:
        return value

    link = Link(trace_id=current.trace_id, node_id=current.node_id, label=label)
    with _lock:
        key = id(value)
        _marks.pop(key, None)  # re-mark moves it to the newest position
        _marks[key] = (value, link)
        while len(_marks) > MAX_MARKS:
            _marks.popitem(last=False)
    return value


def lookup(value: Any) -> Link | None:
    """The link for a marked value, or None if it was never marked or has been evicted."""
    with _lock:
        entry = _marks.get(id(value))
    if entry is None:
        return None
    stored, link = entry
    # Identity, not equality: two equal strings are not the same datum, and treating them
    # as one would invent edges that never existed.
    return link if stored is value else None


def link(source: Any, label: str | None = None) -> bool:
    """Record that the current node consumed something produced elsewhere.

    `source` is either a value previously passed to `mark`, or a `Link` token. Returns
    whether an edge was recorded -- False when there is no active node, or when the
    source was never marked. It never raises: a missing link is a gap in the web, not a
    reason to break the program being traced.
    """
    current = get_propagator().current()
    if current is None:
        return False

    resolved = source if isinstance(source, Link) else lookup(source)
    if resolved is None:
        return False
    if resolved.node_id == current.node_id:
        return False  # a node consuming its own output is not an edge

    runtime.emit_edge(
        EdgeRecord(
            trace_id=current.trace_id,
            kind=EdgeKind.SENDS,
            src_id=resolved.node_id,
            dst_id=current.node_id,
            seq=next_seq(),
            # `label if label is not None` rather than `label or`: an explicit empty
            # string is a caller deliberately suppressing the mark's label.
            label=label if label is not None else resolved.label,
        )
    )
    return True


def clear_marks() -> None:
    """Forget every marked value, releasing the references held with them."""
    with _lock:
        _marks.clear()


def mark_count() -> int:
    """How many values are currently remembered. Bounded by `MAX_MARKS`."""
    with _lock:
        return len(_marks)
