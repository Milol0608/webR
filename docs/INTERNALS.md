# How webR works

Written for someone who needs to maintain this code, extend it, or defend a decision in it
during review. It explains the mechanisms, not just the API, and it says *why* each choice
was made — including where the alternatives would have been reasonable.

- [The whole thing in one paragraph](#the-whole-thing-in-one-paragraph)
- [The life of one traced call](#the-life-of-one-traced-call)
- [The five mechanisms worth understanding](#the-five-mechanisms-worth-understanding)
- [Module map](#module-map)
- [Decisions and their counterfactuals](#decisions-and-their-counterfactuals)
- [Sharp edges](#sharp-edges)
- [Extending it](#extending-it)

---

## The whole thing in one paragraph

A `ContextVar` holds a reference to the node currently executing. When a decorated function
is called, it reads that variable to learn who its parent is, puts itself there while the
function runs, and restores the previous value on the way out. (Generators are the
exception: they attach and detach around each individual resumption, because a generator's
body runs in slices and the context must not be held between them.) When the call
finishes, it builds one immutable record — timings, status, a fingerprint of the payloads,
any signals the detectors produced — and appends that record to a bounded in-memory buffer
and, optionally, to a queue drained by a background thread that writes JSONL. Everything
else in the library is either producing those records, retaining them intelligently, or
turning them back into a graph.

---

## The life of one traced call

Follow `@webR_node` from decoration to record. This is the whole system on one path.

### At decoration time (once, at import)

`webR_node` builds a `_Spec` — the node name, static attributes, capture policy, validator,
and the function's parameter names — and then picks a wrapper based on what kind of callable
it received:

```
inspect.isasyncgenfunction  -> _wrap_async_generator
inspect.iscoroutinefunction -> _wrap_async
inspect.isgeneratorfunction -> _wrap_generator
otherwise                   -> _wrap_sync
```

**Why once:** re-deriving any of this per call would be the most avoidable overhead in the
library. `inspect.signature()` alone costs tens of microseconds — more than everything else
in the wrapper combined. Async generators are checked first because an async generator
function is *not* a coroutine function, and testing in the other order silently
misclassifies them.

### At call time

1. **`if not runtime.enabled: return func(...)`** — one module-attribute load and a branch.
   This is why tracing can be switched on inside a live process; the alternative (returning
   the undecorated function at import time) would be free but frozen at import.

   Two wrappers cannot be quite that cheap. The sync-generator wrapper exits via
   `return (yield from func(...))`, which is still one branch but delegates through the
   generator protocol. The **async-generator wrapper has no early exit at all**: Python has
   no `async yield from`, so the delegation loop is written out by hand and runs even when
   tracing is disabled, driving `asend` per item while skipping the context and the record.
   Disabled tracing is therefore materially cheaper for sync and async functions than for
   async generators.

2. **Collect inputs.** String-valued arguments are matched to parameter names by zipping
   `spec.param_names` against `args` and reading `kwargs` directly. Only `str` and `bytes`
   qualify — they are immutable, so a fingerprint taken now stays true.

3. **Open the node.** `_open()` reads the propagator's current `NodeRef`. If there is one,
   `parent.child(name)` mints a child sharing the parent's `trace_id`, with `depth + 1` and
   a direct pointer back to the parent. If there is none, `new_root(name)` starts a new
   trace.

4. **Attach.** `propagator.attach(ref)` sets the `ContextVar` and returns a token. Anything
   the function calls now sees this node as its parent.

5. **Two clocks.** `time.time_ns()` for a wall-clock timestamp you can correlate against
   external logs, and `perf_counter_ns()` for the duration. Two clocks because wall time can
   move backwards and monotonic time is meaningless as a date.

6. **Call the function**, inside `try/except BaseException`. `BaseException`, not
   `Exception`, so `asyncio.CancelledError` is recorded — an agent killed by a timeout is a
   fact worth having.

7. **`_finish()`** does the rest: stop the clock, `detach` the token, fingerprint the
   payloads, run the detectors, run the validator, decide a status, taint the ancestors if
   the status is not OK, pin the ancestor chain, build the `NodeRecord`, and hand it to
   `runtime.emit()`.

8. **Re-raise**, unchanged, if there was an exception.

### After the call

`runtime.emit()` appends to the `TraceBuffer` and, if a writer is running, to its queue.
Both are O(1). No serialization, no I/O, no locks held across user code.

---

## The five mechanisms worth understanding

### 1. `contextvars` — how a function learns who called it

A `ContextVar` looks like a global variable but has one crucial property: **`asyncio` copies
the context when a task is spawned.** So this works with no cooperation from the caller:

```python
await asyncio.gather(worker(1), worker(2), worker(3))
```

Each worker runs in a *copy* of the orchestrator's context, sees the orchestrator as its
parent, and writes its own node into its own copy — where it cannot be seen by its siblings.
That is the entire reason concurrent agents do not scribble over each other's parentage.

Internally a `Context` is a HAMT (a hash array mapped trie), so copying is O(1) with
structural sharing and a lookup is a few pointer dereferences — about 50–150ns.

**Where it stops working:** raw `threading.Thread` and `ThreadPoolExecutor.submit` do *not*
copy the context. `asyncio.to_thread` does. That asymmetry is why `webrtrace.submit()`
exists — it calls `contextvars.copy_context()` explicitly and runs the work inside the copy.

`attach`/`detach` is strictly paired and the token is opaque, which is what makes nesting
and interleaving unwind correctly.

### 2. Frozen records — why the data model is immutable

`NodeRecord` is `@dataclass(frozen=True, slots=True)`.

- **Frozen** because a record crosses a thread boundary to the writer. If it could be
  mutated after handoff, you would have a data race that silently corrupts the trace file.
- **Slots** because there is one instance per traced call, and `__slots__` removes the
  per-instance `__dict__`.

This immutability has a consequence that shaped the whole design: **detection cannot be
deferred.** Signals must exist before the record is built, which is why the detectors run
inline in the decorator rather than on the writer thread as originally planned. See
[ADR 0002](adr/0002-inline-detection.md).

### 3. Interest-based retention — why an early failure survives a long run

`TraceBuffer` holds two structures:

- a **ring** (`deque`) with every node, dropping the oldest at capacity;
- a **pinned** dict holding nodes that must not be evicted by age.

A node is pinned when it errored, was flagged suspect, is tainted, or its id was passed to
`pin()`.

The subtle part: **ancestors are pinned before they exist.** A parent completes *after* the
child that failed inside it, so when the child fails, its parents have no records yet. `pin()`
therefore accepts ids that are not resident and remembers them in `_pin_requests`; `append()`
checks that set on arrival. The ids come from `NodeRef.chain_ids()`, walking the live parent
pointers — the only place that ancestry exists at that moment.

Everything here is bounded: the ring, the pinned store, *and* the outstanding pin requests.
An earlier draft leaked on the last two, which is what
`test_internal_indexes_do_not_grow_without_bound` exists to prevent.

### 4. Taint — and why it flows *up*

When a node ends up ERROR or SUSPECT, it calls `ref.taint_ancestors()`, walking parent
pointers and setting a flag on each.

Taint travels **up the call tree** because that is the direction *data* flows: a parent
consumed whatever its child returned. If the child's output is wrong, the parent's answer is
built on it.

`NodeRef` is frozen, so the flag lives on a small mutable companion object, `NodeState`.
That split is deliberate — the ref is copied across contexts and must never be rebound, but
one piece of genuinely mutable state has to exist, because a node cannot know at its start
whether a descendant will fail later.

The case that makes this earn its keep:

```python
try:
    return await extract(report)
except ValueError:
    return fallback          # your program now reports success
```

The worker is `[ ok]` — and tainted. The exception was swallowed by your code; it was not
swallowed by the web.

### 5. Identity-based linking — and the strong reference that makes it safe

`mark(value)` stores `id(value) -> (value, Link)` in a bounded `OrderedDict`.

Storing `id()` alone would be a serious bug: once the object is freed, CPython reuses the
address, and a later object landing there would resolve to the wrong producer. **Keeping a
strong reference to the value prevents that** — a referenced object cannot be freed, so its
`id()` cannot be recycled.

That reference is also why the registry must be bounded (2,048 entries, FIFO). It is a
deliberate, capped retention rather than a leak. When a mark is evicted, `link()` returns
`False` — no edge — rather than guessing.

`lookup()` also verifies `stored is value`, not `==`. Two equal lists are not the same datum,
and treating them as one would invent edges that never existed.

---

## Module map

In dependency order — each depends only on those above it.

| Module | Responsibility | The one thing to know |
|---|---|---|
| `_ids.py` | Trace and node ids | 128/64-bit, W3C Trace Context widths, so a future distributed propagator needs no second scheme. Reseeds after `fork` |
| `records.py` | `NodeRecord`, `EdgeRecord`, enums | Frozen, slotted, JSON-ready. `is_interesting` is what drives retention |
| `fingerprint.py` | Bounded payload summaries | The **hash** is the useful part: same hash means the content passed through untouched |
| `propagation.py` | `NodeRef`, `NodeState`, `Propagator` | The decorator never touches `contextvars` directly — it goes through `Propagator`, which is what makes propagation replaceable |
| `detectors.py` | Eight lexical signals, four value signals | Limits apply to the text *before* the scan, not to the results — read via `scanned_output`/`scanned_input`, never the raw payload. The value detectors run only when the *output* had no text |
| `buffer.py` | Bounded retention | Ring + pinned, both capped |
| `writer.py` | JSONL on a daemon thread | Daemon + `atexit`, and it survives write failures rather than dying |
| `runtime.py` | Process-wide state, `emit()` | One place where a record fans out to every sink |
| `decorator.py` | `@webR_node` | Shape dispatch at decoration time; four wrappers |
| `links.py` | `SENDS` edges | Bounded identity registry with strong references |
| `graph.py` | Graph documents | Reports its own gaps: `dropped`, `dangling_edges` |
| `redaction.py` | Scrubbing payloads | Fails closed — a redactor that raises drops the payload rather than recording it |
| `collapse.py` | Per-agent aggregate view | A *view*, not a trace: ids are synthetic and durations are sums. The worst status always wins |
| `instrument.py` | Provider-SDK wrapper | A proxy, not a monkey-patch. Imports no SDK; reads the response shape defensively. Sync-vs-async is decided at wrap time, never by calling |
| `render.py` | Terminal output | ASCII only, on purpose |
| `__main__.py` | `python -m webrtrace` | Reads a file or a directory |

---

## Decisions and their counterfactuals

| Decision | The alternative | Why not |
|---|---|---|
| Ambient `contextvars` | Pass a trace handle through every signature | Correct across any boundary, but changes every function and call site. It is why most tracing libraries abandoned it |
| Decorator always wraps, checks a flag | Return the original function when disabled | Free, but frozen at import — you could never turn tracing on in a running process |
| `SENDS` edges declared explicitly | Infer them by tagging returned objects | Python forbids attributes on `str`, the type agents pass most. It would fail silently on the common case |
| Detection inline | On the writer thread | Impossible with frozen records, and deferring means retaining payloads — the exact unbounded growth the buffer exists to prevent |
| Retention by interest | Drop-oldest only | An early failure gets evicted by an hour of uneventful successes |
| Daemon thread + `atexit` | Non-daemon thread | Python joins non-daemon threads *before* running `atexit` handlers, and this loop only stops when an `atexit` handler says so — the interpreter would hang |
| `flush()`, not `fsync()` | fsync per failed node | Milliseconds on the path that is already going badly. flush survives a process crash, which is the actual scenario |
| Zero dependencies | `pydantic` for the record schema | Saves ~50 lines and costs adoption. People decline to add a debugging library that drags in packages |
| `instrument(client)` wrapper | Patch the SDK on import | Genuinely zero-touch, and it mutates a module webR does not own — the one thing this library promises never to do. It also breaks confusingly when SDK internals move |
| Tokens recorded, cost not | A price table per model | Prices change and vary by contract. A hardcoded table is wrong the week after it ships, and quietly |
| `logging` for webR's own faults | `print` to stderr | stderr is the application's, and a program whose stdout is a data stream should not have a tracing library writing into its output |

---

## Sharp edges

Things that will bite whoever touches this next.

- **`runtime.enabled` is read as a module attribute**, not through `is_enabled()`, to keep
  the hot path short. `from webrtrace.runtime import enabled` captures a snapshot and will
  not track changes. Toggle through `enable()`/`disable()`.
- **Generator wrappers pass `token=None` to `_finish`.** They attach and detach around each
  resumption rather than holding context for the node's lifetime, so there is no token left
  to reset. Passing one would raise.
- **`GeneratorExit` is recorded as success.** A consumer that `break`s out of a loop
  abandoned the generator; treating that as failure would fill the web with phantom errors.
- **`as_text` uses `type(value) is str`, not `isinstance`.** A `str` subclass is deliberately
  not captured — it may carry lazy or mutable behaviour.
- **The number regex has no leading `-?`.** An optional group at the start forces a match
  attempt at every character; anchoring on `\d` measured ~3x faster. The cost is that sign is
  ignored, so `-42` and `42` compare equal.
- **Everything that renders an exception is defended.** `str(exc)` can raise. If you add
  error-handling code, keep it wrapped, or webR will start changing program behaviour.
- **`stats()["dropped"]` covers two causes**: queue overflow and lost write batches.
  `write_errors` isolates the second. Both are persisted to the JSONL as periodic `meta`
  lines, so an offline reader sees them too.
- **`seq` is assigned at node *open*, not completion.** It lives on `NodeRef` and orders
  nodes by invocation, as the docs promise. A terminal record and its `open` marker share
  the same `seq` -- that is how a completed node supersedes its own start marker.
- **A node that never finishes** leaves only an `open` marker (writer-only); the reader
  renders it `running`. This is the sole reason `emit_open` exists, and it is skipped
  entirely when no writer is active.
- **Pinned records are not immortal.** The pinned store has its own ceiling and evicts the
  oldest when full, counting them in `pins_dropped`. "Never evicted by age" means exactly
  that and nothing more.
- **Generator wrappers must stay full delegating generators.** `send`, `throw`, `close`,
  and `StopIteration.value` all have to pass through. An earlier version caught thrown
  exceptions and closed the inner generator instead of throwing into it, which broke
  recovery in any generator that handles exceptions — tracing silently changing behaviour.
  If you touch `_wrap_generator`, run `tests/test_review_findings.py`.
- **Taint does not cross a process boundary.** It rides on `NodeState`, a mutable object
  reachable through the live `NodeRef` chain, and mutable state cannot traverse a process.
  A child process's failure marks its local ancestors; the caller in the parent process
  stays unmarked. The `[ERR]` node still appears in the joined web and the failure chain
  still crosses the boundary — only the `*` stops at the edge. Propagating it would mean
  a return channel from child to parent, which webR does not have and should not grow.
- **Detectors must read `scanned_output` / `scanned_input`, never `payloads.output`
  directly.** Two of them once called `.strip()` on the raw payload. CPython makes that
  free when nothing is stripped and an O(n) copy when something is — 2.1ms on 10MB — and
  model output routinely begins with a newline, so the expensive path was the common one.

---

## Extending it

**A new detector** — a callable taking `Payloads` and returning a mapping or `None`:

```python
def detect_sql(payloads):
    # `lower_output` is the bounded, already-lowercased text. Reading `payloads.output`
    # here instead would scan the entire payload -- unbounded work on every call.
    if "drop table" in payloads.lower_output:
        return {"dangerous_sql": True}

detect_sql.name = "dangerous_sql"
webrtrace.set_detectors(*webrtrace.DEFAULT_DETECTORS, detect_sql)
webrtrace.set_suspect_signals("dangerous_sql", "refusal", "empty_output")
```

It must be cheap, must not do I/O, and must read through the cached, bounded properties on
`Payloads` (`lower_output`, `scanned_output`, `output_words`, `input_numbers`, …) rather
than touching `payloads.output` or re-tokenizing. A detector that raises is contained and
reported as `detector_errors` — but do not rely on that.

**A new sink** — add it to `runtime.emit()`. That function is the single fan-out point, which
is why an OpenTelemetry bridge would be a two-line change.

**A cross-process propagator.** The protocol now carries five methods — `current`,
`attach`, `detach`, `inject`, `extract` — and the seam did what it was supposed to do: the
four decorator wrappers were not touched to add distributed tracing.

`inject()` reduces the active node to a W3C `traceparent` string (the id widths in
`_ids.py` were chosen to match). `extract()` rebuilds a `NodeRef` that stands in for a node
living in another process. That stand-in is never recorded locally — it belongs to the
process that created it — so no node is duplicated when both files are read together.

The "merge step" turned out to need no code at all, which is the design paying off: both
processes emit records carrying the same `trace_id`, and the child's records carry a
`parent_id` pointing at the remote node. `graph_from_jsonl()` over a directory already
sorts by `seq` and joins on ids. Read one file and the edge is dangling; read both and it
resolves.

What remains genuinely unsolved: **clock skew** (`started_unix_ns` is each machine's own
wall clock, so cross-process ordering by timestamp is unreliable — `seq` is only monotonic
within a process), and **partial traces** where the process holding the root died before
writing. Both are distributed-systems problems rather than decorator problems.
