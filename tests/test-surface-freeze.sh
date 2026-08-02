#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-surface-freeze.sh — originally P0 of the clean-room reimplementation: pins the EXACT byte-for-byte
# output shape of the read verbs (qq menu, qq digest — incl. the findings preamble + MAP pin
# line + last-touched footer, qq findings / findings next, qq brief). Golden files live in
# tests/fixtures/surface-freeze/*.golden, captured from a real run of qq against a small
# deterministic fixture; <<NOW>> / <<KBROOT>> are the only substitutions (the two fields that
# are inherently run-time/install-path dependent).
#
# As of P2 this test runs against $QQ_BIN (default: the python dispatcher `qq`, which renders
# these verbs itself — see quintessence/cli.py) — it is the compatibility gate the clean-room
# spec calls for ("the 12 legacy suites run against the new CLI"), not just an archive of the
# old bash engine. This test is an ORACLE, not a spec: if a verb's rendering changes on
# purpose, regenerate the affected golden(s) deliberately — never hand-edit a golden to make a
# real regression pass.
#
# RATIFIED DEVIATION (P2 of the clean-room port): P0 found and
# froze a real bash wart — `qq digest`'s trailing "last touched" footer is written by code that
# runs only if the ranked-list pipeline exits 0, but under `set -e` a store with <= the internal
# max (12) HEADs makes the pipeline's last statement (`[ N -gt 12 ] && printf ...`) evaluate
# FALSE, a non-zero exit that aborts the whole bash script before the footer code ever runs — so
# on a normal-sized bash-engine store, `qq-legacy digest` (a) never prints "last touched" and
# (b) exits 1. Harmless in production (every caller captures digest via `$(...)` without
# checking the exit code) but real. The python engine (quintessence.cli.render_digest) FIXES
# this: the footer prints whenever the activity log is non-empty, REGARDLESS of HEAD count, and
# `qq digest` always exits 0. So: digest-plain/digest-over-max-quirk's TEXT goldens are
# unchanged (this fix only changes behavior when the store is small AND the activity log is
# non-empty — digest-plain's fixture has no activity log at that point), but their exit-code
# assertions below now expect the NEW (fixed) behavior. digest-small-store-footer.golden is a
# new scenario (2 HEADs + a non-empty activity log) proving the fix. (The OLD engine's side
# was pinned here via .legacy.golden fixtures asserted directly against qq-legacy, until the
# bash engine was purged from the tree — that pinning now lives in git history.)
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="${QQ_BIN:-$ENGINE/qq}"
GOLD="$ENGINE/tests/fixtures/surface-freeze"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export QUINTESSENCE_DIR="$TMP/store" QQ_CONFIG="$TMP/config" QQ_STATE_DIR="$TMP/state" QQ_MEMDIR="$TMP/mem" QQ_KB_ROOT="$TMP/kb" QQ_CACHE="$TMP/cache.json"
mkdir -p "$QQ_MEMDIR" "$QQ_KB_ROOT/memory"; : > "$QQ_CONFIG"
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

# diff_golden <label> <golden-file> <actual-output-file>  — byte-exact after placeholder subst.
diff_golden() {
  local label="$1" golden="$2" actual="$3" expected
  expected="$(mktemp)"
  sed "s#<<NOW>>#$NOW#g; s#<<KBROOT>>#$QQ_KB_ROOT#g" "$golden" > "$expected"
  if diff -u "$expected" "$actual" > "$TMP/diff-$label.txt" 2>&1; then
    ok "$label matches frozen golden"
  else
    no "$label DIFFERS from frozen golden:"
    sed 's/^/    /' "$TMP/diff-$label.txt"
  fi
  rm -f "$expected"
}

"$QQ" init "$QUINTESSENCE_DIR" >/dev/null 2>&1
NOW="$(date -u +%FT%TZ)"

cat > "$QUINTESSENCE_DIR/alpha.md" <<EOF
# Quintessence — alpha
> updated: $NOW (created)
> essence: alpha essence for surface freeze

## RE-ENTER HERE
alpha re-enter body.

