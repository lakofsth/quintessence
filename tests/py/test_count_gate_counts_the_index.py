# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Certify check for `the-count-tool-writes-what-the-gate-reads-or-nothing`, the 21 Oct 2026
index clause.

The sentence: the tool and the gate count what a clean checkout of the commit gives by counting
the index — collection runs over a temporary checkout of the engine's index, and the suites are
the index's `tests/test-*.sh` — so an unstaged edit to a tracked test file, a tracked file
deleted only from the working tree, and an untracked or ignored file change neither count, while
a staged file counts in both, being part of the commit about to be made; and every git call
either makes runs with `GIT_*` stripped from its environment, so a `GIT_INDEX_FILE` or `GIT_DIR`
set by a hook cannot name another index.

Written from the sentence alone, black-box, in the shape of test_count_gate_clean_checkout.py:
a throwaway engine is built from this one's tracked files, committed into a fresh repository
with ASSURANCE.md synced by its own tool, and each case perturbs the working tree, the index or
the environment the one way it names, then asks the copy's OWN tool and the copy's OWN gate file
what they make of it. Nothing here imports either of them.

Directions, one test each:
  working tree differs from the index, and neither count moves —
    - an unstaged added test function in a tracked tests/py file;
    - an unstaged removal of every test from a tracked tests/py file;
    - an unstaged edit that breaks a tracked tests/py file (the tool must not refuse: a clean
      checkout collects);
    - a tracked tests/py file deleted from the working tree only;
    - a tracked tests/test-*.sh deleted from the working tree only;
    - an untracked, and an ignored, file that is not a test file but changes what pytest
      collects (a tests/py/pytest.ini narrowing python_files);
    - an untracked tests/py test file and an untracked tests/test-*.sh (the earlier clause,
      kept so the index reading does not lose it);
  the index differs from the working tree, and both counts move with the index —
    - a staged new tests/py test file (the tool writes +N, the gate passes on it and fails on the
      old figure, and a clone of the commit made from that index passes);
    - a staged added test function whose working-tree copy was then reverted;
    - a staged new tests/test-*.sh whose working-tree copy was then deleted;
  a hook's environment cannot name another index —
    - GIT_INDEX_FILE naming an empty index;
    - GIT_DIR naming another repository.
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

# A tracked tests/py file the cases edit, delete or narrow collection to — never the gate.
VICTIM = os.path.join("tests", "py", "test_slugs.py")
VICTIM_SUITE = os.path.join("tests", "test-tsk.sh")
ADDED_FN = "\n\ndef test_zz_cert_index_added():\n    pass\n"
PROBE_TESTS = ("def test_probe_one():\n    pass\n\n\ndef test_probe_two():\n    pass\n\n\n"
               "def test_probe_three():\n    pass\n")
PROBE_SUITE = "#!/usr/bin/env bash\nexit 0\n"
NARROWING_INI = "[pytest]\npython_files = test_slugs.py\n"

