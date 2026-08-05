# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Parity test for the counts ASSURANCE.md quotes about its own gates.

ASSURANCE.md's premise is that a reader need not take the author's word for anything — every
gate it lists is one you can run yourself. A hand-maintained number in that document is the one
claim a reader CANNOT check without re-deriving it, and it drifts silently the moment a test is
added (it was 722 against 727 collected when this was written; 2026-07-29 verification round,
F6). So the numbers are pinned the same way CONFIG.md is pinned to the config registry: the doc
is the surface, the code is the source of truth, and the suite fails on a mismatch.

Counting is done the way a reader would: `pytest --collect-only` for the python suite (a
subprocess, so this file counts itself too — collection never executes anything, so there is no
recursion), and a glob for the shell suites, which is exactly what tests/run.sh iterates.

The atomic-write call-site count is the third, and it is here because the hand-written kind
drifted the moment a site was added: the commit that added a fourteenth left two documents
saying "thirteen", one of them ASSURANCE itself (fourteenth pass, F3). It is derived from the
code by parsing, not by regex — a regex over source text is the instrument that missed
`admin.py`'s `.gitignore` write, which ASSURANCE now warns about in prose. Both halves are
enumerated rather than listed: the writer names come from the definitions in `atomicio.py`, so
a future `atomic_write_bytes` counts the day it is written, and the prose sites come from a
walk of the whole tree, so a THIRD document stating the number is gated too. What counts as
python for both halves is read from the shebang as well as the extension — the engine's three
python entry points have no `.py`, and a scan that globbed for one could not see them.
"""
import ast
import glob
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ENGINE not in sys.path:
    sys.path.insert(0, ENGINE)

from quintessence import atomicio  # noqa: E402  (the rate gate derives from its constants)

ASSURANCE_MD = os.path.join(ENGINE, "ASSURANCE.md")
PACKAGE = os.path.join(ENGINE, "quintessence")
ATOMICIO = os.path.join(PACKAGE, "atomicio.py")

# The phrasings a live claim about this number may use. Kept deliberately specific: an earlier
# draft matched a bare "N call sites", which also caught test-redact.sh discussing ITS call
# sites. WHAT THIS DOES NOT REACH, both worth saying out loud: a fourth document that states the
# number in words of its own invention (adding a phrasing here is the price of inventing one),
# and the OTHER hand-written call-site counts in the same ASSURANCE section — `write.py`'s six
# `Path.write_text` sites, `cli.py`'s one, `write.py`'s three `shutil.copy` sites. Those were
# re-derived by hand on 2026-08-04 and were all correct; they are ungated, which is the same
# exposure this gate exists to remove, one enumeration away.
_COUNT_PHRASES = ("atomic-write call sites", "replace-the-whole-file writes")

# Number words, so ASSURANCE can keep writing counts the way the rest of its prose does. A word
# outside this table is a FAILURE, not a skip: a claim this gate cannot read is exactly the
# claim it was built to stop drifting.
_NUMBER_WORDS = {w: i for i, w in enumerate(
    ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
     "fifteen sixteen seventeen eighteen nineteen twenty").split())}

_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"}
_TEXT_EXT = (".md", ".py", ".sh", ".json", ".txt")

# What the estate itself calls python. `qq`, `qq-search` and `qq-ask` — the engine's three python
# entry points — carry no `.py`, so a scan that globs `*.py` cannot see them: three atomic-write
# lines appended to `qq` left this whole file green (fifteenth pass, F1). The two MCP servers are
# python as well, declared through uv rather than through a python interpreter directly
# (`#!/usr/bin/env -S uv run --script`, a PEP 723 script), so both spellings are matched. Read
# from the shebang rather than from a list of names, so an entry point added tomorrow is covered
# the day its shebang is written. The executable bit is deliberately NOT part of the test:
# `qq-remote-mcp` is mode 0644 in the repo and is python all the same.
_PY_SHEBANG = re.compile(r"^#!.*(?:python|\buv\b)")

# The one directory excluded from the outside-the-package scan below. tests/py/test_atomicio.py
# calls the primitive dozens of times on purpose — that is what a test of it looks like — so
# including the suite would make the control permanently red rather than informative.
_OUTSIDE_SCAN_SKIP = ("tests",)


def _callee_name(func) -> str:
    """The bare name a Call node invokes: `atomic_write_text(...)` and `mod.atomic_write_text(...)`
    both answer `atomic_write_text`."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _local_names(tree, names: set) -> set:
    """`names`, plus whatever THIS file's imports bind them to under another name.

    `from .atomicio import atomic_write_text as _aw` makes `_aw(p, "x")` an atomic write that a
    bare-name match cannot see — a real call site, uncounted, with every assertion in this file
    green (twenty-first pass, F4). The unaliased spelling did turn the gate red, so the hole was
    exactly the one import form that renames what it binds.

    Read from the file's own `ImportFrom` nodes rather than by collecting every short local name,
    so this stays precise in both directions: `_aw` is a writer in a module that imported the
    primitive as `_aw`, and an unrelated function of the same name elsewhere is still unrelated.
    `import quintessence.atomicio as m` needs nothing here — `m.atomic_write_text(...)` is an
    Attribute and `_callee_name` already answers with the attribute.
    """
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliases |= {a.asname for a in node.names if a.asname and a.name in names}
    return names | aliases


