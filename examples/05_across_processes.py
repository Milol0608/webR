# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""One web across two processes.

    python examples/05_across_processes.py

Spawns a real worker process, passes the trace context to it, and reassembles both halves
into a single graph. The worker writes its own JSONL file and knows nothing about the
parent beyond one string.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import webrtrace
from webrtrace import webR_node

WORKER = """
import sys, webrtrace
from webrtrace import webR_node

webrtrace.start_writer(sys.argv[2])

@webR_node(name="worker.extract")
def extract(report):
    if not report.strip().startswith("{"):
        raise ValueError("could not parse JSON from model output")
    return report

@webR_node(name="worker.run")
def run(payload):
    return extract(payload)

# Everything inside this block joins the caller's trace.
with webrtrace.remote_parent({"traceparent": sys.argv[1]}):
    try:
        run("I don't have access to that database.")
    except ValueError:
        pass

webrtrace.stop_writer()
"""


@webR_node(name="orchestrator")
def orchestrator(worker_script: Path, traces: Path) -> None:
    # `inject()` reduces the current node to a W3C traceparent header. Send it wherever
    # the work goes -- argv here, but an HTTP header or a queue message works the same.
    carrier = webrtrace.inject()
    subprocess.run(
        [sys.executable, str(worker_script), carrier["traceparent"], str(traces / "worker.jsonl")],
        check=True,
        env={**__import__("os").environ, "PYTHONPATH": str(Path(webrtrace.__file__).parent.parent)},
    )


def main() -> None:
    workdir = Path(tempfile.mkdtemp())
    traces = workdir / "traces"
    traces.mkdir()
    script = workdir / "worker.py"
    script.write_text(WORKER, encoding="utf-8")

    webrtrace.start_writer(traces / "parent.jsonl")
    try:
        orchestrator(script, traces)
    finally:
        webrtrace.stop_writer()

    print("Reading only the parent's file:")
    parent_only = webrtrace.graph_from_jsonl(traces / "parent.jsonl")
    print(f"  {webrtrace.render_summary(parent_only)}")
    print("  the worker is simply absent -- nothing is wrong, we only read half the web\n")

    print("Reading both files together:")
    whole = webrtrace.graph_from_jsonl(traces)
    print(webrtrace.render(whole))

    print("\nThe failure chain crosses the process boundary:")
    print(webrtrace.render_failures(whole))

    print(f"\n  traces: {len(whole['traces'])}  (one trace id shared by both processes)")
    print(f"  dangling edges: {whole['stats']['dangling_edges']}")
    print(f"\nFiles written: {[p.name for p in sorted(traces.iterdir())]}")
    print(f"Read them yourself with:  python -m webrtrace {traces}")


if __name__ == "__main__":
    main()
