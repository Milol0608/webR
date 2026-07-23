# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""A self-contained HTML report of a web.

    webrtrace.write_html("report.html")

Open the file in a browser. No server, no build step, no network: everything -- the trace
data, the styling, the interactivity -- is inlined into one file you can email, attach to
a bug report, or check into a repository next to the failure it explains.

**No CDN links, no external fonts, no remote anything.** A report that phones out is a
report that renders blank on the air-gapped machine where the interesting failures happen,
and one that leaks the names of internal services into somebody's referer logs.

The data is embedded as JSON in a `<script type="application/json">` block rather than
interpolated into JavaScript source, so a node name containing `</script>` or a quote
cannot break out into executable code. Everything else is HTML-escaped on the way in.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .graph import export_graph

#: Token fields to total across the run, matching the terminal renderer.
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _totals(document: dict[str, Any]) -> dict[str, int]:
    """Sum token usage across every node that reported any."""
    totals = dict.fromkeys(_TOKEN_FIELDS, 0)
    calls = 0
    for node in document.get("nodes", []):
        usage = node.get("usage")
        if not usage:
            continue
        calls += 1
        for field in _TOKEN_FIELDS:
            value = usage.get(field)
            if isinstance(value, int):
                totals[field] += value
    totals["calls"] = calls
    totals["total"] = sum(totals[field] for field in _TOKEN_FIELDS)
    return totals


def render_html(document: dict[str, Any], *, title: str = "webR trace") -> str:
    """Render a graph document to a complete, standalone HTML page."""
    totals = _totals(document)
    counts = {
        "nodes": len(document.get("nodes", [])),
        "edges": len(document.get("edges", [])),
        "dropped": document.get("dropped", 0),
        "dangling": len(document.get("dangling_edges", []) or []),
    }
    for status in ("ok", "error", "suspect", "running"):
        counts[status] = sum(
            1 for node in document.get("nodes", []) if node.get("status") == status
        )
    counts["tainted"] = sum(1 for node in document.get("nodes", []) if node.get("tainted"))

    payload = json.dumps(document, separators=(",", ":"), default=str)
    # `</script>` inside a string would close the block early even inside valid JSON, so
    # the sequence is escaped in a way JSON.parse still reads back identically.
    payload = payload.replace("</", "<\\/")

    return _TEMPLATE.format(
        title=html.escape(title),
        data=payload,
        summary=_summary_html(counts, totals),
    )


def _summary_html(counts: dict[str, int], totals: dict[str, int]) -> str:
    cards = [
        ("nodes", counts["nodes"], ""),
        ("failed", counts["error"], "bad" if counts["error"] else ""),
        ("suspect", counts["suspect"], "warn" if counts["suspect"] else ""),
        ("tainted", counts["tainted"], "warn" if counts["tainted"] else ""),
    ]
    if counts["running"]:
        cards.append(("still running", counts["running"], "warn"))
    if totals["calls"]:
        cards.append(("model calls", totals["calls"], ""))
        cards.append(("tokens", f"{totals['total']:,}", ""))
    if counts["dropped"]:
        cards.append(("dropped", counts["dropped"], "warn"))

    out = []
    for label, value, kind in cards:
        out.append(
            f'<div class="card {kind}"><div class="n">{html.escape(str(value))}</div>'
            f'<div class="l">{html.escape(label)}</div></div>'
        )

    if totals["calls"]:
        breakdown = (
            f'<p class="note">Tokens: {totals["input_tokens"]:,} in, '
            f"{totals['output_tokens']:,} out, "
            f"{totals['cache_read_input_tokens']:,} cache read, "
            f"{totals['cache_creation_input_tokens']:,} cache write. "
            "Cache tokens are priced differently from ordinary input, so they are kept "
            "separate. webR reports tokens, never dollars &mdash; multiply by your own "
            "rates.</p>"
        )
        out.append(f'<div class="break">{breakdown}</div>')

    if counts["dropped"]:
        out.append(
            '<div class="break"><p class="note warn-text">This web is incomplete: '
            f"{counts['dropped']:,} node(s) were evicted before export. Ancestors of "
            "failures are pinned, so the chains below are intact, but overall counts "
            "understate the run.</p></div>"
        )
    return "\n".join(out)


