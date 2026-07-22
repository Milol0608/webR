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

import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, TypeVar

from . import runtime
from .propagation import get_propagator
from .records import EdgeKind, EdgeRecord, next_seq

T = TypeVar("T")

#: How many marked objects are remembered. Each entry keeps its object alive.
MAX_MARKS = 2_048

#: Approximate byte budget for everything the registry is holding alive.
#:
#: A count alone is not a memory bound: 2,048 marked one-megabyte payloads measured at
#: 2.05GB retained, all of it objects the user had already finished with. Sizing uses
#: `sys.getsizeof`, which is shallow -- accurate for the `bytes`/`bytearray`/`str` payloads
#: that actually get large, an undercount for nested containers. An imperfect budget that
#: catches the real case beats a count that catches nothing.
MAX_MARK_BYTES = 64 * 1024 * 1024


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
# id(value) -> (value, link, approximate bytes). The value is a strong reference, which is
# what keeps id() valid -- and what makes the byte budget necessary.
_marks: OrderedDict[int, tuple[Any, Link, int]] = OrderedDict()
_marked_bytes = 0


def origin(label: str | None = None) -> Link | None:
    """A token naming the currently executing node, or None outside a traced call.

    Send it wherever the payload goes -- a queue message, an HTTP header, a job record --
    and `link` it on the far side.
    """
    current = get_propagator().current()
    if current is None:
        return None
    return Link(trace_id=current.trace_id, node_id=current.node_id, label=label)


def _has_reliable_identity(value: Any) -> bool:
    """Whether `id()` identity is meaningful for this value.

    It is not, for interned or cached immutables. CPython interns small ints, `True`,
    `False`, `None`, the empty string, and short string literals, so `"done" is "done"`
    and `0 is 0` are `True` for *distinct* logical values that different agents produced
    independently. Keying marks on `id()` for those fabricates data-dependency edges
    between agents that never exchanged anything -- exactly the lie this module exists to
    avoid. Containers and ordinary objects have stable, distinct identities and are fine.

    Text and scalars that genuinely need linking must use the token API instead
    (`origin()` / `link(token)`), which carries an explicit node id rather than trusting
    an address.
    """
    return not isinstance(value, str | bytes | int | float | complex | bool | type(None))


def mark(value: T, label: str | None = None) -> T:
    """Remember that this value came from the current node. Returns it unchanged.

    Returning the value makes it usable inline (`return webrtrace.mark(plan)`) without ever
    altering what the function produces.

    Values whose `id()` cannot be trusted -- strings, numbers, booleans, `None` -- are
    returned unchanged but **not** registered, so a later `link()` on an equal value
    reports no edge rather than a fabricated one. Use `origin()` / `link(token)` for those.
    """
    current = get_propagator().current()
    if current is None or not _has_reliable_identity(value):
        return value

    link = Link(trace_id=current.trace_id, node_id=current.node_id, label=label)
    try:
        size = sys.getsizeof(value)
    except Exception:  # exotic __sizeof__; assume small rather than refuse to mark
        size = 0

    global _marked_bytes
    with _lock:
        key = id(value)
        previous = _marks.pop(key, None)  # re-mark moves it to the newest position
        if previous is not None:
            _marked_bytes -= previous[2]
        _marks[key] = (value, link, size)
        _marked_bytes += size
        while len(_marks) > MAX_MARKS or (_marked_bytes > MAX_MARK_BYTES and len(_marks) > 1):
            _, evicted = _marks.popitem(last=False)
            _marked_bytes -= evicted[2]
    return value


def lookup(value: Any) -> Link | None:
    """The link for a marked value, or None if it was never marked or has been evicted."""
    if not _has_reliable_identity(value):
        # Never registered (see `mark`); returning None keeps interned values from
        # resolving to whichever node happened to mark an equal value first.
        return None
    with _lock:
        entry = _marks.get(id(value))
    if entry is None:
        return None
    stored, link, _size = entry
    # Identity, not equality: two equal lists are not the same datum, and treating them
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
    global _marked_bytes
    with _lock:
        _marks.clear()
        _marked_bytes = 0


def marked_bytes() -> int:
    """Approximate bytes the mark registry is currently holding alive."""
    with _lock:
        return _marked_bytes


def mark_count() -> int:
    """How many values are currently remembered. Bounded by `MAX_MARKS`."""
    with _lock:
        return len(_marks)
