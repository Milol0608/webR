# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Measure per-call tracing overhead across payload sizes.

    python benchmarks/overhead.py

Reports microseconds added per call, above the same function undecorated. Both capture
modes are measured: publishing only the favourable number would be dishonest, since
capture is on by default and its cost is the one that scales.
"""

from __future__ import annotations

import gc
import timeit

import webrtrace
from webrtrace import webR_node

PAYLOAD_SIZES = (0, 100, 1_000, 10_000, 100_000)


def _plain(prompt: str) -> str:
    return prompt


_traced_off = webR_node(name="off", capture=False)(lambda prompt: prompt)
_traced_on = webR_node(name="on", capture=True)(lambda prompt: prompt)


def _measure(fn, payload: str, *, repeats: int = 7, number: int = 2_000) -> float:
    """Microseconds per call, on a buffer small enough that eviction stays realistic.

    Reports the *minimum* of several repeats with the collector paused. A single
    `autorange()` on a busy machine swung by 2-3x between runs and made the no-capture
    column appear to depend on payload size, which it does not.
    """
    webrtrace.configure(capacity=1_000)
    gc.disable()
    try:
        timer = timeit.Timer(lambda: fn(payload))
        best = min(timer.repeat(repeat=repeats, number=number)) / number
    finally:
        gc.enable()
    return best * 1e6


def main() -> None:
    print(f"{'payload':>10} {'plain':>10} {'capture=off':>13} {'capture=on':>12}")
    for size in PAYLOAD_SIZES:
        payload = "word " * (size // 5) if size else ""
        base = _measure(_plain, payload)
        off = _measure(_traced_off, payload)
        on = _measure(_traced_on, payload)
        print(f"{size:>10} {base:>9.2f}us {off - base:>12.2f}us {on - base:>11.2f}us")


if __name__ == "__main__":
    main()
