# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""The count tool and the count gate count the index, not the working tree.

tests/INVARIANTS.md, `the-count-tool-writes-what-the-gate-reads-or-nothing`, the index clause:
collection runs over a temporary checkout of the index — what a clean checkout of the commit
holds, or, before a commit, what the commit being made will hold — so an unstaged edit to a
tracked test file, a tracked file deleted only from the working tree, and an untracked or
ignored file change neither count; and the git calls that read the index run with GIT_*
stripped, so a hook's GIT_INDEX_FILE cannot name some other index.

Same shape as test_count_gate_clean_checkout.py: each case builds a throwaway engine — the
tracked files of this one, committed into a fresh repository, ASSURANCE.md synced by the tool
and committed — perturbs it the one way the case names, and asks the copy's OWN tool and the
copy's OWN gate what they make of it. Nothing here imports either of them.

What each of the two counts, case by case:
  - an unstaged test function added to a tracked file: neither counts it; the tool names the
    file on stderr. The figure it writes there is green in a fresh clone of the commit.
  - a tracked test file and a tracked suite deleted from the working tree only: both still
    count them (the index holds them).
  - a staged-but-uncommitted new test file: both count it — it is in the commit being made —
    and the figure the tool writes is green in a fresh clone once committed.
  - a committed .gitignore rule: an ignored, untracked test file or suite is counted by
    neither; a file force-added past the same rule is tracked and counted by both.
  - GIT_INDEX_FILE in the environment naming another index (one that tracks a file the
    engine's own index does not): both count the engine's own index.
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
GATE = os.path.join("tests", "py", "test_assurance_counts.py") + "::TestAssuranceCountsMatchReality"
PY_ROW = re.compile(r"(\d+) tests? over the engine modules")
SH_ROW = re.compile(r"(\d+) end-to-end suites?")

PROBE_TESTS = ("def test_probe_one():\n    pass\n\n\ndef test_probe_two():\n    pass\n\n\n"
               "def test_probe_three():\n    pass\n")
PROBE_SUITE = "#!/usr/bin/env bash\nexit 0\n"
EXTRA_FUNCTION = "\n\ndef test_zz_index_unstaged_addition():\n    pass\n"

_GIT = ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]


def _env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and k not in ("PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


def _run(argv, cwd, **extra):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=_env(**extra))


def _git(cwd, *args, **extra):
    p = _run([*_GIT, *args], cwd, **extra)
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
    with open(path) as f:
        return f.read()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _append(path, text):
    with open(path, "a") as f:
        f.write(text)


def _figures(eng):
    text = _read(os.path.join(eng, "ASSURANCE.md"))
    return int(PY_ROW.search(text).group(1)), int(SH_ROW.search(text).group(1))


def _set_figures(eng, py, sh):
    path = os.path.join(eng, "ASSURANCE.md")
    text = _read(path)
    m = PY_ROW.search(text)
    text = text[:m.start(1)] + str(py) + text[m.end(1):]
    m = SH_ROW.search(text)
    text = text[:m.start(1)] + str(sh) + text[m.end(1):]
    _write(path, text)


@unittest.skipIf(importlib.util.find_spec("pytest") is None,
                 "pytest not installed — the count is pytest's collection count")
