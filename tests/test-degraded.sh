#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-degraded.sh — with the embedder unreachable, `qq search` must DEGRADE (keyword fallback +
# a loud warning), not crash and not silently return empty (the Segment-C regression). Forces the
# embedder down by pointing QQ_OLLAMA_URL at a dead port.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="${QQ_BIN:-$ENGINE/qq}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export QUINTESSENCE_DIR="$TMP/store" QQ_CONFIG="$TMP/config" QQ_STATE_DIR="$TMP/state" QQ_MEMDIR="$TMP/mem"
# kb/qq -> store, the same symlink-farm shape a real deployment uses (see test-redact.sh's
# integration section) -- SearchIndex's default per-subdirectory source scan only sees a store's
# top-level HEADs through a source SUBDIRECTORY of QQ_KB_ROOT, never QQ_KB_ROOT itself (the exact
# gap search.py's own _iter_sources docstring documents); without this the corpus below is
# invisible to grep_fallback and every assertion here would only ever exercise the "warned, but
# found nothing" branch, not a real degraded HIT.
export QQ_KB_ROOT="$TMP/kb" QQ_CACHE="$TMP/cache.json"
export QQ_OLLAMA_URL="http://127.0.0.1:1"   # dead port -> embedder unreachable
mkdir -p "$QQ_MEMDIR" "$QQ_KB_ROOT"; : > "$QQ_CONFIG"
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

"$QQ" init "$QUINTESSENCE_DIR" >/dev/null 2>&1
ln -s "$QUINTESSENCE_DIR" "$QQ_KB_ROOT/qq"
"$QQ" new degraded-probe "a HEAD mentioning xyzzy-sentinel for keyword fallback" >/dev/null 2>&1

# run a search with the embedder down; capture both streams + exit code
out="$("$QQ" search "xyzzy-sentinel" 2>&1)"; rc=$?

# 1. must not crash (a clean degrade is exit 0; tolerate a documented non-zero, but never a 2/127 shell blowup)
{ [ "$rc" -eq 0 ] || [ "$rc" -eq 1 ]; } && ok "search exits cleanly with embedder down (rc=$rc)" || no "search blew up with embedder down (rc=$rc)"

# 2. must NOT be silent — either warn about degradation OR surface the keyword hit (not empty)
if printf '%s' "$out" | grep -qiE 'degrad|keyword|fallback|embedder|unreachable|ollama'; then
  ok "search warns loudly when degraded (not silent)"
elif printf '%s' "$out" | grep -q 'xyzzy-sentinel'; then
  ok "search keyword-fallback surfaced the hit"
else
  no "search was silent/empty with embedder down (the regression): '$out'"
fi


# recall-01: `qq ask`/ask_continuity used to silently drop the same keyword-fallback signal
# `qq search` already warns loudly about above -- a completion endpoint (also dead here, so this
# exercises the --json path's `degraded`/`retrieval_degraded` fields together with the no-
# endpoint `degraded` field, never touching a live model) never mattered: the bug was that
# retrieval-level degradation (embedder down) was invisible in ask()'s own return value
# regardless of endpoint health.
ask_json="$("$QQ" ask --json "xyzzy-sentinel" 2>&1)"; ask_rc=$?
[ "$ask_rc" -eq 0 ] && ok "ask exits cleanly with embedder down (rc=$ask_rc)" || no "ask blew up with embedder down (rc=$ask_rc)"
printf '%s' "$ask_json" | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    print(f"not JSON: {e}"); sys.exit(1)
sys.exit(0 if d.get("retrieval_degraded") is True else 1)' \
  && ok "ask --json surfaces retrieval_degraded:true with the embedder down" \
  || no "ask --json missing/false retrieval_degraded with the embedder down: $ask_json"

ask_text="$("$QQ" ask "xyzzy-sentinel" 2>&1)"
printf '%s' "$ask_text" | grep -qi 'embedder unreachable' \
  && ok "ask (text mode) prints the same loud degradation warning qq-search uses" \
  || no "ask (text mode) silent about retrieval degradation: $ask_text"

echo "----- $pass passed, $fail failed -----"
[ "$fail" -eq 0 ]
