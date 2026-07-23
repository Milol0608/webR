# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""A fake model provider with the same response shape as the Anthropic SDK.

Swap `FakeAnthropic()` for `anthropic.Anthropic()` and the rest of the demo is unchanged:
webR reads provider responses by duck typing, never by importing the SDK, so anything with
`usage`, `model`, and `stop_reason` is understood.

Responses are scripted per mode so each run is deterministic and you can read exactly what
produced each node.
"""

from __future__ import annotations

from typing import Any

MODEL = "claude-opus-4-8"


class FakeUsage:
    __slots__ = (
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "input_tokens",
        "output_tokens",
    )

    def __init__(self, inp: int, out: int, cache_read: int = 0, cache_write: int = 0) -> None:
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_write


class _Block:
    __slots__ = ("text", "type")

    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeMessage:
    __slots__ = ("content", "model", "stop_details", "stop_reason", "usage")

    def __init__(self, text: str, stop_reason: str, usage: FakeUsage) -> None:
        self.content = [_Block(text)] if text else []
        self.stop_reason = stop_reason
        self.stop_details = None
        self.model = MODEL
        self.usage = usage


def reply(text: str, *, inp: int = 1_180, out: int = 60, cache_read: int = 0) -> FakeMessage:
    return FakeMessage(text, "end_turn", FakeUsage(inp, out, cache_read=cache_read))


def refusal(*, inp: int = 1_190) -> FakeMessage:
    """A safety decline: HTTP 200, empty content, nothing raised, and you were billed."""
    return FakeMessage("", "refusal", FakeUsage(inp, 0))


def truncated(text: str, *, inp: int = 1_180, out: int = 4_096) -> FakeMessage:
    """An answer cut off mid-thought and passed downstream as though complete."""
    return FakeMessage(text, "max_tokens", FakeUsage(inp, out))


class _Messages:
    """Serves a scripted response per prompt, falling back to a generic reply."""

    def __init__(self, script: dict[str, FakeMessage]) -> None:
        self._script = script
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeMessage:
        self.calls.append(kwargs)
        prompt = str(kwargs.get("messages", [{}])[-1].get("content", ""))
        for key, response in self._script.items():
            if key in prompt:
                return response
        return reply("Acknowledged.")


class FakeAnthropic:
    def __init__(self, script: dict[str, FakeMessage] | None = None) -> None:
        self.messages = _Messages(script or {})
