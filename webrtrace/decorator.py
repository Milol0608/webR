# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""`@webR_node` -- the entire user-facing instrumentation surface.

Design constraints, in priority order:

1. **Invisible to program behaviour.** Exceptions are recorded and re-raised unchanged,
   including `BaseException` subclasses such as `asyncio.CancelledError` -- a cancelled
   agent is a fact worth recording, not an error to swallow. A failing validator does not
   raise. Nothing here alters a return value, a signature, or a traceback.
2. **Everything decided once.** Whether a callable is sync, async, a generator, or an
   async generator, what its parameters are called, and what it captures are all resolved
   at *decoration* time. Re-deriving any of that per call would be pure waste.
3. **Bounded work per call.** A context lookup, two clock samples, one pass over any
   string payload (ADR 0002), one frozen record, one append. No I/O, no locks held across
   user code.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import traceback
from collections.abc import Callable
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from time import perf_counter_ns
from typing import Any, TypeVar

from . import runtime
from .detectors import is_suspect, run_detectors
from .fingerprint import as_text, fingerprint
from .propagation import NodeRef, get_propagator, new_root
from .records import ErrorInfo, NodeRecord, NodeStatus, next_seq, now_unix_ns

F = TypeVar("F", bound=Callable[..., Any])

#: Sentinel distinguishing "returned None" from "never produced a value".
_NO_RESULT = object()

#: Rendered tracebacks are capped so a deep recursive failure cannot produce a record
#: larger than the buffer it lives in.
MAX_TRACEBACK_CHARS = 8_192


@dataclass(frozen=True, slots=True)
class _Spec:
    """Everything decided once, at decoration time, so no call pays to rediscover it."""

    name: str
    attributes: dict[str, Any] | None
    capture: bool | tuple[str, ...] | None
    capture_full: bool | None
    check: Callable[[Any], Any] | None
    param_names: tuple[str, ...]

    def wants_capture(self) -> bool:
        """Per-node setting wins; otherwise follow the process-wide default."""
        return runtime.capture if self.capture is None else bool(self.capture)

    def wants_full(self) -> bool:
        return runtime.capture_full if self.capture_full is None else self.capture_full


def _error_info(exc: BaseException) -> ErrorInfo:
    """Render an exception to plain strings.

    Every step here is defended, because both `str(exc)` and `format_exception` execute
    user code. An exception class with a lazy `__str__` -- common in ORM and RPC layers,
    where the message is built on demand -- can raise while being rendered. If that
    escaped, webR would turn a recoverable error in the traced program into a different,
    unrecoverable one, which is the single thing this library must never do.

    The traceback is formatted rather than stored as frame objects: holding frames would
    keep every local in the failing stack alive for as long as the record sits in the
    buffer, which is a memory leak wearing a very convincing disguise.
    """
    try:
        name = type(exc).__name__
    except Exception:
        name = "<unknown>"

    try:
        message = str(exc)
    except Exception as render_failure:
        message = f"<{name}.__str__ raised {type(render_failure).__name__}>"

    try:
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        # format_exception calls str() on the exception too, so it can fail the same way.
        rendered = None

    if rendered is not None and len(rendered) > MAX_TRACEBACK_CHARS:
        head = MAX_TRACEBACK_CHARS // 2
        tail = MAX_TRACEBACK_CHARS - head
        rendered = f"{rendered[:head]}\n...[truncated]...\n{rendered[-tail:]}"

    return ErrorInfo(type=name, message=message, traceback=rendered)


def _collect_inputs(spec: _Spec, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, str]:
    """Map string-valued arguments to their parameter names.

    Parameter names are zipped against positional arguments rather than resolved through
    `inspect.signature().bind()`, which costs tens of microseconds per call -- more than
    everything else in the wrapper combined.
    """
    selected = spec.capture if isinstance(spec.capture, tuple) else None
    inputs: dict[str, str] = {}

    # strict=False is deliberate: a call may pass fewer positional arguments than the
    # signature declares (defaults), or more (*args), and neither is webR's business.
    for name, value in zip(spec.param_names, args, strict=False):
        if selected is not None and name not in selected:
            continue
        text = as_text(value)
        if text is not None:
            inputs[name] = text

    for name, value in kwargs.items():
        if selected is not None and name not in selected:
            continue
        text = as_text(value)
        if text is not None:
            inputs[name] = text

    return inputs


