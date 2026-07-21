# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""`@webR_node` behaviour: what gets recorded, and what must not change."""

from __future__ import annotations

import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import all_named, by_name

import webr
from webr import submit, webR_node
from webr.records import NodeStatus

# --- the basics ------------------------------------------------------------------


def test_sync_call_is_recorded(buffer):
    @webR_node(name="agent")
    def agent(x):
        return x * 2

    assert agent(21) == 42

    record = by_name(buffer, "agent")
    assert record.status is NodeStatus.OK
    assert record.parent_id is None
    assert record.depth == 0
    assert record.duration_ns > 0
    assert record.error is None


def test_async_call_is_recorded(buffer):
    @webR_node(name="agent")
    async def agent(x):
        await asyncio.sleep(0)
        return x * 2

    assert asyncio.run(agent(21)) == 42
    assert by_name(buffer, "agent").status is NodeStatus.OK


def test_default_name_is_the_qualified_name(buffer):
    @webR_node
    def agent():
        return None

    agent()
    assert by_name(buffer, agent.__webr_name__).name.endswith("agent")


def test_nesting_records_parent_and_depth(buffer):
    @webR_node(name="child")
    def child():
        return 1

    @webR_node(name="parent")
    def parent():
        return child()

    parent()

    parent_rec, child_rec = by_name(buffer, "parent"), by_name(buffer, "child")
    assert child_rec.parent_id == parent_rec.node_id
    assert child_rec.trace_id == parent_rec.trace_id
    assert (parent_rec.depth, child_rec.depth) == (0, 1)


def test_separate_root_calls_get_separate_traces(buffer):
    @webR_node(name="agent")
    def agent():
        return None

    agent()
    agent()

    first, second = all_named(buffer, "agent")
    assert first.trace_id != second.trace_id


def test_static_attributes_are_attached_and_isolated(buffer):
    attrs = {"model": "opus-4.8"}

    @webR_node(name="agent", attributes=attrs)
    def agent():
        return None

    agent()
    attrs["model"] = "mutated-after-decoration"

    # The record must not track later mutations of the caller's dict.
    assert by_name(buffer, "agent").attributes == {"model": "opus-4.8"}


# --- transparency: webR must not change how the program behaves --------------------


def test_functools_wraps_preserves_introspection(buffer):
    def original(a: int, b: str = "x") -> str:
        """Docstring survives."""
        return b * a

    agent = webR_node(original)

    assert agent.__name__ == original.__name__
    assert agent.__doc__ == original.__doc__
    assert inspect.signature(agent) == inspect.signature(original)
    assert agent.__wrapped__ is original  # tooling can still reach the real function


def test_async_wrapper_is_still_a_coroutine_function(buffer):
    # Frameworks branch on this. If the wrapper broke it, webR would change dispatch.
    @webR_node
    async def agent():
        return None

    assert inspect.iscoroutinefunction(agent)


def test_async_generator_wrapper_is_still_an_async_generator_function(buffer):
    @webR_node
    async def agent():
        yield 1

    assert inspect.isasyncgenfunction(agent)


def test_exception_is_recorded_and_re_raised_unchanged(buffer):
    sentinel = ValueError("bad plan")

    @webR_node(name="agent")
    def agent():
        raise sentinel

    with pytest.raises(ValueError) as caught:
        agent()

    assert caught.value is sentinel  # same object, not a wrapper
    record = by_name(buffer, "agent")
    assert record.status is NodeStatus.ERROR
    assert record.error.type == "ValueError"
    assert record.error.message == "bad plan"
    assert "raise sentinel" in record.error.traceback


def test_cancellation_is_recorded(buffer):
    # CancelledError is a BaseException. An agent killed by a timeout vanishing from the
    # web without a trace is precisely the silent failure this library exists to catch.
    @webR_node(name="agent")
    async def agent():
        await asyncio.sleep(10)

    async def main():
        task = asyncio.create_task(agent())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    record = by_name(buffer, "agent")
    assert record.status is NodeStatus.ERROR
    assert record.error.type == "CancelledError"


def test_disabled_tracing_records_nothing_but_still_runs(buffer):
    @webR_node(name="agent")
    def agent(x):
        return x + 1

    webr.disable()
    try:
        assert agent(1) == 2
    finally:
        webr.enable()

    assert buffer.records() == []
    assert agent(1) == 2
    assert len(buffer.records()) == 1


def test_tracing_can_be_toggled_mid_run(buffer):
    # The whole justification for option (b): flip tracing on in a live process.
    @webR_node(name="agent")
    def agent():
        return None

    webr.disable()
    agent()
    webr.enable()
    agent()

    assert len(all_named(buffer, "agent")) == 1


# --- concurrency -------------------------------------------------------------------


def test_gather_fanout_attributes_every_worker_to_the_orchestrator(buffer):
    @webR_node(name="worker")
    async def worker(i):
        await asyncio.sleep(0)
        return i

    @webR_node(name="orchestrator")
    async def orchestrator():
        return await asyncio.gather(*(worker(i) for i in range(5)))

    assert asyncio.run(orchestrator()) == [0, 1, 2, 3, 4]

    orchestrator_rec = by_name(buffer, "orchestrator")
    workers = all_named(buffer, "worker")
    assert len(workers) == 5
    assert {w.parent_id for w in workers} == {orchestrator_rec.node_id}
    assert {w.depth for w in workers} == {1}


