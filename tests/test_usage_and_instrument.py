# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Tokens, provider instrumentation, logging, and non-text detection (ADR 0003)."""

from __future__ import annotations

import asyncio
import logging

import pytest
from conftest import by_name

import webrtrace
from webrtrace import Usage, webR_node
from webrtrace.detectors import (
    detect_all_zeros,
    detect_empty_collection,
    detect_nan,
    detect_unchanged_value,
    run_value_detectors,
)
from webrtrace.records import NodeStatus

# --- a stand-in for the Anthropic SDK -----------------------------------------------
#
# Shaped after the real response: `usage.input_tokens` / `.output_tokens`, the two cache
# counters, plus `model` and `stop_reason`. Nothing here imports `anthropic` -- webR must
# work with no provider SDK installed, which is the point of the duck-typed reader.


class FakeUsage:
    def __init__(self, inp=120, out=45, cache_write=0, cache_read=0):
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_creation_input_tokens = cache_write
        self.cache_read_input_tokens = cache_read


class FakeMessage:
    def __init__(self, text="hello", stop_reason="end_turn", usage=None, model="claude-opus-4-8"):
        self.content = [type("Block", (), {"type": "text", "text": text})()]
        self.stop_reason = stop_reason
        self.stop_details = None
        self.model = model
        self.usage = usage if usage is not None else FakeUsage()


class FakeMessages:
    def __init__(self, response=None):
        self._response = response or FakeMessage()
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class FakeClient:
    def __init__(self, response=None):
        self.messages = FakeMessages(response)
        self.api_key = "sk-not-real"


class FakeAsyncMessages(FakeMessages):
    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class FakeAsyncClient:
    def __init__(self, response=None):
        self.messages = FakeAsyncMessages(response)


# --- Usage --------------------------------------------------------------------------


def test_usage_totals_include_cache_tokens():
    usage = Usage(input_tokens=100, output_tokens=50, cache_read_input_tokens=900)
    assert usage.total_tokens == 1050


def test_usage_omits_unreported_fields():
    payload = Usage(model="claude-opus-4-8", input_tokens=10).to_dict()
    assert payload == {"model": "claude-opus-4-8", "input_tokens": 10}
    # Absent is not zero -- a provider that reports nothing must not look like a free call.
    assert "output_tokens" not in payload


def test_record_usage_attaches_to_the_running_node(buffer):
    @webR_node(name="agent")
    def agent():
        webrtrace.record_usage(Usage(model="m", input_tokens=7, output_tokens=3))
        return "done"

    agent()
    usage = by_name(buffer, "agent").usage
    assert usage.model == "m"
    assert usage.total_tokens == 10


def test_record_usage_outside_a_traced_call_is_a_no_op(buffer):
    assert webrtrace.record_usage(Usage(input_tokens=1)) is False


def test_usage_reaches_the_exported_document(buffer):
    @webR_node(name="agent")
    def agent():
        webrtrace.record_usage(Usage(model="claude-opus-4-8", input_tokens=1204))
        return "x"

    agent()
    node = webrtrace.export_graph(buffer)["nodes"][0]
    assert node["usage"]["model"] == "claude-opus-4-8"
    assert node["usage"]["input_tokens"] == 1204


def test_nodes_without_a_model_call_carry_no_usage(buffer):
    @webR_node(name="plain")
    def plain():
        return 1

    plain()
    assert by_name(buffer, "plain").usage is None
    assert "usage" not in webrtrace.export_graph(buffer)["nodes"][0]


# --- instrumentation ----------------------------------------------------------------


def test_instrumented_call_records_tokens_and_model(buffer):
    client = webrtrace.instrument(FakeClient())
    response = client.messages.create(model="claude-opus-4-8", messages=[])

    assert response.stop_reason == "end_turn"  # returned unchanged
    record = by_name(buffer, "anthropic.messages.create")
    assert record.usage.model == "claude-opus-4-8"
    assert record.usage.input_tokens == 120
    assert record.usage.output_tokens == 45


