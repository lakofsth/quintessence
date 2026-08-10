#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
#
# Real concurrent-start race harness: N truly-parallel `tsk run <same-name>` invocations
# per iteration. Asserts (a) exactly one winner, (b) every loser refuses with a known
# message, (c) the command-substitution signature is absent — no loser's marker in the
# winner's log. Retained from the 2026-08-10 verification rounds (see tests/stress/README.md):
# 1,140 iterations / 0 failures against the flock fix; the pre-fix defect showed a loser's
# command silently executing under the winner's name (41/1300 at ambient load).
#
# Contention lane: spawns real systemd --user units and parallel processes — idle box only.
# Usage: stress-concurrent-start.sh [iterations] [concurrent-starters]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
TSK="${TSK:-$HERE/../../tsk}"
D=$(mktemp -d "${TMPDIR:-/tmp}/tsk-stress-conc.XXXXXX")
trap 'rm -rf "$D"' EXIT
export HOME="$D/home"; mkdir -p "$HOME"
ITERS="${1:-300}"
N="${2:-3}"
# Failure evidence goes OUTSIDE the trap-removed workdir — a captured failure that the
# harness's own cleanup deletes is the discarded-assertion lesson all over again.
FAILDIR="${STRESS_FAILDIR:-$PWD/tsk-stress-failures.$$}"
total_fail=0; subst_fail=0; refusal_fail=0

for i in $(seq 1 "$ITERS"); do
  TMP=$(mktemp -d "$D/crun.XXXXXX")
  export TSK_STATE="$TMP/state"; mkdir -p "$TSK_STATE"
  J="conc$$-$i"
  pids=(); outs=()
  for k in $(seq 1 "$N"); do
    out="$TMP/out_$k.log"; outs+=("$out")
    ( "$TSK" run "$J" -- echo "MARKER_$k" ) >"$out" 2>&1 &
    pids+=("$!")
  done
  for p in "${pids[@]}"; do wait "$p"; done

  winners=(); losers_bad=0
  for idx in "${!outs[@]}"; do
    k=$((idx+1))
    if grep -q "^started tsk-" "${outs[$idx]}"; then
      winners+=("$k")
    elif ! grep -qE "already running|start already in flight" "${outs[$idx]}"; then
      losers_bad=$((losers_bad+1))
    fi
  done

  # fast poll instead of tsk wait (which sleeps in 1-2s increments) — the job is a
  # near-instant echo, so a tight loop with a short cap suffices.
  waited=0
  while [ ! -f "$TSK_STATE/$J.done" ] && [ "$waited" -lt 300 ]; do sleep 0.02; waited=$((waited+1)); done
  logcontent=$(cat "$TSK_STATE/$J.log" 2>/dev/null)

  iter_bad=0
  if [ "${#winners[@]}" -ne 1 ]; then
    iter_bad=1
    echo "iter $i: winner count = ${#winners[@]} (expected 1)" >> "$TMP/verdict.log"
  fi
  if [ "$losers_bad" -gt 0 ]; then
    iter_bad=1; refusal_fail=$((refusal_fail+1))
    echo "iter $i: $losers_bad loser(s) did not refuse with the expected message" >> "$TMP/verdict.log"
  fi
  if [ "${#winners[@]}" -eq 1 ]; then
    wk="${winners[0]}"
    if ! printf '%s' "$logcontent" | grep -q "MARKER_$wk"; then
      iter_bad=1; subst_fail=$((subst_fail+1))
      echo "iter $i: winner's own marker (MARKER_$wk) missing from log" >> "$TMP/verdict.log"
    fi
    for k in $(seq 1 "$N"); do
      if [ "$k" != "$wk" ] && printf '%s' "$logcontent" | grep -q "MARKER_$k"; then
        iter_bad=1; subst_fail=$((subst_fail+1))
        echo "iter $i: SUBSTITUTION — loser's marker MARKER_$k found in log (winner was $wk)" >> "$TMP/verdict.log"
      fi
    done
  fi

  if [ "$iter_bad" -ne 0 ]; then
    total_fail=$((total_fail+1))
    dest="$FAILDIR/fail-$(printf '%04d' "$i")-$$"; mkdir -p "$dest"
    cp "$TMP"/out_*.log "$dest/" 2>/dev/null; cp "$TMP/verdict.log" "$dest/" 2>/dev/null
    { echo "log_content_was:"; printf '%s\n' "$logcontent"; } >> "$dest/verdict.log"
    echo "FAIL iter $i -> $dest"
  fi
  systemctl --user stop "tsk-$J.service" >/dev/null 2>&1
  rm -rf "$TMP"
  if [ $((i % 50)) -eq 0 ]; then echo "progress: $i/$ITERS done, fails=$total_fail (subst=$subst_fail refusal=$refusal_fail)" >&2; fi
done
echo "RESULT total_fail=$total_fail / $ITERS  (substitution_fails=$subst_fail refusal_fails=$refusal_fail)"
[ "$total_fail" -eq 0 ]
