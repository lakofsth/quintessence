#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Retained by fix-review 20261105T123503Z (report: a private review record).
# Not a gate: tests/run.sh does not run it. Usage: bash tests/review-instruments/review-20261105-treefiles-nested-gitignore-probe.sh ENGINE
# Clones ENGINE (a git checkout) into a scratch dir, runs tests/py/test_treefiles.py there once
# clean (expect green), then runs the ASSURANCE.md gate command `python3 -m pytest` once (which
# writes .pytest_cache/ holding its own `.gitignore` of `*`) and runs test_treefiles again
# (red at 85c8eab: walk_files lists .pytest_cache/* because it reads only the top-level
# .gitignore, while git honours the nested one). Also shows the same with a `.venv/.gitignore`
# as `uv venv` writes. Prints one PASS/FAIL line per case. Leaves ENGINE untouched.
set -u
ENGINE="$(cd "${1:-.}" && pwd)"
S="$(mktemp -d)"; trap 'rm -rf "$S"' EXIT
run() { (cd "$1" && PYTHONPATH="$1:$1/tests/py" python3 -m unittest -q test_treefiles 2>&1 >/dev/null | tail -1) ; }
git clone -q "$ENGINE" "$S/c" && cd "$S/c" || exit 2
echo "clean checkout:            $(cd tests/py && run "$S/c")"
python3 -m pytest -q -p no:randomly tests/py/test_treefiles.py >/dev/null 2>&1
echo "after one pytest run:      $(cd tests/py && run "$S/c")   ($(ls -A .pytest_cache 2>/dev/null | tr '\n' ' '))"
rm -rf .pytest_cache; mkdir .venv; printf '*\n' > .venv/.gitignore; : > .venv/pyvenv.cfg
echo "with a .venv/.gitignore:   $(cd tests/py && run "$S/c")"
