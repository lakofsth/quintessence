#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-cli-args.sh — CLI usability guards (2026-07-05 review MEDIUMs): qq-search/qq-ask flag
# values error with a clean one-liner (never a traceback); `qq init` persists an explicit
# QQ_MEMDIR; setup.sh's wired hook commands keep their paths quoted.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="${QQ_BIN:-$ENGINE/qq}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export QUINTESSENCE_DIR="$TMP/store" QQ_STATE_DIR="$TMP/state" QQ_MEMDIR="$TMP/mem"
export QQ_CONFIG="$TMP/config"; : > "$QQ_CONFIG"
mkdir -p "$QQ_MEMDIR"
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

# ---- qq-search / qq-ask: bad flag values die cleanly, before touching any store ------------
for tool in qq-search qq-ask; do
  # missing value
  out="$("$ENGINE/$tool" -k 2>&1)"; rc=$?
  if [ "$rc" -eq 1 ] && ! printf '%s' "$out" | grep -q "Traceback" && printf '%s' "$out" | grep -q "requires a value"; then
    ok "$tool -k with no value: clean one-liner, exit 1"
  else no "$tool -k with no value (rc=$rc, out: $out)"; fi
  # non-numeric value
  out="$("$ENGINE/$tool" -k banana query 2>&1)"; rc=$?
  if [ "$rc" -eq 1 ] && ! printf '%s' "$out" | grep -q "Traceback" && printf '%s' "$out" | grep -q "expects an integer"; then
    ok "$tool -k banana: clean one-liner, exit 1"
  else no "$tool -k banana (rc=$rc, out: $out)"; fi
  # -s missing value
  out="$("$ENGINE/$tool" -s 2>&1)"; rc=$?
  if [ "$rc" -eq 1 ] && ! printf '%s' "$out" | grep -q "Traceback"; then
    ok "$tool -s with no value: clean, exit 1"
  else no "$tool -s with no value (rc=$rc, out: $out)"; fi
done

# ---- qq init: an explicit QQ_MEMDIR in the environment is persisted to the config file -----
"$QQ" init >/dev/null 2>&1
if grep -q "^QQ_MEMDIR=$TMP/mem$" "$QQ_CONFIG"; then
  ok "qq init persists explicit QQ_MEMDIR to the config file"
else no "qq init did not persist QQ_MEMDIR (config: $(cat "$QQ_CONFIG" 2>/dev/null))"; fi

# without QQ_MEMDIR in the env, init must NOT invent a QQ_MEMDIR entry
: > "$TMP/config2"
env -u QQ_MEMDIR QQ_CONFIG="$TMP/config2" QUINTESSENCE_DIR="$TMP/store2" QQ_STATE_DIR="$QQ_STATE_DIR" "$QQ" init >/dev/null 2>&1
if ! grep -q "^QQ_MEMDIR=" "$TMP/config2"; then
  ok "qq init without env QQ_MEMDIR writes no QQ_MEMDIR entry"
else no "qq init invented a QQ_MEMDIR entry: $(grep '^QQ_MEMDIR=' "$TMP/config2")"; fi

# ---- setup.sh: every wired hook command quotes its path (a space-containing install dir) ---
# hooks.json is the reference pattern (bash "…/hook.sh"); a regression reintroduces f'{here}/…
if ! grep -q "f'{here}" "$ENGINE/setup.sh"; then
  ok "setup.sh hook commands keep their paths quoted"
else no "setup.sh has an unquoted {here} hook command: $(grep -n "f'{here}" "$ENGINE/setup.sh")"; fi

echo "----- $pass passed, $fail failed -----"
[ "$fail" -eq 0 ]
