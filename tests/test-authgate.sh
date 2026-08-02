#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-authgate.sh — AUTHORING GATE acceptance gates (write-side model trust, two-axis policy),
# driven end-to-end through the REAL `qq` dispatcher on throwaway fixture stores:
#   PARITY: with the gate at defaults (empty slug list), and with the gate configured but the
#           model TRUSTED, and for a NON-gated slug under an untrusted model, stdout/stderr/
#           committed store content are byte-identical to a gate-disabled run (QQ_BIND-style
#           golden-parity discipline).
#   DIVERT: an untrusted (or unknown) model's write to a gated slug exits 0 with a one-line
#           stderr notice, empty stdout, no HEAD change, no commit — and the verbatim text
#           lands as a [PROPOSED write] line in the pending-findings PROPOSED-WRITES section.
#   ESCAPE HATCH: QQ_AUTHOR_GATE=0 restores today's behavior entirely.
#   RATIFICATION (manual flow): a trusted session replays the JSON-decoded queued text through
#           the same verb and the content lands.
# Model identity is injected via QQ_MODEL_TRANSCRIPT pointing at fixture transcripts — the
# same last-known-model signal the deployed gate reads, without depending on the invoking
# session's own transcript (the explicit override always beats CLAUDE_CODE_SESSION_ID
# derivation, so this suite is immune to the ambient environment it runs in).
# NEVER touches the live store — mktemp fixtures, own QQ_CONFIG/QQ_STATE_DIR, same isolation
# convention as test-bind.sh.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="$ENGINE/qq"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

normalize(){ sed -E -e 's/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z/<TS>/g' \
                     -e 's/index [0-9a-f]{4,}\.\.[0-9a-f]{4,}/index <IDX>/g'; }

mkfixture(){   # mkfixture <dir>  — init an isolated store, set the env for it
  local QDIR="$1"
  export QUINTESSENCE_DIR="$QDIR" QQ_CONFIG="$QDIR.config" QQ_STATE_DIR="$QDIR.state" QQ_MEMDIR="$QDIR.mem"
  mkdir -p "$QQ_MEMDIR"; : > "$QQ_CONFIG"
  "$ENGINE/qq" init "$QDIR" >/dev/null 2>&1
}

# fixture transcripts: the gate reads the LAST message.model line
OPUS_TP="$TMP/opus.jsonl";   printf '{"message":{"model":"claude-opus-4-6"}}\n'  > "$OPUS_TP"
SONNET_TP="$TMP/sonnet.jsonl"; printf '{"message":{"model":"claude-sonnet-4-5"}}\n' > "$SONNET_TP"
FABLE_TP="$TMP/fable.jsonl"; printf '{"message":{"model":"acme-gated-5"}}\n'  > "$FABLE_TP"

GATE_ENV=(QQ_AUTHOR_GATE_SLUGS='sec-*' QQ_WRITE_TRUSTED_MODEL='acme-gated-5')

# ---- PARITY GATE 1: trusted model, gated slug — byte-identical to gate-at-defaults ----------------
SA="$TMP/a/store"; mkdir -p "$TMP/a"; mkfixture "$SA"
env "${GATE_ENV[@]}" QQ_MODEL_TRANSCRIPT="$OPUS_TP" "$QQ" new sec-topic "seed" >/dev/null 2>&1
env "${GATE_ENV[@]}" QQ_MODEL_TRANSCRIPT="$OPUS_TP" "$QQ" update sec-topic "a trusted line" >"$TMP/a.out" 2>"$TMP/a.err"
rc_a=$?

SB="$TMP/b/store"; mkdir -p "$TMP/b"; mkfixture "$SB"
"$QQ" new sec-topic "seed" >/dev/null 2>&1              # gate at defaults: empty slug list
"$QQ" update sec-topic "a trusted line" >"$TMP/b.out" 2>"$TMP/b.err"
rc_b=$?

[ "$rc_a" -eq 0 ] && [ "$rc_b" -eq 0 ] && ok "parity/trusted: both writes exit 0" || no "parity/trusted: exit codes ($rc_a/$rc_b)"
diff -q <(normalize <"$TMP/a.out") <(normalize <"$TMP/b.out") >/dev/null \
  && ok "parity/trusted: stdout byte-identical" || { no "parity/trusted: stdout differs"; diff <(normalize <"$TMP/a.out") <(normalize <"$TMP/b.out") | sed 's/^/       /'; }
[ ! -s "$TMP/a.err" ] && [ ! -s "$TMP/b.err" ] && ok "parity/trusted: stderr empty on both sides" || no "parity/trusted: stderr not empty"
diff -q <(normalize <"$SA/sec-topic.md") <(normalize <"$SB/sec-topic.md") >/dev/null \
  && ok "parity/trusted: committed HEAD content identical" || no "parity/trusted: HEAD content differs"

