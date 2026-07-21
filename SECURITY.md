# Security Policy

## Supported versions

webR is pre-1.0. Security fixes are released against the latest published version only.

| Version | Supported |
|---|---|
| 0.1.x | Yes |
| < 0.1 | No |

## Reporting a vulnerability

Please report privately rather than opening a public issue: use GitHub's
[private vulnerability reporting](https://github.com/Milol0608/webR/security/advisories/new)
on this repository.

Include what you can — affected version, a reproduction, and what an attacker gains. You
should get an acknowledgement within a few days. This is a small project maintained by one
person, so please allow reasonable time for a fix before disclosing publicly.

## What is in scope

webR has no runtime dependencies, opens no sockets, and executes no remote code, so its
attack surface is narrow. Genuine concerns would look like:

- **Trace files leaking data they should not.** The most likely real problem in this
  library (see below).
- **A crafted payload causing unbounded memory or CPU.** Every scan is bounded and every
  buffer is capped, but a bypass would be a real bug.
- **Path handling in the writer** — the trace path is caller-supplied, but a way to make it
  write somewhere unintended would be worth reporting.
- **Anything that makes webR raise into a traced program** it otherwise would not. That is
  a correctness bug with availability consequences.

## Handling your data — read this before enabling full capture

webR is a debugging tool that records what passes through your agents. Understand what it
writes:

- **By default**, payloads are stored as a fingerprint: a length, a 64-bit hash, and the
  **first and last 200 characters**. Those excerpts are real content, and for a short
  prompt the "fingerprint" is the entire text.
- **With `capture_full=True`** or `WEBR_CAPTURE_FULL=1`, prompts and completions are written
  **verbatim**, capped at 8KB each. If your prompts contain customer data, credentials, or
  anything regulated, that data is now in a plaintext file on disk.
- **Trace files are not encrypted, redacted, or access-controlled.** They are line-delimited
  JSON with whatever permissions the filesystem gives them.

Practical guidance:

- Treat `traces/` as sensitive. The default `.gitignore` excludes `*.webrtrace.jsonl` and
  `traces/`, but check before committing anything.
- Prefer `capture=("prompt",)` to name the parameters you actually need rather than
  capturing every string argument.
- Use `capture=False` on nodes handling credentials or PII.
- Turn capture off entirely in production unless you are actively investigating:
  `webrtrace.set_capture(False)` or `WEBR_CAPTURE=0`.
- Scrub traces before attaching them to a bug report.

A redaction hook — a caller-supplied function applied to payloads before they are recorded
— is planned and not yet implemented. Until it exists, the controls above are what you have.