def _writer_names() -> set:
    """The primitive's public write API, read from its own definitions rather than hand-listed —
    so a wrapper added later is counted without anyone remembering to come back here."""
    with open(ATOMICIO, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return {n.name for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name.startswith("atomic_write")}


def _is_python_source(path: str) -> bool:
    """True for python this gate must read: a `.py` file, or a file whose shebang says python.

    The extension is not the estate's own answer to "is this python" — the shebang is, and it is
    what the kernel reads when `qq` runs. Deriving the answer that way covers the extensionless
    entry points, and covers the next one without anyone editing this file.
    """
    if path.endswith(".py"):
        return True
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return bool(_PY_SHEBANG.match(f.readline()))
    except OSError:
        return False


def _python_files(directory: str) -> list:
    """Every python source anywhere under `directory`, subpackages included.

    A WALK, not a one-level glob. The glob it replaces was the package half of a two-half
    enumeration whose other half prunes the package outright, so a call site in a subpackage was
    counted by NEITHER: `quintessence/sub/mod.py` calling `atomic_write_text` left all nine tests
    green, where the identical call in `procprobe.py` turned them red (eighteenth pass, F7). No
    subpackage exists today, which is what made it latent rather than wrong — and is also why a
    walk is the cheap fix: the day someone adds one, this counts it instead of losing it.

    Recursing here keeps the two halves disjoint AND complete, which the prune below depends on:
    everything under PACKAGE is this half's, everything else is the other's, with nothing in the
    gap. Pruning the same directories `_python_outside_the_package` prunes, so the two agree about
    what is not source.
    """
    found = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        found += [p for p in (os.path.join(root, fn) for fn in files)
                  if os.path.isfile(p) and _is_python_source(p)]
    return sorted(found)


def _python_outside_the_package() -> list:
    """Every python source in the tree except the package and the suite.

    A walk rather than a top-level glob, for the same reason `_prose_count_claims` walks: a
    boundary drawn by hand covers the places someone remembered. `tools/` holds python today, and
    a directory added tomorrow holds it without this file being touched.
    """
    skip = {os.path.join(ENGINE, d) for d in _OUTSIDE_SCAN_SKIP} | {PACKAGE}
    found = []
    for root, dirs, files in os.walk(ENGINE):
        dirs[:] = [d for d in dirs
                   if d not in _SKIP_DIRS and os.path.join(root, d) not in skip]
        found += [p for p in (os.path.join(root, fn) for fn in files)
                  if os.path.isfile(p) and _is_python_source(p)]
    return sorted(found)


def _call_sites(paths) -> dict:
    """{module: number of calls to the primitive} over the given python files, atomicio excluded.

    AST, not grep: an import line is not a Call, a name in a docstring is not a Call, and a call
    whose arguments contain brackets or quotes is still a Call. The `# noqa`-style false negative
    a regex gives you here is the same class of miss ASSURANCE's own re-derivation recipe warns
    about.
    """
    names = _writer_names()
    out = {}
    for path in paths:
        if os.path.abspath(path) == os.path.abspath(ATOMICIO):
            continue
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        local = _local_names(tree, names)
        n = sum(1 for node in ast.walk(tree)
                if isinstance(node, ast.Call) and _callee_name(node.func) in local)
        if n:
            out[os.path.relpath(path, ENGINE)] = n
    return out


# The shape a statement of the all-decimal-tail rate must take: "one <up to three words> in
# <number>". Wrap-tolerant in the same way `_prose_count_claims` is, because this claim is
# written across a line break in four places and a line-at-a-time scan finds none of them. The
# number matched is the DENOMINATOR: a sentence that also states a numerator ("~272 tails in
# 273") gives this gate a second number to drift, so the estate states the rate one way.
_RATE_CLAIM = re.compile(r"\bone\b(?:[\s#*]+[A-Za-z][A-Za-z-]*){0,3}[\s#*]+in[\s#*]+(\d+)\b")


def _all_decimal_tail_rate() -> int:
    """One in N, from the constants that decide it rather than from a number written here.

    `_open_unique_temp` draws `_TEMP_TOKEN_CHARS` characters from `_TEMP_TOKEN_ALPHABET`, and
    `is_generated_temp_name` refuses the tail unless one of them is in `_TEMP_TOKEN_LETTERS`. So
    the refused fraction is (non-letters / alphabet) ** width -- (10/16)^12 today -- and the
    disclosed price is its reciprocal, rounded the way prose states it.
    """
    alphabet = atomicio._TEMP_TOKEN_ALPHABET
    decimal = alphabet - atomicio._TEMP_TOKEN_LETTERS
    return round(1 / (len(decimal) / len(alphabet)) ** atomicio._TEMP_TOKEN_CHARS)


def _rate_claims() -> list:
    """[(relative path, line number, the denominator claimed)] over the whole tree.

    A walk, for the reason `_prose_count_claims` walks: the reviewer who found this number wrong
    listed six places and a grep found twenty-one, so a named list of documents is exactly the
    instrument that leaves a claim behind. This file is excluded -- it necessarily contains the
    phrasing it searches for.
    """
    me = os.path.abspath(__file__)
    found = []
    for root, dirs, files in os.walk(ENGINE):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            path = os.path.join(root, fn)
            if os.path.abspath(path) == me:
                continue
            if not (fn.endswith(_TEXT_EXT) or _is_python_source(path)):
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            for m in _RATE_CLAIM.finditer(text):
                found.append((os.path.relpath(path, ENGINE),
                              text.count("\n", 0, m.start()) + 1, int(m.group(1))))
    return found


def _prose_count_claims() -> list:
    """[(relative path, line number, the number claimed)] over the whole tree.

    A walk rather than a named list of documents, so the gate reaches a claim written into a file
    nobody thought of. This file is excluded because it necessarily contains the phrasings it
    searches for. Extensions are not the only way in: a file with no extension whose shebang says
    python is read too, because `qq` is a document as well as a program and a count stated in its
    comments would otherwise be ungated (fifteenth pass, F1, second half).

    Matched against the WHOLE file, not line by line, with the gaps between words allowed to
    contain a newline, a comment `#` or a markdown `*`. Prose wraps: the first version of this
    scan read one line at a time and could not see `atomic-write call\\n    sites` in the
    docstring it was written to gate — the same blindness, in a different instrument, as the
    regex over `open(` that ASSURANCE warns about.
    """
    gap = r"[\s#*]+"
    phrases = "|".join(gap.join(re.escape(w) for w in p.split()) for p in _COUNT_PHRASES)
    pattern = re.compile(r"\b([A-Za-z]+|\d+)[\s#*-]+(?:%s)\b" % phrases)
    me = os.path.abspath(__file__)
    found = []
    for root, dirs, files in os.walk(ENGINE):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            path = os.path.join(root, fn)
            if os.path.abspath(path) == me:
                continue
            if not (fn.endswith(_TEXT_EXT) or _is_python_source(path)):
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            for m in pattern.finditer(text):
                rel, lineno = os.path.relpath(path, ENGINE), text.count("\n", 0, m.start()) + 1
                word = m.group(1).lower()
                if word.isdigit():
                    found.append((rel, lineno, int(word)))
                elif word in _NUMBER_WORDS:
                    found.append((rel, lineno, _NUMBER_WORDS[word]))
                else:
                    raise AssertionError(
                        f"{rel}:{lineno} states a count of atomic-write call sites as "
                        f"{m.group(1)!r}, which this gate cannot read. Write it as a number or "
                        f"as a word up to twenty.")
    return found


def _documented(pattern: str) -> int:
    with open(ASSURANCE_MD) as f:
        text = f.read()
    m = re.search(pattern, text)
    if m is None:
        raise AssertionError(f"ASSURANCE.md no longer states a count matching {pattern!r} — if "
                             f"the wording changed, update this test with it")
    return int(m.group(1))


class TestAssuranceCountsMatchReality(unittest.TestCase):
    def test_python_test_count_matches_collection(self):
        # SKIP, not fail, when pytest is absent (eighth review pass, F4). The suite runs under
        # `unittest discover` (tests/test-py.sh), and the engine is advertised as stdlib-only
        # python plus bash -- so on exactly the minimal host the README describes, this gate used
        # to turn `bash tests/run.sh` red for a missing DEVELOPMENT tool rather than for anything
        # wrong with the engine. The number it pins is pytest's own collection count, so there is
        # nothing to check without it; CI installs pytest and checks it there.
        if importlib.util.find_spec("pytest") is None:
            self.skipTest("pytest not installed — the count gate reads pytest's collection count")
        documented = _documented(r"(\d+) tests over the engine modules")
        out = subprocess.run([sys.executable, "-m", "pytest", os.path.join("tests", "py"),
                              "--collect-only", "-q", "-p", "no:cacheprovider"],
                             cwd=ENGINE, capture_output=True, text=True).stdout
        m = re.search(r"(\d+) tests collected", out)
        self.assertIsNotNone(m, f"could not read a collected-test count from pytest:\n{out[-2000:]}")
        actual = int(m.group(1))
        self.assertEqual(documented, actual,
                         f"ASSURANCE.md says {documented} python tests; pytest collects "
                         f"{actual}. Update the number in ASSURANCE.md's 'Python tests' row.")

    def test_shell_suite_count_matches_the_suite_directory(self):
        documented = _documented(r"(\d+) end-to-end suites")
        actual = len(glob.glob(os.path.join(ENGINE, "tests", "test-*.sh")))
        self.assertEqual(documented, actual,
                         f"ASSURANCE.md says {documented} shell suites; tests/ holds {actual} "
                         f"test-*.sh files (what tests/run.sh iterates). Update the number in "
                         f"ASSURANCE.md's 'Shell suites' row.")


class TestAtomicWriteCallSiteCount(unittest.TestCase):
    """The number of atomic-write call sites, derived from the code and checked against every
    document that states it. Hand-maintained, it went stale in the same commit that changed it."""

    def test_every_stated_call_site_count_matches_the_code(self):
        sites = _call_sites(_python_files(PACKAGE))
        actual = sum(sites.values())
        claims = _prose_count_claims()
        for path, lineno, claimed in claims:
            self.assertEqual(
                claimed, actual,
                f"{path}:{lineno} says {claimed} atomic-write call sites; the package has "
                f"{actual}, across {len(sites)} modules: "
                + ", ".join(f"{m} x{n}" for m, n in sorted(sites.items())))

    def test_the_scan_reaches_the_documents_that_state_it(self):
        """A negative control that cannot fail is not a control. The test above passes vacuously
        if the scan finds nothing — a reworded sentence, a renamed file, a walk that skipped the
        wrong directory — so the scan's own reach is asserted here: both known claims, found by
        walking, not by being named as the files to open."""
        claims = _prose_count_claims()
        where = {path for path, _lineno, _n in claims}
        for expected in ("ASSURANCE.md", os.path.join("quintessence", "atomicio.py")):
            self.assertIn(expected, where,
                          f"{expected} no longer states the atomic-write call-site count in a "
                          f"form this gate can find. If the wording changed, add the phrasing to "
                          f"_COUNT_PHRASES — do not leave the claim ungated. Found: {sorted(where)}")

    def test_the_claim_is_about_the_package_and_nothing_writes_atomically_outside_it(self):
        """The documented sentence says "in the Python engine", so a call site outside the
        package would make the number right and the sentence wrong. Nothing outside it calls the
        primitive today; this is what notices when that changes.

        WHAT THIS DOES NOT REACH — a gate is an enumeration like any other, and the first version
        of this one named no blind axis while missing the engine's three python entry points
        (fifteenth pass, F1). A later version named three axes and still missed a fourth: a
        SUBPACKAGE fell between the halves, counted by neither, because the package half globbed
        one level while this half prunes the package whole (eighteenth pass, F7). That one is now
        reached rather than named — `_python_files` walks — and the pair below is what says so.

        - `tests/`, excluded above, so a call site added to a test HELPER that ships behaviour is
          invisible to this control.
        - Python that is neither a `.py` file nor shebang-declared — above all the python embedded
          in `setup.sh`'s heredoc, which no AST walk over python FILES can see. That heredoc
          open-codes the atomic idiom rather than calling the primitive, and keeping it in step
          with `atomicio.py` is `tests/test-setup-wire.sh`'s job, not this file's.
        - A call reached under a name no import statement spells: `getattr(atomicio, name)(...)`,
          a writer passed as an argument and called through the parameter, or one stored in a
          dict of handlers. The aliased-import form that used to sit on this list is now REACHED
          rather than named (twenty-first pass, F4) — `_local_names` resolves it — but the axis
          it belonged to, "the callee's spelling at the call is not the definition's", is only
          narrowed, not closed. What remains is dynamic, and the instrument for that is a runtime
          one, not an AST walk.
        """
        outside = _call_sites(_python_outside_the_package())
        self.assertEqual({}, outside,
                         "python outside the package now writes through atomicio, so the "
                         "documented count no longer covers every atomic write in the engine")

    def test_the_package_scan_reaches_a_subpackage(self):
        """A negative control that cannot fail is not a control, and the whole gate above rests on
        `_python_files` having seen every call site. For as long as the package was flat, "the
        count matches" and "the scan is one level deep" were indistinguishable — a subpackage call
        site was counted by neither half and all nine tests stayed green (eighteenth pass, F7).

        So the instrument is shown a nested file directly. Built in a temporary directory rather
        than in the package, because a control that writes into the source tree to prove the
        source tree is scanned can leave the repository dirty when it fails — and this file's
        other controls run against the real tree, so nothing is lost by isolating this one.

        `_call_sites` is driven as well as `_python_files`, because "found the file" and "counted
        the call in it" are two claims and only the second is what the gate uses."""
        source = ("from ..atomicio import atomic_write_text\n"
                  "def save(p):\n"
                  "    atomic_write_text(p, 'x')\n")
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "sub", "deeper"))
            nested = os.path.join(d, "sub", "deeper", "mod.py")
            with open(nested, "w", encoding="utf-8") as f:
                f.write(source)
            self.assertIn(nested, _python_files(d),
                          "the package scan must see python in a subpackage; a one-level glob "
                          "does not, and the outside-the-package half prunes the package whole, "
                          "so such a call site is counted by neither")
            self.assertEqual(1, sum(_call_sites([nested]).values()),
                             "finding the file is not enough — the call inside it must be counted")

    def test_the_scan_counts_a_call_reached_through_an_aliased_import(self):
        """Twenty-first pass, F4. The walk matched a callee by its bare name, so
        `from .atomicio import atomic_write_text as _aw` followed by `_aw(p, "x")` added a real
        atomic write that the gate did not count — with every assertion in this file still green,
        and the documented number therefore silently wrong. The same call spelled unaliased, or
        reached through the module attribute, did turn it red, so the gate was not inert; it had
        a hole shaped exactly like the one import form that renames what it binds.

        Resolved now from the file's own `ImportFrom` nodes, which is precise rather than
        heuristic: `_aw` counts in a module that imports the primitive under that name, and a
        variable called `_aw` in a module that does not is still nothing.

        The decoy is built in a temporary directory for the reason the subpackage control above
        gives — a control that writes into the source tree can leave the repository dirty when it
        fails."""
        aliased = ("from .atomicio import atomic_write_text as _aw\n"
                   "from .atomicio import atomic_write_json\n"
                   "def save(p):\n"
                   "    _aw(p, 'x')\n"
                   "    atomic_write_json(p, {})\n")
        # No import of the primitive at all: `_aw` here is somebody else's function, and reading
        # the alias out of the import list is what tells the two files apart. A scan that simply
        # added `_aw` to the name set would count this one too.
        unrelated = ("from .elsewhere import _aw\n"
                     "def save(p):\n"
                     "    _aw(p, 'x')\n")
        with tempfile.TemporaryDirectory() as d:
            paths = {}
            for name, source in (("aliased.py", aliased), ("unrelated.py", unrelated)):
                paths[name] = os.path.join(d, name)
                with open(paths[name], "w", encoding="utf-8") as f:
                    f.write(source)
            self.assertEqual(2, sum(_call_sites([paths["aliased.py"]]).values()),
                             "an aliased import binds the primitive under a new name, and a call "
                             "through it is an atomic write like any other")
            self.assertEqual({}, _call_sites([paths["unrelated.py"]]),
                             "the alias is read from the import that creates it — a same-named "
                             "function from somewhere else is not this primitive")

    def test_the_outside_scan_reaches_the_extensionless_entry_points(self):
        """A control for the control above: it passes vacuously over files it never opens, and for
        a whole review round it never opened the engine's three python entry points. So the reach
        is asserted directly — these files must be IN the list it walks, found by shebang rather
        than by being named as the files to scan. The names are written out on purpose: this is
        the assertion that the derivation works, so it has to say what it expects the derivation
        to find."""
        scanned = {os.path.relpath(p, ENGINE) for p in _python_outside_the_package()}
        for expected in ("qq", "qq-search", "qq-ask", "qq-search-mcp", "qq-remote-mcp"):
            self.assertIn(expected, scanned,
                          f"{expected} is python the atomic-write scan no longer reads. If it was "
                          f"renamed, say so here; if its shebang changed, teach _PY_SHEBANG the "
                          f"new spelling — do not leave it unscanned. Scanned: {sorted(scanned)}")


