#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-essence-lag.sh — the [T1 essence-lag] consistency flag (qq check): an update-line that
# SUPERSEDES/CORRECTS a phrase still present verbatim in the essence must be flagged; refreshing
# the essence clears it; a marker-less line must NOT flag (precision); QQ_ESSENCE_LAG=0 suppresses.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="${QQ_BIN:-$ENGINE/qq}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export QUINTESSENCE_DIR="$TMP/store" QQ_CONFIG="$TMP/config" QQ_STATE_DIR="$TMP/state" QQ_MEMDIR="$TMP/mem" QQ_KB_ROOT="$TMP/kb" QQ_CACHE="$TMP/cache.json"
mkdir -p "$QQ_MEMDIR" "$QQ_KB_ROOT"; : > "$QQ_CONFIG"
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }
flagged(){ "$QQ" check 2>/dev/null | grep -q "\[T1 essence-lag\] HEAD $1"; }

"$QQ" init "$QUINTESSENCE_DIR" >/dev/null 2>&1
P="the gate runs on the substrate"   # >=16 chars, the claim under test

# LAG: essence asserts P; a later update-line SUPERSEDES it, quoting P -> must flag.
"$QQ" new lag "summary: $P with full CoT" >/dev/null 2>&1
"$QQ" update lag "CORRECTION: this SUPERSEDES the claim '$P' — it no longer holds." >/dev/null 2>&1
flagged lag && ok "flags a stale essence superseded by an update-line" || no "missed the essence-lag"

# CLEAR: refresh the essence so it no longer contains P -> must clear.
"$QQ" essence lag "summary: the gate now runs on the resident reviewer" >/dev/null 2>&1
flagged lag && no "still flags after the essence refresh" || ok "clears once the essence is refreshed"

# PRECISION: P in both essence and an update-line, but NO supersession marker -> must NOT flag.
"$QQ" new nomark "summary: $P today" >/dev/null 2>&1
"$QQ" update nomark "routine note repeating '$P' verbatim, just ambient context" >/dev/null 2>&1
flagged nomark && no "false-positive on a marker-less line" || ok "no flag without a supersession marker"

# TOGGLE: QQ_ESSENCE_LAG=0 suppresses even a real lag.
"$QQ" new lag2 "summary: $P here" >/dev/null 2>&1
"$QQ" update lag2 "this is now STALE — SUPERSEDES '$P'." >/dev/null 2>&1
QQ_ESSENCE_LAG=0 "$QQ" check 2>/dev/null | grep -q "\[T1 essence-lag\] HEAD lag2" \
  && no "QQ_ESSENCE_LAG=0 did not suppress" || ok "QQ_ESSENCE_LAG=0 suppresses the check"

echo "----"
[ "$fail" -eq 0 ] && echo "test-essence-lag: all $pass passed" || echo "test-essence-lag: $pass passed, $fail FAILED"
[ "$fail" -eq 0 ]
