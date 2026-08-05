#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-config-reconcile.sh — config-reconcile.py: live-config-vs-store drift. Asserts it flags a
# stale token in a HEAD essence + a memory fact, does NOT scan HEAD bodies (history), respects the
# live-revert guard, flags an unrecognized live value, and is a no-op without a registry.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
RECON="${QQ_RECON_BIN:-$ENGINE/config-reconcile.py}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# QQ_DOCDIR MUST be isolated to a throwaway dir — default is ~/docs, which a test must never scan.
# Kept EMPTY through the store/memory cases below so they are unaffected; populated for the doc block.
export QUINTESSENCE_DIR="$TMP/store" QQ_MEMDIR="$TMP/mem" QQ_STATE_DIR="$TMP/state" QQ_RECONCILE_SNAPSHOT="$TMP/snap.json" QQ_DOCDIR="$TMP/docs"
# An inherited QQ_CONFIG supplies the operator's own values for every key not pinned above, so
# pin an empty one — the same defence every other suite that reads config carries (eighteenth
# pass, F1's class sweep; this suite reads config but does not write it).
export QQ_CONFIG="$TMP/config"; : > "$QQ_CONFIG"
mkdir -p "$QUINTESSENCE_DIR" "$QQ_MEMDIR" "$QQ_STATE_DIR" "$QQ_DOCDIR"
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

# h1: essence names the stale token -> must flag.  h2: only the BODY names it -> must NOT flag.
printf '# h1\n> updated: 2020-01-01T00:00:00Z old note\n> essence: the gate runs on old-model-x today\n' > "$QUINTESSENCE_DIR/h1.md"
printf '# h2\n> updated: 2020-01-01T00:00:00Z historically it was old-model-x, since moved\n> essence: the gate runs on new-model-y\n' > "$QUINTESSENCE_DIR/h2.md"
printf -- '---\nname: m1\n---\nThe gate model is old-model-x.\n' > "$QQ_MEMDIR/m1.md"
printf 'GATE_MODEL=new-model-y\n' > "$TMP/live.env"
printf '[gate-model]\nsource = envfile %s/live.env GATE_MODEL\ncurrent = new-model-y\nstale = old-model-x\n' "$TMP" > "$TMP/reg.ini"

out="$(QQ_RECONCILE_REGISTRY="$TMP/reg.ini" "$RECON" 2>/dev/null)"
grep -q 'HEAD h1 (essence) asserts a stale gate-model' <<<"$out" && ok "flags a stale token in a HEAD essence" || no "missed stale essence"
grep -q 'memory m1.md asserts a stale gate-model'       <<<"$out" && ok "flags a stale token in a memory fact" || no "missed stale memory"
grep -q 'HEAD h2'                                        <<<"$out" && no "false-positive on a body-only (historical) mention" || ok "does NOT scan HEAD bodies (history excluded)"

# revert guard: live value IS the 'stale' token -> docs naming it are correct, no flag.
printf 'V=tok-old\n' > "$TMP/live2.env"
printf '[k2]\nsource = envfile %s/live2.env V\nstale = tok-old\n' "$TMP" > "$TMP/reg2.ini"
printf '# h3\n> essence: still on tok-old\n' > "$QUINTESSENCE_DIR/h3.md"
out2="$(QQ_RECONCILE_REGISTRY="$TMP/reg2.ini" "$RECON" 2>/dev/null)"
grep -q 'stale k2' <<<"$out2" && no "flagged a token the live value still carries" || ok "live-revert guard: no flag when live == the token"

# current-mismatch: live value lacks the registry 'current' token -> flag the unrecognized value.
printf 'V=surprise-value\n' > "$TMP/live3.env"
printf '[k3]\nsource = envfile %s/live3.env V\ncurrent = expected-value\nstale = whatever\n' "$TMP" > "$TMP/reg3.ini"
out3="$(QQ_RECONCILE_REGISTRY="$TMP/reg3.ini" "$RECON" 2>/dev/null)"
grep -q 'live k3 .* does NOT contain' <<<"$out3" && ok "flags a live value the registry doesn't recognize" || no "missed unrecognized live value"

# no registry -> silent no-op (generic install unaffected).
out4="$(QQ_RECONCILE_REGISTRY="" "$RECON" 2>/dev/null)"
[ -z "$out4" ] && ok "no-op without a registry" || no "produced output without a registry"