# The `except` clauses that catch atomicio's name-length refusal. A bare `except:` catches
# BaseException and so catches it too; a tuple is read element by element, because
# `except (OSError, ValueError): pass` swallows exactly as thoroughly as the bare spelling.
_CATCHES_THE_REFUSAL = {"OSError", "Exception", "BaseException"}


def _caught_names(node) -> set:
    """The exception names an `except` clause names — {"BaseException"} for a bare one."""
    if node is None:
        return {"BaseException"}
    if isinstance(node, ast.Tuple):
        return {_callee_name(e) for e in node.elts}
    return {_callee_name(node)}


def _functions_that_write(paths) -> set:
    """The package's own function names whose body contains an atomic write.

    One frame of call graph, and it is here because one frame is exactly what the finding
    turned out to reach: `xref.py`'s three callers of `save_content_store` each wrapped it in
    `except OSError: pass`, so the write itself carried no handler and a scan that only looked
    at the write's own lines called them clean. Names are matched bare, which is how the rest of
    this file matches callees.
    """
    writers = _writer_names()
    found = set()
    for path in paths:
        if os.path.abspath(path) == os.path.abspath(ATOMICIO):
            continue
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(isinstance(c, ast.Call) and _callee_name(c.func) in writers
                   for c in ast.walk(node)):
                found.add(node.name)
    return found


