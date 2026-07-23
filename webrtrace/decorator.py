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
import logging
import traceback
from collections.abc import Callable
from concurrent.futures import Executor, Future
from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter_ns
from typing import Any, TypeVar

from . import redaction, runtime
from .detectors import (
    is_suspect,
    run_detectors,
    run_value_detectors,
    strip_payload_values,
)
from .fingerprint import as_text, fingerprint
from .propagation import NodeRef, get_propagator, new_root
from .records import ErrorInfo, NodeOpen, NodeRecord, NodeStatus, now_unix_ns

#: Never configured with a handler here -- that is the application's decision, and a
#: library that installs handlers hijacks output it does not own.
logger = logging.getLogger("webrtrace")

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
    capture_text: bool | None
    check: Callable[[Any], Any] | None
    redactor: redaction.Redactor | None
    param_names: tuple[str, ...]

    def wants_capture(self) -> bool:
        """Per-node setting wins; otherwise follow the process-wide default."""
        return runtime.capture if self.capture is None else bool(self.capture)

    def wants_full(self) -> bool:
        return runtime.capture_full if self.capture_full is None else self.capture_full

    def wants_text(self) -> bool:
        return runtime.capture_text if self.capture_text is None else self.capture_text


#: Innermost stack frames kept when rendering a traceback. The frames nearest the raise
#: are the ones worth having; a negative limit is what selects them.
MAX_TRACEBACK_FRAMES = 40

# The exception most recently rendered in this context. As one propagates up through many
# traced frames, only the innermost renders it; the rest recognise it here and skip.
#
# A ContextVar rather than a set keyed on the exception: exceptions are **not**
# weak-referenceable in CPython, so a WeakSet silently degrades to "always render" (which
# is exactly the bug this replaced -- the optimisation looked correct and did nothing).
# Identity comparison against a live object avoids both the id-reuse hazard of keying on
# `id()` and the intrusion of setting an attribute on the user's exception. It retains one
# exception per context, replaced by the next -- no more than `except` blocks already hold,
# and per-task, so concurrent agents never interfere.
_last_rendered: ContextVar[BaseException | None] = ContextVar(
    "webrtrace_last_rendered_exception", default=None
)


def _first_sighting(exc: BaseException) -> bool:
    """Whether this traced frame is the innermost one to see this exception."""
    if _last_rendered.get() is exc:
        return False
    _last_rendered.set(exc)
    return True


def _error_info(exc: BaseException) -> ErrorInfo:
    """Render an exception to plain strings.

    Every step here is defended, because both `str(exc)` and `format_exception` execute
    user code. An exception class with a lazy `__str__` -- common in ORM and RPC layers,
    where the message is built on demand -- can raise while being rendered. If that
    escaped, webR would turn a recoverable error in the traced program into a different,
    unrecoverable one, which is the single thing this library must never do.

    **The traceback is rendered once, by the innermost traced frame to see the
    exception.** As it propagates up, each ancestor records the type and message but not
    the traceback. Rendering at every level made a deep failure quadratic -- a depth-2000
    failure took over two minutes, on precisely the path this library exists to record --
    and it duplicated the same text onto every node in the chain, since the frames a
    bounded render keeps are the innermost ones either way.

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

    if not _first_sighting(exc):
        return ErrorInfo(type=name, message=message, traceback=None)

    try:
        rendered = "".join(
            traceback.format_exception(
                type(exc), exc, exc.__traceback__, limit=-MAX_TRACEBACK_FRAMES
            )
        )
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


def _collect_raw_inputs(
    spec: _Spec, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Argument values by parameter name, unconverted.

    The text path deliberately keeps only `str`/`bytes`; the value detectors need what was
    actually passed -- the array, the record, the score dict.
    """
    selected = spec.capture if isinstance(spec.capture, tuple) else None
    raw: dict[str, Any] = {}
    for name, value in zip(spec.param_names, args, strict=False):
        if selected is None or name in selected:
            raw[name] = value
    for name, value in kwargs.items():
        if selected is None or name in selected:
            raw[name] = value
    return raw


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


