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
import logging
import threading
from collections import deque
from pathlib import Path
from typing import Any

from .records import EdgeRecord, NodeRecord

logger = logging.getLogger("webrtrace")

#: Wake the writer once this many records are pending, rather than waiting for the timer.
BATCH_WAKE_THRESHOLD = 256

DEFAULT_FLUSH_INTERVAL = 0.5
DEFAULT_QUEUE_CAPACITY = 10_000
DEFAULT_ROTATE_BYTES = 64 * 1024 * 1024


def _encode(record: NodeRecord | EdgeRecord) -> str:
    # `default=str` is a deliberate backstop: user-supplied attributes may hold objects
    # json knows nothing about, and a tracing library must never raise inside its own
    # writer because someone attached a datetime to a node.
    return json.dumps(record.to_dict(), separators=(",", ":"), default=str)


class JsonlWriter:
    """Drains completed nodes to a rotating JSONL file on a daemon thread.

    **Locking.** Two locks, deliberately separate:

    - `_lock` guards the pending queue and the counters.
    - `_io_lock` guards the file handle, its size, and rotation.

    They are **never held at the same time**; every method that needs both takes them one
    after the other. That is the invariant to preserve when changing this class -- with a
    single lock, `submit()` waited on disk writes, so a slow filesystem could stall the
    traced program by hundreds of milliseconds while the docstring promised it never
    blocks. Since nothing nests them, there is no lock order to get wrong and no deadlock
    to construct.
    """

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

        self._lock = threading.Lock()  # queue + counters
        self._io_lock = threading.Lock()  # file handle, size, rotation
        self._pending: deque[NodeRecord | EdgeRecord] = deque()
        self._wake = threading.Event()
        self._stopping = False
        self._dropped = 0
        self._written = 0
        self._rotations = 0
        self._bytes_in_file = 0
        self._write_errors = 0
        self._warned = False
        self._last_meta = (0, 0)
        self._rotation_failures = 0

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a", encoding="utf-8", newline="\n")
        self._bytes_in_file = self._path.stat().st_size

        # Daemon, because a non-daemon thread would be joined *before* atexit handlers
        # run and this loop only stops when an atexit handler tells it to -- the
        # interpreter would hang on exit. The atexit hook below does the orderly drain.
        self._thread = threading.Thread(target=self._run, name="webrtrace-writer", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def submit(self, record: NodeRecord | EdgeRecord) -> None:
        """Queue a completed node or a declared edge. O(1), no I/O, never blocks."""
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
        """Drain everything queued right now and push it to the OS. Blocks the caller.

        Never raises. A caller reaching for `flush()` is usually mid-incident; handing
        them a second, unrelated failure to debug would be actively hostile.
        """
        self._drain()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the thread after one final drain. Idempotent."""
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
        self._wake.set()
        self._thread.join(timeout=timeout)
        # Drain again in case the thread was killed by the timeout mid-cycle, then write a
        # final meta line so the total drop count is durable even if the last drops
        # happened after the last successful batch, then close.
        self._drain()
        self._write_meta_if_changed()
        with self._io_lock:
            if not self._file.closed:
                self._file.close()
        # Every writer registered an atexit handler; without this, each one ever created
        # stays referenced (and alive) for the life of the process.
        atexit.unregister(self.stop)

    def stats(self) -> dict[str, Any]:
        """Counters for what reached disk and what did not.

        `dropped` covers every record that never made it: queue overflow when the writer
        could not keep up, and batches lost to a write failure. `write_errors` isolates
        the second cause.
        """
        # Two locks, taken one after the other rather than nested (see the class docstring
        # for the ordering rule). The counters and the file state are independent, so a
        # momentarily inconsistent pairing here is harmless.
        with self._lock:
            stats = {
                "path": str(self._path),
                "written": self._written,
                "dropped": self._dropped,
                "pending": len(self._pending),
                "write_errors": self._write_errors,
            }
        with self._io_lock:
            stats["rotations"] = self._rotations
            stats["rotation_failures"] = self._rotation_failures
        return stats

    # -- writer thread -------------------------------------------------------------

    def _run(self) -> None:
        while True:
            self._wake.wait(self._flush_interval)
            self._wake.clear()
            # Read the stop flag *before* draining, so the final drain always happens
            # after the flag is set and no record queued before `stop` is lost.
            with self._lock:
                stopping = self._stopping
            try:
                self._drain()
            except Exception as exc:  # last line of defence; _drain handles its own
                self._record_failure(0, exc)
            if stopping:
                return

    def _drain(self) -> None:
        """Move everything queued to disk. Holds no lock while encoding or writing.

        The queue lock is released before any disk work begins, which is the whole point
        of the split: `submit` -- and therefore every traced call -- must never wait on a
        slow filesystem.
        """
        with self._lock:
            if not self._pending:
                return
            batch = self._pending
            self._pending = deque()  # type: ignore[assignment]

        try:
            payload = "".join(f"{_encode(record)}\n" for record in batch)
        except Exception as exc:
            # Encoding runs user-supplied __repr__ via `default=str`. A batch that cannot
            # be encoded is lost, but it must not take the writer down with it.
            self._record_failure(len(batch), exc)
            return

        failure: Exception | None = None
        with self._io_lock:
            if self._file.closed:
                # The batch is gone and nobody will write it. Count it, rather than
                # letting a shutdown race make records vanish while `dropped` reads zero.
                failure = RuntimeError("writer closed before drain")
            else:
                try:
                    self._file.write(payload)
                    self._file.flush()
                except Exception as exc:
                    # A full disk, a revoked handle, a network mount that went away. The
                    # batch is gone; the writer is not. Dropping the thread here would end
                    # the trace silently, mid-incident, which is exactly when it is needed.
                    failure = exc
                else:
                    self._bytes_in_file += len(payload.encode("utf-8"))
                    rotate_now = self._bytes_in_file >= self._rotate_bytes
                    if rotate_now:
                        self._rotate()

        # Counter updates happen after the io lock is released, never inside it, so the
        # two locks are only ever held one at a time.
        if failure is not None:
            self._record_failure(len(batch), failure)
            return
        with self._lock:
            self._written += len(batch)
        self._write_meta_if_changed()

    def _write_meta_if_changed(self) -> None:
        """Persist the running drop/error counts as a meta line when they move.

        Takes **no** lock on entry: it reads the counters under the queue lock, then
        writes under the io lock, never holding both. Without this line, a reader opening
        the file offline has no way to know the writer dropped records -- the count lives
        only in the live `stats()` of a process that may be gone. `graph_from_jsonl` reads
        these back so an exported document can report `dropped` honestly instead of
        implying a completeness it does not have.
        """
        with self._lock:
            current = (self._dropped, self._write_errors)
            if current == self._last_meta:
                return
            line = json.dumps(
                {"record": "meta", "dropped": current[0], "write_errors": current[1]},
                separators=(",", ":"),
            )

        with self._io_lock:
            if self._file.closed:
                return
            try:
                self._file.write(line + "\n")
                self._file.flush()
            except Exception:  # best-effort; never let a meta line break the writer
                return
            self._bytes_in_file += len(line) + 1

        with self._lock:
            self._last_meta = current

    def _record_failure(self, lost: int, exc: Exception) -> None:
        """Count a failed batch and say so once, loudly, on stderr.

        Once, because a failing disk fails every batch, and a scrolling wall of identical
        errors is its own kind of silence. The running total lives in `stats()`. Must be
        called with neither lock held.
        """
        with self._lock:
            first = self._count_failure(lost)

        if first:
            logger.warning(
                "trace writing to %s failed (%s: %s); tracing continues in memory but "
                "records from here on are not reaching disk -- see writer.stats()",
                self._path,
                type(exc).__name__,
                exc,
            )

    def _count_failure(self, lost: int) -> bool:
        """Update the counters, returning whether this is the first failure. Lock held."""
        self._write_errors += 1
        self._dropped += lost
        first, self._warned = not self._warned, True
        return first

    #: Rotation indices probed before giving up on finding a free name.
    MAX_ROTATION_PROBE = 10_000

    def _next_rotation_path(self) -> Path | None:
        """The first `<name>.N` that does not already exist, or None if none is free.

        Probing rather than trusting a counter matters twice over. `Path.rename` is
        `os.rename`, which **silently replaces** an existing target on POSIX and raises on
        Windows -- so the same code either destroyed a previous run's rotated trace or
        failed to rotate at all, depending on the platform. And `_rotations` starts at zero
        in every process, so a second run would otherwise aim straight at the first run's
        files.
        """
        for index in range(1, self.MAX_ROTATION_PROBE):
            candidate = self._path.with_name(f"{self._path.name}.{index}")
            if not candidate.exists():
                return candidate
        return None

    def _rotate(self) -> None:
        """Start a new file. Caller holds `_io_lock`.

        Rotation is a convenience, never a reason to lose the trace. If no free name
        exists, or the filesystem refuses the rename -- a reader holding the file open on
        Windows, say -- webR keeps appending to the current file. The size counter resets
        either way, so a refused rotation is retried after another `rotate_bytes` rather
        than on every single write.
        """
        self._file.close()
        rotated = self._next_rotation_path()
        if rotated is not None:
            try:
                self._path.rename(rotated)
                self._rotations += 1
            except OSError as exc:
                self._rotation_failures += 1
                if self._rotation_failures == 1:
                    logger.warning(
                        "could not rotate %s (%s: %s); appending to the existing file, "
                        "which may exceed rotate_bytes",
                        self._path,
                        type(exc).__name__,
                        exc,
                    )
        self._file = self._path.open("a", encoding="utf-8", newline="\n")
        self._bytes_in_file = 0