def _swallowing_call_sites(paths) -> set:
    """{(relative path, enclosing function)} for every atomic write inside a handler that
    discards the exception whole, counting one frame of indirection.

    "Discards it whole" is read mechanically, so this cannot become a judgement call: a `try`
    whose BODY contains the write, and which has a handler for `OSError`, `Exception` or nothing
    at all whose body is exactly `pass`. A handler that logs, re-raises, or narrows to something
    the primitive does not raise is not this shape and is not listed.

    Keyed by function name rather than line number: a line number is stale the next time anything
    above it moves, and the point of the record is which WRITE is best-effort, not which line.
    """
    names = _writer_names() | _functions_that_write(paths)
    found = set()
    for path in paths:
        if os.path.abspath(path) == os.path.abspath(ATOMICIO):
            continue
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        local = _local_names(tree, names)
        enclosing = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(node):
                    enclosing[inner] = node.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            writes = any(isinstance(c, ast.Call) and _callee_name(c.func) in local
                         for stmt in node.body for c in ast.walk(stmt))
            if not writes:
                continue
            for handler in node.handlers:
                if not _CATCHES_THE_REFUSAL & _caught_names(handler.type):
                    continue
                if len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass):
                    found.add((os.path.relpath(path, ENGINE),
                               enclosing.get(node, "<module>")))
    return found


