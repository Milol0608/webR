# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Lexical signals that make silent failures visible.

When an LLM agent hallucinates, nothing raises. The function returns a well-formed string
that happens to be wrong, and every conventional observability tool records a success.
These detectors look for the shapes that wrongness tends to take, using nothing but the
text -- no model, no network, no dependency.

What they catch: fabricated figures, format collapse, degenerate repetition, refusals,
truncation, no-op passthroughs, and output unrelated to input.

What they cannot catch, stated plainly so nobody trusts this further than it deserves: a
fluent, plausible, correctly-formatted sentence that is simply false. Detecting that needs
embeddings or a judge model. The `Detector` protocol is public so such a detector can be
added later -- but not inline, for the reasons in ADR 0002.

The check with the clearest diagnostic value is `novel_numbers`. When a summarizer invents
a revenue figure or a date that appeared nowhere in its input, that is detectable
lexically, in one pass, with no model at all. Two limits on it, stated rather than
measured away: inputs are joined and then sampled head-and-tail, so a figure that appears
only in the unread middle will look fabricated when it is not (`detection_truncated` marks
those nodes), and it fires legitimately on any node whose job is to produce a new number.
That is why it is not in `DEFAULT_SUSPECT_SIGNALS`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Protocol, runtime_checkable

#: Hard ceiling on how much text any detector reads, in characters.
#:
#: This bounds *time*, not just memory, and that distinction was learned the hard way:
#: an earlier version capped the number of words with `findall(text)[:2000]`, which scans
#: the whole string before slicing. On a 100KB payload -- unremarkable for an agent
#: prompt -- detection cost 6.4ms per node. Slicing the text *first* makes the cost flat
#: regardless of payload size.
#:
#: Text longer than this is sampled head-and-tail, and `detection_truncated` is reported
#: so nothing downstream mistakes a sampled verdict for a complete one.
MAX_CHARS_SCANNED = 4_000

#: Narrower window for the word-level ratio detectors (repetition, input overlap).
#:
#: Word tokenizing dominates detection cost -- it produces one Python string object per
#: word -- and these two detectors are the only consumers. They are fuzzy heuristics, so
#: judging them on a sample is acceptable in a way that number extraction is not: missing
#: an input number would make a legitimate figure look fabricated.
WORD_WINDOW_CHARS = 2_000

#: Words considered for the structural ratios, after the character window above.
MAX_WORDS_SCANNED = 400

#: Below this many words, repetition and overlap ratios are noise rather than signal.
MIN_WORDS_FOR_RATIOS = 20

REPETITION_THRESHOLD = 0.15
NGRAM_SIZE = 5

# Starts with `\d` rather than `-?\d` deliberately. A leading optional group forces the
# regex engine to attempt a match at every character; anchoring on a digit lets it skip
# ahead to the next digit, which measured about three times faster on prose. The cost is
# that sign is ignored, so "-42" and "42" compare equal -- acceptable for asking whether
# a figure appeared in the input at all.
_NUMBER_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_WORD_RE = re.compile(r"[a-z0-9']+")

#: Phrases that mean the agent gave up. A refusal is a silent failure with excellent
#: grammar: the call succeeded, the pipeline continued, and nothing downstream noticed.
_REFUSAL_PHRASES = (
    "i don't have access",
    "i do not have access",
    "i'm unable to",
    "i am unable to",
    "i cannot provide",
    "i can't provide",
    "as an ai language model",
    "as an ai model",
    "i don't have enough information",
    "i do not have enough information",
    "unable to determine",
    "no information available",
)

#: Signals that on their own justify marking a node suspect. Deliberately conservative:
#: `novel_numbers` fires often and legitimately (a node that computes a total is supposed
#: to produce a number nobody passed in), so it informs rather than accuses.
#:
#: `nan` and `infinite` join them because an undefined number is never a correct result,
#: whatever the domain. `all_zeros` and `empty_collection` do not: an empty result list is
#: frequently the right answer.
DEFAULT_SUSPECT_SIGNALS = frozenset({"empty_output", "refusal", "json_invalid", "nan", "infinite"})


