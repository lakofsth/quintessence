#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-certify-path-key-shell-sites.sh — for every `path` or `csvpath` key in the KEYS registry, and for
# the XDG bases config.py expands into a path default, every shell site outside tests/ that reads
# the key names the path `Config.get` resolves for the same environment and config file, a leading
# `~` or `~/` expanded against HOME on both sides.
#
# The instrument is tests/review-instruments/certify-path-key-shell-sites.py; its docstring lists the
# directions walked and the ones not pinned. Keys, files and sites are all derived (KEYS, a walk of
# the tree, a scan for default-reading expansions); each site's script is run for real under an
# exact environment with a throwaway HOME (nothing inherited, so no override the invoking shell has
# exported reaches it), and stopped by a DEBUG trap just after its last top-level site.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
command -v python3 >/dev/null 2>&1 || { echo "test-certify-path-key-shell-sites.sh: python3 not found — skipping (treated as pass)"; exit 0; }
if python3 "$ENGINE/tests/review-instruments/certify-path-key-shell-sites.py" "$ENGINE"; then
  echo "PASS test-certify-path-key-shell-sites.sh"
  exit 0
fi
echo "FAIL test-certify-path-key-shell-sites.sh"
exit 1
