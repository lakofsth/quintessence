# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Certify: the-count-tool-writes-what-the-gate-reads-or-nothing.

tools/sync_assurance_counts.py either leaves ASSURANCE.md stating exactly the python count and
the shell-suite count the count gate (tests/py/test_assurance_counts.py) checks, or exits
non-zero naming the cause and leaves ASSURANCE.md byte-identical — it never writes a figure
taken from a failed collection.

Written from that sentence alone. Each case copies the tool into a throwaway engine of known
shape (a tests/py/ of a counted number of tests, a tests/ of a counted number of test-*.sh, an
ASSURANCE.md holding both rows with wrong figures) and runs it the way a person would, by path.
The directions the sentence has, one test each:

  - success: exit 0 and both rows now carry the counts the gate reads — pytest's collection
    count (read here with the gate's own command) and the tests/test-*.sh glob;
  - a collection that errors, even when pytest still prints "N tests collected": non-zero,
    the cause named, ASSURANCE.md byte-identical;
  - pytest absent: non-zero, the cause named, byte-identical;
  - a row missing (either one, so a write of the OTHER row is caught too): non-zero, the
    missing row named, byte-identical;
  - a row stated twice: either refused byte-identical, or every statement of it carries the
    true count — the gate reads the first, a reader may read either.
"""
import glob
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

PY_ROW = "| Python tests | `python3 -m pytest tests/py -q` | {} tests over the engine modules. |\n"
SH_ROW = "| Shell suites | `bash tests/run.sh` | {} end-to-end suites against a throwaway store |\n"
GATE_PY = r"(\d+) tests over the engine modules"
GATE_SH = r"(\d+) end-to-end suites"

N_PY = 5          # tests written into the fixture's tests/py
N_SH = 3          # test-*.sh written into the fixture's tests/


def _assurance(py="999", sh="999", py_rows=1, sh_rows=1) -> str:
    return ("# Assurance\n\n| Gate | Command | What |\n|---|---|---|\n"
            + "".join(SH_ROW.format(sh) for _ in range(sh_rows))
            + "".join(PY_ROW.format(py) for _ in range(py_rows))
            + "\ntrailing prose that must survive untouched.\n")


@unittest.skipIf(importlib.util.find_spec("pytest") is None,
                 "pytest not installed — the counts the gate reads are pytest's")
class TestTheCountToolWritesWhatTheGateReadsOrNothing(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(TOOL), f"{TOOL} does not exist")
        self.root = tempfile.mkdtemp(prefix="certify-sync-counts-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "tools"))
        os.makedirs(os.path.join(self.root, "tests", "py"))
        self.tool = os.path.join(self.root, "tools", "sync_assurance_counts.py")
        shutil.copy(TOOL, self.tool)
        # The tool ends through quintessence.pipeexit.run, as every entry point does
        # (a-reader-that-closes-early-ends-every-entry-point-with-141-and-no-message).
        os.makedirs(os.path.join(self.root, "quintessence"))
        for mod in ("__init__.py", "pipeexit.py"):
            shutil.copy(os.path.join(ENGINE, "quintessence", mod),
                        os.path.join(self.root, "quintessence", mod))
        with open(os.path.join(self.root, "tests", "py", "test_a.py"), "w") as f:
            f.write("def test_1(): pass\ndef test_2(): pass\ndef test_3(): pass\n")
        with open(os.path.join(self.root, "tests", "py", "test_b.py"), "w") as f:
            f.write("import unittest\nclass T(unittest.TestCase):\n"
                    "    def test_x(self): pass\n    def test_y(self): pass\n")
        for i in range(N_SH):
            with open(os.path.join(self.root, "tests", f"test-s{i}.sh"), "w") as f:
                f.write("#!/usr/bin/env bash\nexit 0\n")
        # a file the glob must NOT count
        with open(os.path.join(self.root, "tests", "helper.sh"), "w") as f:
            f.write("#!/usr/bin/env bash\n")
        self.assurance = os.path.join(self.root, "ASSURANCE.md")

    def _write_assurance(self, text):
        with open(self.assurance, "wb") as f:
            f.write(text.encode())
        return text.encode()

    def _run(self, *py_flags):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        return subprocess.run([sys.executable, *py_flags, self.tool], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=300)

    def _bytes(self):
        with open(self.assurance, "rb") as f:
            return f.read()

    def _gate_counts(self):
        # Read exactly as the gate reads them: pytest's collection, the test-*.sh glob.
        out = subprocess.run([sys.executable, "-m", "pytest", os.path.join("tests", "py"),
                              "--collect-only", "-q", "-p", "no:cacheprovider", "--color=no"],
                             cwd=self.root, capture_output=True, text=True).stdout
        m = re.search(r"(\d+) tests collected", out)
        self.assertIsNotNone(m, out)
        return int(m.group(1)), len(glob.glob(os.path.join(self.root, "tests", "test-*.sh")))

    def _assert_refused(self, proc, before, cause_words):
        self.assertNotEqual(proc.returncode, 0,
                            f"tool exited 0 where it must refuse\nstdout:{proc.stdout}\n"
                            f"stderr:{proc.stderr}")
        self.assertEqual(self._bytes(), before,
                         f"ASSURANCE.md changed on a refusal\nnow:\n{self._bytes().decode()}")
        said = (proc.stderr + proc.stdout).lower()
        self.assertTrue(any(w.lower() in said for w in cause_words),
                        f"the refusal does not name the cause (looked for {cause_words}):\n"
                        f"{proc.stderr}{proc.stdout}")

    # --- direction 1: success writes exactly the gate's counts -------------------------------
    def test_success_leaves_the_counts_the_gate_reads(self):
        self._write_assurance(_assurance())
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        py, sh = self._gate_counts()
        self.assertEqual((py, sh), (N_PY, N_SH), "fixture sanity")
        text = self._bytes().decode()
        self.assertEqual(int(re.search(GATE_PY, text).group(1)), py, text)
        self.assertEqual(int(re.search(GATE_SH, text).group(1)), sh, text)
        self.assertEqual(text, _assurance(py=str(py), sh=str(sh)),
                         "anything other than the two figures changed")

    def test_success_from_already_true_figures_is_a_no_op(self):
        before = self._write_assurance(_assurance(py=str(N_PY), sh=str(N_SH)))
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._bytes(), before)

    # --- direction 2: a failed collection writes nothing -----------------------------------
    def test_a_collection_error_is_refused_and_nothing_is_written(self):
        # pytest still prints "5 tests collected, 1 error" here: a figure is available, and it
        # is a figure taken from a failed collection.
        with open(os.path.join(self.root, "tests", "py", "test_broken.py"), "w") as f:
            f.write("import a_module_that_does_not_exist_anywhere\n"
                    "def test_never(): pass\n")
        before = self._write_assurance(_assurance())
        proc = self._run()
        self._assert_refused(proc, before, ["test_broken", "a_module_that_does_not_exist",
                                            "collection", "error"])

    def test_a_syntax_error_in_a_test_file_is_refused(self):
        with open(os.path.join(self.root, "tests", "py", "test_syntax.py"), "w") as f:
            f.write("def test_(:\n")
        before = self._write_assurance(_assurance())
        proc = self._run()
        self._assert_refused(proc, before, ["test_syntax", "syntax", "collection", "error"])

    # --- direction 3: no pytest, no figure ---------------------------------------------------
    def test_pytest_absent_is_refused_and_nothing_is_written(self):
        hidden = subprocess.run([sys.executable, "-s", "-c",
                                 "import importlib.util,sys;"
                                 "sys.exit(importlib.util.find_spec('pytest') is not None)"],
                                capture_output=True)
        if hidden.returncode != 0:
            self.skipTest("pytest is importable without the user site; cannot hide it here")
        before = self._write_assurance(_assurance())
        proc = self._run("-s")
        self._assert_refused(proc, before, ["pytest"])

    # --- direction 4: a row the gate reads is missing -----------------------------------------
    def test_missing_python_row_is_refused_and_the_shell_row_is_not_written(self):
        before = self._write_assurance(_assurance(py_rows=0))
        proc = self._run()
        self._assert_refused(proc, before, ["tests over the engine modules", "python"])

    def test_missing_shell_row_is_refused_and_the_python_row_is_not_written(self):
        before = self._write_assurance(_assurance(sh_rows=0))
        proc = self._run()
        self._assert_refused(proc, before, ["end-to-end suites", "shell"])

    def test_missing_assurance_is_refused_and_not_created(self):
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertFalse(os.path.exists(self.assurance), "a missing ASSURANCE.md was created")
        self.assertIn("assurance", (proc.stderr + proc.stdout).lower())

    # --- direction 5: a row stated twice -----------------------------------------------------
    def test_a_doubled_row_is_refused_or_every_statement_is_true(self):
        for kw in ({"py_rows": 2}, {"sh_rows": 2}):
            with self.subTest(**kw):
                before = self._write_assurance(_assurance(**kw))
                proc = self._run()
                if proc.returncode != 0:
                    self._assert_refused(proc, before, ["tests over the engine modules",
                                                        "end-to-end suites", "expected",
                                                        "ambiguous", "two", "2"])
                    continue
                text = self._bytes().decode()
                self.assertEqual([int(x) for x in re.findall(GATE_PY, text)],
                                 [N_PY] * kw.get("py_rows", 1), text)
                self.assertEqual([int(x) for x in re.findall(GATE_SH, text)],
                                 [N_SH] * kw.get("sh_rows", 1), text)


if __name__ == "__main__":
    unittest.main()
