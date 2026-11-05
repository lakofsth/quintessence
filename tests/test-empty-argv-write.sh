#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-empty-argv-write.sh — certify INVARIANTS.md: an-empty-argv-text-is-refused.
#
# A write verb that reads argv (`update`, `new`, `essence`) refuses, with a non-zero exit and
# nothing written, argv text that is GIVEN but is empty or whitespace-only once joined.
# Directions pinned per verb: an empty argument, a whitespace-only argument (spaces, tabs,
# newlines), several such arguments joined (the join is a space, so "" + "" is whitespace-only),
# and the same through the `--` literal-text separator (after `--` the argument IS the text).
# Refusal means: non-zero exit AND the store untouched — HEAD bytes unchanged for update/essence,
# no HEAD created at all for new, and no new commit in either case.
# The sentence's contrast half ("GIVEN") is pinned too: with no argv text the verbs keep their
# not-given behavior — `update` reads stdin, `new` scaffolds the placeholder essence — and text
# that carries a single non-whitespace character (padded, multi-word) still lands.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="${QQ_BIN:-$ENGINE/qq}"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export QUINTESSENCE_DIR="$TMP/store"
export QQ_CONFIG="$TMP/config"           # isolate the GLOBAL config (tests must, or they mutate a live install)
export QQ_STATE_DIR="$TMP/state"
export QQ_MEMDIR="$TMP/mem"; mkdir -p "$QQ_MEMDIR"
: > "$QQ_CONFIG"

fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

"$QQ" init "$QUINTESSENCE_DIR" >/dev/null 2>&1 || { echo "init failed"; exit 1; }
"$QQ" new T "seed essence" >/dev/null 2>&1 || { echo "seed qq new failed"; exit 1; }

headsnap(){ md5sum "$QUINTESSENCE_DIR/T.md" | cut -d' ' -f1; }
gitsnap(){ git -C "$QUINTESSENCE_DIR" rev-parse HEAD 2>/dev/null; }

# refuse_update <case> [text args...]: `qq update T <args>` must exit non-zero, change no byte
refuse_update(){ local name="$1"; shift; local b g out rc a ga
  b="$(headsnap)"; g="$(gitsnap)"
  out="$("$QQ" update T "$@" </dev/null 2>&1)"; rc=$?
  a="$(headsnap)"; ga="$(gitsnap)"
  if [ "$rc" -ne 0 ] && [ "$b" = "$a" ] && [ "$g" = "$ga" ]; then ok "$name"
  else no "$name (rc=$rc, HEAD-changed=$([ "$b" = "$a" ] && echo no || echo YES), git-changed=$([ "$g" = "$ga" ] && echo no || echo YES): ${out:0:120})"; fi
}
# refuse_essence <case> [text args...]: same for `qq essence T <args>`
refuse_essence(){ local name="$1"; shift; local b g out rc a ga
  b="$(headsnap)"; g="$(gitsnap)"
  out="$("$QQ" essence T "$@" </dev/null 2>&1)"; rc=$?
  a="$(headsnap)"; ga="$(gitsnap)"
  if [ "$rc" -ne 0 ] && [ "$b" = "$a" ] && [ "$g" = "$ga" ]; then ok "$name"
  else no "$name (rc=$rc, HEAD-changed=$([ "$b" = "$a" ] && echo no || echo YES), git-changed=$([ "$g" = "$ga" ] && echo no || echo YES): ${out:0:120})"; fi
}
# refuse_new <case> [text args...]: `qq new <topic> <args>` must exit non-zero and create no HEAD.
# Each case gets its OWN topic — otherwise a later case would be refused for "already exists"
# (a refusal for a reason other than the blank text this check plants, R2.9).
newseq=0
refuse_new(){ local name="$1"; shift; local g out rc topic
  topic="refuse-me-$((++newseq))"
  g="$(gitsnap)"
  out="$("$QQ" new "$topic" "$@" </dev/null 2>&1)"; rc=$?
  if [ "$rc" -ne 0 ] && [ ! -f "$QUINTESSENCE_DIR/$topic.md" ] && [ "$g" = "$(gitsnap)" ]; then ok "$name"
  else no "$name (rc=$rc, HEAD-created=$([ -f "$QUINTESSENCE_DIR/$topic.md" ] && echo YES || echo no), git-changed=$([ "$g" = "$(gitsnap)" ] && echo no || echo YES): ${out:0:120})"; fi
}