def _warn_tracing_failure(exc: BaseException) -> None:
    """Report that webR's own machinery faulted and was contained.

    A fault here is swallowed rather than propagated -- tracing must never change what the
    traced program does -- but silence would hide a broken sink or propagator.

    Goes to a logger, never to `print`. The host application owns its output: it decides
    the level, the handler, and whether these appear at all. `logging` also handles the
    de-duplication that a hand-rolled "warn once" flag used to approximate badly.
    """
    logger.warning(
        "internal tracing error, suppressed to protect the traced program (%s: %s); "
        "records may be missing from here on",
        type(exc).__name__,
        exc,
    )


def _finish(
    ref: NodeRef,
    token: Any | None,
    started_unix_ns: int,
    started_ns: int,
    exc: BaseException | None,
    spec: _Spec,
    inputs: dict[str, str] | None = None,
    result: Any = _NO_RESULT,
    raw_inputs: dict[str, Any] | None = None,
) -> None:
    """Close a node: stop the clock, analyse, record, and propagate failure upward.

    **This function never raises.** It runs on both the success and the exception path of
    every traced call, so a fault in webR's own machinery -- a user-supplied `Propagator`,
    a swapped `TraceBuffer`, a broken detector -- must be contained here. Letting one
    escape would either mask the traced program's exception or inject a new one, which is
    the single thing this library must never do. Context detachment is done first and
    separately, so a later failure cannot leak the contextvar.

    `token` is None for the generator wrappers, which attach and detach around each
    resumption rather than holding the context for the node's whole lifetime.
    """
    duration_ns = perf_counter_ns() - started_ns
    if token is not None:
        try:
            get_propagator().detach(token)
        except BaseException as detach_exc:  # a leaked contextvar is bad; a raise is worse
            _warn_tracing_failure(detach_exc)
    try:
        _record_node(ref, started_unix_ns, duration_ns, exc, spec, inputs, result, raw_inputs)
    except BaseException as record_exc:
        _warn_tracing_failure(record_exc)


def _record_node(
    ref: NodeRef,
    started_unix_ns: int,
    duration_ns: int,
    exc: BaseException | None,
    spec: _Spec,
    inputs: dict[str, str] | None,
    result: Any,
    raw_inputs: dict[str, Any] | None = None,
) -> None:
    """Build and emit the record. May raise; `_finish` contains it."""
    redactor = spec.redactor if spec.redactor is not None else runtime.redactor

    io: dict[str, Any] | None = None
    signals: dict[str, Any] = {}

    if inputs is not None:
        full = spec.wants_full()
        output_text = as_text(result) if result is not _NO_RESULT else None

        # Redaction runs first: before the hash, before the detectors, before anything
        # reaches memory or disk. `redaction.apply` returns None when the redactor failed,
        # and a dropped payload is the correct outcome then.
        dropped: list[str] = []
        if redactor is not None:
            scrubbed: dict[str, str] = {}
            for name, text in inputs.items():
                safe = redaction.apply(text, redactor)
                if safe is None:
                    dropped.append(name)
                else:
                    scrubbed[name] = safe
            inputs = scrubbed
            if output_text is not None:
                output_text = redaction.apply(output_text, redactor)
                if output_text is None:
                    dropped.append("output")

        store_text = spec.wants_text()
        if inputs or output_text is not None:
            io = {}
            if inputs:
                io["inputs"] = {
                    name: fingerprint(text, full=full, store_text=store_text)
                    for name, text in inputs.items()
                }
            if output_text is not None:
                io["output"] = fingerprint(output_text, full=full, store_text=store_text)
            # Detectors always see the real text; only what is *stored* is restricted.
            signals = run_detectors(inputs, output_text, runtime.detectors)
            if not store_text:
                signals = strip_payload_values(signals)

        if dropped:
            # Say so rather than leaving a silent hole where a payload should be.
            signals["redaction_failed"] = sorted(dropped)

        if output_text is None and result is not _NO_RESULT:
            # The *output* has no text, so the lexical detectors had nothing to judge --
            # even if the inputs were prose. Run the value detectors on it instead: NaN,
            # all-zeros, an empty result, and unchanged passthrough are the same class of
            # silent wrongness in a numeric agent as a fabricated figure is in prose.
            signals.update(run_value_detectors(raw_inputs or {}, result))

    # A validator's verdict outranks a heuristic: the user knows what correct looks like.
    reason = _validate(spec, result) if exc is None and result is not _NO_RESULT else None
    if reason is None and signals:
        reason = is_suspect(signals, runtime.suspect_signals)

    if exc is not None:
        status: NodeStatus = NodeStatus.ERROR
        # An exception message and traceback are payloads too, and a provider SDK echoing
        # the failing request into the message ("401 for api_key=sk-...") is ordinary, not
        # exotic. Scrub them on the same terms as any other captured text.
        error = _redact_error(_error_info(exc), redactor)
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
        # the buffer yet, so this is the only moment their ids are knowable. Lazy, so pin
        # can stop at the first id it already knows rather than walking to the root.
        buffer.pin(ref.iter_chain_ids())

    parent = ref.parent
    runtime.emit(
        NodeRecord(
            trace_id=ref.trace_id,
            node_id=ref.node_id,
            parent_id=parent.node_id if parent is not None else None,
            name=ref.name,
            seq=ref.seq,  # assigned at open, so ordering reflects invocation not completion
            status=status,
            started_unix_ns=started_unix_ns,
            duration_ns=duration_ns,
            depth=ref.depth,
            error=error,
            tainted=ref.state.tainted,
            attributes=spec.attributes if spec.attributes is not None else {},
            io=io,
            signals=signals or None,
            usage=ref.state.usage,
        )
    )


