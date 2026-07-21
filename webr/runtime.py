# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Process-wide tracing state.

Per ADR 0001, `@webR_node` always wraps and consults `enabled` on every call rather than
disappearing at import time. The check is one module-attribute load and a branch -- tens
of nanoseconds against an agent that is about to wait seconds on a network call -- and it
buys the ability to turn tracing on inside a live, misbehaving process without a restart.

`enabled` is read directly by the decorator's hot path. Toggle it through `enable()` and
`disable()`; rebinding it via `from webr.runtime import enabled` captures a snapshot and
will not do what you want.
"""

from __future__ import annotations

import os

from .buffer import DEFAULT_CAPACITY, DEFAULT_PINNED_CAPACITY, TraceBuffer

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off"})


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSEY:
        return False
    return default


#: Whether instrumented callables record anything. Read on every traced call.
enabled: bool = _env_flag("WEBR_ENABLED", True)

_buffer = TraceBuffer()


def is_enabled() -> bool:
    """Whether tracing is currently recording."""
    return enabled


def enable() -> None:
    """Start recording. Safe to call at any time, including mid-run."""
    global enabled
    enabled = True


def disable() -> None:
    """Stop recording.

    Nodes already executing still complete and are recorded; only calls that begin after
    this point are skipped. Anything else would produce records with no duration.
    """
    global enabled
    enabled = False


def get_buffer() -> TraceBuffer:
    """The buffer completed nodes are appended to."""
    return _buffer


def set_buffer(buffer: TraceBuffer) -> None:
    """Swap the active buffer. Intended for tests and for scoped capture."""
    global _buffer
    _buffer = buffer


def configure(
    capacity: int = DEFAULT_CAPACITY,
    pinned_capacity: int = DEFAULT_PINNED_CAPACITY,
) -> TraceBuffer:
    """Replace the buffer with one of the given size, discarding anything recorded."""
    buffer = TraceBuffer(capacity=capacity, pinned_capacity=pinned_capacity)
    set_buffer(buffer)
    return buffer


def reset() -> None:
    """Drop everything recorded so far and re-enable tracing."""
    global enabled
    _buffer.clear()
    enabled = True
