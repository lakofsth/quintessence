#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-reval.sh — B2/B3 reality-binding acceptance gates (read revalidation + triggers),
# end-to-end through the REAL `qq` dispatcher + the REAL refs-resolve.py, on throwaway fixtures:
#   READ REVALIDATION (B2): a bound referent that moves on disk annotates the next brief/show
#           with '⚠ referent changed since written: <id> (<date>)' — and the read path NEVER
#           writes (refs.jsonl byte-identical across reads).
#   TRIGGER PROBE (B3): a commit touching a bound file in a hooked work repo marks the
#           ref suspect within the hook run; the next brief shows the annotation.
#   PARITY: no refs / unchanged refs / QQ_BIND=0 all render byte-identical to today.
# NEVER touches the live store — mktemp fixtures, own QQ_CONFIG/QQ_STATE_DIR, same isolation
# convention as test-bind.sh.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="$ENGINE/qq"
RESOLVE="$ENGINE/refs-resolve.py"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# Fixtures live under mktemp (= /tmp), which the default QQ_BIND_EXCLUDE_ROOTS covers on both
# the bind and resolver sides — neutralize suite-wide (the exclusion itself is covered by the
# py units + test-bind.sh).
export QQ_BIND_EXCLUDE_ROOTS=
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

STORE="$TMP/store"
export QUINTESSENCE_DIR="$STORE" QQ_CONFIG="$STORE.config" QQ_STATE_DIR="$STORE.state" QQ_MEMDIR="$STORE.mem"
mkdir -p "$QQ_MEMDIR"; : > "$QQ_CONFIG"
"$ENGINE/qq" init "$STORE" >/dev/null 2>&1
REFS="$QQ_STATE_DIR/refs/refs.jsonl"

# ---- a hooked own-work repo (the infra post-commit hook's resolver line, verbatim shape) -----
REPO="$TMP/workrepo"
mkdir -p "$REPO"; git -C "$TMP" init -q workrepo
git -C "$REPO" config user.email t@t; git -C "$REPO" config user.name t
printf 'v1\n' > "$REPO/deploy.sh"
git -C "$REPO" add -A; git -C "$REPO" commit -qm one
cat > "$REPO/.git/hooks/post-commit" <<HOOK
#!/bin/sh
# B3 trigger (test fixture): same invocation shape as a real post-commit hook
[ -x "$RESOLVE" ] && "$RESOLVE" commit --repo "\$(git rev-parse --show-toplevel)" || true
HOOK
chmod +x "$REPO/.git/hooks/post-commit"
export QQ_BIND_REPOS="work=$REPO"

# ---- B2: bind, then move the referent OUTSIDE git (pure read-side re-hash) -------------------
LOOSE="$TMP/loose-artifact.conf"
printf 'a=1\n' > "$LOOSE"
QQ_BIND=1 "$QQ" new T "seed" >/dev/null 2>&1
QQ_BIND=1 "$QQ" update T "verified $LOOSE just now" >/dev/null 2>&1
"$QQ" brief T > "$TMP/brief-clean.out" 2>&1
grep -q '⚠' "$TMP/brief-clean.out" && no "B2: clean brief already annotated" || ok "B2: unchanged referent -> no annotation"

sum_before="$(sha256sum "$REFS")"
printf 'a=2\n' > "$LOOSE"
"$QQ" brief T > "$TMP/brief-changed.out" 2>&1
grep -q "⚠ referent changed since written: $LOOSE (" "$TMP/brief-changed.out" \
  && ok "B2: brief annotates the moved referent" || { no "B2: brief annotation missing"; sed 's/^/       /' "$TMP/brief-changed.out"; }
grep -A1 "verified $LOOSE just now" "$TMP/brief-changed.out" | tail -1 | grep -q '^⚠' \
  && ok "B2: annotation sits directly under its update-line" || no "B2: annotation misplaced"
"$QQ" show T > "$TMP/show-changed.out" 2>&1
grep -q "⚠ referent changed since written: $LOOSE (" "$TMP/show-changed.out" \
  && ok "B2: show annotates too" || no "B2: show annotation missing"
