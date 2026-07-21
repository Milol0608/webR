# ADR 0002 — Detection runs inline, not on the writer thread

- **Status:** Accepted
- **Date:** 2026-07-20
- **Amends:** [ADR 0001](0001-core-architecture.md)

## Context

ADR 0001 stated that payload fingerprints and hallucination signals would be computed on
the background writer thread, keeping the traced call's hot path free of any O(n) work.

Implementing milestones 2 and 3 established two facts that make that impossible:

1. **`NodeRecord` is frozen and emitted immediately.** It reaches the buffer -- and any
   caller reading the buffer -- the moment the node completes. Computing `io` and
   `signals` later would mean either mutating a record other threads can already see, or
   maintaining a second index of amendments and reconciling it at export. Both destroy
   the property that makes the record type safe to hand across threads at all.
2. **Deferring detection means retaining payloads.** The detection stage needs the actual
   prompt and response text. Deferring it means holding references to every payload until
   the worker catches up -- exactly the unbounded memory growth the buffer design exists
   to prevent, and worse, it would fingerprint the *later* state of any mutable payload.

The user has also chosen capture-on-by-default, which raises the stakes: this cost is now
paid by every node unless explicitly disabled.

## Decision

Fingerprinting and detection run **inline**, in the decorator, before the record is
constructed.

To keep that defensible:

- **Only `str` and `bytes` payloads are captured** by default. They are immutable, so the
  fingerprint cannot drift, and they are what LLM agents actually pass. Other types are
  ignored unless named explicitly.
- **Every built-in detector is a single pass over the text**, with no model, no network,
  and no dependency. The whole detection stage is O(n) in payload size with a small
  constant.
- **Detection is skipped entirely when there is nothing to capture**, so a node that takes
  and returns non-string values pays only a type check.
- **Capture is switchable** globally (`webr.set_capture`) and per node
  (`@webR_node(capture=False)`), so the zero-overhead path remains available to anyone who
  needs it.

## Measured cost

Microseconds of overhead per traced call, above the undecorated function, by payload size
in characters (Python 3.14, Windows):

| payload | capture off | capture on |
|--------:|------------:|-----------:|
| 0 | 4.5 | 10.7 |
| 100 | 4.5 | 21.4 |
| 1,000 | 4.5 | 82.5 |
| 10,000 | 4.5 | 199.5 |
| 100,000 | 4.5 | 395.5 |

Three findings from taking the measurement rather than assuming it:

1. The first implementation cost **6.4ms** on a 100KB payload, sixteen times the figure
   above. The cause was capping detector work with `findall(text)[:n]`, which scans the
   entire string and then discards the tail -- a bound on memory that is no bound at all
   on time. Every limit now applies to the text *before* the scan.
2. Replacing `findall` with a `finditer`/`islice` generator made it *worse*. Per-match
   Python-level generator overhead exceeded the C-level scan it was avoiding.
3. Capture cost does not go flat with payload size, and cannot: the content hash is O(n)
   over the whole payload by definition. Everything else is bounded.

## Consequences

- The honest overhead claim changes from "a context lookup and two clock reads" to "that,
  plus one pass over any string payload". For an LLM-bound node this is invisible -- a few
  microseconds against a multi-second network call. For a hot non-LLM function called in a
  tight loop it is not, and such nodes should set `capture=False`.
- Milestone 6's benchmark must measure both paths: capture off, and capture on across a
  range of payload sizes. Publishing only the favourable number would be dishonest.
- The `Detector` protocol remains public and pluggable, but a detector that is *not* cheap
  -- an embedding model, an LLM judge -- must not be run inline. Supporting those requires
  a genuine post-hoc analysis stage that re-reads the JSONL file, which is future work and
  is deliberately not attempted here.
