#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-check-fast.sh — `qq check --fast` (and --no-xref / QQ_XREF_DISABLE) skips the slow embedding
# staleness-xref while still emitting/persisting the fast Tier-1 section. Decoupling: fast findings
# never wait on the xref. (The isolated env has no embedder, so this checks the FLAG plumbing +
# the xref-skipped messaging, not embed timing.)
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="${QQ_BIN:-$ENGINE/qq}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export QUINTESSENCE_DIR="$TMP/store" QQ_CONFIG="$TMP/config" QQ_STATE_DIR="$TMP/state" QQ_MEMDIR="$TMP/mem" QQ_KB_ROOT="$TMP/kb" QQ_CACHE="$TMP/cache.json"
mkdir -p "$QQ_MEMDIR" "$QQ_KB_ROOT"; : > "$QQ_CONFIG"
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }
MI="$QQ_MEMDIR/MEMORY.md"
mkmem(){ printf -- '---\nname: %s\ndescription: d\nmetadata:\n  type: reference\n---\nbody\n' "$1" > "$QQ_MEMDIR/$1.md"; }

"$QQ" init "$QUINTESSENCE_DIR" >/dev/null 2>&1
"$QQ" reindex >/dev/null 2>&1   # clear the fresh-store INDEX-stale T1 finding so a clean store is truly clean
mkmem one

# clean store + --fast: must report consistent AND note the xref was skipped
printf -- '# Memory Index\n\n- [one](one.md) — short\n' > "$MI"
out="$("$QQ" check --fast 2>/dev/null)"
printf '%s' "$out" | grep -q 'xref skipped' && ok "--fast notes the xref was skipped on a clean store" || no "--fast did not note xref-skipped"

# --fast still emits the fast Tier-1 section (a real mem-health breach surfaces)
long="$(head -c 300 < /dev/zero | tr '\0' x)"
printf -- '# Memory Index\n\n- [one](one.md) — %s\n' "$long" > "$MI"
"$QQ" check --fast 2>/dev/null | grep -q '\[T1 mem-health\]' && ok "--fast still emits fast Tier-1 findings" || no "--fast suppressed Tier-1"

# --no-xref is an accepted alias
"$QQ" check --no-xref 2>/dev/null | grep -q '\[T1 mem-health\]' && ok "--no-xref alias works" || no "--no-xref alias broke"

# QQ_XREF_DISABLE=1 env path also skips (clean store -> xref-skipped message)
printf -- '# Memory Index\n\n- [one](one.md) — short\n' > "$MI"
QQ_XREF_DISABLE=1 "$QQ" check 2>/dev/null | grep -q 'xref skipped' && ok "QQ_XREF_DISABLE=1 skips the xref" || no "QQ_XREF_DISABLE=1 did not skip"

# --write --fast: writes the TIER1 section + summary, xref skipped
"$QQ" check --write --fast >/dev/null 2>&1 && ok "--write --fast exits cleanly" || no "--write --fast errored"

echo "----"
[ "$fail" -eq 0 ] && echo "test-check-fast: all $pass passed" || echo "test-check-fast: $pass passed, $fail FAILED"
[ "$fail" -eq 0 ]
