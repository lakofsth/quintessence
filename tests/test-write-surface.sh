#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-write-surface.sh — regression matrix for the `qq` write-verb argument surface.
#
# Guards the class of transitional/usage bugs where an old-surface flag (notably
# `--prepend-update`, an INTERNAL qq-write flag) leaks into a `qq <verb>` positional and is
# silently written as literal content while the real stdin is discarded. Runs entirely against
# an ISOLATED throwaway store (its own QUINTESSENCE_DIR + QQ_CONFIG) so it never touches a live
# install. Exit 0 = all pass.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="${QQ_BIN:-$ENGINE/qq}"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export QUINTESSENCE_DIR="$TMP/store"
export QQ_CONFIG="$TMP/config"           # isolate the GLOBAL config (lesson: tests must, or they mutate the live install)
export QQ_STATE_DIR="$TMP/state"
export QQ_MEMDIR="$TMP/mem"; mkdir -p "$QQ_MEMDIR"
: > "$QQ_CONFIG"

fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }
# assert the HEAD's newest update-line carries EXACTLY the expected text (after the `> updated: <ts> ` prefix)
newest_update(){ grep -m1 '^> updated:' "$QUINTESSENCE_DIR/$1.md" | sed -E 's/^> updated: [^ ]+ ?//'; }
head_has(){ grep -qF -- "$2" "$QUINTESSENCE_DIR/$1.md"; }

"$QQ" init "$QUINTESSENCE_DIR" >/dev/null 2>&1 || { echo "init failed"; exit 1; }
"$QQ" new T "seed essence" >/dev/null 2>&1 || { echo "qq new failed"; exit 1; }

# 1. THE BUG: stdin + leaked --prepend-update flag — stdin must land, flag must NOT become text
echo "AAA-stdin-prepend" | "$QQ" update T --prepend-update >/dev/null 2>&1
if [ "$(newest_update T)" = "AAA-stdin-prepend" ]; then ok "update <t> --prepend-update keeps stdin"; else no "update <t> --prepend-update lost stdin (got: '$(newest_update T)')"; fi
if head_has T "--prepend-update"; then no "literal '--prepend-update' leaked into the HEAD"; else ok "no flag literal leaked into the HEAD"; fi

# 2. stdin form (no flag) — must land
echo "BBB-stdin" | "$QQ" update T >/dev/null 2>&1
[ "$(newest_update T)" = "BBB-stdin" ] && ok "update <t> (stdin) lands" || no "update <t> (stdin) lost (got: '$(newest_update T)')"

# 3. arg form — must land
"$QQ" update T "CCC-arg" >/dev/null 2>&1
[ "$(newest_update T)" = "CCC-arg" ] && ok "update <t> \"text\" lands" || no "update <t> arg lost (got: '$(newest_update T)')"

# 4. unknown stray flag — must ERROR (non-zero), must NOT write the flag as text
before="$(grep -c '^> updated:' "$QUINTESSENCE_DIR/T.md")"
"$QQ" update T --bogus-flag >/dev/null 2>&1; rc=$?
after="$(grep -c '^> updated:' "$QUINTESSENCE_DIR/T.md")"
{ [ "$rc" -ne 0 ] && [ "$before" = "$after" ] && ! head_has T "--bogus-flag"; } \
  && ok "update <t> --bogus-flag errors without writing" || no "update <t> --bogus-flag did not cleanly error (rc=$rc, lines $before->$after)"

# 5. essence with leaked flag — must not silently become the essence
"$QQ" essence T --prepend-update >/dev/null 2>&1
grep -q '^> essence: --prepend-update' "$QUINTESSENCE_DIR/T.md" && no "essence swallowed --prepend-update as the essence" || ok "essence rejects/ignores leaked flag"

# 6. new with a leaked flag — must error, must not create a HEAD whose essence is the flag
"$QQ" new Tnew --prepend-update >/dev/null 2>&1
[ ! -f "$QUINTESSENCE_DIR/Tnew.md" ] && ok "new rejects leaked flag (no HEAD created)" || no "new created a HEAD from a leaked flag"

# 7. the -- escape hatch: text that must start with -- still lands as literal content
echo "ignored" | "$QQ" update T -- --literal-dashes >/dev/null 2>&1
[ "$(newest_update T)" = "--literal-dashes" ] && ok "-- escape forces literal dash-leading text" || no "-- escape failed (got: '$(newest_update T)')"

# 8. flag-shaped TOPIC — the '--help.md HEAD' class: a guessed flag in the topic position must
# be refused, never minted as a HEAD (a real --help.md was created this way once; both engines
# were asserted here until the bash engine left the tree — its side is pinned in git history).
"$QQ" new --flag-topic "junk essence" >/dev/null 2>&1; rc=$?
{ [ "$rc" -ne 0 ] && [ ! -f "$QUINTESSENCE_DIR/--flag-topic.md" ] && [ ! -f "$QUINTESSENCE_DIR/flag-topic.md" ]; } \
  && ok "qq new --flag-topic refused, no HEAD minted" \
  || no "qq new --flag-topic not refused (rc=$rc, files: $(ls "$QUINTESSENCE_DIR" | grep flag-topic || true))"

