# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Read a trace from disk and print it.

    python -m webrtrace traces/run.jsonl
    python -m webrtrace traces/ --failures
    python -m webrtrace traces/run.jsonl --json > web.json

Accepts a file or a directory, so a run split across rotated files reassembles without
the caller having to think about it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from .collapse import collapse_by_agent
from .graph import graph_from_jsonl
from .render import render, render_failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m webrtrace",
        description="Render a webR trace file as a readable web.",
    )
    parser.add_argument("path", type=Path, help="a .jsonl trace file, or a directory of them")
    parser.add_argument(
        "--failures",
        action="store_true",
        help="show only the chains leading to failed or suspect nodes",
    )
    parser.add_argument(
        "--collapse",
        action="store_true",
        help="aggregate repeated invocations of an agent into a single node",
    )
    parser.add_argument("--json", action="store_true", help="emit the raw graph document")
    parser.add_argument(
        "--width", type=int, default=32, help="node name column width (default: 32)"
    )
    args = parser.parse_args(argv)

    if not args.path.exists():
        print(f"error: {args.path} does not exist", file=sys.stderr)
        return 2

    # A node named from a user's prompt can hold any character, and a Windows console on a
    # legacy code page raises UnicodeEncodeError on print(). Replacing unencodable
    # characters is strictly better than a traceback where the trace should have been.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(errors="replace")

    document = graph_from_jsonl(args.path)
    if args.collapse:
        # Failure chains are computed on the raw document below, since a collapsed node
        # has no single error to report.
        document = collapse_by_agent(document)

    if args.json:
        json.dump(document, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    elif args.failures:
        print(render_failures(document))
    else:
        print(render(document, name_width=args.width))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
