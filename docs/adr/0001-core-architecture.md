# ADR 0001 — Core architecture

- **Status:** Accepted
- **Date:** 2026-07-20

## Context

webR traces multi-agent AI systems as a directed acyclic graph, so that a silent failure
or hallucination can be attributed to a specific node. Three properties are in tension:

1. **Zero friction** — instrumentation must be a bare decorator; business logic stays clean.
2. **Correctness** — a graph that silently omits or misattributes edges is worse than no
   graph at all, because the tool exists to be trusted during debugging.
3. **Negligible overhead** — the tracer must never become the reason a run is slow, and it
   must never be able to exhaust memory or block an agent on I/O.

The central design question is how the "who is my caller" pointer is carried across
asynchronous boundaries.

## Options considered

### A. Ambient context (`contextvars`)

A `ContextVar` holds the currently-executing node; the decorator reads it as its parent and
pushes itself for the duration of the call. `asyncio` copies the context at task-spawn time,
so fan-out via `gather` / `TaskGroup` is attributed correctly for free.

- **Pro:** decorator-only, no signature changes, ~100ns lookup.
- **Con:** the context does not cross `ThreadPoolExecutor`, raw threads, or process
  boundaries. Those children silently orphan to root — a lying graph.

### B. Explicit handle threading

A trace handle is passed as an argument through every call. Edges are declared, never
inferred.

- **Pro:** correct across any boundary, including separate machines; serializable by nature.
- **Con:** changes every signature and every call site. Violates requirement 1, which is
  the reason most tracing libraries abandoned this design.

### C. Hybrid (chosen)

Ambient context by default, with an explicit serializable envelope as an escape hatch for
boundaries the runtime cannot cross, and a strict separation between *recording* and
*writing*. This is the W3C `traceparent` pattern applied to a DAG-first data model.

## Decision

Adopt **C**, in three layers:

1. **Propagation** — `contextvars`, accessed only through a `Propagator` protocol so the
   mechanism can be extended (cross-process `inject`/`extract`) without touching the core.
2. **Causality** — `INVOKES` edges are implicit from propagation. `SENDS` edges (data
   dependency between agents that never call each other) are **explicit only**; automatic
   inference is rejected because Python cannot tag `str`, which is the type LLM agents pass
   most often, and a detector that silently fails on the common case is unacceptable.
3. **Buffer / export** — the decorator performs no I/O. It appends an immutable record to a
   bounded queue; a background writer drains it to JSONL.

### Resolved sub-decisions

| Topic | Decision | Rationale |
|---|---|---|
| Node granularity | One node per invocation | Strictly more information; per-agent aggregation is a view, and cannot be recovered if only the aggregate is stored |
| Concurrency scope | asyncio + sync + threads now; cross-process deferred to v0.2 | `asyncio.to_thread` already propagates context; `inject`/`extract` and trace merging are a distributed-systems problem best solved once the core is stable |
| Retention | Bounded hot ring (drop-oldest) + a pinned set that is never age-evicted | Age-only eviction loses an early failure during a long run — the exact record that mattered |
| Pinning policy | Errors, validator-flagged nodes, and their full ancestor chains | The ancestor chain is the causal story; pinning costs O(depth) and only on failure |
| Durability | Streaming JSONL via a background writer; immediate flush on error/suspect | Survives the hard crash that is often the thing being debugged, without disk I/O on the hot path |
| Overflow | Drop-oldest and record `dropped_count` | A trace that silently claims completeness is worse than one that admits a gap |
| Payload capture | Fingerprint by default (length, hash, head/tail); `capture="full"` opt-in | Full prompt capture produces multi-hundred-MB traces; a hash makes "did this node change the content" answerable in constant space |
| Hallucination signals | Lexical detectors computed off the hot path, storing only signals | Fabricated numbers, format breaks, drift, repetition loops, and refusals are detectable without an embedding model or an LLM judge |
| Semantic detection | Out of scope for v0.1; `Detector` protocol is public | Keeps the design from dead-ending without imposing cost or dependencies on v0.1 users |
| Failure semantics | Capture and re-raise, taint descendants; validators mark suspect without raising | webR must be transparent to program behaviour; silent failures need a non-raising channel |
| Runtime dependencies | None | A debugging library that drags in dependencies is one people decline to add |
| Python floor | 3.10 | Broad support; nothing in the core needs 3.11+ |

## Consequences

- The decorator's hot path is a context lookup, two `perf_counter_ns()` calls, a record
  construction, and a queue append. Everything expensive — serialization, detection,
  disk — happens on the writer thread.
- webR owns a background thread and an `atexit` drain. A `SIGKILL` can still lose the last
  unflushed batch of *successful* nodes; errors are flushed immediately and are safe.
- Distributed tracing is a v0.2 feature. The API surface is reserved now so that adding it
  is additive rather than breaking.

## Rejected outright

Auto-instrumentation via `sys.setprofile` or import hooks. Zero-config in a demo,
unacceptable overhead and unusable noise in production.
