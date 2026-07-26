# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Tracing a provider SDK without decorating every call site.

    from anthropic import Anthropic
    import webrtrace

    client = webrtrace.instrument(Anthropic())
    client.messages.create(...)   # now a node, with model, tokens, and stop_reason

**An explicit wrapper, deliberately, rather than patching the SDK on import.** Patching is
what comparable tools do and it is genuinely zero-touch, but mutating a third-party module
at runtime *is* changing how the host program behaves -- the one thing this library
promises never to do. It also surprises a teammate who did not know tracing was on, and
breaks confusingly when SDK internals move. One line of setup buys honesty about what is
being touched.

Nothing here imports the provider SDK at module load. webR keeps **zero runtime
dependencies**; the wrapper works by proxying attribute access, so it never needs the
SDK's types -- only the shape of the response object, and even that is read defensively.

`refusal` deserves special mention. A safety decline is a *successful* HTTP response
carrying `stop_reason: "refusal"` and no content -- an expensive call that returned
nothing, with no exception anywhere. That is precisely the silent failure this library
exists to make visible, so it is recorded as a node and flagged.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from .decorator import webR_node
from .propagation import record_usage
from .records import Usage

logger = logging.getLogger("webrtrace")

#: Methods worth wrapping, as `attribute path -> node name`. Anything not listed is
#: proxied through untouched, so an unfamiliar SDK surface still works.
_ANTHROPIC_METHODS = {
    ("messages", "create"): "anthropic.messages.create",
    ("messages", "stream"): "anthropic.messages.stream",
    ("messages", "parse"): "anthropic.messages.parse",
    ("messages", "count_tokens"): "anthropic.messages.count_tokens",
    ("beta", "messages", "create"): "anthropic.beta.messages.create",
}

#: `stop_reason` values that mean the call produced nothing useful despite succeeding.
_EMPTY_STOP_REASONS = frozenset({"refusal"})


def usage_from_response(response: Any) -> Usage | None:
    """Read model, tokens, and stop reason off a provider response.

    Entirely duck-typed and defensive: a provider is free to change its response object,
    and webR reading it must never be the thing that breaks a working call. Anything
    missing or unreadable simply comes back as None.
    """
    try:
        raw = getattr(response, "usage", None)
        model = getattr(response, "model", None)
        stop_reason = getattr(response, "stop_reason", None)
        if raw is None and model is None:
            return None

        def field(name: str) -> int | None:
            value = getattr(raw, name, None) if raw is not None else None
            return value if isinstance(value, int) else None

        return Usage(
            model=model if isinstance(model, str) else None,
            input_tokens=field("input_tokens"),
            output_tokens=field("output_tokens"),
            # Cache tokens are priced differently from ordinary input, so they are kept
            # separate rather than folded into the input count.
            cache_creation_input_tokens=field("cache_creation_input_tokens"),
            cache_read_input_tokens=field("cache_read_input_tokens"),
            stop_reason=stop_reason if isinstance(stop_reason, str) else None,
        )
    except Exception:  # reading a response must never break the call that produced it
        return None


def _check_response(response: Any) -> bool | str:
    """Validator: flag a call that succeeded while producing nothing.

    A `refusal` returns HTTP 200 with an empty `content` list. Nothing raises, the caller
    is billed, and a pipeline that only checks for exceptions carries the empty result
    forward as if it were an answer.
    """
    stop_reason = getattr(response, "stop_reason", None)
    if isinstance(stop_reason, str) and stop_reason in _EMPTY_STOP_REASONS:
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        return f"model declined the request (stop_reason={stop_reason}, category={category})"
    if stop_reason == "max_tokens":
        return "response truncated at max_tokens"
    return True


