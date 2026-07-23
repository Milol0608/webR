# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Suspicion profiles and the standalone HTML report."""

from __future__ import annotations

import json
import re

import pytest
from conftest import by_name

import webrtrace
from webrtrace import webR_node
from webrtrace.records import NodeStatus

# --- profiles -----------------------------------------------------------------------


def test_llm_profile_is_the_conservative_default(buffer):
    webrtrace.set_profile("llm")
    assert webrtrace.export_graph  # sanity
    assert "all_zeros" not in webrtrace.runtime.suspect_signals
    assert "refusal" in webrtrace.runtime.suspect_signals


def test_data_profile_promotes_all_zeros(buffer):
    webrtrace.set_profile("data")

    @webR_node(name="embed")
    def embed(text):
        return [0.0] * 16

    embed("some text")
    # Under the default policy this is informational; under 'data' it is damning.
    assert by_name(buffer, "embed").status is NodeStatus.SUSPECT


def test_strict_profile_flags_novel_numbers(buffer):
    webrtrace.set_profile("strict")

    @webR_node(name="summarize")
    def summarize(rows):
        return "The total is 4210 units."  # a figure from nowhere

    summarize("north, south, east")
    assert by_name(buffer, "summarize").status is NodeStatus.SUSPECT


def test_an_unknown_profile_raises_rather_than_silently_passing(buffer):
    with pytest.raises(ValueError, match="unknown profile"):
        webrtrace.set_profile("aggressive")
    # The policy is untouched, not left in some half-applied state.
    assert webrtrace.runtime.suspect_signals == webrtrace.DEFAULT_SUSPECT_SIGNALS


def test_profile_is_only_policy_not_which_detectors_run(buffer):
    # 'llm' does not mark all_zeros suspect, but the detector still runs and records it.
    webrtrace.set_profile("llm")

    @webR_node(name="embed")
    def embed(text):
        return [0.0] * 4

    embed("text")
    record = by_name(buffer, "embed")
    assert record.status is NodeStatus.OK
    assert record.signals["all_zeros"] == 4


# --- HTML report --------------------------------------------------------------------


def _run_a_small_web(buffer):
    @webR_node(name="agent")
    def agent():
        webrtrace.record_usage(webrtrace.Usage(model="m", input_tokens=100, output_tokens=40))
        return "ok"

    @webR_node(name="broken")
    def broken():
        raise ValueError("boom")

    import contextlib

    agent()
    with contextlib.suppress(ValueError):
        broken()
    return webrtrace.export_graph(buffer)


def test_html_is_one_self_contained_file(buffer):
    html = webrtrace.render_html(_run_a_small_web(buffer))
    assert html.startswith("<!doctype html>")
    # No external anything: a report that phones out renders blank on an air-gapped box.
    assert "http://" not in html
    assert "https://" not in html
    assert "src=" not in html


def test_html_embeds_the_document_as_inert_json(buffer):
    html = webrtrace.render_html(_run_a_small_web(buffer))
    blob = re.search(r'id="webr-data">(.*?)</script>', html, re.S).group(1)
    # The payload round-trips as JSON, so it was data, not interpolated source.
    data = json.loads(blob.replace("<\\/", "</"))
    assert {n["name"] for n in data["nodes"]} == {"agent", "broken"}


def test_html_shows_tokens_and_failures(buffer):
    html = webrtrace.render_html(_run_a_small_web(buffer))
    assert "140" in html or "100" in html  # token totals rendered
    assert "failed" in html.lower()


def test_a_script_closing_tag_in_a_payload_cannot_break_out(buffer):
    @webR_node(name="x", capture=True)
    def x(prompt):
        return "safe"

    x("</script><script>alert(1)</script>")
    html = webrtrace.render_html(webrtrace.export_graph(buffer))
    # The raw closing tag must not appear unescaped inside the data island.
    island = re.search(r'id="webr-data">(.*?)</script>', html, re.S).group(1)
    assert "</script>" not in island


def test_write_html_creates_parent_directories(buffer, tmp_path):
    out = tmp_path / "nested" / "report.html"
    returned = webrtrace.write_html(out, _run_a_small_web(buffer))
    assert returned == out
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_an_empty_web_still_renders(buffer):
    html = webrtrace.render_html(webrtrace.export_graph(buffer))
    assert "<!doctype html>" in html
