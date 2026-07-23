# ADR 0003 — Tokens, instrumentation, logging, and non-LLM detection

- **Status:** Accepted
- **Date:** 2026-07-21
- **Extends:** [ADR 0001](0001-core-architecture.md), [ADR 0002](0002-inline-detection.md)

## Context

An independent evaluation of webR against a real codebase returned a well-argued NO, and a
review of the library's own positioning surfaced the same conclusion from the other side:
webR is missing the things that make an LLM tracer *usable*, and it is missing them in a
specific order.

1. **No token, cost, or model data.** Every comparable tool records tokens first, because
   that is what people check first. webR recorded durations and payload fingerprints and
   had no concept of a model or a token.
2. **Every node must be decorated by hand.** Comparable tools instrument the provider SDK,
   so one line of setup traces every call. webR's adoption cost scaled with the size of the
   codebase.
3. **The library `print()`s to stderr.** Three code paths wrote directly to `sys.stderr`
   with no way for the host application to silence, filter, or route them — in a library
   whose central invariant is *do not change how the host program behaves*.
4. **Detection is text-only.** All eight detectors read strings, and `as_text` returns None
   for anything else, so a numeric or structured agent got the DAG and the validators but no
   automatic signals at all.

Point 4 also reframes the project honestly. The problem webR addresses is not
"hallucination" specifically; it is **a component returning a wrong answer without
failing**. That describes ML pipelines, ETL, and numeric simulation as much as LLM agents,
and the existing DAG, validators, and taint already work there unchanged.

## Decisions

### 1. Record tokens. Do not compute cost.

`NodeRecord` gains a first-class `usage` block (schema v3): `model`, `input_tokens`,
`output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `stop_reason`.

Typed fields rather than `attributes`, because a renderer, the collapsed view, or any
future UI needs to *sum* tokens across nodes, and an untyped dict of stringly-typed keys
cannot be summed reliably.

**webR will not multiply tokens by a price.** A bundled price table is wrong within months,
every stale number is a bug report, and prices vary by provider, region, tier, and
promotional period. The trace records what was consumed; the caller applies their own rates.
Cache tokens are recorded separately precisely because they are priced differently.

### 2. Instrumentation is an explicit wrapper, not a monkey-patch.

    client = webrtrace.instrument(Anthropic())

Comparable tools patch the provider module on import, which is genuinely zero-touch and
directly contradicts this library's stated invariant: mutating a third-party module at
runtime *is* changing how the host program behaves, it surprises anyone who did not know
tracing was enabled, and it breaks confusingly when SDK internals move.

The wrapper costs one line instead of zero and touches no global state. It proxies
attribute access to the underlying client, so anything webR does not know about still
works.

### 3. Anthropic first.

Cleaner response shape (`usage.input_tokens` / `usage.output_tokens`, cache fields
alongside), fewer legacy code paths than the alternative, and more uniform streaming.
Getting one adapter right teaches the shape the abstraction actually needs; a second
provider follows once that is known.

### 4. Logging via the standard library, with no handler.

    logger = logging.getLogger("webrtrace")

Emitting at `WARNING`, never configuring a handler, never calling `basicConfig`. Handler
configuration belongs to the application. This also removes the hand-rolled "warn only
once" logic — `logging` already handles levels, filtering, and routing far better than a
module-level boolean.

### 5. Detectors for non-text payloads.

Numeric and structural detectors — NaN/infinity, out-of-range, all-zeros, empty
collections, unchanged-from-input — so that the detector layer works for agents whose
payloads are floats, arrays, and records rather than prose.

This is the cheapest item on the list per unit of value: it needs no SDK integration, no
network, and no new dependency, and it widens the library from "LLM agents" to "any system
whose components can be quietly wrong."

## Consequences

- The record schema goes to **v3**. `usage` is optional and absent on nodes that are not
  model calls, so older readers degrade rather than break, and `graph_from_jsonl` continues
  to read v1 and v2 files.
- webR now has an *optional* awareness of the `anthropic` package. It is imported lazily,
  inside the instrumentation path only, and guarded — **the zero-runtime-dependency promise
  is unchanged**, and webR remains fully usable with no provider SDK installed.
- Instrumentation must survive everything the decorator already survives: sync and async
  clients, streaming (where usage only arrives at the end of the stream), exceptions, and a
  `stop_reason` of `refusal` — which is a *successful* HTTP response carrying no content,
  and is exactly the silent-failure shape this library exists to record.

## Deliberately not done

- **Cost computation.** See above.
- **A second provider adapter**, until the first one has proven the abstraction.
- **Auto-instrumentation by import hook**, on the grounds in decision 2.
- **Sessions, run diffing, and a UI.** Real gaps, all of them, but each depends on tokens
  and instrumentation landing first — a UI whose graph cannot show which model ran or what
  it cost would be displaying the wrong thing well.
