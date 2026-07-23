# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""How a node learns who invoked it.

The decorator never touches `contextvars` directly. It asks a `Propagator`. That
indirection is the seam that lets v0.2 add cross-process propagation (`inject`/`extract`)
without rewriting the core -- see ADR 0001.

The default implementation stores the active `NodeRef` in a `ContextVar`. `asyncio` copies
the context when a task is spawned, so `gather` / `TaskGroup` fan-out is attributed
correctly with no cooperation from the caller. `asyncio.to_thread` copies it too. Raw
threads and process boundaries do not, and are handled explicitly in later milestones.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ._ids import new_node_id, new_trace_id
from .records import next_seq


class NodeState:
    """The mutable part of a node while it runs.

    A node cannot know at its own start whether it will end up tainted -- that depends on
    a descendant failing later. And a parent finishes *after* its children, so a child
    that fails must be able to mark parents that have not completed yet. `NodeRef` stays
    frozen (it is copied across contexts and must never be rebound); this small mutable
    companion carries what legitimately changes mid-call.

    `usage` lands here the same way: an instrumented model call learns its token counts
    only once the provider responds, which is partway through the node it belongs to.
    """

    __slots__ = ("tainted", "usage")

    def __init__(self) -> None:
        self.tainted = False
        self.usage: Any = None


@dataclass(frozen=True, slots=True)
class NodeRef:
    """A handle to a node that is currently executing.

    `parent` is a direct reference rather than a lookup key, which makes walking to the
    root an O(depth) pointer chase with no side table to keep in sync. The chain is
    needed at exactly one moment -- when a node fails and its ancestors must be pinned
    against eviction -- and at that moment the ancestors are typically still running, so
    they exist only here and not yet in the buffer.
    """

    trace_id: str
    node_id: str
    name: str
    depth: int = 0
    parent: NodeRef | None = None
    # Assigned when the node is *opened*, so it orders nodes by invocation, not by
    # completion. The record inherits it in `_finish`, and the open-event (for hang
    # detection) shares it, so both lines for one node carry the same seq.
    seq: int = field(default_factory=next_seq)
    # Excluded from equality and repr: it is incidental state, not part of identity.
    state: NodeState = field(default_factory=NodeState, compare=False, repr=False)

    def taint_ancestors(self) -> None:
        """Mark every node above this one as downstream of a failure.

        Taint flows *up* the call tree because that is the direction data flows: a parent
        consumed whatever this node produced, so if this output is wrong, everything that
        used it is suspect too.

        Stops at the first already-tainted ancestor. That is safe because this walk always
        runs to the root, so a tainted node's own ancestors are necessarily tainted
        already. Without the short-circuit, a deep chain where every node is suspect
        re-walked the whole chain at every level -- quadratic in depth, on the failure
        path.
        """
        node = self.parent
        while node is not None and not node.state.tainted:
            node.state.tainted = True
            node = node.parent

    def child(self, name: str) -> NodeRef:
        """Return a ref for a node invoked by this one, in the same trace."""
        return NodeRef(
            trace_id=self.trace_id,
            node_id=new_node_id(),
            name=name,
            depth=self.depth + 1,
            parent=self,
        )

    def ancestor_ids(self) -> tuple[str, ...]:
        """Ids from this node's parent up to the root, nearest first."""
        ids: list[str] = []
        node = self.parent
        while node is not None:
            ids.append(node.node_id)
            node = node.parent
        return tuple(ids)

    def chain_ids(self) -> tuple[str, ...]:
        """`ancestor_ids`, but including this node, nearest first."""
        return (self.node_id, *self.ancestor_ids())

    def iter_chain_ids(self) -> Iterator[str]:
        """`chain_ids` lazily, so a consumer that can stop early does not pay for the rest.

        `TraceBuffer.pin` stops at the first id it already knows about, which makes
        pinning a deep failing chain amortized O(1) instead of O(depth) per node.
        """
        node: NodeRef | None = self
        while node is not None:
            yield node.node_id
            node = node.parent


def new_root(name: str) -> NodeRef:
    """Start a new trace. Used when a node runs with no active parent."""
    return NodeRef(trace_id=new_trace_id(), node_id=new_node_id(), name=name, depth=0)


#: W3C Trace Context version and flags. webR does not implement sampling, so the flag
#: byte is constant -- but emitting the standard shape means an existing OpenTelemetry
#: collector can read the header, and vice versa.
_TRACEPARENT_VERSION = "00"
_TRACEPARENT_FLAGS = "01"

_TRACEPARENT_RE = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


@runtime_checkable
class Propagator(Protocol):
    """Carries the active `NodeRef` across whatever boundaries it can.

    `current`/`attach`/`detach` handle in-process propagation. `inject`/`extract` carry a
    trace across a boundary the runtime cannot cross on its own -- a queue message, an
    HTTP header, a subprocess argument -- by reducing the active node to a string.
    """

    def current(self) -> NodeRef | None:
        """The node executing right now, or None if outside any traced call."""
        ...

    def attach(self, ref: NodeRef) -> Any:
        """Make `ref` current. Returns a token to pass back to `detach`."""
        ...

    def detach(self, token: Any) -> None:
        """Restore whatever was current before the matching `attach`."""
        ...

    def inject(self) -> dict[str, str]:
        """A serializable carrier naming the active node, or `{}` if there is none."""
        ...

    def extract(self, carrier: dict[str, str]) -> NodeRef | None:
        """Rebuild a parent reference from a carrier, or None if it is absent/malformed."""
        ...


