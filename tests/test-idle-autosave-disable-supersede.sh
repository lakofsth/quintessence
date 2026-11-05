#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-idle-autosave-disable-supersede.sh — switching the idle autosave OFF mid-session stands down
# the waiter the previous Stop armed. Pins the invariant sentence: "A Stop run with
# QQ_IDLE_AUTOSAVE_DISABLE=1 performs the stored-PID supersede for its session (cmdline-verified,
# never by pattern) before exiting 0, so turning the autosave off mid-session never leaves the waiter
# armed by the previous Stop to wake the session later."
#
# Written from the sentence alone (certify unit). Directions:
#   (1) the disabled Stop kills this session's armed waiter by its stored PID, promptly — not at the
#       waiter's next poll — and the waiter never fires (exit 2);
#   (2) the disabled Stop itself exits 0, silently, without arming;
#   (3) for ITS session only: another session's armed waiter survives;
#   (4) never by pattern: a bystander whose cmdline names idle-autosave survives;
#   (5) cmdline-verified: a stored PID that is not an idle-autosave process survives;
#   (6) the knob set from the config file (env > config > default) supersedes the same way.
# Hermetic: fixture transcripts, closed environment, a long POLL so a waiter that is merely told to
# stand down (rather than killed) is still alive when the check reads it.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
HOOK="${HOOK_UNDER_TEST:-$ENGINE/idle-autosave.sh}"
TMP="$(mktemp -d)"
trap 'jobs -p | xargs -r kill 2>/dev/null; rm -rf "$TMP"' EXIT
command -v jq >/dev/null || { echo "SKIP: jq not installed"; exit 0; }
pass=0; fail=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

mkdir -p "$TMP/home"
SESSION_N=0
fresh() { SESSION_N=$((SESSION_N+1)); SID="s$SESSION_N"; TR="$TMP/t$SESSION_N.jsonl"; : > "$TR"
          jq -cn '{type:"user",message:{role:"user",content:"hello"}}' >> "$TR"; }
# arm SID TRANSCRIPT -> $ARMED is the pid of a live waiter (DELAY 300, POLL 30) that owns the pidf.
arm() {
  printf '{"session_id":"%s","transcript_path":"%s"}' "$1" "$2" \
    | env -i PATH="$PATH" HOME="$TMP/home" QQ_CONFIG="$TMP/none" QQ_IDLE_AUTOSAVE_STATE="$TMP/state" \
        QQ_IDLE_AUTOSAVE_DELAY=300 QQ_IDLE_AUTOSAVE_POLL=30 bash "$HOOK" >/dev/null 2>&1 &
  ARMED=$!
  for _ in $(seq 1 50); do [ "$(cat "$TMP/state/timer-$1.pid" 2>/dev/null)" = "$ARMED" ] && break; sleep 0.1; done
}
# stop_disabled SID TRANSCRIPT [VAR=VAL...] -> rc in $RC, stderr in $ERR, wall seconds in $EL.
stop_disabled() {
  local sid=$1 tr=$2 t0; shift 2
  ERR="$TMP/err.$RANDOM"; t0=$(date +%s)
  printf '{"session_id":"%s","transcript_path":"%s"}' "$sid" "$tr" \
    | env -i PATH="$PATH" HOME="$TMP/home" QQ_CONFIG="$TMP/none" QQ_IDLE_AUTOSAVE_STATE="$TMP/state" \
        QQ_IDLE_AUTOSAVE_DELAY=300 QQ_IDLE_AUTOSAVE_POLL=30 QQ_IDLE_AUTOSAVE_DISABLE=1 "$@" \
        timeout 20 bash "$HOOK" 2>"$ERR" >/dev/null
  RC=$?; EL=$(( $(date +%s) - t0 ))
}
# gone PID -> 0 if PID has exited within ~3s (well inside one 30 s poll)
gone() { for _ in $(seq 1 30); do kill -0 "$1" 2>/dev/null || return 0; sleep 0.1; done; return 1; }

# (1)+(2) this session's armed waiter is killed by the disabled Stop, which itself exits 0.
fresh; arm "$SID" "$TR"; w=$ARMED
if [ "$(cat "$TMP/state/timer-$SID.pid" 2>/dev/null)" = "$w" ] && kill -0 "$w" 2>/dev/null; then
  ok "(1) precondition: the previous Stop's waiter is armed and owns the stored PID"
else no "(1) precondition: no armed waiter — the checks below would prove nothing"; fi
stop_disabled "$SID" "$TR"
if [ "$RC" -eq 0 ] && [ ! -s "$ERR" ] && [ "$EL" -lt 5 ]; then ok "(2) the disabled Stop exits 0 at once, silently"
else no "(2) the disabled Stop: rc=$RC elapsed=${EL}s stderr=$(head -c 200 "$ERR")"; fi
if gone "$w"; then
  ok "(1) the waiter armed by the previous Stop is gone after the disabled Stop"
  wait "$w"; wrc=$?
  [ "$wrc" -ne 2 ] && ok "(1) the stood-down waiter did not fire (rc=$wrc)" || no "(1) the waiter woke the session (exit 2)"
