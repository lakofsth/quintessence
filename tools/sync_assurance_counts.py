#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""sync_assurance_counts.py – writes the counts ASSURANCE.md's count gate pins.

tests/py/test_assurance_counts.py fails the suite when ASSURANCE.md's "N tests over the engine
modules" differs from pytest's collection, or its "N end-to-end suites" from the tests/test-*.sh
glob. The gate is right to be strict, but a hand-typed number meant every commit that added a
test was red until a follow-up commit fixed the figure: between 1 and 18 October 2026 six
mainline commits did nothing else, and one of them (92cb076) needed a hand landing because the
commit it followed was red. This is the cheap route to making the figure true IN THE SAME
COMMIT: run it after adding or removing a test, then stage ASSURANCE.md with the test.

It counts with the SAME collection the gate runs — the gate calls collect_python_count and
count_shell_suites here and reads PYTHON_ROW and SHELL_ROW, so the two cannot drift apart. It
REFUSES (exit 2, the cause on stderr, ASSURANCE.md untouched) when collection exits non-zero or
reports an error, pytest is missing, or a row is missing or ambiguous, rather than writing a
number taken from a broken collection; the gate fails on the same refusal.

What is counted is what a clean checkout of the commit holds. Collection runs over a temporary
checkout of the INDEX (`git checkout-index`), and the suites are the index's tests/test-*.sh —
so an unstaged edit to a tracked test file, a tracked file deleted only from the working tree,
and a test file git does not track (untracked or ignored) change neither count, and a staged
file counts, being part of the commit about to be made. An intent-to-add (`git add -N`) or
unmerged entry is neither staged content nor absent, so the tool REFUSES while one is under
tests/ and names it. In a clean checkout the index is the
commit. Edits and files left out that way are named on stderr, so an author who meant them for
this commit knows to `git add` them and run again. The index was chosen over refusing a dirty
tree because the gate shares this collection: a refusal would redden the whole suite whenever
a test file is mid-edit. Every git call runs with GIT_* stripped, so a hook's GIT_INDEX_FILE or
GIT_DIR cannot name another repository's index. An engine that is not a git work tree (an
unpacked release) has no index and is counted as it stands.

The figures the counts are held to come from the same place: `--check`, and the gate, read the
index's ASSURANCE.md, so a figure written here and not staged is stale to both, which name the
unstaged copy. The count is read from pytest's final summary line alone, matched whole — the
node ids listed above it are test names, and a string parameter can make one read "7 tests
collected" — and a summary that says tests were deselected is refused, since the figure is the
whole collection.

Usage:
    python3 tools/sync_assurance_counts.py           # rewrite the figures in ASSURANCE.md
    python3 tools/sync_assurance_counts.py --check   # exit 1 if a figure is stale, write nothing
"""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import glob
import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

_ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# What the count gate runs and reads. Shared with tests/py/test_assurance_counts.py.
COLLECT_ARGV = ["-m", "pytest", os.path.join("tests", "py"), "--collect-only", "-q",
                "-p", "no:cacheprovider", "--color=no"]
# pytest's summary, the LAST line of its output, matched whole: "1197 tests collected in 0.5s",
# "1 test collected in 0.01s", with a broken module "1197 tests collected, 1 error in 0.5s" —
# the count alone is not the verdict — and under a -k/-m or a deselecting conftest
# "S/N tests collected (D deselected)". Only that line: `--collect-only -q` prints every node id
# above it, and a string parameter becomes the id, so `test_x[7 tests collected]` is output too.
COLLECTED = re.compile(r"(?:(?P<selected>\d+)/)?(\d+) tests? collected"
                       r"(?: \((?P<deselected>\d+) deselected\))?"
                       r"(?P<groups>(?:, \d+ \w+)*)(?: in [0-9.]+s(?: \([0-9:]+\))?)?")
PYTHON_ROW = r"(\d+) tests over the engine modules"
SHELL_ROW = r"(\d+) end-to-end suites"
# pytest's default python_files, the names its collection picks up under tests/py.
PYTHON_TEST_FILES = ("test_*.py", "*_test.py")
SHELL_SUITES = os.path.join("tests", "test-*.sh")

COMMAND = "python3 tools/sync_assurance_counts.py"


class Refused(Exception):
    """A figure could not be derived honestly; nothing is written."""


def _is_checkout(engine: str) -> bool:
    """Asked of the engine's own .git, so an unpacked release inside some other repository is
    not read as that repository's checkout."""
    return os.path.exists(os.path.join(engine, ".git"))


