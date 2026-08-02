#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-multi-store.sh — multi-store behaviour end-to-end against ISOLATED throwaway stores:
#   * `qq init --project` creates a project store WITHOUT touching the global config;
#   * writes (new/update/finalize) from inside a project route to the project store;
#   * a file-recorded QUINTESSENCE_DIR does NOT pin (project discovery still runs);
#   * reads compose (fetch a user HEAD by name from a project; list unions; --global scopes back);
#   * from the user home (no project store) everything stays on the user store, unchanged.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="${QQ_BIN:-$ENGINE/qq}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# HOME isolation so the user store + walk-up boundary live under TMP (NOT Thomas's real ~). Do
# NOT export QUINTESSENCE_DIR — that would pin and disable discovery, which is the whole point.
export HOME="$TMP"
# Defensive: an inherited QUINTESSENCE_DIR/QQ_MEMDIR (e.g. from setup.sh's own environment, or a
# shell that did `export QUINTESSENCE_DIR=...` per README's manual install path) would pin and
# beat this test's HOME-based discovery — env always wins over config by design (human-1). Strip
# both before anything below relies on walk-up discovery landing under TMP.
unset QUINTESSENCE_DIR QQ_MEMDIR
export QQ_CONFIG="$TMP/config" QQ_STATE_DIR="$TMP/state" QQ_MEMDIR="$TMP/mem"
mkdir -p "$QQ_MEMDIR"; : > "$QQ_CONFIG"
# git identity via env (real ~/.gitconfig is out of reach under the overridden HOME).
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

USER_STORE="$TMP/quintessence"
PROJ="$TMP/work/repo"
PSTORE="$PROJ/.quintessence"
mkdir -p "$PROJ/src"

fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

# --- user store (bare init records QUINTESSENCE_DIR in the config FILE) ---
"$QQ" init "$USER_STORE" >/dev/null 2>&1
[ -d "$USER_STORE/.git" ] && ok "bare init creates the user store" || no "bare init failed"
grep -q "^QUINTESSENCE_DIR=$USER_STORE\$" "$QQ_CONFIG" \
  && ok "bare init records QUINTESSENCE_DIR in the config file" || no "config file missing QUINTESSENCE_DIR"

# --- project store: qq init --project (from inside the project) ---
( cd "$PROJ" && "$QQ" init --project >/dev/null 2>&1 )
[ -d "$PSTORE/.git" ] && ok "init --project creates \$PWD/.quintessence" || no "init --project did not create the project store"
# global-config ISOLATION: the project path must NOT have been recorded anywhere in the config.
grep -q "$PSTORE" "$QQ_CONFIG" && no "init --project polluted the global config" \
  || ok "init --project left the global config untouched"
grep -q "^QUINTESSENCE_DIR=$USER_STORE\$" "$QQ_CONFIG" \
  && ok "global QUINTESSENCE_DIR still points at the user store" || no "global QUINTESSENCE_DIR changed"

# --- writes route to the project store (file-recorded QUINTESSENCE_DIR did NOT pin) ---
( cd "$PROJ/src" && "$QQ" new projtopic "a project head" >/dev/null 2>&1 )
{ [ -f "$PSTORE/projtopic.md" ] && ! [ -f "$USER_STORE/projtopic.md" ]; } \
  && ok "new from a project subdir writes to the PROJECT store, not the user store" \
  || no "new routed to the wrong store"