def _returns_coroutine(method: Any) -> bool:
    """Whether calling `method` produces a coroutine, decided without calling it.

    A plain `async def` answers directly. An SDK that exposes its endpoints as callable
    *objects* rather than bound methods does not, so the check falls through to the
    class's `__call__`. `getattr_static` is used for that lookup because it reads the
    attribute off the type without triggering descriptors or `__getattr__` -- provider
    clients are full of lazily-materialised namespaces, and merely inspecting one must
    not construct anything.
    """
    if inspect.iscoroutinefunction(method):
        return True
    call = inspect.getattr_static(type(method), "__call__", None)
    return inspect.iscoroutinefunction(call)


class _TracedMethod:
    """One provider method, traced. Records usage after the call, before the node closes.

    Sync vs async is decided **here**, at wrap time, from the bound method itself. An
    earlier version decided by calling the sync path and checking whether the result was
    awaitable -- which invoked the provider twice on every async call, once for a
    coroutine that was then discarded. Never probe by calling something with side effects.
    """

    __slots__ = ("_is_async", "_method", "_name", "_traced")

    def __init__(self, method: Any, name: str) -> None:
        self._method = method
        self._name = name
        self._is_async = _returns_coroutine(method)
        target = self._acall if self._is_async else self._call
        self._traced = webR_node(target, name=name, check=_check_response)

    def _call(self, *args: Any, **kwargs: Any) -> Any:
        response = self._method(*args, **kwargs)
        _attach(response)
        return response

    async def _acall(self, *args: Any, **kwargs: Any) -> Any:
        response = await self._method(*args, **kwargs)
        _attach(response)
        return response

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # A streaming call returns a context manager, not a coroutine or a message; the
        # node closes when the *call* returns, so usage (which only arrives at the end of
        # the stream) is attached by the stream wrapper instead.
        if self._name.endswith(".stream"):
            return _TracedStream(self._method(*args, **kwargs), self._name)
        return self._traced(*args, **kwargs)


def _attach(response: Any) -> None:
    usage = usage_from_response(response)
    if usage is not None:
        record_usage(usage)


class _TracedStream:
    """Proxy for a streaming context manager.

    Streaming reports usage only once the stream completes, so the node has to stay open
    across the whole `with` block rather than closing when the call returns.
    """

    __slots__ = ("_entered", "_inner", "_name", "_node")

    def __init__(self, inner: Any, name: str) -> None:
        self._inner = inner
        self._name = name
        self._node = None
        self._entered = None

    def __enter__(self) -> Any:
        self._node = _open_stream_node(self._name)
        self._entered = self._inner.__enter__()
        return self._entered

    def __exit__(self, *exc_info: Any) -> Any:
        try:
            _attach_final(self._entered)
        finally:
            result = self._inner.__exit__(*exc_info)
            _close_stream_node(self._node, exc_info)
        return result

    async def __aenter__(self) -> Any:
        self._node = _open_stream_node(self._name)
        self._entered = await self._inner.__aenter__()
        return self._entered

    async def __aexit__(self, *exc_info: Any) -> Any:
        try:
            _attach_final(self._entered)
        finally:
            result = await self._inner.__aexit__(*exc_info)
            _close_stream_node(self._node, exc_info)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _attach_final(stream: Any) -> None:
    """Pull usage off a finished stream, if it got far enough to have any."""
    try:
        getter = getattr(stream, "get_final_message", None)
        if getter is not None:
            _attach(getter())
    except Exception:  # an unfinished or failed stream simply has no usage to report
        return


def _open_stream_node(name: str) -> Any:
    from . import runtime
    from .propagation import get_propagator, new_root

    if not runtime.enabled:
        return None
    try:
        parent = get_propagator().current()
        ref = parent.child(name) if parent is not None else new_root(name)
        token = get_propagator().attach(ref)
    except BaseException:
        return None
    from .records import now_unix_ns

    return (ref, token, now_unix_ns(), __import__("time").perf_counter_ns())


