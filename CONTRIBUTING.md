# Contributing to webR

Thanks for looking. This is a small, dependency-free library with strong opinions about
what it will and will not do, so the most useful thing you can do before writing code is
open an issue and check that the change fits.

## Setup

```bash
git clone https://github.com/Milol0608/webR
cd webR
python -m pip install -e ".[dev]"

python -m pytest              # the suite
python -m pytest -m perf      # wall-clock assertions; run these on a quiet machine
python -m ruff check .
python -m ruff format .
```

## The rules this library is built on

A change that breaks one of these will be rejected regardless of how useful it is, so they
are worth reading first.

1. **Tracing must never change what the traced program does.** No swallowed exceptions, no
   altered return values, no changed signatures, no new failure modes. If webR can raise
   somewhere it previously could not, that is a bug. Everything that renders user data —
   `str(exc)`, `repr()`, `json.dumps` — is already defended for this reason.
2. **Memory is bounded by construction.** Every structure that grows must have a ceiling
   and a documented eviction policy. Two leaks have already shipped and been caught by
   tests; that is why those tests reach into private attributes.
3. **Nothing expensive on the hot path.** No I/O, no locks held across user code, no
   unbounded scans. If a limit exists, it must bound *time*, not just memory — see
   [ADR 0002](docs/adr/0002-inline-detection.md) for the 16x regression caused by getting
   that wrong.
4. **The trace never lies about itself.** If records were dropped, say how many. If an edge
   points at a node that is gone, mark it dangling. A trace that implies completeness it
   does not have is worse than one that admits the gap.
5. **Zero runtime dependencies.** Development dependencies are fine. A debugging library
   that drags packages into someone's environment is one they decline to add.

## Pull requests

- **One concern per PR.** A bug fix and a refactor in the same diff is two PRs.
- **A bug fix needs a test that fails before it and passes after.** Say so in the
  description.
- **Conventional Commits** for messages: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`,
  `perf:`, `build:`, `ci:`, `chore:`. Breaking changes get a `!` and a `BREAKING CHANGE:`
  footer.
- **CI must be green** — ruff, format, and the suite across Python 3.10–3.14 on Linux,
  Windows, and macOS.
- **Update the docs in the same PR** if you change behaviour. `docs/USING.md` for anything
  user-facing, `docs/INTERNALS.md` for mechanisms.

## Adding a detector

Detectors are the most likely contribution, and the bar is specific:

- Cheap: single pass, bounded by `MAX_CHARS_SCANNED` / `MAX_WORDS_SCANNED`, no I/O, no
  model, no dependency.
- Use the cached properties on `Payloads` (`output_words`, `input_numbers`, …) rather than
  re-tokenizing.
- Return `None` when there is nothing to say. A detector that always fires is noise.
- Default to **informational**. A signal only belongs in `DEFAULT_SUSPECT_SIGNALS` if
  firing almost always means something is genuinely wrong. `novel_numbers` is deliberately
  not in that set — a node that computes a total is *supposed* to produce a new figure, and
  flagging it by default would train people to ignore the flags.

Anything requiring an embedding model or an LLM judge cannot run inline. That needs a
post-hoc analysis stage over the JSONL file, which does not exist yet — open an issue
before starting.

## Architecture decisions

Significant changes get an ADR in `docs/adr/`. Existing ones record the reasoning
*including where it turned out to be wrong*, which is the point — ADR 0002 exists because
ADR 0001 was mistaken about where detection could run.

## Reporting bugs

A trace of the failure is the most useful thing you can attach:

```python
webrtrace.start_writer("traces/bug.jsonl")
```

Please scrub it first — prompts contain real data, and `capture_full=True` writes them
verbatim.
