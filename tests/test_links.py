# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Explicit SENDS edges: data dependencies the call stack cannot see."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import by_name

import webrtrace
from webrtrace import Link, webR_node
from webrtrace.graph import export_graph
from webrtrace.links import MAX_MARKS


@pytest.fixture(autouse=True)
def clean_marks():
    webrtrace.clear_marks()
    yield
    webrtrace.clear_marks()


def test_marking_and_linking_records_an_edge(buffer):
    # The motivating case: producer and consumer never call each other.
    @webR_node(name="planner")
    def planner():
        return webrtrace.mark(["step one", "step two"], "plan")

    @webR_node(name="executor")
    def executor(plan):
        assert webrtrace.link(plan) is True
        return "done"

    plan = planner()
    executor(plan)

    edges = buffer.edges()
    assert len(edges) == 1
    assert edges[0].kind.value == "sends"
    assert edges[0].src_id == by_name(buffer, "planner").node_id
    assert edges[0].dst_id == by_name(buffer, "executor").node_id
    assert edges[0].label == "plan"


def test_mark_returns_the_value_unchanged(buffer):
    payload = {"a": 1}

    @webR_node(name="producer")
    def producer():
        return webrtrace.mark(payload)

    result = producer()
    assert result is payload
    assert result == {"a": 1}


def test_linking_is_identity_based_not_equality_based(buffer):
    # Two equal lists are not the same datum. Treating them as one would invent an edge
    # that never existed, which is worse than recording no edge at all.
    @webR_node(name="producer")
    def producer():
        return webrtrace.mark(["same"])

    @webR_node(name="consumer")
    def consumer(value):
        return webrtrace.link(value)

    producer()
    assert consumer(["same"]) is False
    assert buffer.edges() == []


def test_an_unmarked_value_records_nothing_and_does_not_raise(buffer):
    @webR_node(name="consumer")
    def consumer(value):
        return webrtrace.link(value)

    assert consumer("never marked") is False
    assert buffer.edges() == []


def test_linking_outside_a_traced_call_is_a_no_op(buffer):
    assert webrtrace.link("anything") is False
    assert webrtrace.origin() is None


def test_a_node_does_not_link_to_itself(buffer):
    @webR_node(name="agent")
    def agent():
        value = webrtrace.mark([1, 2, 3])
        return webrtrace.link(value)

    assert agent() is False
    assert buffer.edges() == []


def test_a_label_at_link_time_overrides_the_mark_label(buffer):
    @webR_node(name="producer")
    def producer():
        return webrtrace.mark([1], "original")

    @webR_node(name="consumer")
    def consumer(value):
        webrtrace.link(value, "at-consumption")

    consumer(producer())
    assert buffer.edges()[0].label == "at-consumption"


def test_one_value_can_fan_out_to_several_consumers(buffer):
    @webR_node(name="producer")
    def producer():
        return webrtrace.mark([1])

    @webR_node(name="consumer")
    def consumer(value):
        webrtrace.link(value)

    plan = producer()
    for _ in range(3):
        consumer(plan)

    assert len(buffer.edges()) == 3


# --- tokens, for boundaries marking cannot cross -----------------------------------


def test_a_token_links_across_a_thread_boundary(buffer):
    # A queue hand-off: the payload and its token travel together, and the consumer runs
    # somewhere the producer's context never reaches.
    @webR_node(name="producer")
    def producer():
        return "payload", webrtrace.origin("queued")

    @webR_node(name="consumer")
    def consumer(payload, token):
        webrtrace.link(token)

    payload, token = producer()
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(consumer, payload, token).result()

    edge = buffer.edges()[0]
    assert edge.src_id == by_name(buffer, "producer").node_id
    assert edge.label == "queued"


def test_a_token_survives_serialization(buffer):
    @webR_node(name="producer")
    def producer():
        return webrtrace.origin("plan")

    token = producer()
    restored = Link.from_dict(token.to_dict())

    assert restored == token
    assert restored.node_id == by_name(buffer, "producer").node_id


