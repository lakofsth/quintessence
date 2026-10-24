#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-idle-autosave.sh — idle-autosave.sh (async Stop hook) wakes a quiet session exactly when it
# should. Hermetic: a fixture transcript, DELAY=1/POLL=1, no real hour-long wait, no real session.
# Cases (a)-(g) are the gate logic; (h) pins the wiring the plugin ships. Each case names the
# mutation that turns it red; the mutations are recorded in the commit that added this suite.
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
# run_hook SID TRANSCRIPT [VAR=VAL...] -> rc in $RC, stderr in $ERR. Closed environment: nothing the
# invoking shell exported (QQ_CONFIG, QQ_STATE_DIR, ...) reaches the hook.
run_hook() {
  local sid=$1 tr=$2; shift 2
  ERR="$TMP/err.$$.$RANDOM"
  printf '{"session_id":"%s","transcript_path":"%s"}' "$sid" "$tr" \
    | env -i PATH="$PATH" HOME="$TMP/home" QQ_CONFIG="$TMP/none" \
        QQ_IDLE_AUTOSAVE_STATE="$TMP/state" QQ_IDLE_AUTOSAVE_DELAY=1 QQ_IDLE_AUTOSAVE_POLL=1 "$@" \
        bash "$HOOK" 2>"$ERR" >/dev/null
  RC=$?
}
fresh() { SESSION_N=$((SESSION_N+1)); SID="s$SESSION_N"; TR="$TMP/t$SESSION_N.jsonl"; : > "$TR"; }
user()  { jq -cn --arg c "$1" '{type:"user",message:{role:"user",content:$c}}' >> "$TR"; }
bash_write() { # COMMAND — an assistant Bash tool_use stamped an hour ahead, so it postdates the arm
  jq -cn --arg c "$1" --arg ts "$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)" \
    '{type:"assistant",timestamp:$ts,message:{role:"assistant",content:[{type:"tool_use",name:"Bash",input:{command:$c}}]}}' >> "$TR"
}

# (a) quiet past DELAY with new genuine activity -> exit 2, wake message on stderr.
# Red under: `exit 2` -> `exit 0` (the wake is never delivered).
fresh; user "hello"
run_hook "$SID" "$TR"
if [ "$RC" -eq 2 ] && grep -q 'auto-woken' "$ERR"; then ok "(a) quiet + new activity: exit 2 with the wake message on stderr"
else no "(a) rc=$RC stderr=$(head -c 200 "$ERR")"; fi
grep -q 'Opus' "$ERR" && no "(a) the wake message names a model; it must speak to the session" \
  || ok "(a) the wake message names no model"

# (b) no new activity since the checkpoint -> exit 0, silent. Red under: the gate
# `[ "$cur" -gt "$base" ] || exit 0` deleted (always fire).
run_hook "$SID" "$TR"
if [ "$RC" -eq 0 ] && [ ! -s "$ERR" ]; then ok "(b) no new activity since the last fire: exit 0, silent"
else no "(b) rc=$RC stderr=$(head -c 200 "$ERR")"; fi
user "more work"
run_hook "$SID" "$TR"
[ "$RC" -eq 2 ] && ok "(b) a genuine record after the baseline fires again" || no "(b) new record did not fire (rc=$RC)"

# (c) a transcript write during the wait resets the clock. Red under: the `qstart=$nowp` reset
# deleted (fires DELAY after the arm regardless of the write).
fresh; user "hello"
t0=$(date +%s)
printf '{"session_id":"%s","transcript_path":"%s"}' "$SID" "$TR" \
  | env -i PATH="$PATH" HOME="$TMP/home" QQ_CONFIG="$TMP/none" QQ_IDLE_AUTOSAVE_STATE="$TMP/state" \
      QQ_IDLE_AUTOSAVE_DELAY=4 QQ_IDLE_AUTOSAVE_POLL=1 bash "$HOOK" 2>"$TMP/errc" >/dev/null &
cpid=$!
sleep 3; touch -d '+30 seconds' "$TR"          # a write mid-wait: a new mtime
wait "$cpid"; crc=$?
el=$(( $(date +%s) - t0 ))
if [ "$crc" -eq 2 ] && [ "$el" -ge 7 ]; then ok "(c) a transcript write mid-wait resets the quiet clock (fired after ${el}s, DELAY 4)"
else no "(c) rc=$crc elapsed=${el}s — expected a fire no earlier than ~7s"; fi

