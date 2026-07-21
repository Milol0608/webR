# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Retention policy: bounded memory that still keeps the records worth keeping."""

from __future__ import annotations

import threading

import pytest
from conftest import make_record

from webr.buffer import TraceBuffer
from webr.records import NodeStatus


def test_rejects_nonsense_capacity():
    with pytest.raises(ValueError):
        TraceBuffer(capacity=0)
    with pytest.raises(ValueError):
        TraceBuffer(pinned_capacity=0)


def test_records_come_back_in_invocation_order():
    buf = TraceBuffer(capacity=10)
    for name in ("a", "b", "c"):
        buf.append(make_record(name))
    assert [r.node_id for r in buf.records()] == ["a", "b", "c"]


def test_oldest_uninteresting_records_are_dropped_at_capacity():
    buf = TraceBuffer(capacity=3)
    for i in range(5):
        buf.append(make_record(f"n{i}"))

    assert [r.node_id for r in buf.records()] == ["n2", "n3", "n4"]
    assert buf.stats()["dropped"] == 2
    assert len(buf) == 3


def test_failures_survive_a_long_uneventful_run():
    # The motivating scenario from ADR 0001: a failure early in a long run must not be
    # evicted by the thousands of successes that follow it.
    buf = TraceBuffer(capacity=10)
    buf.append(make_record("boom", status=NodeStatus.ERROR))
    for i in range(500):
        buf.append(make_record(f"ok{i}"))

    ids = [r.node_id for r in buf.records()]
    assert "boom" in ids
    assert buf.stats()["dropped"] == 490


def test_suspect_and_tainted_nodes_are_retained_too():
    buf = TraceBuffer(capacity=2)
    buf.append(make_record("suspect", status=NodeStatus.SUSPECT))
    buf.append(make_record("tainted", tainted=True))
    for i in range(20):
        buf.append(make_record(f"ok{i}"))

    ids = {r.node_id for r in buf.records()}
    assert {"suspect", "tainted"} <= ids


def test_pin_retains_a_record_that_is_already_resident():
    buf = TraceBuffer(capacity=3)
    buf.append(make_record("keep"))
    buf.pin(["keep"])
    for i in range(10):
        buf.append(make_record(f"ok{i}"))

    assert "keep" in {r.node_id for r in buf.records()}


def test_pin_retains_an_ancestor_that_has_not_finished_yet():
    # A parent completes *after* the child that failed inside it, so pinning the ancestor
    # chain necessarily happens before those records exist.
    buf = TraceBuffer(capacity=2)
    buf.pin(["orchestrator", "planner"])
    for i in range(10):
        buf.append(make_record(f"ok{i}"))
    buf.append(make_record("planner"))
    buf.append(make_record("orchestrator"))
    for i in range(10):
        buf.append(make_record(f"later{i}"))

    ids = {r.node_id for r in buf.records()}
    assert {"planner", "orchestrator"} <= ids


def test_pinned_records_are_not_double_counted():
    buf = TraceBuffer(capacity=10)
    buf.append(make_record("boom", status=NodeStatus.ERROR))
    buf.append(make_record("ok"))

    assert len(buf) == 2
    assert len(buf.records()) == 2
    assert buf.stats()["retained"] == 2


def test_evicting_a_pinned_record_from_the_ring_is_not_a_drop():
    buf = TraceBuffer(capacity=1)
    buf.append(make_record("boom", status=NodeStatus.ERROR))
    buf.append(make_record("ok"))

    assert buf.stats()["dropped"] == 0
    assert {r.node_id for r in buf.records()} == {"boom", "ok"}


def test_pinned_store_is_itself_bounded():
    # A run that fails in a loop must not defeat the memory ceiling.
    buf = TraceBuffer(capacity=5, pinned_capacity=3)
    for i in range(10):
        buf.append(make_record(f"boom{i}", status=NodeStatus.ERROR))

    stats = buf.stats()
    assert stats["pinned"] == 3
    assert stats["pins_dropped"] == 7


def test_outstanding_pin_requests_are_bounded():
    # Ancestor ids that never arrive would otherwise accumulate forever.
    buf = TraceBuffer(capacity=5, pinned_capacity=4)
    buf.pin(f"ghost{i}" for i in range(1000))
    for i in range(50):
        buf.append(make_record(f"ok{i}"))

    assert buf.stats()["pinned"] == 0
    assert len(buf) == 5


def test_internal_indexes_do_not_grow_without_bound():
    # Guards the leak the first draft of this class had: the id->record index must
    # shrink with the ring, or the capacity ceiling is decorative.
    buf = TraceBuffer(capacity=10)
    for i in range(5_000):
        buf.append(make_record(f"n{i}"))

    assert len(buf._resident) <= 10  # noqa: SLF001 - deliberate white-box assertion
    assert len(buf._pinned) == 0  # noqa: SLF001


def test_clear_resets_counters():
    buf = TraceBuffer(capacity=1)
    buf.append(make_record("boom", status=NodeStatus.ERROR))
    buf.append(make_record("ok"))
    buf.clear()

    assert len(buf) == 0
    assert buf.stats()["dropped"] == 0
    assert buf.records() == []


def test_concurrent_appends_do_not_lose_records():
    # Records arrive from traced worker threads; the buffer must not rely on the GIL.
    buf = TraceBuffer(capacity=10_000)
    per_thread = 500

    def worker(tid: int) -> None:
        for i in range(per_thread):
            buf.append(make_record(f"t{tid}-{i}"))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(buf) == 8 * per_thread
    assert buf.stats()["dropped"] == 0