def test_edges_may_cross_traces(buffer):
    # Two independent runs joined by a payload. The edge is the only thing connecting
    # them, which is exactly what makes it worth recording.
    @webR_node(name="producer")
    def producer():
        return webrtrace.origin()

    @webR_node(name="consumer")
    def consumer(token):
        webrtrace.link(token)

    token = producer()
    consumer(token)

    producer_rec, consumer_rec = by_name(buffer, "producer"), by_name(buffer, "consumer")
    assert producer_rec.trace_id != consumer_rec.trace_id
    assert len(buffer.edges()) == 1


def test_async_agents_can_link(buffer):
    @webR_node(name="producer")
    async def producer():
        await asyncio.sleep(0)
        return webrtrace.mark(["plan"])

    @webR_node(name="consumer")
    async def consumer(plan):
        await asyncio.sleep(0)
        webrtrace.link(plan)

    async def main():
        plan = await producer()
        await consumer(plan)

    asyncio.run(main())
    assert len(buffer.edges()) == 1


# --- bounded retention -------------------------------------------------------------


def test_the_mark_registry_is_bounded(buffer):
    # Marking holds a strong reference to keep id() valid, so the registry must be
    # bounded or it becomes a leak with a respectable job title.
    @webR_node(name="producer")
    def producer():
        for index in range(MAX_MARKS + 500):
            webrtrace.mark([index])

    producer()
    assert webrtrace.mark_count() == MAX_MARKS


def test_evicted_marks_stop_linking_rather_than_linking_wrongly(buffer):
    @webR_node(name="producer")
    def producer():
        first = webrtrace.mark(["first"])
        for index in range(MAX_MARKS + 10):
            webrtrace.mark([index])
        return first

    @webR_node(name="consumer")
    def consumer(value):
        return webrtrace.link(value)

    assert consumer(producer()) is False


def test_clear_marks_releases_everything(buffer):
    @webR_node(name="producer")
    def producer():
        webrtrace.mark([1])

    producer()
    assert webrtrace.mark_count() == 1
    webrtrace.clear_marks()
    assert webrtrace.mark_count() == 0


# --- the graph document ------------------------------------------------------------


def test_sends_edges_appear_in_the_graph_alongside_call_edges(buffer):
    @webR_node(name="planner")
    def planner():
        return webrtrace.mark(["plan"], "plan")

    @webR_node(name="executor")
    def executor(plan):
        webrtrace.link(plan)

    @webR_node(name="orchestrator")
    def orchestrator():
        executor(planner())

    orchestrator()
    document = export_graph(buffer)

    kinds = [edge["kind"] for edge in document["edges"]]
    assert kinds.count("invokes") == 2
    assert kinds.count("sends") == 1
    assert document["stats"]["sends_edges"] == 1
    assert document["stats"]["invokes_edges"] == 2


def test_sends_edges_round_trip_through_jsonl(buffer, tmp_path):
    path = tmp_path / "run.jsonl"

    @webR_node(name="planner")
    def planner():
        return webrtrace.mark(["plan"], "plan")

    @webR_node(name="executor")
    def executor(plan):
        webrtrace.link(plan)

    webrtrace.start_writer(path, flush_interval=0.05)
    try:
        executor(planner())
        webrtrace.flush()
    finally:
        webrtrace.stop_writer()

    document = webrtrace.graph_from_jsonl(path)
    assert document["stats"]["sends_edges"] == 1
    assert document["stats"]["nodes"] == 2


def test_a_dangling_sends_edge_is_flagged(buffer):
    # The consumer's node was evicted; the edge is still real and is reported as such.
    small = webrtrace.configure(capacity=2, pinned_capacity=2)

    @webR_node(name="producer")
    def producer():
        return webrtrace.mark(["plan"])

    @webR_node(name="consumer")
    def consumer(plan):
        webrtrace.link(plan)

    consumer(producer())

    @webR_node(name="filler")
    def filler():
        return None

    for _ in range(10):
        filler()

    document = export_graph(small)
    sends = [edge for edge in document["edges"] if edge["kind"] == "sends"]
    assert sends[0]["dangling"] is True
    webrtrace.configure()