# ---- update: given-but-blank argv text is refused, nothing written ---------------------------
refuse_update 'update <t> "" refused'            ""
refuse_update 'update <t> "   " refused'         "   "
refuse_update 'update <t> tab/newline-only refused' $'\n\t '
refuse_update 'update <t> "" "" refused'         "" ""
refuse_update 'update <t> -- "" refused'         -- ""

# ---- essence: given-but-blank argv text is refused, nothing written --------------------------
refuse_essence 'essence <t> "" refused'          ""
refuse_essence 'essence <t> space/tab-only refused' $' \t '
refuse_essence 'essence <t> newline-only refused' $'\n'
refuse_essence 'essence <t> "" " " refused'      "" " "
refuse_essence 'essence <t> -- "" refused'       -- ""

# ---- new: given-but-blank argv text is refused, no HEAD created ------------------------------
refuse_new 'new <t> "" refused'                  ""
refuse_new 'new <t> "   " refused'               "   "
refuse_new 'new <t> tab/newline-only refused'    $'\t\n'
refuse_new 'new <t> "" "" refused'               "" ""
refuse_new 'new <t> -- "" refused'               -- ""

# ---- the contrast half: NOT given, and given-with-content, keep working ----------------------
# update with no argv text reads stdin (the sentence binds only text that IS given)
printf 'stdin-not-given-argv\n' | "$QQ" update T >/dev/null 2>&1; rc=$?
if [ "$rc" -eq 0 ] && grep -qF 'stdin-not-given-argv' "$QUINTESSENCE_DIR/T.md"; then
  ok "update <t> with no argv text still reads stdin"
else no "update <t> stdin path broken (rc=$rc)"; fi

# update with real argv text lands (single word, padded word, multi-word join)
"$QQ" update T "real-argv-line" >/dev/null 2>&1; rc=$?
if [ "$rc" -eq 0 ] && grep -qF 'real-argv-line' "$QUINTESSENCE_DIR/T.md"; then
  ok "update <t> \"text\" still lands"
else no "update <t> \"text\" broke (rc=$rc)"; fi
"$QQ" update T "  padded-word  " >/dev/null 2>&1; rc=$?
if [ "$rc" -eq 0 ] && grep -qF 'padded-word' "$QUINTESSENCE_DIR/T.md"; then
  ok "update <t> padded real word still lands (not whitespace-only once stripped)"
else no "update <t> padded real word refused (rc=$rc)"; fi
"$QQ" update T "two" "words" >/dev/null 2>&1; rc=$?
if [ "$rc" -eq 0 ] && grep -qF 'two words' "$QUINTESSENCE_DIR/T.md"; then
  ok "update <t> two real words join and land"
else no "update <t> two real words broke (rc=$rc)"; fi

# new with no argv text scaffolds the placeholder essence (the not-given behavior, unchanged)
"$QQ" new n-place </dev/null >/dev/null 2>&1; rc=$?
if [ "$rc" -eq 0 ] && [ -f "$QUINTESSENCE_DIR/n-place.md" ] && grep -qF '<one-line essence' "$QUINTESSENCE_DIR/n-place.md"; then
  ok "new <t> with no argv text still scaffolds the placeholder"
else no "new <t> no-argv-text path broken (rc=$rc)"; fi

# new / essence with real text land
"$QQ" new n-real "real essence text" >/dev/null 2>&1; rc=$?
if [ "$rc" -eq 0 ] && grep -qF '> essence: real essence text' "$QUINTESSENCE_DIR/n-real.md"; then
  ok "new <t> \"essence\" still lands"
else no "new <t> \"essence\" broke (rc=$rc)"; fi
"$QQ" essence T "refreshed essence" >/dev/null 2>&1; rc=$?
if [ "$rc" -eq 0 ] && grep -qF '> essence: refreshed essence' "$QUINTESSENCE_DIR/T.md"; then
  ok "essence <t> \"text\" still lands"
else no "essence <t> \"text\" broke (rc=$rc)"; fi

echo "----- $pass passed, $fail failed -----"
[ "$fail" -eq 0 ]
