#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-lifecycle.sh — end-to-end HEAD lifecycle against an ISOLATED throwaway store:
# init -> new -> update (arg+stdin) -> show -> finalize -> compact -> rewrite -> delete.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="${QQ_BIN:-$ENGINE/qq}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export QUINTESSENCE_DIR="$TMP/store" QQ_CONFIG="$TMP/config" QQ_STATE_DIR="$TMP/state" QQ_MEMDIR="$TMP/mem"
mkdir -p "$QQ_MEMDIR"; : > "$QQ_CONFIG"
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }
H="$QUINTESSENCE_DIR/L.md"

"$QQ" init "$QUINTESSENCE_DIR" >/dev/null 2>&1
[ -d "$QUINTESSENCE_DIR/.git" ] && ok "init creates a git store" || no "init did not create a git store"

"$QQ" new L "lifecycle essence" >/dev/null 2>&1
{ [ -f "$H" ] && grep -q '^> essence: lifecycle essence' "$H"; } && ok "new creates HEAD with essence" || no "new failed"

"$QQ" new L "dup" >/dev/null 2>&1 && no "new clobbered an existing HEAD" || ok "new refuses to clobber"

"$QQ" update L "arg-line-1" >/dev/null 2>&1
echo "stdin-line-2" | "$QQ" update L >/dev/null 2>&1
{ grep -q 'arg-line-1' "$H" && grep -q 'stdin-line-2' "$H"; } && ok "update lands both arg and stdin forms" || no "update lost a line"

"$QQ" show L 2>/dev/null | grep -q 'lifecycle essence' && ok "show renders the HEAD" || no "show failed"

"$QQ" essence L "refreshed essence" >/dev/null 2>&1
grep -q '^> essence: refreshed essence' "$H" && ok "essence refreshes in place" || no "essence did not refresh"

"$QQ" finalize L >/dev/null 2>&1
ls "$QUINTESSENCE_DIR"/journal/L/*.md >/dev/null 2>&1 && ok "finalize journals a snapshot" || no "finalize did not journal"

# compact: add enough update-lines to exceed keep-N, then trim to 2
for i in 1 2 3 4 5; do "$QQ" update L "bulk-$i" >/dev/null 2>&1; done
before_ul="$(grep -c '^> updated:' "$H")"
"$QQ" compact L 2 >/dev/null 2>&1
after_ul="$(grep -c '^> updated:' "$H")"
{ [ "$after_ul" -lt "$before_ul" ] && [ "$after_ul" -le 2 ] && head -1 "$H" | grep -q '^# '; } \
  && ok "compact trims update-lines, keeps H1 ($before_ul->$after_ul)" || no "compact wrong ($before_ul->$after_ul)"

# compact regression: a multi-paragraph update-line (a `> updated:` line + bare continuation
# paragraphs) must fold WITH its parent — a dropped block takes its continuations with it, and a
# kept block keeps its own. Guards the orphaned-continuation bug.
HM="$QUINTESSENCE_DIR/M.md"
"$QQ" new M "essence-m" >/dev/null 2>&1
cat > "$TMP/m.md" <<'MEOF'
# Quintessence — M
> updated: 2026-06-22T03:00:00Z newest line.
KEEP-CONT-MARKER continuation of the newest block.
> updated: 2026-06-22T02:00:00Z middle line.
> updated: 2026-06-22T01:00:00Z oldest line.
ORPHAN-CONT-MARKER continuation of a dropped block.
> essence: essence-m
## RE-ENTER HERE
BODY-MARKER stays.
MEOF
"$QQ" rewrite M < "$TMP/m.md" >/dev/null 2>&1
"$QQ" compact M 1 >/dev/null 2>&1
{ [ "$(grep -c '^> updated:' "$HM")" -eq 1 ] \
  && grep -q 'KEEP-CONT-MARKER' "$HM" \
  && ! grep -q 'ORPHAN-CONT-MARKER' "$HM" \
  && grep -q 'BODY-MARKER' "$HM"; } \
  && ok "compact folds multi-paragraph update-lines with their parent" \
  || no "compact stranded a continuation line (orphan bug)"
"$QQ" delete M >/dev/null 2>&1

# rewrite: whole-file replace from stdin (must preserve being a valid HEAD)
"$QQ" show L > "$TMP/draft.md" 2>/dev/null
printf '\n> updated: %s rewrite-marker\n' "$(date -u +%FT%TZ)" >> "$TMP/draft.md"
"$QQ" rewrite L < "$TMP/draft.md" >/dev/null 2>&1
grep -q 'rewrite-marker' "$H" && ok "rewrite replaces from stdin" || no "rewrite failed"

# delete: removes the HEAD but journals its final state (recoverable)
"$QQ" delete L >/dev/null 2>&1
{ [ ! -f "$H" ] && ls "$QUINTESSENCE_DIR"/journal/L/*.md >/dev/null 2>&1; } \
  && ok "delete removes HEAD but keeps journal" || no "delete wrong (HEAD present? $( [ -f "$H" ] && echo yes))"
"$QQ" delete L >/dev/null 2>&1 && no "delete of a missing HEAD should error" || ok "delete errors on missing HEAD"

echo "----- $pass passed, $fail failed -----"
[ "$fail" -eq 0 ]
