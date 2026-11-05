# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""The files of the engine tree, in a git checkout or in a plain copy of one.

Tests that DERIVE their sites from the tree (every shell file that reads a path key, every
Python entry point) used to list it with `git ls-files`, which exits 128 outside a checkout, so
the suite errored when run from an export or a tarball (release gate on 2026-11-04: 3 errors in
test_config_path_parity.py and test_path_key_tilde_parity.py on a tree with no `.git`). A plain
copy is a `git archive` of the tracked files, so walking it and leaving out what `.gitignore`
names gives the same set. test_treefiles.py checks that, inside a checkout, the walk and git
agree on every file git does not ignore.

The ignore patterns are read from the tree's own top-level `.gitignore`, one per line, matched
with fnmatch against each path component (a trailing `/` matches directories only). That is a
subset of git's syntax, enough for this repo's file; test_treefiles.py fails if the two disagree.
"""
import fnmatch
import os
import subprocess


def _is_checkout(engine: str) -> bool:
    return os.path.exists(os.path.join(engine, ".git"))


def _ignore_patterns(engine: str):
    try:
        with open(os.path.join(engine, ".gitignore"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return [], []
    files, dirs = [], []
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        (dirs if ln.endswith("/") else files).append(ln.rstrip("/"))
    return files, dirs


def walk_files(engine: str) -> list:
    """Every file under `engine`, relative and sorted, leaving out `.git` and what the top-level
    `.gitignore` names."""
    file_pats, dir_pats = _ignore_patterns(engine)
    out = []
    for d, subdirs, names in os.walk(engine):
        subdirs[:] = sorted(s for s in subdirs if s != ".git"
                            and not any(fnmatch.fnmatch(s, p) for p in dir_pats + file_pats))
        for n in names:
            if n == ".git" or any(fnmatch.fnmatch(n, p) for p in file_pats):
                continue
            out.append(os.path.relpath(os.path.join(d, n), engine))
    return sorted(out)


def engine_files(engine: str) -> list:
    """The tracked files: `git ls-files` in a checkout, else walk_files. Raises if git fails in a
    checkout, rather than reading an empty list as an empty tree."""
    if _is_checkout(engine):
        out = subprocess.run(["git", "-C", engine, "ls-files", "-z"], capture_output=True,
                             text=True, check=True).stdout
        return sorted(p for p in out.split("\0") if p)
    return walk_files(engine)
