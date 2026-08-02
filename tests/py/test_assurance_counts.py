# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Parity test for the counts ASSURANCE.md quotes about its own gates.

ASSURANCE.md's premise is that a reader need not take the author's word for anything — every
gate it lists is one you can run yourself. A hand-maintained number in that document is the one
claim a reader CANNOT check without re-deriving it, and it drifts silently the moment a test is
added (it was 722 against 727 collected when this was written; 2026-07-29 verification round,
F6). So the numbers are pinned the same way CONFIG.md is pinned to the config registry: the doc
is the surface, the code is the source of truth, and the suite fails on a mismatch.

Counting is done the way a reader would: `pytest --collect-only` for the python suite (a
subprocess, so this file counts itself too — collection never executes anything, so there is no
recursion), and a glob for the shell suites, which is exactly what tests/run.sh iterates.
"""
import glob
import os
import re
import subprocess
import sys
import unittest

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ASSURANCE_MD = os.path.join(ENGINE, "ASSURANCE.md")


def _documented(pattern: str) -> int:
    with open(ASSURANCE_MD) as f:
        text = f.read()
    m = re.search(pattern, text)
    if m is None:
        raise AssertionError(f"ASSURANCE.md no longer states a count matching {pattern!r} — if "
                             f"the wording changed, update this test with it")
    return int(m.group(1))


class TestAssuranceCountsMatchReality(unittest.TestCase):
    def test_python_test_count_matches_collection(self):
        documented = _documented(r"(\d+) tests over the engine modules")
        out = subprocess.run([sys.executable, "-m", "pytest", os.path.join("tests", "py"),
                              "--collect-only", "-q", "-p", "no:cacheprovider"],
                             cwd=ENGINE, capture_output=True, text=True).stdout
        m = re.search(r"(\d+) tests collected", out)
        self.assertIsNotNone(m, f"could not read a collected-test count from pytest:\n{out[-2000:]}")
        actual = int(m.group(1))
        self.assertEqual(documented, actual,
                         f"ASSURANCE.md says {documented} python tests; pytest collects "
                         f"{actual}. Update the number in ASSURANCE.md's 'Python tests' row.")

    def test_shell_suite_count_matches_the_suite_directory(self):
        documented = _documented(r"(\d+) end-to-end suites")
        actual = len(glob.glob(os.path.join(ENGINE, "tests", "test-*.sh")))
        self.assertEqual(documented, actual,
                         f"ASSURANCE.md says {documented} shell suites; tests/ holds {actual} "
                         f"test-*.sh files (what tests/run.sh iterates). Update the number in "
                         f"ASSURANCE.md's 'Shell suites' row.")


if __name__ == "__main__":
    unittest.main()
