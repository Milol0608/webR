# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Turn a graph document into something a human can read in a terminal.

A JSON document is the right thing to *store* and the wrong thing to read at 2am while
something is on fire. This renders the web as an indented tree, marks the nodes that
failed or look wrong, and shows the signals that justified each verdict.

Output is deliberately ASCII-only. Box-drawing characters are prettier and they break on
Windows consoles running a legacy code page, which is exactly where someone debugging a
production agent is likely to be.
"""

from __future__ import annotations

from typing import Any

STATUS_MARKS = {"ok": "[ ok]", "error": "[ERR]", "suspect": "[SUS]", "running": "[...]"}

#: Signals worth surfacing in a tree view, in the order they read best.
_HIGHLIGHT_SIGNALS = (
    "suspect",
    "refusal",
    "empty_output",
    "json_invalid",
    "passthrough",
    "novel_numbers",
    "repetition",
    "input_overlap",
    "detection_truncated",
)


def format_duration(nanoseconds: int) -> str:
    """Human-scaled duration, right-sized so columns stay comparable."""
    if nanoseconds < 1_000:
        return f"{nanoseconds}ns"
    if nanoseconds < 1_000_000:
        return f"{nanoseconds / 1_000:.1f}us"
    if nanoseconds < 1_000_000_000:
        return f"{nanoseconds / 1_000_000:.1f}ms"
    return f"{nanoseconds / 1_000_000_000:.2f}s"


def _signal_summary(node: dict[str, Any]) -> str:
    signals = node.get("signals") or {}
    parts: list[str] = []
    for name in _HIGHLIGHT_SIGNALS:
        if name not in signals:
            continue
        value = signals[name]
        if value is True:
            parts.append(name)
        elif isinstance(value, list):
            parts.append(f"{name}={','.join(str(item) for item in value[:3])}")
        else:
            parts.append(f"{name}={value}")
    return " ".join(parts)


def _describe(node: dict[str, Any], width: int) -> str:
    mark = STATUS_MARKS.get(node.get("status", ""), "[ ? ]")
    # Taint is a property of a node's *inputs*, not its own outcome, so it gets its own
    # marker rather than overwriting the status.
    taint = " *" if node.get("tainted") else "  "
    name = node.get("name", "<unnamed>")
    calls = node.get("calls")
    if calls and calls > 1:
        name = f"{name} x{calls}"
    padded = name.ljust(width)[:width] if width else name
    line = f"{mark}{taint} {padded}  {format_duration(node.get('duration_ns', 0)):>8}"

    # Collapsed nodes summarise many invocations, so the counts matter more than any one
    # of them; an aggregate hiding a single failure among forty successes is useless.
    if calls:
        parts = []
        if node.get("errors"):
            parts.append(f"{node['errors']} err")
        if node.get("suspects"):
            parts.append(f"{node['suspects']} suspect")
        if node.get("max_duration_ns"):
            parts.append(f"max {format_duration(node['max_duration_ns'])}")
        if parts:
            line += f"  ({', '.join(parts)})"
        return line.rstrip()

    error = node.get("error")
    if error:
        line += f"  {error['type']}: {error['message']}"
    summary = _signal_summary(node)
    if summary:
        line += f"  {summary}"
    return line.rstrip()


def render_tree(document: dict[str, Any], *, name_width: int = 32) -> str:
    """Render the call structure of a web, one line per node.

    Nodes whose parent is missing -- evicted, or written to a rotated file that was not
    read -- are rendered as roots rather than dropped. A tree that silently omits a
    subtree would be worse than one that shows it detached.
    """
    nodes = document.get("nodes", [])
    if not nodes:
        return "(empty web)"

    by_id = {node["node_id"]: node for node in nodes}
    children: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []

    for node in nodes:
        parent_id = node.get("parent_id")
        if parent_id is None or parent_id not in by_id:
            roots.append(node)
        else:
            children.setdefault(parent_id, []).append(node)

    for siblings in children.values():
        siblings.sort(key=lambda node: node.get("seq", 0))
    roots.sort(key=lambda node: node.get("seq", 0))

    lines: list[str] = []

    def walk(node: dict[str, Any], prefix: str, is_last: bool, is_root: bool) -> None:
        connector = "" if is_root else ("`- " if is_last else "|- ")
        lines.append(f"{prefix}{connector}{_describe(node, name_width)}")
        kids = children.get(node["node_id"], [])
        child_prefix = prefix if is_root else prefix + ("   " if is_last else "|  ")
        for index, child in enumerate(kids):
            walk(child, child_prefix, index == len(kids) - 1, False)

    for root in roots:
        walk(root, "", True, True)

    return "\n".join(lines)


def render_links(document: dict[str, Any]) -> str:
    """Render declared SENDS edges, which do not appear in the call tree at all."""
    by_id = {node["node_id"]: node.get("name", "?") for node in document.get("nodes", [])}
    sends = [edge for edge in document.get("edges", []) if edge.get("kind") == "sends"]
    if not sends:
        return ""

    lines = []
    for edge in sends:
        source = by_id.get(edge["src_id"], f"<evicted {edge['src_id'][:8]}>")
        target = by_id.get(edge["dst_id"], f"<evicted {edge['dst_id'][:8]}>")
        label = f" ({edge['label']})" if edge.get("label") else ""
        flag = "  [dangling]" if edge.get("dangling") else ""
        lines.append(f"  {source} => {target}{label}{flag}")
    return "\n".join(lines)


def render_summary(document: dict[str, Any]) -> str:
    """One-line health report, including everything the web admits it is missing."""
    stats = document.get("stats", {})
    statuses = stats.get("by_status", {})
    parts = [
        f"{stats.get('nodes', 0)} nodes",
        f"{stats.get('edges', 0)} edges",
        f"{len(document.get('traces', []))} trace(s)",
    ]
    if document.get("collapsed"):
        parts.insert(0, f"collapsed from {stats.get('collapsed_from', 0)} invocations")
    for status in ("ok", "error", "suspect"):
        if statuses.get(status):
            parts.append(f"{statuses[status]} {status}")
    if stats.get("dropped"):
        parts.append(f"{stats['dropped']} dropped")
    if stats.get("dangling_edges"):
        parts.append(f"{stats['dangling_edges']} dangling")
    return " | ".join(parts)


def render(document: dict[str, Any], *, name_width: int = 32) -> str:
    """The full report: summary, call tree, and declared data-dependency edges."""
    sections = [render_summary(document), "", render_tree(document, name_width=name_width)]
    links = render_links(document)
    if links:
        sections.extend(["", "data dependencies (SENDS):", links])
    return "\n".join(sections)


def failure_chains(document: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Root-to-failure paths, one per failed or suspect node.

    This is the answer to the question the library exists for: not "what went wrong" but
    "what was the chain of calls that led to the first thing that went wrong".
    """
    by_id = {node["node_id"]: node for node in document.get("nodes", [])}
    chains = []
    for node in document.get("nodes", []):
        if node.get("status") not in ("error", "suspect"):
            continue
        chain, current = [], node
        seen: set[str] = set()
        while current is not None and current["node_id"] not in seen:
            seen.add(current["node_id"])
            chain.append(current)
            current = by_id.get(current.get("parent_id"))
        chains.append(list(reversed(chain)))
    return chains


def render_failures(document: dict[str, Any]) -> str:
    """The failure chains, formatted for a human who wants the answer immediately."""
    chains = failure_chains(document)
    if not chains:
        return "no failures or suspect nodes"

    lines = []
    for chain in chains:
        culprit = chain[-1]
        path = " -> ".join(node.get("name", "?") for node in chain)
        detail = ""
        if culprit.get("error"):
            detail = f"{culprit['error']['type']}: {culprit['error']['message']}"
        elif (culprit.get("signals") or {}).get("suspect"):
            detail = f"suspect: {culprit['signals']['suspect']}"
        lines.append(f"  {path}")
        if detail:
            lines.append(f"      {detail}")
    return "\n".join(lines)
