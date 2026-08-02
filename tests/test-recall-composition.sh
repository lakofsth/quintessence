#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-recall-composition.sh — recall composition end-to-end, NO live-model dependency
# (dead-port embedder -> deterministic keyword/grep fallback, matching the tests/ convention —
# see test-ask.sh/test-degraded.sh):
#   PART A: THE GOLDEN GATE -- the degenerate case (no project store yet) carries no 'store' key
#           in qq-search/qq-ask JSON output and no store-tag in text rendering.
#   PART B: the SAME golden-gate shape holds under -g/--global from INSIDE a real project store
#           (not just "no project store ever existed") -- proves --global genuinely bypasses
#           composition rather than composition just never triggering by circumstance.
#   PART C: composed case -- from inside the project, without --global, qq-search/qq-ask union
#           BOTH stores, each hit tagged with its owning store.
#   PART D: --reindex composes across both stores without crashing.
#   PART E: GC rider -- a stale per-identity cache/sidecar is swept opportunistically; a *.lock
#           file survives regardless of age (the judge's hard constraint).
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="${QQ_BIN:-$ENGINE/qq}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

export HOME="$TMP"
# Defensive: an inherited QUINTESSENCE_DIR/QQ_MEMDIR (e.g. from setup.sh's own environment, or a
# shell that did `export QUINTESSENCE_DIR=...` per README's manual install path) would pin and
# beat this test's HOME-based discovery — env always wins over config by design (human-1). Strip
# both before anything below relies on walk-up discovery landing under TMP.
unset QUINTESSENCE_DIR QQ_MEMDIR
export QQ_CONFIG="$TMP/config" QQ_STATE_DIR="$TMP/state" QQ_MEMDIR="$TMP/mem"
export QQ_KB_ROOT="$TMP/kb" QQ_CACHE="$TMP/cache/embeddings.json"
export QQ_OLLAMA_URL="http://127.0.0.1:1"     # dead port -> deterministic keyword fallback
export QQ_ASK_ENDPOINTS="http://127.0.0.1:1"  # dead port -> no completion endpoint healthy
mkdir -p "$QQ_MEMDIR" "$QQ_KB_ROOT"; : > "$QQ_CONFIG"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

USER_STORE="$TMP/quintessence"
PROJ="$TMP/work/repo"
PSTORE="$PROJ/.quintessence"
mkdir -p "$PROJ/src"

fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

"$QQ" init "$USER_STORE" >/dev/null 2>&1
ln -s "$USER_STORE" "$QQ_KB_ROOT/qq"    # a real corpus source subdir (mirrors test-ask.sh)
"$QQ" new usershared "shared-marker-xyzzy content lives in the user store" >/dev/null 2>&1

# ============================================================================================
# PART A -- THE GOLDEN GATE: the degenerate case (no project store on the path yet)
# ============================================================================================
outA_text="$("$QQ" search "shared-marker-xyzzy" 2>/dev/null)"
outA_json="$("$QQ" search --json "shared-marker-xyzzy" 2>/dev/null)"
echo "$outA_json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert all('store' not in h for h in d), d
" 2>/dev/null && ok "degenerate qq-search --json carries no 'store' key" \
  || no "degenerate qq-search --json unexpectedly carries a 'store' key: $outA_json"
echo "$outA_text" | grep -qE '\(qq/' \
  && no "degenerate qq-search text output carries a store-tag slash" \
  || ok "degenerate qq-search text output has no store-tag slash"

askA_json="$("$QQ" ask --json "shared-marker-xyzzy" 2>/dev/null)"
echo "$askA_json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert all('store' not in s for s in d.get('sources', [])), d
" 2>/dev/null && ok "degenerate qq-ask --json sources carry no 'store' key" \
  || no "degenerate qq-ask --json sources unexpectedly carry a 'store' key: $askA_json"

# ============================================================================================
# PART B -- create a project store with its OWN HEAD; PART A's degenerate calls must render
# byte-identical when re-run WITH -g/--global from inside that project.
# ============================================================================================
( cd "$PROJ" && "$QQ" init --project >/dev/null 2>&1 )
( cd "$PROJ/src" && "$QQ" new projshared "shared-marker-xyzzy content lives in the project store too" >/dev/null 2>&1 )

outB_text="$( cd "$PROJ/src" && "$QQ" search -g "shared-marker-xyzzy" 2>/dev/null )"
[ "$outB_text" = "$outA_text" ] && ok "qq search -g from inside a real project == the degenerate output verbatim" \
  || no "qq search -g diverged from the degenerate baseline"
