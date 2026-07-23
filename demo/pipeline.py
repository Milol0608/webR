# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""A support-ticket triage pipeline: five agents, three ways for a run to go.

The shape is deliberately ordinary -- fetch, summarise, embed, route, report -- because
the point is that nothing here looks unusual. In `silent` mode every function returns, no
exception is raised, and the final answer is wrong.

The three modes:

* `good`   -- everything works. The baseline you compare against.
* `silent` -- **the mode webR exists for.** A model refuses one ticket, the embedder
              returns a dead vector, and a defensive `except` substitutes a fallback.
              Zero exceptions. A confident, wrong report.
* `fail`   -- something raises. Easy mode: any tool finds this. Included so you can see
              what an ordinary failure looks like next to a silent one.
"""

from __future__ import annotations

import webrtrace
from webrtrace import webR_node

from .provider import FakeAnthropic, refusal, reply, truncated

TICKETS = {
    "T-1001": "Payment failed twice on checkout, card was charged anyway.",
    "T-1002": "Cannot reset password, the email never arrives.",
    "T-1003": "Export to CSV truncates at 500 rows.",
    "T-1004": "Billing shows 3 duplicate charges for invoice 88214.",
}

_SCRIPTS = {
    "good": {
        "T-1001": reply("Duplicate charge on a failed payment. Severity: high."),
        "T-1002": reply("Password reset emails not delivered. Severity: medium."),
        "T-1003": reply("CSV export truncates at 500 rows. Severity: low.", cache_read=1_100),
        "T-1004": reply("Three duplicate charges on invoice 88214. Severity: high."),
    },
    "silent": {
        # The poison. A safety decline: successful, billed, and empty.
        "T-1001": refusal(),
        "T-1002": reply("Password reset emails not delivered. Severity: medium."),
        # Cut off mid-sentence, then passed on as if it were the whole answer.
        "T-1003": truncated("CSV export truncates at 500 rows because the pagination"),
        "T-1004": reply("Three duplicate charges on invoice 88214. Severity: high."),
    },
    "fail": {
        "T-1001": reply("Duplicate charge on a failed payment. Severity: high."),
        "T-1002": reply("Password reset emails not delivered. Severity: medium."),
        "T-1003": reply("CSV export truncates at 500 rows. Severity: low."),
        "T-1004": reply("Three duplicate charges on invoice 88214. Severity: high."),
    },
}


class Pipeline:
    """The application. One instance per run, holding the mode and its provider client."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        # One line turns every provider call into a node with model, tokens, and
        # stop_reason. Replace FakeAnthropic with anthropic.Anthropic() and nothing else
        # in this file changes.
        self.client = webrtrace.instrument(FakeAnthropic(_SCRIPTS[mode]))

    # --- the agents ------------------------------------------------------------------

    @webR_node(name="fetch_ticket", attributes={"tier": "storage"})
    def fetch_ticket(self, ticket_id: str) -> str:
        if self.mode == "fail" and ticket_id == "T-1003":
            # An ordinary bug. Loud, obvious, and the easiest kind to find.
            raise ConnectionError(f"ticket store unreachable while reading {ticket_id}")
        return TICKETS[ticket_id]

    @webR_node(name="summarize")
    def summarize(self, ticket_id: str, body: str) -> str:
        response = self.client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1024,
            messages=[{"role": "user", "content": f"Summarize ticket {ticket_id}: {body}"}],
        )
        blocks = response.content
        return blocks[0].text if blocks else ""

    @webR_node(name="embed")
    def embed(self, text: str) -> list[float]:
        """Returns a vector, so the lexical detectors have nothing to read.

        In `silent` mode the provider call fails in the way embedding services actually
        fail: it returns a correctly-shaped vector of zeros. The pipeline carries on and
        every similarity score downstream is meaningless.
        """
        if self.mode == "silent" and not text.strip():
            return [0.0] * 16
        return [round(0.1 * ((i + len(text)) % 7), 3) for i in range(16)]

    @webR_node(name="route", check=lambda queue: queue != "unrouted" or "nothing to route")
    def route(self, summary: str, vector: list[float]) -> str:
        """Picks a queue. The validator flags the fallback rather than raising on it."""
        if not summary.strip():
            return "unrouted"
        if "high" in summary.lower():
            return "escalation"
        return "standard"

    @webR_node(name="triage_ticket")
    def triage_ticket(self, ticket_id: str) -> dict[str, object]:
        try:
            body = self.fetch_ticket(ticket_id)
        except ConnectionError:
            # Defensive code doing exactly what it was told to do. This is the line that
            # turns a loud failure into a quiet one -- and it is not a bug.
            body = ""
        summary = self.summarize(ticket_id, body)
        vector = self.embed(summary)
        queue = self.route(summary, vector)
        return {"ticket": ticket_id, "queue": queue, "summary": summary}

    @webR_node(name="triage_report")
    def run(self) -> str:
        results = [self.triage_ticket(ticket_id) for ticket_id in TICKETS]
        escalations = sum(1 for r in results if r["queue"] == "escalation")
        return (
            f"Triaged {len(results)} tickets: {escalations} escalated, "
            f"{len(results) - escalations} standard."
        )
