#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# Stop hook (async, asyncRewake) — idle-checkpoint autosave.
#
# Purpose: a session that has gone quiet is about to lose its context twice over: Claude Code's
# idle compaction (lossy, ex-post) and the prompt-cache TTL going cold. Wake THIS session
# (asyncRewake, exit 2) shortly before either, so it captures its own synthesis into a quintessence
# HEAD while the full context is still in view and the cache is still warm. A HEAD written at peak
# context is the real save; compaction is salvage.
#
# Trigger = STABILISATION, not size. asyncRewake backgrounds this process; it polls the transcript
# and fires only once the transcript has been CONTINUOUSLY QUIET for DELAY seconds — any transcript
# write resets the clock.
#
# Fire GATE = genuine NEW activity since the last checkpoint, counted (not sized): the number of
# user-turn records that are NOT asyncRewake deliveries (wrapped as "Stop hook feedback") and NOT
# slash-command scaffolding. There is deliberately NO byte/size threshold — size is a poor proxy
# for importance; the woken session decides what (if anything) is worth saving. Counting records
# (atomic lines) is immune to transcript-flush timing, and a woken turn adds no genuine user
# record, so the count cannot advance on our own wakes: no wake-loop, with no magic number.
#
# DELAY default 3000 s, not 3300: Claude Code's idle compaction fires at 54m19s–55m24s of quiet
# (24 of 24 measured records, 2026-10-23), and its transcript write resets this clock, so at 3300
# the autosave lost the race 22 of 24 times. Fire first, while the full context is still in view.
# The hook entry's timeout must exceed DELAY (hooks.json carries 3600).
#
# Config (QQ_* keys; env > config file > default — see CONFIG.md):
#   QQ_IDLE_AUTOSAVE_DELAY   s of continuous quiet before firing (default 3000)
#   QQ_IDLE_AUTOSAVE_POLL    s between transcript checks (default 60)
#   QQ_IDLE_AUTOSAVE_STATE   per-session state dir (default $QQ_STATE_DIR/idle-autosave)
#   QQ_IDLE_AUTOSAVE_DISABLE 1 to no-op
# Silent and fail-open on every error path except the one deliberate exit 2.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
. "$ENGINE/qq-config.sh"

[ "${QQ_IDLE_AUTOSAVE_DISABLE:-0}" = "1" ] && exit 0

STATE="${QQ_IDLE_AUTOSAVE_STATE:-${QQ_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/quintessence}/idle-autosave}"
DELAY="${QQ_IDLE_AUTOSAVE_DELAY:-3000}"
POLL="${QQ_IDLE_AUTOSAVE_POLL:-60}"
mkdir -p "$STATE" 2>/dev/null || exit 0
# tidy stale per-session state (dead sessions). A live session rewrites its armstamp-/timer- every
# turn, so only genuinely-dead sessions age past +2 days.
find "$STATE" -maxdepth 1 -type f \( -name 'act-*' -o -name 'armstamp-*' -o -name 'timer-*' \) -mtime +2 -delete 2>/dev/null

inp=$(cat 2>/dev/null)
sid=$(printf '%s' "$inp"   | jq -r '.session_id // empty'      2>/dev/null)
tpath=$(printf '%s' "$inp" | jq -r '.transcript_path // empty' 2>/dev/null)
[ -n "$sid" ] || exit 0
[ -n "$tpath" ] && [ -f "$tpath" ] || exit 0

pidf="$STATE/timer-$sid.pid"
actf="$STATE/act-$sid"          # genuine-activity count as of the last fire/checkpoint
stampf="$STATE/armstamp-$sid"   # epoch of last arm (to detect checkpoints since)

# genuine user turns = user-type records that are NOT asyncRewake deliveries ("Stop hook feedback")
# and NOT slash-command scaffolding (<command-name>/<command-message>/<local-command-caveat>). The
# latter matters because a bare /clear injects such records; counting them as "new activity" made
# a cleared-then-idle session wake hourly only to decline. Commands that DO save still register
# via their qq writes -> own_checkpoint_since, so they are unaffected.
# One boolean PER RECORD (not per line): rewake content is multi-line, so a line-based grep would
# miscount it as many genuine turns. contains() tests the whole record's content at once.
actcount(){ jq -r 'select(.type=="user") | (.message.content|tostring) | ((contains("Stop hook feedback") or contains("<command-name>") or contains("<command-message>") or contains("<local-command-caveat>")) | not)' "$tpath" 2>/dev/null | grep -c '^true$'; }
mtime(){ stat -c %Y "$tpath" 2>/dev/null || echo 0; }

