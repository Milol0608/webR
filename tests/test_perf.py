# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Wall-clock overhead assertions.

Excluded from the default run (`-m 'not perf'`) and enabled with `pytest -m perf`. A
timing threshold is a real regression detector on a quiet machine and pure noise on a
shared CI runner, so it is opt-in rather than a source of red builds nobody trusts.

The ceilings are deliberately loose -- several times the measured figures in ADR 0002.
They exist to catch an order-of-magnitude regression, like the `findall(text)[:n]` bug
that cost 6.4ms per node, not to police a few percent of drift.
"""

from __future__ import annotations

import timeit

import pytest

import webrtrace
from webrtrace import webR_node

pytestmark = pytest.mark.perf

CAPTURE_OFF_CEILING_US = 25.0
CAPTURE_ON_CEILING_US = 400.0
LARGE_PAYLOAD_CEILING_US = 2_000.0


def _overhead_us(fn, payload: str) -> float:
    webrtrace.configure(capacity=1_000)

    def plain(value: str) -> str:
        return value

    baseline = timeit.Timer(lambda: plain(payload))
    traced = timeit.Timer(lambda: fn(payload))

    base_iters, base_total = baseline.autorange()
    trace_iters, trace_total = traced.autorange()
    return (trace_total / trace_iters - base_total / base_iters) * 1e6


def test_overhead_without_capture_is_small(buffer):
    traced = webR_node(name="off", capture=False)(lambda prompt: prompt)
    assert _overhead_us(traced, "word " * 200) < CAPTURE_OFF_CEILING_US


def test_overhead_without_capture_does_not_depend_on_payload_size(buffer):
    # Nothing on the no-capture path may touch the payload. If this ever fails, some
    # accidental O(n) work has crept into the hot path.
    traced = webR_node(name="off", capture=False)(lambda prompt: prompt)
    small = _overhead_us(traced, "x" * 100)
    large = _overhead_us(traced, "x" * 100_000)
    assert large < small * 4 + CAPTURE_OFF_CEILING_US


def test_overhead_with_capture_is_bounded_on_a_typical_prompt(buffer):
    traced = webR_node(name="on", capture=True)(lambda prompt: prompt)
    assert _overhead_us(traced, "word " * 400) < CAPTURE_ON_CEILING_US


def test_capture_cost_stays_bounded_on_a_huge_payload(buffer):
    # Detection is windowed, so a 100KB payload must not cost proportionally more than a
    # 10KB one. Only the content hash is legitimately linear.
    traced = webR_node(name="on", capture=True)(lambda prompt: prompt)
    assert _overhead_us(traced, "word " * 20_000) < LARGE_PAYLOAD_CEILING_US
