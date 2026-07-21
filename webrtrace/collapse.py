# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Collapsing per-invocation nodes into per-agent nodes.

ADR 0001 chose one node per *invocation* on the grounds that the aggregate is derivable
from the detail and the detail is not recoverable from the aggregate. This is the
derivation.

A run where an orchestrator calls `llm_call` forty times produces forty nodes, which is
the right thing to store and the wrong thing to look at. Collapsed, it is one node
labelled `llm_call x40` carrying total and worst-case durations, a status rollup, and the
count of nodes that were suspect.

The collapsed form is a **view**, not a trace: ids are synthesized, edges are deduplicated,
and a node's timings are sums rather than measurements of anything that happened once. It
is for reading, not for further analysis -- go back to the raw document for that.
"""

from __future__ import annotations

from typing import Any

from .records import NodeStatus

#: Rank used when several invocations of one agent disagree: the worst outcome wins, so a
#: single failure in forty successes is never hidden by the majority.
_STATUS_RANK = {
    NodeStatus.OK.value: 0,
    NodeStatus.RUNNING.value: 1,
    NodeStatus.SUSPECT.value: 2,
    NodeStatus.ERROR.value: 3,
}


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda status: _STATUS_RANK.get(status, 0))


def collapse_by_agent(document: dict[str, Any]) -> dict[str, Any]:
    """Aggregate a graph document by node name, one node per agent per parent.

    Grouping is by `(parent agent name, own name)` rather than by name alone. Two agents
    that happen to share a name but sit in different parts of the web stay distinct --
    merging them would invent a relationship the run never had.
    """
    nodes = document.get("nodes", [])
    if not nodes:
        return {**document, "nodes": [], "edges": [], "collapsed": True}

    by_id = {node["node_id"]: node for node in nodes}

    def group_key(node: dict[str, Any]) -> tuple[str, str]:
        parent = by_id.get(node.get("parent_id") or "")
        return (parent["name"] if parent else "", node.get("name", "?"))

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for node in nodes:
        groups.setdefault(group_key(node), []).append(node)

    # Every original node maps to the synthetic node that now represents it, so edges can
    # be rewritten without guessing.
    representative: dict[str, str] = {}
    collapsed_nodes: list[dict[str, Any]] = []

    for index, (key, members) in enumerate(groups.items()):
        synthetic_id = f"agent-{index:04d}"
        for member in members:
            representative[member["node_id"]] = synthetic_id

        durations = [member.get("duration_ns", 0) for member in members]
        statuses = [member.get("status", "ok") for member in members]
        suspects = sum(1 for status in statuses if status == NodeStatus.SUSPECT.value)
        errors = sum(1 for status in statuses if status == NodeStatus.ERROR.value)

        collapsed_nodes.append(
            {
                "node_id": synthetic_id,
                "name": key[1],
                "calls": len(members),
                "status": _worst(statuses),
                "duration_ns": sum(durations),
                "max_duration_ns": max(durations, default=0),
                "errors": errors,
                "suspects": suspects,
                "tainted": any(member.get("tainted") for member in members),
                "depth": min(member.get("depth", 0) for member in members),
                "seq": min(member.get("seq", 0) for member in members),
                # Kept so a reader can jump from the summary back to the real records.
                "node_ids": [member["node_id"] for member in members],
            }
        )

    # Parents resolve through the same mapping; a node whose parent was evicted keeps a
    # parent_id that maps to nothing, which the renderer already treats as a root.
    for collapsed, (_, members) in zip(collapsed_nodes, groups.items(), strict=True):
        parent_ids = {
            representative.get(member.get("parent_id") or "")
            for member in members
            if member.get("parent_id")
        }
        parent_ids.discard(None)
        collapsed["parent_id"] = parent_ids.pop() if len(parent_ids) == 1 else None

    collapsed_edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in document.get("edges", []):
        source = representative.get(edge["src_id"])
        target = representative.get(edge["dst_id"])
        if source is None or target is None or source == target:
            # Self-edges appear when two invocations of the same agent linked to each
            # other; as an aggregate that says nothing.
            continue
        key = (edge.get("kind", "invokes"), source, target)
        entry = collapsed_edges.setdefault(
            key, {"kind": key[0], "src_id": source, "dst_id": target, "count": 0}
        )
        entry["count"] += 1

    statuses = [node["status"] for node in collapsed_nodes]
    return {
        **document,
        "collapsed": True,
        "nodes": sorted(collapsed_nodes, key=lambda node: node["seq"]),
        "edges": list(collapsed_edges.values()),
        "roots": [node["node_id"] for node in collapsed_nodes if node["parent_id"] is None],
        "stats": {
            **document.get("stats", {}),
            "collapsed_from": len(nodes),
            "nodes": len(collapsed_nodes),
            "edges": len(collapsed_edges),
            "by_status": {status: statuses.count(status) for status in set(statuses)},
        },
    }
