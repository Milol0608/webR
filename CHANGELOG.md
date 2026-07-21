# Changelog

Notable changes to webR. This project follows [Semantic Versioning](https://semver.org/);
while the major version is `0`, the public API may change between minor releases.

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
