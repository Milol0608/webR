# Changelog

Notable changes to webR. This project follows [Semantic Versioning](https://semver.org/);
while the major version is `0`, the public API may change between minor releases.

## [Unreleased]

### Added

- **Token accounting** — a `Usage` record on every node (`model`, `input_tokens`,
  `output_tokens`, both cache counters, `stop_reason`), reported by `record_usage()` from
  inside any traced call. Cache tokens stay separate from ordinary input because they are
  priced differently. Tokens are recorded, not dollars: prices change and vary by contract,
  and a hardcoded table is wrong the week after it ships.
- **Provider instrumentation** — `instrument(client)` wraps an Anthropic sync or async
  client so `messages.create` / `.stream` / `.parse` / `.count_tokens` become nodes without
  decorating a single call site. An explicit wrapper rather than an import-time patch, and
  a streaming call keeps its node open across the whole `with` block because usage only
  arrives when the stream ends. No SDK is imported; webR still has zero runtime
  dependencies, and unrecognised methods proxy straight through untraced.
- **Refusals and truncation are suspect** — `stop_reason: "refusal"` is a *successful*,
  billed call that returned no content, and `max_tokens` is an answer cut off mid-thought
  and passed on as complete. Both are now flagged.
- **Value detectors for non-text agents** — when a call's *output* has no text
  representation, `nan`, `infinite`, `all_zeros`, `empty_collection`, and
  `unchanged_value` run in place of the lexical pass, so embedders, scorers, and feature
  transforms are covered. Only `nan` and `infinite` mark a node suspect; an empty result
  list is frequently correct. Number scanning is bounded at 10,000 values per node, and
  `bool` is excluded from the numeric checks since it subclasses `int`.
- [ADR 0003](docs/adr/0003-tokens-and-instrumentation.md) recording all five decisions.

### Changed

- **webR's own faults go to the `webrtrace` logger**, at `WARNING`, once per condition,
  instead of `print`. No handler is installed — a library that configures logging hijacks
  output it does not own — and nothing is written to stdout or stderr directly, so webR
  cannot corrupt a program whose stdout is a data stream.
- `DEFAULT_SUSPECT_SIGNALS` gains `nan` and `infinite`. An undefined number is never a
  correct result, in any domain.

### Fixed

- **An instrumented async client called the provider twice per call.** Sync-vs-async was
  decided by invoking the sync path and checking whether the result was awaitable, which
  issued a real request, discarded the coroutine, and then issued another — double billing
  and two nodes per call. It is now decided at wrap time from the bound method. Never probe
  by calling something with side effects.
- The value detectors were gated on the whole call having no text, so an agent with a text
  input and a numeric output — the shape of every embedder — got no detection at all. The
  gate now keys off the output alone.

## [0.1.0] — 2026-07-21

First public release.

### Added

- **Core model** — immutable `NodeRecord` / `EdgeRecord`, W3C-width trace and node ids,
  and a `Propagator` protocol so propagation can be extended without touching the core.
- **`@webR_node`** — drop-in tracing for sync, async, generator, and async-generator
  callables, with shape dispatch resolved at decoration time.
- **Interest-based retention** — a bounded ring plus a pinned store that keeps failures,
  suspects, and their ancestor chains, so an early failure survives a long run.
- **Durable export** — JSONL streaming on a background thread with immediate flush on
  failure, size-based rotation, and an `atexit` drain.
- **Graph documents** — `export_graph()` and `graph_from_jsonl()` assemble nodes and edges
  into one document, reporting dropped nodes and dangling edges rather than hiding them.
- **Hallucination signals** — eight dependency-free lexical detectors, including
  `novel_numbers` for fabricated figures, plus per-node validators that mark a node
  suspect without raising.
- **Taint propagation** — a failed or suspect node marks every ancestor as downstream of
  a problem, even ancestors that have not finished yet.
- **Explicit `SENDS` edges** — `mark()`/`link()` for in-process data dependencies and
  serializable `origin()` tokens for hand-offs across threads, queues, or machines.
- **Redaction** — `set_redactor()` and a per-node `redact=`, applied before payloads are
  hashed, inspected, or stored. Fails closed: a redactor that raises causes the payload to
  be dropped rather than recorded. Ships `common_secrets` as a floor for API keys, tokens,
  emails, and card-length digit runs.
- **Cross-process propagation** — `inject()` emits a W3C Trace Context `traceparent`;
  `remote_parent()` adopts it, so work in another process joins the caller's trace with a
  real parent/child edge. Exporting a directory of per-process JSONL files stitches the
  halves into one web. Taint does not cross the boundary, and clock skew makes
  cross-process timestamp ordering unreliable; both are documented.
- **Terminal renderer and CLI** — `render()` and `python -m webrtrace <file-or-dir>`,
  including `--failures` for just the chains that broke and `--collapse` for the aggregate.
- **Per-agent aggregate view** — `collapse_by_agent()` folds repeated invocations into one
  node per agent with call counts, summed and worst-case durations, and a status rollup in
  which the worst outcome wins, so a single failure among forty successes is never hidden.

### Fixed before release — multi-agent break-it review

Five adversarial agents (concurrency, resource exhaustion, API abuse, data integrity,
portability) attacked the package with runnable reproductions. Regression tests live in
`tests/test_breakit_findings.py` and `tests/test_hang_and_order.py`.

**Tracing could change the traced program**
- A faulting `Propagator` or swapped `TraceBuffer` could mask the program's own exception,
  inject a new one, or stop the function body running. All tracing machinery is now
  contained; a fault is reported once on stderr and never propagates.
- Exception messages and tracebacks bypassed the redactor entirely, so a provider SDK
  echoing `api_key=sk-...` into an error wrote it to disk verbatim.
- `str`/`bytes` subclasses (`enum.StrEnum`, `markupsafe.Markup`) silently received no
  capture and no detection.
- Stacking `@webR_node` twice recorded every call as two nodes.

**The trace could lie**
- **A hung or killed node was invisible.** It emitted no record at all, so the trace showed
  only what finished and pointed at the wrong node. An open-marker is now written to the
  durable stream and such nodes appear as `running`.
- Writer drops never reached the exported document; a lost error read as a clean run.
- `mark()`/`link()` fabricated data-dependency edges between unrelated agents, because
  CPython interns `"done"` and `0` so identity comparison succeeded across them.
- `collapse_by_agent` detached a failing subtree and promoted it to a phantom root when two
  parents shared a name.
- `seq` was completion order while documented and used as invocation order.

**Portability and data handling**
- Trace rotation used `Path.rename`, which **silently replaces** an existing target on
  POSIX (destroying a previous run's rotated trace) and **raises** on Windows (disabling
  rotation, so the file grew unbounded). Rotation now probes for a free name, so neither
  happens and runs no longer collide.
- The default trace path was fixed, so two processes appended to one file with no locking
  and destroyed each other's records. It is now `traces/webrtrace-<pid>.jsonl`.
- `python -m webrtrace` crashed with `UnicodeEncodeError` on a Windows console using a
  legacy code page when a node name contained non-ASCII characters.
- `render` and `collapse_by_agent` raised `KeyError` on a document with a missing
  `node_id` or edge endpoint — which `load_jsonl` can legitimately produce from a
  truncated file, since it skips unparseable lines rather than refusing to open one.
- **New: `set_capture(True, text=False)` / `capture_text=False`** keeps hallucination
  detection while storing no readable payload — lengths and hashes only, with
  value-quoting signals reduced to counts. This closed a real gap: the previous choices
  were "store excerpts" (the default, which keeps a payload under 400 characters *in
  full*) or "capture nothing", which lost detection too. Documented plainly in
  `SECURITY.md`: the default is not a privacy control.

**Performance cliffs on the failure path**
- A deep traced failure was O(depth²): rendering the full traceback at every unwind level
  made a depth-2000 failure take **145 seconds**. The traceback is now rendered once, by
  the innermost frame to see the exception — **21.6ms**, and taint/pin short-circuit.
- `failure_chains` was O(N²): a 5MB document peaked at **1.6GB**. Chains are now bounded,
  deduplicated, and deepest-first so the origin survives the cap.
- `render_tree` recursed and raised `RecursionError` at ~depth 1000 on traces webR itself
  had recorded, and emitted 38MB of whitespace at depth 5000. It is iterative and the
  indent is capped.
- The mark registry was bounded in entries, not bytes: 2,048 × 1MB payloads retained
  **2.05GB**. A byte budget now caps it (measured 2050MB → 66MB).
- A shutdown race discarded whole batches while `dropped` still read zero; each writer also
  leaked an `atexit` handler for the life of the process.
- The writer held its only lock across `file.write()`, so `submit()` — and therefore every
  traced call — blocked on the filesystem, despite a docstring promising it never blocks.
  The queue and the file handle now have separate locks that are never held at once.

### Fixed before release

Found by an independent adversarial review of the package, and kept as regression tests in
`tests/test_review_findings.py`:

- **Traced generators dropped `throw()` and `StopIteration.value`.** `gen.throw(exc)` closed
  the inner generator instead of throwing into it, so a generator that recovers from an
  exception could not once traced, and `value = yield from traced_gen()` returned `None`.
  Both wrappers are now full delegating generators.
- **`*args` was captured under the wrong name.** Zipping every parameter name against the
  positional tuple paired the name `args` with the first extra value.
- **`reset()` did not clear the mark registry**, so a later `link()` could emit an edge
  pointing at a node from a discarded run.
- **`link(value, "")` could not suppress a mark's label** (`or` instead of `is not None`).
- **Two detectors stripped the raw payload**, an O(n) copy whenever leading whitespace was
  present — 2.1ms on 10MB, on a path model output routinely takes. Now 0.86µs.
- **Documentation corrections**, including a claim in `docs/USING.md` that pinned records
  are "never evicted by age" — the pinned store is bounded and evicts, as `pins_dropped`
  reports.

### Notes

- Distributed (cross-process) propagation is not implemented. The API seam exists; see
  [ADR 0001](docs/adr/0001-core-architecture.md).
- The package is published as `webrtrace` because `webr` is taken on PyPI by an unrelated
  project. The import name matches the distribution name; the project is called webR.
