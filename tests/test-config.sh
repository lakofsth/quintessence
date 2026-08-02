#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-config.sh — qq config get/set round-trip, arbitrary keys (the QQ_*_EXTRA seam relies on
# this), and env > file precedence. Isolated config file via QQ_CONFIG.
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

# set/get round-trip on an ARBITRARY key (the skill seams use custom keys like QQ_WRAP_REPOS)
"$QQ" config set QQ_WRAP_REPOS "/a:/b:/c" >/dev/null 2>&1
[ "$("$QQ" config get QQ_WRAP_REPOS)" = "/a:/b:/c" ] && ok "config set/get round-trips an arbitrary key" || no "arbitrary key round-trip (got '$("$QQ" config get QQ_WRAP_REPOS)')"

# set is idempotent-overwrite (no duplicate lines)
"$QQ" config set QQ_WRAP_REPOS "/x" >/dev/null 2>&1
[ "$(grep -c '^QQ_WRAP_REPOS=' "$QQ_CONFIG")" = "1" ] && ok "config set overwrites (no dup line)" || no "config set duplicated the key"

# get of an unset key is empty + non-fatal
[ -z "$("$QQ" config get QQ_NONEXISTENT 2>/dev/null)" ] && ok "config get of unset key is empty" || no "unset key not empty"

# env > file precedence (resolved value, via a probe that prints the resolved var)
"$QQ" config set QQ_KB_ROOT "/from/file" >/dev/null 2>&1
got="$(QQ_KB_ROOT=/from/env bash -c '. "'"$ENGINE"'/qq-config.sh"; printf %s "$QQ_KB_ROOT"')"
[ "$got" = "/from/env" ] && ok "env overrides file (precedence)" || no "precedence wrong (got '$got')"

# a config value with no surrounding quotes survives verbatim (loader parses, never sources)
got2="$(bash -c '. "'"$ENGINE"'/qq-config.sh"; printf %s "$QQ_WRAP_REPOS"')"
[ "$got2" = "/x" ] && ok "file value loads verbatim" || no "file value mangled (got '$got2')"

# QQ_SANDBOX refuses global-config mutation (a harness opts in; the write must not land)
before="$(cat "$QQ_CONFIG")"
QQ_SANDBOX=1 "$QQ" config set QQ_SHOULD_NOT_LAND "boom" >/dev/null 2>&1; rc=$?
[ "$rc" -eq 2 ] && ok "config set under QQ_SANDBOX exits 2" || no "sandbox set exit was $rc, want 2"
[ -z "$("$QQ" config get QQ_SHOULD_NOT_LAND)" ] && ok "config set under QQ_SANDBOX writes nothing" || no "sandbox set leaked a value"
[ "$(cat "$QQ_CONFIG")" = "$before" ] && ok "config file byte-unchanged under QQ_SANDBOX" || no "sandbox mutated the config file"
# without the guard the same set lands (proves the refusal is the guard, not a broken key)
"$QQ" config set QQ_SHOULD_NOT_LAND "ok-now" >/dev/null 2>&1
[ "$("$QQ" config get QQ_SHOULD_NOT_LAND)" = "ok-now" ] && ok "same set lands once QQ_SANDBOX is unset" || no "non-sandbox set failed"

# store-location keys are protected: a bare `config set QUINTESSENCE_DIR` is refused (can't
# silently repoint the store), but an ordinary key is unaffected, and QQ_ALLOW_RELOCATE authorizes.
"$QQ" config set QUINTESSENCE_DIR "/evil/store" >/dev/null 2>&1; rc=$?
[ "$rc" -eq 2 ] && ok "config set QUINTESSENCE_DIR refused without QQ_ALLOW_RELOCATE" || no "unprotected store relocate (rc=$rc)"
[ -z "$("$QQ" config get QUINTESSENCE_DIR)" ] && ok "refused relocate wrote nothing" || no "relocate leaked a value"
"$QQ" config set QQ_MEMDIR "/evil/mem" >/dev/null 2>&1
[ "$?" -eq 2 ] && ok "config set QQ_MEMDIR is protected too" || no "QQ_MEMDIR not protected"
QQ_ALLOW_RELOCATE=1 "$QQ" config set QUINTESSENCE_DIR "/deliberate/store" >/dev/null 2>&1
[ "$("$QQ" config get QUINTESSENCE_DIR)" = "/deliberate/store" ] && ok "QQ_ALLOW_RELOCATE authorizes a deliberate relocate" || no "authorized relocate did not land"
"$QQ" config set QQ_ORDINARY_KEY "fine" >/dev/null 2>&1
[ "$("$QQ" config get QQ_ORDINARY_KEY)" = "fine" ] && ok "ordinary keys are unaffected by the relocate gate" || no "relocate gate over-reached to an ordinary key"

echo "----- $pass passed, $fail failed -----"
[ "$fail" -eq 0 ]
