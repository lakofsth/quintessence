# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Certify check for tests/INVARIANTS.md `the-count-tool-writes-what-the-gate-reads-or-nothing`,
the 21 Oct 2026 summary-line clause, written from the sentence alone:

  The tool and the gate read the collected count only from pytest's final summary line, matched
  whole: a node id or any other line of the output that contains 'N tests collected' never
  supplies the figure, and output whose last line is not a summary is refused. They refuse,
  naming it, a summary that says tests were deselected (`S/N tests collected (D deselected)`),
  an error in that shape included. The figures the counts are held to are read from the same
  index as the counts, so the gate and the tool's `--check` fail on a figure written and not
  staged, and name ASSURANCE.md's unstaged copy.

Each direction is driven through both surfaces the sentence names: the tool, run as a program
(`tools/sync_assurance_counts.py --engine <fixture>`), and the gate's own two methods in
tests/py/test_assurance_counts.py, run against a throwaway engine. Every fixture is a real git
repository with real pytest output, nothing faked.
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(ENGINE, "tools", "sync_assurance_counts.py")
GATE_PATH = os.path.join(ENGINE, "tests", "py", "test_assurance_counts.py")

_DOC = ("| Python tests | `pytest` | {py} tests over the engine modules. |\n"
        "| Shell suites | `run.sh` | {sh} end-to-end suites against a store |\n")

_HAVE_PYTEST = importlib.util.find_spec("pytest") is not None


def _load_gate():
    spec = importlib.util.spec_from_file_location("_certify_count_gate", GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(cwd, *argv):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@invalid",
                    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *argv],
                   cwd=cwd, env=env, check=True, capture_output=True)


