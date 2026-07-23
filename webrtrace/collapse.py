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
    """Aggregate a graph document by agent, one node per agent per distinct parent.

    Grouping keys each node on `(the group its parent belongs to, its own name)`, computed
    parents-first. That keeps invocations of one agent under one parent merged, while
    keeping same-named agents under *different* parents separate.

    An earlier version keyed on the parent's *name*. When two different parents shared a
    name -- two `worker` nodes each calling `step` -- their children merged into one group
    whose members had two different real parents, and the parent resolution then gave up
    and set `parent_id = None`, detaching a failing subtree and promoting it to a root.
    Keying on the parent's resolved group, not its name, makes that impossible: every group
    has exactly one parent group by construction.
    """
    nodes = document.get("nodes", [])
    if not nodes:
        return {
            **document,
            "collapsed": True,
            "nodes": [],
            "edges": [],
            "roots": [],
            "stats": {**document.get("stats", {}), "collapsed_from": 0, "nodes": 0, "edges": 0},
        }

    by_id = {node["node_id"]: node for node in nodes if "node_id" in node}

    # Process parents before children so a node's parent already has a group id assigned.
    ordered = sorted(nodes, key=lambda n: (n.get("depth", 0), n.get("seq", 0)))

    representative: dict[str, str] = {}  # original node_id -> synthetic group id
    parent_rep_of: dict[str, str | None] = {}  # group id -> its parent group id (or None)
    group_of: dict[tuple[str | None, str], str] = {}  # (parent group, name) -> group id
    members_of: dict[str, list[dict[str, Any]]] = {}
    counter = 0

    for node in ordered:
        node_id = node.get("node_id")
        if node_id is None:
            continue
        parent_id = node.get("parent_id")
        if parent_id is None or parent_id not in by_id:
            # Genuine root, or a node whose parent was evicted -- either way it has no
            # resolvable parent group. Evicted parents are kept distinct from real roots
            # by tagging with the (unique) missing id so unrelated subtrees do not merge.
            parent_rep: str | None = None if parent_id is None else f"evicted:{parent_id}"
        else:
            parent_rep = representative.get(parent_id)

        key = (parent_rep, node.get("name", "?"))
        synthetic_id = group_of.get(key)
        if synthetic_id is None:
            synthetic_id = f"agent-{counter:04d}"
            counter += 1
            group_of[key] = synthetic_id
            members_of[synthetic_id] = []
            # A parent group that is a real synthetic id becomes the collapsed parent; an
            # evicted or absent parent leaves this group a root of the collapsed view.
            parent_rep_of[synthetic_id] = parent_rep if parent_rep in members_of else None
        representative[node_id] = synthetic_id
        members_of[synthetic_id].append(node)

    collapsed_nodes: list[dict[str, Any]] = []
    for synthetic_id, members in members_of.items():
        durations = [member.get("duration_ns", 0) for member in members]
        statuses = [member.get("status", "ok") for member in members]
        suspects = sum(1 for status in statuses if status == NodeStatus.SUSPECT.value)
        errors = sum(1 for status in statuses if status == NodeStatus.ERROR.value)

        collapsed_nodes.append(
            {
                "node_id": synthetic_id,
                "name": members[0].get("name", "?"),
                "parent_id": parent_rep_of[synthetic_id],
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

    collapsed_edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in document.get("edges", []):
        # `.get` rather than `[...]`: a trace file truncated mid-write can yield an edge
        # missing an endpoint, and refusing to collapse a damaged trace helps nobody.
        source = representative.get(edge.get("src_id"))
        target = representative.get(edge.get("dst_id"))
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