def write_html(
    path: str | Path,
    document: dict[str, Any] | None = None,
    *,
    title: str = "webR trace",
) -> Path:
    """Write a standalone HTML report and return where it landed.

    Args:
        path: Destination file. Parent directories are created.
        document: A graph document; the in-memory buffer is exported when omitted.
        title: Page title, shown in the header.
    """
    document = export_graph() if document is None else document
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_html(document, title=title), encoding="utf-8")
    return destination


# The page itself. Braces are doubled because this is a `str.format` template -- only
# {title}, {data}, and {summary} are substituted.
_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --bg: #0f1115; --panel: #171a21; --line: #262b36; --text: #dfe3ea; --dim: #8b93a3;
  --ok: #4ea36b; --err: #d9534f; --sus: #d9a441; --taint: #7a6ad9; --link: #5aa9e6;
}}
@media (prefers-color-scheme: light) {{
  :root {{
    --bg: #f7f8fa; --panel: #fff; --line: #e2e5ea; --text: #1c2027; --dim: #6a7183;
    --ok: #2f7d4f; --err: #c0392b; --sus: #a97514; --taint: #5a49b8; --link: #1f6feb;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 24px; background: var(--bg); color: var(--text);
  font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}}
h1 {{ font-size: 20px; margin: 0 0 2px; }}
.sub {{ color: var(--dim); margin: 0 0 20px; }}
.cards {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 8px; }}
.card {{
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 10px 16px; min-width: 96px;
}}
.card .n {{ font-size: 22px; font-weight: 600; }}
.card .l {{ color: var(--dim); font-size: 12px; text-transform: uppercase;
            letter-spacing: .04em; }}
.card.bad .n {{ color: var(--err); }}
.card.warn .n {{ color: var(--sus); }}
.break {{ flex-basis: 100%; }}
.note {{ color: var(--dim); font-size: 13px; margin: 4px 0 0; max-width: 90ch; }}
.warn-text {{ color: var(--sus); }}
.controls {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
             margin: 18px 0 10px; }}
input[type=search] {{
  background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
  color: var(--text); padding: 7px 10px; min-width: 240px; font: inherit;
}}
label {{ color: var(--dim); display: flex; gap: 6px; align-items: center;
         cursor: pointer; user-select: none; }}
.tree {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
         padding: 6px; overflow-x: auto; }}
