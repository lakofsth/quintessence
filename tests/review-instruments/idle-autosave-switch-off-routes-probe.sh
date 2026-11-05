#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# idle-autosave-switch-off-routes-probe.sh — review instrument (fix-review qq-idle-autosave-2026-10,
# second leg, 2026-10-31). NOT a suite: tests/run.sh does not glob this directory.
#
# Arms a real idle-autosave.sh waiter in a scratch HOME (env -i, DELAY=8, POLL=1, a fixture
# transcript with genuine activity so a fire WOULD happen), then switches the autosave off by
# one route the tool offers and reports what the armed waiter did. No Stop runs after the switch
# unless the route IS a Stop. Never touches the live store or the real config file.
#
#   R1  hand-edit of the default config file  ($HOME/.config/quintessence/config)
#   R2  hand-edit under an XDG_CONFIG_HOME
#   R3  hand-edit of a QQ_CONFIG file, in-place rewrite by sed -i (rename, not truncate)
#   R4  `qq config set QQ_IDLE_AUTOSAVE_DISABLE 1` (atomic rewrite), default file
#   R5  `qq config set` with QQ_CONFIG written with a literal tilde (as a settings.json env
#       value arrives: unexpanded) — hook and qq see the same environment
#   R6  CRLF config line "QQ_IDLE_AUTOSAVE_DISABLE=1\r"
#   R7  a later Stop whose environment carries QQ_IDLE_AUTOSAVE_DISABLE=1 (the supersede route)
#   R8  environment QQ_IDLE_AUTOSAVE_DISABLE=1 at the Stop itself: the hook never arms
#   W   window: the key flips to 1 AFTER the last in-loop re-read, while the post-break actcount
#       runs over a large transcript -> does the waiter still exit 2?
# Expectation per row is printed beside the observation; the probe exits 0 regardless.
#
# Usage: HOOK_UNDER_TEST=<path/idle-autosave.sh> QQ_UNDER_TEST=<path/qq> bash <this>
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
HOOK="${HOOK_UNDER_TEST:-$ENGINE/idle-autosave.sh}"
QQ="${QQ_UNDER_TEST:-$ENGINE/qq}"
T="$(mktemp -d)"
trap 'jobs -p | xargs -r kill 2>/dev/null; rm -rf "$T"' EXIT
N=0
row(){ printf '%-4s %-62s want=%-10s got=%s\n' "$1" "$2" "$3" "$4"; }

# arm ROUTE-ENV... : start a waiter in a fresh scratch HOME; sets H, SID, TR, ARMED, JOB.
arm(){
  N=$((N+1)); H="$T/h$N"; SID="s$N"; TR="$H/t.jsonl"; mkdir -p "$H/.config/quintessence" "$H/cwd"
  jq -cn '{type:"user",message:{role:"user",content:"hello"}}' > "$TR"
  [ -n "${BIG:-}" ] && python3 -c "
import json,sys
with open(sys.argv[1],'a') as f:
    for i in range(int(sys.argv[2])): f.write(json.dumps({'type':'assistant','message':{'content':'x'*400}})+'\n')
" "$TR" "$BIG"
  printf '{"session_id":"%s","transcript_path":"%s"}' "$SID" "$TR" \
    | (cd "$H/cwd" && env -i PATH="$PATH" HOME="$H" QQ_IDLE_AUTOSAVE_STATE="$H/state" \
        QQ_IDLE_AUTOSAVE_DELAY="${DELAY:-8}" QQ_IDLE_AUTOSAVE_POLL=1 "$@" \
        timeout 60 bash "$HOOK" >/dev/null 2>&1) &
  JOB=$!; ARMED=""
  for _ in $(seq 1 50); do
    ARMED="$(cat "$H/state/timer-$SID.pid" 2>/dev/null)"
    [ -n "$ARMED" ] && kill -0 "$ARMED" 2>/dev/null && return 0
    kill -0 "$JOB" 2>/dev/null || { ARMED=""; return 1; }
    sleep 0.1
  done; ARMED=""; return 1
}
# outcome: wait for the job; sets OUT="rc=<n> after <s>s" (no subshell: wait needs the job table)
outcome(){ local t0 rc; t0=$(date +%s); wait "$JOB"; rc=$?; OUT="rc=$rc after $(( $(date +%s)-t0 ))s"; }
qqset(){ (cd "$H/cwd" && env -i PATH="$PATH" HOME="$H" "$@" python3 "$QQ" config set QQ_IDLE_AUTOSAVE_DISABLE 1 >/dev/null 2>&1); }

