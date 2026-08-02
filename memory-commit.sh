#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# SessionEnd hook — the atomic-fact MEMORY store is written with Edit/Write (never git), so without
# a commit trigger it accumulates uncommitted. This commits it at session end IF something changed,
# so facts are versioned at session boundaries. LOCAL COMMIT ONLY — pushing to an off-box mirror is
# a deployment-specific extension (add your own `git push` after this line if you keep a remote).
# Silent + fail-open; never blocks or fails the session. Memory is optional → no-op when unused.
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
. "$ENGINE/qq-config.sh"
m="${QQ_MEMDIR:-$HOME/.quintessence-memory}"
[ -d "$m/.git" ] || exit 0                                   # memory unused / not version-controlled
git -C "$m" add -A 2>/dev/null || exit 0
git -C "$m" diff --cached --quiet 2>/dev/null && exit 0      # nothing new → done
git -C "$m" commit -q -m "memory: auto-snapshot $(date -u +%FT%TZ)" 2>/dev/null || true
# The memory repo has no post-commit hook (it is not in the mirror
# set), so THIS is where its corpus rebind fires — same entry point the auto-mirror hook uses
# for docs. refs-resolve.py no-ops unless QQ_BIND_CORPUS names this directory, always exits 0,
# and logs its own errors — the fail-open contract of this hook is unchanged.
[ -x "$ENGINE/refs-resolve.py" ] && "$ENGINE/refs-resolve.py" commit --repo "$m" >/dev/null 2>&1
# (off-box push intentionally omitted — deployment-specific; see DESIGN-NOTES / the reference setup)
exit 0
