# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Parent attribution across sync, nested, and concurrent execution.

These are the tests that matter most in milestone 1: every claim webR makes about *who
caused what* rests on the propagator returning the right parent under concurrency.
"""

from __future__ import annotations

import asyncio

from webr.propagation import (
    ContextVarPropagator,
    NodeRef,
    Propagator,
    get_propagator,
    new_root,
    set_propagator,
)


def test_default_propagator_satisfies_the_protocol():
    assert isinstance(get_propagator(), Propagator)


def test_no_current_node_outside_any_traced_call():
    assert ContextVarPropagator().current() is None


def test_attach_and_detach_restore_the_previous_node():
    prop = ContextVarPropagator()
    root = new_root("orchestrator")
    token = prop.attach(root)
    assert prop.current() is root
    prop.detach(token)
    assert prop.current() is None


def test_nested_attach_unwinds_in_order():
    prop = ContextVarPropagator()
    root = new_root("orchestrator")
    t1 = prop.attach(root)
    child = root.child("planner")
    t2 = prop.attach(child)

    assert prop.current() is child
    prop.detach(t2)
    assert prop.current() is root
    prop.detach(t1)
    assert prop.current() is None


def test_child_inherits_trace_and_increments_depth():
    root = new_root("orchestrator")
    child = root.child("planner")
    grandchild = child.child("extractor")

    assert child.trace_id == root.trace_id == grandchild.trace_id
    assert (root.depth, child.depth, grandchild.depth) == (0, 1, 2)
    assert child.parent is root


def test_separate_roots_get_separate_traces():
    assert new_root("a").trace_id != new_root("b").trace_id


def test_ancestor_chain_walks_to_the_root_nearest_first():
    root = new_root("orchestrator")
    child = root.child("planner")
    grandchild = child.child("extractor")

    assert grandchild.ancestor_ids() == (child.node_id, root.node_id)
    assert grandchild.chain_ids() == (grandchild.node_id, child.node_id, root.node_id)
    assert root.ancestor_ids() == ()


def test_node_refs_are_immutable():
    root = new_root("orchestrator")
    try:
        root.name = "renamed"  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("NodeRef must be frozen")


def test_asyncio_fanout_attributes_every_worker_to_the_same_parent():
    # The core promise of contextvar propagation: gather() copies the context per task,
    # so a fan-out of agents records the orchestrator as parent with no user cooperation.
    prop = ContextVarPropagator()
    seen: dict[int, NodeRef | None] = {}

    async def worker(i: int) -> None:
        await asyncio.sleep(0)
        seen[i] = prop.current()

    async def orchestrate() -> NodeRef:
        root = new_root("orchestrator")
        token = prop.attach(root)
        try:
            await asyncio.gather(*(worker(i) for i in range(5)))
        finally:
            prop.detach(token)
        return root

    root = asyncio.run(orchestrate())
    assert {ref.node_id for ref in seen.values() if ref} == {root.node_id}
    assert len(seen) == 5


def test_concurrent_tasks_do_not_leak_context_into_each_other():
    # Two agents running concurrently must not see each other's node, or the web would
    # invent edges that never existed.
    prop = ContextVarPropagator()
    observed: dict[str, str | None] = {}

    async def agent(name: str) -> None:
        root = new_root(name)
        token = prop.attach(root)
        try:
            await asyncio.sleep(0)  # force a suspension point between the two tasks
            current = prop.current()
            observed[name] = current.name if current else None
        finally:
            prop.detach(token)

    async def main() -> None:
        await asyncio.gather(agent("alpha"), agent("beta"))

    asyncio.run(main())
    assert observed == {"alpha": "alpha", "beta": "beta"}


def test_child_task_mutation_does_not_escape_to_the_parent():
    # A task gets a *copy* of the context, so a node attached inside a child task must
    # not become the parent's current node after that task finishes.
    prop = ContextVarPropagator()

    async def main() -> NodeRef | None:
        root = new_root("orchestrator")
        token = prop.attach(root)
        try:

            async def child() -> None:
                prop.attach(root.child("planner"))  # deliberately never detached

            await asyncio.create_task(child())
            return prop.current()
        finally:
            prop.detach(token)

    assert asyncio.run(main()).name == "orchestrator"


def test_propagator_can_be_swapped():
    class NullPropagator:
        def current(self) -> NodeRef | None:
            return None

        def attach(self, ref: NodeRef) -> object:
            return object()

        def detach(self, token: object) -> None:
            pass

    original = get_propagator()
    try:
        set_propagator(NullPropagator())
        assert get_propagator().current() is None
    finally:
        set_propagator(original)
    assert isinstance(get_propagator(), ContextVarPropagator)
