#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# run.sh — run every test-*.sh in this directory against an isolated throwaway store and report.
# Exit 0 only if all suites pass. Override the engine under test with QQ_BIN=/path/to/qq.
#
# ISOLATION IS THE SUITES' OWN JOB, and this line says what that is worth rather than promising it.
# Each suite pins its own QUINTESSENCE_DIR / QQ_CONFIG / QQ_MEMDIR into a throwaway directory, and
# the one suite that RUNS THE INSTALLER (test-setup-wire.sh) launches it under `env -i` with a
# closed allowlist (PATH, HOME and a scratch git identity), so no environment override an operator
# has exported can reach it. An earlier version of this comment stated the conclusion — "so this
# never touches a live install" — and it
# was FALSE: `QQ_CONFIG=/path/to/your/config bash tests/run.sh` reported all suites passed and
# rewrote that config to a directory the suite then deleted (eighteenth pass, F1). What holds it up
# now is checked, not asserted: tests/test-store-pollution.sh fails this gate if a suite runs the
# installer without both defences, and test-setup-wire.sh pins the isolation behaviourally against
# a decoy of every override setup.sh and qq-config.sh honour.
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

# Pin locale collation so the suite is reproducible regardless of the INVOKING shell/environment's
# own locale (P6 fresh-install verification finding): quintessence.store._locale_sort_key
# deliberately mirrors bash glob's own LC_COLLATE-aware order (so the python and bash engines
# agree with each other AND with legacy behaviour) — correct, but it means a few golden fixtures
# in test-surface-freeze.sh were captured under one specific collation (en_US.UTF-8) and a
# same-day multi-HEAD ordering can legitimately differ under a different one (e.g. a stripped
# C/POSIX-locale container, or `env -i` with no locale exported at all). This is a golden-fixture/
# harness portability gap, not a functional bug in `qq` (both orders are "correct" for their own
# locale) — pin the fixtures' own locale here so `tests/run.sh` is green independent of the caller.
if locale -a 2>/dev/null | grep -qi '^en_US\.utf8$'; then
  export LC_ALL=en_US.UTF-8
else
  echo "tests/run.sh: en_US.UTF-8 locale not installed on this host -- a collation-order golden"
  echo "  in test-surface-freeze.sh may legitimately not match (see quintessence/store.py's"
  echo "  _locale_sort_key docstring; not a qq functional bug)."
fi

# Scratch git author identity (tools-5/human-3): most suites `git commit` for real inside their
# own throwaway store, which fails on a fresh box/CI with no ~/.gitconfig — the suite should be
# hermetic to that, not cascade into a wall of unrelated-looking "ambiguous argument 'HEAD'"
# failures. Only fill in what's missing; a caller's own configured identity (env or gitconfig)
# always wins.
: "${GIT_AUTHOR_NAME:=qq-tests}"
: "${GIT_AUTHOR_EMAIL:=qq-tests@localhost}"
: "${GIT_COMMITTER_NAME:=qq-tests}"
: "${GIT_COMMITTER_EMAIL:=qq-tests@localhost}"
export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL

# Keep the embedding cache out of the invoking user's home (eighth review pass, F3). QQ_CACHE
# defaults to ~/.cache/qq-search/embeddings.json, so any suite that builds an index left .lock and
# .orphan-ages.json files there -- contradicting this file's own header, in a directory the suite
# does not own. Measured identically at the pre-atomicio base, so it is not a regression; it is
# just the last thing the gate still deposited in a real HOME. Same idiom as the git identity
# above: a caller's own QQ_CACHE wins, and the scratch directory goes away with this process.
QQ_CACHE_SCRATCH="$(mktemp -d)"
trap 'rm -rf "$QQ_CACHE_SCRATCH"' EXIT
: "${QQ_CACHE:=$QQ_CACHE_SCRATCH/embeddings.json}"
export QQ_CACHE

pass=0; fail=0; failed_suites=""
for t in "$HERE"/test-*.sh; do
  [ -e "$t" ] || continue
  name="$(basename "$t")"
  echo "=== $name ==="
  if bash "$t"; then pass=$((pass+1)); else fail=$((fail+1)); failed_suites="$failed_suites $name"; fi
  echo
done
echo "===================================================="
if [ "$fail" -eq 0 ]; then
  echo "ALL $pass suite(s) passed."
else
  echo "$pass suite(s) passed, $fail FAILED:$failed_suites"
fi
[ "$fail" -eq 0 ]
