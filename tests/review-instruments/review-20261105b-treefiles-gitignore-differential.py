#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Differential probe: tests/py/_treefiles.walk_files against git, over generated trees.

fix-review 20261105T135828Z (round 2 of the 2026-11 release review) over 819665a. Each case builds a
small tree with .gitignore files at several depths, drawn from the syntax the module's docstring
says it reads (`#`, `!`, leading `\\`, trailing `/`, anchoring, `**`, `*`, `?`, `[...]`), runs
`git init` and compares walk_files with the same listing test_treefiles._git_listing uses
(tracked-on-disk | `ls-files --others --exclude-per-directory=.gitignore`). It then runs a fixed
list of named edge cases, each one a form a real .gitignore carries.

Usage: review-20261105b-treefiles-gitignore-differential.py <engine-root> [cases] [seed]
Prints one line per mismatching named case and a count of random mismatches; exit 1 if any.
"""
import os, random, subprocess, sys, tempfile

ENGINE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
N = int(sys.argv[2]) if len(sys.argv) > 2 else 400
SEED = int(sys.argv[3]) if len(sys.argv) > 3 else 1
sys.path.insert(0, os.path.join(ENGINE, "tests", "py"))
import _treefiles  # noqa: E402


def git_listing(root):
    def git(*a):
        return [p for p in subprocess.run(["git", "-C", root, *a, "-z"], capture_output=True,
                                          check=True).stdout.decode("utf-8", "surrogateescape").split("\0") if p]
    on_disk = {p for p in git("ls-files") if os.path.isfile(os.path.join(root, p))}
    return on_disk | set(git("ls-files", "--others", "--exclude-per-directory=.gitignore"))


def write(root, rel, data):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())


def compare(files, ignores, setup=None):
    with tempfile.TemporaryDirectory() as d:
        for rel in files:
            write(d, rel, "x\n")
        for rel, text in ignores.items():
            write(d, rel, text)
        if setup:
            setup(d)
        subprocess.run(["git", "init", "-q", d], check=True)
        try:
            walk = set(_treefiles.walk_files(d))
        except Exception as e:  # noqa: BLE001
            return f"walk_files raised {type(e).__name__}: {e}"
        want = git_listing(d)
        if walk != want:
            return f"walk-only={sorted(walk - want)} git-only={sorted(want - walk)}"
    return None


NAMED = {
    # each: (files, {gitignore path: content})
    "BOM at the start of a .gitignore (an editor on Windows writes one)":
        (["a.log", "b.py"], {".gitignore": b"\xef\xbb\xbf*.log\n"}),
    "CRLF line endings":
        (["a.log", "b.py"], {".gitignore": "*.log\r\n"}),
    "[^...] negated class (git wildmatch reads ^ as !)":
        (["a.x", "b.y", "c.z"], {".gitignore": "*.[^x]\n"}),
    "[!...] negated class":
        (["a.x", "b.y"], {".gitignore": "*.[!x]\n"}),
    "inner backslash escape \\*":
        (["a*b", "axb"], {".gitignore": "a\\*b\n"}),
    "unanchored ** with no slash":
        (["d/e/f.txt", "g.txt"], {".gitignore": "**.txt\n"}),
    "a/**/b zero dirs":
        (["a/b", "a/x/y/b", "a/c"], {".gitignore": "a/**/b\n"}),
    "**/ prefix in a nested file":
        (["p/q/r.tmp", "r.tmp"], {"p/.gitignore": "**/r.tmp\n"}),
    "trailing /** keeps the directory's own name":
        (["out/a", "out/sub/b", "outx"], {".gitignore": "out/**\n!out/a\n"}),
    "negated directory-only rule":
        (["b/x", "c/b/y"], {".gitignore": "b/\n!/c/b/\n"}),
    "non-UTF-8 byte in a comment":
        (["a.log", "b.py"], {".gitignore": b"# caf\xe9\n*.log\n"}),
    "pattern with a leading ./ (git never matches it)":
        (["a.log"], {".gitignore": "./a.log\n"}),
    "class with a range and a slash-free bracket":
        (["v1.bak", "v9.bak", "va.bak"], {".gitignore": "v[0-5].bak\n"}),
    "[[:digit:]] POSIX class":
        (["v1.bak", "va.bak"], {".gitignore": "v[[:digit:]].bak\n"}),
}

NAMES = ["a", "b", "x.log", "y.tmp", "build", "keep.tmp", "gen.txt", ".hid", "#h", "!b", "c1", "c9"]
PATS = ["*.log", "*.tmp", "!keep.tmp", "build/", "/build", "build", "a/b", "**/gen.txt",
        "a/**", "a/**/b", "!a/b", "*", "!*.log", "\\#h", "\\!b", ".hid", "c[0-5]", "c?", "/a/",
        "b/**/x.log", "!/build/", "**/b/", "#comment", "", "y.tmp ", "a/*/gen.txt"]


def rand_case(rng):
    files = set()
    for _ in range(rng.randint(3, 14)):
        depth = rng.randint(0, 3)
        files.add("/".join(rng.choice(NAMES) for _ in range(depth + 1)))
    # a path may not be both a file and a directory
    dirs = {"/".join(f.split("/")[:i]) for f in files for i in range(1, f.count("/") + 1)}
    files = {f for f in files if f not in dirs}
    ignores = {}
    for dd in [""] + sorted(dirs):
        if rng.random() < 0.5:
            ignores[(dd + "/" if dd else "") + ".gitignore"] = "\n".join(
                rng.choice(PATS) for _ in range(rng.randint(1, 4))) + "\n"
    return sorted(files), ignores


bad = 0
def _symlink_dir(d):
    os.symlink("real", os.path.join(d, "link"))


def _nested_repo(d):
    subprocess.run(["git", "init", "-q", os.path.join(d, "vendor")], check=True)


SETUPS = {
    "an untracked symlink to a directory (git lists it as a file)":
        (["real/a.py"], {}, _symlink_dir),
    "a nested git repository (git lists `vendor/` and does not descend)":
        (["vendor/a.py"], {}, _nested_repo),
}
for name, (files, ign, setup) in SETUPS.items():
    NAMED[name] = (files, ign, setup)
for name, (files, ign, *setup) in NAMED.items():
    r = compare(files, ign, *setup)
    print(f"{'MISMATCH' if r else 'ok      '} {name}" + (f": {r}" if r else ""))
    bad += bool(r)
rng = random.Random(SEED)
rbad, examples = 0, []
for i in range(N):
    files, ign = rand_case(rng)
    r = compare(files, ign)
    if r:
        rbad += 1
        if len(examples) < 5:
            examples.append((files, ign, r))
print(f"random: {rbad} of {N} cases mismatch (seed {SEED})")
for f, ig, r in examples:
    print("  files", f, "ignores", ig, "->", r)
sys.exit(1 if bad or rbad else 0)
