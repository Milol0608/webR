# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Capture, validators, and taint -- the pieces that turn a call graph into a diagnosis."""

from __future__ import annotations

import pytest
from conftest import by_name

import webrtrace
from webrtrace import webR_node
from webrtrace.records import NodeStatus

# --- capture -----------------------------------------------------------------------


def test_string_arguments_are_captured_by_parameter_name(buffer):
    @webR_node(name="agent")
    def agent(prompt, temperature=0.0):
        return "a response"

    agent("summarize this")

    io = by_name(buffer, "agent").io
    assert io["inputs"]["prompt"]["text"] == "summarize this"
    assert io["output"]["text"] == "a response"
    # Non-string arguments are ignored rather than coerced.
    assert "temperature" not in io["inputs"]


def test_keyword_arguments_are_captured(buffer):
    @webR_node(name="agent")
    def agent(*, prompt):
        return "ok"

    agent(prompt="hello")
    assert by_name(buffer, "agent").io["inputs"]["prompt"]["text"] == "hello"


def test_capture_can_be_narrowed_to_named_parameters(buffer):
    @webR_node(name="agent", capture=("prompt",))
    def agent(prompt, system):
        return "ok"

    agent("the prompt", "a very large system message")

    inputs = by_name(buffer, "agent").io["inputs"]
    assert set(inputs) == {"prompt"}


def test_capture_can_be_disabled_per_node(buffer):
    @webR_node(name="agent", capture=False)
    def agent(prompt):
        return "ok"

    agent("secret")

    record = by_name(buffer, "agent")
    assert record.io is None
    assert record.signals is None


def test_capture_can_be_disabled_process_wide(buffer):
    @webR_node(name="agent")
    def agent(prompt):
        return "ok"

    webrtrace.set_capture(False)
    agent("secret")

    assert by_name(buffer, "agent").io is None


def test_per_node_setting_overrides_the_process_default(buffer):
    @webR_node(name="agent", capture=True)
    def agent(prompt):
        return "ok"

    webrtrace.set_capture(False)
    agent("still captured")

    assert by_name(buffer, "agent").io["inputs"]["prompt"]["text"] == "still captured"


def test_full_capture_stores_text_and_fingerprint_mode_does_not(buffer):
    long_prompt = "x" * 1_000

    @webR_node(name="fingerprinted")
    def fingerprinted(prompt):
        return "ok"

    @webR_node(name="full", capture_full=True)
    def full(prompt):
        return "ok"

    fingerprinted(long_prompt)
    full(long_prompt)

    assert "text" not in by_name(buffer, "fingerprinted").io["inputs"]["prompt"]
    assert by_name(buffer, "full").io["inputs"]["prompt"]["text"] == long_prompt


def test_inputs_are_still_captured_when_the_call_fails(buffer):
    # The inputs to a failing node are exactly what you want during a post-mortem.
    @webR_node(name="agent")
    def agent(prompt):
        raise ValueError("nope")

    with pytest.raises(ValueError):
        agent("what did I send")

    record = by_name(buffer, "agent")
    assert record.status is NodeStatus.ERROR
    assert record.io["inputs"]["prompt"]["text"] == "what did I send"
    assert "output" not in record.io


def test_nodes_with_no_text_payloads_record_no_io(buffer):
    @webR_node(name="agent")
    def agent(count):
        return count + 1

    agent(1)

    assert by_name(buffer, "agent").io is None


# --- signals reach the record ------------------------------------------------------


def test_detector_signals_are_recorded(buffer):
    @webR_node(name="summarizer")
    def summarizer(source):
        return "Revenue was 9999999 last quarter."

    summarizer("A report about 1200 customers.")

    signals = by_name(buffer, "summarizer").signals
    assert signals["novel_numbers"] == ["9999999"]


def test_a_refusal_marks_the_node_suspect_without_raising(buffer):
    # Nothing failed. The call returned a well-formed string. That is the point.
    @webR_node(name="agent")
    def agent(prompt):
        return "I'm sorry, I don't have access to that system."

    result = agent("fetch the data")

    assert result.startswith("I'm sorry")
    record = by_name(buffer, "agent")
    assert record.status is NodeStatus.SUSPECT
    assert record.signals["suspect"] == "refusal"


def test_suspect_signals_are_configurable(buffer):
    @webR_node(name="agent")
    def agent(source):
        return "The total is 8888."

    webrtrace.set_suspect_signals("novel_numbers")
    agent("A report with 12 items.")

    assert by_name(buffer, "agent").status is NodeStatus.SUSPECT