_current: ContextVar[NodeRef | None] = ContextVar("webr_current_node", default=None)


class ContextVarPropagator:
    """Default propagator: a `ContextVar` holding the active node.

    Attach/detach is strictly paired and the token is opaque, so nested and concurrent
    calls restore correctly even when they interleave.
    """

    __slots__ = ()

    def current(self) -> NodeRef | None:
        return _current.get()

    def attach(self, ref: NodeRef) -> Token[NodeRef | None]:
        return _current.set(ref)

    def detach(self, token: Token[NodeRef | None]) -> None:
        _current.reset(token)

    def inject(self) -> dict[str, str]:
        """Reduce the active node to a W3C `traceparent` header.

        Returns an empty carrier outside a traced call rather than inventing a trace --
        a receiver would otherwise become the root of a trace that never had a caller.
        """
        ref = _current.get()
        if ref is None:
            return {}
        parent = f"{_TRACEPARENT_VERSION}-{ref.trace_id}-{ref.node_id}-{_TRACEPARENT_FLAGS}"
        return {"traceparent": parent}

    def extract(self, carrier: dict[str, str]) -> NodeRef | None:
        """Rebuild the remote caller from a carrier.

        The returned ref stands in for a node that lives in another process. Nothing
        local ever completes it, so no record is written for it here -- it exists purely
        to give local nodes the right `parent_id`. The two halves are stitched together
        at export time, when both processes' JSONL files are read together.

        Malformed or absent carriers return None rather than raising: a trace context
        that arrived corrupted should cost you the link, not the request.
        """
        if not carrier:
            return None
        raw = carrier.get("traceparent") or carrier.get("Traceparent")
        if not isinstance(raw, str):
            return None
        match = _TRACEPARENT_RE.match(raw.strip().lower())
        if match is None:
            return None
        _, trace_id, node_id, _ = match.groups()
        if trace_id == "0" * 32 or node_id == "0" * 16:
            return None  # the all-zero ids are reserved as "invalid" by the spec
        return NodeRef(trace_id=trace_id, node_id=node_id, name="<remote>", depth=0)


_propagator: Propagator = ContextVarPropagator()


def record_usage(usage: Any) -> bool:
    """Attach token counts to the node currently executing. Returns whether it landed.

    Called by instrumentation once a provider responds, and usable directly for a client
    webR does not wrap:

        webrtrace.record_usage(webrtrace.Usage(model="...", input_tokens=1204))

    Returns False outside a traced call rather than raising -- missing usage is a gap in
    the trace, never a reason to break the program being traced.
    """
    current = _propagator.current()
    if current is None:
        return False
    current.state.usage = usage
    return True


def inject() -> dict[str, str]:
    """A carrier naming the current node, to send wherever the work is going.

        message = {"payload": data, **webrtrace.inject()}
        queue.put(message)

    Empty outside a traced call. The carrier is a plain `{"traceparent": "..."}` dict in
    W3C Trace Context format, so it can be used directly as HTTP headers.
    """
    return _propagator.inject()


@contextmanager
def remote_parent(carrier: dict[str, str]) -> Iterator[NodeRef | None]:
    """Run this block as a child of a node in another process.

        with webrtrace.remote_parent(message):
            handle(message["payload"])

    Traced calls inside the block join the caller's trace and record it as their parent.
    Nothing is recorded *for* the remote node here -- it belongs to the process that
    created it. Export both processes' JSONL files together and the halves join up; until
    then the edge reads as dangling, which is honest rather than broken.

    An absent or malformed carrier is not an error: the block simply starts a new trace.

    **Trust.** A carrier is an unauthenticated `traceparent` string, exactly as in W3C
    Trace Context -- there is no signature, and webR cannot tell a genuine one from a
    forged one. A carrier accepted from an untrusted source can therefore attribute local
    work to any parent the sender names, and if that id happens to match a real local node
    the fabricated edge will look genuine. Only adopt carriers from infrastructure you
    control (your own queue, your own services). The cross-process depth of adopted work
    is also relative to this process, not the remote one, since the remote depth is not
    carried.
    """
    ref = _propagator.extract(carrier)
    if ref is None:
        yield None
        return
    token = _propagator.attach(ref)
    try:
        yield ref
    finally:
        _propagator.detach(token)


def get_propagator() -> Propagator:
    """The propagator the decorator will consult."""
    return _propagator


def set_propagator(propagator: Propagator) -> None:
    """Replace the propagator.

    Intended for tests and for the cross-process propagator planned in v0.2. Swapping it
    mid-run will orphan any node that is already executing.
    """
    global _propagator
    _propagator = propagator
