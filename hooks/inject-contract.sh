#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# SessionStart hook: inject the small operational contract (CONTRACT.md) so a fresh, qq-naive
# session learns the write-path. Silent no-op on any failure — must never block session start.
root="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)}"
# capture the hook's stdin JSON up front (nothing else here reads stdin) — used below for
# model-aware redaction of the optional open-loops digest.
input="$(cat 2>/dev/null || true)"
c="$(cat "$root/CONTRACT.md" 2>/dev/null)" || exit 0
[ -n "$c" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0
# If no store exists yet, the plugin is otherwise silently inert — lead with a one-line setup hint.
. "$root/qq-config.sh" 2>/dev/null || true
. "$root/qq-redact.sh" 2>/dev/null || true
tp="$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
qd="${QUINTESSENCE_DIR:-$HOME/quintessence}"
[ -d "$qd" ] || c="quintessence is installed but NOT set up yet — run:  bash \"$root/setup.sh\"  (creates the store + config). Until then qq commands won't work.

$c"
# Optional local SessionStart extra (generic seam): a deployment's richer discipline block,
# appended verbatim after the contract. File at QQ_CONTRACT_EXTRA or $QQ_STATE_DIR/contract-extra.md.
state="${QQ_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/quintessence}"
extra_f="${QQ_CONTRACT_EXTRA:-$state/contract-extra.md}"
if [ -f "$extra_f" ]; then x="$(cat "$extra_f" 2>/dev/null)"; [ -n "$x" ] && c="$c"$'\n\n'"$x"; fi
# Optional open-loops digest at SessionStart (set QQ_SESSIONSTART_DIGEST=1).
if [ "${QQ_SESSIONSTART_DIGEST:-}" = "1" ] && [ -d "$qd" ]; then
  dg="$("$root/qq" digest 2>/dev/null)"
  # `qq digest` does NO redaction of its own, so the filter below is the ONLY thing standing
  # between a redact-listed slug and an involuntary injection at session start. If the filter is
  # not available (qq-redact.sh moved/renamed, a syntax error, a refactor that drops the
  # function), DROP THE DIGEST — the previous `command -v ... && dg=filtered` shape left the raw
  # digest in place instead, so the one failure mode the guard exists for emitted everything
  # unfiltered (2026-07-29 verification round, F4). The digest is an optional extra; the contract
  # itself still goes out.
  if command -v qq_redact_filter_lines >/dev/null 2>&1; then
    dg="$(printf '%s' "$dg" | qq_redact_filter_lines "$tp")"
  else
    dg=""
  fi
  [ -n "$dg" ] && c="$c"$'\n\n'"$dg"
fi
jq -n --arg c "$c" '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$c}}'
