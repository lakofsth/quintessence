# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# qq-lib.sh — shared write-path plumbing for quintessence. SOURCED, not executed.
#
# The single concurrency primitive. Multiple Claude sessions share ONE working tree /
# index / branch / mirror set with no coordination, which races: a session committing
# another's mid-edit, lost HEAD updates, push races (git's index.lock stops corruption,
# not these). This funnels every shared-state mutation through ONE kernel flock so the
# discrete write-ops serialize (B waits a beat); long-lived sessions still share freely.
# flock auto-releases when the fd closes (process exit) => crash-safe, no stale-lock /
# heartbeat / session-scope bookkeeping.
#
# Two invariants make it correct:
#   1. PATH-SCOPED commits — we stage and commit ONLY the named paths (never `-A`/bare
#      `git commit`), so a concurrent session's dirty/staged scratch is never swept into
#      our commit. This is the direct fix for the observed "committed another's mid-edit".
#   2. IN-LOCK MARKER — we export QQ_WRITE_TXN before touching git; the pre-commit/pre-push
#      hooks reject anything without it, so nothing reaches shared/mirror state except
#      under the lock (a raw-Bash bypass write just stays local-scratch until a proper
#      write reconciles it).

# load the per-install config file (env > file > default) before applying any default below
. "$(dirname "${BASH_SOURCE[0]}")/qq-config.sh"

: "${QUINTESSENCE_DIR:=$HOME/quintessence}"
QDIR="$QUINTESSENCE_DIR"
QLOCK="$QDIR/.qq.lock"
QQ_LOCK_WAIT="${QQ_LOCK_WAIT:-30}"   # seconds to block for the lock before giving up

# Compaction thresholds — single source of truth, shared by qq-write's SOFT nudge (on every
# write) and qq check's HARD flag (the consistency sweep). The BYTES gates size the UPDATE-LINE
# REGION only (what `qq compact` can actually fold) — NOT the whole file, because a large curated
# body is not bloat and compaction can't shrink it (see ul_region_bytes + QQ_BODY_HARD_BYTES).
# Two tiers on purpose: nudge early (advisory), flag later (it's actually a problem now).
QQ_COMPACT_SOFT_BYTES="${QQ_COMPACT_SOFT_BYTES:-32768}"; QQ_COMPACT_SOFT_LINES="${QQ_COMPACT_SOFT_LINES:-12}"
QQ_COMPACT_HARD_BYTES="${QQ_COMPACT_HARD_BYTES:-49152}"; QQ_COMPACT_HARD_LINES="${QQ_COMPACT_HARD_LINES:-15}"
# Body ceiling — a large body (the ## sections below `> essence:`) blows a cold read just as a
# bloated update-log does, but `qq compact` won't touch it; flag it separately, with a different
# remedy (condense the sections / `qq brief`). Used by qq check only.
QQ_BODY_HARD_BYTES="${QQ_BODY_HARD_BYTES:-49152}"

# ul_region_bytes <file> — bytes of the update-line region: the `> updated:` block INCLUDING its
# multi-line continuation paragraphs, up to (not incl.) `> essence:`. Exactly what `qq compact`
# folds, so the compaction nudges size THIS, not the whole file.
ul_region_bytes() {
  awk '/^> essence:/{f=0} /^> updated:/{f=1} f{b+=length($0)+1} END{print b+0}' "${1:?ul_region_bytes: file}"
}

# Operational-contract version. CONTRACT.md (the small text injected into a session to teach the
# write-path) carries a matching "qq-contract vN" marker. Bump BOTH when the write-path verbs or
# rules change; `qq doctor` compares them so a drifted/stale injected contract gets flagged
# (confidently-wrong instructions are worse than none).
QQ_CONTRACT_VERSION=2

# Runtime state (activity log, findings queue, prederive cache) — kept OUT of the engine/repo
# dir (and out of an ephemeral plugin dir) so it survives updates and never pollutes a tracked
# tree. Override with QQ_STATE_DIR.
QQ_STATE_DIR="${QQ_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/quintessence}"
mkdir -p "$QQ_STATE_DIR" 2>/dev/null || true

# qq_lock_acquire — open fd 9 on the lock and block (bounded). The fd stays open for the
# life of the process, so the lock is held until exit (then auto-released by the kernel).
# Sets the in-lock marker the git hooks require.
qq_lock_acquire() {
  exec 9>"$QLOCK" || { echo "qq: cannot open lock $QLOCK" >&2; return 1; }
  flock -w "$QQ_LOCK_WAIT" 9 || { echo "qq: timed out after ${QQ_LOCK_WAIT}s waiting for the qq lock (another session is writing)" >&2; return 1; }
  export QQ_WRITE_TXN="$$"
}

# Mirror/remote pushes do NOT happen here. If you mirror this repo to a remote, do it from a
# single post-commit hook and let that be the SOLE pusher: pushing again from here would launch
# two concurrent background pushes that can race a remote's manifest read-modify-write (a real
# failure mode with encrypted remotes). Note a post-commit hook runs after the commit returns,
# outside this lock — so the qq lock is NOT held across a slow off-site push (acceptable: the
# commit is already durable locally).

# qq_commit_push "<msg>" <path>...  — stage + commit ONLY the named paths (pathspec commit
# ignores any other staged/dirty files); the post-commit hook mirrors the commit. Idempotent:
# a no-op when the named paths have no changes. MUST be called with the lock held
# (qq_lock_acquire). Paths are relative to $QDIR.
qq_commit_push() {
  local msg="$1"; shift
  [ -n "$(git -C "$QDIR" status --porcelain -- "$@" 2>/dev/null)" ] || { echo "qq: no changes to commit for $*"; return 0; }
  git -C "$QDIR" add -- "$@"
  # pathspec commit: commits the working-tree content of THESE paths only, leaving any
  # unrelated staged changes (a concurrent session's) untouched.
  git -C "$QDIR" commit --quiet -m "$msg" -- "$@"
}