def _close_stream_node(node: Any, exc_info: Any) -> None:
    if node is None:
        return
    from .decorator import _NO_RESULT, _finish, _Spec

    ref, token, started_unix, started = node
    spec = _Spec(
        name=ref.name,
        attributes=None,
        capture=False,
        capture_full=None,
        capture_text=None,
        check=None,
        redactor=None,
        param_names=(),
    )
    exc = exc_info[1] if exc_info and len(exc_info) > 1 else None
    _finish(ref, token, started_unix, started, exc, spec, None, _NO_RESULT)


class _Proxy:
    """Forwards everything to the wrapped object, tracing the methods webR knows about."""

    __slots__ = ("_cache", "_methods", "_path", "_wrapped")

    def __init__(self, wrapped: Any, path: tuple[str, ...], methods: dict) -> None:
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_methods", methods)
        object.__setattr__(self, "_cache", {})

    def __getattr__(self, name: str) -> Any:
        wrapped = object.__getattribute__(self, "_wrapped")
        # Read through to the live attribute on every access. An earlier version cached
        # the wrapper on first touch and returned it forever -- so when the application
        # swapped `client.messages` (test monkeypatching, retry logic rebuilding a
        # resource), the instrumented client silently kept calling the *old* object.
        # A tracing wrapper that redirects the program to a dead object is changing the
        # program's behaviour, which is the one thing this library must never do. The
        # cache now keys on the identity of the underlying value: same object, same
        # wrapper; swapped object, fresh wrapper.
        value = getattr(wrapped, name)

        cache = object.__getattribute__(self, "_cache")
        hit = cache.get(name)
        if hit is not None and hit[0] is value:
            return hit[1]

        path = object.__getattribute__(self, "_path")
        methods = object.__getattribute__(self, "_methods")
        full = (*path, name)

        if full in methods:
            wrapper = _TracedMethod(value, methods[full])
        elif any(candidate[: len(full)] == full for candidate in methods):
            # An intermediate namespace on the way to something traced.
            wrapper = _Proxy(value, full, methods)
        else:
            wrapper = value

        cache[name] = (value, wrapper)
        return wrapper

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_wrapped"), name, value)

    def __repr__(self) -> str:
        return f"<webrtrace-instrumented {object.__getattribute__(self, '_wrapped')!r}>"


def instrument(client: Any, *, methods: dict | None = None) -> Any:
    """Wrap a provider client so its calls become traced nodes.

    Supports Anthropic's sync and async clients out of the box. Everything webR does not
    recognise is proxied straight through, so an unfamiliar or newer SDK surface keeps
    working -- untraced rather than broken.

    Idempotent: instrumenting an already-instrumented client returns it unchanged. The
    naive alternative stacked two wrappers, which recorded **two nodes and double the
    tokens for every single call** -- and wrapping twice is easy to do by accident, once
    in a shared helper and once at the call site.

    If *none* of the traced method paths exist on the client -- an OpenAI client handed
    to the Anthropic map, say -- a warning is logged once. The alternative was a silent
    no-op, where every call passes through untraced while the caller believes tracing is
    on; a library built to expose silent failures does not get to fail silently itself.

    Args:
        client: The provider client to wrap.
        methods: Override the traced-method map, as `("attr", "path") -> node name`.
    """
    if isinstance(client, _Proxy):
        return client

    table = methods if methods is not None else _ANTHROPIC_METHODS
    try:
        if not _any_path_resolves(client, table):
            logger.warning(
                "instrument(): none of the traced method paths %s exist on %s; "
                "every call will pass through untraced. Pass methods= with the "
                "correct attribute paths for this SDK.",
                sorted({".".join(p) for p in table}),
                type(client).__name__,
            )
    except Exception:  # a diagnostic must never break the wrap
        pass
    return _Proxy(client, (), table)


def _any_path_resolves(client: Any, table: dict) -> bool:
    """Whether at least one traced path leads to a callable on this client."""
    for path in table:
        obj: Any = client
        for part in path:
            obj = getattr(obj, part, None)
            if obj is None:
                break
        if callable(obj):
            return True
    return False
