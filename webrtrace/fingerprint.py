# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Bounded summaries of payload text.

A trace that records "node `planner` ran, 2.3s, ok" says nothing about a hallucination.
A trace that stores every prompt and completion is hundreds of megabytes and cannot be
kept in memory. A fingerprint is the middle: constant-ish size, and enough to answer the
question that actually matters -- *where did the content change, and into what shape*.

The hash is the quietly powerful part. Identical hashes across two nodes prove the text
passed through untouched; the first node whose hash differs is where the content was
rewritten. That is answerable in 16 bytes.

blake2b is used rather than sha256 because this is a change-detector, not a security
boundary: 64 bits is ample for spotting mutation, and it is roughly twice as fast.
"""

from __future__ import annotations

from hashlib import blake2b
from typing import Any

#: Characters kept from each end in fingerprint mode. Enough to see the shape of the
#: content -- a JSON opening, a refusal, a truncation -- without storing the payload.
HEAD_TAIL_CHARS = 200

#: Ceiling on stored text in full-capture mode.
MAX_FULL_CHARS = 8_192


def _digest(text: str) -> str:
    return blake2b(text.encode("utf-8", "replace"), digest_size=8).hexdigest()


def fingerprint(text: str, *, full: bool = False, store_text: bool = True) -> dict[str, Any]:
    """Summarize one payload.

    Args:
        text: The payload.
        full: Store the text itself, capped at `MAX_FULL_CHARS`. Off by default because
            full capture is what turns a trace file into a liability.
        store_text: When False, store only the length and hash -- no text, no head, no
            tail. Detectors still run (they see the text in memory), so hallucination
            signals survive while nothing readable is persisted. This is the setting for
            payloads you may not retain at all; the default is *not* it, because a short
            payload is stored in full and a long one keeps its first and last 200
            characters.
    """
    length = len(text)
    summary: dict[str, Any] = {"len": length, "hash": _digest(text)}

    if not store_text:
        if length > HEAD_TAIL_CHARS * 2:
            summary["truncated"] = True
        return summary

    if full:
        if length > MAX_FULL_CHARS:
            summary["text"] = text[:MAX_FULL_CHARS]
            summary["truncated"] = True
        else:
            summary["text"] = text
        return summary

    if length <= HEAD_TAIL_CHARS * 2:
        summary["text"] = text
    else:
        summary["head"] = text[:HEAD_TAIL_CHARS]
        summary["tail"] = text[-HEAD_TAIL_CHARS:]
        summary["truncated"] = True
    return summary


def as_text(value: Any) -> str | None:
    """Return the payload as text if it is one, else None.

    Accepts `str` and `bytes` and their subclasses -- `enum.StrEnum` members, framework
    string types like `markupsafe.Markup`, Pydantic constrained strings. An earlier
    version used `type(value) is str`, which silently gave those *no* capture and *no*
    detection, so a hallucination inside a `StrEnum`-typed field was invisible.

    Subclasses are snapshotted to a plain `str`/`bytes` immediately (`str(value)`), which
    is the point of ADR 0002: the fingerprint must reflect the value *now*, and a subclass
    could be lazy or mutable. Everything that is not text is ignored rather than coerced --
    `repr()`-ing an arbitrary object into a trace is how tracing libraries end up
    serializing a database connection.
    """
    if isinstance(value, str):
        return str(value)  # snapshot: a StrEnum/Markup becomes a plain, frozen str
    if isinstance(value, bytes):
        return bytes(value).decode("utf-8", "replace")
    return None
