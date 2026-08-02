#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-size-nudge.sh — the compaction nudge must size the UPDATE-LINE REGION, not the whole file.
# A HEAD with a large CURATED body but few/lean update-lines must NOT be nudged to `qq compact`
# (compaction can't shrink a body); instead qq check emits a distinct body-size note. A HEAD with
# a bloated update-log MUST still get the compact nudge. Regression for: total-bytes mis-firing the
# compact advice on big-body HEADs (where compaction is a no-op).
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="${QQ_BIN:-$ENGINE/qq}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export QUINTESSENCE_DIR="$TMP/store" QQ_CONFIG="$TMP/config" QQ_STATE_DIR="$TMP/state" QQ_MEMDIR="$TMP/mem" QQ_KB_ROOT="$TMP/kb" QQ_CACHE="$TMP/cache.json"
mkdir -p "$QQ_MEMDIR" "$QQ_KB_ROOT"; : > "$QQ_CONFIG"
# Small thresholds so the test is cheap + deterministic. BODY ceiling low; update-line-region byte
# gate high (so only the LINE COUNT or a genuinely huge update-region trips the compact nudge).
export QQ_BODY_HARD_BYTES=2000 QQ_COMPACT_HARD_BYTES=100000 QQ_COMPACT_HARD_LINES=3
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

"$QQ" init "$QUINTESSENCE_DIR" >/dev/null 2>&1
"$QQ" reindex >/dev/null 2>&1

# --- HEAD BIGBODY: 1 lean update-line + a ~4kB body of ## sections ---
big="$(head -c 4000 < /dev/zero | tr '\0' x)"
"$QQ" new bigbody "essence-bb" >/dev/null 2>&1
cat > "$TMP/bb.md" <<BBEOF
# Quintessence — bigbody
> updated: 2026-06-23T00:00:00Z one short line.
> essence: essence-bb
## Section
$big
BBEOF
"$QQ" rewrite bigbody < "$TMP/bb.md" >/dev/null 2>&1

out="$("$QQ" check --fast 2>/dev/null)"
printf '%s' "$out" | grep -q 'HEAD bigbody:.*qq compact bigbody' \
  && no "big body wrongly nudged to \`qq compact\` (the bug)" \
  || ok "big body is NOT nudged to compact (compaction can't shrink a body)"
printf '%s' "$out" | grep -Eq 'HEAD bigbody: body [0-9]+kB \(update-lines lean\)' \
  && ok "big body gets the distinct body-size note instead" \
  || no "big body did not get the body-size note"

# --- HEAD BLOAT: many update-lines, tiny body ---
"$QQ" new bloat "essence-bl" >/dev/null 2>&1
for i in 1 2 3 4 5; do "$QQ" update bloat "bulk update line $i" >/dev/null 2>&1; done
out="$("$QQ" check --fast 2>/dev/null)"
printf '%s' "$out" | grep -Eq 'HEAD bloat: update-lines [0-9]+kB / [0-9]+ lines .*qq compact bloat' \
  && ok "bloated update-log IS nudged to compact" \
  || no "bloated update-log was not nudged to compact"

# --- ul_region_bytes excludes the body (sanity on the helper via the lib) ---
# shellcheck disable=SC1090
. "$ENGINE/qq-lib.sh"
rb="$(ul_region_bytes "$QUINTESSENCE_DIR/bigbody.md")"
fb="$(wc -c <"$QUINTESSENCE_DIR/bigbody.md")"
{ [ "$rb" -lt 200 ] && [ "$fb" -gt 4000 ]; } \
  && ok "ul_region_bytes measures only the update-line region ($rb of $fb file bytes)" \
  || no "ul_region_bytes wrong (region=$rb file=$fb)"

echo "----"
[ "$fail" -eq 0 ] && echo "test-size-nudge: all $pass passed" || echo "test-size-nudge: $pass passed, $fail FAILED"
[ "$fail" -eq 0 ]
