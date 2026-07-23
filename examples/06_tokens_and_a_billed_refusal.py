# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Token accounting, a refusal that cost money, and an embedder that quietly died.

    python examples/06_tokens_and_a_billed_refusal.py

No API key and no network: `FakeAnthropic` below has the same shape as the real client --
`messages.create(...)` returning an object with `usage`, `model`, and `stop_reason` -- which
is the whole point of webR reading provider responses by duck typing rather than by import.
Swap it for `anthropic.Anthropic()` and nothing else changes.

Two failures here raise nothing at all:

* the second call comes back with `stop_reason="refusal"` -- HTTP 200, empty content, and
  you were billed for the input tokens anyway;
* `embed()` returns a 16-dimensional vector of zeros, which is the right *shape*, so the
  pipeline carries on and retrieval returns garbage two stages later.
"""

from __future__ import annotations

import webrtrace
from webrtrace import webR_node

# --- a stand-in for the Anthropic SDK ------------------------------------------------


class FakeUsage:
    def __init__(self, inp: int, out: int, cache_read: int = 0) -> None:
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = cache_read


class FakeMessage:
    def __init__(self, text: str, stop_reason: str, usage: FakeUsage) -> None:
        self.content = [type("Block", (), {"type": "text", "text": text})()]
        self.stop_reason = stop_reason
        self.stop_details = None
        self.model = "claude-opus-4-8"
        self.usage = usage


RESPONSES = [
    FakeMessage("North region: 400 customers.", "end_turn", FakeUsage(1_204, 38)),
    # A safety decline. Nothing raises. You are billed for the input.
    FakeMessage("", "refusal", FakeUsage(1_190, 0)),
    FakeMessage("West region: 300 customers.", "end_turn", FakeUsage(1_198, 41, cache_read=1_100)),
]


class FakeMessages:
    def __init__(self) -> None:
        self._next = 0

    def create(self, **kwargs: object) -> FakeMessage:
        response = RESPONSES[self._next % len(RESPONSES)]
        self._next += 1
        return response


class FakeAnthropic:
    def __init__(self) -> None:
        self.messages = FakeMessages()


# --- the pipeline ---------------------------------------------------------------------

client = webrtrace.instrument(FakeAnthropic())


@webR_node(name="embed")
def embed(text: str) -> list[float]:
    # A provider call that failed in a way that returns a correctly-shaped vector. No
    # text output, so the lexical detectors have nothing to read -- the value pass runs
    # instead and reports all_zeros.
    return [0.0] * 16


@webR_node(name="summarize_region")
def summarize_region(region: str) -> str:
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=1024,
        messages=[{"role": "user", "content": f"Summarize sales for {region}"}],
    )
    blocks = response.content
    return blocks[0].text if blocks and blocks[0].text else ""


@webR_node(name="report")
def report() -> str:
    summaries = [summarize_region(r) for r in ("north", "east", "west")]
    embed(" ".join(summaries))
    return " | ".join(s for s in summaries if s)


def main() -> None:
    answer = report()

    print("What your program saw:")
    print(f"  no exceptions, final answer = {answer!r}")
    print("  three calls made, three calls returned.\n")

    web = webrtrace.export_graph()
    print("What webR saw:")
    print(webrtrace.render(web))

    print("\nPer-call usage:")
    billed = 0
    for node in web["nodes"]:
        usage = node.get("usage")
        if not usage:
            continue
        total = (
            usage.get("input_tokens", 0)
            + usage.get("output_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        billed += total
        stop = usage.get("stop_reason")
        print(f"  {node['name']:<26} {total:>6} tokens   stop_reason={stop}")
    print(f"  {'':<26} {billed:>6} tokens total")

    print("\nThe two silent failures:")
    print("  the refusal      -- a successful, billed call that returned nothing at all.")
    print("                      1,190 input tokens spent on an empty answer.")
    print("  the dead embed   -- 16 zeros. Correct shape, no information, and every")
    print("                      similarity score downstream is now meaningless.")
    print("\nOnly the refusal is marked [SUS] by default. all_zeros informs rather than")
    print("accuses, because an empty or zero result is legitimately correct often enough")
    print("that flagging it everywhere would train you to ignore the flag. To promote it:")
    print('  webrtrace.set_suspect_signals("nan", "infinite", "all_zeros", "refusal")')
    print("\nwebR reports tokens, never dollars. Prices change and vary by contract, so a")
    print("hardcoded price table would be wrong the week after it shipped -- and quietly.")


if __name__ == "__main__":
    main()
