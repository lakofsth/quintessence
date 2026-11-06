# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Certify: `_treefiles.walk_files` names exactly the files git names from the tree's own
`.gitignore` files at every depth -- tracked files on disk plus untracked ones that
`git ls-files --others --exclude-per-directory=.gitignore` lists -- whatever ignore file a tool
has written below the top.

Written from the sentence alone. Each case builds a scratch git repository, writes ignore files
into it, and compares the walk with git's own answer for that tree. The directions the sentence
has, one test each:

  * a nested ignore file a TOOL writes (`.pytest_cache/.gitignore` of `*`) drops its directory;
  * a nested ignore file's patterns apply below it, at any depth (depth 1 and depth 3);
  * a nested directory pattern (`build/`) drops directories below its file only;
  * a nested ignore file drops NOTHING outside its own directory (anchored `/x`, and a sibling);
  * a nested negation re-includes what a higher file ignored, as git does;
  * a tracked file an ignore file covers is the one thing a filesystem walk cannot know: the walk
    drops it, and `no-tracked-file-is-covered-by-an-ignore-file` is what keeps the tree free of
    one (`_treefiles.tracked_under_ignore` names it; scoped out of the walk's sentence, 6 Nov 2026);
  * the engine tree itself, in a clone, after a tool has written a nested ignore file.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _treefiles  # noqa: E402

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GIT_ENV = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="c", GIT_AUTHOR_EMAIL="c@x", GIT_COMMITTER_NAME="c",
               GIT_COMMITTER_EMAIL="c@x")


def _git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True,
                          env=GIT_ENV).stdout


def _git_names(repo):
    """What the sentence says git names: tracked files on disk plus untracked ones that
    `git ls-files --others --exclude-per-directory=.gitignore` lists."""
    tracked = [p for p in _git(repo, "ls-files", "-z").split("\0") if p]
    on_disk = {p for p in tracked if os.path.isfile(os.path.join(repo, p))}
    others = {p for p in _git(repo, "ls-files", "--others", "--exclude-per-directory=.gitignore",
                              "-z").split("\0") if p}
    return on_disk | others


def _write(root, files):
    for rel, body in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(body)


