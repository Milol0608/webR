# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Scrubbing payloads before they are recorded.

webR writes what passes through your agents, and what passes through agents is prompts:
customer names, account numbers, API keys pasted into a system message. A redactor runs
on the payload text **before** it is fingerprinted, before the detectors see it, and
before anything reaches memory or disk.

The single most important property here is that redaction **fails closed**. If a redactor
raises, the payload is discarded rather than recorded. Any other choice means the one time
your redactor hits an edge case is the one time the unredacted data lands in a file.

Redaction happens before hashing, so two payloads that differ only in redacted content
hash identically. That is a real limitation: the hash then answers "did the non-secret
part change", which is usually what you want and is worth knowing about.
"""

from __future__ import annotations

import re
from collections.abc import Callable

#: A redactor takes payload text and returns it with sensitive spans replaced.
Redactor = Callable[[str], str]

REDACTED = "[REDACTED]"

# Deliberately narrow. Each pattern targets a *structurally distinctive* secret -- one
# whose shape is unlikely to occur in ordinary prose. Broad heuristics that try to catch
# names or addresses produce false confidence, which is worse than no redactor at all.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Provider-style API keys: sk-..., pk_live_..., ghp_..., xoxb-...
    re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj)?[-_]?[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # AWS access key ids
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    # Authorization headers
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*"),
    # JWTs
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    # "password": "...", api_key=..., token: ...
    re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
        r"\b\s*[:=]\s*[\"']?[^\s\"',;]{6,}"
    ),
    # Email addresses
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    # Long digit runs that look like card or account numbers
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),
)


def common_secrets(text: str) -> str:
    """Replace structurally distinctive secrets: API keys, tokens, emails, card numbers.

    **This is not a PII scrubber and must not be treated as compliance tooling.** It
    catches things with a recognisable shape. It will not catch a customer's name, a
    physical address, a medical detail, or an account number written in prose -- and it
    will occasionally redact something harmless that happens to match.

    Use it as a floor, not a guarantee. If you have a real obligation, write a redactor
    for your own data and pass that instead.
    """
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def apply(text: str, redactor: Redactor | None) -> str | None:
    """Redact `text`, or return None if it must not be recorded at all.

    None means "drop this payload": the redactor raised, or returned something that is
    not text. Failing closed is the entire point -- a redactor that breaks on an unusual
    input must not thereby cause that exact input to be written out in full.
    """
    if redactor is None:
        return text
    try:
        redacted = redactor(text)
    except Exception:
        return None
    if not isinstance(redacted, str):
        return None
    return redacted
