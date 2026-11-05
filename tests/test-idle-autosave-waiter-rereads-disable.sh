#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-idle-autosave-waiter-rereads-disable.sh — pins the invariant sentence: "An armed
# idle-autosave waiter re-reads QQ_IDLE_AUTOSAVE_DISABLE (environment over config file, the file
# through qq-config.sh's own parser) after every poll, so it never exits 2 once the key it would
# resolve at that moment is 1, whether or not another Stop runs in its session."
#
# Written from the sentence alone (certify unit). The waiter itself — not a later Stop — must
# stand itself down: NO second Stop run happens in any case below, which is the half of the
# sentence the next-Stop supersede (tests/test-idle-autosave-disable-supersede.sh) cannot reach.
# Directions enumerated from the sentence:
#   (a) the key flips to 1 mid-wait via the config file → the waiter exits 0 on its own, promptly
#       (well before DELAY), silently, and never exits 2 — red under a waiter that reads the key
#       only at arm (pre-fix): it sleeps to DELAY and wakes the session anyway;
#   (b) "after every poll": the flip lands several polls into the wait → same stand-down — red
#       under a single re-read scheduled at a fixed early moment instead of per poll;
#   (c) "the file through qq-config.sh's own parser": a parser-shaped value (spaces around '=',
#       one layer of quotes) resolves to 1 → stand down — red under a re-read that greps the raw
#       file for a literal KEY=1 instead of using the parser;
#   (d) the same parser in reverse: a commented-out key is NOT the key set → the waiter still
#       fires at DELAY — red under a re-read that substring-matches the file;
#   (e) "environment over config file": an explicit QQ_IDLE_AUTOSAVE_DISABLE=0 in the waiter's
#       own environment beats a config file flipped to 1 → the waiter still fires at DELAY — red
#       under a re-read that lets the file override the environment (or that unsets its own env
#       before re-sourcing);
#   (f) "never exits 2 once the key it would resolve at that moment is 1": the key flips 0→1→0
#       during the wait → once the waiter has resolved 1 it stands down for good, so the fire at
#       DELAY does not happen even though the key reads 0 at that moment.
# Hermetic: fixture transcript with genuine new activity (so a fire WOULD happen), closed
# environment (env -i; the key reaches the waiter only through the config file unless a case puts
# it in the environment), POLL=1, DELAY=8 — a red run costs ~9 s, a green run ~3 s.
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
# arm [VAR=VAL...] — a Stop run with NO QQ_IDLE_AUTOSAVE_DISABLE in its environment (the key
# comes from $CFG unless VARs override) that becomes the armed waiter; $ARMED is its pid and it
# owns the stored timer PID before returning. DELAY=8, POLL=1.
arm() {
  SESSION_N=$((SESSION_N+1))
  local sid="s$SESSION_N" tr="$TMP/t$SESSION_N"
  : > "$tr"
  jq -cn '{type:"user",message:{role:"user",content:"hello"}}' >> "$tr"
  SID=$sid; TR=$tr; CFG="$TMP/cfg-$sid"
  printf 'QQ_IDLE_AUTOSAVE_DISABLE=0\n' > "$CFG"
  printf '{"session_id":"%s","transcript_path":"%s"}' "$SID" "$TR" \
    | env -i PATH="$PATH" HOME="$TMP/home" QQ_CONFIG="$CFG" QQ_IDLE_AUTOSAVE_STATE="$TMP/state" \
        QQ_IDLE_AUTOSAVE_DELAY=8 QQ_IDLE_AUTOSAVE_POLL=1 "$@" \
        timeout 30 bash "$HOOK" >/dev/null 2>&1 &
  local job=$!
  for _ in $(seq 1 50); do
    ARMED="$(cat "$TMP/state/timer-$SID.pid" 2>/dev/null)"
    [ -n "$ARMED" ] && kill -0 "$ARMED" 2>/dev/null && { JOB=$job; return 0; }
    sleep 0.1
  done
  ARMED=""; JOB=$job
  no "precondition: no armed waiter took the stored PID for $SID — the case below would prove nothing"
}
# flip VAL — edit the config file under the armed waiter. No Stop runs after this point.
flip() { printf 'QQ_IDLE_AUTOSAVE_DISABLE=%s\n' "$1" > "$CFG"; }
# wait_timed — wait for the armed waiter up to $1 s; rc in WRC, wall seconds in WEL,
# STILL=1 if it is still running when the window closed.
wait_timed() {
  local lim=$1 t0; t0=$(date +%s); WRC=0; STILL=0
  while kill -0 "$ARMED" 2>/dev/null; do
    if [ $(( $(date +%s) - t0 )) -ge "$lim" ]; then STILL=1; kill "$ARMED" 2>/dev/null; break; fi
    sleep 0.1
  done
  wait "$JOB" 2>/dev/null; WRC=$?
  WEL=$(( $(date +%s) - t0 ))
}
# expect_standdown NAME LIM — the waiter exited on its own with 0, silently, before LIM seconds.
expect_standdown() {
  wait_timed "$2"
  if [ "$STILL" -eq 0 ] && [ "$WRC" -ne 2 ] && [ "$WRC" -ne 124 ] && [ "$WRC" -ne 143 ] \
     && [ "$WRC" -eq 0 ] && [ "$WEL" -lt "$2" ]; then
    ok "$1 (exited 0 on its own after ${WEL}s, never exit 2)"
  else no "$1 (still=$STILL rc=$WRC elapsed=${WEL}s — expected a voluntary exit 0 before ${2}s)"
  fi
}
# expect_fire NAME — the waiter was NOT standing down: it ran to DELAY and woke the session (2).
expect_fire() {
  wait_timed 15
  if [ "$STILL" -eq 0 ] && [ "$WRC" -eq 2 ]; then ok "$1 (fired at DELAY with exit 2, as the key demands)"
  else no "$1 (still=$STILL rc=$WRC elapsed=${WEL}s — expected the fire at DELAY, exit 2)"; fi
}

# (a) the key flips to 1 mid-wait via the config file → the waiter stands down by itself.
arm; flip 1; expect_standdown "(a) config flipped to 1 mid-wait: the armed waiter exits 0 on its own, never 2" 6

# (b) after EVERY poll: the flip lands several polls into the wait (t≈4.5 of DELAY 8).
arm; sleep 3; flip 1; expect_standdown "(b) flip four polls into the wait: the re-read is per poll, not a one-shot" 5

# (c) the file arrives through qq-config.sh's own parser: spaces around '=' and one layer of
#     quotes still resolve to 1.
arm; printf 'QQ_IDLE_AUTOSAVE_DISABLE = "1"\n' > "$CFG"; expect_standdown "(c) a parser-shaped config value (spaces, quotes) resolves to 1 and stands the waiter down" 6

# (d) the parser in reverse: a comment is not the key set → no stand-down, the fire happens.
arm; printf '#QQ_IDLE_AUTOSAVE_DISABLE=1\n' > "$CFG"; expect_fire "(d) a commented-out key is not the key set: the waiter still fires"

# (e) environment over config file: an explicit 0 in the waiter's own environment beats the file.
arm QQ_IDLE_AUTOSAVE_DISABLE=0; flip 1; expect_fire "(e) env 0 over file 1: the key does not resolve to 1, the waiter still fires"

# (f) once resolved 1, never exit 2: flip 0→1→0 while the waiter polls → stood down for good.
arm; sleep 1; flip 1; sleep 1; flip 0; expect_standdown "(f) flip 1 then back to 0: having resolved 1, the waiter never exits 2" 7

printf -- '----- %d passed, %d failed -----\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
