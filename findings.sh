#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# findings.sh — the durable, deferrable consistency-findings queue.
# SOURCED (not executed) by: qq (check + digest), consistency-audit.sh.
#
# pending-findings.md holds marker-delimited sections, each a list of terse one-line findings:
#   <!-- TIER1:START --> … <!-- TIER1:END -->   owned by `qq check --write`        (deterministic shell)
#   <!-- XREF:START -->  … <!-- XREF:END -->    owned by `qq check --write` too    (T2 staleness-xref:
#                                               memory-vs-HEAD supersession via the qq-search embedding
#                                               index; see staleness-xref.py; wave-off = `qq waveoff`)
#   <!-- AUDIT:START --> … <!-- AUDIT:END -->    owned by consistency-audit.sh      (LLM audit)
#   <!-- SALIENCE:START --> … <!-- SALIENCE:END -->  owned by salience-audit.sh     (per-session
#                                               LLM capture-audit, limited-reader ideation proposal #2,
#                                               ratified 2026-07-02). UNLIKE the other sections,
#                                               each session's transcript is audited ONCE (its
#                                               content is fixed history) and its surviving
#                                               [SALIENCE] lines are APPENDED (via
#                                               findings_get_section + findings_set_section, not
#                                               a full-corpus re-derive) — so this section does
#                                               NOT auto-resolve like AUDIT/XREF; a captured item
#                                               is cleared by editing it out of the file by hand.
# Empty sections render nothing. Idempotent regeneration = AUTO-RESOLVE: when the drift is gone the
# next write empties that section, so a fixed finding simply disappears; an unfixed one re-presents.
# (SALIENCE is the one exception — see above.) Plumbing only persists; the MODEL decides WHEN to
# surface these (the digest renders them at SessionStart). See HEAD continuity-consistency.

QQ_STATE_DIR="${QQ_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/quintessence}"
FINDINGS_FILE="${FINDINGS_FILE:-$QQ_STATE_DIR/pending-findings.md}"

# ---- the state-dir lock (it only helps if EVERY writer takes the same one) -------------------
# python's persist_findings (quintessence/checks.py) already writes pending-findings.md under
# `quintessence.store.state_lock` — a fcntl.flock on $QQ_STATE_DIR/.state.lock, bounded by
# QQ_LOCK_WAIT seconds. Before this fix, findings_set_section (below) rewrote the SAME file with
# no lock at all, so a bash audit script (consistency-audit.sh, or a private audit extension) racing a
# python `qq check --write` could interleave with it and lose one side's section — the exact
# race this lock exists to stop. findings_set_section is the ONE choke point every bash writer goes through
# (verified by grep: only consistency-audit.sh calls it today, alongside any private audit extension;
# finalize-check.sh writes via `qq check --write` itself, i.e. already through python's lock; a
# future salience-audit.sh — referenced above, not yet shipped — is designed to go through this
# same function) — so locking it here covers every present and planned bash writer at once.
#
# QQ_LOCK_WAIT / STATE_LOCK mirror python's knob and lockfile path exactly (same env var, same
# default, same $QQ_STATE_DIR/.state.lock — this IS the interop: bash and python flock the same
# inode). Mirrors qq-lib.sh's own `qq_lock_acquire` bash/python interop pattern on `.qq.lock`
# (one flock-with-timeout mechanism, ported here as fd 8 rather than qq-lib.sh's fd 9
# so sourcing both files in one process never collides).
QQ_LOCK_WAIT="${QQ_LOCK_WAIT:-30}"
STATE_LOCK="$QQ_STATE_DIR/.state.lock"

# findings_with_state_lock — run "$@" with fd 8 flocked on STATE_LOCK, bounded by QQ_LOCK_WAIT
# seconds; the lock is released the instant the subshell running "$@" exits (crash-safe: a killed
# holder's fd closes and the kernel drops the flock — no stale-lock bookkeeping, exactly like
# python's acquire_flock). On timeout: a LOUD stderr message + non-zero return, never a silent or
# unbounded hang — a stuck lock must not wedge a nightly audit run forever (findings_set_section's
# caller already tolerates a non-zero return: `set -o pipefail` without `-e`, so the audit script
# continues rather than aborting; see consistency-audit.sh, or a private audit extension).
findings_with_state_lock() {
  mkdir -p "$(dirname "$STATE_LOCK")" 2>/dev/null || true
  (
    exec 8>"$STATE_LOCK" || { echo "qq: cannot open state lock $STATE_LOCK" >&2; exit 1; }
    flock -w "$QQ_LOCK_WAIT" 8 || {
      echo "qq: timed out after ${QQ_LOCK_WAIT}s waiting for the state lock ($STATE_LOCK) — skipping this findings write" >&2
      exit 1
    }
    "$@"
  )
}