class NestedGitignore(unittest.TestCase):
    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git not on PATH: there is no git answer to compare the walk with")
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, True)
        _git(self.repo, "init", "-q")

    def agree(self, files, expect_in=(), expect_out=(), track=()):
        _write(self.repo, files)
        if track:
            _git(self.repo, "add", "-f", "--", *track)
        want = _git_names(self.repo)
        # Guard the fixture itself: git must say what the case is built to say, or the
        # comparison below is about a different tree than the one described.
        for p in expect_in:
            self.assertIn(p, want, "fixture: git does not name %s" % p)
        for p in expect_out:
            self.assertNotIn(p, want, "fixture: git names %s" % p)
        got = set(_treefiles.walk_files(self.repo))
        self.assertEqual(sorted(got - want), [], "the walk names files git does not")
        self.assertEqual(sorted(want - got), [], "git names files the walk does not")

    def test_a_tool_written_star_ignore_drops_its_directory(self):
        self.agree({".gitignore": "*.log\n", "keep.py": "x\n",
                    ".pytest_cache/.gitignore": "# Created by pytest automatically.\n*\n",
                    ".pytest_cache/CACHEDIR.TAG": "x\n", ".pytest_cache/README.md": "x\n",
                    ".pytest_cache/v/cache/nodeids": "[]\n"},
                   expect_in=["keep.py", ".gitignore"],
                   expect_out=[".pytest_cache/README.md", ".pytest_cache/.gitignore"])

    def test_a_nested_pattern_applies_below_its_file(self):
        self.agree({"top.tmp": "x\n", "pkg/.gitignore": "*.tmp\n", "pkg/a.tmp": "x\n",
                    "pkg/a.py": "x\n", "pkg/sub/b.tmp": "x\n", "pkg/sub/b.py": "x\n"},
                   expect_in=["top.tmp", "pkg/a.py", "pkg/sub/b.py", "pkg/.gitignore"],
                   expect_out=["pkg/a.tmp", "pkg/sub/b.tmp"])

    def test_an_ignore_file_three_deep_is_read(self):
        self.agree({"a/b/c/.gitignore": "secret\n", "a/b/c/secret": "x\n", "a/b/c/kept": "x\n",
                    "a/b/secret": "x\n", "secret": "x\n"},
                   expect_in=["a/b/c/kept", "a/b/secret", "secret"],
                   expect_out=["a/b/c/secret"])

    def test_a_nested_directory_pattern_drops_directories_below_it_only(self):
        self.agree({"pkg/.gitignore": "build/\n", "pkg/build/out.o": "x\n",
                    "pkg/sub/build/out.o": "x\n", "build/out.o": "x\n", "pkg/keep.c": "x\n"},
                   expect_in=["build/out.o", "pkg/keep.c"],
                   expect_out=["pkg/build/out.o", "pkg/sub/build/out.o"])

    def test_a_nested_ignore_file_drops_nothing_outside_its_directory(self):
        self.agree({"pkg/.gitignore": "/only.txt\nnotes.md\n", "pkg/only.txt": "x\n",
                    "pkg/sub/only.txt": "x\n", "only.txt": "x\n", "notes.md": "x\n",
                    "pkg/notes.md": "x\n", "other/notes.md": "x\n"},
                   expect_in=["pkg/sub/only.txt", "only.txt", "notes.md", "other/notes.md"],
                   expect_out=["pkg/only.txt", "pkg/notes.md"])

    def test_a_nested_negation_re_includes_what_a_higher_file_ignored(self):
        self.agree({".gitignore": "*.log\n", "a.log": "x\n", "pkg/.gitignore": "!keep.log\n",
                    "pkg/keep.log": "x\n", "pkg/drop.log": "x\n"},
                   expect_in=["pkg/keep.log"], expect_out=["a.log", "pkg/drop.log"])

    def test_a_tracked_file_under_an_ignore_is_dropped_by_the_walk_and_named_by_the_pin(self):
        _write(self.repo, {"gen/.gitignore": "*\n", "gen/pinned.txt": "x\n", "gen/scratch.txt": "x\n"})
        _git(self.repo, "add", "-f", "--", "gen/pinned.txt")
        self.assertIn("gen/pinned.txt", _git_names(self.repo), "fixture: git does not name it")
        self.assertNotIn("gen/pinned.txt", _treefiles.walk_files(self.repo),
                         "the walk named a file only git knows is tracked")
        self.assertEqual(_treefiles.tracked_under_ignore(self.repo), ["gen/pinned.txt"],
                         "the pin's instrument does not name the force-added file")


class EngineTreeWithAToolCache(unittest.TestCase):
    def test_the_engine_clone_agrees_with_git_after_a_tool_writes_below_the_top(self):
        if shutil.which("git") is None or not _treefiles._is_checkout(ENGINE):
            self.skipTest("not a git checkout: there is no git listing to compare the walk with")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        clone = os.path.join(d, "engine")
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", ENGINE, clone], check=True,
                       capture_output=True, env=GIT_ENV)
        _write(clone, {".pytest_cache/.gitignore": "# Created by pytest automatically.\n*\n",
                       ".pytest_cache/v/cache/lastfailed": "{}\n",
                       "tests/py/.mypy_cache/.gitignore": "*\n",
                       "tests/py/.mypy_cache/3.13/x.json": "{}\n"})
        want = _git_names(clone)
        self.assertGreater(len(want), 50, "git listed almost nothing: the comparison is vacuous")
        got = set(_treefiles.walk_files(clone))
        self.assertEqual(sorted(got - want), [], "the walk names files git does not")
        self.assertEqual(sorted(want - got), [], "git names files the walk does not")


if __name__ == "__main__":
    unittest.main()