class Payloads:
    """The text of one call, with the expensive derivations computed at most once.

    Detectors receive this rather than raw strings so that tokenizing, number extraction,
    and lowercasing happen once per node no matter how many detectors want them.
    """

    __slots__ = (
        "_input_numbers",
        "_input_words",
        "_lower_output",
        "_output_numbers",
        "_output_words",
        "_scanned_input",
        "_scanned_output",
        "inputs",
        "output",
        "truncated",
    )

    def __init__(self, inputs: Mapping[str, str], output: str | None) -> None:
        self.inputs = inputs
        self.output = output
        self.truncated = False
        self._scanned_input: str | None = None
        self._scanned_output: str | None = None
        self._input_numbers: set[str] | None = None
        self._output_numbers: list[str] | None = None
        self._input_words: list[str] | None = None
        self._output_words: list[str] | None = None
        self._lower_output: str | None = None

    def _bound(self, text: str) -> str:
        """Sample head and tail so scanning cost is flat in payload size."""
        if len(text) <= MAX_CHARS_SCANNED:
            return text
        self.truncated = True
        half = MAX_CHARS_SCANNED // 2
        return f"{text[:half]}\n{text[-half:]}"

    @property
    def scanned_output(self) -> str:
        if self._scanned_output is None:
            self._scanned_output = self._bound(self.output) if self.output else ""
        return self._scanned_output

    @property
    def scanned_input(self) -> str:
        if self._scanned_input is None:
            self._scanned_input = self._bound(" ".join(self.inputs.values()))
        return self._scanned_input

    @property
    def lower_output(self) -> str:
        if self._lower_output is None:
            self._lower_output = self.scanned_output.lower()
        return self._lower_output

    @property
    def input_numbers(self) -> set[str]:
        if self._input_numbers is None:
            self._input_numbers = {
                _normalize_number(match) for match in _NUMBER_RE.findall(self.scanned_input)
            }
        return self._input_numbers

    @property
    def output_numbers(self) -> list[str]:
        if self._output_numbers is None:
            found = _NUMBER_RE.findall(self.scanned_output)
            self._output_numbers = [_normalize_number(match) for match in found]
        return self._output_numbers

    @property
    def input_words(self) -> list[str]:
        if self._input_words is None:
            window = self.scanned_input[:WORD_WINDOW_CHARS].lower()
            self._input_words = _WORD_RE.findall(window)[:MAX_WORDS_SCANNED]
        return self._input_words

    @property
    def output_words(self) -> list[str]:
        if self._output_words is None:
            window = self.lower_output[:WORD_WINDOW_CHARS]
            self._output_words = _WORD_RE.findall(window)[:MAX_WORDS_SCANNED]
        return self._output_words


def _normalize_number(raw: str) -> str:
    """`1,234.50` and `1234.5` are the same figure and must compare equal."""
    cleaned = raw.replace(",", "")
    if "." in cleaned:
        cleaned = cleaned.rstrip("0").rstrip(".")
    return cleaned or "0"


@runtime_checkable
class Detector(Protocol):
    """Inspects one call's payloads and returns signals, or None if it has nothing to say.

    A detector must be cheap, must not raise, and must not perform I/O. One that violates
    the first rule slows every traced call; one that violates the others is isolated by
    `run_detectors`, but should not rely on that.
    """

    name: str

    def __call__(self, payloads: Payloads) -> Mapping[str, Any] | None: ...


def _named(name: str):
    def attach(fn):
        fn.name = name
        return fn

    return attach


@_named("empty_output")
def detect_empty_output(payloads: Payloads) -> Mapping[str, Any] | None:
    """An agent that returned nothing at all, successfully.

    Reads the bounded text, not the raw output: `"   ".strip()` on a megabyte of
    whitespace copies a megabyte. If head and tail are both blank the payload is blank
    for every purpose that matters here.
    """
    if payloads.output is not None and not payloads.scanned_output.strip():
        return {"empty_output": True}
    return None


@_named("passthrough")
def detect_passthrough(payloads: Payloads) -> Mapping[str, Any] | None:
    """Output identical to an input: the node did nothing to the content."""
    if not payloads.output:
        return None
    for name, text in payloads.inputs.items():
        if text == payloads.output:
            return {"passthrough": name}
    return None


@_named("length_ratio")
def detect_length_ratio(payloads: Payloads) -> Mapping[str, Any] | None:
    """Output size against input size. Catches truncation and runaway generation."""
    if payloads.output is None or not payloads.inputs:
        return None
    input_length = sum(len(text) for text in payloads.inputs.values())
    if input_length == 0:
        return None
    return {"length_ratio": round(len(payloads.output) / input_length, 3)}


@_named("novel_numbers")
def detect_novel_numbers(payloads: Payloads) -> Mapping[str, Any] | None:
    """Figures in the output that appear in no input.

    The classic fabrication signature: a summary citing a revenue number, a date, or a
    percentage that was never in the source material.
    """
    if not payloads.output or not payloads.inputs:
        return None
    known = payloads.input_numbers
    novel = [number for number in payloads.output_numbers if number not in known]
    if not novel:
        return None
    unique = list(dict.fromkeys(novel))
    return {"novel_numbers": unique[:10], "novel_number_count": len(unique)}