class TestNoCallSiteSwallowsTheWriteRefusal(unittest.TestCase):
    """Seventeenth pass, F1, generalised. `atomicio` gained an ENAMETOOLONG refusal for a target
    name it cannot build a temp beside, and `search.py`'s `except OSError: pass` swallowed it —
    so the sidecar silently stopped being written and ASSURANCE's "the refusal is loud" was false
    at the only call site that could reach it. The fix was the class, not the instance: a caller
    that must not fail its surrounding work uses `atomicio.best_effort_write`, which keeps the
    silence for a transient OSError and says so for a name that can never work.

    The sweep found four more of the same shape — `reconcile._save_snapshot`, and `xref`'s three
    callers of `save_content_store`, which is where the one-frame reach below comes from. Those
    five sites write three targets, and every one of them is named by a path setting an operator
    can make arbitrarily long.

    This is what keeps it fixed. A call site added next Tuesday with a fresh `except OSError:
    pass` fails here until someone either routes it through the helper or writes down why its
    target's name can never reach the refusal.
    """

    # The one write that may still discard an OSError whole, and why. Not a suppression list to
    # append to lightly: adding a row is a claim about a target's NAME, which is the axis the
    # refusal turns on.
    #
    # refsresolve.append_event trims `refs/events.jsonl` past a high-water mark. The basename is
    # a literal twelve bytes, fixed in the code and not derived from any setting, so no operator
    # can push it near NAME_MAX; and a failed trim loses nothing — the append already landed and
    # the next event trims again. Its handler is `except Exception`, wider still, and narrowing
    # it is a separate question from this one.
    _BEST_EFFORT_BY_DESIGN = {("quintessence/refsresolve.py", "append_event")}

    def test_no_atomic_write_is_swallowed_except_the_one_that_may_be(self):
        found = _swallowing_call_sites(_python_files(PACKAGE))
        self.assertEqual(
            found, self._BEST_EFFORT_BY_DESIGN,
            "an atomic write sits inside a handler that discards the exception whole. If the "
            "write is housekeeping that must not fail the work around it, wrap it in "
            "`atomicio.best_effort_write` instead — it keeps the silence for a transient failure "
            "and says so for a name that can never work. If it genuinely may stay silent, add it "
            "above with the reason its target's name cannot reach the refusal.\n"
            f"  found:    {sorted(found)}\n  expected: {sorted(self._BEST_EFFORT_BY_DESIGN)}")

    def test_the_scan_can_see_the_shape_it_looks_for(self):
        """A negative control that cannot fail is not a control, and this one's negative is the
        whole assertion. So the instrument is shown a file of the exact shape it must catch."""
        source = ("from .atomicio import atomic_write_json\n"
                  "def save(p, o):\n"
                  "    try:\n"
                  "        atomic_write_json(p, o)\n"
                  "    except OSError:\n"
                  "        pass\n")
        with tempfile.TemporaryDirectory() as d:
            decoy = os.path.join(d, "decoy.py")
            with open(decoy, "w", encoding="utf-8") as f:
                f.write(source)
            self.assertEqual(_swallowing_call_sites([decoy]),
                             {(os.path.relpath(decoy, ENGINE), "save")},
                             "the scan must catch the pre-fix shape, or the assertion above is "
                             "vacuous")

    def test_the_scan_sees_the_shape_reached_through_an_aliased_import(self):
        """This scan matches callees by bare name too, so it carried the same alias hole the
        counts gate did (twenty-first pass, F4) — a swallowed write reached under a local name
        would have been absent from the list this file pins, which reads as "no site swallows the
        refusal". Fixed in the shared helper, and shown here on this scan's own shape."""
        source = ("from .atomicio import atomic_write_json as _aj\n"
                  "def save(p, o):\n"
                  "    try:\n"
                  "        _aj(p, o)\n"
                  "    except OSError:\n"
                  "        pass\n")
        with tempfile.TemporaryDirectory() as d:
            decoy = os.path.join(d, "decoy.py")
            with open(decoy, "w", encoding="utf-8") as f:
                f.write(source)
            self.assertEqual(_swallowing_call_sites([decoy]),
                             {(os.path.relpath(decoy, ENGINE), "save")},
                             "a write swallowed under an aliased name is still a swallowed write")

    def test_what_this_does_not_reach(self):
        """Named rather than left implicit, because a guard on one axis makes the whole question
        look settled. This scan sees a handler around the write or around a function that
        contains one — one frame — in python the package ships. It does not see:

        - a handler TWO or more frames up. One frame is not a principled boundary, it is the
          distance the finding turned out to travel: the write's own line in `search.py`, and one
          call up in `xref.py`. A third frame swallowing the same refusal would be invisible here,
          and the instrument that would see it is a runtime one — a write refused with a name past
          the budget, driven through each entry point in turn;
        - a handler whose body is not exactly `pass` but discards the message all the same — a
          bare `return`, or a log line that drops the exception text;
        - `except FileNotFoundError`-style narrow handlers, which cannot catch this refusal today
          but would if the errno mapping ever changed;
        - callees matched by bare name, so a same-named method on an unrelated class counts as a
          writer here. That is the FALSE-POSITIVE direction. The false-negative one — a writer
          called under a name of the importing file's choosing — is resolved by `_local_names`
          for aliased imports and remains open for anything dynamic (`getattr`, a callable passed
          in as an argument);
        - python outside the package, and the atomic idiom open-coded in `setup.sh`'s heredoc,
          which no AST walk over python files can see."""
        callers_of_the_two_helped_sites = _swallowing_call_sites(_python_files(PACKAGE))
        self.assertNotIn(("quintessence/search.py", "_save_orphan_ages"),
                         callers_of_the_two_helped_sites,
                         "the site the finding named must be off this list, by fix rather than "
                         "by the scan failing to reach it")


