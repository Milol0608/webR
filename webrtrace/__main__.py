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
import json
import sys
from pathlib import Path

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
    parser.add_argument("--json", action="store_true", help="emit the raw graph document")
    parser.add_argument(
        "--width", type=int, default=32, help="node name column width (default: 32)"
    )
    args = parser.parse_args(argv)

    if not args.path.exists():
        print(f"error: {args.path} does not exist", file=sys.stderr)
        return 2

    document = graph_from_jsonl(args.path)

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