def _redact_error(error: ErrorInfo, redactor: redaction.Redactor | None) -> ErrorInfo:
    """Scrub an exception's message and traceback, failing closed like every payload."""
    if redactor is None:
        return error
    message = redaction.apply(error.message, redactor)
    traceback_text = error.traceback
    if traceback_text is not None:
        traceback_text = redaction.apply(traceback_text, redactor)
    return ErrorInfo(
        type=error.type,
        # None means the redactor failed; drop the text rather than risk leaking it.
        message=message if message is not None else "[redaction failed]",
        traceback=traceback_text,
    )


def webR_node(
    fn: F | None = None,
    /,
    *,
    name: str | None = None,
    attributes: dict[str, Any] | None = None,
    capture: bool | tuple[str, ...] | None = None,
    capture_full: bool | None = None,
    capture_text: bool | None = None,
    check: Callable[[Any], Any] | None = None,
    redact: redaction.Redactor | None = None,
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
            Shallow-copied once at decoration time, then shared by reference across every
            record from this callable. webR never mutates it, and the copy means later
            changes to the caller's dict do not leak in. It does *not* make the contents
            immutable: mutating a value inside `record.attributes` mutates it for every
            record this callable has produced. Treat records as read-only.
        capture: Whether to fingerprint string payloads. `None` follows the process-wide
            setting, `False` disables it for this node, `True` captures every string
            argument, and a tuple of parameter names narrows it to those.
        capture_full: Store payload text rather than a fingerprint. `None` follows the
            process-wide setting. Bounded by `fingerprint.MAX_FULL_CHARS`, but still the
            setting most likely to turn a trace file into a liability -- prompts contain
            customer data.
        capture_text: `False` stores no readable payload at all -- lengths and hashes only,
            and signals that quote the payload are reduced to counts. Detection still runs.
            Use it for data you may not retain: the *default* stores a short payload in
            full and the first and last 200 characters of a long one.
        check: A validator run on the return value. Return `True` to pass; return `False`,
            `None`, or a string reason to mark the node **suspect**. It never raises and
            never changes the returned value: a hallucination is a call that succeeded.
        redact: Scrub this node's payloads before anything is recorded. Overrides the
            process-wide redactor. If it raises, the payload is **dropped**, not stored
            unredacted, and `redaction_failed` is recorded in the node's signals.
    """
    # Attributes are copied once here, not per call: the caller's dict must not be able
    # to mutate records that have already been handed to the buffer.
    static_attributes = dict(attributes) if attributes else None

    def decorate(func: F) -> F:
        if getattr(func, "__webr_node__", False):
            # Already traced. Stacking @webR_node on @webR_node would record every call
            # twice as two nested nodes with the same name; first decoration wins.
            return func

        node_name = name or getattr(func, "__qualname__", None) or repr(func)
        spec = _Spec(
            name=node_name,
            attributes=static_attributes,
            capture=capture,
            capture_full=capture_full,
            capture_text=capture_text,
            check=check,
            redactor=redact,
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
    """Names of the parameters that positional arguments actually bind to, in order.

    Only `POSITIONAL_ONLY` and `POSITIONAL_OR_KEYWORD` qualify, and the scan stops at a
    `*args`. Taking every parameter name was a bug: zipping the full list against the
    flat positional tuple paired the *name* `args` with the *first extra value*, so
    `def f(a, *args)` called as `f("x", "y", "z")` captured `"y"` under the name `args`.
    That is a silent misattribution, which is worse than capturing nothing.

    A callable that refuses introspection -- some builtins and C extensions do -- simply
    gets no named inputs rather than breaking decoration.
    """
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return ()

    names: list[str] = []
    for name, parameter in parameters.items():
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD):
            names.append(name)
        elif parameter.kind is parameter.VAR_POSITIONAL:
            # Everything from here on is variadic; no positional index maps to a name.
            break
    return tuple(names)


def _begin(spec: _Spec, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Set up tracing for one call: collect inputs, open the node, make it current.

    **Never raises.** Returns `(ref, token, inputs, started_unix, started)`, or None if
    any of that faulted -- in which case the caller runs the function untraced rather than
    letting webR's own failure surface in the traced program. `attach` is last, so a
    failure cannot leave the contextvar attached with no one to detach it.
    """
    try:
        capturing = spec.wants_capture()
        inputs = _collect_inputs(spec, args, kwargs) if capturing else None
        # Raw values, for the non-text detectors. Held only until `_finish` runs, which is
        # inside the wrapper frame that already holds these arguments -- no new retention.
        raw_inputs = _collect_raw_inputs(spec, args, kwargs) if capturing else None
        ref = _open(spec.name)
        token = get_propagator().attach(ref)
        started_unix = now_unix_ns()
        # A start marker to the durable stream, so a node that never returns (a hang, a
        # killed process) still appears -- as `running` -- instead of vanishing and
        # letting the trace blame whatever did finish.
        #
        # The writer check comes *first*: building the marker unconditionally cost a
        # dataclass allocation on every traced call even with no writer running, which
        # measurably regressed the hot path for a record nobody would receive.
        if runtime.get_writer() is not None:
            parent = ref.parent
            runtime.emit_open(
                NodeOpen(
                    trace_id=ref.trace_id,
                    node_id=ref.node_id,
                    parent_id=parent.node_id if parent is not None else None,
                    name=ref.name,
                    seq=ref.seq,
                    started_unix_ns=started_unix,
                    depth=ref.depth,
                )
            )
    except BaseException as exc:
        _warn_tracing_failure(exc)
        return None
    return ref, token, inputs, started_unix, perf_counter_ns(), raw_inputs


def _wrap_sync(func: Any, spec: _Spec) -> Any:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not runtime.enabled:
            return func(*args, **kwargs)
        began = _begin(spec, args, kwargs)
        if began is None:
            return func(*args, **kwargs)
        ref, token, inputs, started_unix, started, raw_inputs = began
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            _finish(ref, token, started_unix, started, exc, spec, inputs)
            raise
        _finish(ref, token, started_unix, started, None, spec, inputs, result, raw_inputs)
        return result

    return wrapper


def _wrap_async(func: Any, spec: _Spec) -> Any:
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not runtime.enabled:
            return await func(*args, **kwargs)
        began = _begin(spec, args, kwargs)
        if began is None:
            return await func(*args, **kwargs)
        ref, token, inputs, started_unix, started, raw_inputs = began
        try:
            result = await func(*args, **kwargs)
        except BaseException as exc:
            # Includes CancelledError: an agent killed by a timeout is exactly the kind
            # of silent disappearance this library exists to make visible.
            _finish(ref, token, started_unix, started, exc, spec, inputs)
            raise
        _finish(ref, token, started_unix, started, None, spec, inputs, result, raw_inputs)
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

    The wrapper is a full delegating generator: `send`, `throw`, `close`, and the return
    value carried by `StopIteration` all pass through. An earlier version caught thrown
    exceptions and closed the inner generator instead of throwing into it, which meant a
    generator that recovers from an exception could no longer do so once traced -- the
    library changing program behaviour, which it must never do.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not runtime.enabled:
            # `yield from` forwards send(), throw(), and the return value, which is
            # exactly what the traced path below reproduces by hand.
            return (yield from func(*args, **kwargs))

        propagator = get_propagator()
        inputs = _collect_inputs(spec, args, kwargs) if spec.wants_capture() else None
        iterator = func(*args, **kwargs)
        ref = _open(spec.name)
        started_unix, started = now_unix_ns(), perf_counter_ns()
        finished = False

        def close_node(exc: BaseException | None = None) -> None:
            # Idempotent: the GeneratorExit path records success and re-raises, and the
            # outer handler must not then record the same node a second time.
            nonlocal finished
            if not finished:
                finished = True
                _finish(ref, None, started_unix, started, exc, spec, inputs)

        def resume(value: Any = None, throw: BaseException | None = None) -> Any:
            """Advance the inner generator with this node current."""
            token = propagator.attach(ref)
            try:
                if throw is not None:
                    return iterator.throw(throw)
                return iterator.send(value)
            finally:
                propagator.detach(token)

        try:
            try:
                item = resume()
            except StopIteration as stop:
                close_node()
                return stop.value

            while True:
                try:
                    sent = yield item
                except GeneratorExit:
                    iterator.close()
                    close_node()
                    raise
                except BaseException as thrown:
                    # Forward it into the generator, which may handle it and yield again.
                    try:
                        item = resume(throw=thrown)
                    except StopIteration as stop:
                        close_node()
                        return stop.value
                else:
                    try:
                        item = resume(sent)
                    except StopIteration as stop:
                        close_node()
                        return stop.value
        except BaseException as exc:
            iterator.close()
            close_node(exc)
            raise

    return wrapper


def _wrap_async_generator(func: Any, spec: _Spec) -> Any:
    """Async-generator counterpart of `_wrap_generator`, with the same semantics.

    Two differences are forced by the language rather than chosen:

    - There is no `async yield from`, so the delegation loop is written out by hand and
      runs even when tracing is disabled. The disabled path therefore costs more here
      than in the other three wrappers -- it still drives `asend` per item, it just skips
      the context and the record.
    - An async generator may not `return` a value (PEP 525), so there is no equivalent of
      forwarding `StopIteration.value`.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        propagator = get_propagator()
        tracing = runtime.enabled
        inputs = _collect_inputs(spec, args, kwargs) if tracing and spec.wants_capture() else None
        iterator = func(*args, **kwargs)
        ref = _open(spec.name) if tracing else None
        started_unix, started = now_unix_ns(), perf_counter_ns()
        finished = False

        def close_node(exc: BaseException | None = None) -> None:
            nonlocal finished
            if ref is not None and not finished:
                finished = True
                _finish(ref, None, started_unix, started, exc, spec, inputs)

        async def resume(value: Any = None, throw: BaseException | None = None) -> Any:
            token = propagator.attach(ref) if ref is not None else None
            try:
                if throw is not None:
                    return await iterator.athrow(throw)
                return await iterator.asend(value)
            finally:
                if token is not None:
                    propagator.detach(token)

        try:
            try:
                item = await resume()
            except StopAsyncIteration:
                close_node()
                return

            while True:
                try:
                    sent = yield item
                except GeneratorExit:
                    await iterator.aclose()
                    close_node()
                    raise
                except BaseException as thrown:
                    try:
                        item = await resume(throw=thrown)
                    except StopAsyncIteration:
                        close_node()
                        return
                else:
                    try:
                        item = await resume(sent)
                    except StopAsyncIteration:
                        close_node()
                        return
        except BaseException as exc:
            await iterator.aclose()
            close_node(exc)
            raise

    return wrapper


def submit(executor: Executor, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
    """`executor.submit(fn, ...)` that carries the active node into the worker thread.

    `asyncio.to_thread` already copies the context, so it needs no help. Raw threads and
    `ThreadPoolExecutor.submit` do not: work handed to them would otherwise start a fresh
    trace, and the web would quietly claim those nodes had no caller.
    """
    context = contextvars.copy_context()
    return executor.submit(context.run, functools.partial(fn, *args, **kwargs))
