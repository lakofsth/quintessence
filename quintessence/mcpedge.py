# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.mcpedge — the checks the two MCP faces apply at their edge, in one importable
place so they are pinned by unit tests rather than by exec'ing the scripts.

Three concerns (posture review 2026-10-08):

* `check_passthrough` — the stdio server's `qq` escape hatch forwards an argv list to the CLI.
  The CLI's own surface includes `--ref probe:<cmd>`, which runs `bash -c <cmd>` at write time
  and again on every sweep (M1), and `config set`, which rewrites the install's config (M2/M3).
  An MCP tool that a client has allowlisted must not be a second, prompt-free road to those.
  The verb allowlist is the verbs the tool's own docstring advertises plus the read verbs.
* `clamp_k` — the hosted face's `k` was unbounded (L5); a bulk pull of policy-filtered results
  is still a bulk pull.
* `strip_name_hints` — `qq brief` on a near-miss topic appends "Did you mean: …" drawn from
  every slug in the store, including ones the remote deny lists withhold (M7). The hosted face
  drops the hint rather than filtering it: a name the caller is not allowed to see is not a
  hint it should get.

INVARIANTS.md: the-mcp-passthrough-cannot-reach-a-shell, the-remote-face-names-only-what-policy-allows."""
from __future__ import annotations

import re
from typing import Optional, Sequence

# Verbs the passthrough may forward. Absent on purpose: config, init, reconcile (admin surface),
# rewrite (reads the whole HEAD from stdin, which the server nulls), search/ask (dedicated tools).
PASSTHROUGH_VERBS = frozenset({
    "menu", "index", "list", "digest", "path", "fact", "findings", "brief", "show", "load",
    "doctor", "check", "new", "essence", "update", "finalize", "save", "checkpoint", "compact",
    "delete", "rm", "reindex", "waveoff", "help",
})

K_MIN, K_MAX = 1, 50

_HINT_RE = re.compile(r"[ \t]*Did you mean: [^\n]*\?")


def check_passthrough(args: Sequence[str]) -> Optional[str]:
    """None when `args` may be forwarded to `qq`; otherwise the one-line refusal to return to
    the caller. A `probe:` reference is refused in flag position only — after `--` it is the
    update's literal text, which the CLI never interprets."""
    if not args:
        return "[qq refused] no subcommand given (e.g. args=[\"menu\"])"
    verb = args[0]
    if verb not in PASSTHROUGH_VERBS:
        return (f"[qq refused] verb '{verb}' is not available through this tool "
                f"(allowed: {', '.join(sorted(PASSTHROUGH_VERBS))})")
    i = 1
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a == "--ref" and i + 1 < len(args) and args[i + 1].startswith("probe:"):
            return "[qq refused] a probe: reference runs a shell command; bind it from a terminal"
        if a.startswith("--ref=") and a[len("--ref="):].startswith("probe:"):
            return "[qq refused] a probe: reference runs a shell command; bind it from a terminal"
        i += 1
    return None


def clamp_k(k) -> int:
    """`k` as the hosted face will use it: an int in [K_MIN, K_MAX]; junk reads as K_MIN."""
    try:
        n = int(k)
    except (TypeError, ValueError):
        return K_MIN
    return max(K_MIN, min(K_MAX, n))


def strip_name_hints(text: str) -> str:
    """Remove `qq brief`/`qq show`'s "Did you mean: a, b?" suggestions from an outward-bound
    rendering. The hint sits on the "no HEAD" line after the closing dashes."""
    return _HINT_RE.sub("", text)