@_named("refusal")
def detect_refusal(payloads: Payloads) -> Mapping[str, Any] | None:
    """A polite, well-formed surrender."""
    if not payloads.output:
        return None
    haystack = payloads.lower_output[:1_000]
    for phrase in _REFUSAL_PHRASES:
        if phrase in haystack:
            return {"refusal": phrase}
    return None


@_named("json_shape")
def detect_json_shape(payloads: Payloads) -> Mapping[str, Any] | None:
    """Whether output that is trying to be JSON actually is.

    Only fires when the text looks like an attempt at JSON, so prose is never flagged for
    failing to be something it never claimed to be.
    """
    if not payloads.output:
        return None

    # Bounded text first. Stripping the raw output was an unbounded copy whenever the
    # payload actually had leading whitespace -- measured at 2.1ms on 10MB -- and model
    # output very often begins with a newline, so that path was routinely taken.
    stripped = payloads.scanned_output.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    if payloads.truncated or len(payloads.output) > MAX_CHARS_SCANNED:
        # Parsing is bounded like every other check. Saying "not checked" is honest;
        # spending milliseconds proving a huge payload is well-formed is not the job.
        return {"json_unchecked": True}
    try:
        json.loads(stripped)
    except ValueError:
        return {"json_invalid": True}
    return {"json_valid": True}


@_named("repetition")
def detect_repetition(payloads: Payloads) -> Mapping[str, Any] | None:
    """Degenerate loops -- the model getting stuck repeating a phrase."""
    words = payloads.output_words
    if len(words) < max(MIN_WORDS_FOR_RATIOS, NGRAM_SIZE * 2):
        return None
    # Non-overlapping n-grams: five times less work than the sliding window, and a
    # degenerate loop repeats often enough that stride sampling still catches it.
    counts: dict[tuple[str, ...], int] = {}
    total = 0
    for index in range(0, len(words) - NGRAM_SIZE + 1, NGRAM_SIZE):
        gram = tuple(words[index : index + NGRAM_SIZE])
        counts[gram] = counts.get(gram, 0) + 1
        total += 1
    if total == 0:
        return None
    worst = max(counts.values())
    ratio = worst / total
    if ratio < REPETITION_THRESHOLD:
        return None
    return {"repetition": round(ratio, 3)}


@_named("input_overlap")
def detect_input_overlap(payloads: Payloads) -> Mapping[str, Any] | None:
    """Token overlap between input and output.

    A near-zero overlap on a transform that should have been grounded in its input is a
    strong hint the model wandered off and generated something unrelated.
    """
    output_words, input_words = payloads.output_words, payloads.input_words
    if len(output_words) < MIN_WORDS_FOR_RATIOS or len(input_words) < MIN_WORDS_FOR_RATIOS:
        return None
    output_set, input_set = set(output_words), set(input_words)
    union = output_set | input_set
    if not union:
        return None
    return {"input_overlap": round(len(output_set & input_set) / len(union), 3)}


# --- non-text payloads --------------------------------------------------------------
#
# The detectors above read strings, which leaves a numeric or structured agent with the
# DAG and validators but no automatic signals at all. These close that gap.
#
# The framing that makes them belong here: the problem is not "hallucination", it is **a
# component returning a wrong answer without failing**. A NaN propagating through a
# pipeline, an all-zero embedding, a confidence outside [0, 1] -- none of them raise, and
# all of them are the same shape of bug as a fabricated number in prose.


def _numbers_in(value: Any) -> Iterator[float]:
    """Yield the floats reachable in a value, bounded, without importing numpy.

    Handles scalars, sequences, mappings, and one level of nesting -- enough for the
    embeddings, score dicts, and record lists agents actually pass. Arrays from numpy and
    friends are read through the sequence protocol they already implement.
    """
    if isinstance(value, bool):
        return  # a bool is an int in Python, and "True is out of range" is nonsense
    if isinstance(value, (int, float)):
        yield float(value)
        return
    if isinstance(value, Mapping):
        items: Iterable[Any] = value.values()
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = value
    elif hasattr(value, "tolist"):  # numpy-like, without importing numpy
        try:
            items = value.tolist()
            if isinstance(items, (int, float)):
                yield float(items)
                return
        except Exception:
            return
    else:
        return

    count = 0
    for item in items:
        for number in _numbers_in(item):
            yield number
            count += 1
            if count >= MAX_NUMBERS_SCANNED:
                return


#: Ceiling on how many numbers a value detector reads, mirroring the text bounds above.
MAX_NUMBERS_SCANNED = 10_000


def detect_nan(inputs: Mapping[str, Any], output: Any) -> Mapping[str, Any] | None:
    """NaN or infinity in the output -- a computation that silently went undefined."""
    for number in _numbers_in(output):
        if number != number:  # NaN is the only value not equal to itself
            return {"nan": True}
        if number in (float("inf"), float("-inf")):
            return {"infinite": True}
    return None