## Notes
alpha notes.
EOF
cat > "$QUINTESSENCE_DIR/beta.md" <<EOF
# Quintessence — beta
> updated: $NOW (created)
> essence: beta essence for surface freeze pairing

## RE-ENTER HERE
beta re-enter body.
EOF

# ---- 1. qq menu -------------------------------------------------------------------------
"$QQ" menu > "$TMP/menu.out" 2>&1
diff_golden "menu" "$GOLD/menu.golden" "$TMP/menu.out"

# ---- 2. qq digest, clean store (no findings, no pin, 2 HEADs <= max) --------------------
"$QQ" digest > "$TMP/digest-plain.out" 2>&1; ec=$?
diff_golden "digest-plain" "$GOLD/digest-plain.golden" "$TMP/digest-plain.out"
[ "$ec" -eq 0 ] && ok "digest-plain: exit 0 (P2 ratified fix — see header)" \
                || no "digest-plain: expected exit 0, got $ec"
# ---- 2b. RATIFIED DEVIATION: same small (<=max) store, but a non-empty activity log -------
# New engine: footer prints (activity log non-empty is the only condition), exit 0. The OLD
# engine's behavior (footer absent, exit 1 — see header) was asserted here directly against
# qq-legacy until the bash engine left the tree; its side of the deviation stays pinned by
# git history (the .legacy.golden fixtures went with it). The activity log is removed again
# afterward so later scenarios in this file (which assume no activity log until section 11)
# are unaffected.
# (the state dir used to exist here as a side effect of qq-legacy's config bootstrap, which
# mkdir'd it on every invocation; the python engine creates it lazily — own the fixture dir)
mkdir -p "$QQ_STATE_DIR"
printf '2026-07-01T00:00:00Z\talpha\n2026-07-01T01:00:00Z\tbeta\n' > "$QQ_STATE_DIR/activity.log"
"$QQ" digest > "$TMP/digest-small-store-footer.out" 2>&1; ec=$?
diff_golden "digest-small-store-footer" "$GOLD/digest-small-store-footer.golden" "$TMP/digest-small-store-footer.out"
[ "$ec" -eq 0 ] && ok "digest-small-store-footer: exit 0 (P2 ratified fix)" \
                || no "digest-small-store-footer: expected exit 0, got $ec"
rm -f "$QQ_STATE_DIR/activity.log"

# ---- 3. qq brief alpha --------------------------------------------------------------------
"$QQ" brief alpha > "$TMP/brief-alpha.out" 2>&1
diff_golden "brief-alpha" "$GOLD/brief-alpha.golden" "$TMP/brief-alpha.out"

# ---- 4. pending findings: TIER1 [T1 link] + XREF [T2 stale?] (betamem vs beta) -----------
cat > "$QQ_STATE_DIR/pending-findings.md" <<'EOF'
<!-- TIER1:START -->
- [T1 link] 2 unresolved [[link]](s) (may be intentional to-write markers): foo, bar
<!-- TIER1:END -->
<!-- XREF:START -->
- [T2 stale?] memory betamem (2026-01-01) vs HEAD beta (updated 2026-06-01T00:00:00Z, sim 0.71) – verify the memory's claims against the HEAD; edit or wave off (`qq waveoff betamem beta`).
<!-- XREF:END -->
<!-- AUDIT:START -->
<!-- AUDIT:END -->
<!-- SALIENCE:START -->
<!-- SALIENCE:END -->
EOF
cat > "$QQ_KB_ROOT/memory/betamem.md" <<'EOF'
---
name: betamem
description: a test memory fact
type: reference
---
betamem body content here.
EOF

# ---- 5. qq digest WITH findings preamble + QQ_DIGEST_PIN=alpha (MAP row first) -----------
QQ_DIGEST_PIN=alpha "$QQ" digest > "$TMP/digest-pin.out" 2>&1
diff_golden "digest-with-findings-and-pin" "$GOLD/digest-with-findings-and-pin.golden" "$TMP/digest-pin.out"

