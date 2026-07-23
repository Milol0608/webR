# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Run the demo pipeline and write a trace report.

python -m demo --mode good
python -m demo --mode silent
python -m demo --mode fail
python -m demo --mode silent --open      # open the HTML report in a browser
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

import webrtrace

from .pipeline import Pipeline

MODES = ("good", "silent", "fail")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo", description=__doc__)
    parser.add_argument("--mode", choices=MODES, default="silent")
    parser.add_argument(
        "--profile",
        choices=sorted(webrtrace.PROFILES),
        default="data",
        help="suspicion policy; 'data' promotes all_zeros to suspect (default)",
    )
    parser.add_argument("--open", action="store_true", help="open the HTML report afterwards")
    parser.add_argument("--out", type=Path, default=None, help="where to write the HTML report")
    args = parser.parse_args(argv)

    # A fresh run each time, so repeated invocations do not accumulate into one web.
    webrtrace.reset()
    webrtrace.set_profile(args.profile)

    pipeline = Pipeline(args.mode)
    error: BaseException | None = None
    try:
        answer = pipeline.run()
    except BaseException as exc:  # `fail` mode raises; we still want the trace
        error = exc
        answer = None

    web = webrtrace.export_graph()
    out = args.out or Path("traces") / f"demo-{args.mode}.html"
    webrtrace.write_html(out, web, title=f"webR demo — {args.mode} mode")

    print(f"mode: {args.mode}   profile: {args.profile}")
    if answer is not None:
        print(f"program's answer: {answer!r}")
    if error is not None:
        print(f"program raised: {type(error).__name__}: {error}")
    print()
    print(webrtrace.render(web))

    failures = webrtrace.render_failures(web)
    if failures.strip():
        print("\nThe chain(s) that broke or looked wrong:")
        print(failures)

    print(f"\nHTML report: {out.resolve()}")
    _explain(args.mode)

    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


def _explain(mode: str) -> None:
    if mode == "good":
        print("\nA clean run. Every node OK, no taint, tokens accounted for. This is your")
        print("baseline: compare the other two modes against it.")
    elif mode == "silent":
        print("\nRead this one carefully. No exception was raised and every function")
        print("returned, yet the report is wrong. webR marks three things your program")
        print("could not see:")
        print("  - the refusal on T-1001: a billed call that returned nothing [SUS]")
        print("  - the truncated answer on T-1003, cut off at max_tokens [SUS]")
        print("  - taint (*) climbing from each to triage_report, because the final")
        print("    answer was built on them.")
        print("This is the failure webR exists for. Every other tool records a clean run.")
    else:
        print("\nAn ordinary loud failure: fetch_ticket raised, and the [ERR] marks the")
        print("origin. Easy mode -- any tool catches this. Shown so you can see it next")
        print("to a silent failure and tell them apart.")


if __name__ == "__main__":
    sys.exit(main())
