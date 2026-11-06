# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Check of `no-tracked-file-is-covered-by-an-ignore-file` (tests/INVARIANTS.md): no file git
tracks in this tree is covered by one of the tree's own `.gitignore` files. A walk of the
filesystem (`_treefiles.walk_files`, what an export without `.git` runs) cannot know a file is
tracked, so a force-added file under an ignore would leave the export silently; this keeps there
being none.

  1. in this checkout, `_treefiles.tracked_under_ignore` names no file;
  2. the instrument can show a positive: on a scratch repo with a file force-added under a nested
     `*` ignore, it names exactly that file (and not the untracked file beside it).
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
GIT_ENV = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)


class NoTrackedFileUnderIgnore(unittest.TestCase):
    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git not on PATH")

    def test_this_checkout_tracks_no_file_an_ignore_file_covers(self):
        if not _treefiles._is_checkout(ENGINE):
            self.skipTest("not a git checkout (an export): the invariant binds the dev line")
        self.assertEqual(_treefiles.tracked_under_ignore(ENGINE), [],
                         "tracked files an ignore file covers: an export's walk would drop them")

    def test_the_instrument_names_a_force_added_file(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        subprocess.run(["git", "-C", d, "init", "-q"], check=True, env=GIT_ENV)
        os.makedirs(os.path.join(d, "gen"))
        for rel, body in (("gen/.gitignore", "*\n"), ("gen/pinned.txt", "x\n"), ("gen/scratch.txt", "x\n")):
            with open(os.path.join(d, rel), "w") as f:
                f.write(body)
        subprocess.run(["git", "-C", d, "add", "-f", "--", "gen/pinned.txt"], check=True, env=GIT_ENV)
        self.assertEqual(_treefiles.tracked_under_ignore(d), ["gen/pinned.txt"])


if __name__ == "__main__":
    unittest.main()
