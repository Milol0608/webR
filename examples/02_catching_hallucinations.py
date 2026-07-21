# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Every built-in detector, firing on a realistic failure.

    python examples/02_catching_hallucinations.py

None of these agents raise. Every call returns a plausible string, and a conventional
tracer would record six successes.
"""

from __future__ import annotations

import webrtrace
from webrtrace import webR_node

SOURCE = (
    "Q3 internal report. The team onboarded 1200 customers across 14 regions. "
    "Support handled 3400 tickets with a median response time of 2 hours."
)


@webR_node(name="fabricates_a_figure")
def fabricates_a_figure(source: str) -> str:
    # The single most common hallucination: a confident number from nowhere.
    return "In Q3 the team onboarded 1200 customers and generated 8400000 in revenue."


@webR_node(name="refuses_politely")
def refuses_politely(source: str) -> str:
    # Nothing failed. The pipeline continues, carrying an apology as if it were data.
    return "I'm sorry, I don't have access to the financial system to answer that."


@webR_node(name="returns_nothing")
def returns_nothing(source: str) -> str:
    return "   \n  "


@webR_node(name="breaks_its_format")
def breaks_its_format(source: str) -> str:
    # Asked for JSON, produced something that starts like JSON and is not.
    return '{"customers": 1200, "regions": 14,'


@webR_node(name="does_nothing")
def does_nothing(source: str) -> str:
    return source


@webR_node(name="gets_stuck")
def gets_stuck(source: str) -> str:
    return "the report says the report says " * 30


@webR_node(name="ignores_its_input")
def ignores_its_input(source: str) -> str:
    return " ".join(f"unrelated{i}" for i in range(80))


# A validator encodes domain knowledge no generic heuristic can have. Returning a string
# instead of False records *why* the output was rejected.
def wants_a_customer_count(output: str) -> bool | str:
    if "customers" in output:
        return True
    return "answer omitted the customer count"


@webR_node(name="checked_by_you", check=wants_a_customer_count)
def checked_by_you(source: str) -> str:
    return "The regions expanded considerably."


AGENTS = (
    fabricates_a_figure,
    refuses_politely,
    returns_nothing,
    breaks_its_format,
    does_nothing,
    gets_stuck,
    ignores_its_input,
    checked_by_you,
)


def main() -> None:
    for agent in AGENTS:
        agent(SOURCE)  # every one of these "succeeds"

    web = webrtrace.export_graph()
    print(webrtrace.render_summary(web))
    print()

    for node in web["nodes"]:
        signals = node.get("signals") or {}
        verdict = signals.get("suspect")
        mark = "SUSPECT" if verdict else "  ok   "
        detail = ", ".join(f"{k}={v}" for k, v in signals.items() if k != "length_ratio")
        print(f"{mark}  {node['name']:<22} {detail}")

    print("\nNote which ones webR refused to accuse:")
    print("  fabricates_a_figure reports novel_numbers but is NOT marked suspect --")
    print("  a node that computes a total is supposed to produce a new figure.")
    print("  Flagging it by default would train you to ignore the flags.")
    print("\nTo make that signal damning in your own pipeline:")
    print("  webrtrace.set_suspect_signals('refusal', 'empty_output', 'novel_numbers')")


if __name__ == "__main__":
    main()
