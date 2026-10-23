# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Pins for tools/sync_assurance_counts.py (tests/INVARIANTS.md
`the-count-tool-writes-what-the-gate-reads-or-nothing`): the temporary index checkout is removed
when the tool is ended by SIGTERM or SIGHUP, pytest runs without the caller's PYTEST_ADDOPTS, and
the refusals and notices rounds 3 to 5 found unpinned each have a check.

Every fixture is a real git repository with real pytest output.
"""
import glob
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

_ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(_ENGINE, "tools", "sync_assurance_counts.py")
sys.path.insert(0, os.path.join(_ENGINE, "tools"))

import sync_assurance_counts as sync  # noqa: E402

_DOC = ("| Python tests | `pytest` | {py} tests over the engine modules. |\n"
        "| Shell suites | `run.sh` | {sh} end-to-end suites against a store |\n")


def _git(cwd, *argv):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@invalid",
                    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *argv],
                   cwd=cwd, env=env, check=True, capture_output=True)


@unittest.skipIf(importlib.util.find_spec("pytest") is None, "pytest not installed")
class TestCountToolPins(unittest.TestCase):
    def setUp(self):
        self.engine = tempfile.mkdtemp(prefix="pins-engine-")
        self.addCleanup(shutil.rmtree, self.engine, True)
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

    def _commit(self):
        _git(self.engine, "init", "-q")
        _git(self.engine, "add", "-A")
        _git(self.engine, "commit", "-q", "-m", "fixture")

    def _tool(self, *argv, env=None):
        full = dict(os.environ)
        full.update(env or {})
        return subprocess.run([sys.executable, TOOL, "--engine", self.engine, *argv],
                              capture_output=True, text=True, env=full)

    # -- signals ---------------------------------------------------------------------------------

    def _signalled_run_leaves(self, sig):
        scratch = tempfile.mkdtemp(prefix="pins-scratch-")
        self.addCleanup(shutil.rmtree, scratch, True)
        marker = os.path.join(scratch, "collecting")
        self._write("tests/py/conftest.py",
                    "import os, time\nopen(os.environ['PINS_MARKER'], 'w').close()\n"
                    "time.sleep(60)\n")
        self._commit()
        env = dict(os.environ, TMPDIR=scratch, PINS_MARKER=marker)
        proc = subprocess.Popen([sys.executable, TOOL, "--engine", self.engine, "--check"],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 60
            while not os.path.exists(marker):
                self.assertIsNone(proc.poll(), "the tool ended before collection started")
                self.assertLess(time.time(), deadline, "collection never started")
                time.sleep(0.05)
            self.assertTrue(glob.glob(os.path.join(scratch, "sync-assurance-index-*")))
            proc.send_signal(sig)
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        return glob.glob(os.path.join(scratch, "sync-assurance-index-*"))

    def test_sigterm_mid_collection_removes_the_index_checkout(self):
        self.assertEqual(self._signalled_run_leaves(signal.SIGTERM), [])

    def test_sighup_mid_collection_removes_the_index_checkout(self):
        self.assertEqual(self._signalled_run_leaves(signal.SIGHUP), [])

    def test_the_signal_handlers_are_put_back(self):
        self._commit()
        before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))
        with sync.counted_tree(self.engine):
            pass
        self.assertEqual((signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)),
                         before)

    # -- the pytest environment ------------------------------------------------------------------

    def test_pytest_addopts_in_the_callers_environment_does_not_change_the_count(self):
        for addopts in ("-v", "-k test_one", "--color=yes"):
            with self.subTest(addopts=addopts):
                old = os.environ.get("PYTEST_ADDOPTS")
                os.environ["PYTEST_ADDOPTS"] = addopts
                try:
                    self.assertEqual(sync.collect_python_count(self.engine), 2)
                finally:
                    if old is None:
                        del os.environ["PYTEST_ADDOPTS"]
                    else:
                        os.environ["PYTEST_ADDOPTS"] = old

    def test_a_forced_colour_environment_does_not_reach_the_summary_line(self):
        # --color=no in COLLECT_ARGV: PY_COLORS=1 would otherwise wrap the summary in escapes.
        old = os.environ.get("PY_COLORS")
        os.environ["PY_COLORS"] = "1"
        try:
            self.assertEqual(sync.collect_python_count(self.engine), 2)
        finally:
            if old is None:
                del os.environ["PY_COLORS"]
            else:
                os.environ["PY_COLORS"] = old

    # -- refusals --------------------------------------------------------------------------------

    def test_a_failing_git_call_is_refused_and_writes_nothing(self):
        os.makedirs(os.path.join(self.engine, ".git"))  # looks like a checkout, is not a repo
        res = self._tool()
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("git ", res.stderr)
        self.assertIn("failed in the engine", res.stderr)
        self.assertEqual(self._doc(), _DOC.format(py=1, sh=7))

    def _intent_to_add(self):
        self._commit()
        self._write("tests/py/test_new.py", "def test_new():\n    pass\n")
        _git(self.engine, "add", "-N", "tests/py/test_new.py")

    def test_intent_to_add_is_refused_at_each_counting_call_site(self):
        self._intent_to_add()
        for name, call in (("python count", sync.collect_python_count),
                           ("shell count", sync.count_shell_suites)):
            with self.subTest(site=name):
                with self.assertRaises(sync.Refused) as cm:
                    call(self.engine)
                self.assertIn("intent-to-add", str(cm.exception))
                self.assertIn("tests/py/test_new.py", str(cm.exception))
        res = self._tool()
        self.assertEqual(res.returncode, 2)
        self.assertEqual(self._doc(), _DOC.format(py=1, sh=7))

    def test_an_unmerged_test_file_is_refused_at_each_counting_call_site(self):
        self._commit()
        _git(self.engine, "checkout", "-q", "-b", "other")
        self._write("tests/test-x.sh", "other\n")
        _git(self.engine, "commit", "-q", "-am", "other")
        _git(self.engine, "checkout", "-q", "-")
        self._write("tests/test-x.sh", "mine\n")
        _git(self.engine, "commit", "-q", "-am", "mine")
        subprocess.run(["git", "-C", self.engine, "-c", "user.name=f", "-c", "user.email=f@i",
                        "merge", "other"], capture_output=True)
        for name, call in (("python count", sync.collect_python_count),
                           ("shell count", sync.count_shell_suites)):
            with self.subTest(site=name):
                with self.assertRaises(sync.Refused) as cm:
                    call(self.engine)
                self.assertIn("unmerged", str(cm.exception))
                self.assertIn("tests/test-x.sh", str(cm.exception))

    def test_an_unmerged_assurance_md_is_refused_by_name(self):
        self._commit()
        # An ASSURANCE.md that is untracked: the commit states no figure.
        _git(self.engine, "rm", "-q", "--cached", "ASSURANCE.md")
        with self.assertRaises(sync.Refused) as cm:
            sync.stated_doc(self.engine)
        self.assertIn("no resolved ASSURANCE.md", str(cm.exception))
        self.assertIn("git add ASSURANCE.md", str(cm.exception))
        res = self._tool("--check")
        self.assertEqual(res.returncode, 2)
        self.assertIn("no resolved ASSURANCE.md", res.stderr)

    # -- notices ---------------------------------------------------------------------------------

    def test_an_unstaged_test_is_named_once(self):
        self._commit()
        self._write("tests/py/test_a.py", "def test_one():\n    pass\n")
        self.assertEqual(sync.unstaged_tests(self.engine), ["tests/py/test_a.py"])
        res = self._tool()
        self.assertEqual(res.stderr.count("tests/py/test_a.py"), 1, res.stderr)

    def test_write_mode_names_an_unstaged_assurance_md(self):
        self._commit()
        res = self._tool()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self._doc(), _DOC.format(py=2, sh=1))
        # The notice comes on the run that finds the figures already true but unstaged.
        res = self._tool()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("has unstaged changes", res.stderr)
        self.assertIn("git add ASSURANCE.md", res.stderr)
        # Staged, the notice goes.
        _git(self.engine, "add", "ASSURANCE.md")
        res = self._tool()
        self.assertEqual(res.returncode, 0)
        self.assertNotIn("unstaged", res.stderr)


if __name__ == "__main__":
    unittest.main()
