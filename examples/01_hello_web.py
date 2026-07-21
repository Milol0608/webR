# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""The smallest useful web: three agents, one call chain.

    python examples/01_hello_web.py

Shows what `@webR_node` records with no configuration at all.
"""

from __future__ import annotations

import webrtrace
from webrtrace import webR_node


# Stand-ins for real agents. Nothing here touches a network, so the example runs
# anywhere, deterministically, with no API key.
@webR_node
def fetch_sources(topic: str) -> str:
    return f"Three papers on {topic}, published in 2019, 2021 and 2024."


@webR_node
def summarize(sources: str) -> str:
    return f"Summary: {sources.split(',')[0]}."


@webR_node
def format_answer(summary: str) -> str:
    return f"## Result\n\n{summary}"


@webR_node
def pipeline(topic: str) -> str:
    return format_answer(summarize(fetch_sources(topic)))


def main() -> None:
    pipeline("sea otter tool use")

    web = webrtrace.export_graph()
    print(webrtrace.render(web))

    print("\n--- what one node looks like underneath ---")
    node = next(n for n in web["nodes"] if n["name"].endswith("summarize"))
    for key in ("name", "status", "depth", "duration_ns"):
        print(f"  {key}: {node[key]}")
    # `io` holds a bounded fingerprint of the payloads, not the payloads themselves.
    print(f"  input fingerprint: {node['io']['inputs']['sources']}")
    print(f"  output fingerprint: {node['io']['output']}")


if __name__ == "__main__":
    main()
