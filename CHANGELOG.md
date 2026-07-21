# Changelog

Notable changes to webR. This project follows [Semantic Versioning](https://semver.org/);
while the major version is `0`, the public API may change between minor releases.

## [Unreleased]

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

### Notes

- Distributed (cross-process) propagation is not implemented. The API seam exists; see
  [ADR 0001](docs/adr/0001-core-architecture.md).
- The package is published as `webrtrace` because `webr` is taken on PyPI by an unrelated
  project. The import name matches the distribution name; the project is called webR.
