# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Tests for tools/sync_assurance_counts.py — the tool an author runs so a test-adding commit
carries its own ASSURANCE.md count (tests/INVARIANTS.md,
`the-count-tool-writes-what-the-gate-reads-or-nothing`).

Each case builds a throwaway engine — an ASSURANCE.md stating both rows, a tests/py with known
tests, a tests/ with known test-*.sh — and drives the tool against it with --engine, so the real
ASSURANCE.md is never touched and the real collection is never the subject.
"""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ENGINE, "tools"))

import sync_assurance_counts as sync  # noqa: E402

def _git(cwd, *argv):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@invalid",
                    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *argv],
                   cwd=cwd, env=env, check=True, capture_output=True)


_DOC = ("| Python tests | `pytest` | {py} tests over the engine modules. |\n"
        "| Shell suites | `run.sh` | {sh} end-to-end suites against a store |\n")


@unittest.skipIf(importlib.util.find_spec("pytest") is None,
                 "pytest not installed — the tool counts pytest's collection")
class TestSyncAssuranceCounts(unittest.TestCase):
    def setUp(self):
        self.engine = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.engine)
        os.makedirs(os.path.join(self.engine, "tests", "py"))
        self._write("tests/py/test_a.py", "def test_one():\n    pass\n\n"
                                          "def test_two():\n    pass\n")
        self._write("tests/test-x.sh", "")
        self._write("ASSURANCE.md", _DOC.format(py=1, sh=7))

    def _write(self, rel, text):
        with open(os.path.join(self.engine, rel), "w") as f:
            f.write(text)

    def _doc(self):
        with open(os.path.join(self.engine, "ASSURANCE.md")) as f:
            return f.read()

    def _run(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = sync.main(["--engine", self.engine, *argv])
        return rc, err.getvalue()

    def test_it_writes_the_collected_figure_and_the_suite_glob(self):
        rc, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self._doc(), _DOC.format(py=2, sh=1))
        self.assertEqual(self._run("--check")[0], 0, "a second pass found the written figure stale")

    def test_check_reports_a_stale_figure_and_writes_nothing(self):
        rc, err = self._run("--check")
        self.assertEqual(rc, 1)
        self.assertIn(sync.COMMAND, err)
        self.assertEqual(self._doc(), _DOC.format(py=1, sh=7))

    def test_a_failed_collection_is_refused_not_counted(self):
        # pytest still prints "N tests collected, 1 error" here, so a tool that read the figure
        # without the exit code would write 2 and call it true.
        self._write("tests/py/test_broken.py", "def (\n")
        rc, err = self._run()
        self.assertEqual(rc, 2)
        self.assertIn("collection failed", err)
        self.assertIn("test_broken.py", err)
        self.assertEqual(self._doc(), _DOC.format(py=1, sh=7))

    def test_a_one_test_collection_is_read(self):
        # pytest says "1 test collected" — singular — and a plural-only pattern refused it as a
        # failed collection.
        self._write("tests/py/test_a.py", "def test_only():\n    pass\n")
        rc, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._doc(), _DOC.format(py=1, sh=1))

    def test_a_reported_error_is_refused_even_on_exit_zero(self):
        # COLLECT_ARGV makes pytest exit 2 on a collection error, so the exit code catches it
        # today; the summary's ", N error" is read too, so a flag that lets collection go on
        # past errors (exit 0) cannot turn a partial count into a figure.
        for summary in ("2 tests collected, 1 error in 0.1s", "1 test collected, 3 errors"):
            proc = subprocess.CompletedProcess([], 0, stdout=summary + "\n", stderr="")
            with self.assertRaises(sync.Refused, msg=summary) as cm:
                sync.read_collected(proc)
            self.assertIn("error", str(cm.exception))
        ok = subprocess.CompletedProcess([], 0, stdout="2 tests collected in 0.1s\n", stderr="")
        self.assertEqual(sync.read_collected(ok), 2)
        # And the exit code is read on its own: a non-zero pytest is no verdict, count or not.
        ok.returncode = 3
        with self.assertRaises(sync.Refused):
            sync.read_collected(ok)

    def test_a_test_id_naming_a_count_does_not_supply_the_figure(self):
        # `--collect-only -q` lists every node id before its summary, and pytest turns a string
        # parameter into the id — so the natural test of read_collected, parametrized over
        # sample summaries, prints `test_reads[7 tests collected]` above `3 tests collected`.
        self._write("tests/py/test_ids.py",
                    "import pytest\n\n\n@pytest.mark.parametrize('summary', ['7 tests collected'])\n"
                    "def test_reads(summary):\n    pass\n")
        rc, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._doc(), _DOC.format(py=3, sh=1),
                         "a test id, not pytest's summary, supplied the figure")

    def test_only_the_final_summary_line_is_read(self):
        # subTest, not parametrize: a parametrized sample would be the id this guards against.
        cases = (
            # (stdout, the count, or None for a refusal)
            ("tests/py/t.py::test_x[7 tests collected]\n\n2 tests collected in 0.10s\n", 2),
            # A line shaped like a summary ABOVE the summary (a plugin's print) is not it.
            ("7 tests collected in 0.00s\ntests/py/t.py::test_x\n\n2 tests collected in 0.10s\n",
             2),
            ("tests/py/t.py::test_x\n\n1 test collected in 61.20s (0:01:01)\n", 1),
            # A line that merely CONTAINS the shape is no summary, even when it is the last.
            ("tests/py/t.py::test_x[7 tests collected in 0.00s]\n", None),
            ("2 tests collected in 0.10s\ntests/py/t.py::test_after\n", None),
            ("no tests collected in 0.01s\n", None),
        )
        for stdout, want in cases:
            with self.subTest(stdout=stdout):
                proc = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
                if want is None:
                    with self.assertRaises(sync.Refused):
                        sync.read_collected(proc)
                else:
                    self.assertEqual(sync.read_collected(proc), want)

    def test_the_deselected_shape_is_refused_and_named(self):
        # `S/N tests collected (D deselected)` — a -k/-m or a deselecting conftest. The figure is
        # the whole collection, so neither S nor N is it; and `, 1 error` after the parenthesis
        # is still an error.
        for summary, named in (("1/3 tests collected (2 deselected) in 0.00s", "deselected"),
                               ("1/3 tests collected (2 deselected), 1 error in 0.09s", "error")):
            proc = subprocess.CompletedProcess([], 0, stdout=summary + "\n", stderr="")
            with self.assertRaises(sync.Refused, msg=summary) as cm:
                sync.read_collected(proc)
            self.assertIn(named, str(cm.exception))
        # And end to end: a conftest that deselects one of the two tests.
        self._write("tests/py/conftest.py",
                    "def pytest_collection_modifyitems(config, items):\n"
                    "    dropped = [i for i in items if i.name == 'test_two']\n"
                    "    config.hook.pytest_deselected(items=dropped)\n"
                    "    items[:] = [i for i in items if i not in dropped]\n")
        rc, err = self._run()
        self.assertEqual(rc, 2, err)
        self.assertIn("deselected", err)
        self.assertEqual(self._doc(), _DOC.format(py=1, sh=7))

    def test_check_judges_the_staged_assurance_md_and_names_an_unstaged_one(self):
        # --check reads what the gate reads: the index's ASSURANCE.md, beside the index's counts.
        _git(self.engine, "init", "-q")
        _git(self.engine, "add", "-A")
        _git(self.engine, "commit", "-q", "-m", "fixture")
        rc, err = self._run()
        self.assertEqual((rc, self._doc()), (0, _DOC.format(py=2, sh=1)), err)
        rc, err = self._run("--check")
        self.assertEqual(rc, 1, "--check passed a figure the index does not hold")
        self.assertIn("git add ASSURANCE.md", err)
        _git(self.engine, "add", "ASSURANCE.md")
        self.assertEqual(self._run("--check")[0], 0)

    def test_untracked_test_files_are_not_counted_and_are_named(self):
        self._write(".gitignore", "test_ignored.py\n")
        _git(self.engine, "init", "-q")
        _git(self.engine, "add", "-A")
        _git(self.engine, "commit", "-q", "-m", "fixture")
        # An ignored file is as absent from a clean checkout as an untracked one.
        self._write("tests/py/test_ignored.py", "def test_four():\n    pass\n")
        self._write("tests/py/test_untracked.py", "def test_three():\n    pass\n")
        self._write("tests/test-untracked.sh", "")
        rc, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._doc(), _DOC.format(py=2, sh=1),
                         "the tool counted a test file a clean checkout of the commit lacks")
        self.assertIn("test_untracked.py", err)
        self.assertIn("test-untracked.sh", err)
        # Once tracked, it counts.
        _git(self.engine, "add", "tests")
        self.assertEqual(self._run()[0], 0)
        self.assertEqual(self._doc(), _DOC.format(py=3, sh=2))

    def test_a_missing_or_doubled_row_is_refused(self):
        for doc in ("| Python tests | no count here |\n",
                    _DOC.format(py=1, sh=7) + "and 9 tests over the engine modules again\n"):
            self._write("ASSURANCE.md", doc)
            rc, err = self._run()
            self.assertEqual(rc, 2, doc)
            self.assertIn("expected exactly one", err)
            self.assertEqual(self._doc(), doc)


if __name__ == "__main__":
    unittest.main()