def _validate(spec: _Spec, result: Any) -> str | None:
    """Run the user's validator. Returns a reason if the output looks wrong.

    A failing check never raises. That is the entire point: the call *succeeded*, the
    value is being returned to the caller, and webR's job is to note that it looks wrong
    without changing what the program does.
    """
    if spec.check is None:
        return None
    try:
        verdict = spec.check(result)
    except Exception as exc:  # a broken validator must not break the traced run
        return f"check raised {type(exc).__name__}: {exc}"
    if verdict is True:
        return None
    if isinstance(verdict, str):
        return verdict
    if not verdict:
        return "check returned a falsy value"
    return None


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
    spec: _Spec,
    inputs: dict[str, str] | None = None,
    result: Any = _NO_RESULT,
) -> None:
    """Close a node: stop the clock, analyse, record, and propagate failure upward.

    `token` is None for the generator wrappers, which attach and detach around each
    resumption rather than holding the context for the node's whole lifetime.
    """
    duration_ns = perf_counter_ns() - started_ns
    if token is not None:
        get_propagator().detach(token)

    io: dict[str, Any] | None = None
    signals: dict[str, Any] = {}

    if inputs is not None:
        full = spec.wants_full()
        output_text = as_text(result) if result is not _NO_RESULT else None
        if inputs or output_text is not None:
            io = {}
            if inputs:
                io["inputs"] = {name: fingerprint(text, full=full) for name, text in inputs.items()}
            if output_text is not None:
                io["output"] = fingerprint(output_text, full=full)
            signals = run_detectors(inputs, output_text, runtime.detectors)

    # A validator's verdict outranks a heuristic: the user knows what correct looks like.
    reason = _validate(spec, result) if exc is None and result is not _NO_RESULT else None
    if reason is None and signals:
        reason = is_suspect(signals, runtime.suspect_signals)

    if exc is not None:
        status: NodeStatus = NodeStatus.ERROR
        error = _error_info(exc)
    elif reason is not None:
        status, error = NodeStatus.SUSPECT, None
        signals["suspect"] = reason
    else:
        status, error = NodeStatus.OK, None

    buffer = runtime.get_buffer()
    if status is not NodeStatus.OK:
        # Everything above consumed this node's output, so it is downstream of a problem.
        ref.taint_ancestors()
        # Pin the causal chain now: those parents are still executing and do not exist in
        # the buffer yet, so this is the only moment their ids are knowable.
        buffer.pin(ref.chain_ids())

    parent = ref.parent
    runtime.emit(
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
            tainted=ref.state.tainted,
            attributes=spec.attributes if spec.attributes is not None else {},
            io=io,
            signals=signals or None,
        )
    )


def webR_node(
    fn: F | None = None,
    /,
    *,
    name: str | None = None,
    attributes: dict[str, Any] | None = None,
    capture: bool | tuple[str, ...] | None = None,
    capture_full: bool | None = None,
    check: Callable[[Any], Any] | None = None,
) -> F | Callable[[F], F]:
    """Trace every call to this callable as a node in the web.

    Usable bare or with arguments::

        @webR_node
        async def planner(task: str) -> str: ...

        @webR_node(capture=("prompt",), check=lambda out: out.strip().startswith("{"))
        async def extractor(prompt: str, retries: int = 0) -> str: ...

    Args:
        name: Node name in the web. Defaults to the callable's qualified name.
        attributes: Static metadata attached to every record this callable produces.
            Copied once at decoration time and then shared by reference across records,
            which is safe because records are frozen and webR never mutates it.
        capture: Whether to fingerprint string payloads. `None` follows the process-wide
            setting, `False` disables it for this node, `True` captures every string
            argument, and a tuple of parameter names narrows it to those.
        capture_full: Store payload text rather than a fingerprint. `None` follows the
            process-wide setting. Bounded by `fingerprint.MAX_FULL_CHARS`, but still the
            setting most likely to turn a trace file into a liability -- prompts contain
            customer data.
        check: A validator run on the return value. Return `True` to pass; return `False`,
            `None`, or a string reason to mark the node **suspect**. It never raises and
            never changes the returned value: a hallucination is a call that succeeded.
    """
    # Attributes are copied once here, not per call: the caller's dict must not be able
    # to mutate records that have already been handed to the buffer.
    static_attributes = dict(attributes) if attributes else None

    def decorate(func: F) -> F:
        node_name = name or getattr(func, "__qualname__", None) or repr(func)
        spec = _Spec(
            name=node_name,
            attributes=static_attributes,
            capture=capture,
            capture_full=capture_full,
            check=check,
            param_names=_parameter_names(func),
        )

        # An async generator function is not a coroutine function, so it must be tested
        # first; otherwise it would fall through to the wrong wrapper.
        if inspect.isasyncgenfunction(func):
            wrapper = _wrap_async_generator(func, spec)
        elif inspect.iscoroutinefunction(func):
            wrapper = _wrap_async(func, spec)
        elif inspect.isgeneratorfunction(func):
            wrapper = _wrap_generator(func, spec)
        else:
            wrapper = _wrap_sync(func, spec)

        wrapper.__webr_node__ = True
        wrapper.__webr_name__ = node_name
        return wrapper  # type: ignore[return-value]

    if fn is None:
        return decorate
    return decorate(fn)