( cd "$PROJ/src" && "$QQ" update projtopic "an update line" >/dev/null 2>&1 )
grep -q "an update line" "$PSTORE/projtopic.md" && ok "update lands in the project store" || no "update did not land in the project store"
( cd "$PROJ/src" && "$QQ" finalize projtopic >/dev/null 2>&1 )
ls "$PSTORE"/journal/projtopic/*.md >/dev/null 2>&1 && ok "finalize journals in the project store" || no "finalize did not journal in the project store"
# the project store's git actually recorded the commits (write-path hooks passed).
( cd "$PSTORE" && git log --format=%s 2>/dev/null | grep -q "projtopic" ) \
  && ok "project store git recorded the write-path commits" || no "project store has no commits"

# --- a user-store HEAD, then compose reads from inside the project ---
"$QQ" new usertopic "a user head" >/dev/null 2>&1      # cwd = $HOME (no project) -> user store
[ -f "$USER_STORE/usertopic.md" ] && ok "new from \$HOME writes to the user store" || no "user-store write failed"
( cd "$PROJ/src" && "$QQ" path usertopic ) | grep -q "^$USER_STORE/usertopic.md\$" \
  && ok "path resolves a USER HEAD by name from inside the project (composite fetch)" || no "composite fetch failed"
( cd "$PROJ/src" && "$QQ" path projtopic ) | grep -q "^$PSTORE/projtopic.md\$" \
  && ok "path resolves the PROJECT HEAD via walk-up" || no "project fetch failed"

# --- list unions from the project; --global scopes back to the user store ---
union="$( cd "$PROJ/src" && "$QQ" list | sort | tr '\n' ' ' )"
{ echo "$union" | grep -q projtopic && echo "$union" | grep -q usertopic; } \
  && ok "list from the project unions project + user HEADs" || no "list did not union ($union)"
glob="$( cd "$PROJ/src" && "$QQ" list --global | sort | tr '\n' ' ' )"
{ echo "$glob" | grep -q usertopic && ! echo "$glob" | grep -q projtopic; } \
  && ok "list --global scopes to the user store only" || no "--global did not scope to the user store ($glob)"

# --- P5: check's link resolver spans the search path (cross-store link not false-flagged) ---
( cd "$PROJ/src" && "$QQ" update projtopic "cross ref [[usertopic]] plus [[deadlink-xyz]]" >/dev/null 2>&1 )
chk="$( cd "$PROJ/src" && "$QQ" check --fast 2>&1 )"
echo "$chk" | grep -q "deadlink-xyz" \
  && ok "check --fast flags a genuinely missing link" || no "check missed a dead link"
echo "$chk" | grep -q "usertopic" \
  && no "check false-flagged a cross-store link (usertopic) as unresolved" \
  || ok "check resolves a project->user link via the composite (no false unresolved)"

# --- P7: qq memdir surfaces the write-target memory dir (agent-facing project-fact location) ---
( cd "$PROJ/src" && "$QQ" memdir ) | grep -q "^$PSTORE/memory\$" \
  && ok "memdir prints the project store's memory dir" || no "memdir wrong from project"
( cd "$PROJ/src" && "$QQ" memdir --global ) | grep -q "^$QQ_MEMDIR\$" \
  && ok "memdir --global prints the user memory dir" || no "memdir --global wrong"

# --- the --global strip honours the `--` literal-text contract (review fix 2026-07-03) ---
# `--global`/`-g` after `--` is TEXT (the write verbs' documented escape hatch), not the flag.
( cd "$PROJ/src" && "$QQ" update projtopic -- keep --global and -g literal >/dev/null 2>&1 )
grep -q -- "keep --global and -g literal" "$PSTORE/projtopic.md" \
  && ok "update -- keeps a literal --global/-g in the recorded text" \
  || no "--global/-g swallowed from literal text after --"
# ...while the flag BEFORE `--` still scopes a WRITE to the user store from inside a project.
( cd "$PROJ/src" && "$QQ" update usertopic -g "routed-to-user-via-g" >/dev/null 2>&1 )
grep -q "routed-to-user-via-g" "$USER_STORE/usertopic.md" \
  && ok "update -g from a project writes to the USER store (write-verb --global)" \
  || no "update -g did not route to the user store"

# --- cross-store write guard: write verbs must not silently miss/fork a user HEAD ---
# usertopic exists ONLY in the user store here. update/rewrite from the project must refuse
# with the --global hint (rewrite would otherwise silently CREATE a shadowing project fork).
guard_err="$( cd "$PROJ/src" && "$QQ" update usertopic "meant for the user store" 2>&1 >/dev/null )"
rc=$?
{ [ $rc -ne 0 ] && echo "$guard_err" | grep -q -- "--global"; } \
  && ok "update of a user-only HEAD from a project refuses with the --global hint" \
  || no "cross-store update guard missing (rc=$rc: $guard_err)"
( cd "$PROJ/src" && echo "fork content" | "$QQ" rewrite usertopic >/dev/null 2>&1 )
[ -f "$PSTORE/usertopic.md" ] && no "rewrite silently forked a user HEAD into the project store" \
  || ok "rewrite of a user-only HEAD refuses (no silent shadow fork)"

# --- deliberate shadow via qq new is allowed, noted, and cleanly removable ---
shadow_note="$( cd "$PROJ/src" && "$QQ" new usertopic "project-local shadow" 2>&1 >/dev/null )"
{ [ -f "$PSTORE/usertopic.md" ] && echo "$shadow_note" | grep -qi "shadow"; } \
  && ok "qq new may shadow a user HEAD, and says so on stderr" \
  || no "shadow-new did not create or did not warn ($shadow_note)"
( cd "$PROJ/src" && "$QQ" path usertopic ) | grep -q "^$PSTORE/usertopic.md\$" \
  && ok "shadow is live: path now resolves to the project copy" || no "shadow not visible"

# --- store-scoped LEGACY verbs (delete/rm/compact/reindex) follow the same routing ---
# `qq delete` of the shadowed slug from inside the project must delete the PROJECT copy and
# leave the user HEAD untouched (pre-fix it deleted the USER copy — destructive, wrong store).
( cd "$PROJ/src" && "$QQ" delete usertopic >/dev/null 2>&1 )
{ ! [ -f "$PSTORE/usertopic.md" ] && [ -f "$USER_STORE/usertopic.md" ]; } \
  && ok "delete from a project removes the PROJECT copy; the user HEAD survives" \
  || no "delete routed to the wrong store (project: $([ -f "$PSTORE/usertopic.md" ] && echo alive || echo gone), user: $([ -f "$USER_STORE/usertopic.md" ] && echo alive || echo GONE))"
( cd "$PROJ/src" && "$QQ" path usertopic ) | grep -q "^$USER_STORE/usertopic.md\$" \
  && ok "after shadow removal, path falls back to the user HEAD" || no "fallback broken"
( cd "$PROJ/src" && "$QQ" delete usertopic >/dev/null 2>&1 )
[ -f "$USER_STORE/usertopic.md" ] \
  && ok "delete of a user-only slug from a project does NOT touch the user store" \
  || no "delete reached across and removed the user HEAD"
( cd "$PROJ/src" && "$QQ" reindex >/dev/null 2>&1 )
[ -f "$PSTORE/INDEX.md" ] && ok "reindex from a project rebuilds the PROJECT index" \
  || no "reindex did not build the project INDEX.md"

# --- init --project <dir>: a custom dir is the PROJECT ROOT; the store must be discoverable ---
# (walk-up finds only a dir literally named .quintessence, so the store goes at <dir>/.quintessence)
PROJ2="$TMP/work/repo2"; mkdir -p "$PROJ2"
"$QQ" init --project "$PROJ2" >/dev/null 2>&1
[ -d "$PROJ2/.quintessence/.git" ] \
  && ok "init --project <dir> creates <dir>/.quintessence" \
  || no "init --project <dir> put the store at <dir> itself (undiscoverable)"
( cd "$PROJ2" && "$QQ" new p2topic "second project" >/dev/null 2>&1 )
[ -f "$PROJ2/.quintessence/p2topic.md" ] \
  && ok "a write from <dir> lands in the discoverable <dir>/.quintessence store" \
  || no "write from <dir> missed the project store (walk-up cannot see it)"
# an explicit path already ending in /.quintessence passes through unchanged.
PROJ3="$TMP/work/repo3"; mkdir -p "$PROJ3"
"$QQ" init --project "$PROJ3/.quintessence" >/dev/null 2>&1
{ [ -d "$PROJ3/.quintessence/.git" ] && ! [ -d "$PROJ3/.quintessence/.quintessence" ]; } \
  && ok "init --project <dir>/.quintessence is used as-is (no double nesting)" \
  || no "explicit .quintessence path mangled"

# --- the home level is the USER layer: init --project refuses at $HOME; a stray store is inert ---
( cd "$HOME" && "$QQ" init --project >/dev/null 2>&1 ) && no "init --project at \$HOME did not refuse" \
  || ok "init --project at \$HOME refuses (home level = user store)"
[ -d "$HOME/.quintessence" ] && no "refused init still created \$HOME/.quintessence" \
  || ok "refused init created nothing"
# a stray $HOME/.quintessence (rsync/unzip/another tool) must be INERT, not shadow every
# $HOME-rooted invocation — the degenerate checks below run WITH it present.
mkdir -p "$HOME/.quintessence"
( cd "$HOME" && "$QQ" new straycheck "still the user store" >/dev/null 2>&1 )
{ [ -f "$USER_STORE/straycheck.md" ] && ! [ -f "$HOME/.quintessence/straycheck.md" ]; } \
  && ok "a stray \$HOME/.quintessence is inert (writes from \$HOME stay on the user store)" \
  || no "stray \$HOME/.quintessence hijacked the store"

# --- degenerate: from $HOME (no project store) nothing composes ---
home_list="$( cd "$HOME" && "$QQ" list | sort | tr '\n' ' ' )"
{ echo "$home_list" | grep -q usertopic && ! echo "$home_list" | grep -q projtopic; } \
  && ok "from \$HOME the project store is invisible (degenerate = user store only)" \
  || no "degenerate view leaked the project store ($home_list)"

echo "----------------------------------------------------"
[ "$fail" -eq 0 ] && echo "test-multi-store: all $pass checks passed." || echo "test-multi-store: $pass passed, $fail FAILED."
[ "$fail" -eq 0 ]