# ---- 6. qq findings (raw list) ------------------------------------------------------------
"$QQ" findings > "$TMP/findings-list.out" 2>&1
diff_golden "findings-list" "$GOLD/findings-list.golden" "$TMP/findings-list.out"

# ---- 7. qq findings next — topmost = generic-fallback branch (T1 link) ------------------
"$QQ" findings next > "$TMP/findings-next-generic.out" 2>&1
diff_golden "findings-next-generic" "$GOLD/findings-next-generic.golden" "$TMP/findings-next-generic.out"

# ---- 8. qq findings next — T2 stale? branch (memory body + brief_one of the HEAD) --------
cat > "$QQ_STATE_DIR/pending-findings.md" <<'EOF'
<!-- TIER1:START -->
<!-- TIER1:END -->
<!-- XREF:START -->
- [T2 stale?] memory betamem (2026-01-01) vs HEAD beta (updated 2026-06-01T00:00:00Z, sim 0.71) — verify the memory's claims against the HEAD; edit or wave off (`qq waveoff betamem beta`).
<!-- XREF:END -->
<!-- AUDIT:START -->
<!-- AUDIT:END -->
<!-- SALIENCE:START -->
<!-- SALIENCE:END -->
EOF
"$QQ" findings next > "$TMP/findings-next-t2stale.out" 2>&1
diff_golden "findings-next-t2stale" "$GOLD/findings-next-t2stale.golden" "$TMP/findings-next-t2stale.out"

# ---- 9. qq findings next — T1 size branch -------------------------------------------------
cat > "$QQ_STATE_DIR/pending-findings.md" <<'EOF'
<!-- TIER1:START -->
- [T1 size] HEAD alpha: update-lines 40kB / 20 lines → `qq compact alpha` (folds old update-lines to the journal, keeps newest ~5 + the body; `qq brief` reads it meanwhile)
<!-- TIER1:END -->
<!-- XREF:START -->
<!-- XREF:END -->
<!-- AUDIT:START -->
<!-- AUDIT:END -->
<!-- SALIENCE:START -->
<!-- SALIENCE:END -->
EOF
"$QQ" findings next > "$TMP/findings-next-t1size.out" 2>&1
diff_golden "findings-next-t1size" "$GOLD/findings-next-t1size.golden" "$TMP/findings-next-t1size.out"

# ---- 10. qq findings next — clean (no pending findings) ----------------------------------
cat > "$QQ_STATE_DIR/pending-findings.md" <<'EOF'
<!-- TIER1:START -->
<!-- TIER1:END -->
<!-- XREF:START -->
<!-- XREF:END -->
<!-- AUDIT:START -->
<!-- AUDIT:END -->
<!-- SALIENCE:START -->
<!-- SALIENCE:END -->
EOF
"$QQ" findings next > "$TMP/findings-next-clean.out" 2>&1
diff_golden "findings-next-clean" "$GOLD/findings-next-clean.golden" "$TMP/findings-next-clean.out"

# ---- 11. the >max-HEADs quirk: footer + "(+N older)" DO appear, exit 0 (see header note) -
for i in $(seq 1 12); do
cat > "$QUINTESSENCE_DIR/extra$i.md" <<EOF
# Quintessence — extra$i
> updated: $NOW (created)
> essence: extra $i essence

## RE-ENTER HERE
EOF
done
printf '2026-07-01T00:00:00Z\tbeta\n2026-07-01T01:00:00Z\talpha\n2026-07-01T02:00:00Z\tbeta\n' > "$QQ_STATE_DIR/activity.log"
"$QQ" digest > "$TMP/digest-over12.out" 2>&1; ec=$?
diff_golden "digest-over-max-quirk" "$GOLD/digest-over-max-quirk.golden" "$TMP/digest-over12.out"
[ "$ec" -eq 0 ] && ok "digest-over-max-quirk: exit 0 once total>max (footer/older-line reachable)" \
                || no "digest-over-max-quirk: expected exit 0, got $ec"

echo "----- $pass passed, $fail failed -----"
[ "$fail" -eq 0 ]
