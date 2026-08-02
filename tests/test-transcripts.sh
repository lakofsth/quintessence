#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-transcripts.sh — `qq ask --transcripts` regression, NO live-model/live-corpus dependency:
#   (a) --transcripts exits 0 and prints the TRANSCRIPT banner (not the store SNAPSHOT banner),
#       even fully degraded (no embedder, no completion endpoint) — grep_fallback path.
#   (b) plain `qq ask` (no flag) still prints the store SNAPSHOT banner, unaffected.
#   (c) --json --transcripts includes a "banner" key set to the transcript banner text.
#   (d) render() unit: banner=None defaults to SNAPSHOT_BANNER (the MCP tool's call shape).
# Isolated store/config/state/embedder per the tests/ convention (see test-ask.sh); --transcripts
# hardcodes ~/kb-transcripts / ~/.cache/qq-search/embeddings-transcripts.json (by design — a CLI
# flag, not env-configurable) but the degraded path never reads/writes either, so this is safe to
# run against a real install too. Exit 0 = all pass.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="${QQ_BIN:-$ENGINE/qq}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export QUINTESSENCE_DIR="$TMP/store" QQ_CONFIG="$TMP/config" QQ_STATE_DIR="$TMP/state" QQ_MEMDIR="$TMP/mem"
export QQ_KB_ROOT="$TMP/kb" QQ_CACHE="$TMP/cache.json"   # isolate store-mode too (P3: D4 cache
                                                          # identity keys off QQ_KB_ROOT+model, but
                                                          # the identity FILE still sits beside
                                                          # QQ_CACHE's directory -- leaving QQ_CACHE
                                                          # at its real-home default would litter
                                                          # ~/.cache/qq-search/ every run)
export QQ_OLLAMA_URL="http://127.0.0.1:1"     # dead port -> embedder unreachable, keyword fallback
export QQ_ASK_ENDPOINTS="http://127.0.0.1:1"  # dead port -> no completion endpoint healthy
mkdir -p "$QQ_MEMDIR"; : > "$QQ_CONFIG"
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

"$QQ" init "$QUINTESSENCE_DIR" >/dev/null 2>&1

# --- (a) --transcripts: exit 0, TRANSCRIPT banner, not the store banner --------------------
out="$("$QQ" ask --transcripts "xyzzy-sentinel" 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ok "qq ask --transcripts exits 0 fully degraded (rc=$rc)" || no "qq ask --transcripts exited non-zero (rc=$rc)"
printf '%s' "$out" | grep -qF "transcript = unverified working memory, not a decision record — verify at source" \
  && ok "qq ask --transcripts prints the TRANSCRIPT banner" || no "missing transcript banner: '$out'"
printf '%s' "$out" | grep -qF "point-in-time snapshot" \
  && no "qq ask --transcripts leaked the store SNAPSHOT banner too: '$out'" || ok "qq ask --transcripts does NOT print the store banner"

# --- (b) plain `qq ask` (no flag): unchanged store SNAPSHOT banner --------------------------
out2="$("$QQ" ask "xyzzy-sentinel" 2>&1)"; rc2=$?
[ "$rc2" -eq 0 ] && ok "qq ask (store mode) still exits 0 (rc=$rc2)" || no "qq ask (store mode) exited non-zero (rc=$rc2)"
printf '%s' "$out2" | grep -qF "point-in-time snapshot" \
  && ok "qq ask (store mode) prints the store SNAPSHOT banner" || no "store-mode banner missing: '$out2'"
printf '%s' "$out2" | grep -qF "transcript = unverified working memory" \
  && no "qq ask (store mode) leaked the transcript banner: '$out2'" || ok "qq ask (store mode) does NOT print the transcript banner"

# --- (c) --json --transcripts: banner key present with the transcript text -----------------
jout="$("$QQ" ask --transcripts --json "xyzzy-sentinel" 2>&1)"
printf '%s' "$jout" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert d.get('banner') == 'transcript = unverified working memory, not a decision record — verify at source', d
" 2>/dev/null && ok "qq ask --transcripts --json carries the transcript banner key" \
  || no "--json --transcripts banner key wrong/missing: '$jout'"

# --- (d) render() unit: banner=None default -> SNAPSHOT_BANNER (the MCP call shape) ---------
rd="$(python3 -c "
import sys; sys.path.insert(0, '$ENGINE')
from quintessence.ask import render, SNAPSHOT_BANNER
r = {'answer': 'a', 'sources': []}
print(render(r).splitlines()[-1] == SNAPSHOT_BANNER)
")"
[ "$rd" = "True" ] && ok "render(result) with no banner arg defaults to SNAPSHOT_BANNER" || no "render() default banner regression"

echo "----- $pass passed, $fail failed -----"
[ "$fail" -eq 0 ]
