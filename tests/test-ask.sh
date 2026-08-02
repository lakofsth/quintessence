#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-ask.sh — qq ask / quintessence.ask regression, NO live-model dependency:
#   (a) degraded path: a REAL keyword-fallback hit (embedder down), no completion endpoint
#       healthy -> the degraded marker, exit 0.
#   (b) redaction unit: QQ_REDACT_FILE drops a matching hit, keeps others (qqask_core.filter_redacted).
#   (c) QQ_ASK_UNREDACTED=1 bypasses redaction even with a matching slug file.
# Isolated store/config/cache/embedder per the tests/ convention (see test-degraded.sh) so this
# never touches a live install, never needs a real embedder, and never needs a real completion
# endpoint. Exit 0 = all pass.
#
# LEGACY BUG surfaced by the P3 port's parity testing (2026-07-03, harmless, fixed here): (a)
# used to point QQ_KB_ROOT directly AT the store dir. quintessence.search's (and qqsearch_core's
# own) chunks() only descends into SUBDIRECTORIES of the kb root (one per corpus source) — a
# file sitting at the kb root's own top level, like $QUINTESSENCE_DIR/ask-probe.md, was never
# actually walked, so this test's "xyzzy-sentinel" HEAD was NEVER part of the corpus on EITHER
# engine; grep_fallback() always saw zero chunks; (a) passed only because "no chunks + no
# endpoint" and "endpoint down" both used to print the SAME degraded marker on the OLD engine.
# The P3 port's new A7 evidence-pack floor (quintessence.ask) distinguishes those two cases —
# "genuinely nothing retrieved" now returns a more precise refusal — which is what exposed the
# always-empty corpus here. Fixed below by giving QQ_KB_ROOT a real subdirectory (a symlink to
# the store, mirroring the real kb/qq convention) so the keyword-fallback hit this test's name
# and comment always intended to exercise is now genuinely present.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="${QQ_BIN:-$ENGINE/qq}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export QUINTESSENCE_DIR="$TMP/store" QQ_CONFIG="$TMP/config" QQ_STATE_DIR="$TMP/state" QQ_MEMDIR="$TMP/mem"
export QQ_KB_ROOT="$TMP/kb" QQ_CACHE="$TMP/cache.json"
export QQ_OLLAMA_URL="http://127.0.0.1:1"     # dead port -> embedder unreachable, keyword fallback
export QQ_ASK_ENDPOINTS="http://127.0.0.1:1"  # dead port -> no completion endpoint healthy
mkdir -p "$QQ_MEMDIR" "$TMP/kb"; : > "$QQ_CONFIG"
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

"$QQ" init "$QUINTESSENCE_DIR" >/dev/null 2>&1
"$QQ" new ask-probe "a HEAD mentioning xyzzy-sentinel for qq ask retrieval" >/dev/null 2>&1
ln -s "$QUINTESSENCE_DIR" "$TMP/kb/qq"   # a real corpus source subdir -> ask-probe.md is now findable

# --- (a) degraded path: a real keyword-fallback hit, no completion endpoint healthy ---------
out="$("$QQ" ask "xyzzy-sentinel" 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ok "qq ask exits 0 with no completion endpoint healthy (rc=$rc)" || no "qq ask exited non-zero when degraded (rc=$rc)"
printf '%s' "$out" | grep -qi 'QA unavailable' && ok "qq ask prints the degraded marker" || no "qq ask did not print the degraded marker: '$out'"

# --- (b) redaction unit: quintessence.ask filter_redacted drops a matching hit, keeps others ------
REDACT_FILE="$TMP/redact-slugs"
printf 'secret-topic\n' > "$REDACT_FILE"
redb="$(QQ_REDACT_FILE="$REDACT_FILE" python3 -c "
import sys; sys.path.insert(0, '$ENGINE')
from quintessence.ask import Ask
from quintessence.config import Config
filter_redacted = Ask(Config(), None).filter_redacted
hits = [
    {'path': 'quintessence/secret-topic.md', 'title': 'secret'},
    {'path': 'quintessence/public-topic.md', 'title': 'public'},
]
out = filter_redacted(hits)
print(','.join(sorted(h['path'] for h in out)))
")"
[ "$redb" = "quintessence/public-topic.md" ] && ok "filter_redacted drops the matching hit, keeps the other" || no "filter_redacted wrong result: '$redb'"

# --- (c) QQ_ASK_UNREDACTED=1 bypasses redaction ---------------------------------------------
redc="$(QQ_REDACT_FILE="$REDACT_FILE" QQ_ASK_UNREDACTED=1 python3 -c "
import sys; sys.path.insert(0, '$ENGINE')
from quintessence.ask import Ask
from quintessence.config import Config
filter_redacted = Ask(Config(), None).filter_redacted
hits = [
    {'path': 'quintessence/secret-topic.md', 'title': 'secret'},
    {'path': 'quintessence/public-topic.md', 'title': 'public'},
]
out = filter_redacted(hits)
print(','.join(sorted(h['path'] for h in out)))
")"
[ "$redc" = "quintessence/public-topic.md,quintessence/secret-topic.md" ] && ok "QQ_ASK_UNREDACTED=1 bypasses redaction" || no "unredacted bypass wrong result: '$redc'"

echo "----- $pass passed, $fail failed -----"
[ "$fail" -eq 0 ]
