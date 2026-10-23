# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Certify check for `the-count-tool-writes-what-the-gate-reads-or-nothing`, clean-checkout amendment.

The sentence: the count the tool (`tools/sync_assurance_counts.py`) writes and the gate
(`tests/py/test_assurance_counts.py`) checks is the count a clean checkout of the commit gives —
the gate fails, naming the cause, on every collection the tool refuses (a non-zero pytest exit,
or a summary reporting an error), a `tests/py` test file or `tests/test-*.sh` suite that git does
not track (untracked or ignored) is left out of both counts, and pytest's singular
`1 test collected` is read as 1.

Written from the sentence alone, black-box: each case builds a throwaway engine — the tracked
files of this one, committed into a fresh repository, with ASSURANCE.md synced by the tool and
committed so the copy starts clean and green — perturbs it the one way the case names, and then
asks the copy's OWN tool and the copy's OWN gate file what they make of it. Nothing here imports
either of them, so the check reads what they do, not how they do it.

Directions, one test per (direction, subject):
  - collection exits non-zero (a tracked test module that fails to import): the tool refuses and
    leaves ASSURANCE.md byte-identical; the gate FAILS and its output names the broken module.
  - collection exits 0 but its summary reports an error (a tracked conftest that zeroes the exit
    status of a --collect-only run): the same two expectations.
  - an untracked `tests/py` test file, at top level and in a subdirectory: the tool's --check and
    its write both leave ASSURANCE.md as it is, and the gate passes.
  - an ignored `tests/py` test file (`.git/info/exclude`): the same.
  - an untracked `tests/test-*.sh` suite: the same.
  - an ignored `tests/test-*.sh` suite: the same.
  - a collection of exactly one test (a tracked conftest that ignores every other test module
    under --collect-only, so pytest prints `1 test collected`): the tool writes 1, and the gate,
    against that committed figure, passes.
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
TOOL = os.path.join("tools", "sync_assurance_counts.py")
GATE = os.path.join("tests", "py", "test_assurance_counts.py")
PY_ROW = re.compile(r"(\d+) tests? over the engine modules")
SH_ROW = re.compile(r"(\d+) end-to-end suites?")

BROKEN = "test_zz_cert_broken_probe.py"
SINGLE = "test_zz_cert_single_probe.py"
PROBE_TESTS = "def test_probe_one():\n    pass\n\n\ndef test_probe_two():\n    pass\n\n\ndef test_probe_three():\n    pass\n"
PROBE_SUITE = "#!/usr/bin/env bash\nexit 0\n"

