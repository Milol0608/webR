# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for defects found in external adversarial review.

Each of these failed against the code as reviewed. They are kept as the guard against
the same mistakes returning.
"""

from __future__ import annotations

import pytest
from conftest import by_name

import webrtrace
from webrtrace import webR_node
from webrtrace.detectors import MAX_CHARS_SCANNED, Payloads, detect_json_shape


def test_generator_throw_is_forwarded_to_the_inner_generator(buffer):
    # DEF-1: the wrapper caught the thrown exception and closed the inner generator
    # instead of throwing into it, so a generator that recovers from an exception could
    # not. Tracing changed program behaviour, which is the one thing it must never do.
    @webR_node(name="stream")
    def stream():
        try:
            yield 1
        except ValueError as exc:
            yield f"caught: {exc}"

    gen = stream()
    assert next(gen) == 1
    assert gen.throw(ValueError("handled")) == "caught: handled"


def test_generator_return_value_reaches_yield_from(buffer):
    # DEF-5: StopIteration.value was discarded, so `yield from` on a traced generator
    # produced None instead of the generator's return value.
    @webR_node(name="stream")
    def stream():
        yield 1
        return "the result"

    def consumer():
        value = yield from stream()
        return value

    gen = consumer()
    next(gen)
    with pytest.raises(StopIteration) as caught:
        next(gen)
    assert caught.value.value == "the result"


def test_async_generator_athrow_is_forwarded(buffer):
    # DEF-1, async half.
    import asyncio

    @webR_node(name="stream")
    async def stream():
        try:
            yield 1
        except ValueError as exc:
            yield f"caught: {exc}"

    async def main():
        gen = stream()
        assert await gen.asend(None) == 1
        return await gen.athrow(ValueError("handled"))

    assert asyncio.run(main()) == "caught: handled"


def test_reset_clears_the_mark_registry(buffer):
    # DEF-3: marks outlived reset(), so a link after reset produced an edge pointing at
    # a node from a discarded run -- fabricated provenance.
    @webR_node(name="producer")
    def producer():
        return webrtrace.mark(["plan"])

    @webR_node(name="consumer")
    def consumer(plan):
        webrtrace.link(plan)

    plan = producer()
    webrtrace.reset()
    consumer(plan)

    sends = [e for e in webrtrace.export_graph()["edges"] if e["kind"] == "sends"]
    assert sends == []


def test_variadic_arguments_are_not_captured_under_the_wrong_name(buffer):
    # DEF-4: zip(param_names, args) paired the *args parameter name with the first
    # extra positional value, capturing "first" as if it were the whole tuple.
    @webR_node(name="agent", capture=("args",))
    def agent(*args):
        return "ok"

    agent("first", "second", "third")
    # The output is still fingerprinted; only the misattributed input must be gone.
    io = by_name(buffer, "agent").io
    assert "inputs" not in io


def test_keyword_only_and_variadic_parameters_do_not_shift_positional_names(buffer):
    # The same defect in its more damaging form: a name/value mismatch that silently
    # attributes one argument's text to a different parameter.
    @webR_node(name="agent")
    def agent(first, *rest, flag="x"):
        return "ok"

    agent("alpha", "beta")
    inputs = (by_name(buffer, "agent").io or {}).get("inputs", {})
    assert inputs.get("first") is not None
    assert inputs["first"]["text"] == "alpha"
    assert "rest" not in inputs


def test_an_explicit_empty_label_is_not_overridden_by_the_mark_label(buffer):
    # Section 2c: `label or resolved.label` meant an explicit "" fell through to the
    # mark's label, so a caller could not suppress it.
    @webR_node(name="producer")
    def producer():
        return webrtrace.mark(["plan"], "from-mark")

    @webR_node(name="consumer")
    def consumer(plan):
        webrtrace.link(plan, "")

    consumer(producer())
    assert buffer.edges()[0].label == ""


def test_json_detector_respects_the_scan_bound():
    # DEF-2: detect_json_shape read the raw unbounded output. Touching scanned_output
    # is what marks the payload truncated, so this asserts the detector went through
    # the bounded path.
    huge = '{"key": "' + "x" * (MAX_CHARS_SCANNED * 4) + '"}'
    payload = Payloads({}, huge)
    detect_json_shape(payload)
    assert payload.truncated is True