def _git(engine: str, *argv: str, env_extra: dict = None) -> str:
    # GIT_DIR / GIT_INDEX_FILE set by a hook would name some other repository's index.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(env_extra or {})
    try:
        proc = subprocess.run(["git", "-C", engine, *argv], capture_output=True, text=True,
                              env=env)
    except FileNotFoundError:
        raise Refused("the engine is a git checkout but git is not installed, so what its "
                      "index holds cannot be read")
    if proc.returncode != 0:
        raise Refused(f"git {argv[0]} failed in the engine (exit {proc.returncode}): "
                      f"{proc.stderr.strip()}")
    return proc.stdout


def _paths(out: str) -> list:
    return sorted(p for p in out.split("\0") if p)


def _is_python_test(p: str) -> bool:
    return (p.startswith(os.path.join("tests", "py") + os.sep)
            and any(fnmatch.fnmatch(os.path.basename(p), pat) for pat in PYTHON_TEST_FILES))


def _is_shell_suite(p: str) -> bool:
    return os.path.dirname(p) == "tests" and fnmatch.fnmatch(p, SHELL_SUITES)


def _is_counted(p: str) -> bool:
    return _is_python_test(p) or _is_shell_suite(p)


def untracked_tests(engine: str) -> list:
    """Test files and suites git does not track, untracked and ignored alike: none of them is
    in the index, so none is counted. [] when the engine is not its own checkout."""
    if not _is_checkout(engine):
        return []
    return [p for p in _paths(_git(engine, "ls-files", "--others", "-z", "--", "tests"))
            if _is_counted(p)]


def unstaged_tests(engine: str) -> list:
    """Tracked test files and suites whose working-tree copy differs from the index (edited or
    deleted): what is counted is the index's copy, not this one."""
    if not _is_checkout(engine):
        return []
    # --no-renames: a renamed file's old path is a deletion, and a rename lists only the new one.
    return sorted({p for p in _paths(_git(engine, "diff", "--no-renames", "--name-only", "-z",
                                          "--", "tests"))
                   if _is_counted(p)})