@unittest.skipUnless(_HAVE_PYTEST, "pytest not installed — the figure is pytest's collection")
class TestCountIsReadFromTheSummaryLineOnly(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def setUp(self):
        self.engine = tempfile.mkdtemp(prefix="certify-count-")
        self.addCleanup(shutil.rmtree, self.engine, True)
        os.makedirs(os.path.join(self.engine, "tests", "py"))
        self._write("tests/py/test_a.py", "def test_one():\n    pass\n\n"
                                          "def test_two():\n    pass\n")
        self._write("tests/test-x.sh", "")
        _git(self.engine, "init", "-q")

    # -- fixture helpers -----------------------------------------------------------------------

    def _write(self, rel, text):
        with open(os.path.join(self.engine, rel), "w") as f:
            f.write(text)

    def _read(self, rel):
        with open(os.path.join(self.engine, rel)) as f:
            return f.read()

    def _commit(self, doc=None):
        if doc is not None:
            self._write("ASSURANCE.md", doc)
        _git(self.engine, "add", "-A")
        _git(self.engine, "commit", "-q", "-m", "fixture")

    def _tool(self, *argv):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return subprocess.run([sys.executable, TOOL, "--engine", self.engine, *argv],
                              capture_output=True, text=True, env=env)

    def _gate(self, method):
        """Run the real gate method against the fixture engine: its failure text, or None."""
        g = self.gate
        saved = g.ENGINE, g.ASSURANCE_MD
        g.ENGINE, g.ASSURANCE_MD = self.engine, os.path.join(self.engine, "ASSURANCE.md")
        try:
            result = unittest.TestResult()
            g.TestAssuranceCountsMatchReality(method).run(result)
        finally:
            g.ENGINE, g.ASSURANCE_MD = saved
        self.assertEqual((result.errors, result.skipped), ([], []),
                         f"the gate errored or skipped rather than judging: "
                         f"{result.errors or result.skipped}")
        return result.failures[0][1] if result.failures else None

    def _py_gate(self):
        return self._gate("test_python_test_count_matches_collection")

    def _sh_gate(self):
        return self._gate("test_shell_suite_count_matches_the_suite_directory")

    def _assert_refused(self, why, *, naming=None):
        before = self._read("ASSURANCE.md")
        proc = self._tool()
        self.assertEqual(proc.returncode, 2,
                         f"the tool did not refuse {why} (exit {proc.returncode}); "
                         f"stdout={proc.stdout!r} stderr={proc.stderr[-800:]!r}")
        self.assertEqual(self._read("ASSURANCE.md"), before,
                         f"the tool rewrote ASSURANCE.md over {why}")
        check = self._tool("--check")
        self.assertEqual(check.returncode, 2,
                         f"the tool's --check did not refuse {why} (exit {check.returncode})")
        failure = self._py_gate()
        self.assertIsNotNone(failure, f"the gate passed {why}")
        if naming:
            self.assertRegex(proc.stderr, naming, f"the tool's refusal of {why} does not name it")
            self.assertRegex(failure, naming, f"the gate's refusal of {why} does not name it")

    # -- direction 1: a node id carrying 'N tests collected' never supplies the figure ---------

    def test_a_node_id_containing_tests_collected_does_not_supply_the_figure(self):
        self._write("tests/py/test_p.py",
                    "import pytest\n\n"
                    "@pytest.mark.parametrize('x', [1], ids=['99 tests collected'])\n"
                    "def test_p(x):\n    pass\n")
        # Three tests are collected; the first line of the output is a node id saying 99.
        self._commit(_DOC.format(py=3, sh=1))
        self.assertIsNone(self._py_gate(), "the gate read the figure from a node id, not 3")
        check = self._tool("--check")
        self.assertEqual(check.returncode, 0,
                         f"the tool's --check read a figure other than the summary's 3: "
                         f"{check.stderr[-800:]!r}")
        # Control: the node id's number is rejected by both.
        self._commit(_DOC.format(py=99, sh=1))
        self.assertIsNotNone(self._py_gate(), "control: the gate accepted the node id's 99")
        self.assertEqual(self._tool("--check").returncode, 1,
                         "control: the tool's --check accepted the node id's 99")

    def test_the_tool_writes_the_summary_figure_not_a_node_ids(self):
        self._write("tests/py/test_p.py",
                    "import pytest\n\n"
                    "@pytest.mark.parametrize('x', [1], ids=['99 tests collected'])\n"
                    "def test_p(x):\n    pass\n")
        self._commit(_DOC.format(py=7, sh=1))
        proc = self._tool()
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        self.assertIn("3 tests over the engine modules", self._read("ASSURANCE.md"))

    # -- direction 2: output whose last line is not a summary is refused -----------------------

    def test_output_whose_last_line_is_not_a_summary_is_refused(self):
        self._write("tests/py/conftest.py",
                    "def pytest_unconfigure(config):\n"
                    "    print('trailing line after the summary')\n")
        self._commit(_DOC.format(py=2, sh=1))
        self._assert_refused("output whose last line is not pytest's summary")

    def test_a_summary_shaped_text_on_a_trailing_line_is_not_the_figure(self):
        # The last line contains 'N tests collected' but is not a summary matched whole.
        self._write("tests/py/conftest.py",
                    "def pytest_unconfigure(config):\n"
                    "    print('note: 2 tests collected earlier, see above')\n")
        self._commit(_DOC.format(py=2, sh=1))
        self._assert_refused("a last line that merely contains 'N tests collected'")

    # -- direction 3: a deselected summary is refused, naming it -------------------------------

    _DESELECT = ("def pytest_collection_modifyitems(config, items):\n"
                 "    drop = [i for i in items if i.name == 'test_two']\n"
                 "    items[:] = [i for i in items if i.name != 'test_two']\n"
                 "    config.hook.pytest_deselected(items=drop)\n")

    def test_a_deselected_summary_is_refused_and_named(self):
        # pytest prints '1/2 tests collected (1 deselected)': neither 1 nor 2 is the figure.
        self._write("tests/py/conftest.py", self._DESELECT)
        self._commit(_DOC.format(py=2, sh=1))
        self._assert_refused("a summary reporting deselected tests", naming=r"deselected")
        self._commit(_DOC.format(py=1, sh=1))
        self._assert_refused("a summary reporting deselected tests (figure 1)",
                             naming=r"deselected")

    def test_a_deselected_summary_with_an_error_is_refused_and_named(self):
        # '1/2 tests collected (1 deselected), 1 error'.
        self._write("tests/py/conftest.py", self._DESELECT)
        self._write("tests/py/test_broken.py", "def (\n")
        self._commit(_DOC.format(py=2, sh=1))
        self._assert_refused("a deselected summary that also reports an error",
                             naming=r"deselected")

    # -- direction 4: the figures are read from the index, as the counts are -------------------

    def test_a_figure_written_and_not_staged_fails_the_gate_and_check(self):
        self._commit(_DOC.format(py=9, sh=9))       # the index holds stale figures
        self._write("ASSURANCE.md", _DOC.format(py=2, sh=1))   # true figures, not staged
        unstaged = r"(?i)unstaged|not staged"
        check = self._tool("--check")
        self.assertNotEqual(check.returncode, 0,
                            "the tool's --check passed a figure written and not staged")
        self.assertIn("ASSURANCE.md", check.stderr)
        self.assertRegex(check.stderr, unstaged,
                         "the tool's --check does not name ASSURANCE.md's unstaged copy")
        for name, failure in (("python", self._py_gate()), ("shell", self._sh_gate())):
            self.assertIsNotNone(failure,
                                 f"the {name} gate passed a figure written and not staged")
            self.assertIn("ASSURANCE.md", failure)
            self.assertRegex(failure, unstaged,
                             f"the {name} gate does not name ASSURANCE.md's unstaged copy")
        # Control: staged, the same figures are green on both surfaces.
        _git(self.engine, "add", "ASSURANCE.md")
        self.assertEqual(self._tool("--check").returncode, 0)
        self.assertIsNone(self._py_gate())
        self.assertIsNone(self._sh_gate())


if __name__ == "__main__":
    unittest.main()