def detect_all_zeros(inputs: Mapping[str, Any], output: Any) -> Mapping[str, Any] | None:
    """An all-zero vector: the usual shape of an embedding or feature call that failed."""
    numbers = list(_numbers_in(output))
    if len(numbers) >= 2 and all(number == 0.0 for number in numbers):
        return {"all_zeros": len(numbers)}
    return None


def detect_empty_collection(inputs: Mapping[str, Any], output: Any) -> Mapping[str, Any] | None:
    """An empty container returned where the caller expected results."""
    if isinstance(output, (list, tuple, set, frozenset, dict)) and len(output) == 0:
        return {"empty_collection": type(output).__name__}
    return None


def detect_unchanged_value(inputs: Mapping[str, Any], output: Any) -> Mapping[str, Any] | None:
    """Output equal to an input: the node did nothing to the value.

    Equality rather than identity here, deliberately, and unlike `link()`. A transform
    that returns a distinct-but-identical object has still done nothing, which is the
    thing worth reporting.
    """
    if output is None:
        return None
    for name, value in inputs.items():
        try:
            if type(value) is type(output) and value == output:
                return {"unchanged_value": name}
        except Exception:  # __eq__ on a user type is allowed to be exotic
            continue
    return None


#: Detectors that read raw values rather than text. Run only when a node has no text
#: payload, so a prose-returning agent never pays for them.
VALUE_DETECTORS: tuple[Any, ...] = (
    detect_nan,
    detect_all_zeros,
    detect_empty_collection,
    detect_unchanged_value,
)

for _detector, _name in zip(
    VALUE_DETECTORS,
    ("nan", "all_zeros", "empty_collection", "unchanged_value"),
    strict=True,
):
    _detector.name = _name


def run_value_detectors(
    inputs: Mapping[str, Any],
    output: Any,
    detectors: tuple[Any, ...] = VALUE_DETECTORS,
) -> dict[str, Any]:
    """Run the non-text detectors. Contained exactly like `run_detectors`."""
    signals: dict[str, Any] = {}
    failures: list[str] = []
    for detector in detectors:
        try:
            reported = detector(inputs, output)
        except Exception as exc:  # a broken heuristic must not break the traced program
            failures.append(f"{getattr(detector, 'name', detector)}: {exc!r}")
            continue
        if reported:
            signals.update(reported)
    if failures:
        signals["detector_errors"] = failures
    return signals


#: Every built-in, in the order they run.
DEFAULT_DETECTORS: tuple[Detector, ...] = (
    detect_empty_output,
    detect_passthrough,
    detect_length_ratio,
    detect_novel_numbers,
    detect_refusal,
    detect_json_shape,
    detect_repetition,
    detect_input_overlap,
)


def run_detectors(
    inputs: Mapping[str, str],
    output: str | None,
    detectors: tuple[Detector, ...] = DEFAULT_DETECTORS,
) -> dict[str, Any]:
    """Run every detector over one call's payloads and merge what they report.

    A detector that raises is contained and reported as a signal rather than allowed to
    escape: a bug in a heuristic must never take down the traced program.
    """
    payloads = Payloads(inputs, output)
    signals: dict[str, Any] = {}
    failures: list[str] = []

    for detector in detectors:
        try:
            reported = detector(payloads)
        except Exception as exc:  # containment is the entire point of this loop
            failures.append(f"{getattr(detector, 'name', detector)}: {exc!r}")
            continue
        if reported:
            signals.update(reported)

    if payloads.truncated:
        # Nothing downstream should mistake a sampled verdict for a complete one.
        signals["detection_truncated"] = True
    if failures:
        signals["detector_errors"] = failures
    return signals


#: Signals whose *value* is copied out of the payload rather than derived from it. With
#: text storage disabled these are reduced to counts and booleans, so enabling detection
#: never becomes a side channel that reintroduces the content you chose not to keep.
_VALUE_BEARING_SIGNALS = ("novel_numbers", "refusal")


def strip_payload_values(signals: dict[str, Any]) -> dict[str, Any]:
    """Reduce signals that quote the payload to non-quoting equivalents."""
    if "novel_numbers" in signals:
        # The count already carries the diagnostic weight; the figures themselves were
        # lifted verbatim out of the output.
        signals["novel_numbers"] = f"{len(signals['novel_numbers'])} redacted"
    if "refusal" in signals:
        signals["refusal"] = True
    return signals


def is_suspect(signals: Mapping[str, Any], suspect_signals: frozenset[str]) -> str | None:
    """The name of the first signal that justifies marking this node suspect, if any."""
    for name in signals:
        if name in suspect_signals:
            return name
    return None