def _intent_to_add(engine: str) -> set:
    """Paths under tests/ the index lists and a commit of it would not record: what `ls-files`
    lists less what `write-tree` builds from the same index, which leaves out every
    intent-to-add entry. Read from the index itself, so neither a rename `diff` pairs it with
    nor a deleted working-tree copy hides one. `write-tree` runs on a copy of the index into a
    scratch object directory, so the engine's index and objects are left as they were. The
    index must hold no unmerged entry (`write-tree` refuses one)."""
    def where(name):
        return _git(engine, "rev-parse", "--path-format=absolute", "--git-path", name).strip()
    scratch = tempfile.mkdtemp(prefix="sync-assurance-ita-")
    try:
        index = os.path.join(scratch, "index")
        if os.path.exists(where("index")):
            shutil.copyfile(where("index"), index)
        os.mkdir(os.path.join(scratch, "objects"))
        env = {"GIT_INDEX_FILE": index, "GIT_OBJECT_DIRECTORY": os.path.join(scratch, "objects"),
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": where("objects")}
        tree = _git(engine, "write-tree", env_extra=env).strip()
        recorded = set(_paths(_git(engine, "ls-tree", "-r", "-z", "--name-only", tree, "--",
                                   "tests", env_extra=env)))
        listed = set(_paths(_git(engine, "ls-files", "-z", "--", "tests", env_extra=env)))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return listed - recorded


def refuse_unresolved_index(engine: str) -> None:
    """The index holds two entries `checkout-index` and `ls-files` read differently — an
    intent-to-add entry (`git add -N`: listed, but written out empty) and an unmerged one
    (stages 1 to 3: listed, but skipped) — so a count over either is a figure no commit holds.
    Refuses, naming them, while the index has one under tests/ — an intent-to-add entry
    renamed or deleted in the working tree included. No-op outside a checkout."""
    if not _is_checkout(engine):
        return
    unmerged = {rec.split("\t", 1)[1] for rec in _git(engine, "ls-files", "-u", "-z", "--",
                                                       "tests").split("\0") if "\t" in rec}
    # Unmerged entries are refused first; the intent-to-add read needs an index without them.
    intent = set() if unmerged else _intent_to_add(engine)
    bad = sorted(p for p in unmerged | intent if _is_counted(p))
    if not bad:
        return
    why = []
    if any(p in unmerged for p in bad):
        why.append("unmerged (resolve and `git add` it)")
    if any(p in intent and p not in unmerged for p in bad):
        why.append("intent-to-add (`git add` it for real, or `git reset` it)")
    raise Refused("the index holds test files that are " + " / ".join(why) + ", so no count "
                  "of it is what a commit holds: " + ", ".join(bad))


@contextlib.contextmanager
def counted_tree(engine: str):
    """A directory holding what the commit holds: a temporary checkout of the engine's index,
    removed afterwards, or the engine itself when it is not a git checkout."""
    if not _is_checkout(engine):
        yield engine
        return
    refuse_unresolved_index(engine)
    root = tempfile.mkdtemp(prefix="sync-assurance-index-")
    # SIGTERM and SIGHUP end the process without running a `finally`; turn them into an exit so
    # the checkout is removed. Only the main thread may install a handler.
    def _exit_on_signal(signum, _frame):
        raise SystemExit(128 + signum)
    saved = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                saved[sig] = signal.signal(sig, _exit_on_signal)
            except ValueError:
                break
        _git(engine, "checkout-index", "--all", f"--prefix={root}{os.sep}")
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def read_collected(proc) -> int:
    """The collected count from a finished COLLECT_ARGV run, or Refused naming why not. Read
    from pytest's final summary line only, matched whole (COLLECTED)."""
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    last = lines[-1].strip() if lines else ""
    m = COLLECTED.fullmatch(last)
    errors = [g for g in (m.group("groups") if m else "").split(", ") if "error" in g]
    if proc.returncode != 0 or m is None or errors:
        out = proc.stdout + proc.stderr
        cause = out[out.find("= ERRORS ="):] if "= ERRORS =" in out else out
        said = f", pytest reported {', '.join(errors)}" if errors else ""
        if m is None and proc.returncode == 0:
            said = f", and its last line is not its collection summary: {last!r}"
        raise Refused(f"pytest collection failed (exit {proc.returncode}{said}); fix it before "
                      f"counting:\n{cause[-2000:]}")
    if m.group("deselected"):
        raise Refused(f"pytest deselected {m.group('deselected')} of the {m.group(2)} tests it "
                      f"collected ({last!r}): a -k/-m option, an addopts or a conftest narrows "
                      f"the collection, and the figure is the whole of it — remove the "
                      f"deselection before counting")
    return int(m.group(2))


def collect_python_count(engine: str) -> int:
    if importlib.util.find_spec("pytest") is None:
        raise Refused("pytest is not installed — the figure is pytest's collection count")
    with counted_tree(engine) as root:
        # PYTEST_ADDOPTS (-v, -k ...) changes the summary line the count is read from.
        env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
        proc = subprocess.run([sys.executable, *COLLECT_ARGV], cwd=root,
                              capture_output=True, text=True, env=env)
    return read_collected(proc)


def count_shell_suites(engine: str) -> int:
    if not _is_checkout(engine):
        return len(glob.glob(os.path.join(engine, SHELL_SUITES)))
    refuse_unresolved_index(engine)
    return len([p for p in _paths(_git(engine, "ls-files", "-z", "--", "tests"))
                if _is_shell_suite(p)])


DOC = "ASSURANCE.md"


def stated_doc(engine: str) -> str:
    """ASSURANCE.md as the counts read it: the index's copy, which is what the commit holds, or
    the file itself when the engine is not a git checkout. The gate reads its figures here, so
    a figure written and not staged is held to the commit it is not in, not passed."""
    if not _is_checkout(engine):
        with open(os.path.join(engine, DOC)) as f:
            return f.read()
    entries = _git(engine, "ls-files", "--stage", "--", DOC).splitlines()
    if len(entries) != 1 or entries[0].split()[2] != "0":
        raise Refused(f"the index holds no resolved {DOC} (untracked or unmerged), so the commit "
                      f"states no figure to check: `git add {DOC}`")
    return _git(engine, "show", f":{DOC}")


def unstaged_doc(engine: str) -> bool:
    """True when the working-tree ASSURANCE.md differs from the index's (what stated_doc reads)."""
    return _is_checkout(engine) and bool(_git(engine, "diff", "--name-only", "--", DOC).strip())


UNSTAGED_DOC = (f"{DOC} has unstaged changes, and the figures are read from the index's copy "
                f"(what the commit holds) — `git add {DOC}` if they belong in this commit")


def _replace_one(text: str, pattern: str, value: int) -> str:
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise Refused(f"ASSURANCE.md states {len(matches)} counts matching {pattern!r}, "
                      f"expected exactly one")
    m = matches[0]
    return text[:m.start(1)] + str(value) + text[m.end(1):]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="exit 1 if a figure is stale; write nothing")
    ap.add_argument("--engine", default=_ENGINE, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    path = os.path.join(args.engine, DOC)
    try:
        # --check judges what the gate judges, the index's copy; a write edits the file itself.
        if args.check:
            before = stated_doc(args.engine)
        else:
            with open(path) as f:
                before = f.read()
        after = _replace_one(before, PYTHON_ROW, collect_python_count(args.engine))
        after = _replace_one(after, SHELL_ROW, count_shell_suites(args.engine))
        untracked = untracked_tests(args.engine)
        unstaged = unstaged_tests(args.engine)
        doc_unstaged = unstaged_doc(args.engine)
    except (Refused, OSError) as e:
        print(f"sync_assurance_counts: REFUSED, ASSURANCE.md not written: {e}", file=sys.stderr)
        return 2
    for p in untracked:
        print(f"sync_assurance_counts: not counted, git does not track it: {p} — `git add` it "
              f"and run again if it belongs in this commit", file=sys.stderr)
    for p in unstaged:
        print(f"sync_assurance_counts: counted as staged, not as it stands: {p} has unstaged "
              f"changes — `git add` it and run again if they belong in this commit",
              file=sys.stderr)
    if after == before:
        print("sync_assurance_counts: ASSURANCE.md counts already match")
        if not args.check and doc_unstaged:
            print(f"sync_assurance_counts: {UNSTAGED_DOC}", file=sys.stderr)
        return 0
    if args.check:
        hint = f"; {UNSTAGED_DOC}" if doc_unstaged else ""
        print(f"sync_assurance_counts: ASSURANCE.md counts are stale — run `{COMMAND}`{hint}",
              file=sys.stderr)
        return 1
    with open(path, "w") as f:
        f.write(after)
    print("sync_assurance_counts: wrote ASSURANCE.md — stage it in the same commit as the tests")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    from quintessence import pipeexit
    pipeexit.run(main)
