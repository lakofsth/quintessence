#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-mem-health.sh — the [T1 mem-health] consistency checks (qq check): MEMORY.md STRUCTURAL
# health, the clarity sibling of the integrity checks. Three deterministic signals: (a) index size
# approaching the harness silent-truncation cap, (b) per-entry one-line-hook char budget, (c)
# duplicate index entries. Budgets env-tunable (QQ_MEMINDEX_MAX_BYTES / QQ_MEMLINE_MAX_CHARS);
# QQ_MEM_HEALTH=0 disables the whole block.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="${QQ_BIN:-$ENGINE/qq}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export QUINTESSENCE_DIR="$TMP/store" QQ_CONFIG="$TMP/config" QQ_STATE_DIR="$TMP/state" QQ_MEMDIR="$TMP/mem" QQ_KB_ROOT="$TMP/kb" QQ_CACHE="$TMP/cache.json"
mkdir -p "$QQ_MEMDIR" "$QQ_KB_ROOT"; : > "$QQ_CONFIG"
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }
chk(){ "$QQ" check 2>/dev/null; }
MI="$QQ_MEMDIR/MEMORY.md"
mkmem(){ printf -- '---\nname: %s\ndescription: d\nmetadata:\n  type: reference\n---\nbody\n' "$1" > "$QQ_MEMDIR/$1.md"; }
long="$(head -c 300 < /dev/zero | tr '\0' x)"

"$QQ" init "$QUINTESSENCE_DIR" >/dev/null 2>&1
mkmem one; mkmem two

# --- SIZE: index >= cap flags; comfortably under clears ---
printf -- '# Memory Index\n\n- [one](one.md) — short hook\n- [two](two.md) — short hook\n' > "$MI"
QQ_MEMINDEX_MAX_BYTES=10     chk | grep -q '\[T1 mem-health\] MEMORY.md is .*approaching' && ok "size: flags at/over cap"  || no "size: missed over-cap"
QQ_MEMINDEX_MAX_BYTES=100000 chk | grep -q '\[T1 mem-health\] MEMORY.md is'               && no "size: false-positive under cap" || ok "size: clears under cap"

# --- LINE: an over-budget hook flags (summarized); short-only clears ---
printf -- '# Memory Index\n\n- [one](one.md) — %s\n- [two](two.md) — short\n' "$long" > "$MI"
QQ_MEMINDEX_MAX_BYTES=100000 chk | grep -q '\[T1 mem-health\] MEMORY.md index line(s) over' && ok "line: flags over-budget hook" || no "line: missed over-budget"
printf -- '# Memory Index\n\n- [one](one.md) — short\n- [two](two.md) — short\n' > "$MI"
QQ_MEMINDEX_MAX_BYTES=100000 chk | grep -q '\[T1 mem-health\] MEMORY.md index line(s) over' && no "line: false-positive on short lines" || ok "line: short lines clear"

# --- DUP: same memory file linked by >1 entry ---
printf -- '# Memory Index\n\n- [one](one.md) — a\n- [one, again](one.md) — b\n- [two](two.md) — c\n' > "$MI"
QQ_MEMINDEX_MAX_BYTES=100000 chk | grep -q '\[T1 mem-health\] MEMORY.md duplicate' && ok "dup: flags doubled entry" || no "dup: missed duplicate"
printf -- '# Memory Index\n\n- [one](one.md) — a\n- [two](two.md) — c\n' > "$MI"
QQ_MEMINDEX_MAX_BYTES=100000 chk | grep -q '\[T1 mem-health\] MEMORY.md duplicate' && no "dup: false-positive on distinct entries" || ok "dup: distinct entries clear"

# --- TOGGLE: QQ_MEM_HEALTH=0 suppresses even a real breach ---
printf -- '# Memory Index\n\n- [one](one.md) — %s\n' "$long" > "$MI"
QQ_MEM_HEALTH=0 QQ_MEMINDEX_MAX_BYTES=10 chk | grep -q '\[T1 mem-health\]' && no "toggle: QQ_MEM_HEALTH=0 did not suppress" || ok "toggle: QQ_MEM_HEALTH=0 suppresses"

echo "----"
[ "$fail" -eq 0 ] && echo "test-mem-health: all $pass passed" || echo "test-mem-health: $pass passed, $fail FAILED"
[ "$fail" -eq 0 ]
