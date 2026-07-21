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
SCHEMA_VERSION = 1


def _build(nodes: list[dict[str, Any]], source_stats: dict[str, Any]) -> dict[str, Any]:
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
            "dangling_edges": dangling,
            "by_status": statuses,
        },
    }


def export_graph(buffer: TraceBuffer | None = None) -> dict[str, Any]:
    """Build a graph document from what is currently retained in memory."""
    target = buffer if buffer is not None else runtime.get_buffer()
    nodes = [record.to_dict() for record in target.records()]
    return _build(nodes, {"source": "buffer", **target.stats()})


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

    nodes: list[dict[str, Any]] = []
    read = 0
    for file in files:
        if file.is_dir():
            continue
        nodes.extend(load_jsonl(file))
        read += 1

    nodes.sort(key=lambda node: node.get("seq", 0))
    return _build(nodes, {"source": "jsonl", "files_read": read})


def write_graph(path: str | Path, buffer: TraceBuffer | None = None, *, indent: int = 2) -> Path:
    """Write the in-memory graph document to a JSON file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = export_graph(buffer)
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=indent, default=str)
        handle.write("\n")
    return destination