# did THIS SESSION checkpoint (a qq write in ITS OWN transcript) since epoch $1? echoes "yes".
# MULTI-SESSION SAFE: keyed on this session's transcript, NOT the shared store journal — which every
# concurrent session writes to, spuriously rebaselining every other session's activity baseline and
# suppressing its fire. A /wrap, a manual qq finalize, or our own woken checkpoint all surface here
# as a qq-write tool_use.
own_checkpoint_since(){
  # WRITE = a Bash `qq <write-verb>` run in COMMAND position, an update_head MCP call (always a
  # write), or the generic quintessence__qq MCP tool carrying a write verb. Reads (qq show/brief/
  # search/ask, show_head/resume_brief/…) do NOT count — nor does a command that merely mentions a
  # write verb in a quoted arg / grep pattern / heredoc (why qq is anchored to command position, not
  # a bare substring).
  local WV='(update|finalize|checkpoint|rewrite|new)' out
  out=$(jq -r --argjson since "$1" '
    ((.timestamp // "") | (sub("\\.[0-9]+Z$";"Z")) | fromdateiso8601?) as $e
    | select($e != null and $e > $since)
    | (.message.content // empty) | (if type=="array" then .[] else empty end)
    | select(.type? == "tool_use")
    | if .name=="Bash" then "B\t" + ((.input.command // "") | tojson)
      elif ((.name // "")|test("quintessence")) then "M\t" + (.name) + " " + ((.input // {})|tostring)
      else empty end
  ' "$tpath" 2>/dev/null)
  # The command arrives as one JSON string per line. quote-mask.py masks quoted spans and comments
  # of the WHOLE command as bash lexes it, THEN folds newlines to ";" — folding first would turn a
  # `# don't …` comment line into a comment swallowing the next line's real `qq update`. The mask is
  # length-preserving and bash-aware (an apostrophe inside "…" is a letter), so a qq-write inside a
  # printf/heredoc/grep-pattern argument is removed while a real unquoted `echo x | qq update`
  # survives. Anything the mask cannot run (no python3) yields no match: no rebaseline, which errs
  # toward waking once more rather than toward suppressing a wake.
  printf '%s\n' "$out" | sed -n 's/^B\t//p' \
    | python3 "$ENGINE/quote-mask.py" 2>/dev/null \
    | grep -qE "(^|[|;&])[[:space:]]*(sudo[[:space:]]+)?qq[[:space:]]+$WV([[:space:]]|$)" && { echo yes; return; }
  printf '%s\n' "$out" | sed -n 's/^M\t//p' \
    | grep -qE "update_head|__qq[[:space:]].*$WV" && echo yes
}

# --- SUPERSEDE: kill this session's previous idle-timer (by stored PID; verify cmdline; never pkill -f) ---
if [ -f "$pidf" ]; then
  oldpid=$(cat "$pidf" 2>/dev/null || true)
  if [ -n "${oldpid:-}" ] && [ "$oldpid" != "$$" ] && kill -0 "$oldpid" 2>/dev/null \
     && grep -qa 'idle-autosave' "/proc/$oldpid/cmdline" 2>/dev/null; then
    kill "$oldpid" 2>/dev/null || true
  fi
fi

# --- REBASELINE: if THIS SESSION saved its own work since last arm (its own fire, /wrap, or a manual
#     qq finalize — detected in this session's transcript), absorb the current activity count so we
#     don't re-checkpoint work that's already saved ---
now=$(date +%s)
if [ -f "$stampf" ]; then
  laststamp=$(cat "$stampf" 2>/dev/null || echo 0)
  if [ "$(own_checkpoint_since "$laststamp")" = "yes" ]; then
    actcount > "$actf"
  fi
else
  echo 0 > "$actf"                        # first arm → all existing activity counts until first checkpoint
fi
echo "$now" > "$stampf"
[ -f "$actf" ] || echo 0 > "$actf"

# --- ARM: become the timer. Poll the transcript; fire only after DELAY of CONTINUOUS quiet. ---
echo "$$" > "$pidf"
last=$(mtime); qstart=$(date +%s)
while :; do
  sleep "$POLL"
  [ "$(cat "$pidf" 2>/dev/null || echo)" = "$$" ] || exit 0          # superseded by a newer arm → stand down
  m=$(mtime); nowp=$(date +%s)
  if [ "$m" != "$last" ]; then last=$m; qstart=$nowp; continue; fi   # transcript moved → still active, reset
  [ $(( nowp - qstart )) -ge "$DELAY" ] && break                     # settled + quiet to the cache edge
done

# reached only after DELAY of uninterrupted quiet
cur=$(actcount); base=$(cat "$actf" 2>/dev/null || echo 0)
: "${cur:=0}"; : "${base:=0}"
echo "$cur" > "$actf"; rm -f "$pidf"      # rebaseline to now regardless (a woken turn adds no genuine record → no loop)

[ "$cur" -gt "$base" ] || exit 0          # no NEW genuine activity since last checkpoint → don't wake

# --- FIRE: exit 2 wakes the idle session; stderr becomes the system-reminder the session acts on ---
cat >&2 <<'MSG'
You were auto-woken because this session has gone quiet and its prompt cache is about to expire —
a chance to preserve synthesis cheaply while your context is still warm. Do a LIGHT checkpoint ONLY:

- If this session holds a reasoning thread, decision-state, or open loops worth keeping, capture
  the current synthesis to the right quintessence HEAD via `qq update` / `qq finalize` (create it
  with `qq new` if needed) — concise: just what a future session would need to resume.
- If nothing is worth saving, do nothing.

Do NOT begin or continue substantive work. Use only `qq`. Then end the turn.
MSG
exit 2