def test_cache_tokens_are_kept_separate(buffer):
    response = FakeMessage(usage=FakeUsage(inp=10, out=5, cache_write=200, cache_read=3000))
    client = webrtrace.instrument(FakeClient(response))
    client.messages.create()

    usage = by_name(buffer, "anthropic.messages.create").usage
    assert usage.cache_read_input_tokens == 3000
    assert usage.cache_creation_input_tokens == 200
    assert usage.total_tokens == 3215


def test_a_refusal_is_recorded_as_suspect(buffer):
    # The silent failure this library exists for: HTTP 200, no content, nothing raised,
    # and the caller was still billed.
    response = FakeMessage(text="", stop_reason="refusal")
    client = webrtrace.instrument(FakeClient(response))
    client.messages.create()

    record = by_name(buffer, "anthropic.messages.create")
    assert record.status is NodeStatus.SUSPECT
    assert "declined" in record.signals["suspect"]
    assert record.usage.stop_reason == "refusal"


def test_truncation_is_flagged(buffer):
    client = webrtrace.instrument(FakeClient(FakeMessage(stop_reason="max_tokens")))
    client.messages.create()
    assert "truncated" in by_name(buffer, "anthropic.messages.create").signals["suspect"]


def test_an_async_client_is_traced(buffer):
    client = webrtrace.instrument(FakeAsyncClient())

    async def main():
        return await client.messages.create(model="claude-opus-4-8")

    assert asyncio.run(main()).stop_reason == "end_turn"
    assert by_name(buffer, "anthropic.messages.create").usage.input_tokens == 120


def test_untraced_attributes_pass_straight_through(buffer):
    client = webrtrace.instrument(FakeClient())
    assert client.api_key == "sk-not-real"  # unknown attributes still work


def test_instrumentation_does_not_swallow_provider_errors(buffer):
    class Failing(FakeMessages):
        def create(self, **kwargs):
            raise RuntimeError("rate limited")

    client = FakeClient()
    client.messages = Failing()
    traced = webrtrace.instrument(client)

    with pytest.raises(RuntimeError, match="rate limited"):
        traced.messages.create()
    assert by_name(buffer, "anthropic.messages.create").status is NodeStatus.ERROR


def test_a_response_without_usage_is_still_traced(buffer):
    class Bare:
        stop_reason = "end_turn"

    client = FakeClient()
    client.messages = FakeMessages(Bare())
    traced = webrtrace.instrument(client)
    traced.messages.create()

    record = by_name(buffer, "anthropic.messages.create")
    assert record.status is NodeStatus.OK
    assert record.usage is None or record.usage.input_tokens is None


def test_reading_a_hostile_response_never_breaks_the_call(buffer):
    class Hostile:
        @property
        def usage(self):
            raise RuntimeError("cannot read usage")

        @property
        def stop_reason(self):
            raise RuntimeError("cannot read stop_reason")

    client = FakeClient()
    client.messages = FakeMessages(Hostile())
    traced = webrtrace.instrument(client)

    assert isinstance(traced.messages.create(), Hostile)  # the call still returns


def test_instrumented_calls_nest_under_the_caller(buffer):
    client = webrtrace.instrument(FakeClient())

    @webR_node(name="agent")
    def agent():
        return client.messages.create()

    agent()
    call = by_name(buffer, "anthropic.messages.create")
    assert call.parent_id == by_name(buffer, "agent").node_id


# --- non-text detection -------------------------------------------------------------


def test_nan_and_infinity_are_caught():
    assert detect_nan({}, float("nan")) == {"nan": True}
    assert detect_nan({}, [1.0, 2.0, float("inf")]) == {"infinite": True}
    assert detect_nan({}, [1.0, 2.0]) is None


def test_nan_is_found_inside_nested_structures():
    assert detect_nan({}, {"scores": [0.5, float("nan")]}) == {"nan": True}


def test_an_all_zero_vector_is_flagged():
    assert detect_all_zeros({}, [0.0] * 8) == {"all_zeros": 8}
    assert detect_all_zeros({}, [0.0, 0.1]) is None
    assert detect_all_zeros({}, 0) is None  # a single zero is just a number