# CHANGE DETECTION (deploy-hook half): baseline a snapshot, then move the live value -> CHANGE
# finding + auto-flag the now-old value; read-only does not consume it; --commit-snapshot does.
rm -f "$QQ_RECONCILE_SNAPSHOT"
printf 'M=alpha\n' > "$TMP/cd.env"
printf '[k]\nsource = envfile %s/cd.env M\n' "$TMP" > "$TMP/cdreg.ini"
printf '# hc\n> essence: currently using alpha mode\n' > "$QUINTESSENCE_DIR/hc.md"
b="$(QQ_RECONCILE_REGISTRY="$TMP/cdreg.ini" "$RECON" --commit-snapshot 2>/dev/null)"
grep -q 'CHANGED' <<<"$b" && no "spurious CHANGE on the first baseline run" || ok "first run sets baseline, no CHANGE"
printf 'M=beta\n' > "$TMP/cd.env"
c="$(QQ_RECONCILE_REGISTRY="$TMP/cdreg.ini" "$RECON" 2>/dev/null)"
grep -q "k CHANGED since last check: 'alpha' -> 'beta'" <<<"$c" && ok "detects a knob change vs the snapshot" || no "missed the change"
grep -q "HEAD hc (essence) asserts a stale k – names 'alpha'" <<<"$c" && ok "auto-flags the now-old value (no manual stale upkeep)" || no "missed the auto-stale scan"
c2="$(QQ_RECONCILE_REGISTRY="$TMP/cdreg.ini" "$RECON" 2>/dev/null)"
grep -q 'CHANGED' <<<"$c2" && ok "read-only run does not consume the change" || no "change vanished without a commit"
QQ_RECONCILE_REGISTRY="$TMP/cdreg.ini" "$RECON" --commit-snapshot >/dev/null 2>&1
c3="$(QQ_RECONCILE_REGISTRY="$TMP/cdreg.ini" "$RECON" 2>/dev/null)"
grep -q 'CHANGED' <<<"$c3" && no "change still firing after --commit-snapshot" || ok "--commit-snapshot acknowledges the change"

# DOC SCANNING (improvement #1) + false-positive guards (improvement #2). Reuse the gate-model reg:
#   current = new-model-y, stale = old-model-x. Docs live in $QQ_DOCDIR (isolated tmp).
# tp     : present-tense claim of the stale value, no current token, no history heading -> MUST fire.
# fa     : guard (a) names-current-too -> SILENT (no history heading; isolates the current guard).
# fc     : guard (c) under a `## Purged` heading, no current token -> SILENT (isolates the strip).
# fr     : history section then a NON-history section that re-asserts the stale value -> MUST fire
#          (proves strip resumes at a same/shallower heading and doesn't swallow later prose).
# fd     : guard (d) inline `<!-- reconcile-ok: ... -->` marker -> SILENT.
printf '# LLM stack\nThe wake-gate is old-model-x only.\n'                                                  > "$QQ_DOCDIR/tp.md"
printf '# Stack\nThe gate runs new-model-y now; old-model-x is no longer used.\n'                            > "$QQ_DOCDIR/fa.md"
printf '# Models\nCurrent notes.\n\n## Purged\nold-model-x: dropped 2026-06-20.\n'                           > "$QQ_DOCDIR/fc.md"
printf '# Doc\n\n## History\nold-model-x was the original.\n\n## Current\nthe gate is old-model-x.\n'         > "$QQ_DOCDIR/fr.md"
printf '# Doc\nthe gate is old-model-x.  <!-- reconcile-ok: old-model-x -->\n'                               > "$QQ_DOCDIR/fd.md"
dout="$(QQ_RECONCILE_REGISTRY="$TMP/reg.ini" "$RECON" 2>/dev/null)"
grep -q 'doc tp.md asserts a stale gate-model'  <<<"$dout" && ok "scans docs + flags a stale present-tense claim" || no "missed stale doc claim"
grep -q 'doc fa.md'                             <<<"$dout" && no "(a) flagged a doc that also names the current value" || ok "(a) names-current-too guard clears a transition note"
grep -q 'doc fc.md'                             <<<"$dout" && no "(c) flagged a token under a ## Purged history heading"  || ok "(c) history-section strip clears a past-state mention"
grep -q 'doc fr.md asserts a stale gate-model'  <<<"$dout" && ok "(c) strip resumes — later non-history prose still scanned" || no "(c) over-stripped past the history section"
grep -q 'doc fd.md'                             <<<"$dout" && no "(d) inline reconcile-ok marker did not suppress" || ok "(d) inline reconcile-ok marker suppresses a token"

# guard (d) registry exempt-glob: a normally-firing doc skipped by `exempt = <glob>` (own docdir).
mkdir -p "$TMP/docs2"
printf '# Doc\nthe gate is old-model-x.\n' > "$TMP/docs2/skipme.md"
printf '# Doc\nthe gate is old-model-x.\n' > "$TMP/docs2/keepme.md"
printf '[gate-model]\nsource = envfile %s/live.env GATE_MODEL\ncurrent = new-model-y\nstale = old-model-x\nexempt = skipme.md\n' "$TMP" > "$TMP/reg_ex.ini"
eout="$(QQ_DOCDIR="$TMP/docs2" QQ_RECONCILE_REGISTRY="$TMP/reg_ex.ini" "$RECON" 2>/dev/null)"
grep -q 'doc keepme.md' <<<"$eout" && ok "exempt-glob: non-matching doc still flagged" || no "exempt-glob suppressed too much"
grep -q 'doc skipme.md' <<<"$eout" && no "exempt-glob did not skip the matching doc" || ok "(d) registry exempt-glob skips a matching file"

# docs disabled: QQ_DOCDIR="" -> docs not scanned at all (portable default-off path).
ddis="$(QQ_DOCDIR="" QQ_RECONCILE_REGISTRY="$TMP/reg.ini" "$RECON" 2>/dev/null)"
grep -q '\] doc ' <<<"$ddis" && no "scanned docs with QQ_DOCDIR empty" || ok "QQ_DOCDIR empty disables doc scanning"

echo "----"
[ "$fail" -eq 0 ] && echo "test-config-reconcile: all $pass passed" || echo "test-config-reconcile: $pass passed, $fail FAILED"
[ "$fail" -eq 0 ]