findings_init() {
  [ -f "$FINDINGS_FILE" ] && return 0
  mkdir -p "$(dirname "$FINDINGS_FILE")"
  printf '%s\n' \
    '<!-- TIER1:START -->' '<!-- TIER1:END -->' \
    '<!-- XREF:START -->'  '<!-- XREF:END -->'  \
    '<!-- AUDIT:START -->' '<!-- AUDIT:END -->' \
    '<!-- SALIENCE:START -->' '<!-- SALIENCE:END -->' > "$FINDINGS_FILE"
}

# findings_set_section TAG   < content-on-stdin
# Replace the lines between <!-- TAG:START --> and <!-- TAG:END --> with stdin (markers preserved).
# A section missing from a pre-existing file (e.g. a section added in a later version) gets its markers appended
# first — otherwise the awk below would drop the content silently.
#
# The whole read-modify-write (findings_init + the section replace + the atomic `mv`) runs under
# `findings_with_state_lock` — the SAME dedicated lock python's persist_findings holds for the
# equivalent span — so a bash and a python writer can never interleave on this file (A1
# remainder). Capturing stdin into a temp file happens FIRST, outside the lock: it doesn't touch
# the shared file, and a slow/blocked upstream pipe must not hold the lock open. On a lock
# timeout, findings_with_state_lock already printed the loud stderr skip message; the section is
# simply left as it was (the caller's `set -o pipefail` without `-e` means the audit script
# continues rather than hanging or aborting — see consistency-audit.sh, or a private audit extension).
findings_set_section() {
  local tag="$1" body rc
  body="$(mktemp)"
  cat > "$body"
  findings_with_state_lock _findings_set_section_locked "$tag" "$body"
  rc=$?
  rm -f "$body"
  return $rc
}

# _findings_set_section_locked TAG BODYFILE — internal; the actual mutation, called ONLY from
# inside findings_with_state_lock's locked subshell (findings_set_section above).
_findings_set_section_locked() {
  local tag="$1" bf="$2" out
  findings_init
  grep -qF "<!-- $tag:START -->" "$FINDINGS_FILE" \
    || printf '%s\n' "<!-- $tag:START -->" "<!-- $tag:END -->" >> "$FINDINGS_FILE"
  out="$(mktemp)"
  awk -v s="<!-- $tag:START -->" -v e="<!-- $tag:END -->" -v bf="$bf" '
    $0==s { print; while ((getline line < bf) > 0) print line; close(bf); skip=1; next }
    $0==e { skip=0; print; next }
    skip  { next }
            { print }
  ' "$FINDINGS_FILE" > "$out"
  mv "$out" "$FINDINGS_FILE"
}

# findings_get_section TAG — print the lines between <!-- TAG:START --> / <!-- TAG:END -->
# (markers stripped). Empty output if the section/file doesn't exist yet. Read counterpart to
# findings_set_section, for a caller that needs to ACCUMULATE into a section (read current body,
# merge in new lines, write back the union) instead of replacing it outright — see SALIENCE above.
findings_get_section() {
  local tag="$1"
  [ -f "$FINDINGS_FILE" ] || return 0
  awk -v s="<!-- $tag:START -->" -v e="<!-- $tag:END -->" '
    $0==s { inside=1; next }
    $0==e { inside=0; next }
    inside { print }
  ' "$FINDINGS_FILE"
}

# findings_render — print every finding line (content between any START/END, markers + blanks stripped).
findings_render() {
  [ -f "$FINDINGS_FILE" ] || return 0
  awk '
    /<!-- .*:START -->/ { inside=1; next }
    /<!-- .*:END -->/   { inside=0; next }
    inside && NF        { print }
  ' "$FINDINGS_FILE"
}

# findings_count — number of finding lines across all sections.
findings_count() { findings_render | grep -c . || true; }
