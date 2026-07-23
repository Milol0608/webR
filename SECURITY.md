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
  **first and last 200 characters**. Those excerpts are real content, and for a prompt
  under 400 characters the "fingerprint" is the entire text, stored verbatim.
- **Detector signals can also quote the payload.** `novel_numbers` copies the figures it
  found — which is the point, but it means an account balance or a date can appear in
  `signals` even when you were thinking only about `io`. `refusal` records the matched
  phrase.
- **To keep detection but store nothing readable**, disable text capture. Lengths and
  hashes are kept, detectors still run against the in-memory text, and value-bearing
  signals are reduced to counts:

  ```python
  webrtrace.set_capture(True, text=False)          # process-wide
  @webrtrace.webR_node(capture_text=False)         # or per node
  ```

  This is the setting for data you are not permitted to retain. The default is **not** it.
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
  `webrtrace.set_capture(False)` or `WEBR_CAPTURE=0`. To keep hallucination detection
  without storing text, prefer `set_capture(True, text=False)` / `WEBR_CAPTURE_TEXT=0`.
- Trace files are written per process by default (`traces/webrtrace-<pid>.jsonl`). If you
  pass an explicit path, keep it unique per process — two writers on one file corrupt it.
- Scrub traces before attaching them to a bug report.

## Redaction

A redactor runs on payload text **before** it is fingerprinted, before the detectors see
it, and before anything reaches memory or disk:

```python
import webrtrace

webrtrace.set_redactor(webrtrace.common_secrets)          # process-wide

@webrtrace.webR_node(redact=my_scrubber)                  # or per node
def handle(prompt: str) -> str: ...
```

It scrubs **inputs, outputs, and the message and traceback of any exception** — a provider
SDK echoing the failing request into an error ("401 for `api_key=sk-...`") is ordinary, so
the error path is redacted on the same terms as any other payload.

**It fails closed.** If your redactor raises or returns a non-string, the payload is
*discarded* rather than recorded, and `redaction_failed` appears in the node's signals. Any
other behaviour would mean the one input that breaks your redactor is the one input written
out in full.

`common_secrets` is a **floor, not a guarantee.** It matches structurally distinctive
secrets — API keys, bearer tokens, JWTs, AWS key ids, `password:` assignments, email
addresses, card-length digit runs. It will not catch a customer's name, an address, or a
medical detail, and it will occasionally redact something harmless. If you have a real
regulatory obligation, write a redactor for your own data and pass that instead.

One consequence worth knowing: because redaction happens before hashing, two payloads
differing only in redacted content hash identically. The hash then answers "did the
non-secret part change", which is usually what you want.
