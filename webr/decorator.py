# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""`@webR_node` -- the entire user-facing instrumentation surface.

Design constraints, in priority order:

1. **Invisible to program behaviour.** Exceptions are recorded and re-raised unchanged,
   including `BaseException` subclasses such as `asyncio.CancelledError` -- a cancelled
   agent is a fact worth recording, not an error to swallow. Nothing here alters a return
   value, a signature, or a traceback.
2. **No I/O and no serialization on the hot path.** The wrapper reads a context variable,
   takes two clock samples, builds one frozen record, and appends it. Everything
   expensive happens later, on the writer thread.
3. **Shape decided once.** Whether a callable is sync, async, a generator, or an async
   generator is determined at *decoration* time. Inspecting the function on every call
   would be the most avoidable overhead imaginable.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import traceback
from collections.abc import Callable
from concurrent.futures import Executor, Future
from time import perf_counter_ns
from typing import Any, TypeVar

from . import runtime
from .propagation import NodeRef, get_propagator, new_root
from .records import ErrorInfo, NodeRecord, NodeStatus, next_seq, now_unix_ns

F = TypeVar("F", bound=Callable[..., Any])

#: Rendered tracebacks are capped so a deep recursive failure cannot produce a record
#: larger than the buffer it lives in.
MAX_TRACEBACK_CHARS = 8_192


def _error_info(exc: BaseException) -> ErrorInfo:
    """Render an exception to plain strings.

    The traceback is formatted here rather than stored as frame objects: holding frames
    would keep every local in the failing stack alive for as long as the record sits in
    the buffer, which is a memory leak wearing a very convincing disguise.
    """
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(rendered) > MAX_TRACEBACK_CHARS:
        head = MAX_TRACEBACK_CHARS // 2
        tail = MAX_TRACEBACK_CHARS - head
        rendered = f"{rendered[:head]}\n...[truncated]...\n{rendered[-tail:]}"
    return ErrorInfo(type=type(exc).__name__, message=str(exc), traceback=rendered)


def _open(name: str) -> NodeRef:
    """Derive a node from whatever is currently executing, without making it current."""
    parent = get_propagator().current()
    return parent.child(name) if parent is not None else new_root(name)


def _finish(
    ref: NodeRef,
    token: Any | None,
    started_unix_ns: int,
    started_ns: int,
    exc: BaseException | None,
    attributes: dict[str, Any] | None,
) -> None:
    """Close a node: stop the clock, restore context, record, and pin if it failed.

    `token` is None for the generator wrappers, which attach and detach around each
    resumption instead of holding the context for the node's whole lifetime.
    """
    duration_ns = perf_counter_ns() - started_ns
    if token is not None:
        get_propagator().detach(token)

    buffer = runtime.get_buffer()
    if exc is None:
        status, error = NodeStatus.OK, None
    else:
        status, error = NodeStatus.ERROR, _error_info(exc)
        # The ancestor chain is the causal story -- "orchestrator -> planner -> boom".
        # Pin it now: those parents are still executing and do not exist in the buffer
        # yet, so this is the only moment their ids are knowable.
        buffer.pin(ref.chain_ids())

    parent = ref.parent
    buffer.append(
        NodeRecord(
            trace_id=ref.trace_id,
            node_id=ref.node_id,
            parent_id=parent.node_id if parent is not None else None,
            name=ref.name,
            seq=next_seq(),
            status=status,
            started_unix_ns=started_unix_ns,
            duration_ns=duration_ns,
            depth=ref.depth,
            error=error,
            attributes=attributes if attributes is not None else {},
        )
    )


