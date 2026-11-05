# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""The count tool finds an intent-to-add entry by reading the index, whatever the working tree
holds for it.

tests/INVARIANTS.md, the unresolved-index clause, the index-read sentence: an intent-to-add entry
is what `ls-files` lists under tests/ and a tree written from the same index does not record, so
it is refused whatever the working tree holds for it — one a rename pairs with a deleted tracked
file and one whose working-tree copy is deleted included.

Written by a certifier from the sentence alone. Each case builds a small throwaway engine (an
ASSURANCE.md holding both figures, two test files, one suite, committed and synced), adds ONE
intent-to-add entry under tests/ with the working tree holding the thing the case names, and runs
THIS checkout's tool against it with `--engine`, in both modes. The tool must exit 2, say
REFUSED, name the entry, and leave ASSURANCE.md untouched.

The working-tree states, one per way the sentence can be false:
  - a new file with content (the plain case);
  - an empty working-tree file;
  - the working-tree copy deleted after `git add -N`;
  - a tracked test file moved in the working tree to a new name added with `-N` (the tracked
    file is still in the index, so the working-tree view pairs the two as a rename);
  - the same, with the tracked file's deletion staged;
  - the rename and the deletion cases for a tests/test-*.sh suite as well as a tests/py file.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(ENGINE, "tools", "sync_assurance_counts.py")

_GIT = ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]

TWO_TESTS = "def test_one():\n    pass\n\n\ndef test_two():\n    pass\n"
ONE_TEST = "def test_three():\n    pass\n"
SUITE = "#!/usr/bin/env bash\nexit 0\n"
DOC = ("# Assurance\n\n- 0 tests over the engine modules\n- 0 end-to-end suites\n")


def _env():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and k not in ("PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _git(cwd, *args):
    p = subprocess.run([*_GIT, *args], cwd=cwd, capture_output=True, text=True, env=_env())
    if p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{p.stdout}{p.stderr}")
    return p.stdout


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _read(path):
    with open(path) as f:
        return f.read()


@unittest.skipIf(importlib.util.find_spec("pytest") is None,
                 "pytest not installed — the count is pytest's collection count")
class TestIntentToAddIsReadFromTheIndex(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="count-tool-ita-")
        eng = self.eng = os.path.join(self.tmp, "engine")
        _write(os.path.join(eng, "ASSURANCE.md"), DOC)
        _write(os.path.join(eng, "tests", "py", "test_alpha.py"), TWO_TESTS)
        _write(os.path.join(eng, "tests", "py", "test_beta.py"), ONE_TEST)
        _write(os.path.join(eng, "tests", "test-gamma.sh"), SUITE)
        _git(eng, "init", "-q")
        _git(eng, "add", "-A")
        _git(eng, "commit", "-q", "-m", "base")
        p = self._tool()
        self.assertEqual(p.returncode, 0, f"baseline sync failed:\n{p.stdout}{p.stderr}")
        doc = _read(os.path.join(eng, "ASSURANCE.md"))
        self.assertIn("3 tests over the engine modules", doc)
        self.assertIn("1 end-to-end suites", doc)
        _git(eng, "add", "-A")
        _git(eng, "commit", "-q", "-m", "sync")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tool(self, *args):
        return subprocess.run([sys.executable, TOOL, "--engine", self.eng, *args],
                              capture_output=True, text=True, env=_env(), cwd=self.tmp)

    def _p(self, rel):
        return os.path.join(self.eng, rel)

    def _assert_refused(self, entry):
        listed = _git(self.eng, "ls-files", "--", entry).split()
        self.assertIn(entry, listed, "fixture: the entry is not in the index")
        tree = _git(self.eng, "write-tree").strip()
        self.assertNotIn(entry, _git(self.eng, "ls-tree", "-r", "--name-only", tree).split(),
                         "fixture: the entry is not intent-to-add (a written tree records it)")
        doc_before = _read(self._p("ASSURANCE.md"))
        for mode in ((), ("--check",)):
            p = self._tool(*mode)
            said = f"tool {' '.join(mode) or '(write)'}: exit {p.returncode}\n{p.stdout}{p.stderr}"
            self.assertEqual(p.returncode, 2, "not refused — " + said)
            self.assertIn("REFUSED", p.stderr, said)
            self.assertIn(entry, p.stderr, "refusal does not name the entry — " + said)
            self.assertEqual(_read(self._p("ASSURANCE.md")), doc_before,
                             "ASSURANCE.md was written — " + said)

    # --- the working tree holds a new file, with content or empty ---

    def test_new_file_with_content(self):
        rel = "tests/py/test_new.py"
        _write(self._p(rel), ONE_TEST)
        _git(self.eng, "add", "-N", rel)
        self._assert_refused(rel)

    def test_new_file_empty_in_working_tree(self):
        rel = "tests/py/test_empty.py"
        _write(self._p(rel), "")
        _git(self.eng, "add", "-N", rel)
        self._assert_refused(rel)

    # --- the working-tree copy is deleted ---

    def test_working_tree_copy_deleted_python(self):
        rel = "tests/py/test_gone.py"
        _write(self._p(rel), ONE_TEST)
        _git(self.eng, "add", "-N", rel)
        os.remove(self._p(rel))
        self._assert_refused(rel)

    def test_working_tree_copy_deleted_suite(self):
        rel = "tests/test-gone.sh"
        _write(self._p(rel), SUITE)
        _git(self.eng, "add", "-N", rel)
        os.remove(self._p(rel))
        self._assert_refused(rel)

    # --- a rename pairs it with a deleted tracked file ---

    def test_rename_from_tracked_file_deleted_in_working_tree_python(self):
        rel = "tests/py/test_alpha_renamed.py"
        os.rename(self._p("tests/py/test_alpha.py"), self._p(rel))
        _git(self.eng, "add", "-N", rel)
        self._assert_refused(rel)

    def test_rename_from_tracked_file_deleted_in_working_tree_suite(self):
        rel = "tests/test-gamma-renamed.sh"
        os.rename(self._p("tests/test-gamma.sh"), self._p(rel))
        _git(self.eng, "add", "-N", rel)
        self._assert_refused(rel)

    def test_rename_from_tracked_file_deletion_staged(self):
        rel = "tests/py/test_beta_renamed.py"
        _git(self.eng, "rm", "-q", "--cached", "tests/py/test_beta.py")
        os.rename(self._p("tests/py/test_beta.py"), self._p(rel))
        _git(self.eng, "add", "-N", rel)
        self._assert_refused(rel)


if __name__ == "__main__":
    unittest.main()