# ---- PARITY GATE 2: UNTRUSTED model, NON-gated slug — byte-identical too --------------------------
export QUINTESSENCE_DIR="$SA" QQ_CONFIG="$SA.config" QQ_STATE_DIR="$SA.state" QQ_MEMDIR="$SA.mem"
env "${GATE_ENV[@]}" QQ_MODEL_TRANSCRIPT="$SONNET_TP" "$QQ" new roadmap "plain topic" >/dev/null 2>&1
env "${GATE_ENV[@]}" QQ_MODEL_TRANSCRIPT="$SONNET_TP" "$QQ" update roadmap "an untrusted line on a plain topic" >"$TMP/c.out" 2>"$TMP/c.err"
rc_c=$?
export QUINTESSENCE_DIR="$SB" QQ_CONFIG="$SB.config" QQ_STATE_DIR="$SB.state" QQ_MEMDIR="$SB.mem"
"$QQ" new roadmap "plain topic" >/dev/null 2>&1
"$QQ" update roadmap "an untrusted line on a plain topic" >"$TMP/d.out" 2>"$TMP/d.err"
rc_d=$?
[ "$rc_c" -eq 0 ] && [ "$rc_d" -eq 0 ] \
  && diff -q <(normalize <"$TMP/c.out") <(normalize <"$TMP/d.out") >/dev/null \
  && [ ! -s "$TMP/c.err" ] && [ ! -s "$TMP/d.err" ] \
  && diff -q <(normalize <"$SA/roadmap.md") <(normalize <"$SB/roadmap.md") >/dev/null \
  && ok "parity/non-gated-slug: untrusted write byte-identical to gate-at-defaults" \
  || no "parity/non-gated-slug: divergence (rc $rc_c/$rc_d)"

# ---- DIVERT: untrusted model, gated slug ----------------------------------------------------------
export QUINTESSENCE_DIR="$SA" QQ_CONFIG="$SA.config" QQ_STATE_DIR="$SA.state" QQ_MEMDIR="$SA.mem"
head_before="$(cat "$SA/sec-topic.md")"
rev_before="$(git -C "$SA" rev-parse HEAD)"
env "${GATE_ENV[@]}" QQ_MODEL_TRANSCRIPT="$SONNET_TP" "$QQ" update sec-topic "an untrusted security claim" >"$TMP/e.out" 2>"$TMP/e.err"
rc=$?
[ "$rc" -eq 0 ] && ok "divert: exit 0 (caller must not read it as failure)" || no "divert: exit $rc"
[ ! -s "$TMP/e.out" ] && ok "divert: stdout empty" || { no "divert: stdout not empty"; sed 's/^/       /' "$TMP/e.out"; }
[ "$(wc -l <"$TMP/e.err")" -eq 1 ] && grep -q "AUTHORING GATE" "$TMP/e.err" && grep -q "claude-sonnet-4-5" "$TMP/e.err" \
  && ok "divert: one-line stderr notice naming the gate + model" || { no "divert: stderr notice wrong"; sed 's/^/       /' "$TMP/e.err"; }
[ "$(cat "$SA/sec-topic.md")" = "$head_before" ] && ok "divert: HEAD byte-unchanged" || no "divert: HEAD changed"
[ "$(git -C "$SA" rev-parse HEAD)" = "$rev_before" ] && ok "divert: no commit" || no "divert: a commit landed"
PF="$SA.state/pending-findings.md"
grep -q '<!-- PROPOSED-WRITES:START -->' "$PF" \
  && grep -q '^- \[PROPOSED write\] .* HEAD sec-topic via `qq update sec-topic` by model claude-sonnet-4-5' "$PF" \
  && ok "divert: PROPOSED write queued in its own section" || { no "divert: queue entry missing"; sed 's/^/       /' "$PF" 2>/dev/null; }
grep -qF '"> updated: ' "$PF" && grep -qF 'an untrusted security claim' "$PF" \
  && ok "divert: verbatim (stamped) proposed text recorded" || no "divert: verbatim text missing"

# unknown model (no resolvable transcript) fails safe to divert
env "${GATE_ENV[@]}" QQ_MODEL_TRANSCRIPT="$TMP/nonexistent.jsonl" "$QQ" update sec-topic "an unknown-model claim" >"$TMP/f.out" 2>"$TMP/f.err"
[ $? -eq 0 ] && [ ! -s "$TMP/f.out" ] && grep -q "model (unknown)" "$TMP/f.err" \
  && [ "$(git -C "$SA" rev-parse HEAD)" = "$rev_before" ] \
  && ok "divert: unknown/undetectable model fails safe to PROPOSED" || no "divert: unknown model not diverted"

# fable (the configured limited reader) is a trusted AUTHOR on the write axis
env "${GATE_ENV[@]}" QQ_MODEL_TRANSCRIPT="$FABLE_TP" "$QQ" update sec-topic "a fable-authored line" >"$TMP/g.out" 2>"$TMP/g.err"
[ $? -eq 0 ] && grep -q "committed + mirrored" "$TMP/g.out" && [ ! -s "$TMP/g.err" ] \
  && grep -q "a fable-authored line" "$SA/sec-topic.md" \
  && ok "trusted: QQ_WRITE_TRUSTED_MODEL match authors directly (write axis != read axis)" \
  || no "trusted: fable write did not land"