class TestTheAllDecimalTailRateMatchesTheConstants(unittest.TestCase):
    """The one-in-N rate of an all-decimal temp tail, derived from the constants that set it and
    checked against every place that states it.

    The reclaim is deliberately narrower than the writer -- it wants a hex LETTER in the tail --
    and the price is a temp the writer really wrote that the reclaim will not take. The estate
    disclosed that price as "about one in 273" in twenty-one places, two of them operator-facing.
    The arithmetic is `(10/16)^12`, one in 281.5 (twenty-third pass, F3; the reviewer also
    measured 1 in 282.7 over two million real draws). Wrong in the direction of overstating the
    cost, and nothing computes on it -- but this project gates its counts precisely because a
    hand-maintained number is the one claim a reader cannot check, and this one was stated in two
    operator documents, five times across three source files, and fourteen test comments, with
    nothing checking any of them. The finding named six of the twenty-one; a grep found the rest.

    So it is derived, from `atomicio`'s own alphabet, letter set and tail width rather than from
    a number written here. Widen the tail, change the alphabet, or drop the letter condition and
    every sentence stating the rate has to move with it.
    """

    def test_every_stated_rate_matches_the_one_the_constants_produce(self):
        derived = _all_decimal_tail_rate()
        claims = _rate_claims()
        for path, lineno, claimed in claims:
            self.assertEqual(
                claimed, derived,
                f"{path}:{lineno} says one temp in {claimed} draws an all-decimal tail; "
                f"atomicio's constants give one in {derived} -- "
                f"({len(atomicio._TEMP_TOKEN_ALPHABET - atomicio._TEMP_TOKEN_LETTERS)}/"
                f"{len(atomicio._TEMP_TOKEN_ALPHABET)})^{atomicio._TEMP_TOKEN_CHARS}")

    def test_the_scan_reaches_the_surfaces_that_state_the_rate(self):
        """A negative control that cannot fail is not a control: the test above passes vacuously
        over an empty scan. Four surfaces are asserted, and each is structural rather than
        remembered -- the module whose constants set the rate, the two operator-facing documents,
        and the shell mirror that open-codes the same reclaim and states the same price."""
        where = {path for path, _lineno, _n in _rate_claims()}
        for expected in (os.path.join("quintessence", "atomicio.py"), "ASSURANCE.md", "CONFIG.md",
                         "setup.sh"):
            self.assertIn(expected, where,
                          f"{expected} no longer states the all-decimal-tail rate in a form this "
                          f"gate can read (`one <words> in <number>`). If the wording changed, "
                          f"change it back or teach _RATE_CLAIM the new one -- do not leave the "
                          f"number ungated. Found: {sorted(where)}")

    def test_the_scan_would_see_a_wrong_number(self):
        """And the instrument proved on a positive it can actually fail on: the pattern read over
        text that states the rate wrongly, in the wrapped-across-a-line shape the real estate
        uses, must come back with the wrong number rather than with nothing."""
        wrapped = "the price is that about one\n    generated temp in 999 is not reclaimed here"
        self.assertEqual([int(n) for n in _RATE_CLAIM.findall(wrapped)], [999])


if __name__ == "__main__":
    unittest.main()