_GIT = ["git", "-c", "user.name=cert", "-c", "user.email=cert@example.invalid",
        "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]


def _env():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and k not in ("PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(argv, cwd):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=_env())


def _git(cwd, *args):
    p = _run([*_GIT, *args], cwd)
    if p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{p.stdout}{p.stderr}")
    return p.stdout


def _tracked_files(root):
    p = _run(["git", "ls-files", "-z"], root)
    if p.returncode == 0 and p.stdout:
        return [f for f in p.stdout.split("\0") if f and os.path.isfile(os.path.join(root, f))]
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in (".git", "__pycache__")]
        out += [os.path.relpath(os.path.join(d, f), root) for f in files if not f.endswith(".pyc")]
    return out


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _append(path, text):
    with open(path, "a") as f:
        f.write(text)


@unittest.skipIf(importlib.util.find_spec("pytest") is None,
                 "pytest not installed — the count is pytest's collection count")
class TestCountIsWhatACleanCheckoutGives(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="cert-count-gate-")
        base = os.path.join(cls._tmp, "base")
        for rel in _tracked_files(ENGINE):
            dst = os.path.join(base, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(ENGINE, rel), dst)
        _git(base, "init", "-q")
        _git(base, "add", "-A")
        _git(base, "commit", "-q", "-m", "base")
        p = _run([sys.executable, TOOL], base)
        if p.returncode != 0:
            raise AssertionError(f"baseline: the tool could not sync a clean copy:\n{p.stdout}{p.stderr}")
        _git(base, "add", "-A")
        _git(base, "commit", "-q", "--allow-empty", "-m", "sync")
        cls.base = base
        cls._n = 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------------------------

    def _copy(self):
        type(self)._n += 1
        d = os.path.join(self._tmp, f"case{self._n}")
        shutil.copytree(self.base, d, symlinks=True)
        return d

    def _gate(self, eng):
        return _run([sys.executable, "-m", "pytest", GATE, "-q", "-p", "no:cacheprovider"], eng)

    def _tool(self, eng, *args):
        return _run([sys.executable, TOOL, *args], eng)

    def _assert_gate_passes(self, eng, why):
        p = self._gate(eng)
        self.assertEqual(p.returncode, 0, f"{why}: the gate failed:\n{(p.stdout + p.stderr)[-3000:]}")

    def _assert_gate_fails_naming(self, eng, cause, why):
        p = self._gate(eng)
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, f"{why}: the gate PASSED:\n{out[-3000:]}")
        self.assertIn(cause, out, f"{why}: the gate failed without naming {cause}:\n{out[-3000:]}")

    def _assert_tool_leaves_figures(self, eng, why):
        before = _read(os.path.join(eng, "ASSURANCE.md"))
        c = self._tool(eng, "--check")
        self.assertEqual(c.returncode, 0, f"{why}: `--check` says the figures are stale "
                         f"(exit {c.returncode}):\n{c.stdout}{c.stderr}")
        w = self._tool(eng)
        self.assertEqual(w.returncode, 0, f"{why}: the tool exited {w.returncode}:\n{w.stdout}{w.stderr}")
        self.assertEqual(_read(os.path.join(eng, "ASSURANCE.md")), before,
                         f"{why}: the tool rewrote ASSURANCE.md:\n{w.stdout}{w.stderr}")

    def _assert_tool_refuses_naming(self, eng, cause, why):
        before = _read(os.path.join(eng, "ASSURANCE.md"))
        w = self._tool(eng)
        self.assertNotEqual(w.returncode, 0, f"{why}: the tool did not refuse:\n{w.stdout}{w.stderr}")
        self.assertEqual(_read(os.path.join(eng, "ASSURANCE.md")), before,
                         f"{why}: the tool refused but ASSURANCE.md changed")
        self.assertIn(cause, w.stdout + w.stderr, f"{why}: the refusal does not name {cause}:\n{w.stderr}")

    # -- perturbations ---------------------------------------------------------------------

    def _with_broken_module(self, exit_zero):
        eng = self._copy()
        _write(os.path.join(eng, "tests", "py", BROKEN),
               "import nonexistent_module_zz_cert  # noqa: F401\n\n\ndef test_never():\n    pass\n")
        if exit_zero:
            _append(os.path.join(eng, "tests", "py", "conftest.py"),
                    "\n\ndef pytest_sessionfinish(session, exitstatus):  # cert probe\n"
                    "    if session.config.option.collectonly:\n"
                    "        session.exitstatus = 0\n")
        _git(eng, "add", "-A")
        _git(eng, "commit", "-q", "-m", "broken collection")
        if exit_zero:
            p = _run([sys.executable, "-m", "pytest", os.path.join("tests", "py"), "--collect-only",
                      "-q", "-p", "no:cacheprovider", "--color=no"], eng)
            assert p.returncode == 0 and " error" in p.stdout, \
                f"probe precondition: expected exit 0 with an error summary:\n{p.stdout[-1500:]}"
        return eng

    def _with_untracked(self, rel, text, ignored):
        eng = self._copy()
        _write(os.path.join(eng, rel), text)
        if ignored:
            _append(os.path.join(eng, ".git", "info", "exclude"), "\n/" + rel.replace(os.sep, "/") + "\n")
            st = _git(eng, "status", "--porcelain", "--ignored", "--", rel)
            assert st.startswith("!!"), f"probe precondition: {rel} is not ignored: {st!r}"
        else:
            st = _git(eng, "status", "--porcelain", "--untracked-files=all", "--", rel)
            assert st.startswith("??"), f"probe precondition: {rel} is not untracked: {st!r}"
        return eng

    def _with_single_test(self):
        eng = self._copy()
        _write(os.path.join(eng, "tests", "py", SINGLE), "def test_the_only_one():\n    pass\n")
        _append(os.path.join(eng, "tests", "py", "conftest.py"),
                "\n\ndef pytest_ignore_collect(collection_path, config):  # cert probe\n"
                "    if (config.option.collectonly and collection_path.name.startswith('test_')\n"
                f"            and collection_path.suffix == '.py' and collection_path.name != {SINGLE!r}):\n"
                "        return True\n"
                "    return None\n")
        _git(eng, "add", "-A")
        _git(eng, "commit", "-q", "-m", "one test")
        p = _run([sys.executable, "-m", "pytest", os.path.join("tests", "py"), "--collect-only",
                  "-q", "-p", "no:cacheprovider", "--color=no"], eng)
        assert re.search(r"(?m)^1 test collected\b", p.stdout), \
            f"probe precondition: expected `1 test collected`:\n{p.stdout[-1500:]}"
        return eng

    # -- the baseline the cases perturb ----------------------------------------------------

    def test_baseline_clean_copy_is_green_for_both(self):
        eng = self._copy()
        self._assert_tool_leaves_figures(eng, "clean copy")
        self._assert_gate_passes(eng, "clean copy")

    # -- a collection the tool refuses: non-zero exit --------------------------------------

    def test_nonzero_collection_exit_tool_refuses(self):
        eng = self._with_broken_module(exit_zero=False)
        self._assert_tool_refuses_naming(eng, BROKEN, "collection exits non-zero")

    def test_nonzero_collection_exit_gate_fails_naming_the_cause(self):
        eng = self._with_broken_module(exit_zero=False)
        self._assert_gate_fails_naming(eng, BROKEN, "collection exits non-zero")

    # -- a collection the tool refuses: exit 0, summary reports an error -------------------

    def test_error_summary_with_exit_zero_tool_refuses(self):
        eng = self._with_broken_module(exit_zero=True)
        self._assert_tool_refuses_naming(eng, BROKEN, "collection exits 0 with an error in its summary")

    def test_error_summary_with_exit_zero_gate_fails_naming_the_cause(self):
        eng = self._with_broken_module(exit_zero=True)
        self._assert_gate_fails_naming(eng, BROKEN, "collection exits 0 with an error in its summary")

    # -- untracked / ignored python test files ---------------------------------------------

    def test_untracked_python_test_file_left_out_of_tool_count(self):
        eng = self._with_untracked(os.path.join("tests", "py", "test_zz_cert_untracked.py"), PROBE_TESTS, False)
        self._assert_tool_leaves_figures(eng, "untracked tests/py test file")

    def test_untracked_python_test_file_left_out_of_gate_count(self):
        eng = self._with_untracked(os.path.join("tests", "py", "test_zz_cert_untracked.py"), PROBE_TESTS, False)
        self._assert_gate_passes(eng, "untracked tests/py test file")

    def test_untracked_python_test_file_in_subdirectory_left_out_of_both(self):
        eng = self._with_untracked(os.path.join("tests", "py", "zz_cert_sub", "test_zz_cert_nested.py"),
                                   PROBE_TESTS, False)
        self._assert_tool_leaves_figures(eng, "untracked tests/py/<sub> test file")
        self._assert_gate_passes(eng, "untracked tests/py/<sub> test file")

    def test_ignored_python_test_file_left_out_of_tool_count(self):
        eng = self._with_untracked(os.path.join("tests", "py", "test_zz_cert_ignored.py"), PROBE_TESTS, True)
        self._assert_tool_leaves_figures(eng, "ignored tests/py test file")

    def test_ignored_python_test_file_left_out_of_gate_count(self):
        eng = self._with_untracked(os.path.join("tests", "py", "test_zz_cert_ignored.py"), PROBE_TESTS, True)
        self._assert_gate_passes(eng, "ignored tests/py test file")

    # -- untracked / ignored shell suites --------------------------------------------------

    def test_untracked_shell_suite_left_out_of_tool_count(self):
        eng = self._with_untracked(os.path.join("tests", "test-zz-cert-untracked.sh"), PROBE_SUITE, False)
        self._assert_tool_leaves_figures(eng, "untracked tests/test-*.sh suite")

    def test_untracked_shell_suite_left_out_of_gate_count(self):
        eng = self._with_untracked(os.path.join("tests", "test-zz-cert-untracked.sh"), PROBE_SUITE, False)
        self._assert_gate_passes(eng, "untracked tests/test-*.sh suite")

    def test_ignored_shell_suite_left_out_of_tool_count(self):
        eng = self._with_untracked(os.path.join("tests", "test-zz-cert-ignored.sh"), PROBE_SUITE, True)
        self._assert_tool_leaves_figures(eng, "ignored tests/test-*.sh suite")

    def test_ignored_shell_suite_left_out_of_gate_count(self):
        eng = self._with_untracked(os.path.join("tests", "test-zz-cert-ignored.sh"), PROBE_SUITE, True)
        self._assert_gate_passes(eng, "ignored tests/test-*.sh suite")

    # -- pytest's singular summary ---------------------------------------------------------

    def test_singular_collection_tool_writes_one(self):
        eng = self._with_single_test()
        w = self._tool(eng)
        self.assertEqual(w.returncode, 0, f"`1 test collected`: the tool exited {w.returncode}:\n"
                         f"{w.stdout}{w.stderr}")
        with open(os.path.join(eng, "ASSURANCE.md")) as f:
            m = PY_ROW.search(f.read())
        self.assertIsNotNone(m, "`1 test collected`: ASSURANCE.md lost its python row")
        self.assertEqual(int(m.group(1)), 1, f"`1 test collected`: the tool wrote {m.group(1)}")

    def test_singular_collection_gate_reads_one(self):
        eng = self._with_single_test()
        path = os.path.join(eng, "ASSURANCE.md")
        with open(path) as f:
            text = f.read()
        m = PY_ROW.search(text)
        self.assertIsNotNone(m, "baseline ASSURANCE.md has no python row")
        # The figure a reader would write for one test, set by hand so this half does not lean on
        # the tool: the tool's own half is the test above.
        text = text[:m.start(1)] + "1" + text[m.end(1):]
        with open(path, "w") as f:
            f.write(text)
        _git(eng, "commit", "-q", "-am", "state one test")
        self._assert_gate_passes(eng, "`1 test collected` with ASSURANCE.md stating 1")


if __name__ == "__main__":
    unittest.main()