def test_detectors_can_be_switched_off_while_capture_stays_on(buffer):
    @webR_node(name="agent")
    def agent(prompt):
        return ""

    webrtrace.set_detectors()
    agent("hello")

    record = by_name(buffer, "agent")
    assert record.io is not None  # still fingerprinted
    assert record.status is NodeStatus.OK  # but nothing judged it


# --- validators --------------------------------------------------------------------


def test_a_failing_check_marks_suspect_and_returns_the_value(buffer):
    @webR_node(name="extractor", check=lambda out: out.strip().startswith("{"))
    def extractor(prompt):
        return "Sure! Here is your JSON: {}"

    result = extractor("give me json")

    assert result == "Sure! Here is your JSON: {}"  # unchanged, nothing raised
    record = by_name(buffer, "extractor")
    assert record.status is NodeStatus.SUSPECT
    assert record.signals["suspect"] == "check returned a falsy value"


def test_a_check_can_explain_itself(buffer):
    @webR_node(name="agent", check=lambda out: True if len(out) > 5 else "output too short")
    def agent(prompt):
        return "hi"

    agent("say something")

    assert by_name(buffer, "agent").signals["suspect"] == "output too short"


def test_a_passing_check_leaves_the_node_ok(buffer):
    @webR_node(name="agent", check=lambda out: True)
    def agent(prompt):
        return "fine"

    agent("go")
    assert by_name(buffer, "agent").status is NodeStatus.OK


def test_a_broken_check_does_not_break_the_call(buffer):
    def exploding(_):
        raise KeyError("bad validator")

    @webR_node(name="agent", check=exploding)
    def agent(prompt):
        return "fine"

    assert agent("go") == "fine"

    record = by_name(buffer, "agent")
    assert record.status is NodeStatus.SUSPECT
    assert "check raised KeyError" in record.signals["suspect"]


def test_a_check_is_not_run_when_the_call_raised(buffer):
    calls = []

    @webR_node(name="agent", check=lambda out: calls.append(out) or True)
    def agent(prompt):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        agent("go")

    assert calls == []
    assert by_name(buffer, "agent").status is NodeStatus.ERROR


# --- taint -------------------------------------------------------------------------


def test_a_suspect_node_taints_everything_above_it(buffer):
    # The blast radius: the orchestrator succeeded, but it built its answer on output
    # that looks wrong, so it is downstream of a problem.
    @webR_node(name="extractor", check=lambda out: False)
    def extractor(prompt):
        return "questionable"

    @webR_node(name="planner")
    def planner(prompt):
        return extractor(prompt)

    @webR_node(name="orchestrator")
    def orchestrator():
        return planner("go")

    orchestrator()

    assert by_name(buffer, "extractor").status is NodeStatus.SUSPECT
    assert by_name(buffer, "planner").tainted is True
    assert by_name(buffer, "orchestrator").tainted is True


def test_taint_does_not_spread_to_unrelated_branches(buffer):
    @webR_node(name="bad", check=lambda out: False)
    def bad():
        return "wrong"

    @webR_node(name="good")
    def good():
        return "fine"

    @webR_node(name="root")
    def root():
        good()
        bad()

    root()

    assert by_name(buffer, "good").tainted is False
    assert by_name(buffer, "root").tainted is True


def test_a_failure_also_taints_its_ancestors(buffer):
    @webR_node(name="boom")
    def boom():
        raise RuntimeError("nope")

    @webR_node(name="parent")
    def parent():
        try:
            boom()
        except RuntimeError:
            return "recovered"

    parent()

    # The parent swallowed the exception and reports success -- but the web still shows
    # that its result was built on top of something that failed.
    record = by_name(buffer, "parent")
    assert record.status is NodeStatus.OK
    assert record.tainted is True


def test_tainted_nodes_survive_eviction(buffer):
    small = webrtrace.configure(capacity=3, pinned_capacity=50)

    @webR_node(name="bad", check=lambda out: False)
    def bad():
        return "wrong"

    @webR_node(name="root")
    def root():
        return bad()

    @webR_node(name="filler")
    def filler():
        return None

    root()
    for _ in range(100):
        filler()

    names = {r.name for r in small.records()}
    assert {"bad", "root"} <= names
    webrtrace.configure()