# (d) a newer arm supersedes the old one, by stored PID only, never by a name pattern.
# Red under: the supersede block deleted (old timer survives); and under the stored-PID kill
# replaced by a kill-by-name-pattern over `idle-autosave` (the bystander below dies).
fresh; user "hello"
printf '{"session_id":"%s","transcript_path":"%s"}' "$SID" "$TR" \
  | env -i PATH="$PATH" HOME="$TMP/home" QQ_CONFIG="$TMP/none" QQ_IDLE_AUTOSAVE_STATE="$TMP/state" \
      QQ_IDLE_AUTOSAVE_DELAY=300 QQ_IDLE_AUTOSAVE_POLL=1 bash "$HOOK" >/dev/null 2>&1 &
apid=$!
for _ in $(seq 1 50); do [ -n "$(cat "$TMP/state/timer-$SID.pid" 2>/dev/null)" ] && break; sleep 0.1; done
# The bystander is a renamed bash, not a renamed sleep: uutils ships one multicall binary that
# picks its tool from argv[0], so `exec -a <name> sleep` exits at once there and the bystander
# looked killed with no hook involved. `; :` stops bash exec-ing sleep in its own place.
bash -c 'exec -a idle-autosave-bystander bash -c "sleep 120; :"' & bystander=$!
for _ in $(seq 1 50); do tr '\0' ' ' <"/proc/$bystander/cmdline" 2>/dev/null | grep -q '^idle-autosave-bystander' && break; sleep 0.1; done
if tr '\0' ' ' <"/proc/$bystander/cmdline" 2>/dev/null | grep -q '^idle-autosave-bystander'; then ok "(d) precondition: the bystander is alive and its cmdline names idle-autosave"
else no "(d) precondition: no live bystander named idle-autosave — the kill check below would prove nothing"; fi
run_hook "$SID" "$TR"                          # the newer arm (DELAY 1)
sleep 1.5
if ! kill -0 "$apid" 2>/dev/null; then ok "(d) the older arm was superseded and is gone"
else no "(d) the older arm survived a newer arm of the same session"; kill "$apid" 2>/dev/null; fi
if kill -0 "$bystander" 2>/dev/null; then ok "(d) an unrelated process whose name matches idle-autosave was not killed"
else no "(d) a bystander named idle-autosave was killed — supersede is going by name pattern"; fi
kill "$bystander" 2>/dev/null
# a stored PID whose cmdline is not ours is not killed either (PID reuse)
fresh; user "hello"
sleep 120 & decoy=$!
mkdir -p "$TMP/state"; echo "$decoy" > "$TMP/state/timer-$SID.pid"
run_hook "$SID" "$TR"
if kill -0 "$decoy" 2>/dev/null; then ok "(d) a stored PID that is not an idle-autosave process is left alone"
else no "(d) the stored-PID kill did not verify the cmdline"; fi
kill "$decoy" 2>/dev/null

# (e) own-checkpoint detection: a command-position `qq update` rebaselines (no wake); a quoted or
# grep-pattern mention does not (wake). Red under: the mask removed (quoted mentions count, so the
# wake the quoted cases expect is suppressed), and under the command-position anchor loosened to a
# bare substring (same).
checkpoint_case() { # NAME EXPECTED_RC COMMAND
  fresh; user "hello"
  run_hook "$SID" "$TR"                         # first arm + fire: baseline 1
  user "second genuine turn"; bash_write "$3"
  run_hook "$SID" "$TR"
  if [ "$RC" -eq "$2" ]; then ok "(e) $1"; else no "(e) $1 (rc=$RC, wanted $2)"; fi
}
checkpoint_case "a piped command-position \`qq update\` counts as the session's own checkpoint" 0 'echo x | qq update topic "body"'
checkpoint_case "a bare \`qq finalize\` counts" 0 'qq finalize topic'
checkpoint_case "a real \`qq update\` after a comment with an apostrophe still counts" 0 $'# don\'t forget\nqq update topic body'
checkpoint_case "a real \`qq update\` after an apostrophe inside double quotes still counts" 0 'echo "it'"'"'s done"; qq update topic body'
checkpoint_case "a grep pattern naming \`qq update\` does not count" 2 "grep -n 'qq update' notes.md"
checkpoint_case "a double-quoted mention does not count" 2 'echo "remember to qq update later"'
checkpoint_case "a separator inside a double-quoted argument does not make a command position" 2 'echo "done; qq update later"'
checkpoint_case "a separator inside a single-quoted commit message does not either" 2 "git commit -m 'x && qq update t'"
checkpoint_case "an unquoted mention outside command position does not count" 2 'echo remember to qq update later'
checkpoint_case "a printf fixture holding a qq write does not count" 2 'printf "qq update t x\n" > fixture.txt'
checkpoint_case "a read verb (\`qq show\`) does not count" 2 'qq show topic'