arm; printf 'QQ_IDLE_AUTOSAVE_DISABLE=1\n' > "$H/.config/quintessence/config"
outcome; row R1 "hand-edit default config file" "rc=0 fast" "$OUT"

arm XDG_CONFIG_HOME="$T/xdg$((N+1))"; mkdir -p "$T/xdg$N/quintessence"
printf 'QQ_IDLE_AUTOSAVE_DISABLE=1\n' > "$T/xdg$N/quintessence/config"
outcome; row R2 "hand-edit under XDG_CONFIG_HOME" "rc=0 fast" "$OUT"

C="$T/qqc$((N+1))"; printf 'QQ_FOO=1\nQQ_IDLE_AUTOSAVE_DISABLE=0\n' > "$C"
arm QQ_CONFIG="$C"; sed -i 's/^QQ_IDLE_AUTOSAVE_DISABLE=0$/QQ_IDLE_AUTOSAVE_DISABLE=1/' "$C"
outcome; row R3 "sed -i edit of a QQ_CONFIG file" "rc=0 fast" "$OUT"

arm; qqset; outcome; got="$OUT"
row R4 "qq config set (default file: $(cat "$H/.config/quintessence/config" 2>/dev/null | tr '\n' ' '))" "rc=0 fast" "$got"

arm QQ_CONFIG='~/qq.conf'; qqset QQ_CONFIG='~/qq.conf'; outcome; got="$OUT"
row R5 "qq config set, QQ_CONFIG='~/qq.conf' (written: $( [ -f "$H/qq.conf" ] && echo \$HOME/qq.conf || echo none))" "rc=0 fast" "$got"

arm; printf 'QQ_IDLE_AUTOSAVE_DISABLE=1\r\n' > "$H/.config/quintessence/config"
outcome; row R6 "CRLF config line" "rc=0 fast" "$OUT"

arm; printf '{"session_id":"%s","transcript_path":"%s"}' "$SID" "$TR" \
  | env -i PATH="$PATH" HOME="$H" QQ_IDLE_AUTOSAVE_STATE="$H/state" QQ_IDLE_AUTOSAVE_DISABLE=1 \
      bash "$HOOK" >/dev/null 2>&1
outcome; row R7 "later Stop with env DISABLE=1 (supersede)" "rc=143" "$OUT"

N=$((N+1)); H="$T/h$N"; mkdir -p "$H"; jq -cn '{type:"user",message:{content:"hi"}}' > "$H/t"
printf '{"session_id":"x","transcript_path":"%s"}' "$H/t" \
  | env -i PATH="$PATH" HOME="$H" QQ_IDLE_AUTOSAVE_STATE="$H/state" QQ_IDLE_AUTOSAVE_DISABLE=1 \
      QQ_IDLE_AUTOSAVE_POLL=1 QQ_IDLE_AUTOSAVE_DELAY=2 timeout 10 bash "$HOOK" >/dev/null 2>&1
row R8 "env DISABLE=1 at the Stop: never arms" "rc=0,nostate" "rc=$? state=$(ls "$H/state" 2>/dev/null | wc -l)"

# W — the window between the last re-read and the exit 2. A large transcript makes the post-break
# actcount (one jq over the whole file) take measurable time; flip the key while it runs.
BIG="${BIG_RECORDS:-110000}" DELAY=3 arm
seen=0
for _ in $(seq 1 400); do
  if pgrep -P "$ARMED" -f 'select\(.type=="user"\)' >/dev/null 2>&1 || \
     pgrep -f "jq -r select\(.type==\"user\"\).*$TR" >/dev/null 2>&1; then seen=1; break; fi
  sleep 0.025
done
printf 'QQ_IDLE_AUTOSAVE_DISABLE=1\n' > "$H/.config/quintessence/config"
outcome; row W "flip during post-break actcount ($(du -m "$TR" | cut -f1) MB, seen=$seen)" "rc=2 (gap)" "$OUT"
# how long actcount takes on that transcript, outside the hook
t0=$(date +%s%N); jq -r 'select(.type=="user") | (.message.content|tostring) | (contains("Stop hook feedback")|not)' "$TR" | grep -c '^true$' >/dev/null
echo "W-timing: one actcount-shaped jq over $(du -m "$TR" | cut -f1) MB took $(( ($(date +%s%N)-t0)/1000000 )) ms"
