# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""The failure webR exists for: a pipeline that reports total success and is wrong.

    python examples/03_the_silent_failure.py

Five workers run concurrently. One hits a parse error, catches it, and substitutes a
fallback -- a completely reasonable thing for production code to do. The orchestrator
returns five results and zero exceptions. Every conventional tool records a clean run.

The answer is still wrong, and webR can say exactly where it started.
"""

from __future__ import annotations

import asyncio

import webrtrace
from webrtrace import webR_node

REPORTS = {
    0: '{"region": "north", "customers": 400}',
    1: '{"region": "south", "customers": 350}',
    2: '{"region": "east", "customers": 450}',
    3: "I don't have access to the east-2 regional database.",  # the poison
    4: '{"region": "west", "customers": 300}',
}


@webR_node(name="llm_call", attributes={"model": "simulated"})
async def llm_call(prompt: str, region: int) -> str:
    await asyncio.sleep(0.001)
    return REPORTS[region]


@webR_node(name="extract_customers", check=lambda out: out.strip().startswith("{"))
async def extract_customers(report: str) -> str:
    if not report.strip().startswith("{"):
        raise ValueError("could not parse JSON from model output")
    return report


@webR_node(name="worker")
async def worker(region: int) -> str:
    report = await llm_call(f"summarize region {region}", region)
    try:
        return await extract_customers(report)
    except ValueError:
        # Defensive code doing exactly what it was told to do. This is the line that
        # makes the failure silent -- and it is not a bug, it is a design choice.
        return '{"region": "unknown", "customers": 0}'


@webR_node(name="total_customers")
async def total_customers(results: str) -> str:
    return f"Total: {results.count('customers') * 300} customers"


@webR_node(name="orchestrator")
async def orchestrator() -> str:
    parts = await asyncio.gather(*(worker(i) for i in range(5)))
    return await total_customers(" ".join(parts))


def main() -> None:
    answer = asyncio.run(orchestrator())

    print("What your program saw:")
    print(f"  no exceptions raised, final answer = {answer!r}")
    print("  every worker returned. by any normal measure, a clean run.\n")

    web = webrtrace.export_graph()
    print("What webR saw:")
    print(webrtrace.render(web))

    print("\nThe chain that broke:")
    print(webrtrace.render_failures(web))

    print("\nHow to read the tree above:")
    print("  [ERR] the node that actually failed -- the origin")
    print("  [SUS] a node whose output looked wrong though nothing raised")
    print("  *     tainted: this node succeeded, but consumed something that did not")
    print("\nNote that `worker` and `orchestrator` are marked tainted even though they")
    print("returned normally. The exception was swallowed by your code -- it was not")
    print("swallowed by the web.")


if __name__ == "__main__":
    main()
