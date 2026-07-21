# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""The lexical signals, in isolation."""

from __future__ import annotations

from webrtrace.detectors import (
    DEFAULT_SUSPECT_SIGNALS,
    MAX_CHARS_SCANNED,
    MAX_WORDS_SCANNED,
    Payloads,
    detect_empty_output,
    detect_input_overlap,
    detect_json_shape,
    detect_novel_numbers,
    detect_passthrough,
    detect_refusal,
    detect_repetition,
    is_suspect,
    run_detectors,
)
from webrtrace.fingerprint import HEAD_TAIL_CHARS, MAX_FULL_CHARS, as_text, fingerprint


def payloads(output, **inputs):
    return Payloads(inputs, output)


# --- fingerprints ------------------------------------------------------------------


def test_short_text_is_kept_whole():
    summary = fingerprint("hello")
    assert summary["len"] == 5
    assert summary["text"] == "hello"
    assert "truncated" not in summary


def test_long_text_keeps_only_head_and_tail():
    text = "A" * 5_000 + "END"
    summary = fingerprint(text)

    assert summary["len"] == 5_003
    assert len(summary["head"]) == HEAD_TAIL_CHARS
    assert summary["tail"].endswith("END")
    assert summary["truncated"] is True
    assert "text" not in summary


def test_hash_detects_change_and_ignores_identity():
    # The property the whole fingerprint idea rests on: same content, same hash.
    assert fingerprint("abc")["hash"] == fingerprint("abc")["hash"]
    assert fingerprint("abc")["hash"] != fingerprint("abd")["hash"]


def test_full_capture_stores_text_up_to_the_cap():
    text = "x" * (MAX_FULL_CHARS + 500)
    summary = fingerprint(text, full=True)

    assert len(summary["text"]) == MAX_FULL_CHARS
    assert summary["truncated"] is True
    assert summary["len"] == MAX_FULL_CHARS + 500


def test_only_immutable_text_payloads_are_captured():
    # ADR 0002: a mutable payload could change between capture and export, and repr-ing
    # arbitrary objects into a trace is how tracers end up serializing a DB connection.
    assert as_text("prompt") == "prompt"
    assert as_text(b"bytes") == "bytes"
    assert as_text({"prompt": "x"}) is None
    assert as_text(42) is None
    assert as_text(None) is None


# --- individual detectors ----------------------------------------------------------


def test_empty_output_is_flagged():
    assert detect_empty_output(payloads("   \n ")) == {"empty_output": True}
    assert detect_empty_output(payloads("content")) is None


def test_passthrough_names_the_untouched_input():
    assert detect_passthrough(payloads("same", prompt="same")) == {"passthrough": "prompt"}
    assert detect_passthrough(payloads("different", prompt="same")) is None


def test_novel_numbers_catch_fabricated_figures():
    # The headline detector: a summary citing revenue that appears in no source.
    result = detect_novel_numbers(
        payloads("Revenue was 4200000 in Q3", source="Q3 report covering 1200 customers")
    )
    assert result["novel_numbers"] == ["4200000"]
    assert result["novel_number_count"] == 1


def test_numbers_present_in_the_input_are_not_novel():
    assert detect_novel_numbers(payloads("total 1200", source="we had 1200 users")) is None


def test_number_formatting_does_not_create_false_positives():
    # "1,234.50" and "1234.5" are the same figure and must not read as fabrication.
    assert detect_novel_numbers(payloads("1234.5 dollars", source="cost was 1,234.50")) is None


def test_refusal_is_caught():
    result = detect_refusal(payloads("I'm sorry, I don't have access to that file."))
    assert result == {"refusal": "i don't have access"}
    assert detect_refusal(payloads("Here is the answer.")) is None


def test_json_shape_only_judges_text_that_claims_to_be_json():
    assert detect_json_shape(payloads('{"a": 1}')) == {"json_valid": True}
    assert detect_json_shape(payloads('{"a": 1')) == {"json_invalid": True}
    # Prose is not flagged for failing to be something it never claimed to be.
    assert detect_json_shape(payloads("The answer is 4.")) is None


def test_repetition_catches_a_degenerate_loop():
    looped = "the model is stuck repeating itself " * 20
    assert detect_repetition(payloads(looped))["repetition"] > 0.15
    varied = " ".join(f"word{i}" for i in range(200))
    assert detect_repetition(payloads(varied)) is None


def test_input_overlap_scores_grounding():
    source = " ".join(f"token{i}" for i in range(60))
    grounded = detect_input_overlap(payloads(source, source=source))
    unrelated = detect_input_overlap(
        payloads(" ".join(f"other{i}" for i in range(60)), source=source)
    )

    assert grounded["input_overlap"] == 1.0
    assert unrelated["input_overlap"] == 0.0


def test_short_texts_are_not_scored_on_ratios():
    # Ratios over a handful of words are noise, not signal.
    assert detect_repetition(payloads("a b c")) is None
    assert detect_input_overlap(payloads("a b c", source="a b c")) is None


# --- the runner --------------------------------------------------------------------


def test_run_detectors_merges_every_signal():
    signals = run_detectors({"source": "report of 1200 users"}, '{"total": 9999}')

    assert signals["json_valid"] is True
    assert signals["novel_numbers"] == ["9999"]
    assert "length_ratio" in signals


def test_a_broken_detector_is_contained():
    # A bug in a heuristic must never take down the traced program.
    def exploding(payloads):
        raise RuntimeError("detector bug")

    exploding.name = "exploding"

    signals = run_detectors({"a": "input"}, "output", (exploding,))
    assert "detector_errors" in signals
    assert "RuntimeError" in signals["detector_errors"][0]


def test_number_sign_is_ignored():
    # Documents a deliberate trade-off: the number pattern anchors on a digit so the
    # regex engine can skip ahead, which measured ~3x faster and loses the sign.
    assert detect_novel_numbers(payloads("we lost -42 units", source="42 units")) is None


# --- bounded cost ------------------------------------------------------------------


def test_scanning_is_bounded_by_construction():
    # Guards a real regression: an earlier version capped words with `findall(text)[:n]`,
    # which scans everything and then discards, costing 6.4ms per node on a 100KB
    # payload. The bound has to apply to the text before the scan, not to its results.
    huge = "word " * 100_000
    payload = Payloads({"source": huge}, huge)

    assert len(payload.scanned_output) <= MAX_CHARS_SCANNED + 1
    assert len(payload.output_words) <= MAX_WORDS_SCANNED
    assert len(payload.input_words) <= MAX_WORDS_SCANNED
    assert payload.truncated is True


def test_sampled_detection_says_so():
    huge = "word " * 100_000
    assert run_detectors({"source": huge}, huge)["detection_truncated"] is True


def test_a_huge_json_payload_is_reported_as_unchecked_not_parsed():
    big = "[" + ",".join('"item"' for _ in range(5_000)) + "]"
    assert detect_json_shape(payloads(big)) == {"json_unchecked": True}


def test_suspicion_is_conservative():
    # Fabricated numbers inform but do not accuse: a node that computes a total is
    # supposed to produce a figure nobody passed in.
    assert is_suspect({"novel_numbers": ["42"]}, DEFAULT_SUSPECT_SIGNALS) is None
    assert is_suspect({"refusal": "i cannot provide"}, DEFAULT_SUSPECT_SIGNALS) == "refusal"
    assert is_suspect({"empty_output": True}, DEFAULT_SUSPECT_SIGNALS) == "empty_output"