def test_concurrent_orchestrators_do_not_cross_traces(buffer):
    @webR_node(name="worker")
    async def worker():
        await asyncio.sleep(0)

    @webR_node(name="orchestrator")
    async def orchestrator():
        await worker()

    async def main():
        await asyncio.gather(orchestrator(), orchestrator())

    asyncio.run(main())

    traces = {r.trace_id for r in buffer.records()}
    assert len(traces) == 2  # two independent webs, no edges invented between them


def test_submit_carries_the_active_node_into_a_worker_thread(buffer):
    @webR_node(name="worker")
    def worker():
        return 1

    @webR_node(name="orchestrator")
    def orchestrator(executor):
        return submit(executor, worker).result()

    with ThreadPoolExecutor(max_workers=1) as executor:
        orchestrator(executor)

    assert by_name(buffer, "worker").parent_id == by_name(buffer, "orchestrator").node_id


def test_plain_executor_submit_orphans_the_worker(buffer):
    # Documents the failure mode `submit` exists to prevent: a raw executor gets no
    # context, so the worker starts a brand-new trace with no caller.
    @webR_node(name="worker")
    def worker():
        return 1

    @webR_node(name="orchestrator")
    def orchestrator(executor):
        return executor.submit(worker).result()

    with ThreadPoolExecutor(max_workers=1) as executor:
        orchestrator(executor)

    worker_rec = by_name(buffer, "worker")
    assert worker_rec.parent_id is None
    assert worker_rec.trace_id != by_name(buffer, "orchestrator").trace_id


def test_asyncio_to_thread_propagates_without_help(buffer):
    @webR_node(name="worker")
    def worker():
        return 1

    @webR_node(name="orchestrator")
    async def orchestrator():
        return await asyncio.to_thread(worker)

    asyncio.run(orchestrator())

    assert by_name(buffer, "worker").parent_id == by_name(buffer, "orchestrator").node_id


# --- retention ---------------------------------------------------------------------


def test_failure_pins_its_ancestor_chain(buffer):
    # The parents are still executing when the child fails, so their records do not exist
    # yet. They must survive the flood of successes that follows.
    small = webr.configure(capacity=5, pinned_capacity=50)

    @webR_node(name="boom")
    def boom():
        raise RuntimeError("hallucinated")

    @webR_node(name="planner")
    def planner():
        boom()

    @webR_node(name="orchestrator")
    def orchestrator():
        planner()

    @webR_node(name="filler")
    def filler():
        return None

    with pytest.raises(RuntimeError):
        orchestrator()
    for _ in range(200):
        filler()

    names = {r.name for r in small.records()}
    assert {"boom", "planner", "orchestrator"} <= names
    assert small.stats()["dropped"] > 0


# --- generators --------------------------------------------------------------------


def test_generator_node_spans_the_whole_iteration(buffer):
    @webR_node(name="inner")
    def inner(i):
        return i

    @webR_node(name="stream")
    def stream():
        for i in range(3):
            yield inner(i)

    assert list(stream()) == [0, 1, 2]

    stream_rec = by_name(buffer, "stream")
    inners = all_named(buffer, "inner")
    assert len(inners) == 3
    # Calls made inside the body belong to the generator, not to whoever iterated it.
    assert {i.parent_id for i in inners} == {stream_rec.node_id}


def test_generator_supports_send(buffer):
    @webR_node(name="accumulate")
    def accumulate():
        total = 0
        while True:
            value = yield total
            if value is None:
                return
            total += value

    gen = accumulate()
    assert next(gen) == 0
    assert gen.send(5) == 5
    assert gen.send(3) == 8
    gen.close()

    assert by_name(buffer, "accumulate").status is NodeStatus.OK


def test_abandoned_generator_is_not_recorded_as_a_failure(buffer):
    # `break` out of a loop closes the generator with GeneratorExit. That is ordinary
    # control flow; recording it as an error would fill the web with phantom failures.
    @webR_node(name="stream")
    def stream():
        yield 1
        yield 2
        yield 3

    gen = stream()
    next(gen)
    gen.close()

    assert by_name(buffer, "stream").status is NodeStatus.OK


def test_generator_failure_is_recorded(buffer):
    @webR_node(name="stream")
    def stream():
        yield 1
        raise RuntimeError("mid-stream")

    with pytest.raises(RuntimeError):
        list(stream())

    record = by_name(buffer, "stream")
    assert record.status is NodeStatus.ERROR
    assert record.error.type == "RuntimeError"


def test_async_generator_node_spans_the_whole_iteration(buffer):
    @webR_node(name="inner")
    async def inner(i):
        return i

    @webR_node(name="stream")
    async def stream():
        for i in range(3):
            yield await inner(i)

    async def main():
        return [item async for item in stream()]

    assert asyncio.run(main()) == [0, 1, 2]

    stream_rec = by_name(buffer, "stream")
    assert {i.parent_id for i in all_named(buffer, "inner")} == {stream_rec.node_id}


def test_async_generator_failure_is_recorded(buffer):
    @webR_node(name="stream")
    async def stream():
        yield 1
        raise RuntimeError("mid-stream")

    async def main():
        async for _ in stream():
            pass

    with pytest.raises(RuntimeError):
        asyncio.run(main())

    assert by_name(buffer, "stream").status is NodeStatus.ERROR


def test_disabled_generators_still_behave_identically(buffer):
    @webR_node(name="stream")
    def stream():
        received = yield 1
        yield received

    webr.disable()
    try:
        gen = stream()
        assert next(gen) == 1
        assert gen.send("echo") == "echo"
        gen.close()
    finally:
        webr.enable()

    assert buffer.records() == []