# rewrite + essence + new divert shapes
rev_before="$(git -C "$SA" rev-parse HEAD)"
printf '# Quintessence — sec-topic\n> updated: x\n> essence: clobbered\n\n## Notes\n' \
  | env "${GATE_ENV[@]}" QQ_MODEL_TRANSCRIPT="$SONNET_TP" "$QQ" rewrite sec-topic >"$TMP/h.out" 2>"$TMP/h.err"
[ $? -eq 0 ] && [ ! -s "$TMP/h.out" ] && grep -q "AUTHORING GATE" "$TMP/h.err" \
  && grep -q 'via `qq rewrite sec-topic`' "$PF" && ! grep -q "clobbered" "$SA/sec-topic.md" \
  && ok "divert: rewrite queues the whole proposed file, HEAD untouched" || no "divert: rewrite shape wrong"
env "${GATE_ENV[@]}" QQ_MODEL_TRANSCRIPT="$SONNET_TP" "$QQ" essence sec-topic "a proposed essence" >/dev/null 2>"$TMP/i.err"
[ $? -eq 0 ] && grep -q 'via `qq essence sec-topic`' "$PF" && ! grep -q "a proposed essence" "$SA/sec-topic.md" \
  && ok "divert: essence queues, HEAD untouched" || no "divert: essence shape wrong"
env "${GATE_ENV[@]}" QQ_MODEL_TRANSCRIPT="$SONNET_TP" "$QQ" new sec-fresh "a proposed new thread" >/dev/null 2>"$TMP/j.err"
[ $? -eq 0 ] && [ ! -f "$SA/sec-fresh.md" ] && grep -q 'via `qq new sec-fresh`' "$PF" \
  && ok "divert: new queues the essence arg, no HEAD created" || no "divert: new shape wrong"
[ "$(git -C "$SA" rev-parse HEAD)" = "$rev_before" ] && ok "divert: still no commits after all shapes" || no "divert: a commit leaked"

# gated-slug REFUSALS stay refusals (never converted to a queued 'success') —
# the under-lock gate re-check also sees the target absent and does not divert
env "${GATE_ENV[@]}" QQ_MODEL_TRANSCRIPT="$SONNET_TP" "$QQ" update sec-missing "text" >/dev/null 2>"$TMP/k.err"
rc=$?
[ "$rc" -ne 0 ] && grep -q "needs an existing" "$TMP/k.err" && ! grep -q "sec-missing" "$PF" \
  && ok "refusal-parity: update to a missing gated HEAD still errors after under-lock re-check, nothing queued" \
  || no "refusal-parity: missing-HEAD update mishandled (rc=$rc)"

# ---- ESCAPE HATCH: QQ_AUTHOR_GATE=0 ---------------------------------------------------------------
env QQ_AUTHOR_GATE=0 "${GATE_ENV[@]}" QQ_MODEL_TRANSCRIPT="$SONNET_TP" "$QQ" update sec-topic "gate disabled line" >"$TMP/l.out" 2>"$TMP/l.err"
[ $? -eq 0 ] && grep -q "committed + mirrored" "$TMP/l.out" && [ ! -s "$TMP/l.err" ] \
  && grep -q "gate disabled line" "$SA/sec-topic.md" \
  && ok "escape hatch: QQ_AUTHOR_GATE=0 restores direct authoring" || no "escape hatch: gate still active"

# ---- RATIFICATION (the documented manual flow) -----------------------------------------------------
# a trusted session lists the queue, JSON-decodes the text, replays it through the same verb,
# then deletes the line from pending-findings (state dir — direct edit is sanctioned there).
line="$(grep -m1 'an untrusted security claim' "$PF")"
text="$(printf '%s' "$line" | sed 's/.*; text: //' | python3 -c 'import json,sys; sys.stdout.write(json.loads(sys.stdin.read()))')"
printf '%s' "$text" | env "${GATE_ENV[@]}" QQ_MODEL_TRANSCRIPT="$OPUS_TP" "$QQ" update sec-topic >"$TMP/m.out" 2>"$TMP/m.err"
[ $? -eq 0 ] && grep -q "an untrusted security claim" "$SA/sec-topic.md" \
  && ok "ratify: trusted replay of the JSON-decoded text lands verbatim" || no "ratify: replay failed"
grep -vF -- "$line" "$PF" > "$PF.tmp" && mv "$PF.tmp" "$PF"
"$QQ" findings > "$TMP/n.out" 2>/dev/null
if grep -qF -- "$line" "$TMP/n.out"; then no "ratify: entry still listed after deletion"
else ok "ratify: deleting the line clears it from qq findings"; fi

# ---- summary --------------------------------------------------------------------------------------
echo "----- $pass passed, $fail failed -----"
[ "$fail" -eq 0 ]
