## What this changes

<!-- One paragraph. What is different after this PR, and why. -->

## Why

<!-- The problem being solved. Link the issue if there is one. -->

## Checklist

- [ ] One concern — this is not a fix and a refactor in the same diff
- [ ] Tests added; for a bug fix, a test that **fails before and passes after** (say which)
- [ ] `python -m pytest` passes
- [ ] `python -m ruff check .` and `python -m ruff format --check .` pass
- [ ] Docs updated in this PR if behaviour changed (`docs/USING.md`, `docs/INTERNALS.md`)
- [ ] Conventional Commit messages

## The rules this must not break

<!-- Delete any that plainly do not apply. -->

- [ ] Tracing does not change what the traced program does — no new exceptions, no altered
      return values or signatures
- [ ] Any structure that grows has a ceiling and an eviction policy
- [ ] No I/O, no unbounded scans, and no locks held across user code on the hot path
- [ ] The trace still reports its own gaps honestly (`dropped`, `dangling_edges`)
- [ ] No new runtime dependencies

## Performance

<!-- If you touched the decorator, detectors, buffer, or writer, paste
     `python benchmarks/overhead.py` before and after. Otherwise write "not applicable". -->