else
  no "(1) the waiter armed by the previous Stop is still alive after the disabled Stop — it will wake the session later"
  kill "$w" 2>/dev/null
fi
pf="$(cat "$TMP/state/timer-$SID.pid" 2>/dev/null)"
if [ -z "$pf" ] || ! kill -0 "$pf" 2>/dev/null; then ok "(2) the disabled Stop left no live waiter of its own behind the stored PID"
else no "(2) a live process ($pf) holds the session's stored PID after the disabled Stop"; kill "$pf" 2>/dev/null; fi

# (3) for its session only: another session's waiter survives this session's disabled Stop.
fresh; arm "$SID" "$TR"; mine=$ARMED; mysid=$SID; mytr=$TR
fresh; arm "$SID" "$TR"; theirs=$ARMED
stop_disabled "$mysid" "$mytr"
gone "$mine" && ok "(3) control: the disabled session's own waiter is gone" \
  || { no "(3) control: the disabled session's own waiter survived"; kill "$mine" 2>/dev/null; }
sleep 0.5
if kill -0 "$theirs" 2>/dev/null; then ok "(3) another session's armed waiter is untouched"
else no "(3) another session's waiter was killed by this session's disabled Stop"; fi
kill "$theirs" 2>/dev/null

# (4) never by pattern: a live bystander whose cmdline names idle-autosave is not killed.
# A renamed bash (not a renamed sleep: uutils' multicall binary picks its tool from argv[0]).
fresh; arm "$SID" "$TR"; w=$ARMED
bash -c 'exec -a idle-autosave-bystander bash -c "sleep 120; :"' & by=$!
for _ in $(seq 1 50); do tr '\0' ' ' <"/proc/$by/cmdline" 2>/dev/null | grep -q '^idle-autosave-bystander' && break; sleep 0.1; done
if tr '\0' ' ' <"/proc/$by/cmdline" 2>/dev/null | grep -q '^idle-autosave-bystander'; then ok "(4) precondition: a live bystander names idle-autosave"
else no "(4) precondition: no live bystander — the check below would prove nothing"; fi
stop_disabled "$SID" "$TR"
gone "$w" && ok "(4) control: the session's own waiter is gone" || { no "(4) control: the session's own waiter survived"; kill "$w" 2>/dev/null; }
sleep 0.5
if kill -0 "$by" 2>/dev/null; then ok "(4) the bystander named idle-autosave was not killed"
else no "(4) the bystander was killed — the disabled Stop supersedes by name pattern"; fi
kill "$by" 2>/dev/null

# (5) cmdline-verified: a stored PID that is not an idle-autosave process is left alone.
fresh; sleep 120 & decoy=$!
mkdir -p "$TMP/state"; echo "$decoy" > "$TMP/state/timer-$SID.pid"
stop_disabled "$SID" "$TR"
sleep 0.5
if [ "$RC" -eq 0 ] && kill -0 "$decoy" 2>/dev/null; then ok "(5) a stored PID that is not an idle-autosave process is left alone"
else no "(5) rc=$RC; the decoy behind the stored PID was killed — the supersede did not verify the cmdline"; fi
kill "$decoy" 2>/dev/null

# (6) the knob from the config file (env > config > default): the same supersede.
fresh; arm "$SID" "$TR"; w=$ARMED
printf 'QQ_IDLE_AUTOSAVE_DISABLE=1\n' > "$TMP/config-off"
ERR="$TMP/err.cfg"; t0=$(date +%s)
printf '{"session_id":"%s","transcript_path":"%s"}' "$SID" "$TR" \
  | env -i PATH="$PATH" HOME="$TMP/home" QQ_CONFIG="$TMP/config-off" QQ_IDLE_AUTOSAVE_STATE="$TMP/state" \
      QQ_IDLE_AUTOSAVE_DELAY=300 QQ_IDLE_AUTOSAVE_POLL=30 timeout 20 bash "$HOOK" 2>"$ERR" >/dev/null
RC=$?; EL=$(( $(date +%s) - t0 ))
[ "$RC" -eq 0 ] && [ "$EL" -lt 5 ] && ok "(6) a Stop disabled by the config file exits 0 at once" \
  || no "(6) a Stop disabled by the config file: rc=$RC elapsed=${EL}s"
gone "$w" && ok "(6) ...and the previous Stop's waiter is gone" \
  || { no "(6) the waiter survived a Stop disabled by the config file"; kill "$w" 2>/dev/null; }

printf -- '----- %d passed, %d failed -----\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
