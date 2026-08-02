#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# PreToolUse(Bash) DECISION-POINT recall — the within-loop half of the continuity hook.
# resume-match.sh fires on the USER's prompt (session-entry); this fires the instant I'm about
# to RUN a substantive shell command, keyed on the command itself, and surfaces any STRONGLY
# matching documented procedure / HEAD / script-header so I check it instead of reverse-
# engineering something already solved (the "still-guessing" failure).
#
# Discipline: ALLOWLIST-gated (fires only on ops-SETUP commands, not everything) + DISCOUNTER (silent unless a high-confidence hit),
# deduped per artifact per session (never nag twice), bounded latency, NEVER blocks the tool
# (always permissionDecision=allow), silent on any failure. Tune THRESH from live behaviour.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
. "$ENGINE/qq-config.sh"
. "$ENGINE/qq-redact.sh" 2>/dev/null || true   # model-aware withholding of involuntary doc-hints
# If the filter is unavailable, emit no hints at all: the guard below would otherwise go false
# and surface every hit UNFILTERED — the one failure mode it exists to stop (same rule as
# inject-contract.sh and resume-match.sh).
command -v qq_should_redact >/dev/null 2>&1 || exit 0
QQ_STATE_DIR="${QQ_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/quintessence}"
qqs="$ENGINE/qq-search"
THRESH=0.55          # the ALLOWLIST below is the primary noise control now (only ops-setup commands reach
                     # the search), so the threshold can be a touch looser than the skip-list era's 0.60.
MINLEN=30            # skip short/trivial commands
[ -x "$qqs" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

input=$(cat) || exit 0
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null) || exit 0
sid=$(printf '%s' "$input" | jq -r '.session_id // "x"' 2>/dev/null)
tp=$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null) || tp=""
[ -n "$cmd" ] && [ "${#cmd}" -ge "$MINLEN" ] || exit 0

# ALLOWLIST gate (replaced a skip-list, which was noisy: commands ABOUT the documented system
# matched docs about themselves). Fire ONLY when the command looks like SETTING UP / CONFIGURING
# an operational thing — exactly where a runbook / HEAD / script-header likely exists. Everything
# else stays silent. Grow the vocabulary as new procedure classes show up.
DERIVE_RE='git[[:space:]]+(config|remote|init)|git-remote-gcrypt|gcrypt|(^|[^[:alnum:]])gpg([^a-z]|$)|ssh-keygen|ssh-keyscan|ssh-copy-id|authorized_keys|cryptsetup|mkfs|luks|zpool|zfs[[:space:]]+create|systemctl[[:space:]]+(enable|disable|mask)|\.(service|timer)([^a-z]|$)|crontab|cron\.d|CRON_TZ|ufw|iptables|nft|virsh|virt-install|modprobe|update-initramfs|dkms|fstab|ollama[[:space:]]+create|Modelfile|certbot|wireguard|openvpn|rinetd|pkcs11|yubikey|keychain|mirror-init|git-mirror'
printf '%s' "$cmd" | grep -qiE "$DERIVE_RE" || exit 0

q=$(printf '%s' "$cmd" | head -c 300)
line=$(timeout 8 "$qqs" -k 1 "$q" 2>/dev/null | grep -m1 -E '^[[:space:]]*\[[0-9]') || exit 0
[ -n "$line" ] || exit 0
score=$(printf '%s' "$line" | sed -E 's/^[[:space:]]*\[([0-9.]+)\].*/\1/')
awk -v s="$score" -v t="$THRESH" 'BEGIN{exit !(s+0>=t+0)}' || exit 0   # below threshold → silent
# Parse the LOCATOR out of the rendered hit line: `  [0.712] (qq) qq show my-slug › Title`.
# The locator is whatever sits between the source tag and the ` › ` separator, and it can contain
# SPACES (`qq show <slug>` / `qq fact <name>` for HEADs and facts — quintessence.search.hit_locator
# returns a runnable command there, a path only for plain files). The old expression required a
# single space-free token before ` › `, so it never matched a HEAD hit at all and sed passed the
# WHOLE RENDERED LINE through: the redact check then only caught the slug by substring luck, the
# dedup key hashed the score and title too (so "once per artifact per session" was really once per
# LINE), and the hint printed the whole line back (2026-07-29 verification round, F3). Strip the
# score+tag prefix and everything from ` › ` on, each anchored, instead of guessing token shapes.
# Fail closed: a line without the separator is not a hit line we understand — say nothing.
case "$line" in *' › '*) ;; *) exit 0 ;; esac
loc=$(printf '%s' "$line" | sed -E -e 's/^[[:space:]]*\[[0-9.]+\][[:space:]]*\([^)]*\)[[:space:]]*//' \
                                   -e 's/[[:space:]]*›.*$//' -e 's/[[:space:]]+$//')
[ -n "$loc" ] || exit 0
# model-aware redaction: never surface a redact-listed slug on a gated/unknown model.
if command -v qq_should_redact >/dev/null 2>&1 && qq_should_redact "$tp" && qq_slug_redacted "$loc"; then exit 0; fi

# dedup: surface a given artifact at most once per session (keyed on the locator alone — a key
# carrying the score or title would change with either and re-fire on the same artifact).
dir="$QQ_STATE_DIR/prederive/$sid"; mkdir -p "$dir" 2>/dev/null
# sweep stale per-session dedup dirs, same one-day rule as resume-match.sh's sentinels
find "$QQ_STATE_DIR/prederive" -mindepth 1 -maxdepth 1 -type d -mtime +1 -exec rm -rf {} + 2>/dev/null || true
key=$(printf '%s' "$loc" | sha1sum | cut -c1-16)
[ -e "$dir/$key" ] && exit 0
touch "$dir/$key" 2>/dev/null

msg="📎 Likely already documented (qq-search ${score}): \`${loc}\`. Before reverse-engineering this, read it (qq-search / open the file) — there may be an existing runbook, script-header, or decided HEAD."
jq -nc --arg c "$msg" '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"allow",additionalContext:$c}}'
exit 0
