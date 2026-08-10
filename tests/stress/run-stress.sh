#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
#
# Run every retained stress harness (tests/stress/stress-*.sh, enumerated — never
# hand-listed) with release-gate iteration counts, or a smoke pass with `quick`.
# Contention lane: idle box only; see tests/stress/README.md. A harness exits nonzero on
# any failed iteration, and this runner requires the enumeration to find work — an empty
# glob is instrument failure, not a pass.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
mode="${1:-full}"
case "$mode" in
  quick) declare -A iters=([default]=5)  ;;
  full)  declare -A iters=([default]=200);;
  *) echo "usage: run-stress.sh [quick|full]" >&2; exit 2;;
esac
ran=0 failed=0
for h in "$HERE"/stress-*.sh; do
  [ -e "$h" ] || continue
  ran=$((ran+1))
  echo "== $(basename "$h") (${iters[default]} iterations) =="
  if ! bash "$h" "${iters[default]}"; then
    failed=$((failed+1))
    echo "STRESS FAIL: $(basename "$h")"
  fi
done
if [ "$ran" -eq 0 ]; then echo "run-stress: found NO harnesses — enumeration broken"; exit 3; fi
echo "stress: $((ran-failed))/$ran harness(es) clean"
[ "$failed" -eq 0 ]
