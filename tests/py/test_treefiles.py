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


class TreeFiles(unittest.TestCase):
    def test_the_walk_names_what_git_does_not_ignore(self):
        if not _treefiles._is_checkout(ENGINE):
            self.skipTest("not a git checkout: there is no git listing to compare the walk with")

        def git(*a):
            return [p for p in subprocess.run(["git", "-C", ENGINE, *a, "-z"], capture_output=True,
                                              text=True, check=True).stdout.split("\0") if p]
        on_disk = {p for p in git("ls-files") if os.path.isfile(os.path.join(ENGINE, p))}
        expected = on_disk | set(git("ls-files", "--others", "--exclude-standard"))
        self.assertGreater(len(expected), 50, "git listed almost nothing: the comparison is vacuous")
        self.assertEqual(set(_treefiles.walk_files(ENGINE)), expected)

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