[ "$(sha256sum "$REFS")" = "$sum_before" ] \
  && ok "B2 HARD GATE: reads never wrote refs.jsonl (byte-identical)" \
  || no "B2 HARD GATE VIOLATED: refs.jsonl changed on a read"
QQ_BIND=0 "$QQ" brief T > "$TMP/brief-off.out" 2>&1
grep -q '⚠' "$TMP/brief-off.out" && no "B2: QQ_BIND=0 still annotated" || ok "B2: QQ_BIND=0 disables read-side too"

# ---- PARITY: a refs-free HEAD renders byte-identical with binding on and off -----------------
QQ_BIND=1 "$QQ" new P "plain topic, no referents" >/dev/null 2>&1
QQ_BIND=1 "$QQ" brief P > "$TMP/p-on.out" 2>&1
QQ_BIND=0 "$QQ" brief P > "$TMP/p-off.out" 2>&1
diff -q "$TMP/p-on.out" "$TMP/p-off.out" >/dev/null \
  && ok "parity: refs-free brief byte-identical (QQ_BIND on/off)" || no "parity: refs-free brief differs"
QQ_BIND=1 "$QQ" show P > "$TMP/ps-on.out" 2>&1
QQ_BIND=0 "$QQ" show P > "$TMP/ps-off.out" 2>&1
diff -q "$TMP/ps-on.out" "$TMP/ps-off.out" >/dev/null \
  && ok "parity: refs-free show byte-identical (QQ_BIND on/off)" || no "parity: refs-free show differs"

# ---- B3 TRIGGER PROBE: commit touching a bound file -> suspect -> annotated brief ----------
QQ_BIND=1 "$QQ" update T "deployed $REPO/deploy.sh to the fixture fleet" >/dev/null 2>&1
grep -qF "\"$REPO/deploy.sh\"" "$REFS" && ok "B3: repo file bound at write" || no "B3: bind missing"
printf 'v2\n' > "$REPO/deploy.sh"
git -C "$REPO" add -A; git -C "$REPO" commit -qm two   # post-commit hook fires the resolver
grep -qF '"status": "suspect"' "$REFS" \
  && ok "B3: ref suspect within the hook run" || { no "B3: no suspect after hooked commit"; sed 's/^/       /' "$REFS"; }
grep -qF "\"$REPO\"" "$QQ_STATE_DIR/refs/events.jsonl" 2>/dev/null \
  && ok "B3: {repo, sha, changed_paths} event appended" || no "B3: event missing from events.jsonl"
"$QQ" brief T > "$TMP/brief-trigger.out" 2>&1
grep -q "⚠ referent changed since written: $REPO/deploy.sh (" "$TMP/brief-trigger.out" \
  && ok "B3->B2: next brief shows the annotation" || { no "B3->B2: annotation missing"; sed 's/^/       /' "$TMP/brief-trigger.out"; }

# ---- B3 debounce: a second commit updates the SAME suspect record, no re-flip ---------------
n_suspect_before="$(grep -cF '"status": "suspect"' "$REFS")"
printf 'v3\n' > "$REPO/deploy.sh"
git -C "$REPO" add -A; git -C "$REPO" commit -qm three
n_suspect_after="$(grep -cF '"status": "suspect"' "$REFS")"
[ "$n_suspect_after" -eq "$n_suspect_before" ] \
  && ok "B3: hot file stays ONE suspect record (debounce)" || no "B3: suspect count changed ($n_suspect_before -> $n_suspect_after)"
grep -qF '"suspect_fp": "sha256:' "$REFS" && ok "B3: latest fp recorded on the suspect" || no "B3: suspect_fp missing"

# ---- resolver is fail-soft from the hook's seat ----------------------------------------------
"$RESOLVE" commit --repo "$TMP/not-a-repo" >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "resolver: exit 0 even on a non-repo (never fails the caller)" || no "resolver: exit $rc on non-repo"
"$RESOLVE" >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "resolver: exit 0 on usage error" || no "resolver: exit $rc on usage error"

echo
[ "$fail" -eq 0 ] && { echo "test-reval.sh: all $pass checks passed"; exit 0; }
echo "test-reval.sh: $fail FAILED ($pass passed)"; exit 1
