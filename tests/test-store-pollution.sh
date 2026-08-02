#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-store-pollution.sh — regression for human-1-store-pollution: `bash setup.sh` (and any shell
# that has `export QUINTESSENCE_DIR=...` per README's manual install path) leaves QUINTESSENCE_DIR
# exported into every child process, including tests/run.sh's own suites. A suite that isolates via
# HOME=$TMP but never UNSETS an inherited QUINTESSENCE_DIR gets its walk-up discovery silently
# beaten — env wins over config by design — and writes its fixture HEADs into whatever store
# QUINTESSENCE_DIR points at instead of its own throwaway sandbox.
#
# This test reproduces exactly that invocation shape (QUINTESSENCE_DIR pre-exported before the
# suite runs, matching setup.sh's real environment) against a DECOY scratch store that is never
# Thomas's real store, and asserts the decoy stays completely empty. It targets the two suites the
# finding named (test-multi-store.sh, test-recall-composition.sh) directly, independent of
# setup.sh's own `env -u QUINTESSENCE_DIR` guard — so a regression in either suite's own defensive
# `unset` is caught even if setup.sh's invocation-side fix stays intact.
set -u
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
DECOY="$(mktemp -d)"; trap 'rm -rf "$DECOY"' EXIT

fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

for suite in test-multi-store.sh test-recall-composition.sh; do
  [ -e "$DECOY" ] || mkdir -p "$DECOY"
  before="$(find "$DECOY" -mindepth 1 2>/dev/null | wc -l)"
  # QUINTESSENCE_DIR pre-exported into the child's environment BEFORE it runs — exactly what
  # `bash setup.sh` (steps 1-4 export it) or README's manual `export QUINTESSENCE_DIR=...` path
  # leaves in the invoking shell when tests/run.sh is later run in it.
  QUINTESSENCE_DIR="$DECOY" bash "$HERE/$suite" >/dev/null 2>&1
  after="$(find "$DECOY" -mindepth 1 2>/dev/null | wc -l)"
  if [ "$after" -eq "$before" ]; then
    ok "$suite left the pre-exported decoy QUINTESSENCE_DIR untouched"
  else
    no "$suite wrote into the pre-exported decoy QUINTESSENCE_DIR ($before -> $after entries) -- walk-up isolation defeated by an inherited env var"
  fi
done

echo "----------------------------------------"
echo "$pass ok, $fail failed"
[ "$fail" -eq 0 ]