class TestTheCountIsTheIndex(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="count-gate-index-")
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
        cls.base_figures = _figures(base)
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

    def _clone(self, eng):
        type(self)._n += 1
        d = os.path.join(self._tmp, f"clone{self._n}")
        _git(self._tmp, "clone", "-q", eng, d)
        return d

    def _gate(self, eng, **extra):
        return _run([sys.executable, "-m", "pytest", GATE, "-q", "-p", "no:cacheprovider"], eng, **extra)

    def _tool(self, eng, *args, **extra):
        return _run([sys.executable, TOOL, *args], eng, **extra)

    def _assert_gate_passes(self, eng, why, **extra):
        p = self._gate(eng, **extra)
        self.assertEqual(p.returncode, 0, f"{why}: the gate failed:\n{(p.stdout + p.stderr)[-3000:]}")

    def _assert_gate_fails(self, eng, why):
        p = self._gate(eng)
        self.assertNotEqual(p.returncode, 0, f"{why}: the gate PASSED:\n{(p.stdout + p.stderr)[-3000:]}")

    def _assert_tool_leaves_figures(self, eng, why, **extra):
        before = _read(os.path.join(eng, "ASSURANCE.md"))
        c = self._tool(eng, "--check", **extra)
        self.assertEqual(c.returncode, 0, f"{why}: `--check` says the figures are stale "
                         f"(exit {c.returncode}):\n{c.stdout}{c.stderr}")
        w = self._tool(eng, **extra)
        self.assertEqual(w.returncode, 0, f"{why}: the tool exited {w.returncode}:\n{w.stdout}{w.stderr}")
        self.assertEqual(_read(os.path.join(eng, "ASSURANCE.md")), before,
                         f"{why}: the tool rewrote ASSURANCE.md:\n{w.stdout}{w.stderr}")
        return w

    def _status(self, eng, rel):
        return _git(eng, "status", "--porcelain", "--ignored", "--untracked-files=all", "--", rel)

    def _with_unstaged_function(self):
        eng = self._copy()
        rel = os.path.join("tests", "py", "test_atomicio.py")
        _append(os.path.join(eng, rel), EXTRA_FUNCTION)
        assert self._status(eng, rel).startswith(" M"), "probe precondition: the edit is not unstaged"
        return eng, rel

    # -- an unstaged edit to a tracked test file -------------------------------------------

    def test_an_unstaged_test_function_changes_neither_count(self):
        eng, rel = self._with_unstaged_function()
        w = self._assert_tool_leaves_figures(eng, "an unstaged test function in a tracked file")
        self.assertIn(rel, w.stderr, "the tool did not name the file whose edit it left out")
        self._assert_gate_passes(eng, "an unstaged test function in a tracked file")

    def test_what_the_tool_writes_beside_an_unstaged_edit_is_green_in_a_clean_checkout(self):
        eng, _ = self._with_unstaged_function()
        py, sh = self.base_figures
        _set_figures(eng, py + 50, sh + 50)
        _git(eng, "commit", "-q", "-m", "stale figures", "--", "ASSURANCE.md")
        w = self._tool(eng)
        self.assertEqual(w.returncode, 0, w.stderr)
        _git(eng, "commit", "-q", "-m", "count", "--", "ASSURANCE.md")
        clean = self._clone(eng)
        self._assert_gate_passes(clean, "the tool's figure, in a fresh clone of the commit")
        # Control: the working tree's figure is one the same clean checkout rejects.
        _set_figures(clean, py + 1, sh)
        _git(clean, "add", "ASSURANCE.md")
        self._assert_gate_fails(clean, "control: the clean checkout cannot fail")

    # -- a tracked file deleted from the working tree only ---------------------------------

    def test_a_tracked_test_and_suite_deleted_only_from_the_working_tree_are_still_counted(self):
        eng = self._copy()
        py_rel = os.path.join("tests", "py", "test_atomicio.py")
        suites = sorted(f for f in os.listdir(os.path.join(eng, "tests"))
                        if f.startswith("test-") and f.endswith(".sh"))
        sh_rel = os.path.join("tests", suites[0])
        os.remove(os.path.join(eng, py_rel))
        os.remove(os.path.join(eng, sh_rel))
        for rel in (py_rel, sh_rel):
            assert self._status(eng, rel).startswith(" D"), f"probe precondition: {rel} not deleted"
        self._assert_tool_leaves_figures(eng, "tracked files deleted from the working tree")
        self._assert_gate_passes(eng, "tracked files deleted from the working tree")

    # -- a staged but uncommitted new test file --------------------------------------------

    def test_a_staged_test_file_is_counted_by_both(self):
        eng = self._copy()
        rel = os.path.join("tests", "py", "test_zz_index_staged.py")
        _write(os.path.join(eng, rel), PROBE_TESTS)
        _git(eng, "add", "--", rel)
        assert self._status(eng, rel).startswith("A "), "probe precondition: not staged"
        py, sh = self.base_figures
        # The gate counts it: the base figure is stale for the commit being made.
        self._assert_gate_fails(eng, "a staged test file the figure does not count")
        w = self._tool(eng)
        self.assertEqual(w.returncode, 0, w.stderr)
        self.assertEqual(_figures(eng), (py + 3, sh), "the tool did not count the staged file")
        # The gate reads the index's ASSURANCE.md: the figure counts once it is staged too.
        self._assert_gate_fails(eng, "the tool's figure, written and not staged")
        _git(eng, "add", "ASSURANCE.md")
        self._assert_gate_passes(eng, "the staged file with the tool's figure")
        _git(eng, "commit", "-q", "-m", "staged test and its count")
        self._assert_gate_passes(self._clone(eng), "the committed figure, in a fresh clone")

    # -- entries the index holds that ls-files and checkout-index read differently ----------

    def _assert_refused_untouched(self, eng, rel, why):
        before = _read(os.path.join(eng, "ASSURANCE.md"))
        for args in ((), ("--check",)):
            w = self._tool(eng, *args)
            self.assertEqual(w.returncode, 2, f"{why}: exit {w.returncode}:\n{w.stdout}{w.stderr}")
            self.assertIn(rel, w.stderr, f"{why}: the refusal does not name the file")
            self.assertNotIn("counted as staged", w.stderr)
        self.assertEqual(_read(os.path.join(eng, "ASSURANCE.md")), before,
                         f"{why}: ASSURANCE.md was rewritten")
        self._assert_gate_fails(eng, why)

    def test_an_intent_to_add_test_file_and_suite_are_refused_not_counted(self):
        eng = self._copy()
        rel = os.path.join("tests", "py", "test_zz_index_ita.py")
        suite = os.path.join("tests", "test-zz-index-ita.sh")
        _write(os.path.join(eng, rel), PROBE_TESTS)
        _write(os.path.join(eng, suite), PROBE_SUITE)
        _git(eng, "add", "-N", "--", rel, suite)
        self._assert_refused_untouched(eng, rel, "an intent-to-add test file")
        self.assertIn(suite, self._tool(eng).stderr)

    def test_an_unmerged_test_file_is_refused_not_counted(self):
        eng = self._copy()
        rel = os.path.join("tests", "py", "test_atomicio.py")
        _git(eng, "checkout", "-q", "-b", "side")
        _append(os.path.join(eng, rel), "\n\ndef test_zz_side():\n    pass\n")
        _git(eng, "commit", "-q", "-am", "side")
        _git(eng, "checkout", "-q", "-")
        _append(os.path.join(eng, rel), "\n\ndef test_zz_main():\n    pass\n")
        _git(eng, "commit", "-q", "-am", "main")
        p = _run([*_GIT, "merge", "side"], eng)
        assert p.returncode != 0 and _git(eng, "ls-files", "-u"), "probe precondition: no conflict"
        self._assert_refused_untouched(eng, rel, "an unmerged test file")
        _write(os.path.join(eng, rel), PROBE_TESTS)
        _git(eng, "add", "--", rel)
        self.assertEqual(self._tool(eng).returncode, 0, "resolved and staged, the tool still refuses")

    # -- a rule in a committed .gitignore --------------------------------------------------

    def test_a_committed_gitignore_rule_is_honoured_and_a_forced_file_still_counts(self):
        eng = self._copy()
        _append(os.path.join(eng, ".gitignore"), "\ntest_zz_ignored*.py\ntest-zz-ignored*.sh\n")
        _git(eng, "add", ".gitignore")
        _git(eng, "commit", "-q", "-m", "ignore rule")
        ignored = [os.path.join("tests", "py", "test_zz_ignored.py"),
                   os.path.join("tests", "test-zz-ignored.sh")]
        _write(os.path.join(eng, ignored[0]), PROBE_TESTS)
        _write(os.path.join(eng, ignored[1]), PROBE_SUITE)
        for rel in ignored:
            assert self._status(eng, rel).startswith("!!"), f"probe precondition: {rel} not ignored"
        self._assert_tool_leaves_figures(eng, "files a committed .gitignore ignores")
        self._assert_gate_passes(eng, "files a committed .gitignore ignores")
        # Force-added past the rule, they are tracked, and both count them.
        forced = [os.path.join("tests", "py", "test_zz_ignored_forced.py"),
                  os.path.join("tests", "test-zz-ignored-forced.sh")]
        _write(os.path.join(eng, forced[0]), PROBE_TESTS)
        _write(os.path.join(eng, forced[1]), PROBE_SUITE)
        _git(eng, "add", "-f", "--", *forced)
        py, sh = self.base_figures
        w = self._tool(eng)
        self.assertEqual(w.returncode, 0, w.stderr)
        self.assertEqual(_figures(eng), (py + 3, sh + 1), "a force-added file was not counted")
        _git(eng, "add", "ASSURANCE.md")
        self._assert_gate_passes(eng, "force-added files with the tool's figure")

    # -- GIT_INDEX_FILE naming another index -----------------------------------------------

    def test_another_index_named_in_the_environment_is_not_read(self):
        eng = self._copy()
        rel = [os.path.join("tests", "py", "test_zz_other_index.py"),
               os.path.join("tests", "test-zz-other-index.sh")]
        _write(os.path.join(eng, rel[0]), PROBE_TESTS)
        _write(os.path.join(eng, rel[1]), PROBE_SUITE)
        other = os.path.join(self._tmp, f"other-index-{self._n}")
        # A second index that tracks the two files; the engine's own index does not.
        _git(eng, "read-tree", "HEAD", GIT_INDEX_FILE=other)
        _git(eng, "add", "--", *rel, GIT_INDEX_FILE=other)
        listed = _git(eng, "ls-files", "--", *rel, GIT_INDEX_FILE=other)
        assert len(listed.split()) == 2, "probe precondition: the other index lacks the files"
        for r in rel:
            assert self._status(eng, r).startswith("??"), f"probe precondition: {r} is tracked"
        self._assert_tool_leaves_figures(eng, "GIT_INDEX_FILE naming another index",
                                         GIT_INDEX_FILE=other)
        self._assert_gate_passes(eng, "GIT_INDEX_FILE naming another index", GIT_INDEX_FILE=other)


if __name__ == "__main__":
    unittest.main()
