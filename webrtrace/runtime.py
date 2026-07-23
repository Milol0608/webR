# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Process-wide tracing state.

Per ADR 0001, `@webR_node` always wraps and consults `enabled` on every call rather than
disappearing at import time. The check is one module-attribute load and a branch -- tens
of nanoseconds against an agent that is about to wait seconds on a network call -- and it
buys the ability to turn tracing on inside a live, misbehaving process without a restart.

`enabled` is read directly by the decorator's hot path. Toggle it through `enable()` and
`disable()`; rebinding it via `from webrtrace.runtime import enabled` captures a snapshot and
will not do what you want.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .buffer import DEFAULT_CAPACITY, DEFAULT_PINNED_CAPACITY, TraceBuffer
from .detectors import DEFAULT_DETECTORS, DEFAULT_SUSPECT_SIGNALS, Detector
from .records import EdgeRecord, NodeOpen, NodeRecord
from .redaction import Redactor
from .writer import JsonlWriter

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

#: Whether string payloads are fingerprinted and run through the detectors. On by default
#: so webR is useful without configuration; see ADR 0002 for what that costs.
capture: bool = _env_flag("WEBR_CAPTURE", True)

#: When True, capture stores the payload text itself (capped) rather than a fingerprint.
capture_full: bool = _env_flag("WEBR_CAPTURE_FULL", False)

#: When False, no readable payload is stored at all -- only lengths and hashes. Detectors
#: still run, so hallucination signals survive; signals that quote the payload are reduced
#: to counts. Note the default (True) stores short payloads in full and the first and last
#: 200 characters of long ones.
capture_text: bool = _env_flag("WEBR_CAPTURE_TEXT", True)

#: Detectors run on every captured node.
detectors: tuple[Detector, ...] = DEFAULT_DETECTORS

#: Signals that, on their own, mark a node suspect.
suspect_signals: frozenset[str] = DEFAULT_SUSPECT_SIGNALS

#: Applied to payload text before it is fingerprinted, inspected, or stored.
redactor: Redactor | None = None


def set_redactor(fn: Redactor | None) -> None:
    """Scrub every payload before it is recorded, process-wide.

    `webrtrace.common_secrets` is a reasonable floor for API keys and tokens; write your
    own for anything you actually have an obligation about. A redactor that raises causes
    the payload to be dropped rather than recorded -- see `redaction.apply`.
    """
    global redactor
    redactor = fn


def set_capture(on: bool, *, full: bool | None = None, text: bool | None = None) -> None:
    """Turn payload capture on or off process-wide.

    `full=True` stores payloads verbatim (capped). `text=False` stores none of the payload
    at all -- lengths and hashes only -- while leaving detection running, which is the
    setting for data you are not permitted to retain.
    """
    global capture, capture_full, capture_text
    capture = on
    if full is not None:
        capture_full = full
    if text is not None:
        capture_text = text


def set_detectors(*chosen: Detector) -> None:
    """Replace the detector set. Passing nothing disables detection but keeps capture."""
    global detectors
    detectors = tuple(chosen)


def set_suspect_signals(*names: str) -> None:
    """Choose which signals are damning enough to mark a node suspect."""
    global suspect_signals
    suspect_signals = frozenset(names)


_buffer = TraceBuffer()
_writer: JsonlWriter | None = None


def emit(record: NodeRecord) -> None:
    """Hand a completed node to every sink. Called once per traced call.

    The buffer is the bounded live view; the writer, when running, is the durable record.
    Both are O(1) appends -- no serialization happens here.
    """
    _buffer.append(record)
    writer = _writer
    if writer is not None:
        writer.submit(record)


def emit_edge(edge: EdgeRecord) -> None:
    """Hand a declared edge to every sink, on the same path completed nodes take."""
    _buffer.append_edge(edge)
    writer = _writer
    if writer is not None:
        writer.submit(edge)


def emit_open(marker: NodeOpen) -> None:
    """Record that a node has started, to the durable stream only.

    Skipped entirely when no writer is running: hang detection is a question you ask of
    the file after the fact, and the bounded in-memory buffer cannot answer it anyway.
    """
    writer = _writer
    if writer is not None:
        writer.submit(marker)


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


def default_trace_path() -> Path:
    """`traces/webrtrace-<pid>.jsonl` -- per process, deliberately.

    A single fixed default meant two processes appended to one file with no locking, and
    their interleaved partial lines destroyed records that neither process ever knew were
    lost. Since `graph_from_jsonl` reads a whole directory and stitches the halves, one
    file per process costs nothing and makes the multi-process case correct by default.
    """
    return Path("traces") / f"webrtrace-{os.getpid()}.jsonl"


def start_writer(
    path: str | Path | None = None,
    **options: Any,
) -> JsonlWriter:
    """Begin streaming completed nodes to a JSONL file.

    Without this, webR keeps only the bounded in-memory view, and a hard crash takes the
    trace with it. Any writer already running is stopped first so that two writers can
    never interleave lines into the same file.

    The default path includes the process id. If you pass an explicit path, it is yours to
    keep unique -- two processes sharing one file will corrupt it.
    """
    if path is None:
        path = default_trace_path()
    global _writer
    stop_writer()
    _writer = JsonlWriter(path, **options)
    return _writer


def stop_writer(timeout: float = 5.0) -> None:
    """Drain and close the active writer, if any."""
    global _writer
    writer, _writer = _writer, None
    if writer is not None:
        writer.stop(timeout=timeout)


def get_writer() -> JsonlWriter | None:
    """The active writer, or None if nothing is being streamed to disk."""
    return _writer


def flush() -> None:
    """Push everything queued to the OS. Useful before reading a trace file back."""
    writer = _writer
    if writer is not None:
        writer.flush()


def reset() -> None:
    """Drop everything recorded so far and re-enable tracing.

    Does not touch the writer: records already on disk are the durable history, and
    silently truncating a file the user asked for would be a nasty surprise.
    """
    global enabled, capture, capture_full, capture_text, detectors, suspect_signals, redactor
    # Imported here rather than at module scope: `links` imports this module, and a
    # top-level import either way would be circular.
    from .links import clear_marks

    _buffer.clear()
    # Marks outliving a reset meant a later link() could resolve to a node from the
    # discarded run, emitting an edge that claims data flowed from a node that is no
    # longer in the trace. Fabricated provenance is worse than a missing edge.
    clear_marks()
    enabled = True
    capture = _env_flag("WEBR_CAPTURE", True)
    capture_full = _env_flag("WEBR_CAPTURE_FULL", False)
    capture_text = _env_flag("WEBR_CAPTURE_TEXT", True)
    detectors = DEFAULT_DETECTORS
    suspect_signals = DEFAULT_SUSPECT_SIGNALS
    redactor = None
