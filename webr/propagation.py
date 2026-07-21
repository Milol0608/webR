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

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ._ids import new_node_id, new_trace_id


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


def new_root(name: str) -> NodeRef:
    """Start a new trace. Used when a node runs with no active parent."""
    return NodeRef(trace_id=new_trace_id(), node_id=new_node_id(), name=name, depth=0)


@runtime_checkable
class Propagator(Protocol):
    """Carries the active `NodeRef` across whatever boundaries it can."""

    def current(self) -> NodeRef | None:
        """The node executing right now, or None if outside any traced call."""
        ...

    def attach(self, ref: NodeRef) -> Any:
        """Make `ref` current. Returns a token to pass back to `detach`."""
        ...

    def detach(self, token: Any) -> None:
        """Restore whatever was current before the matching `attach`."""
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


_propagator: Propagator = ContextVarPropagator()


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