def test_empty_collections_are_flagged():
    assert detect_empty_collection({}, []) == {"empty_collection": "list"}
    assert detect_empty_collection({}, {}) == {"empty_collection": "dict"}
    assert detect_empty_collection({}, [1]) is None


def test_unchanged_value_uses_equality_not_identity():
    # Unlike link(), which must use identity: a transform returning an equal-but-distinct
    # object has still done nothing, and that is the thing worth reporting.
    assert detect_unchanged_value({"rows": [1, 2, 3]}, [1, 2, 3]) == {"unchanged_value": "rows"}
    assert detect_unchanged_value({"rows": [1, 2, 3]}, [1, 2]) is None


def test_booleans_are_not_treated_as_numbers():
    # bool is a subclass of int in Python; "True is out of range" would be nonsense.
    assert detect_all_zeros({}, [False, False]) is None


def test_a_broken_value_detector_is_contained():
    def exploding(inputs, output):
        raise RuntimeError("bad heuristic")

    exploding.name = "exploding"
    signals = run_value_detectors({}, [1], (exploding,))
    assert "detector_errors" in signals


def test_numeric_agents_get_signals_end_to_end(buffer):
    @webR_node(name="embed")
    def embed(text):
        return [0.0] * 16  # a failed embedding call

    embed("some input")
    assert by_name(buffer, "embed").signals["all_zeros"] == 16


def test_a_nan_result_marks_the_node_suspect(buffer):
    @webR_node(name="compute")
    def compute(values):
        return float("nan")

    compute([1, 2, 3])
    record = by_name(buffer, "compute")
    assert record.status is NodeStatus.SUSPECT
    assert record.signals["nan"] is True


def test_value_detectors_do_not_run_when_there_is_text(buffer):
    # A prose-returning agent must not pay for the numeric pass.
    @webR_node(name="agent")
    def agent(prompt):
        return "a normal answer"

    agent("a normal prompt")
    assert "all_zeros" not in (by_name(buffer, "agent").signals or {})


# --- rendering ----------------------------------------------------------------------


def test_tokens_and_value_signals_reach_the_tree(buffer):
    # A signal the renderer computes but never prints is a signal nobody acts on -- the
    # value detectors shipped invisible in the tree until this test existed.
    client = webrtrace.instrument(FakeClient())

    @webR_node(name="embed")
    def embed(text):
        return [0.0] * 16

    client.messages.create()
    embed("some input")

    tree = webrtrace.render_tree(webrtrace.export_graph(buffer))
    assert "in 120" in tree
    assert "out 45" in tree
    assert "all_zeros=16" in tree


def test_a_node_without_usage_renders_no_token_block(buffer):
    @webR_node(name="plain")
    def plain():
        return "x"

    plain()
    assert "[" not in webrtrace.render_tree(webrtrace.export_graph(buffer)).split("plain")[1]


# --- logging ------------------------------------------------------------------------


def test_library_warnings_go_to_a_logger_not_stdout(buffer, caplog, capsys):
    class ExplodingBuffer:
        def append(self, record):
            raise RuntimeError("sink down")

        def append_edge(self, edge):
            raise RuntimeError("sink down")

        def pin(self, ids):
            raise RuntimeError("sink down")

    original = webrtrace.get_buffer()
    webrtrace.set_buffer(ExplodingBuffer())
    try:
        with caplog.at_level(logging.WARNING, logger="webrtrace"):

            @webR_node(name="agent")
            def agent():
                return "fine"

            assert agent() == "fine"
    finally:
        webrtrace.set_buffer(original)

    assert any(r.name == "webrtrace" for r in caplog.records)
    # Nothing was printed: the application owns its output streams.
    assert capsys.readouterr().err == ""


def test_the_library_installs_no_handlers():
    # A library that configures handlers hijacks output it does not own.
    assert logging.getLogger("webrtrace").handlers == []
