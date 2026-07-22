# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Assembling the web into a single document.

JSONL is the durable stream: append-only, crash-safe, one node per line, unbounded. It is
the wrong shape for anything that needs the whole graph at once -- a visualizer, a diff
between two runs, a "which node broke the chain" query. This module produces that other
shape, from either source.

The exported document always carries its own honesty: `stats` reports how many nodes were
dropped and how many edges point at a parent that is no longer present, so a rendering can
show "42 nodes not recorded" instead of quietly implying the web is complete.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import runtime
from .buffer import TraceBuffer
from .records import EdgeKind, now_unix_ns

#: Bump when the document layout changes in a way consumers must notice.
#: v2 added the `record` discriminator and explicit `sends` edges.
SCHEMA_VERSION = 2


def _build(
    nodes: list[dict[str, Any]],
    sends: list[dict[str, Any]],
    source_stats: dict[str, Any],
) -> dict[str, Any]:
    from . import __version__

    index = {node["node_id"] for node in nodes}
    edges: list[dict[str, Any]] = []
    dangling = 0

    for node in nodes:
        parent_id = node.get("parent_id")
        if parent_id is None:
            continue
        # An edge whose parent was evicted (or written to a different rotated file) is
        # still real -- it is reported, and flagged, rather than dropped.
        is_dangling = parent_id not in index
        dangling += is_dangling
        edge: dict[str, Any] = {
            "kind": EdgeKind.INVOKES.value,
            "src_id": parent_id,
            "dst_id": node["node_id"],
        }
        if is_dangling:
            edge["dangling"] = True
        edges.append(edge)

    for declared in sends:
        # A SENDS edge may legitimately cross traces -- that is the whole point when a
        # payload moves through a queue -- so a missing endpoint is flagged, never
        # dropped, exactly as for call edges.
        entry = {key: value for key, value in declared.items() if key != "record"}
        missing = declared["src_id"] not in index or declared["dst_id"] not in index
        if missing:
            entry["dangling"] = True
            dangling += 1
        edges.append(entry)

    statuses: dict[str, int] = {}
    for node in nodes:
        statuses[node["status"]] = statuses.get(node["status"], 0) + 1

    return {
        "schema": SCHEMA_VERSION,
        "webr_version": __version__,
        "exported_unix_ns": now_unix_ns(),
        "traces": sorted({node["trace_id"] for node in nodes}),
        "roots": [node["node_id"] for node in nodes if node.get("parent_id") is None],
        "nodes": nodes,
        "edges": edges,
        "stats": {
            **source_stats,
            "nodes": len(nodes),
            "edges": len(edges),
            "invokes_edges": len(edges) - len(sends),
            "sends_edges": len(sends),
            "dangling_edges": dangling,
            "by_status": statuses,
        },
    }


def export_graph(buffer: TraceBuffer | None = None) -> dict[str, Any]:
    """Build a graph document from what is currently retained in memory."""
    target = buffer if buffer is not None else runtime.get_buffer()
    nodes = [record.to_dict() for record in target.records()]
    sends = [edge.to_dict() for edge in target.edges()]
    source_stats: dict[str, Any] = {"source": "buffer", **target.stats()}

    # If a writer is streaming, its drops are records that left the buffer and never
    # reached disk. Fold them in so the document does not imply completeness the run did
    # not have.
    writer = runtime.get_writer()
    if writer is not None:
        writer_stats = writer.stats()
        writer_dropped = writer_stats.get("dropped", 0)
        if writer_dropped:
            source_stats["writer_dropped"] = writer_dropped
        if writer_stats.get("write_errors"):
            source_stats["write_errors"] = writer_stats["write_errors"]
    return _build(nodes, sends, source_stats)


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL trace file.

    Malformed lines are skipped rather than fatal: the last line of a file written by a
    process that was killed mid-write is frequently a partial record, and refusing to
    read the whole trace because of it would be exactly the wrong behaviour for a
    post-mortem tool.
    """
    nodes: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                nodes.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return nodes


def graph_from_jsonl(path: str | Path) -> dict[str, Any]:
    """Build a graph document from one or more JSONL files.

    Accepts a file or a directory. Nodes are ordered by `seq`, which restores invocation
    order even when rotation split the run across several files.
    """
    target = Path(path)
    files = sorted(target.iterdir()) if target.is_dir() else [target]

    entries: list[dict[str, Any]] = []
    read = 0
    for file in files:
        if file.is_dir():
            continue
        entries.extend(load_jsonl(file))
        read += 1

    entries.sort(key=lambda entry: entry.get("seq", 0))
    # Lines written before schema v2 carry no discriminator; treat them as nodes, which
    # is what they were, rather than refusing to read an older trace file.
    nodes = [entry for entry in entries if entry.get("record", "node") == "node"]
    sends = [entry for entry in entries if entry.get("record") == "edge"]

    # An "open" marker whose node has no terminal record is a node that started and never
    # finished -- a hang, or a process killed mid-call. Surface it as `running` so the
    # trace shows the node that was actually stuck rather than silently omitting it.
    terminal_ids = {node["node_id"] for node in nodes if "node_id" in node}
    running = 0
    for entry in entries:
        if entry.get("record") != "open" or entry.get("node_id") in terminal_ids:
            continue
        nodes.append(
            {
                "trace_id": entry.get("trace_id"),
                "node_id": entry.get("node_id"),
                "parent_id": entry.get("parent_id"),
                "name": entry.get("name", "?"),
                "seq": entry.get("seq", 0),
                "status": "running",
                "started_unix_ns": entry.get("started_unix_ns", 0),
                "duration_ns": 0,
                "depth": entry.get("depth", 0),
            }
        )
        running += 1

    nodes.sort(key=lambda node: node.get("seq", 0))
    stats: dict[str, Any] = {"source": "jsonl", "files_read": read}
    if running:
        stats["running"] = running

    # Meta lines carry the writer's running drop/error counts (monotonic), so a reader can
    # tell the trace is incomplete instead of trusting a file that silently lost records.
    dropped = max((e.get("dropped", 0) for e in entries if e.get("record") == "meta"), default=0)
    write_errors = max(
        (e.get("write_errors", 0) for e in entries if e.get("record") == "meta"), default=0
    )
    if dropped:
        stats["dropped"] = dropped
    if write_errors:
        stats["write_errors"] = write_errors
    return _build(nodes, sends, stats)


def write_graph(path: str | Path, buffer: TraceBuffer | None = None, *, indent: int = 2) -> Path:
    """Write the in-memory graph document to a JSON file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = export_graph(buffer)
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=indent, default=str)
        handle.write("\n")
    return destination
