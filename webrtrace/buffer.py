# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Bounded in-memory retention for completed nodes.

Memory is capped by construction: a long-running agent cannot make webR consume the
process. The naive way to enforce that -- drop the oldest -- has a bad failure mode. A
run fails at minute two and continues for an hour; by export time the failure has been
evicted by thousands of uneventful successes, and the buffer has thrown away the only
record that mattered.

So retention is by *interest*, not by age (ADR 0001):

- a **ring** holds every node and drops the oldest when full;
- a **pinned** store holds nodes that must survive: anything that errored, anything a
  detector flagged as suspect, and the ancestor chain of either.

Ancestors are pinned by id before they finish, because a parent completes *after* the
child that failed inside it. `pin` therefore accepts ids that are not resident yet and
retains them on arrival.

Once the JSONL writer lands (milestone 3), eviction from this buffer loses nothing
permanently -- the record is already on disk, and this becomes a cache for live queries.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from collections.abc import Iterable

from .records import EdgeRecord, NodeRecord

DEFAULT_CAPACITY = 100_000
DEFAULT_PINNED_CAPACITY = 10_000

_MISSING = object()


class TraceBuffer:
    """Thread-safe, fixed-ceiling store of `NodeRecord`s.

    A single lock guards both stores. Uncontended acquisition is tens of nanoseconds and
    the critical section is a handful of dict and deque operations, which is cheap next to
    anything an instrumented agent is doing. Correctness here matters more than shaving
    that lock: records arrive from traced worker threads, and free-threaded builds offer
    no implicit protection.
    """

    __slots__ = (
        "_capacity",
        "_dropped",
        "_edges",
        "_edges_dropped",
        "_lock",
        "_pin_requests",
        "_pinned",
        "_pinned_capacity",
        "_pinned_order",
        "_pins_dropped",
        "_resident",
        "_ring",
    )

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        pinned_capacity: int = DEFAULT_PINNED_CAPACITY,
    ) -> None:
        if capacity < 1 or pinned_capacity < 1:
            raise ValueError("capacity and pinned_capacity must be >= 1")
        self._capacity = capacity
        self._pinned_capacity = pinned_capacity
        self._lock = threading.Lock()
        # Age-ordered view. Records here may also be in `_pinned`; snapshot de-duplicates.
        self._ring: deque[NodeRecord] = deque()
        self._resident: dict[str, NodeRecord] = {}
        self._pinned: dict[str, NodeRecord] = {}
        self._pinned_order: deque[str] = deque()
        # Ids to retain when they arrive -- ancestors pinned while still executing.
        # Bounded like everything else here: a run that fails in a loop would otherwise
        # accumulate ancestor ids forever. An OrderedDict as a FIFO set, so evicting the
        # oldest is O(1) via popitem(last=False).
        self._pin_requests: OrderedDict[str, None] = OrderedDict()
        self._dropped = 0
        self._pins_dropped = 0
        # Explicit SENDS edges. Kept in their own ring: they are far rarer than nodes and
        # must not be squeezed out by node volume.
        self._edges: deque[EdgeRecord] = deque(maxlen=capacity)
        self._edges_dropped = 0

    def append_edge(self, edge: EdgeRecord) -> None:
        """Record a declared data-dependency edge. O(1)."""
        with self._lock:
            if len(self._edges) == self._edges.maxlen:
                self._edges_dropped += 1
            self._edges.append(edge)

    def edges(self) -> list[EdgeRecord]:
        """Every retained edge, in declaration order."""
        with self._lock:
            return sorted(self._edges, key=lambda edge: edge.seq)

    def append(self, record: NodeRecord) -> None:
        """Record a completed node. O(1), never blocks on I/O."""
        with self._lock:
            self._ring.append(record)
            self._resident[record.node_id] = record
            was_requested = self._pin_requests.pop(record.node_id, _MISSING) is not _MISSING
            if record.is_interesting or was_requested:
                self._retain(record)
            while len(self._ring) > self._capacity:
                self._evict_oldest()

    def pin(self, node_ids: Iterable[str]) -> None:
        """Protect these nodes from age eviction, now and on arrival.

        Called with a failing node's ancestor chain, nearest first. Ids that are neither
        resident nor yet created are remembered, so a parent still executing is retained
        when it finishes.

        **Stops at the first id already known.** Every pin walks to the root, so an id
        that is already pinned or already requested had its own ancestors pinned by that
        earlier walk. Pass a lazy iterator (`NodeRef.iter_chain_ids`) and a deep failing
        chain costs O(1) amortized per node instead of O(depth).
        """
        with self._lock:
            for node_id in node_ids:
                if node_id in self._pinned or node_id in self._pin_requests:
                    return  # everything above this was pinned by an earlier walk
                record = self._resident.get(node_id)
                if record is not None:
                    self._retain(record)
                    continue
                # Not here yet: remember the id so `append` retains it on arrival.
                self._pin_requests[node_id] = None
                if len(self._pin_requests) > self._pinned_capacity:
                    # popitem(last=False) is O(1); popping via next(iter(...)) walked the
                    # dict's deleted slots and degraded with capacity.
                    self._pin_requests.popitem(last=False)

    def records(self) -> list[NodeRecord]:
        """Everything still retained, in invocation order."""
        with self._lock:
            merged = {r.node_id: r for r in self._ring}
            merged.update(self._pinned)
        return sorted(merged.values(), key=lambda r: r.seq)

    def stats(self) -> dict[str, int]:
        """Retention counters.

        `dropped` is reported in every export. A trace that quietly claims to be complete
        when it is not would undermine the whole tool.
        """
        with self._lock:
            return {
                "retained": len({r.node_id for r in self._ring} | self._pinned.keys()),
                "pinned": len(self._pinned),
                "dropped": self._dropped,
                "pins_dropped": self._pins_dropped,
                "sends_edges": len(self._edges),
                "sends_edges_dropped": self._edges_dropped,
                "capacity": self._capacity,
                "pinned_capacity": self._pinned_capacity,
            }

    def clear(self) -> None:
        """Reset to empty, counters included."""
        with self._lock:
            self._ring.clear()
            self._resident.clear()
            self._pinned.clear()
            self._pinned_order.clear()
            self._pin_requests.clear()
            self._edges.clear()
            self._dropped = 0
            self._pins_dropped = 0
            self._edges_dropped = 0

    def __len__(self) -> int:
        with self._lock:
            return len({r.node_id for r in self._ring} | self._pinned.keys())

    # -- internals; callers already hold the lock ---------------------------------

    def _retain(self, record: NodeRecord) -> None:
        if record.node_id in self._pinned:
            return
        self._pinned[record.node_id] = record
        self._pinned_order.append(record.node_id)
        # The pinned store is bounded too, or a pathological run that fails constantly
        # would defeat the memory ceiling this class exists to enforce.
        while len(self._pinned_order) > self._pinned_capacity:
            evicted = self._pinned_order.popleft()
            self._pinned.pop(evicted, None)
            self._pins_dropped += 1

    def _evict_oldest(self) -> None:
        record = self._ring.popleft()
        # `_resident` mirrors the ring exactly, so it always shrinks with it; a pinned
        # record is reachable through `_pinned` and does not need a second index entry.
        self._resident.pop(record.node_id, None)
        if record.node_id in self._pinned:
            # Still retained by the pinned store; nothing is lost.
            return
        self._dropped += 1
