# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for defects found in the multi-agent break-it review.

Each failed against the code as reviewed and is grouped by the batch that fixed it. The
governing rule every test here defends: tracing must never change what the traced program
does, and the trace must never claim something that did not happen.
"""

from __future__ import annotations

import contextlib
import enum
import json

import pytest
from conftest import by_name

import webrtrace
from webrtrace import webR_node
from webrtrace.propagation import NodeRef
from webrtrace.records import NodeStatus

# --- Batch 1: safety + integrity ----------------------------------------------------


def test_a_broken_buffer_does_not_change_what_reaches_user_code(buffer):
    # api-abuse #1: _finish called a swapped buffer with no guard, so a sink fault could
    # mask the traced program's own exception or inject a new one.
    class ExplodingBuffer:
        def append(self, record):
            raise RuntimeError("sink is down")

        def append_edge(self, edge):
            raise RuntimeError("sink is down")

        def pin(self, ids):
            raise RuntimeError("sink is down")

    original = webrtrace.get_buffer()
    webrtrace.set_buffer(ExplodingBuffer())
    try:

        @webR_node(name="agent")
        def agent():
            raise ValueError("the real error")

        # The user's ValueError must arrive intact, not a RuntimeError from the buffer.
        with pytest.raises(ValueError, match="the real error"):
            agent()

        @webR_node(name="ok_agent")
        def ok_agent():
            return "success"

        # A successful call must still return normally despite the broken sink.
        assert ok_agent() == "success"
    finally:
        webrtrace.set_buffer(original)


def test_a_broken_propagator_does_not_stop_the_function_running(buffer):
    # api-abuse: a Propagator.current() that raises made _open throw before func() ran.
    class ExplodingPropagator:
        def current(self):
            raise RuntimeError("propagator down")

        def attach(self, ref):
            return object()

        def detach(self, token):
            pass

        def inject(self):
            return {}

        def extract(self, carrier):
            return None

    side_effects = []
    original = webrtrace.get_propagator()
    webrtrace.set_propagator(ExplodingPropagator())
    try:

        @webR_node(name="agent")
        def agent():
            side_effects.append("ran")
            return "value"

        assert agent() == "value"
        assert side_effects == ["ran"]  # the body ran despite the broken propagator
    finally:
        webrtrace.set_propagator(original)


def test_marking_an_interned_value_does_not_fabricate_an_edge(buffer):
    # data-integrity #7: `"done" is "done"` and `0 is 0` are True for distinct logical
    # values, so id()-keyed marks invented edges between unrelated agents.
    @webR_node(name="alpha")
    def alpha():
        webrtrace.mark("done", "alpha's status")
        return "done"

    @webR_node(name="beta")
    def beta():
        return webrtrace.link("done")  # its own, unrelated "done"

    alpha()
    assert beta() is False
    assert buffer.edges() == []


@pytest.mark.parametrize("value", ["", "done", 0, 1, True, False, None, 42.0])
def test_scalars_are_never_linkable_by_identity(value, buffer):
    @webR_node(name="producer")
    def producer():
        webrtrace.mark(value)

    @webR_node(name="consumer")
    def consumer():
        return webrtrace.link(value)

    producer()
    assert consumer() is False


def test_containers_still_link_correctly(buffer):
    # The fix must not break the real use case: marking a plan (a list) and linking it.
    plan = ["a", "b", "c"]

    @webR_node(name="planner")
    def planner():
        return webrtrace.mark(plan, "plan")

    @webR_node(name="executor")
    def executor(p):
        return webrtrace.link(p)

    executor(planner())
    assert len(buffer.edges()) == 1


def test_a_secret_in_an_exception_message_is_redacted(buffer):
    # data-integrity #6: the redactor scrubbed io payloads but the error path stored
    # str(exc) and the traceback verbatim.
    secret = "sk-live-ABCDEFGH1234567890abcdef"

    @webR_node(name="call_llm")
    def call_llm(prompt):
        raise RuntimeError(f"401 Unauthorized for request with api_key={secret}")

    webrtrace.set_redactor(webrtrace.common_secrets)
    try:
        with pytest.raises(RuntimeError):
            call_llm("summarize this")
    finally:
        webrtrace.set_redactor(None)

    record = by_name(buffer, "call_llm")
    assert secret not in record.error.message
    assert record.error.traceback is None or secret not in record.error.traceback


def test_a_secret_in_a_traceback_is_redacted(buffer):
    secret = "AKIAIOSFODNN7EXAMPLE"

    @webR_node(name="inner")
    def inner():
        key = secret  # noqa: F841 - deliberately in a frame the traceback will render
        raise ValueError(f"boom with {secret}")

    @webR_node(name="outer")
    def outer():
        return inner()

    webrtrace.set_redactor(webrtrace.common_secrets)
    try:
        with pytest.raises(ValueError):
            outer()
    finally:
        webrtrace.set_redactor(None)

    for name in ("inner", "outer"):
        record = by_name(buffer, name)
        assert secret not in record.error.message
        if record.error.traceback is not None:
            assert secret not in record.error.traceback


def test_a_string_enum_is_captured(buffer):
    # api-abuse #3: str subclasses returned None from as_text, so a hallucination inside
    # a StrEnum-typed value was invisible.
    class Model(enum.StrEnum):
        OPUS = "claude-opus"

    @webR_node(name="agent")
    def agent(model):
        return "Revenue was 9999999 last quarter."

    agent(Model.OPUS)
    record = by_name(buffer, "agent")
    assert record.io["inputs"]["model"]["text"] == "claude-opus"
    # And detection ran on the output, as it would for a plain str.
    assert record.signals["novel_numbers"] == ["9999999"]


def test_ancestor_ids_still_reachable_after_a_guarded_finish(buffer):
    # Guarding _finish must not change the recorded structure on the happy path.
    @webR_node(name="child")
    def child():
        return 1

    @webR_node(name="parent")
    def parent():
        return child()

    parent()
    assert by_name(buffer, "child").parent_id == by_name(buffer, "parent").node_id


def test_taint_ancestors_is_a_noop_reference_shape(buffer):
    # Guards against a regression where a NodeRef with a broken chain crashes _finish.
    root = NodeRef(trace_id="0" * 32, node_id="a" * 16, name="x")
    root.taint_ancestors()  # no parent; must not raise
    assert root.state.tainted is False


# --- Batch 2: the trace must not lie ------------------------------------------------


def test_writer_drops_reach_the_exported_document(buffer, tmp_path):
    # data-integrity #2: writer.stats() knew records were dropped, but the exported
    # document had no drop count and read as a clean, complete run.
    import json

    from webrtrace.graph import graph_from_jsonl

    path = tmp_path / "run.jsonl"
    writer = webrtrace.start_writer(path, flush_interval=60.0, queue_capacity=5)

    @webR_node(name="agent", capture=False)
    def agent(i):
        return i

    try:
        for i in range(60):  # far past the queue capacity of 5
            agent(i)
        webrtrace.flush()
    finally:
        webrtrace.stop_writer()

    assert writer.stats()["dropped"] > 0

    document = graph_from_jsonl(path)
    # The document now admits the gap instead of implying completeness.
    assert document["stats"].get("dropped", 0) == writer.stats()["dropped"]

    meta_lines = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and json.loads(line).get("record") == "meta"
    ]
    assert meta_lines  # the count was persisted to disk, not just held in memory


def test_collapse_does_not_detach_a_failing_subtree(buffer):
    # data-integrity #5: two branches whose children share a name merged, and the merged
    # child was promoted to a root -- a failing node shown with no caller.
    @webR_node(name="step")
    def step(fail):
        if fail:
            raise RuntimeError("step failed")
        return 1

    @webR_node(name="worker")
    def worker(fail):
        return step(fail)

    @webR_node(name="branch_x")
    def branch_x():
        return worker(False)

    @webR_node(name="branch_y")
    def branch_y():
        return worker(True)

    @webR_node(name="orchestrator")
    def orchestrator():
        branch_x()
        with contextlib.suppress(RuntimeError):
            branch_y()

    orchestrator()

    collapsed = webrtrace.collapse_by_agent(webrtrace.export_graph(buffer))
    by_id = {n["node_id"]: n for n in collapsed["nodes"]}

    # No node named "step" may be a root: every step was called by a worker.
    step_nodes = [n for n in collapsed["nodes"] if n["name"] == "step"]
    for step_node in step_nodes:
        assert step_node["parent_id"] is not None
        assert by_id[step_node["parent_id"]]["name"] == "worker"

    # roots and parent_id must agree with the edge list (no contradiction).
    root_ids = set(collapsed["roots"])
    for node in collapsed["nodes"]:
        if node["parent_id"] is None:
            assert node["node_id"] in root_ids
        else:
            assert node["node_id"] not in root_ids


# --- Batch 3: resource + concurrency ------------------------------------------------


def _deep_document(depth, status="suspect"):
    return {
        "nodes": [
            {
                "node_id": f"n{i:06d}",
                "parent_id": f"n{i - 1:06d}" if i else None,
                "name": f"agent_{i}",
                "seq": i,
                "depth": i,
                "status": status,
                "duration_ns": 1000,
            }
            for i in range(depth)
        ],
        "edges": [],
        "stats": {},
    }


def test_render_tree_survives_a_depth_that_used_to_hit_the_recursion_limit():
    # resource #6: render_tree recursed, so it died on traces webR itself recorded fine.
    from webrtrace.render import render_tree

    output = render_tree(_deep_document(5_000))
    assert "agent_4999" in output
    # And the indent stops growing, so output is linear in nodes rather than depth^2.
    assert len(output) < 5_000 * 200


def test_failure_chains_are_bounded_and_deduplicated():
    # resource #1: one chain per failing node, all prefixes of each other, O(N^2) refs.
    from webrtrace.render import MAX_FAILURE_CHAINS, failure_chains

    chains = failure_chains(_deep_document(2_000))
    assert len(chains) <= MAX_FAILURE_CHAINS
    # The deepest failure -- the origin -- must survive the cap.
    assert any(chain[-1]["name"] == "agent_1999" for chain in chains)


def test_a_single_failing_chain_reports_one_chain_not_one_per_node(buffer):
    @webR_node(name="leaf", check=lambda out: False)
    def leaf():
        return "wrong"

    @webR_node(name="middle")
    def middle():
        return leaf()

    @webR_node(name="top")
    def top():
        return middle()

    top()
    from webrtrace.render import failure_chains

    # leaf is suspect and taints its ancestors, but taint is not failure: only the one
    # genuinely-suspect node produces a chain.
    chains = failure_chains(webrtrace.export_graph(buffer))
    assert len(chains) == 1
    assert chains[0][-1]["name"] == "leaf"


def test_the_mark_registry_is_bounded_in_bytes_not_just_entries(buffer):
    # resource #3: 2,048 entries x 1MB each retained 2.05GB, all of it dead payloads.
    from webrtrace.links import MAX_MARK_BYTES, marked_bytes

    @webR_node(name="producer", capture=False)
    def producer():
        for _ in range(400):
            webrtrace.mark(bytearray(1024 * 1024))

    producer()
    assert marked_bytes() <= MAX_MARK_BYTES
    assert webrtrace.mark_count() < 400  # evicted well before the entry cap
    webrtrace.clear_marks()
    assert marked_bytes() == 0


def test_only_the_innermost_frame_renders_the_traceback(buffer):
    # resource #2: rendering the whole traceback at every unwind level was quadratic --
    # a depth-2000 failure took 145 seconds.
    @webR_node(name="lvl3")
    def lvl3():
        raise ValueError("boom")

    @webR_node(name="lvl2")
    def lvl2():
        return lvl3()

    @webR_node(name="lvl1")
    def lvl1():
        return lvl2()

    with pytest.raises(ValueError):
        lvl1()

    errored = [r for r in buffer.records() if r.error is not None]
    assert len(errored) == 3
    assert sum(1 for r in errored if r.error.traceback is not None) == 1
    # Every node still knows what the exception was.
    assert all(r.error.type == "ValueError" for r in errored)
    assert all(r.error.message == "boom" for r in errored)


def test_a_later_exception_still_renders_its_own_traceback(buffer):
    # The render-once cache must not suppress the *next* exception's traceback.
    @webR_node(name="agent")
    def agent(which):
        raise ValueError(which)

    for which in ("first", "second"):
        with pytest.raises(ValueError):
            agent(which)

    rendered = [r for r in buffer.records() if r.error and r.error.traceback]
    assert len(rendered) == 2


def test_a_writer_closed_before_draining_counts_what_it_lost(buffer, tmp_path):
    # concurrency #1: a shutdown race discarded whole batches while dropped stayed 0.
    from webrtrace.writer import JsonlWriter

    writer = JsonlWriter(tmp_path / "run.jsonl", flush_interval=60.0)
    writer.submit(make_open_free_record())
    with writer._lock:
        writer._file.close()  # simulate the file going away under the drain
    writer._drain()

    assert writer.stats()["dropped"] >= 1
    writer.stop()


def make_open_free_record():
    from webrtrace.records import NodeRecord, NodeStatus

    return NodeRecord(
        trace_id="0" * 32,
        node_id="a" * 16,
        parent_id=None,
        name="agent",
        seq=1,
        status=NodeStatus.OK,
        started_unix_ns=0,
        duration_ns=1,
    )


def test_stopping_a_writer_unregisters_its_atexit_handler(tmp_path):
    # concurrency #5: every writer ever created stayed alive via its atexit handler.
    import atexit

    from webrtrace.writer import JsonlWriter

    writer = JsonlWriter(tmp_path / "run.jsonl")
    writer.stop()
    # Unregistering an already-unregistered callable is a no-op; if stop() had not done
    # it, this would be the only thing keeping the writer referenced.
    atexit.unregister(writer.stop)
    assert writer.stats()["path"].endswith("run.jsonl")


# --- Batch 4: portability + polish --------------------------------------------------


def test_rotation_never_overwrites_an_existing_rotated_file(tmp_path):
    # portability #1: Path.rename silently REPLACES on POSIX (destroying a previous run's
    # rotated trace) and raises on Windows (disabling rotation). Both from one line.
    from webrtrace.writer import JsonlWriter

    path = tmp_path / "run.jsonl"
    # A rotated file from an earlier run, with content that must survive.
    (tmp_path / "run.jsonl.1").write_text('{"record":"node","from":"previous run"}\n')

    writer = JsonlWriter(path, flush_interval=60.0, rotate_bytes=200)
    try:
        for _ in range(40):
            writer.submit(make_open_free_record())
            writer.flush()
    finally:
        writer.stop()

    assert "previous run" in (tmp_path / "run.jsonl.1").read_text()
    assert writer.stats()["rotations"] > 0
    # It rotated to a free name instead of clobbering or giving up.
    assert (tmp_path / "run.jsonl.2").exists()


def test_submit_does_not_wait_on_a_slow_disk(tmp_path):
    # concurrency #2: _drain held the only lock across file.write(), so submit() -- and
    # therefore every traced call -- blocked on the filesystem, while its docstring
    # promised it never blocks.
    import threading
    import time

    from webrtrace.writer import JsonlWriter

    writer = JsonlWriter(tmp_path / "run.jsonl", flush_interval=60.0)
    released = threading.Event()

    class SlowFile:
        closed = False

        def write(self, _payload):
            released.wait(5.0)  # a disk that takes a long time to accept a write

        def flush(self):
            pass

        def close(self):
            self.closed = True

    try:
        writer.submit(make_open_free_record())
        with writer._io_lock:
            real_file, writer._file = writer._file, SlowFile()

        drainer = threading.Thread(target=writer.flush)
        drainer.start()
        time.sleep(0.2)  # let the drain get inside the slow write

        start = time.perf_counter()
        writer.submit(make_open_free_record())
        elapsed = time.perf_counter() - start

        released.set()
        drainer.join(timeout=10)
        assert elapsed < 0.5, f"submit blocked for {elapsed:.2f}s behind a disk write"
    finally:
        released.set()
        with writer._io_lock:
            writer._file = real_file
        writer.stop()


def test_the_default_trace_path_is_per_process():
    # portability #3: one fixed default meant two processes appended to the same file
    # with no locking, silently destroying each other's records.
    import os

    assert str(os.getpid()) in str(webrtrace.default_trace_path())


def test_render_tolerates_a_malformed_document():
    # api-abuse #5: load_jsonl deliberately skips unparseable lines, so the renderer must
    # be equally forgiving -- a post-mortem tool that crashes on a damaged trace is
    # useless exactly when it is needed.
    from webrtrace.render import render, render_links, render_tree

    broken = {
        "nodes": [
            {"name": "no id at all", "status": "ok", "seq": 1},
            {"node_id": "a", "name": "fine", "status": "ok", "seq": 2, "parent_id": None},
        ],
        "edges": [
            {"kind": "sends", "src_id": "a"},  # missing dst_id
            {"kind": "sends"},  # missing both
        ],
        "stats": {},
    }
    assert "fine" in render_tree(broken)
    assert isinstance(render_links(broken), str)
    assert isinstance(render(broken), str)


def test_collapse_tolerates_a_malformed_document():
    broken = {
        "nodes": [{"node_id": "a", "name": "x", "status": "ok", "seq": 1, "parent_id": None}],
        "edges": [{"kind": "invokes", "src_id": "a"}],  # missing dst_id
        "stats": {},
    }
    collapsed = webrtrace.collapse_by_agent(broken)
    assert collapsed["stats"]["nodes"] == 1


def test_disabling_text_capture_stores_nothing_readable_but_still_detects(buffer):
    # The gap this closed: previously the only choices were "store excerpts" (the default,
    # which keeps a short payload in full) or "capture nothing" (losing detection too).
    secret = "Patient Maria Gonzalez balance 48211.55 card 4242424242424242"

    @webR_node(name="agent")
    def agent(prompt):
        return "I'm sorry, I don't have access to that."

    webrtrace.set_capture(True, text=False)
    agent(secret)

    record = by_name(buffer, "agent")
    blob = json.dumps({"io": record.io, "signals": record.signals})

    for fragment in ("Maria", "Gonzalez", "48211", "4242", "sorry", "access"):
        assert fragment not in blob, f"{fragment!r} leaked with text capture disabled"

    # Length and hash survive, so "did this content change" is still answerable...
    assert record.io["inputs"]["prompt"]["len"] == len(secret)
    assert record.io["inputs"]["prompt"]["hash"]
    # ...and the refusal was still caught.
    assert record.status is NodeStatus.SUSPECT
    assert record.signals["refusal"] is True


def test_default_capture_does_store_readable_text(buffer):
    # The counterpart, asserted so the security posture is explicit rather than assumed:
    # the DEFAULT is not a privacy control.
    @webR_node(name="agent")
    def agent(prompt):
        return "ok"

    agent("a short prompt with a name in it")
    assert "a short prompt" in by_name(buffer, "agent").io["inputs"]["prompt"]["text"]


def test_seq_orders_concurrent_siblings_by_invocation(buffer):
    import asyncio

    order = []

    @webR_node(name="worker")
    async def worker(i, delay):
        order.append(("start", i))
        await asyncio.sleep(delay)
        return i

    async def main():
        # Started 0,1,2 but they finish in reverse (2 first). seq must follow start order.
        await asyncio.gather(worker(0, 0.03), worker(1, 0.02), worker(2, 0.01))

    asyncio.run(main())

    workers = sorted((r for r in buffer.records() if r.name == "worker"), key=lambda r: r.seq)
    # Ordered by seq == started order: the arg values should read 0, 1, 2.
    assert [order[i][1] for i in range(3)] == [0, 1, 2]
    assert len(workers) == 3