def webR_node(
    fn: F | None = None,
    /,
    *,
    name: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> F | Callable[[F], F]:
    """Trace every call to this callable as a node in the web.

    Usable bare or with arguments::

        @webR_node
        async def planner(task: str) -> str: ...

        @webR_node(name="planner.v2", attributes={"model": "opus-4.8"})
        async def planner_v2(task: str) -> str: ...

    Args:
        name: Node name in the web. Defaults to the callable's qualified name.
        attributes: Static metadata attached to every record this callable produces.
            Copied once at decoration time and then shared by reference across records,
            which is safe because records are frozen and webR never mutates it.
    """
    # Copied once here, not per call: the caller's dict must not be able to mutate
    # records that have already been handed to the buffer.
    static_attributes = dict(attributes) if attributes else None

    def decorate(func: F) -> F:
        node_name = name or getattr(func, "__qualname__", None) or repr(func)

        # An async generator function is not a coroutine function, so it must be tested
        # first; otherwise it would fall through to the wrong wrapper.
        if inspect.isasyncgenfunction(func):
            wrapper = _wrap_async_generator(func, node_name, static_attributes)
        elif inspect.iscoroutinefunction(func):
            wrapper = _wrap_async(func, node_name, static_attributes)
        elif inspect.isgeneratorfunction(func):
            wrapper = _wrap_generator(func, node_name, static_attributes)
        else:
            wrapper = _wrap_sync(func, node_name, static_attributes)

        wrapper.__webr_node__ = True
        wrapper.__webr_name__ = node_name
        return wrapper  # type: ignore[return-value]

    if fn is None:
        return decorate
    return decorate(fn)


def _wrap_sync(func: Any, node_name: str, attributes: dict[str, Any] | None) -> Any:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not runtime.enabled:
            return func(*args, **kwargs)
        ref = _open(node_name)
        token = get_propagator().attach(ref)
        started_unix, started = now_unix_ns(), perf_counter_ns()
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            _finish(ref, token, started_unix, started, exc, attributes)
            raise
        _finish(ref, token, started_unix, started, None, attributes)
        return result

    return wrapper


def _wrap_async(func: Any, node_name: str, attributes: dict[str, Any] | None) -> Any:
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not runtime.enabled:
            return await func(*args, **kwargs)
        ref = _open(node_name)
        token = get_propagator().attach(ref)
        started_unix, started = now_unix_ns(), perf_counter_ns()
        try:
            result = await func(*args, **kwargs)
        except BaseException as exc:
            # Includes CancelledError: an agent killed by a timeout is exactly the kind
            # of silent disappearance this library exists to make visible.
            _finish(ref, token, started_unix, started, exc, attributes)
            raise
        _finish(ref, token, started_unix, started, None, attributes)
        return result

    return wrapper


def _wrap_generator(func: Any, node_name: str, attributes: dict[str, Any] | None) -> Any:
    """Trace a generator across its whole lifetime, not just the call that creates it.

    Calling a generator function runs no user code -- the body executes on each `next()`.
    So the node spans from first resumption to exhaustion, and the context is attached
    around every resumption so that calls made *inside* the body see this node as their
    parent rather than whoever happened to be iterating.

    `GeneratorExit` is recorded as success, not failure. A consumer that `break`s out of
    a loop has abandoned the generator, which is ordinary control flow; reporting it as
    an error would fill the web with failures that never happened.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not runtime.enabled:
            # `yield from` forwards send() and throw() correctly, so the untraced path
            # behaves identically to the traced one.
            yield from func(*args, **kwargs)
            return

        propagator = get_propagator()
        iterator = func(*args, **kwargs)
        ref = _open(node_name)
        started_unix, started = now_unix_ns(), perf_counter_ns()
        sent: Any = None
        try:
            while True:
                token = propagator.attach(ref)
                try:
                    item = iterator.send(sent)
                except StopIteration:
                    break
                finally:
                    propagator.detach(token)
                sent = yield item
        except GeneratorExit:
            iterator.close()
            _finish(ref, None, started_unix, started, None, attributes)
            raise
        except BaseException as exc:
            iterator.close()
            _finish(ref, None, started_unix, started, exc, attributes)
            raise
        _finish(ref, None, started_unix, started, None, attributes)

    return wrapper


def _wrap_async_generator(func: Any, node_name: str, attributes: dict[str, Any] | None) -> Any:
    """Async-generator counterpart of `_wrap_generator`, with the same semantics."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        propagator = get_propagator()
        iterator = func(*args, **kwargs)
        tracing = runtime.enabled
        ref = _open(node_name) if tracing else None
        started_unix, started = now_unix_ns(), perf_counter_ns()
        sent: Any = None
        try:
            while True:
                # `async for` cannot forward asend(), so the loop is written out even
                # when tracing is off, keeping both paths behaviourally identical.
                token = propagator.attach(ref) if ref is not None else None
                try:
                    item = await iterator.asend(sent)
                except StopAsyncIteration:
                    break
                finally:
                    if token is not None:
                        propagator.detach(token)
                sent = yield item
        except GeneratorExit:
            await iterator.aclose()
            if ref is not None:
                _finish(ref, None, started_unix, started, None, attributes)
            raise
        except BaseException as exc:
            await iterator.aclose()
            if ref is not None:
                _finish(ref, None, started_unix, started, exc, attributes)
            raise
        if ref is not None:
            _finish(ref, None, started_unix, started, None, attributes)

    return wrapper


def submit(executor: Executor, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
    """`executor.submit(fn, ...)` that carries the active node into the worker thread.

    `asyncio.to_thread` already copies the context, so it needs no help. Raw threads and
    `ThreadPoolExecutor.submit` do not: work handed to them would otherwise start a fresh
    trace, and the web would quietly claim those nodes had no caller.
    """
    context = contextvars.copy_context()
    return executor.submit(context.run, functools.partial(fn, *args, **kwargs))
