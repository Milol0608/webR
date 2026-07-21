# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Record semantics and the on-disk shape."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from conftest import make_record

from webr._ids import new_node_id, new_trace_id
from webr.records import (
    EdgeKind,
    EdgeRecord,
    ErrorInfo,
    NodeStatus,
    next_seq,
)


def test_ids_have_trace_context_widths():
    assert len(new_trace_id()) == 32
    assert len(new_node_id()) == 16
    int(new_trace_id(), 16)  # parses as hex


def test_ids_are_unique_across_many_draws():
    assert len({new_node_id() for _ in range(10_000)}) == 10_000


def test_seq_is_monotonic():
    first, second = next_seq(), next_seq()
    assert second > first


def test_records_are_immutable():
    # Records cross a thread boundary to the writer; mutation after handoff would be a
    # data race that silently corrupts the exported web.
    record = make_record("a")
    with pytest.raises(AttributeError):
        record.status = NodeStatus.ERROR  # type: ignore[misc]


@pytest.mark.parametrize(
    ("status", "tainted", "expected"),
    [
        (NodeStatus.OK, False, False),
        (NodeStatus.ERROR, False, True),
        (NodeStatus.SUSPECT, False, True),
        (NodeStatus.OK, True, True),
    ],
)
def test_is_interesting_drives_retention(status, tainted, expected):
    assert make_record("a", status=status, tainted=tainted).is_interesting is expected


def test_to_dict_omits_empty_fields_and_is_json_serializable():
    payload = make_record("a").to_dict()
    assert "error" not in payload
    assert "tainted" not in payload
    assert "io" not in payload
    assert json.loads(json.dumps(payload))["node_id"] == "a"


def test_to_dict_includes_error_when_present():
    record = replace(
        make_record("a", status=NodeStatus.ERROR),
        error=ErrorInfo("ValueError", "bad plan", "Traceback..."),
    )
    payload = record.to_dict()
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "ValueError"


def test_status_and_edge_kind_serialize_as_plain_strings():
    # str-enums keep JSONL lines readable without a custom encoder.
    assert json.dumps({"s": NodeStatus.SUSPECT.value}) == '{"s": "suspect"}'
    assert EdgeKind.SENDS.value == "sends"


def test_edge_record_round_trips():
    edge = EdgeRecord(
        trace_id="0" * 32, kind=EdgeKind.SENDS, src_id="a", dst_id="b", seq=next_seq()
    )
    payload = edge.to_dict()
    assert payload["kind"] == "sends"
    assert "label" not in payload
    assert json.loads(json.dumps(payload))["src_id"] == "a"