outB_json="$( cd "$PROJ/src" && "$QQ" search --json -g "shared-marker-xyzzy" 2>/dev/null )"
[ "$outB_json" = "$outA_json" ] && ok "qq search --json -g == the degenerate JSON verbatim" \
  || no "qq search --json -g diverged from the degenerate JSON baseline"
askB_json="$( cd "$PROJ/src" && "$QQ" ask --json -g "shared-marker-xyzzy" 2>/dev/null )"
[ "$askB_json" = "$askA_json" ] && ok "qq ask --json -g == the degenerate JSON verbatim" \
  || no "qq ask --json -g diverged from the degenerate JSON baseline"

# ============================================================================================
# PART C -- composed case: from inside the project, WITHOUT --global, both stores contribute.
# ============================================================================================
outC_json="$( cd "$PROJ/src" && "$QQ" search --json -k 10 "shared-marker-xyzzy" 2>/dev/null )"
stores="$(echo "$outC_json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(','.join(sorted(set(h.get('store', 'MISSING') for h in d))))
" 2>/dev/null)"
[ "$stores" = "project,user" ] && ok "composed qq-search --json unions BOTH stores (project,user)" \
  || no "composed qq-search --json store set wrong: '$stores'"

outC_text="$( cd "$PROJ/src" && "$QQ" search -k 10 "shared-marker-xyzzy" 2>/dev/null )"
echo "$outC_text" | grep -q '(qq/project)' && ok "composed qq-search text tags a project hit" \
  || no "composed qq-search text missing the project tag: $outC_text"
echo "$outC_text" | grep -q '(qq/user)' && ok "composed qq-search text tags a user hit" \
  || no "composed qq-search text missing the user tag: $outC_text"

askC_json="$( cd "$PROJ/src" && "$QQ" ask --json -k 10 "shared-marker-xyzzy" 2>/dev/null )"
askC_stores="$(echo "$askC_json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(','.join(sorted(set(s.get('store', 'MISSING') for s in d.get('sources', [])))))
" 2>/dev/null)"
[ "$askC_stores" = "project,user" ] && ok "composed qq-ask --json sources union BOTH stores" \
  || no "composed qq-ask --json source-store set wrong: '$askC_stores'"

# ============================================================================================
# PART D -- --reindex composes across both stores without crashing.
# ============================================================================================
( cd "$PROJ/src" && "$QQ" search --reindex "shared-marker-xyzzy" >/dev/null 2>&1 ); rc=$?
[ "$rc" -eq 0 ] && ok "composed --reindex exits 0 across both stores" \
  || no "composed --reindex exited non-zero (rc=$rc)"

# ============================================================================================
# PART E -- GC rider: a stale identity cache + sidecar is swept opportunistically; a *.lock
# file survives regardless of age (the judge's HARD CONSTRAINT).
# ============================================================================================
CACHE_DIR="$(dirname "$QQ_CACHE")"
mkdir -p "$CACHE_DIR"
stale_json="$CACHE_DIR/embeddings.deadbeef-model-v1.json"
stale_sidecar="$CACHE_DIR/embeddings.deadbeef-model-v1.json.orphan-ages.json"
stale_lock="$CACHE_DIR/embeddings.deadbeef-model-v1.json.lock"
echo '{}' > "$stale_json"; echo '{}' > "$stale_sidecar"; : > "$stale_lock"
python3 -c "
import os
old = 1  # epoch second 1 -- an eternity older than any GC_DAYS cutoff this test uses
for p in ('$stale_json', '$stale_sidecar', '$stale_lock'):
    os.utime(p, (old, old))
"

# GC only runs at build_index() start (see quintessence/search.py's _gc_stale_identity_caches
# docstring) -- a fully degraded (dead-embedder) plain search takes the grep_fallback path,
# which never calls build_index(). --reindex explicitly force-rebuilds every composed store's
# index, so it's the reliable trigger here (and matches a command a real deployment would run).
( cd "$PROJ/src" && QQ_CACHE_GC_DAYS=1 "$QQ" search --reindex "shared-marker-xyzzy" >/dev/null 2>&1 )

[ -f "$stale_json" ] && no "GC did not sweep a stale identity cache file" \
  || ok "GC swept the stale identity cache file"
[ -f "$stale_sidecar" ] && no "GC did not sweep the stale orphan-ages sidecar" \
  || ok "GC swept the stale orphan-ages sidecar"
[ -f "$stale_lock" ] && ok "GC left a *.lock file untouched regardless of age (hard constraint)" \
  || no "GC REMOVED a *.lock file -- mutual-exclusion break, must never happen"

echo "----- $pass passed, $fail failed -----"
[ "$fail" -eq 0 ]