def _parameter_names(func: Any) -> tuple[str, ...]:
    """Positional parameter names, resolved once.

    A callable that refuses introspection -- some builtins and C extensions do -- simply
    gets no named inputs rather than breaking decoration.
    """
    try:
        return tuple(inspect.signature(func).parameters)
    except (TypeError, ValueError):
        return ()


def _wrap_sync(func: Any, spec: _Spec) -> Any:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not runtime.enabled:
            return func(*args, **kwargs)
        inputs = _collect_inputs(spec, args, kwargs) if spec.wants_capture() else None
        ref = _open(spec.name)
        token = get_propagator().attach(ref)
        started_unix, started = now_unix_ns(), perf_counter_ns()
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            _finish(ref, token, started_unix, started, exc, spec, inputs)
            raise
        _finish(ref, token, started_unix, started, None, spec, inputs, result)
        return result

    return wrapper


def _wrap_async(func: Any, spec: _Spec) -> Any:
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not runtime.enabled:
            return await func(*args, **kwargs)
        inputs = _collect_inputs(spec, args, kwargs) if spec.wants_capture() else None
        ref = _open(spec.name)
        token = get_propagator().attach(ref)
        started_unix, started = now_unix_ns(), perf_counter_ns()
        try:
            result = await func(*args, **kwargs)
        except BaseException as exc:
            # Includes CancelledError: an agent killed by a timeout is exactly the kind
            # of silent disappearance this library exists to make visible.
            _finish(ref, token, started_unix, started, exc, spec, inputs)
            raise
        _finish(ref, token, started_unix, started, None, spec, inputs, result)
        return result

    return wrapper


def _wrap_generator(func: Any, spec: _Spec) -> Any:
    """Trace a generator across its whole lifetime, not just the call that creates it.

    Calling a generator function runs no user code -- the body executes on each `next()`.
    So the node spans from first resumption to exhaustion, and the context is attached
    around every resumption so that calls made *inside* the body see this node as their
    parent rather than whoever happened to be iterating.

    Only inputs are captured. A generator has no single return value to fingerprint, and
    accumulating every yielded item would reintroduce the unbounded retention the whole
    design avoids.

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
        inputs = _collect_inputs(spec, args, kwargs) if spec.wants_capture() else None
        iterator = func(*args, **kwargs)
        ref = _open(spec.name)
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
            _finish(ref, None, started_unix, started, None, spec, inputs)
            raise
        except BaseException as exc:
            iterator.close()
            _finish(ref, None, started_unix, started, exc, spec, inputs)
            raise
        _finish(ref, None, started_unix, started, None, spec, inputs)

    return wrapper


def _wrap_async_generator(func: Any, spec: _Spec) -> Any:
    """Async-generator counterpart of `_wrap_generator`, with the same semantics."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        propagator = get_propagator()
        tracing = runtime.enabled
        inputs = _collect_inputs(spec, args, kwargs) if tracing and spec.wants_capture() else None
        iterator = func(*args, **kwargs)
        ref = _open(spec.name) if tracing else None
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
                _finish(ref, None, started_unix, started, None, spec, inputs)
            raise
        except BaseException as exc:
            await iterator.aclose()
            if ref is not None:
                _finish(ref, None, started_unix, started, exc, spec, inputs)
            raise
        if ref is not None:
            _finish(ref, None, started_unix, started, None, spec, inputs)

    return wrapper


def submit(executor: Executor, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
    """`executor.submit(fn, ...)` that carries the active node into the worker thread.

    `asyncio.to_thread` already copies the context, so it needs no help. Raw threads and
    `ThreadPoolExecutor.submit` do not: work handed to them would otherwise start a fresh
    trace, and the web would quietly claim those nodes had no caller.
    """
    context = contextvars.copy_context()
    return executor.submit(context.run, functools.partial(fn, *args, **kwargs))
