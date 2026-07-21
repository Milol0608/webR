# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""The terminal renderer: the surface most people will actually read a trace through."""

from __future__ import annotations

import pytest

import webrtrace
from webrtrace import webR_node
from webrtrace.render import (
    failure_chains,
    format_duration,
    render,
    render_failures,
    render_links,
    render_summary,
    render_tree,
)


@pytest.mark.parametrize(
    ("nanoseconds", "expected"),
    [(500, "500ns"), (1_500, "1.5us"), (2_500_000, "2.5ms"), (3_000_000_000, "3.00s")],
)
def test_durations_are_scaled_to_something_readable(nanoseconds, expected):
    assert format_duration(nanoseconds) == expected


def test_empty_web_renders_without_crashing():
    assert render_tree({"nodes": []}) == "(empty web)"


def test_tree_shows_nesting_and_status(buffer):
    @webR_node(name="child")
    def child():
        return 1

    @webR_node(name="parent")
    def parent():
        return child()

    parent()
    output = render_tree(webrtrace.export_graph(buffer))

    lines = output.splitlines()
    assert lines[0].startswith("[ ok]")
    assert "parent" in lines[0]
    assert lines[1].lstrip().startswith("`-") or lines[1].lstrip().startswith("|-")
    assert "child" in lines[1]


def test_failures_and_suspects_are_marked(buffer):
    @webR_node(name="boom")
    def boom():
        raise RuntimeError("nope")

    @webR_node(name="fishy", check=lambda out: False)
    def fishy(prompt):
        return "questionable"

    with pytest.raises(RuntimeError):
        boom()
    fishy("go")

    output = render_tree(webrtrace.export_graph(buffer))
    assert "[ERR]" in output
    assert "[SUS]" in output
    assert "RuntimeError: nope" in output


def test_tainted_nodes_carry_their_own_marker(buffer):
    @webR_node(name="bad", check=lambda out: False)
    def bad(prompt):
        return "wrong"

    @webR_node(name="root")
    def root():
        return bad("go")

    root()
    output = render_tree(webrtrace.export_graph(buffer))

    root_line = next(line for line in output.splitlines() if "root" in line)
    # Taint describes a node's inputs, not its outcome, so it must not overwrite [ ok].
    assert "[ ok] *" in root_line


def test_a_node_whose_parent_was_evicted_renders_as_a_root(buffer):
    # Dropping an orphaned subtree would hide exactly the nodes someone is looking for.
    document = {
        "nodes": [
            {"node_id": "b", "parent_id": "missing", "name": "orphan", "status": "ok", "seq": 1}
        ]
    }
    assert "orphan" in render_tree(document)


def test_sends_edges_are_rendered_separately(buffer):
    @webR_node(name="producer")
    def producer():
        return webrtrace.mark(["plan"], "plan")

    @webR_node(name="consumer")
    def consumer(plan):
        webrtrace.link(plan)

    consumer(producer())
    output = render_links(webrtrace.export_graph(buffer))

    assert "producer => consumer" in output
    assert "(plan)" in output


def test_no_links_renders_empty_rather_than_a_header(buffer):
    @webR_node(name="solo")
    def solo():
        return None

    solo()
    assert render_links(webrtrace.export_graph(buffer)) == ""


def test_summary_reports_gaps_honestly(buffer):
    small = webrtrace.configure(capacity=2, pinned_capacity=2)

    @webR_node(name="filler")
    def filler():
        return None

    for _ in range(20):
        filler()

    assert "dropped" in render_summary(webrtrace.export_graph(small))
    webrtrace.configure()


def test_failure_chains_run_from_root_to_culprit(buffer):
    @webR_node(name="boom")
    def boom():
        raise RuntimeError("nope")

    @webR_node(name="middle")
    def middle():
        boom()

    @webR_node(name="top")
    def top():
        middle()

    with pytest.raises(RuntimeError):
        top()

    chains = failure_chains(webrtrace.export_graph(buffer))
    names = [[node["name"] for node in chain] for chain in chains]
    assert ["top", "middle", "boom"] in names


def test_render_failures_says_so_when_there_are_none(buffer):
    @webR_node(name="fine")
    def fine():
        return None

    fine()
    assert render_failures(webrtrace.export_graph(buffer)) == "no failures or suspect nodes"


def test_full_render_includes_every_section(buffer):
    @webR_node(name="producer")
    def producer():
        return webrtrace.mark(["plan"])

    @webR_node(name="consumer")
    def consumer(plan):
        webrtrace.link(plan)

    consumer(producer())
    output = render(webrtrace.export_graph(buffer))

    assert "nodes" in output
    assert "producer" in output
    assert "data dependencies (SENDS):" in output


def test_output_is_ascii_only(buffer):
    # Box-drawing characters break on Windows consoles using a legacy code page, which
    # is exactly where someone debugging a production agent tends to be.
    @webR_node(name="parent")
    def parent():
        @webR_node(name="child")
        def child():
            return 1

        return child()

    parent()
    output = render(webrtrace.export_graph(buffer))
    output.encode("ascii")  # raises UnicodeEncodeError if anything non-ASCII crept in
