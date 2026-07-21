# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Data dependencies the call stack cannot see.

    python examples/04_links_across_a_queue.py

Two cases:

1. A producer and a consumer in the same process that never call each other.
2. A hand-off through a queue to a worker thread, where no shared context exists at all.

Without `link`, both look like unrelated roots. The web would be technically accurate and
completely useless -- it would show two separate runs and no reason to connect them.
"""

from __future__ import annotations

import queue
import threading

import webrtrace
from webrtrace import Link, webR_node

# --- case 1: same process, no call relationship -------------------------------------


@webR_node(name="planner")
def planner() -> list[str]:
    plan = ["fetch inventory", "reconcile totals", "write report"]
    # "This value came from me." Returns the value untouched.
    return webrtrace.mark(plan, "plan")


@webR_node(name="executor")
def executor(plan: list[str]) -> str:
    # "I consumed something somebody else produced." Records planner -> executor.
    webrtrace.link(plan)
    return f"executed {len(plan)} steps"


# --- case 2: across a queue, into a worker thread ------------------------------------


@webR_node(name="producer")
def producer(work: queue.Queue) -> None:
    payload = {"job": "reconcile", "rows": 1200}
    # A token is just data: trace id, node id, label. Serialize it and send it wherever
    # the payload goes -- a queue, an HTTP header, a database row.
    work.put({"payload": payload, "webr": webrtrace.origin("job").to_dict()})


@webR_node(name="consumer")
def consumer(message: dict) -> str:
    webrtrace.link(Link.from_dict(message["webr"]))
    return f"handled {message['payload']['job']}"


def drain(work: queue.Queue) -> None:
    consumer(work.get())


def main() -> None:
    # Case 1
    plan = planner()
    executor(plan)

    # Case 2 -- the consumer runs on a thread the producer's context never reaches.
    work: queue.Queue = queue.Queue()
    producer(work)
    thread = threading.Thread(target=drain, args=(work,))
    thread.start()
    thread.join()

    web = webrtrace.export_graph()
    print(webrtrace.render(web))

    print("\nWhat to notice:")
    print("  The call tree shows four separate roots -- structurally, nothing connects")
    print("  them. The SENDS edges are the only record that the data flowed at all.")
    print(f"\n  invokes edges: {web['stats']['invokes_edges']}")
    print(f"  sends edges:   {web['stats']['sends_edges']}")
    print("\nLinking is identity-based, never equality-based. An equal-but-distinct")
    print("list records no edge, because it is not the same datum:")

    @webR_node(name="impostor")
    def impostor() -> bool:
        return webrtrace.link(["fetch inventory", "reconcile totals", "write report"])

    print(f"  link(equal copy) -> {impostor()}")


if __name__ == "__main__":
    main()
