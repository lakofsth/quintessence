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

The ignore rules are read the way git reads a tree's own `.gitignore` files: one in every
directory the walk enters, each applying below its own directory, a deeper file's rules after a
shallower one's, and the last rule that matches a path deciding it. Within a file: `#` comments,
`!` negation, a leading `\\` escape, a trailing `/` for directories only, a slash at the start or in
the middle anchoring the pattern to the file's directory, `**` across directories, and `*`, `?` and
`[...]` within one name. A directory a rule leaves out is not entered, so a rule below it, as in
git, cannot bring a file back. What git reads besides the tree (`.git/info/exclude`, the user's
global excludes) is not read: an export carries neither. test_treefiles.py checks the walk against
git on this repo and on a fixture that uses each of these forms in a nested file (a tool's own
`.pytest_cache/.gitignore` or `.venv/.gitignore`, `*`, is the case that earned it).
"""
import fnmatch
import os
import subprocess


def _is_checkout(engine: str) -> bool:
    return os.path.exists(os.path.join(engine, ".git"))


def _read_rules(directory: str, base: tuple) -> list:
    """The rules of `directory`/.gitignore, as (base, pattern parts, anchored, dir_only, negate),
    where `base` is the directory's path parts from the engine root."""
    try:
        with open(os.path.join(directory, ".gitignore"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    rules = []
    for ln in lines:
        while ln.endswith(" ") and not ln.endswith("\\ "):
            ln = ln[:-1]
        if not ln or ln.startswith("#"):
            continue
        negate = ln.startswith("!")
        if negate:
            ln = ln[1:]
        elif ln.startswith("\\"):
            ln = ln[1:]
        dir_only = ln.endswith("/")
        ln = ln.rstrip("/")
        if not ln:
            continue
        anchored = "/" in ln
        rules.append((base, tuple(ln.lstrip("/").split("/")), anchored, dir_only, negate))
    return rules


def _parts_match(pat: tuple, parts: tuple) -> bool:
    if not pat:
        return not parts
    if pat[0] == "**":
        if len(pat) == 1:  # a trailing `/**` matches everything inside, not the directory itself
            return bool(parts)
        return any(_parts_match(pat[1:], parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pat[0]) and _parts_match(pat[1:], parts[1:])


def _ignored(rules: list, parts: tuple, is_dir: bool) -> bool:
    verdict = False
    for base, pat, anchored, dir_only, negate in rules:
        if dir_only and not is_dir:
            continue
        if parts[:len(base)] != base or len(parts) <= len(base):
            continue
        rel = parts[len(base):]
        if anchored:
            hit = _parts_match(pat, rel)
        else:
            hit = fnmatch.fnmatchcase(rel[-1], pat[0])
        if hit:
            verdict = not negate
    return verdict


def walk_files(engine: str) -> list:
    """Every file under `engine`, relative and sorted, leaving out `.git` and what the tree's own
    `.gitignore` files, at every depth, leave out."""
    rules_of = {(): _read_rules(engine, ())}
    out = []
    for d, subdirs, names in os.walk(engine):
        rel_d = os.path.relpath(d, engine)
        base = () if rel_d == "." else tuple(rel_d.split(os.sep))
        rules = rules_of.pop(base)
        kept = []
        for s in sorted(subdirs):
            if s == ".git" or _ignored(rules, base + (s,), True):
                continue
            kept.append(s)
            rules_of[base + (s,)] = rules + _read_rules(os.path.join(d, s), base + (s,))
        subdirs[:] = kept
        for n in names:
            if n == ".git" or _ignored(rules, base + (n,), False):
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


def tracked_under_ignore(engine: str) -> list:
    """The tracked files an ignore file of the tree covers (`git ls-files -c -i
    --exclude-per-directory=.gitignore`): what a walk of the filesystem cannot name, because only
    git knows a file is tracked. Raises if git fails, rather than reading an empty list as none."""
    out = subprocess.run(["git", "-C", engine, "ls-files", "-z", "-c", "-i",
                          "--exclude-per-directory=.gitignore"], capture_output=True,
                         text=True, check=True).stdout
    return sorted(p for p in out.split("\0") if p)
