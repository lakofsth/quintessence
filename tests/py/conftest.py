# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Keep the documented pytest gate out of the invoking user's home.

`python3 -m pytest tests/py -q` is one of the gates ASSURANCE.md names, and `QQ_CACHE` defaults
to `~/.cache/qq-search/embeddings.json`, so a direct run left `.lock` and `.orphan-ages.json`
files in a directory the suite does not own (ninth pass, P2 -- measured: four files from one
invocation).

`tests/run.sh` grew a scratch cache for exactly this reason one pass earlier, but its export
cannot reach an invocation that never goes through it, and conftest.py is pytest-only. So the
two runners each carry the guard in the place their own runner reads: this file for `pytest`,
and `tests/test-py.sh` for the `unittest discover` path.

Same rule as run.sh: fill in only what is missing, so a caller's own `QQ_CACHE` still wins.

The agent-session identity below is the one thing here that does NOT follow that rule -- see its
own note.
"""
import os
import shutil
import tempfile

_scratch = None

if not os.environ.get("QQ_CACHE"):
    _scratch = tempfile.mkdtemp(prefix="qq-pytest-cache-")
    os.environ["QQ_CACHE"] = os.path.join(_scratch, "embeddings.json")

# `qq update` derives a `[<model>, session <id8>]` marker into the update-lines it writes when the
# harness names a session in the environment (quintessence/agentid.py). This suite is routinely
# run BY an agent session, so leaving it set makes the bytes under test depend on who ran them.
# Unconditional, unlike QQ_CACHE above: a caller's QQ_CACHE is a preference, a caller's ambient
# session id is contamination. A test that wants a marker patches the variable for its own case.
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)


def pytest_sessionfinish(session, exitstatus):   # noqa: ARG001 - pytest's signature
    """Remove the scratch cache, and only if this file is what created it."""
    if _scratch:
        shutil.rmtree(_scratch, ignore_errors=True)
