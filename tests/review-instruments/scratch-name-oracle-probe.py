#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""scratch-name-oracle-probe.py -- derives, by running them, every assertIn / assertNotIn in
tests/py whose verdict depends on the NAME of the ambient scratch directory (the register row
`a-test-oracle-over-paths-does-not-read-the-scratch-directory-name`).

Usage:  python3 tests/review-instruments/scratch-name-oracle-probe.py [ENGINE] [MODULE ...]
        (ENGINE defaults to the repo this file sits in; MODULEs default to every tests/py/test_*.py)

How it decides, without guessing a word: it runs the python suite exactly as tests/test-py.sh
does (unittest discovery, scratch QQ_CACHE, no CLAUDE_CODE_SESSION_ID), with
TestCase.assertIn/assertNotIn wrapped. At every call whose member is a str (or bytes) and whose
container is a str (or bytes) holding the scratch root, it re-evaluates the membership twice: once
with every occurrence of the scratch root replaced by a neutral `/SCRATCH`, and once replaced by
`/SCRATCH` + the member itself -- that is, under a scratch directory whose name carries the word.
If the two answers differ, the assertion's verdict is decided by what the scratch directory is
called, and the call site (file:line) is reported. An assertNotIn site so reported goes red under
a scratch name holding its word; an assertIn site is vacuously green under one.

It quantifies over the calls the suite EXECUTES, not over the source text, so a site the suite
skips on this host is not seen; that count is printed. Exit 1 when any site is reported, 0 when
none, 2 when the suite itself could not be run. Not a gate: tests/run.sh does not run it.

Positive control (--self-test): a planted assertNotIn("refused", <a path under the scratch root>)
must be reported; a planted assertNotIn("refused", <a path-free string>) and a planted
assertNotIn(<a word carrying a 16-hex nonce>, <a path under the scratch root>) must not.
"""
import io
import re
import os
import sys
import tempfile
import traceback
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def scratch_roots():
    roots = set()
    for t in (os.environ.get("TMPDIR"), tempfile.gettempdir()):
        if t:
            roots.add(t.rstrip("/"))
            roots.add(os.path.realpath(t).rstrip("/"))
    roots.discard("")
    return sorted(roots, key=len, reverse=True)


ROOTS = scratch_roots()
NONCE = re.compile(r"[0-9a-f]{12,}")
HITS = {}       # site -> (method, member)
SEEN = [0]      # calls whose container held the scratch root


def _sub(container, insert):
    out = container
    for r in ROOTS:
        if isinstance(container, bytes):
            out = out.replace(r.encode(), b"/SCRATCH" + insert)
        else:
            out = out.replace(r, "/SCRATCH" + insert)
    return out


def _site():
    for fr in reversed(traceback.extract_stack()[:-3]):
        if os.sep + "tests" + os.sep + "py" + os.sep in fr.filename or "selftest" in fr.name:
            return "%s:%d" % (os.path.relpath(fr.filename), fr.lineno)
    return "?"


def _check(method, member, container):
    if not isinstance(member, (str, bytes)) or type(member) is not type(container):
        return
    enc = (lambda s: s.encode()) if isinstance(container, bytes) else (lambda s: s)
    if not any(enc(r) in container for r in ROOTS):
        return
    # A member that names the scratch root itself compares a whole fixture path, root included,
    # and the root is the same bytes on both sides. A member holding a '/' or a newline cannot be
    # (part of) one directory name, so no scratch directory can be called after it.
    if any(enc(r) in member for r in ROOTS) or enc("/") in member or enc("\n") in member:
        return
    # A member carrying a run of 12+ lowercase hex digits is taken to be a per-run nonce (the
    # shape of uuid4().hex, and of tests/py/test_search.py's _nonce_slug): no scratch directory
    # is called after a word that did not exist before the run, which is the escape the register
    # row names. This is the probe's one judgement about a member, and it is stated here.
    if NONCE.search(member if isinstance(member, str) else member.decode("latin-1")):
        return
    SEEN[0] += 1
    neutral = member in _sub(container, enc(""))
    named = member in _sub(container, member)
    if neutral != named:
        HITS.setdefault(_site(), (method, member))


_orig_in, _orig_not_in = unittest.TestCase.assertIn, unittest.TestCase.assertNotIn


def _assert_in(self, member, container, msg=None):
    _check("assertIn", member, container)
    return _orig_in(self, member, container, msg)


def _assert_not_in(self, member, container, msg=None):
    _check("assertNotIn", member, container)
    return _orig_not_in(self, member, container, msg)


unittest.TestCase.assertIn = _assert_in
unittest.TestCase.assertNotIn = _assert_not_in


def self_test():
    root = ROOTS[0]

    class P(unittest.TestCase):
        def test_selftest_path(self):
            try:
                self.assertNotIn("refused", os.path.join(root, "f.tmp"))
            except AssertionError:
                pass

        def test_selftest_clean(self):
            self.assertNotIn("refused", "Deleted 0 of 0\n")

        def test_selftest_nonce(self):
            self.assertNotIn("CALLED-0123456789abcdef", os.path.join(root, "f.tmp"))

    unittest.TextTestRunner(stream=io.StringIO()).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(P))
    sites = [s for s in HITS]
    ok = len(sites) == 1 and HITS[sites[0]] == ("assertNotIn", "refused")
    print("self-test:", "OK (planted site reported; path-free and nonce sites not)" if ok
          else "FAILED: %r" % HITS)
    return 0 if ok else 1


def main(argv):
    if argv[:1] == ["--self-test"]:
        return self_test()
    engine = os.path.abspath(argv[0]) if argv else os.path.dirname(os.path.dirname(HERE))
    modules = argv[1:]
    os.chdir(engine)
    sys.path.insert(0, engine)
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    cache = tempfile.mkdtemp()
    os.environ.setdefault("QQ_CACHE", os.path.join(cache, "embeddings.json"))
    loader = unittest.defaultTestLoader
    if modules:
        sys.path.insert(0, os.path.join(engine, "tests", "py"))
        suite = unittest.TestSuite(loader.loadTestsFromName(m) for m in modules)
    else:
        suite = loader.discover("tests/py", pattern="test_*.py", top_level_dir=None)
    res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    print("scratch roots: %s" % ", ".join(ROOTS))
    print("tests run: %d (failures %d, errors %d, skipped %d)" % (
        res.testsRun, len(res.failures), len(res.errors), len(res.skipped)))
    print("assertIn/assertNotIn calls whose container held the scratch root: %d" % SEEN[0])
    print("sites whose verdict depends on the scratch directory's name: %d" % len(HITS))
    for site, (m, w) in sorted(HITS.items()):
        print("  %s  %s(%r, ...)" % (site, m, w))
    if res.testsRun == 0:
        return 2
    return 1 if HITS else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
