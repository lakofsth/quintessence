#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# Stop-hook SILENT housekeeping for quintessence. TWO once-per-session jobs, NEVER any nag/re-wake:
#   1) autosnapshot — if a HEAD has changes not yet in the journal, quietly snapshot it (exit 0).
#   2) Tier-1 consistency check — refresh the pending-findings queue so drift surfaces at the
#      NEXT SessionStart digest (deterministic + fast → fine at Stop; the heavy LLM audit does NOT
#      run here — see consistency-audit.sh / HEAD continuity-consistency).
# Reads hook JSON on stdin (.session_id). Keep it dumb. finalize = STATE completion, not thread.
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
. "$ENGINE/qq-config.sh"
qd="${QUINTESSENCE_DIR:-$HOME/quintessence}"
[ -d "$qd" ] || exit 0
# -H: dereference the starting point. `[ -d "$qd" ]` above follows a symlink, so a symlinked
# store passes the guard and then find's default -P silently refuses to descend it, tidying
# nothing forever. Same defect as tsk's orphan sweep (ledger D114).
find -H "$qd" -maxdepth 1 -name '.rewake-*'  -mtime +1 -delete 2>/dev/null   # tidy old sentinels
find -H "$qd" -maxdepth 1 -name '.checked-*' -mtime +1 -delete 2>/dev/null
sid=$(jq -r '.session_id // "nosession"' 2>/dev/null) || sid="nosession"

# 1) silent autosnapshot of any unfinalized HEAD (once/session, only if something is actually stale)
rsentinel="$qd/.rewake-$sid"
if [ ! -f "$rsentinel" ]; then
  stale=()
  for f in "$qd"/*.md; do
    [ -e "$f" ] || continue
    b=$(basename "$f" .md)
    case "$b" in RUBRIC|INDEX) continue;; esac
    newest=$(ls -t "$qd/journal/$b"/*.md 2>/dev/null | head -1)
    if [ -z "$newest" ] || [ "$f" -nt "$newest" ]; then stale+=("$b"); fi
  done
  if [ "${#stale[@]}" -gt 0 ]; then
    touch "$rsentinel"
    for b in "${stale[@]}"; do QQ_LOCK_WAIT=5 "$ENGINE/qq" finalize "$b" >/dev/null 2>&1; done
  fi
fi

# 2) Tier-1 consistency check → write findings to the queue for next SessionStart (once/session)
csentinel="$qd/.checked-$sid"
if [ ! -f "$csentinel" ]; then
  touch "$csentinel"
  QQ_LOCK_WAIT=5 "$ENGINE/qq" check --write >/dev/null 2>&1 || true
fi
exit 0
