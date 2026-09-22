"""Serialisation of state-mutating tool calls (inner-process layer).

One process-wide asyncio.Lock held across the whole child-process run of any
tool whose wrapped CLI writes tools/.rm_state.json (rm_push_reading, rm_pull,
rm_capture_todos) or its own snapshot (rm_diff). It stops
two long CLI runs racing inside one server process without blocking the
event loop.

Cross-PROCESS safety is the substrate's job since v2: rm_config has atomic
state writes plus the tools/.rm_state.lock file lock, taken by the CLIs
around their state mutations and by rm_config.update_state /
record_project_push for the server's direct /Projects push records. The old
invariant "CLIs are the sole writers of .rm_state.json" is retired.

Lock order is always asyncio lock -> file lock (the file lock is only ever
taken inside to_thread workers or child CLI processes), so the pair cannot
deadlock. A busy file lock surfaces as a StateLockTimeout with exit code 3
from the CLIs, classified to the state_locked remedy.
"""

from __future__ import annotations

import asyncio

STATE_LOCK = asyncio.Lock()
