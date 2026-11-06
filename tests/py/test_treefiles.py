# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""_treefiles: the tree listing the derivation tests share names the same files with and without
`.git`.

The tests that derive sites from the tree must run from an export or a tarball (no `.git`) and
see what they would see in a checkout. Inside a checkout the walk is compared with git itself;
in a plain copy there is no git to compare with, so the ignore filter is checked on a fixture.
"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _treefiles  # noqa: E402

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _git_listing(root):
    """What git names in `root` from the tree's own ignore rules: the tracked files on disk and
    the untracked ones no `.gitignore` of the tree excludes, at any depth. Like with like with the
    walk, which has only the tree to read: `--exclude-standard` would add `.git/info/exclude` and
    the user's global excludes, which an export does not carry."""
    def git(*a):
        return [p for p in subprocess.run(["git", "-C", root, *a, "-z"], capture_output=True,
                                          text=True, check=True).stdout.split("\0") if p]
    on_disk = {p for p in git("ls-files") if os.path.isfile(os.path.join(root, p))}
    return on_disk | set(git("ls-files", "--others", "--exclude-per-directory=.gitignore"))


def _w(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class TreeFiles(unittest.TestCase):
    def test_the_walk_names_what_git_does_not_ignore(self):
        if not _treefiles._is_checkout(ENGINE):
            self.skipTest("not a git checkout: there is no git listing to compare the walk with")
        self.assertGreater(len(_git_listing(ENGINE)), 50,
                           "git listed almost nothing: the comparison is vacuous")
        self.assertEqual(set(_treefiles.walk_files(ENGINE)), _git_listing(ENGINE))

    def test_the_walk_honours_every_gitignore_the_tree_carries(self):
        # A checkout a tool has written into: pytest's `.pytest_cache/.gitignore` and `uv venv`'s
        # `.venv/.gitignore` are each `*`, which git honours below their own directory. The walk
        # read only the top-level file, so after one pytest run it listed the cache (red at
        # 7c39c67, fix-review 20261105T123503Z). The fixture also carries the rest of the syntax
        # a nested file can use, each in a file below the top: an anchored pattern, one with an
        # inner slash, `**`, a directory-only rule, a negation and an escaped `#`.
        with tempfile.TemporaryDirectory() as d:
            _w(d, ".gitignore", "*.log\n__pycache__/\n")
            _w(d, "keep.py", "x\n")
            _w(d, ".pytest_cache/.gitignore", "# Created by pytest automatically.\n*\n")
            _w(d, ".pytest_cache/v/cache/nodeids", "[]\n")
            _w(d, ".pytest_cache/README.md", "x\n")
            _w(d, ".venv/.gitignore", "*\n")
            _w(d, ".venv/pyvenv.cfg", "x\n")
            _w(d, ".venv/lib/site-packages/pip/__main__.py", "x\n")
            _w(d, "pkg/.gitignore",
               "/anchored.txt\nsub/inner.txt\ndeep/**/gen.txt\nbuild/\n*.tmp\n!keep.tmp\n\\#hash\n")
            for rel in ("pkg/anchored.txt", "pkg/x/anchored.txt", "pkg/sub/inner.txt",
                        "pkg/x/sub/inner.txt", "pkg/deep/gen.txt", "pkg/deep/a/b/gen.txt",
                        "pkg/build/out.bin", "pkg/x/build/out.bin", "pkg/y/build", "pkg/a.tmp", "pkg/keep.tmp",
                        "pkg/#hash", "pkg/x/#hash", "anchored.txt", "a.tmp", "pkg/run.log"):
                _w(d, rel, "x\n")
            subprocess.run(["git", "init", "-q", d], check=True)
            want = _git_listing(d)
            # Positive control: the fixture is not vacuous, git keeps some and drops some.
            self.assertIn("pkg/keep.tmp", want)
            self.assertIn("pkg/x/anchored.txt", want)
            self.assertIn("pkg/y/build", want)  # a FILE named like the `build/` directory rule
            self.assertNotIn(".pytest_cache/v/cache/nodeids", want)
            self.assertEqual(set(_treefiles.walk_files(d)), want)

    def test_the_tree_listing_names_the_entry_points(self):
        # Positive control for every caller: the listing is not empty and holds known files.
        files = _treefiles.engine_files(ENGINE)
        for known in ("qq", "qq-config.sh", "quintessence/cli.py"):
            self.assertIn(known, files)
        self.assertFalse([p for p in files if "__pycache__" in p or p.startswith(".git/")])

    def test_the_ignore_filter_drops_what_gitignore_names(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".gitignore"), "w") as f:
                f.write("# comment\n*.log\n__pycache__/\n*.pyc\n")
            os.makedirs(os.path.join(d, "pkg", "__pycache__"))
            os.makedirs(os.path.join(d, ".git"))
            # pkg/__pycache__/notes.txt matches no FILE pattern: only the `__pycache__/` directory
            # rule drops it, so this fixture fails if directory patterns stop being applied.
            for rel in ("keep.py", "run.log", "pkg/mod.py", "pkg/__pycache__/mod.cpython-314.pyc",
                        "pkg/__pycache__/notes.txt", "pkg/stray.pyc", ".git/HEAD"):
                with open(os.path.join(d, rel), "w") as f:
                    f.write("x\n")
            self.assertEqual(_treefiles.walk_files(d), [".gitignore", "keep.py", "pkg/mod.py"])


if __name__ == "__main__":
    unittest.main()