_GIT = ["git", "-c", "user.name=cert", "-c", "user.email=cert@example.invalid",
        "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]


def _env(extra=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and k not in ("PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra or {})
    return env


def _run(argv, cwd, extra=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=_env(extra))


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


def _figures(eng):
    with open(os.path.join(eng, "ASSURANCE.md")) as f:
        text = f.read()
    py, sh = PY_ROW.search(text), SH_ROW.search(text)
    assert py and sh, "ASSURANCE.md lost a row"
    return int(py.group(1)), int(sh.group(1))


def _set_figures(eng, py, sh):
    path = os.path.join(eng, "ASSURANCE.md")
    with open(path) as f:
        text = f.read()
    m = PY_ROW.search(text)
    text = text[:m.start(1)] + str(py) + text[m.end(1):]
    m = SH_ROW.search(text)
    text = text[:m.start(1)] + str(sh) + text[m.end(1):]
    with open(path, "w") as f:
        f.write(text)


@unittest.skipIf(importlib.util.find_spec("pytest") is None,
                 "pytest not installed — the count is pytest's collection count")
class TestTheCountIsTheIndexs(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="cert-count-index-")
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
        cls.py, cls.sh = _figures(base)
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

    def _gate(self, eng, extra=None):
        return _run([sys.executable, "-m", "pytest", GATE, "-q", "-p", "no:cacheprovider"], eng, extra)

    def _tool(self, eng, *args, extra=None):
        return _run([sys.executable, TOOL, *args], eng, extra)

    def _assert_gate_passes(self, eng, why, extra=None):
        p = self._gate(eng, extra)
        self.assertEqual(p.returncode, 0, f"{why}: the gate failed:\n{(p.stdout + p.stderr)[-3000:]}")

    def _assert_gate_fails(self, eng, why):
        p = self._gate(eng)
        self.assertNotEqual(p.returncode, 0, f"{why}: the gate PASSED:\n{(p.stdout + p.stderr)[-3000:]}")

    def _assert_tool_leaves_figures(self, eng, why, extra=None):
        before = _read(os.path.join(eng, "ASSURANCE.md"))
        c = self._tool(eng, "--check", extra=extra)
        self.assertEqual(c.returncode, 0, f"{why}: `--check` exit {c.returncode}, the figures "
                         f"{self.py}/{self.sh} of the index read as stale or refused:\n"
                         f"{c.stdout}{c.stderr[-3000:]}")
        w = self._tool(eng, extra=extra)
        self.assertEqual(w.returncode, 0, f"{why}: the tool exited {w.returncode}:\n"
                         f"{w.stdout}{w.stderr[-3000:]}")
        self.assertEqual(_read(os.path.join(eng, "ASSURANCE.md")), before,
                         f"{why}: the tool rewrote ASSURANCE.md to {_figures(eng)} "
                         f"(the index gives {self.py}/{self.sh}):\n{w.stdout}{w.stderr}")

    def _assert_neither_moves(self, eng, why, extra=None):
        self._assert_tool_leaves_figures(eng, why, extra)
        self._assert_gate_passes(eng, why, extra)

    def _assert_both_follow_the_index(self, eng, py, sh, why):
        """The tool writes py/sh; with that figure staged the gate passes in the dirty tree and in
        a clone of the commit made from this index; the old figure fails the gate (control)."""
        w = self._tool(eng)
        self.assertEqual(w.returncode, 0, f"{why}: the tool exited {w.returncode}:\n"
                         f"{w.stdout}{w.stderr[-3000:]}")
        self.assertEqual(_figures(eng), (py, sh),
                         f"{why}: the tool wrote {_figures(eng)}, the index gives {(py, sh)}")
        _git(eng, "add", "ASSURANCE.md")
        self._assert_gate_passes(eng, f"{why} (dirty tree, figure {py}/{sh} staged)")
        _git(eng, "commit", "-q", "-m", "the commit about to be made")
        clean = os.path.join(self._tmp, f"clone{self._n}")
        _git(self._tmp, "clone", "-q", eng, clean)
        self.assertEqual(_figures(clean), (py, sh))
        self._assert_gate_passes(clean, f"{why} (clean clone of the commit)")
        # Staged as well as written: the figure the gate reads is the commit's, so an unstaged
        # edit to ASSURANCE.md is no control.
        _set_figures(clean, self.py, self.sh)
        _git(clean, "add", "ASSURANCE.md")
        if (py, sh) != (self.py, self.sh):
            self._assert_gate_fails(clean, f"{why}, control: the pre-change figure in the clone")

    # -- the baseline ----------------------------------------------------------------------

    def test_baseline_clean_copy_is_green_for_both(self):
        self._assert_neither_moves(self._copy(), "clean copy")

    # -- the working tree differs, the index does not: neither count moves ------------------

    def test_unstaged_added_test_function_in_a_tracked_file_moves_neither(self):
        eng = self._copy()
        _append(os.path.join(eng, VICTIM), ADDED_FN)
        self._assert_neither_moves(eng, "unstaged added test function in a tracked tests/py file")

    def test_unstaged_removal_of_a_tracked_files_tests_moves_neither(self):
        eng = self._copy()
        _write(os.path.join(eng, VICTIM), "# emptied in the working tree only\n")
        self._assert_neither_moves(eng, "unstaged removal of every test in a tracked tests/py file")

    def test_unstaged_breakage_of_a_tracked_test_file_is_not_a_refusal(self):
        eng = self._copy()
        _append(os.path.join(eng, VICTIM), "\ndef (\n")
        self._assert_neither_moves(eng, "unstaged syntax error in a tracked tests/py file")

    def test_tracked_test_file_deleted_from_the_working_tree_only_moves_neither(self):
        eng = self._copy()
        os.remove(os.path.join(eng, VICTIM))
        self._assert_neither_moves(eng, "tracked tests/py file deleted from the working tree only")

    def test_tracked_shell_suite_deleted_from_the_working_tree_only_moves_neither(self):
        eng = self._copy()
        os.remove(os.path.join(eng, VICTIM_SUITE))
        self._assert_neither_moves(eng, "tracked tests/test-*.sh deleted from the working tree only")

    def test_untracked_collection_config_moves_neither(self):
        eng = self._copy()
        _write(os.path.join(eng, "tests", "py", "pytest.ini"), NARROWING_INI)
        st = _git(eng, "status", "--porcelain", "--untracked-files=all", "--", "tests/py/pytest.ini")
        assert st.startswith("??"), f"probe precondition: not untracked: {st!r}"
        self._assert_neither_moves(eng, "untracked tests/py/pytest.ini narrowing collection")

    def test_ignored_collection_config_moves_neither(self):
        eng = self._copy()
        _write(os.path.join(eng, "tests", "py", "pytest.ini"), NARROWING_INI)
        _append(os.path.join(eng, ".git", "info", "exclude"), "\n/tests/py/pytest.ini\n")
        st = _git(eng, "status", "--porcelain", "--ignored", "--", "tests/py/pytest.ini")
        assert st.startswith("!!"), f"probe precondition: not ignored: {st!r}"
        self._assert_neither_moves(eng, "ignored tests/py/pytest.ini narrowing collection")

    def test_untracked_test_file_and_suite_move_neither(self):
        eng = self._copy()
        _write(os.path.join(eng, "tests", "py", "test_zz_cert_untracked.py"), PROBE_TESTS)
        _write(os.path.join(eng, "tests", "test-zz-cert-untracked.sh"), PROBE_SUITE)
        self._assert_neither_moves(eng, "untracked tests/py test file and tests/test-*.sh")

    # -- the index differs: both counts follow it ------------------------------------------

    def test_staged_new_test_file_counts_in_both(self):
        eng = self._copy()
        _write(os.path.join(eng, "tests", "py", "test_zz_cert_staged.py"), PROBE_TESTS)
        _git(eng, "add", "tests/py/test_zz_cert_staged.py")
        self._assert_both_follow_the_index(eng, self.py + 3, self.sh, "staged new tests/py file")

    def test_staged_test_function_reverted_in_the_working_tree_counts_in_both(self):
        eng = self._copy()
        path = os.path.join(eng, VICTIM)
        original = _read(path)
        _append(path, ADDED_FN)
        _git(eng, "add", VICTIM)
        with open(path, "wb") as f:
            f.write(original)
        st = _git(eng, "status", "--porcelain", "--", VICTIM)
        assert st.startswith("MM"), f"probe precondition: expected staged+unstaged: {st!r}"
        self._assert_both_follow_the_index(eng, self.py + 1, self.sh,
                                           "staged added test function, reverted in the working tree")

    def test_staged_new_suite_deleted_from_the_working_tree_counts_in_both(self):
        eng = self._copy()
        rel = os.path.join("tests", "test-zz-cert-staged.sh")
        _write(os.path.join(eng, rel), PROBE_SUITE)
        _git(eng, "add", rel)
        os.remove(os.path.join(eng, rel))
        st = _git(eng, "status", "--porcelain", "--", rel)
        assert st.startswith("AD"), f"probe precondition: expected staged, deleted in tree: {st!r}"
        self._assert_both_follow_the_index(eng, self.py, self.sh + 1,
                                           "staged new tests/test-*.sh, deleted in the working tree")

    # -- a hook's environment cannot name another index ------------------------------------

    def test_git_index_file_naming_another_index_moves_neither(self):
        eng = self._copy()
        other = os.path.join(eng, ".git", "zz-cert-other-index")
        p = _run(["git", "read-tree", "--empty"], eng, {"GIT_INDEX_FILE": other})
        assert p.returncode == 0 and os.path.exists(other), f"probe precondition:\n{p.stderr}"
        self._assert_neither_moves(eng, "GIT_INDEX_FILE naming an empty index",
                                   extra={"GIT_INDEX_FILE": other})

    def test_git_dir_naming_another_repository_moves_neither(self):
        eng = self._copy()
        other = os.path.join(self._tmp, f"other{self._n}")
        os.makedirs(other)
        _git(other, "init", "-q")
        _write(os.path.join(other, "README"), "another repository\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-q", "-m", "other")
        self._assert_neither_moves(eng, "GIT_DIR naming another repository",
                                   extra={"GIT_DIR": os.path.join(other, ".git")})


if __name__ == "__main__":
    unittest.main()
