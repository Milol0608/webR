# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""Identifier generation.

Trace ids are 128-bit and node ids are 64-bit, rendered as lowercase hex. The widths
match W3C Trace Context so that a future cross-process propagator (v0.2) can emit a
`traceparent` header without a second id scheme.

A dedicated `random.Random` instance is used rather than `secrets`: ids need to be
collision-resistant, not unpredictable, and this runs on the hot path.
"""

from __future__ import annotations

import os
import random

_rand = random.Random()

# A forked child inherits the parent's RNG state verbatim, which would make both
# processes mint identical ids. Reseed on fork where the platform supports it.
if hasattr(os, "register_at_fork"):  # pragma: no cover - not reachable on Windows
    os.register_at_fork(after_in_child=lambda: _rand.seed(os.urandom(16)))


def new_trace_id() -> str:
    """Return a fresh 128-bit trace id as 32 hex characters."""
    return f"{_rand.getrandbits(128):032x}"


def new_node_id() -> str:
    """Return a fresh 64-bit node id as 16 hex characters."""
    return f"{_rand.getrandbits(64):016x}"