.row {{
  display: flex; align-items: baseline; gap: 10px; padding: 4px 8px;
  border-radius: 5px; white-space: nowrap; cursor: default;
  font-family: ui-monospace, "Cascadia Code", Menlo, Consolas, monospace;
}}
.row:hover {{ background: rgba(127,127,127,.10); }}
.row.hidden {{ display: none; }}
.twist {{ width: 14px; color: var(--dim); cursor: pointer; flex: none; }}
.twist.leaf {{ visibility: hidden; }}
.badge {{ flex: none; font-size: 11px; font-weight: 700; letter-spacing: .04em; }}
.ok .badge {{ color: var(--ok); }}
.error .badge {{ color: var(--err); }}
.suspect .badge {{ color: var(--sus); }}
.running .badge {{ color: var(--link); }}
.nm {{ font-weight: 600; }}
.error .nm {{ color: var(--err); }}
.suspect .nm {{ color: var(--sus); }}
.dur {{ color: var(--dim); }}
.taint {{ color: var(--taint); font-weight: 700; }}
.tok {{ color: var(--link); }}
.sig {{ color: var(--sus); }}
.msg {{ color: var(--err); overflow: hidden; text-overflow: ellipsis; }}
.detail {{
  margin: 2px 0 8px 34px; padding: 10px 12px; background: var(--bg);
  border: 1px solid var(--line); border-left: 3px solid var(--link);
  border-radius: 6px; white-space: pre-wrap; word-break: break-word;
  font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px;
}}
.detail.hidden {{ display: none; }}
.detail dt {{ color: var(--dim); margin-top: 6px; }}
.detail dd {{ margin: 0; }}
.empty {{ color: var(--dim); padding: 20px; text-align: center; }}
footer {{ color: var(--dim); font-size: 12px; margin-top: 22px; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="sub">Click a node for its payloads, tokens, and signals. Taint (&#9679;) means the
node succeeded but consumed something that did not.</p>
<div class="cards">{summary}</div>
<div class="controls">
  <input type="search" id="q" placeholder="filter by name, signal, or error&hellip;"
         autocomplete="off">
  <label><input type="checkbox" id="only"> only failures, suspects, and taint</label>
</div>
<div class="tree" id="tree"></div>
<footer>Generated by webR. Everything in this file is self-contained &mdash; no network
requests, no external assets.</footer>

<script type="application/json" id="webr-data">{data}</script>
<script>
(function () {{
  "use strict";
  var doc = JSON.parse(document.getElementById("webr-data").textContent);
  var nodes = doc.nodes || [];
  var tree = document.getElementById("tree");

  function duration(ns) {{
    if (ns == null) return "";
    if (ns < 1000) return ns + "ns";
    if (ns < 1e6) return (ns / 1e3).toFixed(1) + "us";
    if (ns < 1e9) return (ns / 1e6).toFixed(1) + "ms";
    return (ns / 1e9).toFixed(2) + "s";
  }}

  // Nodes whose parent is missing -- evicted, or in a rotated file that was not read --
  // are shown as roots. A tree that silently drops a subtree is worse than a detached one.
  var byId = {{}}, kids = {{}}, roots = [];
  nodes.forEach(function (n) {{ byId[n.node_id] = n; }});
  nodes.forEach(function (n) {{
    if (n.parent_id && byId[n.parent_id]) (kids[n.parent_id] = kids[n.parent_id] || []).push(n);
    else roots.push(n);
  }});

  var TOKENS = [
    ["input_tokens", "in"], ["output_tokens", "out"],
    ["cache_read_input_tokens", "cached"], ["cache_creation_input_tokens", "cache-write"]
  ];

  function tokenText(n) {{
    var u = n.usage; if (!u) return "";
    var parts = TOKENS.filter(function (f) {{ return u[f[0]]; }})
                      .map(function (f) {{ return f[1] + " " + u[f[0]].toLocaleString(); }});
    return parts.length ? "[" + parts.join(", ") + "]" : "";
  }}

  function signalText(n) {{
    var s = n.signals; if (!s) return "";
    return Object.keys(s).map(function (k) {{
      var v = s[k];
      if (v === true) return k;
      if (Array.isArray(v)) return k + "=" + v.slice(0, 3).join(",");
      return k + "=" + v;
    }}).join("  ");
  }}

  function el(tag, cls, text) {{
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;   // textContent, never innerHTML
    return e;
  }}

  function detailFor(n) {{
    var d = el("dl", "detail hidden");
    function add(label, value) {{
      if (value == null || value === "") return;
      d.appendChild(el("dt", null, label));
      d.appendChild(el("dd", null, value));
    }}
    add("node", n.node_id + (n.parent_id ? "  (child of " + n.parent_id + ")" : "  (root)"));
    add("trace", n.trace_id);
    if (n.usage) {{
      add("usage", Object.keys(n.usage).map(function (k) {{
        return k + ": " + n.usage[k];
      }}).join("\\n"));
    }}
    if (n.io && n.io.inputs) {{
      Object.keys(n.io.inputs).forEach(function (name) {{
        var p = n.io.inputs[name];
        add("input " + name + "  (" + p.len + " chars)", p.text != null ? p.text : "<not stored>");
      }});
    }}
    if (n.io && n.io.output) {{
      var o = n.io.output;
      add("output  (" + o.len + " chars)", o.text != null ? o.text : "<not stored>");
    }}
    if (n.signals) add("signals", JSON.stringify(n.signals, null, 2));
    if (n.attributes) add("attributes", JSON.stringify(n.attributes, null, 2));
    if (n.error) {{
      add("error", n.error.type + ": " + n.error.message);
      add("traceback", n.error.traceback);
    }}
    if (!d.childNodes.length) d.appendChild(el("dd", null, "(nothing else recorded)"));
    return d;
  }}

  var rows = [];

  function build(node, depth, parentRow) {{
    var children = kids[node.node_id] || [];
    var row = el("div", "row " + (node.status || "ok"));
    row.style.paddingLeft = (8 + Math.min(depth, 30) * 18) + "px";

    var tw = el("span", "twist" + (children.length ? "" : " leaf"),
                children.length ? "\\u25be" : "");
    row.appendChild(tw);
    row.appendChild(el("span", "badge", {{
      ok: "OK", error: "FAIL", suspect: "SUS", running: "RUN"
    }}[node.status] || "?"));
    if (node.tainted) row.appendChild(el("span", "taint", "\\u25cf"));
    row.appendChild(el("span", "nm", node.name || "<unnamed>"));
    if (depth > 30) row.appendChild(el("span", "dur", "+" + (depth - 30)));
    row.appendChild(el("span", "dur", duration(node.duration_ns)));

    var tok = tokenText(node);
    if (tok) row.appendChild(el("span", "tok", tok));
    var sig = signalText(node);
    if (sig) row.appendChild(el("span", "sig", sig));
    if (node.error) row.appendChild(el("span", "msg", node.error.type + ": " + node.error.message));

    var detail = detailFor(node);
    row.addEventListener("click", function (ev) {{
      if (ev.target === tw) return;
      detail.classList.toggle("hidden");
    }});

    var entry = {{
      row: row, detail: detail, node: node, depth: depth, parent: parentRow,
      collapsed: false, children: [],
      text: [node.name, sig, node.error ? node.error.type + " " + node.error.message : "",
             node.status].join(" ").toLowerCase(),
      interesting: node.status === "error" || node.status === "suspect" ||
                   node.status === "running" || !!node.tainted
    }};
    if (parentRow) parentRow.children.push(entry);
    rows.push(entry);
    tree.appendChild(row);
    tree.appendChild(detail);

    if (children.length) {{
      tw.addEventListener("click", function (ev) {{
        ev.stopPropagation();
        entry.collapsed = !entry.collapsed;
        tw.textContent = entry.collapsed ? "\\u25b8" : "\\u25be";
        apply();
      }});
    }}
    // Iterative ordering by seq keeps siblings in invocation order; recursion here is
    // bounded by real call depth, which the buffer already caps.
    children.sort(function (a, b) {{ return (a.seq || 0) - (b.seq || 0); }});
    children.forEach(function (c) {{ build(c, depth + 1, entry); }});
  }}

  roots.sort(function (a, b) {{ return (a.seq || 0) - (b.seq || 0); }});
  roots.forEach(function (r) {{ build(r, 0, null); }});

  if (!rows.length) tree.appendChild(el("div", "empty", "(empty web \\u2014 nothing was traced)"));

  var q = document.getElementById("q");
  var only = document.getElementById("only");

  function hiddenByAncestor(entry) {{
    for (var p = entry.parent; p; p = p.parent) if (p.collapsed) return true;
    return false;
  }}

  function apply() {{
    var needle = q.value.trim().toLowerCase();
    var failuresOnly = only.checked;
    // A matching node keeps its ancestors visible; a chain shown without its parents
    // tells you what broke but not what it was doing.
    var keep = {{}};
    rows.forEach(function (e) {{
      var match = (!needle || e.text.indexOf(needle) !== -1) &&
                  (!failuresOnly || e.interesting);
      if (!match) return;
      for (var p = e; p; p = p.parent) keep[p.node.node_id] = true;
    }});
    rows.forEach(function (e) {{
      var show = keep[e.node.node_id] && !hiddenByAncestor(e);
      e.row.classList.toggle("hidden", !show);
      if (!show) e.detail.classList.add("hidden");
    }});
  }}

  q.addEventListener("input", apply);
  only.addEventListener("change", apply);
  apply();
}})();
</script>
</body>
</html>
"""