# 9. the advice loop-breaker: a verb hitting a flag-shaped topic must name the flag problem,
# NOT emit the '(qq new <topic> first)' advice that used to instruct the agent to mint the junk HEAD.
err="$("$QQ" essence --flag-topic "e" 2>&1)"; rc=$?
{ [ "$rc" -ne 0 ] && printf '%s' "$err" | grep -q "looks like a command-line flag" && ! printf '%s' "$err" | grep -q "qq new --flag-topic"; } \
  && ok "essence --flag-topic names the flag problem instead of advising 'qq new' it" \
  || no "essence --flag-topic still advises minting the flag HEAD (rc=$rc: $err)"

# 10. read verbs inherit the same guard (choke point, not per-verb patching)
"$QQ" show --flag-topic >/dev/null 2>&1; rc=$?
[ "$rc" -ne 0 ] && ok "show --flag-topic refused via the shared choke point" || no "show --flag-topic accepted a flag-shaped topic (rc=$rc)"

# --- timestamp fabrication guard (2026-07-09): qq owns the update stamp; rewrite rejects future ---
newest_stamp(){ grep -m1 '^> updated:' "$QUINTESSENCE_DIR/$1.md" | awk '{print $3}'; }
FUT="$(date -u -d '+2 days' +%Y-%m-%dT%H:%M:%SZ)"

# 11. `qq update` IGNORES a caller-supplied '> updated:' marker — qq stamps now(), not the caller's
# (future) ts, and the marker is stripped so the prose lands cleanly.
"$QQ" update T "> updated: $FUT fabricated-future-line" >/dev/null 2>&1
{ [ "$(newest_update T)" = "fabricated-future-line" ] && [ "$(newest_stamp T)" != "$FUT" ]; } \
  && ok "update ignores a caller '> updated:' marker (qq stamps now, not the caller's future ts)" \
  || no "update honored a caller future marker (stamp=$(newest_stamp T) fut=$FUT text='$(newest_update T)')"

# 12. `qq update` strips a leading BARE ISO timestamp too (the other fabrication form).
"$QQ" update T "$FUT leading-iso-line" >/dev/null 2>&1
{ [ "$(newest_update T)" = "leading-iso-line" ] && [ "$(newest_stamp T)" != "$FUT" ]; } \
  && ok "update strips a leading bare ISO timestamp (qq stamps now)" \
  || no "update kept a leading caller ISO (stamp=$(newest_stamp T) text='$(newest_update T)')"

"$QQ" new RW "rw essence" >/dev/null 2>&1
# 13. `qq rewrite` REFUSES a whole-file replace carrying a FUTURE '> updated:' stamp (HEAD unchanged).
rw_before="$(cat "$QUINTESSENCE_DIR/RW.md")"
printf '# Quintessence — RW\n> updated: %s FABRICATED-FUTURE\n> essence: rw essence\n' "$FUT" | "$QQ" rewrite RW >/dev/null 2>&1; rc=$?
{ [ "$rc" -ne 0 ] && [ "$rw_before" = "$(cat "$QUINTESSENCE_DIR/RW.md")" ]; } \
  && ok "rewrite refuses a FUTURE-stamped line (rc=$rc, HEAD unchanged)" \
  || no "rewrite accepted a future stamp (rc=$rc)"

# 14. `--allow-future` is the escape hatch for a deliberate timestamp repair/migration.
printf '# Quintessence — RW\n> updated: %s DELIBERATE-FUTURE\n> essence: rw essence\n' "$FUT" | "$QQ" rewrite RW --allow-future >/dev/null 2>&1; rc=$?
{ [ "$rc" -eq 0 ] && head_has RW "DELIBERATE-FUTURE"; } \
  && ok "rewrite --allow-future accepts a deliberate future stamp" \
  || no "rewrite --allow-future did not land (rc=$rc)"

# 15. A PAST stamp is fine — the guard is future-only (repairs/migrations of historical stamps work).
printf '# Quintessence — RW\n> updated: 2020-01-01T00:00:00Z PAST-OK\n> essence: rw essence\n' | "$QQ" rewrite RW >/dev/null 2>&1; rc=$?
{ [ "$rc" -eq 0 ] && head_has RW "PAST-OK"; } \
  && ok "rewrite accepts a PAST stamp (guard is future-only)" \
  || no "rewrite wrongly rejected a past stamp (rc=$rc)"

echo "----- $pass passed, $fail failed -----"
[ "$fail" -eq 0 ]