# (f) /clear scaffolding and rewake deliveries are not activity. Red under: the scaffolding
# `contains()` terms removed from actcount (a cleared session wakes hourly).
fresh
user '<command-name>/clear</command-name><command-message>clear</command-message>'
user '<local-command-caveat>Caveat: local command</local-command-caveat>'
user 'Stop hook feedback: You were auto-woken because this session has gone quiet'
run_hook "$SID" "$TR"
if [ "$RC" -eq 0 ] && [ ! -s "$ERR" ]; then ok "(f) /clear scaffolding and a rewake delivery are not counted: no wake"
else no "(f) scaffolding counted as activity (rc=$RC)"; fi
user "a real prompt"
run_hook "$SID" "$TR"
[ "$RC" -eq 2 ] && ok "(f) the same transcript with one genuine prompt does wake" || no "(f) control did not wake (rc=$RC)"

# (g) the disable knob no-ops: exit 0 at once, no state written. Red under: the knob line deleted.
fresh; user "hello"; rm -rf "$TMP/state-off"
t0=$(date +%s)
run_hook "$SID" "$TR" QQ_IDLE_AUTOSAVE_STATE="$TMP/state-off" QQ_IDLE_AUTOSAVE_DISABLE=1
if [ "$RC" -eq 0 ] && [ ! -s "$ERR" ] && [ ! -e "$TMP/state-off" ] && [ $(( $(date +%s) - t0 )) -lt 1 ]; then
  ok "(g) QQ_IDLE_AUTOSAVE_DISABLE=1: immediate silent exit 0, no state"
else no "(g) disable did not no-op (rc=$RC)"; fi

# state dir derives from QQ_STATE_DIR when QQ_IDLE_AUTOSAVE_STATE is unset (the product's own state dir)
fresh; user "hello"
run_hook "$SID" "$TR" QQ_IDLE_AUTOSAVE_STATE= QQ_STATE_DIR="$TMP/qqstate"
[ -f "$TMP/qqstate/idle-autosave/act-$SID" ] && ok "(h) state lives under \$QQ_STATE_DIR/idle-autosave by default" \
  || no "(h) no state under QQ_STATE_DIR (found: $(find "$TMP/qqstate" 2>/dev/null | tr '\n' ' '))"

# (h) shipped wiring: the plugin's Stop entry carries the async fields and a timeout above the delay;
# the config keys are registered with the measured default; nothing estate-specific ships.
python3 - "$ENGINE" <<'PY' && ok "(h) hooks.json Stop entry: async + asyncRewake + rewakeMessage + timeout > default delay" || no "(h) hooks.json Stop entry is wrong"
import json, sys
e = sys.argv[1]
sys.path.insert(0, e)
from quintessence.config import KEYS_BY_NAME
hs = [h for g in json.load(open(e + "/hooks/hooks.json"))["hooks"]["Stop"] for h in g["hooks"]
      if "idle-autosave.sh" in h["command"]]
assert len(hs) == 1, hs
h = hs[0]
assert h["async"] is True and h["asyncRewake"] is True and h["rewakeMessage"], h
assert h["timeout"] > KEYS_BY_NAME["QQ_IDLE_AUTOSAVE_DELAY"].default == 3000, h
PY
if grep -nE '/home/|\.claude/hooks|Opus|Sonnet|Fable' "$ENGINE/idle-autosave.sh" "$ENGINE/quote-mask.py" >/dev/null; then
  no "(h) estate path or model name in the shipped hook files: $(grep -nE '/home/|\.claude/hooks|Opus|Sonnet|Fable' "$ENGINE/idle-autosave.sh" "$ENGINE/quote-mask.py" | head -2)"
else ok "(h) no estate path or model name in the shipped hook files"; fi

printf -- '----- %d passed, %d failed -----\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
