# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Redaction: the payload never reaches memory or disk unscrubbed."""

from __future__ import annotations

import json

import pytest
from conftest import by_name

import webrtrace
from webrtrace import REDACTED, common_secrets, webR_node


@pytest.fixture(autouse=True)
def no_redactor():
    yield
    webrtrace.set_redactor(None)


# --- the built-in floor -------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "sk-proj-abcdefghijklmnopqrstuvwxyz012345",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "xoxb-1234567890-abcdefghij",
        "AKIAIOSFODNN7EXAMPLE",
        "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
        'password: "hunter2swordfish"',
        "api_key=abcdef123456",
        "customer@example.com",
        "4111 1111 1111 1111",
    ],
)
def test_common_secrets_catches_structurally_distinctive_secrets(secret):
    scrubbed = common_secrets(f"the value is {secret} and that is all")
    assert secret not in scrubbed
    assert REDACTED in scrubbed


def test_common_secrets_leaves_ordinary_prose_alone():
    prose = "The planner produced three steps and the executor ran all of them in 12 seconds."
    assert common_secrets(prose) == prose


def test_common_secrets_does_not_pretend_to_catch_pii():
    # Documented limitation, asserted so nobody mistakes it for compliance tooling.
    prose = "The patient, Maria Gonzalez, lives at 44 Elm Street and has diabetes."
    assert common_secrets(prose) == prose


# --- the hook ------------------------------------------------------------------------


def test_a_global_redactor_scrubs_before_anything_is_stored(buffer):
    @webR_node(name="agent", capture_full=True)
    def agent(prompt):
        return "responding to sk-proj-abcdefghijklmnopqrstuvwxyz012345"

    webrtrace.set_redactor(common_secrets)
    agent("my key is sk-proj-abcdefghijklmnopqrstuvwxyz012345 please use it")

    io = by_name(buffer, "agent").io
    assert "sk-proj" not in io["inputs"]["prompt"]["text"]
    assert "sk-proj" not in io["output"]["text"]
    assert REDACTED in io["inputs"]["prompt"]["text"]


def test_a_per_node_redactor_overrides_the_global_one(buffer):
    @webR_node(name="agent", capture_full=True, redact=lambda text: "SCRUBBED")
    def agent(prompt):
        return "out"

    webrtrace.set_redactor(common_secrets)
    agent("anything at all")

    assert by_name(buffer, "agent").io["inputs"]["prompt"]["text"] == "SCRUBBED"


def test_redaction_happens_before_the_hash(buffer):
    # Two payloads differing only in the redacted span must hash identically -- that is
    # what proves the hash is taken after scrubbing, not before.
    @webR_node(name="agent")
    def agent(prompt):
        return "ok"

    webrtrace.set_redactor(common_secrets)
    agent("token AKIAIOSFODNN7EXAMPLE here")
    agent("token AKIA1234567890ABCDEF here")

    hashes = {r.io["inputs"]["prompt"]["hash"] for r in buffer.records() if r.io}
    assert len(hashes) == 1


def test_redaction_happens_before_the_detectors_see_the_text(buffer):
    @webR_node(name="agent")
    def agent(prompt):
        return "   "

    webrtrace.set_redactor(lambda text: "")
    agent("some prompt")

    # The detectors ran on the scrubbed text, so the output still reads as empty.
    assert by_name(buffer, "agent").signals["empty_output"] is True


def test_redaction_reaches_the_jsonl_file(buffer, tmp_path):
    path = tmp_path / "run.jsonl"

    @webR_node(name="agent", capture_full=True)
    def agent(prompt):
        return "fine"

    webrtrace.set_redactor(common_secrets)
    webrtrace.start_writer(path, flush_interval=0.05)
    try:
        agent("key AKIAIOSFODNN7EXAMPLE")
        webrtrace.flush()
    finally:
        webrtrace.stop_writer()

    raw = path.read_text(encoding="utf-8")
    assert "AKIAIOSFODNN7EXAMPLE" not in raw
    assert REDACTED in raw
    assert json.loads(raw.splitlines()[0])["name"] == "agent"


# --- failing closed ------------------------------------------------------------------


def test_a_redactor_that_raises_drops_the_payload_rather_than_storing_it(buffer):
    # The whole point. A redactor breaking on an unusual input must not mean that exact
    # input gets written out in full.
    def broken(text):
        raise RuntimeError("regex blew up")

    @webR_node(name="agent", capture_full=True, redact=broken)
    def agent(prompt):
        return "response text"

    assert agent("sensitive value") == "response text"

    record = by_name(buffer, "agent")
    assert record.io is None
    assert record.signals["redaction_failed"] == ["output", "prompt"]


def test_a_redactor_returning_a_non_string_drops_the_payload(buffer):
    @webR_node(name="agent", redact=lambda text: None)
    def agent(prompt):
        return "out"

    agent("sensitive")
    assert by_name(buffer, "agent").io is None


def test_a_broken_redactor_does_not_break_the_traced_call(buffer):
    def broken(text):
        raise ValueError("nope")

    @webR_node(name="agent", redact=broken)
    def agent(prompt):
        return prompt.upper()

    assert agent("still works") == "STILL WORKS"


def test_redaction_failure_is_visible_rather_than_silent(buffer):
    @webR_node(name="agent", redact=lambda text: 1 / 0)
    def agent(prompt):
        return "out"

    agent("secret")
    assert "redaction_failed" in by_name(buffer, "agent").signals


def test_reset_clears_the_redactor(buffer):
    webrtrace.set_redactor(common_secrets)
    webrtrace.reset()
    assert webrtrace.runtime.redactor is None
