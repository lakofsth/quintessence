#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-py.sh — wires the python unit/property test suite (tests/py/test_*.py) into the
# same tests/run.sh harness as the bash suites. Covers quintessence/{config,store,heads,
# memory,slugs,findings}.py: KEYS registry sanity, the config-file parity test against a
# REAL qq-config.sh subprocess, Head/MemoryFact lossless round-trip against the live store
# (read-only), SlugResolver against every real [[link]] token, the state_lock primitive, and
# the Finding renderers against the P0 surface-freeze goldens.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
command -v python3 >/dev/null 2>&1 || { echo "test-py.sh: python3 not found — skipping (treated as pass)"; exit 0; }
cd "$ENGINE" || exit 1
PYTHONPATH="$ENGINE${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest discover -s tests/py -p 'test_*.py' -v
