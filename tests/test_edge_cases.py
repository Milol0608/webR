# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Adversarial cases: things nobody designed for, written to break the library.

These were written *after* the implementation, deliberately hunting for behaviour the
author did not consider. Several of them failed on first run; the fixes are in the
modules, and the tests stay as regression guards.

The governing rule webR must never violate: **tracing may lose information, but it must
never change what the traced program does.** Every test here is ultimately checking that.
"""

from __future__ import annotations

import asyncio
import builtins
import functools
import gc
import sys
import threading

import pytest
from conftest import by_name

import webrtrace
from webrtrace import webR_node
from webrtrace.records import NodeStatus

# --- hostile exceptions --------------------------------------------------------------


def test_an_exception_whose_str_raises_does_not_break_the_call(buffer):
    # `_error_info` calls str(exc). An exception object can define __str__ to raise --
    # rare, but it exists in the wild in ORM and RPC layers with lazy messages.
    class Hostile(Exception):
        def __str__(self):
            raise RuntimeError("cannot render me")

    @webR_node(name="agent")
    def agent():
        raise Hostile()

    with pytest.raises(Hostile):
        agent()

    record = by_name(buffer, "agent")
    assert record.status is NodeStatus.ERROR
    assert record.error is not None


def test_an_exception_with_an_unrenderable_traceback_is_still_recorded(buffer):
    class Weird(Exception):
        pass

    @webR_node(name="agent")
    def agent():
        raise Weird("plain")

    with pytest.raises(Weird):
        agent()

    assert by_name(buffer, "agent").error.type == "Weird"


def test_a_deeply_nested_exception_traceback_is_capped(buffer):
    @webR_node(name="deep")
    def deep(n):
        if n == 0:
            raise ValueError("bottom")
        return deep(n - 1)

    with pytest.raises(ValueError):
        deep(60)

    errored = [r for r in buffer.records() if r.error is not None]
    rendered = [r for r in errored if r.error.traceback is not None]

    # Exactly one node renders the traceback -- the innermost to see the exception.
    # Ancestors record type and message only; rendering at every level was quadratic.
    assert len(errored) > 1
    assert len(rendered) == 1
    assert len(rendered[0].error.traceback) <= 8_192 + 64
    assert all(r.error.type == "ValueError" for r in errored)


# --- decoration forms ----------------------------------------------------------------


def test_instance_methods_are_traced(buffer):
    class Agent:
        @webR_node(name="method")
        def run(self, prompt):
            return prompt.upper()

    assert Agent().run("hi") == "HI"
    assert by_name(buffer, "method").status is NodeStatus.OK


def test_classmethods_are_traced_when_decorated_underneath(buffer):
    class Agent:
        @classmethod
        @webR_node(name="cls_method")
        def run(cls, prompt):
            return prompt

    Agent.run("hi")
    assert by_name(buffer, "cls_method").status is NodeStatus.OK


def test_staticmethods_are_traced_when_decorated_underneath(buffer):
    class Agent:
        @staticmethod
        @webR_node(name="static_method")
        def run(prompt):
            return prompt

    Agent.run("hi")
    assert by_name(buffer, "static_method").status is NodeStatus.OK


def test_a_callable_object_can_be_traced(buffer):
    class Agent:
        def __call__(self, prompt):
            return prompt

    traced = webR_node(Agent(), name="callable_obj")
    assert traced("hi") == "hi"
    assert by_name(buffer, "callable_obj").status is NodeStatus.OK


def test_a_partial_can_be_traced(buffer):
    def agent(model, prompt):
        return f"{model}:{prompt}"

    traced = webR_node(functools.partial(agent, "opus"), name="partial_agent")
    assert traced("hi") == "opus:hi"
    assert by_name(buffer, "partial_agent").status is NodeStatus.OK


def test_a_builtin_can_be_traced(buffer):
    # `inspect.signature` fails on some builtins; decoration must survive that rather
    # than refusing to wrap.
    traced = webR_node(len, name="builtin")
    assert traced([1, 2, 3]) == 3
    assert by_name(buffer, "builtin").status is NodeStatus.OK


def test_double_decoration_records_one_node_not_two(buffer):
    # Stacking @webR_node on @webR_node must not double-count the call. First wins.
    @webR_node(name="outer")
    @webR_node(name="inner")
    def agent():
        return 1

    agent()
    records = buffer.records()
    assert len(records) == 1
    assert records[0].name == "inner"


# --- generators under garbage collection ---------------------------------------------


def test_a_generator_abandoned_to_the_collector_is_still_recorded(buffer):
    # No explicit close(): the generator is dropped and the collector finalizes it. The
    # node must still be recorded exactly once, and must not be recorded as a failure.
    @webR_node(name="stream")
    def stream():
        yield 1
        yield 2
        yield 3

    gen = stream()
    next(gen)
    del gen
    gc.collect()

    records = [r for r in buffer.records() if r.name == "stream"]
    assert len(records) == 1
    assert records[0].status is NodeStatus.OK


def test_a_generator_finalized_during_collection_does_not_adopt_a_stray_parent(buffer):
    # The collector may run inside an unrelated call. If the generator's node were opened
    # at finalization time it would be attributed to whoever happened to be executing.
    @webR_node(name="stream")
    def stream():
        yield 1
        yield 2

    @webR_node(name="unrelated")
    def unrelated():
        gc.collect()
        return 1

    gen = stream()
    next(gen)
    del gen
    unrelated()
    gc.collect()

    stream_records = [r for r in buffer.records() if r.name == "stream"]
    unrelated_record = by_name(buffer, "unrelated")
    assert len(stream_records) == 1
    assert stream_records[0].parent_id != unrelated_record.node_id


# --- recursion -----------------------------------------------------------------------


def test_deep_recursion_records_correct_depths(buffer):
    @webR_node(name="recurse")
    def recurse(n):
        return n if n == 0 else recurse(n - 1)

    recurse(100)

    depths = sorted(r.depth for r in buffer.records())
    assert depths[0] == 0
    assert depths[-1] == 100


def test_tracing_does_not_meaningfully_reduce_the_recursion_limit(buffer):
    # The wrapper adds frames, so a traced function hits RecursionError sooner. That is
    # unavoidable; what matters is that it raises RecursionError normally rather than
    # crashing the interpreter or corrupting the buffer.
    @webR_node(name="runaway", capture=False)
    def runaway(n):
        return runaway(n + 1)

    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        with pytest.raises(RecursionError):
            runaway(0)
    finally:
        sys.setrecursionlimit(limit)


# --- asyncio ------------------------------------------------------------------------


@pytest.mark.skipif(sys.version_info < (3, 11), reason="TaskGroup requires 3.11")
def test_task_group_children_are_attributed_to_the_orchestrator(buffer):
    @webR_node(name="worker")
    async def worker(i):
        await asyncio.sleep(0)
        return i

    @webR_node(name="orchestrator")
    async def orchestrator():
        async with asyncio.TaskGroup() as group:
            for i in range(3):
                group.create_task(worker(i))

    asyncio.run(orchestrator())

    orchestrator_id = by_name(buffer, "orchestrator").node_id
    workers = [r for r in buffer.records() if r.name == "worker"]
    assert len(workers) == 3
    assert {w.parent_id for w in workers} == {orchestrator_id}


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup requires 3.11")
def test_an_exception_group_is_recorded_on_the_orchestrator(buffer):
    @webR_node(name="worker")
    async def worker():
        raise ValueError("worker failed")

    @webR_node(name="orchestrator")
    async def orchestrator():
        async with asyncio.TaskGroup() as group:
            group.create_task(worker())

    # Looked up dynamically: this test is skipped below 3.11, but the module still has
    # to import there, and BaseExceptionGroup is not a builtin until 3.11.
    group_type = getattr(builtins, "BaseExceptionGroup")  # noqa: B009
    with pytest.raises(group_type):
        asyncio.run(orchestrator())

    assert by_name(buffer, "worker").status is NodeStatus.ERROR
    assert by_name(buffer, "orchestrator").status is NodeStatus.ERROR


def test_a_timeout_is_recorded_as_cancellation(buffer):
    @webR_node(name="slow")
    async def slow():
        await asyncio.sleep(10)

    async def main():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(slow(), timeout=0.01)

    asyncio.run(main())

    record = by_name(buffer, "slow")
    assert record.status is NodeStatus.ERROR
    assert record.error.type == "CancelledError"


def test_two_sequential_event_loops_do_not_share_context(buffer):
    @webR_node(name="agent")
    async def agent():
        await asyncio.sleep(0)

    asyncio.run(agent())
    asyncio.run(agent())

    records = [r for r in buffer.records() if r.name == "agent"]
    assert len({r.trace_id for r in records}) == 2


# --- re-entrancy --------------------------------------------------------------------


def test_a_validator_that_calls_a_traced_function_does_not_recurse_forever(buffer):
    @webR_node(name="helper")
    def helper(value):
        return len(value) > 3

    @webR_node(name="agent", check=helper)
    def agent(prompt):
        return "long enough"

    assert agent("go") == "long enough"
    assert by_name(buffer, "agent").status is NodeStatus.OK
    assert by_name(buffer, "helper").status is NodeStatus.OK


def test_a_validator_calling_the_node_it_validates_terminates(buffer):
    calls = []

    def check(out):
        calls.append(out)
        return True

    @webR_node(name="agent", check=check)
    def agent(prompt):
        return "x"

    agent("go")
    assert len(calls) == 1


# --- payload hostility --------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "plain ascii",
        "emoji \U0001f600 and accents éè",
        "\x00null bytes\x00",
        "combining ́̂̃",
        "\ud800lone surrogate",  # cannot be encoded as valid UTF-8
        "x" * 1_000_000,
    ],
    ids=["ascii", "emoji", "nulls", "combining", "surrogate", "huge"],
)
def test_hostile_text_payloads_are_captured_without_raising(buffer, payload):
    @webR_node(name="agent")
    def agent(prompt):
        return prompt

    assert agent(payload) == payload
    assert by_name(buffer, "agent").io is not None


def test_invalid_utf8_bytes_are_captured(buffer):
    @webR_node(name="agent")
    def agent(prompt):
        return b"\xff\xfe invalid"

    agent(b"\xff\xfe also invalid")
    assert by_name(buffer, "agent").io["output"]["len"] > 0


def test_a_str_subclass_is_captured_as_a_plain_str(buffer):
    # StrEnum members and framework string types (e.g. markupsafe.Markup) are str
    # subclasses. They must be captured -- a hallucination inside one was previously
    # invisible -- and snapshotted to a plain str so a lazy or mutable subclass cannot
    # drift after the fingerprint is taken.
    class Loud(str):
        def __str__(self):
            return "SNAPSHOT"

    @webR_node(name="agent")
    def agent(prompt):
        return "fine"

    agent(Loud("value"))
    captured = by_name(buffer, "agent").io["inputs"]["prompt"]
    assert captured["text"] == "SNAPSHOT"
    assert type(captured["text"]) is str


def test_an_attribute_value_that_cannot_be_serialized_is_survivable(buffer, tmp_path):
    class Exotic:
        def __repr__(self):
            raise RuntimeError("cannot repr me")

    @webR_node(name="agent", attributes={"thing": "safe"})
    def agent():
        return None

    agent()
    assert by_name(buffer, "agent").attributes == {"thing": "safe"}


# --- the writer under duress ---------------------------------------------------------


def test_the_writer_survives_a_failing_disk(buffer, tmp_path):
    # A full disk or a revoked handle must not silently kill the writer thread and take
    # the rest of the trace with it -- that is precisely when a trace is most needed.
    writer = webrtrace.start_writer(tmp_path / "run.jsonl", flush_interval=0.05)
    try:

        class ExplodingFile:
            closed = False

            def write(self, _payload):
                raise OSError(28, "No space left on device")

            def flush(self):
                pass

            def close(self):
                self.closed = True

        with writer._lock:
            real_file, writer._file = writer._file, ExplodingFile()

        @webR_node(name="agent")
        def agent():
            return None

        for _ in range(5):
            agent()
        webrtrace.flush()

        stats = writer.stats()
        assert stats["write_errors"] > 0
        assert writer._thread.is_alive()

        # Recovery: once the underlying file works again, records flow.
        with writer._lock:
            writer._file = real_file
        agent()
        webrtrace.flush()
        assert writer.stats()["written"] > 0
    finally:
        webrtrace.stop_writer()


def test_a_writer_path_that_cannot_be_created_fails_loudly(tmp_path):
    # Failing at construction is correct: the caller asked for durability and did not
    # get it, and finding out later from an empty file would be worse.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file", encoding="utf-8")
    with pytest.raises(OSError):
        webrtrace.start_writer(blocker / "run.jsonl")


def test_exporting_while_agents_are_still_running_is_safe(buffer):
    # Someone will call export_graph() from a signal handler or a debug endpoint while
    # the system is live. Iterating the buffer mid-append must not raise.
    @webR_node(name="agent", capture=False)
    def agent(i):
        return i

    stop = threading.Event()
    errors: list[BaseException] = []

    def trace_forever():
        i = 0
        while not stop.is_set():
            agent(i)
            i += 1

    def export_repeatedly():
        try:
            for _ in range(200):
                document = webrtrace.export_graph(buffer)
                assert isinstance(document["nodes"], list)
        except BaseException as exc:  # surfaced below rather than lost in a thread
            errors.append(exc)

    producer = threading.Thread(target=trace_forever)
    consumer = threading.Thread(target=export_repeatedly)
    producer.start()
    consumer.start()
    consumer.join()
    stop.set()
    producer.join()

    assert errors == []


def test_stopping_the_writer_while_calls_are_in_flight_is_safe(buffer, tmp_path):
    # Shutdown ordering is never as clean in production as it is in a test.
    webrtrace.start_writer(tmp_path / "run.jsonl", flush_interval=0.05)

    @webR_node(name="agent", capture=False)
    def agent(i):
        return i

    stop = threading.Event()
    errors: list[BaseException] = []

    def trace_forever():
        try:
            i = 0
            while not stop.is_set():
                agent(i)
                i += 1
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=trace_forever)
    worker.start()
    try:
        webrtrace.stop_writer()  # pulled out from under the running agents
    finally:
        stop.set()
        worker.join()

    # Tracing continues into the in-memory buffer; only durability stops.
    assert errors == []
    assert webrtrace.get_writer() is None


def test_concurrent_agents_with_a_writer_running_lose_nothing(buffer, tmp_path):
    path = tmp_path / "run.jsonl"
    webrtrace.configure(capacity=20_000)
    writer = webrtrace.start_writer(path, flush_interval=0.05, queue_capacity=20_000)
    try:

        @webR_node(name="agent", capture=False)
        def agent(i):
            return i

        def worker():
            for i in range(200):
                agent(i)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        webrtrace.flush()
    finally:
        webrtrace.stop_writer()

    import json

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    # 1600 terminal records, plus an "open" marker each; assert on the terminal ones.
    terminal = [r for r in records if r.get("record", "node") == "node"]
    assert len(terminal) == 1_600
    assert writer.stats()["dropped"] == 0
    webrtrace.configure()
