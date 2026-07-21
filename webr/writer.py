# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Durable JSONL streaming on a background thread.

The in-memory buffer is bounded and evicts, so on its own it cannot answer "what happened
during the whole run". This writer is the system of record: every completed node is
serialized to a JSONL file, which means eviction from the buffer loses nothing permanent
and a hard crash still leaves the trace on disk.

Nothing here runs on the traced thread. `submit` appends to a bounded deque and returns;
serialization, disk writes, and rotation all happen on the writer thread. The one
concession to latency is that a failed or suspect node wakes the writer immediately
instead of waiting for the next interval -- the moment something goes wrong, it is
durable.

Durability caveat, stated plainly: the writer calls `flush()`, which hands bytes to the
OS. That survives a process crash, which is the scenario this exists for. It does not
call `fsync()` and so does not survive a power cut; paying an fsync per failed node would
cost milliseconds on the very path that is already going badly.
"""

from __future__ import annotations

import atexit
import json
import threading
from collections import deque
from pathlib import Path
from typing import Any

from .records import NodeRecord

#: Wake the writer once this many records are pending, rather than waiting for the timer.
BATCH_WAKE_THRESHOLD = 256

DEFAULT_FLUSH_INTERVAL = 0.5
DEFAULT_QUEUE_CAPACITY = 10_000
DEFAULT_ROTATE_BYTES = 64 * 1024 * 1024


def _encode(record: NodeRecord) -> str:
    # `default=str` is a deliberate backstop: user-supplied attributes may hold objects
    # json knows nothing about, and a tracing library must never raise inside its own
    # writer because someone attached a datetime to a node.
    return json.dumps(record.to_dict(), separators=(",", ":"), default=str)


class JsonlWriter:
    """Drains completed nodes to a rotating JSONL file on a daemon thread."""

    def __init__(
        self,
        path: str | Path,
        *,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
        rotate_bytes: int = DEFAULT_ROTATE_BYTES,
    ) -> None:
        self._path = Path(path)
        self._flush_interval = flush_interval
        self._capacity = queue_capacity
        self._rotate_bytes = rotate_bytes

        self._lock = threading.Lock()
        self._pending: deque[NodeRecord] = deque()
        self._wake = threading.Event()
        self._stopping = False
        self._dropped = 0
        self._written = 0
        self._rotations = 0
        self._bytes_in_file = 0

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a", encoding="utf-8", newline="\n")
        self._bytes_in_file = self._path.stat().st_size

        # Daemon, because a non-daemon thread would be joined *before* atexit handlers
        # run and this loop only stops when an atexit handler tells it to -- the
        # interpreter would hang on exit. The atexit hook below does the orderly drain.
        self._thread = threading.Thread(target=self._run, name="webr-writer", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def submit(self, record: NodeRecord) -> None:
        """Queue a completed node. O(1), no I/O, never blocks."""
        with self._lock:
            if len(self._pending) >= self._capacity:
                # Drop-oldest and count it. Silently claiming a complete trace would be
                # worse than admitting the gap.
                self._pending.popleft()
                self._dropped += 1
            self._pending.append(record)
            urgent = record.is_interesting or len(self._pending) >= BATCH_WAKE_THRESHOLD
        if urgent:
            self._wake.set()

    def flush(self) -> None:
        """Drain everything queued right now and push it to the OS. Blocks the caller."""
        self._drain()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the thread after one final drain. Idempotent."""
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
        self._wake.set()
        self._thread.join(timeout=timeout)
        # Drain again in case the thread was killed by the timeout mid-cycle, then close.
        self._drain()
        with self._lock:
            if not self._file.closed:
                self._file.close()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "path": str(self._path),
                "written": self._written,
                "dropped": self._dropped,
                "pending": len(self._pending),
                "rotations": self._rotations,
            }

    # -- writer thread -------------------------------------------------------------

    def _run(self) -> None:
        while True:
            self._wake.wait(self._flush_interval)
            self._wake.clear()
            # Read the stop flag *before* draining, so the final drain always happens
            # after the flag is set and no record queued before `stop` is lost.
            with self._lock:
                stopping = self._stopping
            self._drain()
            if stopping:
                return

    def _drain(self) -> None:
        with self._lock:
            if not self._pending:
                return
            batch = self._pending
            self._pending = deque()
            closed = self._file.closed

        if closed:
            return

        payload = "".join(f"{_encode(record)}\n" for record in batch)
        with self._lock:
            if self._file.closed:
                return
            self._file.write(payload)
            self._file.flush()
            self._written += len(batch)
            self._bytes_in_file += len(payload.encode("utf-8"))
            if self._bytes_in_file >= self._rotate_bytes:
                self._rotate()

    def _rotate(self) -> None:
        """Start a new file. Caller holds the lock."""
        self._file.close()
        self._rotations += 1
        rotated = self._path.with_name(f"{self._path.name}.{self._rotations}")
        try:
            self._path.rename(rotated)
        except OSError:
            # Rotation is a convenience. If the filesystem refuses -- a reader holds the
            # file open on Windows, say -- keep appending rather than losing the trace.
            self._file = self._path.open("a", encoding="utf-8", newline="\n")
            self._bytes_in_file = 0
            return
        self._file = self._path.open("a", encoding="utf-8", newline="\n")
        self._bytes_in_file = 0
