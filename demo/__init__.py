# Copyright 2026 Emilio Briones
# SPDX-License-Identifier: Apache-2.0
"""A small multi-agent application, runnable in three modes, for trying webR out.

    python -m demo --mode good
    python -m demo --mode silent
    python -m demo --mode fail

Not part of the published package. It exists so you can see what a trace looks like when
a run is healthy, when it is quietly wrong, and when it breaks loudly -- without needing
an API key, a network connection, or a real agent framework.
"""
