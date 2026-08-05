# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.atomicio — the one atomic write.

The property that matters is symlink resolution. `os.replace` does not follow symlinks: it
replaces the LINK with a regular file. Every durable write here used to hand-roll that call, so a
config or state file the operator had symlinked into a git repo (the normal way to version-control
a dotfile) was silently detached on the first write — link became a real file, repo copy kept the
old contents, nothing reported it. These tests pin the fix and the atomicity it must not lose.
"""
import errno
import io
import json
import os
import re
import shutil
import stat
import sys
import time
import tempfile
import unittest
import unittest.mock

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ENGINE)

from quintessence.atomicio import (atomic_write, atomic_write_json, atomic_write_lines,
                                   atomic_write_text, resolve_for_write)

ASSURANCE_MD = os.path.join(ENGINE, "ASSURANCE.md")

# The grace an operator is promised, in seconds, WRITTEN OUT rather than imported from the module
# under test. `TEMP_GRACE_SECONDS` is what these tests are holding still; a fixture that derives
# its ages from it moves with any drift and pins nothing, which is how a shrink to 60 seconds
# would have stayed green through nineteen passes while ASSURANCE.md kept promising operators an
# hour (nineteenth pass, F2). The documents' own word for this number is read back below.
DOCUMENTED_GRACE_SECONDS = 60 * 60

# Where a document states that grace in a form a test can read: "older than an hour", "never
# reclaimed at the one-hour grace". Deliberately not anchored to one sentence — every statement
# of the promise found in the two documents has to agree, so rewording one of them and leaving
# the others behind is itself a failure.
_GRACE_PROMISE_RE = re.compile(r"(?:older than|reclaimed at) (?:the )?(an|one|a|\d+)[- ]"
                               r"(hours?|minutes?|seconds?|days?)")


def _letter_bearing_token(width: int, seq: int) -> bytes:
    """Bytes to hand a forced `os.urandom`: `width` of them, distinct per `seq`, and always
    spelling a hex tail that carries one of `abcdef`.

    WHY A FIXTURE HERE MAY NOT USE THE REAL RANDOMNESS. `is_generated_temp_name` is deliberately
    NARROWER than the writer: on top of twelve lowercase hex it requires a hex LETTER, because
    twelve digits are also twelve valid hex characters and an operator's
    `date +%Y%m%d%H%M` backup sat inside the width (twenty-first pass, F2). So about one tail in
    281 comes out all-decimal and is NOT reclaimed — the disclosed cost, pinned by
    `test_a_temp_the_writer_really_wrote_with_an_all_decimal_tail_is_not_reclaimed`. Every pin
    that needs its temp RECOGNISED was therefore asserting a coin flip: nine of them across this
    file, tests/py/test_search.py and tests/test-setup-wire.sh. Thirteen of the tails they draw
    decide an assertion — twelve in the python gate, one in the installer suite — so the python
    gate went red about one run in twenty-four until the tails were held still (D77).

    NOT a name spelled by hand: only the randomness is pinned, and the temp is still opened by
    `_open_unique_temp` itself, so a change to the writer's spelling or width still moves the
    fixture with it (rule 7). Width is taken from the caller — whatever `os.urandom` was asked
    for — rather than from the module's constant, so a width change cannot leave this handing
    back the old one. The same holding-still is what the O_EXCL attack in this file does, for the
    same reason: a property you cannot assert under real randomness is not asserted at all.

    Twinned by an identical helper in tests/py/test_search.py, which has the same fixtures and no
    import path to here; each use site guards itself with the real predicate, so a divergence
    shows up as a red fixture guard rather than as a quiet re-arming of the flake.
    """
    return b"\xaa" + seq.to_bytes(width - 1, "big")


def _writer_temp(mod, parent: str, basename: str, seq: int = 0) -> str:
    """A temp opened by the WRITER with its tail forced to one the reclaim predicate claims.

    Returns the path; the descriptor is closed. `seq` must differ between temps that share a
    parent and basename — the forced token IS the name, so two of them would collide on O_EXCL.
    """
    with unittest.mock.patch.object(mod.os, "urandom",
                                    lambda n: _letter_bearing_token(n, seq)):
        fd, path = mod._open_unique_temp(parent, basename)
    os.close(fd)
    return path


def _documented_temp_examples() -> tuple:
    """(removed, kept) — the temp names ASSURANCE's deletion paragraph says a reclaim deletes, and
    the ones it says survive, read out of the document rather than copied into this file.

    Read rather than restated so the fixture cannot drift from the disclosure: if the paragraph is
    reworded, the names the suite plants move with it, and if the paragraph is emptied the lists
    come back short and the test that uses them says so.
    """
    with open(ASSURANCE_MD, encoding="utf-8") as f:
        text = f.read()
    start = text.index("**A temp older than an hour beside a target is deleted")
    para = text[start:text.index("\n\nAtomicity is in any case", start)]

    def _names(fragment: str) -> list:
        return [n for n in re.findall(r"`([^`]+)`", fragment) if ".tmp" in n]

    removed = re.search(r"removed \(([^)]*)\)", para)
    kept = re.search(r"left alone:(.*?)\n\n", para, re.S)
    if removed is None or kept is None:
        raise AssertionError(
            "ASSURANCE.md's deletion paragraph no longer states which names a reclaim removes and "
            "which it leaves alone in a form this test can read. If the wording changed, teach the "
            "reader here — do not leave the disclosure unexecuted.")
    return _names(removed.group(1)), _names(kept.group(1))


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _p(self, *parts):
        return os.path.join(self.tmp, *parts)

    # ---- the regression this module exists for ----------------------------------------
    def test_writes_through_a_symlink_instead_of_replacing_it(self):
        """The 2026-08-03 defect: writing a symlinked config detached it from its repo."""
        os.makedirs(self._p("repo"))
        real = self._p("repo", "config")
        with open(real, "w") as fh:
            fh.write("OLD=1\n")
        link = self._p("config")
        os.symlink(real, link)

        atomic_write_text(link, "NEW=2\n")

        self.assertTrue(os.path.islink(link), "the symlink must survive the write")
        self.assertEqual(os.path.realpath(link), real)
        with open(real) as fh:
            self.assertEqual(fh.read(), "NEW=2\n", "the real file must receive the content")

    def test_returns_the_resolved_target(self):
        os.makedirs(self._p("repo"))
        real = self._p("repo", "f")
        open(real, "w").close()
        link = self._p("f")
        os.symlink(real, link)
        self.assertEqual(atomic_write_text(link, "x"), real)

    def test_temp_file_lands_beside_the_real_file_not_the_link(self):
        """os.replace is only atomic within one filesystem, so the temp must be created next to
        the RESOLVED destination — a link may point across a mount."""
        os.makedirs(self._p("repo"))
        real = self._p("repo", "f")
        link = self._p("f")
        os.symlink(real, link)
        atomic_write_text(link, "x")
        self.assertEqual(os.listdir(self._p("repo")), ["f"], "no .tmp left behind")
        self.assertEqual(sorted(os.listdir(self.tmp)), ["f", "repo"])

    def test_symlinked_parent_directory_is_resolved(self):
        os.makedirs(self._p("real_dir"))
        os.symlink(self._p("real_dir"), self._p("link_dir"))
        atomic_write_text(self._p("link_dir", "f"), "v")
        with open(self._p("real_dir", "f")) as fh:
            self.assertEqual(fh.read(), "v")

    # ---- behaviour that must not change ------------------------------------------------
    def test_plain_file_create_and_overwrite(self):
        p = self._p("plain")
        atomic_write_text(p, "one")
        atomic_write_text(p, "two")
        with open(p) as fh:
            self.assertEqual(fh.read(), "two")
        self.assertFalse(os.path.islink(p))

    def test_creates_missing_parent_directories(self):
        p = self._p("a", "b", "c", "f")
        atomic_write_text(p, "deep")
        with open(p) as fh:
            self.assertEqual(fh.read(), "deep")

    def test_no_temp_file_survives(self):
        p = self._p("f")
        atomic_write_text(p, "x")
        self.assertEqual(os.listdir(self.tmp), ["f"])

    def test_utf8_roundtrip(self):
        p = self._p("f")
        atomic_write_text(p, "Juusjärvi — ä\n")
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "Juusjärvi — ä\n")

    def test_dangling_symlink_creates_the_target(self):
        """A link whose target does not exist yet: write the target, keep the link."""
        os.makedirs(self._p("repo"))
        real = self._p("repo", "not-yet")
        link = self._p("f")
        os.symlink(real, link)
        atomic_write_text(link, "created")
        self.assertTrue(os.path.islink(link))
        with open(real) as fh:
            self.assertEqual(fh.read(), "created")

    # ---- json wrapper -------------------------------------------------------------------
    def test_json_wrapper_writes_and_passes_kwargs(self):
        p = self._p("j.json")
        atomic_write_json(p, {"b": 1, "a": 2}, sort_keys=True)
        with open(p) as fh:
            text = fh.read()
        self.assertEqual(json.loads(text), {"a": 2, "b": 1})
        self.assertLess(text.index('"a"'), text.index('"b"'), "sort_keys must pass through")

    def test_json_follows_symlinks_too(self):
        os.makedirs(self._p("repo"))
        real = self._p("repo", "cache.json")
        open(real, "w").close()
        link = self._p("cache.json")
        os.symlink(real, link)
        atomic_write_json(link, {"k": "v"})
        self.assertTrue(os.path.islink(link))
        with open(real) as fh:
            self.assertEqual(json.load(fh), {"k": "v"})

    # ---- resolve_for_write ---------------------------------------------------------------
    def test_resolve_of_nonexistent_path_is_itself(self):
        p = self._p("never-created")
        self.assertEqual(resolve_for_write(p), p)

    def test_resolve_accepts_pathlike(self):
        import pathlib
        p = self._p("f")
        self.assertEqual(resolve_for_write(pathlib.Path(p)), p)


class TestStreamingAndFailure(unittest.TestCase):
    """The primitive hands out a handle rather than taking a finished string: the embedding cache
    is ~350MB here, and serialising it first cost ~2 extra copies in RSS (measured 2026-08-03)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def test_caller_streams_in_chunks(self):
        p = self._p("f")
        with atomic_write(p) as fh:
            for i in range(5):
                fh.write(f"chunk{i}\n")
        with open(p) as fh:
            self.assertEqual(fh.read(), "".join(f"chunk{i}\n" for i in range(5)))

    def test_lines_accepts_a_lazy_generator(self):
        """Never force the caller to materialise one big string."""
        p = self._p("f")
        consumed = []

        def gen():
            for i in range(3):
                consumed.append(i)
                yield f"{i}\n"

        atomic_write_lines(p, gen())
        self.assertEqual(consumed, [0, 1, 2])
        with open(p) as fh:
            self.assertEqual(fh.read(), "0\n1\n2\n")

    def test_failure_leaves_no_temp_and_keeps_the_old_file(self):
        p = self._p("f")
        atomic_write_text(p, "original")
        with self.assertRaises(ValueError):
            with atomic_write(p) as fh:
                fh.write("half-written")
                raise ValueError("boom")
        self.assertEqual(os.listdir(self.tmp), ["f"], "the .tmp must be cleaned up")
        with open(p) as fh:
            self.assertEqual(fh.read(), "original", "a failed write must not damage the old file")

    def test_failure_through_a_symlink_keeps_link_and_target(self):
        os.makedirs(self._p("repo"))
        real = self._p("repo", "f")
        with open(real, "w") as fh:
            fh.write("original")
        link = self._p("f")
        os.symlink(real, link)
        with self.assertRaises(RuntimeError):
            with atomic_write(link):
                raise RuntimeError("boom")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.listdir(self._p("repo")), ["f"])
        with open(real) as fh:
            self.assertEqual(fh.read(), "original")

    def test_json_is_streamed_not_serialised_first(self):
        """json.dump writes through the handle; json.dumps would build the whole string first.
        Pinned by observing the handle receive more than one write."""
        p = self._p("j.json")
        writes = []
        real_open = open

        class CountingHandle:
            def __init__(self, fh):
                self._fh = fh

            def write(self, s):
                writes.append(s)
                return self._fh.write(s)

            def __getattr__(self, name):
                return getattr(self._fh, name)

        real_fdopen = os.fdopen
        with unittest.mock.patch("os.fdopen",
                                 lambda *a, **k: CountingHandle(real_fdopen(*a, **k))):
            atomic_write_json(p, {"a": [1, 2, 3], "b": {"c": 4}})
        self.assertGreater(len(writes), 1,
                           "json.dump streams many small writes; json.dumps would be one")
        with real_open(p) as fh:
            self.assertEqual(json.load(fh), {"a": [1, 2, 3], "b": {"c": 4}})


class TestReviewFindings(unittest.TestCase):
    """Findings from the 2026-08-03 pre-publication review, both confirmed by reproduction."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def test_destination_is_resolved_exactly_once(self):
        """Each wrapper used to realpath() twice -- once to write, once to return. That is a path
        walk inside whatever lock the caller holds, and _save_cache checkpoints repeatedly
        mid-build while holding its flock."""
        real = os.path.realpath
        for fn, args in ((atomic_write_text, ("x",)),
                         (atomic_write_lines, (["a\n"],)),
                         (atomic_write_json, ({"k": 1},))):
            calls = []
            with unittest.mock.patch("os.path.realpath",
                                     lambda p: (calls.append(p), real(p))[1]):
                fn(self._p(fn.__name__), *args)
            self.assertEqual(len(calls), 1,
                             f"{fn.__name__} resolved the destination {len(calls)} times")

    def test_symlink_cycle_refuses_instead_of_clobbering(self):
        """realpath gives up on a cycle and returns the path unchanged -- still a link, so
        os.replace would silently replace it: the exact detachment this module prevents."""
        a, b = self._p("a"), self._p("b")
        os.symlink(b, a)
        os.symlink(a, b)
        with self.assertRaises(OSError) as ctx:
            atomic_write_text(a, "written")
        self.assertEqual(ctx.exception.errno, errno.ELOOP)
        self.assertTrue(os.path.islink(a), "the cycle must be left alone, not replaced")
        self.assertEqual(sorted(os.listdir(self.tmp)), ["a", "b"], "no .tmp left behind")

    def test_lines_are_streamed_not_joined(self):
        """F1 from the 2026-08-03 independent pass: `11ffa9a` reverted TWO buffering regressions
        (json.dumps and "".join(lines)) but only the json one was pinned -- reintroducing
        fh.write("".join(lines)) left the whole file green. Same shape as the json test."""
        p = self._p("f")
        writes = []
        real_fdopen = os.fdopen

        class CountingHandle:
            def __init__(self, fh):
                self._fh = fh

            def write(self, s):
                writes.append(s)
                return self._fh.write(s)

            def writelines(self, lines):
                for ln in lines:
                    self.write(ln)

            def __getattr__(self, name):
                return getattr(self._fh, name)

        with unittest.mock.patch("os.fdopen",
                                 lambda *a, **k: CountingHandle(real_fdopen(*a, **k))):
            atomic_write_lines(p, [f"{i}\n" for i in range(5)])
        self.assertEqual(len(writes), 5,
                         "writelines streams one write per line; \"\".join would be a single write")
        with open(p) as fh:
            self.assertEqual(fh.read(), "0\n1\n2\n3\n4\n")

    def test_concurrent_writers_cannot_truncate_each_others_temp(self):
        """The temp name must be unique per writer, not `<target>.tmp`. Two unlocked writers to
        one destination (xref's content store, config_set, reconcile's snapshot) would otherwise
        splice: A opens the temp, B truncates the same path, A renames B's partial file into
        place."""
        p = self._p("f")
        seen = []
        import quintessence.atomicio as mod
        real_open_tmp = mod._open_unique_temp

        def spy(*a, **k):
            fd, path = real_open_tmp(*a, **k)
            seen.append(path)
            return fd, path

        with unittest.mock.patch.object(mod, "_open_unique_temp", spy):
            atomic_write_text(p, "one")
            atomic_write_text(p, "two")
        self.assertEqual(len(set(seen)), 2, f"temp paths must differ per write, got {seen}")
        self.assertNotIn(p + ".tmp", seen, "the predictable name must not be used")

        # Distinct names are the mechanism; not truncating each other is the PROPERTY, and the
        # assertions above only reach the mechanism. So two temps for one destination are opened
        # and held TOGETHER here and written through separately: a writer that reused one name
        # would leave one file carrying one writer's bytes instead of two files carrying their
        # own. (With the name made constant, the second open cannot even be had -- O_EXCL refuses
        # a hundred times and this raises EEXIST, which is the same failure said earlier.)
        fd_a, path_a = mod._open_unique_temp(self.tmp, "f")
        try:
            fd_b, path_b = mod._open_unique_temp(self.tmp, "f")
            try:
                os.write(fd_a, b"A" * 8)
                os.write(fd_b, b"B" * 8)
            finally:
                os.close(fd_b)
        finally:
            os.close(fd_a)
        self.assertNotEqual(path_a, path_b, "two in-flight temps for one target must be two files")
        for path, written in ((path_a, "A" * 8), (path_b, "B" * 8)):
            with open(path) as fh:
                self.assertEqual(fh.read(), written,
                                 f"{os.path.basename(path)} carries another writer's bytes -- one "
                                 f"in-flight temp truncated the other, which is the splice unique "
                                 f"names exist to prevent")

    def test_a_symlink_planted_at_the_temp_path_cannot_redirect_the_write(self):
        """O_EXCL, pinned by the attack it exists to refuse rather than by the temp name's
        randomness (nineteenth pass, F1).

        The fixture this replaces planted its symlink at `<target>.tmp` — the name the OLD,
        predictable idiom used, and the one name the current writer will never emit. It therefore
        passed because `os.urandom` had not collided, not because the open refuses to follow a
        link: deleting `os.O_EXCL` from `_open_unique_temp` left the whole suite green while the
        module docstring named that defence twice.

        So the randomness is pinned for the length of the attack, and the symlink is planted at
        the name the writer WILL use — taken from the writer itself rather than spelled here.
        Under a pinned `os.urandom`, `_open_unique_temp` is deterministic for a given (parent,
        basename), so the name it hands back is the name the next write asks the kernel for.

        With O_EXCL the open refuses on every one of its hundred attempts and the write fails
        EEXIST, leaving the victim, the destination and the planted link exactly as they were.
        Without it the open follows the link, the payload lands in the victim, and `os.replace`
        renames the LINK onto the destination — which is then a symlink to the victim, the exact
        redirection this is here to stop.

        Not a live hole at HEAD either way: predicting 48 bits of `urandom` is infeasible, so what
        was unpinned was defence in depth, not an exposure."""
        import quintessence.atomicio as mod
        victim = self._p("victim")
        with open(victim, "w") as fh:
            fh.write("untouched")
        p = self._p("data")
        atomic_write_text(p, "original")

        def _fixed_urandom(n):
            return b"\x5a" * n

        target = resolve_for_write(p)
        with unittest.mock.patch("os.urandom", _fixed_urandom):
            fd, will_use = mod._open_unique_temp(os.path.dirname(target),
                                                 os.path.basename(target))
        os.close(fd)
        os.unlink(will_use)                     # the writer's own next name, now free to be taken
        os.symlink(victim, will_use)
        self.assertTrue(mod.is_generated_temp_name(os.path.basename(will_use)),
                        "fixture guard: the plant has to sit at a name this module's own writer "
                        "emits, which is exactly what the `<target>.tmp` fixture did not")

        with unittest.mock.patch("os.urandom", _fixed_urandom):
            with self.assertRaises(OSError) as ctx:
                atomic_write_text(p, "PAYLOAD")
        self.assertEqual(ctx.exception.errno, errno.EEXIST,
                         "a temp path that is already taken must be refused, not opened through")

        with open(victim) as fh:
            self.assertEqual(fh.read(), "untouched", "the payload must not reach the victim")
        self.assertFalse(os.path.islink(p), "the destination must not become a symlink")
        with open(p) as fh:
            self.assertEqual(fh.read(), "original",
                             "a refused write must leave the old file whole")
        self.assertTrue(os.path.islink(will_use),
                        "the planted link is the operator's file as far as this module knows: "
                        "refusing means leaving it alone, not consuming it")
        self.assertEqual(sorted(os.listdir(self.tmp)),
                         sorted(["victim", "data", os.path.basename(will_use)]),
                         "a refused write must leave nothing else behind either")

    def test_existing_destination_mode_is_preserved(self):
        """A rewrite must not widen permissions -- and through a symlink, must not widen them on
        the real file."""
        # 0640, NOT 0600: mkstemp already creates 0600, so asserting 0600 would pass even if
        # the mode were never carried across -- a toothless test of exactly the F1 kind.
        p = self._p("secret")
        atomic_write_text(p, "v1")
        os.chmod(p, 0o640)
        atomic_write_text(p, "v2")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o640)

        os.makedirs(self._p("repo"))
        real = self._p("repo", "s")
        atomic_write_text(real, "v1")
        os.chmod(real, 0o640)
        link = self._p("s")
        os.symlink(real, link)
        atomic_write_text(link, "v2")
        self.assertEqual(stat.S_IMODE(os.stat(real).st_mode), 0o640)

    def test_temp_names_are_recognisable_to_directory_sweepers(self):
        """search.py's cache reaper skips in-flight writes by name. It used to test
        endswith(".tmp"); the unique temps are "<target>.tmp.<random>", so the predicate has to
        know both spellings or the reaper deletes a save in progress."""
        from quintessence.atomicio import is_temp_name
        seen = []
        import quintessence.atomicio as mod
        real_open_tmp = mod._open_unique_temp

        def spy(*a, **k):
            fd, path = real_open_tmp(*a, **k)
            seen.append(os.path.basename(path))
            return fd, path

        with unittest.mock.patch.object(mod, "_open_unique_temp", spy):
            atomic_write_text(self._p("cache.json"), "x")
        self.assertTrue(seen and is_temp_name(seen[0]), f"sweeper would not skip {seen}")
        self.assertTrue(is_temp_name("cache.json.tmp"), "legacy spelling must still be recognised")
        self.assertFalse(is_temp_name("cache.json"), "a real file must not be mistaken for a temp")

    def test_the_delete_side_predicate_recognises_only_our_own_temps(self):
        """Seventh pass, F2. Skipping one file too many is free; DELETING one is not. So the
        broad predicate guards the skip and this exact one guards the removal.

        The real name is captured from the writer rather than spelled out here, so that widening
        or narrowing the generated tail cannot leave this test asserting against a shape nothing
        produces — which is how the reaper's own fixture came to use a six-character token.

        The RANDOMNESS is pinned (`_letter_bearing_token`) while the name still comes from the
        writer. Left free, "the writer's own temp must be reclaimable" is not true of every temp
        the writer emits: the predicate wants a hex letter and about one tail in 281 has none, so
        this assertion flaked from the day that condition landed (D77). The all-decimal tail is
        pinned in the other direction, once, by
        `test_a_temp_the_writer_really_wrote_with_an_all_decimal_tail_is_not_reclaimed` — one
        authoritative pin each way, neither of them a coin flip."""
        from quintessence.atomicio import is_generated_temp_name, is_temp_name
        import quintessence.atomicio as mod
        seen = []
        real_open_tmp = mod._open_unique_temp

        def spy(*a, **k):
            fd, path = real_open_tmp(*a, **k)
            seen.append(os.path.basename(path))
            return fd, path

        with unittest.mock.patch.object(mod.os, "urandom",
                                        lambda n: _letter_bearing_token(n, 0)), \
                unittest.mock.patch.object(mod, "_open_unique_temp", spy):
            atomic_write_text(self._p("cache.json"), "x")
        self.assertTrue(seen and is_generated_temp_name(seen[0]),
                        f"the writer's own temp must be reclaimable: {seen}")
        # The legacy spelling is deliberately NOT claimed by this predicate: it carries no
        # generated tail, so only a caller that knows the target basename may claim it, which
        # _reclaim_stale_temps does by exact match. A directory sweep must not (twelfth pass, F1).
        self.assertFalse(is_generated_temp_name("cache.json.tmp"),
                         "the legacy spelling has no generated tail and is not claimable by name")
        # mkstemp's tail, from the build that existed between 27dff8a and 73e2050. This used to
        # assert True, on the rationale that such temps could be on disk after an upgrade. They
        # cannot: that code was never on any install but the author's own mirror, which the
        # reflog puts at those commits from 10:40 to 15:54 on 2026-08-03 (eighteenth pass, F5).
        # The window it bought cost the shapes below, which operators really do have.
        self.assertFalse(is_generated_temp_name("cache.json.tmp.Ab3_x9Zq"),
                         "an 8-character mixed-case tail is not a name this module can write, and "
                         "no install exists that could hold one")

        # The reviewer's measured set: every one of these satisfied the old 8+ window and was
        # deleted an hour after it was written, in a directory whose age policy is 60 days.
        # `notes.tmp.markdown` is the sharpest — it died beside a target called `notes` while
        # `notes.tmp.md`, the case the docstring highlighted protecting, survived. A rule that
        # protects the two-letter extension and not the eight-letter one is drawn in the wrong
        # place, whatever its floor.
        # `settings.json.tmp.202608041200` is `date +%Y%m%d%H%M`, and twelve digits are twelve
        # valid lowercase hex characters — so the width test alone claimed it, and an operator's
        # `cp settings.json settings.json.tmp.$(date +%Y%m%d%H%M)` died an hour later (twenty-
        # first pass, F2). The predicate now also requires at least one of `abcdef`.
        for foreign in ("notes.tmp.md", "draft.tmp.json", "sheet.tmp.bak", "x.tmp.swp",
                        "report.tmp.20260804", "backup.tmp.snapshot", "other.tmp.a1b2c3d4",
                        "notes.tmp.markdown", "config.tmp.original",
                        "settings.json.tmp.202608041200", "cache.json.tmp.000000000000"):
            self.assertFalse(is_generated_temp_name(foreign),
                             f"{foreign} is not ours and must not be deleted as litter")
            self.assertTrue(is_temp_name(foreign),
                            f"{foreign} must still be SKIPPED — breadth is safe on the skip side")

        # Length is exact on BOTH sides, not a floor: one hex short and one hex long are equally
        # not-ours. A floor is what let the 8-character shapes in, so the assertion that replaced
        # it has to separate the right answer from the plausible wrong ones in both directions.
        self.assertFalse(is_generated_temp_name("cache.json.tmp.a1b2c3d4e5f"),
                         "eleven hex is not a name this module writes")
        self.assertFalse(is_generated_temp_name("cache.json.tmp.a1b2c3d4e5f6a"),
                         "thirteen hex is not a name this module writes")
        self.assertFalse(is_generated_temp_name("cache.json.tmp.A1B2C3D4E5F6"),
                         "os.urandom(6).hex() is lowercase; uppercase is somebody else's name")

    def test_a_chmod_refusing_filesystem_does_not_break_or_strand_the_write(self):
        """V1 from the sandboxed pass: the mode carry-across sat OUTSIDE the cleanup try, so on a
        filesystem with no unix modes (vfat, exfat, some CIFS/NFS) chmod's refusal aborted a write
        that used to succeed AND left the temp behind, cleanup never having been armed."""
        p = self._p("conf")
        atomic_write_text(p, "OLD")
        with unittest.mock.patch("os.chmod", side_effect=PermissionError(1, "not permitted")):
            atomic_write_text(p, "NEW")          # must not raise
        with open(p) as fh:
            self.assertEqual(fh.read(), "NEW")
        self.assertEqual(os.listdir(self.tmp), ["conf"], "no temp may be stranded")

    def test_an_explicit_mode_sets_a_fresh_file_and_never_a_rewrite(self):
        """`mode=` exists for a caller copying a file, whose source mode must travel with the
        contents — `atomic_write` has nothing to stat when the destination is absent, which is
        how the D4 migration silently republished a 0600 cache at 0644 (fourteenth pass, F1).

        Two properties, and the second is the one that keeps the older promise: a FRESH file
        takes the argument (asserted under umask 022, so the umask default 0644 is a different
        value and cannot pass for the answer), and a REWRITE keeps the destination's own mode
        even when a caller passes one — a rewrite must never change permissions."""
        old = os.umask(0o022)
        try:
            fresh = self._p("copied")
            with atomic_write(fresh, mode=0o600) as fh:
                fh.write("payload")
            self.assertEqual(stat.S_IMODE(os.stat(fresh).st_mode), 0o600,
                             "a fresh file must take the mode the caller carried over, not the "
                             "umask default 0644")

            existing = self._p("existing")
            atomic_write_text(existing, "v1")
            os.chmod(existing, 0o640)
            with atomic_write(existing, mode=0o666) as fh:
                fh.write("v2")
            self.assertEqual(stat.S_IMODE(os.stat(existing).st_mode), 0o640,
                             "an existing destination's mode outranks the argument — a rewrite "
                             "may not widen permissions")
            with open(existing) as fh:
                self.assertEqual(fh.read(), "v2")
        finally:
            os.umask(old)

    def test_a_fresh_file_honours_the_umask(self):
        """A fresh file must get 0o666 & ~umask, exactly as plain open() would.

        TWO umasks, and the second is the one that earns its keep. umask 027 -> 0640 catches a
        hardcoded 0600, which is what mkstemp creates anyway; the first version of this test used
        umask 077 and asserted 0600, so it passed whether or not the mode logic existed at all
        (sixth pass, F2).

        But 027 masks BOTH 0o666 and 0o644 to 0640, so on its own it cannot see the create mode
        being narrowed -- and narrowing it silently drops group write for an operator on umask 002.
        That is ninth pass, F3, the third appearance of this one shape. umask 002 separates them:
        0o666 -> 0664, 0o644 -> 0644. The general form, which the previous docstring got wrong by
        claiming 0640 "differs from every default in play": an assertion has to distinguish the
        correct value from the plausible WRONG ones, not merely from the defaults."""
        for mask, expected in ((0o027, 0o640), (0o002, 0o664)):
            old = os.umask(mask)
            try:
                p = self._p(f"fresh{mask:03o}")
                atomic_write_text(p, "x")
                self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), expected,
                                 f"under umask {mask:03o} a fresh file must be 0o666 & ~umask "
                                 f"(={expected:04o}), not a hardcoded or narrowed mode")
            finally:
                os.umask(old)

    def test_the_process_umask_is_never_mutated(self):
        """os.umask is process-global and there is no read-only form, so READING it by setting
        and restoring races every other thread — and any child forked in the window inherits the
        wrong mask for its whole lifetime. Reproduced under umask 077: a concurrent thread
        created a file 0o644. The mode must come from the kernel instead, so nothing here may
        call os.umask at all. Deterministic: make the call itself fatal."""
        def forbidden(*a, **k):
            raise AssertionError("atomicio must not touch the process-global umask")

        with unittest.mock.patch("os.umask", forbidden):
            atomic_write_text(self._p("f"), "x")            # fresh
            atomic_write_text(self._p("f"), "y")            # rewrite
            atomic_write_json(self._p("j"), {"k": 1})
        with open(self._p("f")) as fh:
            self.assertEqual(fh.read(), "y")

    def test_a_failing_fdopen_strands_no_temp(self):
        """F1 from the fifth pass: mkstemp and fdopen sat OUTSIDE the cleanup try, so an fdopen
        failure left the temp behind — the same structural flaw as the chmod placement, on the
        lines immediately above it."""
        with self.assertRaises(LookupError):
            atomic_write_text(self._p("f"), "x", encoding="not-a-real-codec")
        self.assertEqual(os.listdir(self.tmp), [], "a failed fdopen must leave no temp")

    def test_the_temp_is_never_created_wider_than_the_finished_file(self):
        """Sixteenth pass, F7. The comment beside the chmod claimed "the file is never visible at
        the wrong mode", and the code did not provide it: the temp was created at `0o666 & ~umask`
        and narrowed afterwards. Instrumented at the reviewed tip, `secret.tmp.*` was 0o644 at
        creation and 0o600 after — and a reader who opened it inside that window keeps its access
        while the payload streams in. The destination's mode is now read BEFORE the temp is
        created and passed to `os.open`, so the claim is true rather than corrected away.

        The mode is caught AT CREATION, from inside a spy on `_open_unique_temp` — after the fact
        every version of this code looks identical. umask 022 separates the answers: a 0600
        destination gives 0600 if the mode is carried into the create and 0644 (`0o666 & ~022`) if
        it is not, which is exactly the old behaviour."""
        import quintessence.atomicio as mod
        real_open_tmp = mod._open_unique_temp
        at_creation = []

        def spy(*a, **k):
            fd, path = real_open_tmp(*a, **k)
            at_creation.append(stat.S_IMODE(os.stat(path).st_mode))
            return fd, path

        old = os.umask(0o022)
        try:
            secret = self._p("secret")
            atomic_write_text(secret, "v1")
            os.chmod(secret, 0o600)
            with unittest.mock.patch.object(mod, "_open_unique_temp", spy):
                atomic_write_text(secret, "v2")
                fresh = self._p("copied")
                with atomic_write(fresh, mode=0o600) as fh:
                    fh.write("payload")
        finally:
            os.umask(old)

        self.assertEqual(len(at_creation), 2, "expected one temp per write")
        self.assertEqual(at_creation[0], 0o600,
                         "a rewrite's temp must be BORN at the destination's mode (0o600), not at "
                         "0o644 and narrowed after the payload is already streaming")
        self.assertEqual(at_creation[1], 0o600,
                         "and a copy's temp at the mode its caller carried over")
        self.assertEqual(stat.S_IMODE(os.stat(secret).st_mode), 0o600,
                         "the finished rewrite still keeps the destination's mode")

    def test_the_chmod_runs_before_the_caller_writes_a_byte(self):
        """The other end of the same window, which the test above cannot see (twenty-third pass,
        F6). That one spies the mode AT CREATION, so moving `os.chmod(tmp, carry)` to after
        `yield fh` leaves it green — and the whole suite with it. Not a live defect: the create
        already carries the mode, so the mutation makes the in-flight temp NARROWER than the
        finished file and the promise beside the code ("the payload is never readable at a mode
        wider than the one the destination ends up with") still holds. It is recorded and pinned
        because the promise then rests on the create alone: revert the create to a constant, as it
        was until `6bb1384`, and the ordering is load-bearing again with nothing holding it.

        WHAT SEPARATES THE TWO ORDERINGS. The create applies `carry & ~umask`; the chmod puts back
        what the umask took. So the two differ only when the umask actually removes a bit, and the
        test has to arrange that: umask 077 against a 0o644 destination gives a create at 0o600
        and a chmod back to 0o644. The mode is read INSIDE the `with` block, from the temp the
        spy captured — the one moment the two orderings look different, since after the
        `os.replace` every version of this code leaves 0o644 behind.
        """
        import quintessence.atomicio as mod
        real_open_tmp = mod._open_unique_temp
        temps = []

        def spy(*a, **k):
            fd, path = real_open_tmp(*a, **k)
            temps.append(path)
            return fd, path

        old = os.umask(0o077)
        try:
            target = self._p("carried")
            atomic_write_text(target, "v1")
            os.chmod(target, 0o644)
            with unittest.mock.patch.object(mod, "_open_unique_temp", spy):
                with atomic_write(target) as fh:
                    in_flight = stat.S_IMODE(os.stat(temps[-1]).st_mode)
                    fh.write("v2")
        finally:
            os.umask(old)

        self.assertEqual(len(temps), 1, "expected one temp for the rewrite")
        self.assertEqual(in_flight, 0o644,
                         "while the caller was writing, the temp was still at `carry & ~umask` "
                         "(0o600) — the chmod that puts the umask's bits back must run BEFORE the "
                         "yield, not after it")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o644,
                         "and the finished file keeps the destination's mode either way, which "
                         "is exactly why the assertion above has to be taken mid-write")

    def test_the_name_length_envelope_is_refused_at_its_edge_not_narrowed_silently(self):
        """Sixteenth pass, F8; the top of the band added at the seventeenth, F2. A temp name is 17
        bytes longer than its target (`.tmp.` + 12 hex), where the idiom this module replaced spent
        4 — so an install whose target name sits in the last 17 bytes of NAME_MAX gets ENAMETOOLONG
        from a write a plain `open()` still performs, and the last 13 of those are names a qq that
        used to work could write. Reachable through the five settings that name a file outright,
        `QQ_CACHE` above all, because identity-scoping and `_orphan_ages_path` spend another 58 on
        top of it — 63 when the configured name carries no extension. (That "18" was this
        docstring's own arithmetic error, alongside the document's; the sidecar suffix is 17 and
        the two derived totals are now read back against the code two tests below.)

        The band's TOP is asserted here because it was stated wrongly and nothing measured it: the
        paragraph in ASSURANCE.md said 239-251, with 252 "beyond the filesystem either way", and
        252 is nothing of the kind. `open()` runs to NAME_MAX; 251 was where the replaced idiom
        stopped, not where the filesystem does. This test used `edge + 1` as its only control,
        which is the right control for the bottom of the band and cannot see the top at all.

        The decision is to REFUSE with the number rather than truncate the name to fit: a
        truncated temp would break the prefix rule `_reclaim_stale_temps` runs on and the
        `<target>.tmp.<tail>` shape ASSURANCE.md tells operators to recognise. So what is pinned
        here is the boundary itself and the quality of the refusal.

        The numbers come from the filesystem and from the module, not from this file: NAME_MAX by
        pathconf (255 on ext4, tmpfs, xfs and btrfs, but this test should be right on a filesystem
        where it is not) and the overhead from `_TEMP_NAME_OVERHEAD`, which is derived from the
        infix and the token width. The plain-open control on the same name is what makes this an
        envelope rather than a filesystem limit: at one byte past the edge, `open()` still works
        and only the atomic write refuses."""
        import quintessence.atomicio as mod
        limit = os.pathconf(self.tmp, "PC_NAME_MAX")
        edge = limit - mod._TEMP_NAME_OVERHEAD

        atomic_write_text(self._p("a" * edge), "x")
        self.assertTrue(os.path.exists(self._p("a" * edge)),
                        f"a {edge}-byte name is the longest that leaves room for its own temp "
                        f"({mod._TEMP_NAME_OVERHEAD} bytes) inside NAME_MAX {limit}")

        too_long = "b" * (edge + 1)
        with self.assertRaises(OSError) as ctx:
            atomic_write_text(self._p(too_long), "x")
        self.assertEqual(ctx.exception.errno, errno.ENAMETOOLONG)
        message = str(ctx.exception)
        for number in (str(mod._TEMP_NAME_OVERHEAD), str(edge + 1), str(limit), str(edge)):
            self.assertIn(number, message,
                          f"the refusal must name the budget it enforces — {number} is missing "
                          f"from {message!r}, and an operator cannot act on 'File name too long' "
                          f"for a path they did not write")
        self.assertEqual(os.listdir(self.tmp), ["a" * edge],
                         "a refused write must leave nothing behind")

        with open(self._p(too_long), "w") as fh:      # the control: not the filesystem's limit
            fh.write("x")
        self.assertTrue(os.path.exists(self._p(too_long)),
                        "this name is fine for a plain open — the 17 bytes are ours, which is "
                        "why the refusal has to explain itself rather than pass the kernel's "
                        "message along")

        # The TOP of the band, and one byte past it. At NAME_MAX itself the atomic write still
        # refuses and `open()` still works, so the whole of edge+1..NAME_MAX is ours; at
        # NAME_MAX+1 `open()` fails too, and there the filesystem is the one saying no.
        at_limit = "c" * limit
        with self.assertRaises(OSError) as ctx:
            atomic_write_text(self._p(at_limit), "x")
        self.assertEqual(ctx.exception.errno, errno.ENAMETOOLONG)
        with open(self._p(at_limit), "w") as fh:
            fh.write("x")
        self.assertTrue(os.path.exists(self._p(at_limit)),
                        f"a plain open() runs all the way to NAME_MAX ({limit}) — the band this "
                        f"module narrows is {edge + 1}-{limit}, not {edge + 1}-251")
        with self.assertRaises(OSError) as ctx:
            with open(self._p("d" * (limit + 1)), "w") as fh:
                fh.write("x")
        self.assertEqual(ctx.exception.errno, errno.ENAMETOOLONG,
                         "one byte past NAME_MAX is the filesystem's refusal, not ours")

        # And the old idiom's edge, which is what 251 actually was. `<target>.tmp` costs 4, so it
        # reached NAME_MAX - 4 and no further; asserting it here keeps the document's two numbers
        # attached to the two different limits they came from.
        legacy_edge = limit - len(".tmp")
        with open(self._p("e" * legacy_edge + ".tmp"), "w") as fh:
            fh.write("x")
        with self.assertRaises(OSError):
            with open(self._p("f" * (legacy_edge + 1) + ".tmp"), "w") as fh:
                fh.write("x")

    def test_the_documented_band_is_the_band_this_module_has(self):
        """Seventeenth pass, F2. The band above was measured and then written down wrong, and
        nothing read the two back against each other — a measured claim has to carry its harness
        or it is only a claim. So the three numbers in ASSURANCE.md's paragraph are read out of
        the document and checked against what the module and the filesystem actually give.

        WHAT THIS DOES NOT REACH. Only the three numbers in that one sentence: the surrounding
        prose about which band is a narrowing and which was never reachable is not machine-read.
        The QQ_CACHE arithmetic in the paragraph below it went ungated for the same reason and
        drifted (twentieth pass, F1); the test after this one now reads it. And it can only run
        where `NAME_MAX` is 255, which is what the document says it is describing — on a
        filesystem where it is not, the document's numbers are simply about a different one, so
        the comparison skips rather than failing."""
        import quintessence.atomicio as mod
        limit = os.pathconf(self.tmp, "PC_NAME_MAX")
        if limit != 255:
            self.skipTest(f"ASSURANCE's paragraph states the NAME_MAX 255 case; here it is {limit}")
        with open(ASSURANCE_MD, encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"the longest basename qq can write atomically is \*\*(\d+) bytes\*\*\.\s+"
                      r"From (\d+) to (\d+)\s+a plain `open\(\)` still succeeds and this refuses",
                      text)
        self.assertIsNotNone(
            m, "ASSURANCE.md no longer states the name-length band in a form this test can read. "
               "If the wording changed, teach the reader here — do not leave the numbers ungated.")
        documented_edge, documented_low, documented_high = (int(g) for g in m.groups())
        self.assertEqual(documented_edge, limit - mod._TEMP_NAME_OVERHEAD,
                         "the documented longest atomically-writable basename is not what the "
                         "module's own overhead leaves")
        self.assertEqual(documented_low, documented_edge + 1)
        self.assertEqual(documented_high, limit,
                         "the band's top is NAME_MAX — a plain open() runs the whole way there, "
                         "which is exactly what the earlier '239 to 251' got wrong")

    def test_the_documented_QQ_CACHE_budget_is_the_arithmetic_the_code_performs(self):
        """Twentieth pass, F1. The paragraph gave ONE overhead (58) and ONE last-working
        basename (180), and both are the with-an-extension case only. `identity_cache_path`
        splits the configured name and does `ext = ext or ".json"` (search.py), so an
        EXTENSIONLESS `QQ_CACHE` also collects five bytes it never asked for: the derived
        sidecar is 63 bytes longer than the configured name, not 58, and an extensionless 180 —
        one byte inside the number the document gave — loses its sidecar on every build. The
        document's own sentence said the arithmetic was ungated, and it drifted, which is the
        finding twice over.

        WHAT IS DERIVED, AND FROM WHOM. Nothing here is spelled by hand. The overheads come from
        `SearchIndex._orphan_ages_path()` on two probe names that differ only in having an
        extension, so the identity's cost, the sidecar suffix's cost and the supplied
        extension's cost each fall out of a subtraction between real derived names. The budget
        edge comes from `pathconf` and `_TEMP_NAME_OVERHEAD`, as the band test above. The embed
        model is the registry DEFAULT — no override — because "measured with the default embed
        model" is what the paragraph claims, and the identity carries the model name.

        AND THEN THE ARITHMETIC IS SPENT, at the filesystem. Believing a subtraction is how the
        58 got written down in the first place: for both spellings the documented last-working
        basename is CONSTRUCTED, its real sidecar path derived, and the atomic write performed.
        One byte more must raise this module's own `NameTooLong` on the sidecar while the
        identity cache beside it still writes — "fails on the sidecar write" is the document's
        claim, and a name that failed everywhere would satisfy a laxer assertion while meaning
        something else entirely.

        WHAT THIS DOES NOT REACH. The other four path settings that name a file outright
        (`QQ_CONFIG`, `QQ_RECONCILE_SNAPSHOT`, `QQ_XREF_CONTENT`, `QQ_XREF_WAVEOFFS`) derive
        nothing on top of what you configure, so they have no arithmetic to check; if one ever
        grows a derived name this test will not notice. And like its neighbour it can only run
        where `NAME_MAX` is 255 — the case the paragraph says it is describing."""
        import quintessence.atomicio as mod
        from quintessence.config import Config
        from quintessence.search import SearchIndex

        limit = os.pathconf(self.tmp, "PC_NAME_MAX")
        if limit != 255:
            self.skipTest(f"ASSURANCE's paragraph states the NAME_MAX 255 case; here it is {limit}")
        edge = limit - mod._TEMP_NAME_OVERHEAD
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)   # this one writes real files

        def index_for(configured_basename):
            cfg = Config(env={}, config_file="/nonexistent",
                         overrides={"QQ_CACHE": os.path.join(self.tmp, configured_basename),
                                    "QQ_KB_ROOT": self.tmp})
            return SearchIndex(cfg)

        with_ext, without_ext = index_for("probe.json"), index_for("probe")
        self.assertEqual(with_ext.embed_model, without_ext.embed_model,
                         "fixture guard: both probes must carry the SAME (default) model, or the "
                         "difference measured below is the model's and not the extension's")
        overhead = {"with": len(os.path.basename(with_ext._orphan_ages_path())) - len("probe.json"),
                    "without": len(os.path.basename(without_ext._orphan_ages_path())) - len("probe")}
        identity_cost = len(os.path.basename(with_ext.cache_path)) - len("probe.json")
        sidecar_cost = (len(os.path.basename(with_ext._orphan_ages_path()))
                        - len(os.path.basename(with_ext.cache_path)))
        extension_cost = overhead["without"] - overhead["with"]
        self.assertGreater(extension_cost, 0,
                           "fixture guard: this test exists because the two spellings cost "
                           "different amounts — if they no longer do, the paragraph it reads "
                           "needs rewriting, not re-checking")

        with open(ASSURANCE_MD, encoding="utf-8") as f:
            flat = " ".join(f.read().split())

        totals = re.search(r"runs \*\*(\d+) bytes\*\* longer than the one you configure when that "
                           r"name has an extension, and \*\*(\d+) bytes\*\* longer when it does not",
                           flat)
        parts = re.search(r"(\d+) bytes for the identity, (\d+) for the sidecar, and (\d+) more "
                          r"for the extension", flat)
        last = re.search(r"the last configured cache basename that works is \*\*(\d+) bytes\*\* "
                         r"with an extension and \*\*(\d+) bytes\*\* without one", flat)
        for name, m in (("the two overheads", totals), ("their three components", parts),
                        ("the two last-working basenames", last)):
            self.assertIsNotNone(
                m, f"ASSURANCE.md no longer states {name} in a form this test can read. If the "
                   f"wording changed, teach the reader here — that paragraph drifted once "
                   f"already while nothing was reading it.")

        self.assertEqual([int(g) for g in totals.groups()],
                         [overhead["with"], overhead["without"]],
                         "the documented overheads are not what identity-scoping plus the "
                         "orphan-ages sidecar actually add")
        self.assertEqual([int(g) for g in parts.groups()],
                         [identity_cost, sidecar_cost, extension_cost],
                         "the components do not add up to the overheads the code produces")
        self.assertEqual([int(g) for g in last.groups()],
                         [edge - overhead["with"], edge - overhead["without"]],
                         "the documented last-working basenames are not what the budget leaves")

        # Spend it. The document's numbers, turned back into names and written.
        for documented, spelling in zip((int(g) for g in last.groups()), (".json", "")):
            works = "c" * (documented - len(spelling)) + spelling
            fails = "c" * (documented + 1 - len(spelling)) + spelling
            self.assertEqual(len(works), documented, "fixture guard: the configured basename "
                                                     "must be the length the document quotes")
            ok = index_for(works)
            atomic_write_text(ok._orphan_ages_path(), "{}")
            self.assertTrue(os.path.exists(ok._orphan_ages_path()),
                            f"a configured basename of {documented} bytes is documented to work")
            over = index_for(fails)
            atomic_write_text(over.cache_path, "{}")
            self.assertTrue(os.path.exists(over.cache_path),
                            "the identity cache beside it must still write — the document says "
                            "the SIDECAR is what fails, and a name too long for both would be a "
                            "different (and louder) problem")
            with self.assertRaises(mod.NameTooLong):
                atomic_write_text(over._orphan_ages_path(), "{}")

    def test_a_too_long_PATH_is_not_claimed_as_a_too_long_BASENAME(self):
        """Nineteenth pass, F4. `_name_budget_error`'s `None` return — the whole reason its
        docstring argues the NAME_MAX-versus-PATH_MAX distinction at length — was pinned by
        nothing: forcing the function to claim the budget error every time left every suite green.

        ENAMETOOLONG has two sources. A basename with no room for its temp beside it is this
        module's own refusal and gets the arithmetic. A whole PATH over PATH_MAX is somebody
        else's problem with somebody else's fix, and answering it with a basename budget tells an
        operator to shorten a name that is already one byte long — a wrong-fix message, which is
        worse than passing the kernel's own through.

        THE FIXTURE IS THE SECOND CASE, END TO END rather than by calling the helper: a directory
        deep enough that the target path fits inside PATH_MAX and the target plus a temp's 17
        bytes does not. That is the operator's shape exactly — deep tree, short basename — and it
        reaches `_open_unique_temp`'s ENAMETOOLONG handler the way a real write does, which a
        direct call to the helper would not prove.

        The controls come after the measurement, on purpose: `open()` on the same path succeeds
        (so the refusal is about the temp's bytes, not the path), and a genuinely over-long
        BASENAME in a shallow directory still gets the budget message (so this is discrimination
        and not a helper that has stopped speaking)."""
        import quintessence.atomicio as mod
        path_max = os.pathconf(self.tmp, "PC_PATH_MAX")
        name_max = os.pathconf(self.tmp, "PC_NAME_MAX")
        deep = tempfile.mkdtemp(dir=self.tmp)
        self.addCleanup(shutil.rmtree, deep, ignore_errors=True)

        # 4 bytes of margin under PATH_MAX for the target; the temp's 17 put the candidate over.
        want = path_max - 1 - 4 - len(os.sep + "f")
        remaining = want - len(deep)
        self.assertGreater(remaining, 1, "the temp dir is already too deep to build this fixture")
        while remaining > 0:
            take = min(201, remaining)          # one component costs 1 separator + at least 1 byte
            if remaining - take == 1:
                take -= 1
            deep = os.path.join(deep, "d" * (take - 1))
            os.mkdir(deep)
            remaining -= take

        target = os.path.join(deep, "f")
        self.assertLess(len(target), path_max, "fixture guard: the TARGET must be a path the "
                                               "kernel accepts, or this measures nothing")
        self.assertGreater(len(target) + mod._TEMP_NAME_OVERHEAD, path_max - 1,
                           "fixture guard: the target plus a temp's overhead must be the thing "
                           "that overflows, which is what makes this a PATH_MAX case")
        self.assertLess(len(os.path.basename(target)) + mod._TEMP_NAME_OVERHEAD, name_max,
                        "fixture guard: the BASENAME must be comfortably inside its own budget, "
                        "or the two answers are both right and nothing is discriminated")

        with self.assertRaises(OSError) as ctx:
            atomic_write_text(target, "x")
        self.assertEqual(ctx.exception.errno, errno.ENAMETOOLONG)
        self.assertNotIsInstance(
            ctx.exception, mod.NameTooLong,
            f"a PATH_MAX failure was raised as this module's basename refusal. The basename here "
            f"is {len(os.path.basename(target))} byte(s) against a NAME_MAX of {name_max}: an "
            f"operator told to shorten it has been sent to the wrong fix, and the one that would "
            f"work — a shallower directory — is not mentioned")
        self.assertNotIn("temp-name room", str(ctx.exception),
                         "and the budget arithmetic must not be in the message either")

        with open(target, "w") as fh:           # the control: the PATH itself is fine
            fh.write("x")
        self.assertTrue(os.path.exists(target),
                        "a plain open() on this path succeeds — the bytes that overflow are the "
                        "temp's, which is why the atomic write is the one that refuses")

        shallow = self._p("g" * (name_max - mod._TEMP_NAME_OVERHEAD + 1))
        with self.assertRaises(mod.NameTooLong) as ctx:
            atomic_write_text(shallow, "x")
        self.assertIn("temp-name room", str(ctx.exception),
                      "a genuinely over-long BASENAME must still get the budget arithmetic — "
                      "a helper that answered None to everything would pass the assertions above")

    def test_a_refused_name_is_its_own_error_class_not_a_bare_OSError(self):
        """Seventeenth pass, F1. The refusal above is only loud if a caller can tell it apart.

        Two call sites write bookkeeping best-effort — `except OSError: pass`, because a sidecar
        must not fail the search that produces it — and that handler swallowed this refusal along
        with the transient failures it was written for. So the refusal carries its own class.

        Both halves matter and are asserted here. It IS an OSError, or every existing handler
        would change behaviour the day this landed; and it is a PROPER subclass, or no handler
        can single it out. The errno stays ENAMETOOLONG so a caller matching on the number is
        unaffected either way."""
        import quintessence.atomicio as mod
        limit = os.pathconf(self.tmp, "PC_NAME_MAX")
        too_long = "b" * (limit - mod._TEMP_NAME_OVERHEAD + 1)

        with self.assertRaises(mod.NameTooLong) as ctx:
            atomic_write_text(self._p(too_long), "x")
        self.assertIsInstance(ctx.exception, OSError,
                              "every caller's `except OSError` must keep catching this")
        self.assertIsNot(type(ctx.exception), OSError,
                         "and a best-effort caller must be able to single it out, which a bare "
                         "OSError cannot be")
        self.assertEqual(ctx.exception.errno, errno.ENAMETOOLONG)

    def test_a_best_effort_write_stays_quiet_on_a_hiccup_and_speaks_up_on_a_name(self):
        """Seventeenth pass, F1. The handler that keeps housekeeping from failing the work.

        Three behaviours, and the middle one is what separates this fix from the wrong one:
        warning on EVERY OSError would also pass the two tests around it, so the ENOSPC case is
        asserted silent. A name that cannot work is a standing property of the configuration and
        gets a line; a full disk may be gone by the next run and gets the old silence; anything
        that is not an OSError at all was never this handler's business and propagates.

        The stderr line must carry the arithmetic, not just the fact — an operator who is told
        'not written' and not told the budget has to go and find it."""
        import quintessence.atomicio as mod
        limit = os.pathconf(self.tmp, "PC_NAME_MAX")
        edge = limit - mod._TEMP_NAME_OVERHEAD
        too_long = self._p("b" * (edge + 1))

        err = io.StringIO()
        with unittest.mock.patch.object(sys, "stderr", err):
            with mod.best_effort_write("orphan-ages sidecar", too_long):
                atomic_write_text(too_long, "x")
        line = err.getvalue()
        self.assertEqual(line.count("\n"), 1, f"one line, not a stack trace: {line!r}")
        self.assertIn("orphan-ages sidecar", line, "the message must name what was not written")
        self.assertIn(too_long, line, "and which file")
        for number in (str(mod._TEMP_NAME_OVERHEAD), str(edge + 1), str(limit), str(edge)):
            self.assertIn(number, line,
                          f"the warning must carry the primitive's arithmetic — {number} is "
                          f"missing from {line!r}")

        err = io.StringIO()
        with unittest.mock.patch.object(sys, "stderr", err):
            with mod.best_effort_write("orphan-ages sidecar", self._p("short")):
                raise OSError(errno.ENOSPC, "No space left on device")
        self.assertEqual(err.getvalue(), "",
                         "a transient failure keeps the silence this handler was written for — "
                         "warning on every OSError would make a full disk noisy in the middle of "
                         "a search and would not be this fix")

        with self.assertRaises(ValueError):
            with mod.best_effort_write("orphan-ages sidecar", self._p("short")):
                raise ValueError("not an OSError")

    def test_a_failing_fdopen_closes_the_descriptor_exactly_once(self):
        """Sixteenth pass, F3. `os.fdopen` is `io.open(fd, ...)` with closefd true, and io.open
        closes the descriptor itself when it fails part-way. The `fd = None` handoff ran only on
        success, so the cleanup then closed the same number a SECOND time — two closes on fd 3,
        the second EBADF, suppressed and invisible. Closing a number the process no longer owns
        is not a no-op in the MCP servers, which run sync tool handlers in a threadpool: the
        number can already have been reissued to another thread's file.

        In-tree reachability is nil today — all fourteen call sites use the default utf-8 — so
        this is latent, and the trigger here is the same bad `encoding=` the reviewer used.

        The negative ("we never closed it") is only worth something if the counter could have
        recorded a close at all, so the control below closes a descriptor under the same patch
        and requires the counter to see it. The positive half is asserted too: the descriptor
        really is closed, by fdopen — exactly once, not zero times."""
        import quintessence.atomicio as mod
        real_open_tmp = mod._open_unique_temp
        real_close = os.close
        handed, closed = [], []

        def spy(*a, **k):
            fd, path = real_open_tmp(*a, **k)
            handed.append(fd)
            return fd, path

        def counting_close(fd):
            closed.append(fd)
            return real_close(fd)

        with unittest.mock.patch.object(mod, "_open_unique_temp", spy), \
                unittest.mock.patch("os.close", counting_close):
            with self.assertRaises(LookupError):
                atomic_write_text(self._p("f"), "x", encoding="not-a-real-codec")
            # Snapshot BEFORE the control runs: a freed descriptor number is immediately
            # reusable, and the control's own open took the very number under test on the first
            # run of this test, which made the control look like a double close.
            during_the_write = list(closed)
            with self.assertRaises(OSError) as ctx:
                os.fstat(handed[0])
            control_fd, control_path = real_open_tmp(self.tmp, "control")
            os.close(control_fd)
            os.unlink(control_path)

        self.assertEqual(len(handed), 1, f"expected one temp descriptor, got {handed}")
        self.assertIn(control_fd, closed,
                      "the control: this counter must be able to record a close, or the "
                      "assertion below is a negative that cannot fail")
        self.assertEqual(during_the_write.count(handed[0]), 0,
                         f"fd {handed[0]} was handed to fdopen, which owns it from that moment "
                         f"and closes it on its own failure — closing it again here is a close "
                         f"of whatever the number now refers to")
        self.assertEqual(ctx.exception.errno, errno.EBADF,
                         "and it must really have been closed, once, by fdopen — a descriptor "
                         "left open would be a leak, the other half of this defect")

    def test_an_interrupt_during_the_cleanup_still_removes_the_temp(self):
        """Sixteenth pass, F1. The cleanup loop suppressed `Exception`, which does not cover
        `KeyboardInterrupt` — so an operator's second Ctrl-C landing in the cleanup's `fh.close()`
        flush (the ~350 MB embedding cache takes seconds to flush) escaped the loop before
        `os.unlink` ran and stranded the temp, contradicting this module's own promise that a
        failed write removes it. `except BaseException` was already the outer catch two lines
        above, so the narrow suppression was an oversight, not a decision.

        The injection is the reviewer's: a `BaseException` out of `fh.close()`, which is the step
        that runs BEFORE the unlink and is therefore the one that can skip it. Both halves are
        asserted — the temp goes, AND the interrupt still reaches the caller rather than being
        swallowed, with the write's own failure preserved as its `__context__`."""
        p = self._p("f")
        atomic_write_text(p, "original")
        real_fdopen = os.fdopen

        class InterruptingHandle:
            def __init__(self, fh):
                self._fh = fh

            def close(self):
                self._fh.close()
                raise KeyboardInterrupt("second Ctrl-C, during the cleanup flush")

            def __getattr__(self, name):
                return getattr(self._fh, name)

        with unittest.mock.patch("os.fdopen",
                                 lambda *a, **k: InterruptingHandle(real_fdopen(*a, **k))):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                with atomic_write(p) as fh:
                    fh.write("half-written")
                    raise ValueError("the write body failed")

        self.assertEqual(os.listdir(self.tmp), ["f"],
                         "an interrupt during the cleanup flush must not strand the temp — the "
                         "unlink runs after it, and unique names mean each strand is a new file")
        self.assertIsInstance(ctx.exception.__context__, ValueError,
                              "the caller's real failure must survive as context, not be replaced "
                              "outright")
        with open(p) as fh:
            self.assertEqual(fh.read(), "original",
                             "an interrupted write must not damage the old file")

    def test_an_interrupt_from_the_write_body_still_removes_the_temp(self):
        """Twenty-first pass, F3. The OUTER handler is `except BaseException`, and until this
        test nothing pinned the `BaseException` part of it.

        The test above raises `ValueError` from the body and `KeyboardInterrupt` from the
        cleanup's `fh.close()`, so the outer handler is entered through an ordinary `Exception`
        either way — narrowing it to `except Exception:` left that test green and the whole suite
        with it, while a Ctrl-C landing in the BODY (the case the module docstring names: "a
        failed write removes its temp, including when the failure is an interrupt") walked
        straight past the cleanup and stranded the temp.

        Nothing is patched here. The body raises the interrupt itself, which is what an operator
        pressing Ctrl-C during `qq check`'s write actually does."""
        p = self._p("f")
        atomic_write_text(p, "original")

        with self.assertRaises(KeyboardInterrupt):
            with atomic_write(p) as fh:
                fh.write("half-written")
                raise KeyboardInterrupt("Ctrl-C, in the write body")

        self.assertEqual(os.listdir(self.tmp), ["f"],
                         "a BODY-raised interrupt must still take the temp with it — with the "
                         "outer handler narrowed to `except Exception:` the cleanup never runs "
                         "at all, and unique names mean every Ctrl-C leaves another file")
        with open(p) as fh:
            self.assertEqual(fh.read(), "original",
                             "and the old file is left exactly as it was")

    def test_stale_sibling_temps_are_reclaimed_on_the_next_write(self):
        """F2 from the fifth pass: the first reclaim covered only the embedding cache, while
        eleven of the call sites then existing wrote into the state dir, which nothing swept at
        all. Sweeping at the primitive covers every call site and any future one. (The count is
        left in the past tense on purpose — it was true of that commit, and a live number here
        would be a fourth place for the same figure to drift.)"""
        import quintessence.atomicio as mod
        p = self._p("target")
        atomic_write_text(p, "v1")

        # Litter made by the WRITER, not spelled by hand. The previous fixtures used
        # `target.tmp.kill0` -- a five-character token `_open_unique_temp` cannot produce -- so
        # they could not exercise the predicate that decides what gets deleted, and the primitive
        # matching on prefix ALONE went unnoticed through several passes (eighth pass, F2).
        #
        # The tails are held still as well as produced by the writer (D77): the reclaim only
        # claims a tail carrying a hex letter, so litter left on the real randomness is
        # unreclaimable about one temp in 281 and "stale litter must be reclaimed" was a coin
        # flip. Distinct seeds because the forced token IS the name.
        litter = []
        for i in range(3):
            path = _writer_temp(mod, self.tmp, "target", seq=i)
            old = time.time() - 2 * DOCUMENTED_GRACE_SECONDS   # past the promised hour
            os.utime(path, (old, old))
            litter.append(path)

        # Held still too, though it survives on its AGE either way: on an all-decimal tail it
        # would survive because the predicate refuses it, and a fixture that can pass by the
        # wrong condition pins nothing about the grace.
        fresh = _writer_temp(mod, self.tmp, "target", seq=3)   # may be in flight right now

        # A DIFFERENT target's temp, and it must be a REAL one: only the PREFIX condition can
        # save this file, so its tail has to be a tail the writer emits. The fixture used to be
        # `unrelated.tmp.xyz`, whose 3-character tail `is_generated_temp_name` rejects anyway --
        # so dropping the prefix condition entirely (`mine = entry.name == legacy or
        # is_generated_temp_name(...)`) left this whole file green, and the docstring's "Two
        # conditions, not one" was pinned here by one of them (sixteenth pass, F5). The mutation
        # is real, not theoretical: with the prefix gone, a write to `target` deletes another
        # target's in-flight temp.
        other = _writer_temp(mod, self.tmp, "unrelated", seq=4)
        os.utime(other, (time.time() - 2 * DOCUMENTED_GRACE_SECONDS,) * 2)
        self.assertTrue(mod.is_generated_temp_name(os.path.basename(other)),
                        "fixture guard: this pin only has teeth while the OTHER target's temp "
                        "carries a generated tail — a tail the predicate rejects would satisfy "
                        "the assertion below by the wrong condition, which is exactly how it "
                        "stopped pinning last time")

        # A bare `<target>.tmp`. This module matched it EXACTLY until 2026-08-04, on the grounds
        # that the pre-atomicio idiom left `<path>.tmp` behind on any exception (ninth pass, F2)
        # and nothing else sweeps the state directory. The rule went on the owner's ruling
        # (twentieth pass, F2): the current writer cannot produce this name, so the clause could
        # only delete a file this package did not write -- an operator's own `cp config
        # config.tmp` reads identically to the litter it was aimed at. It is planted at the same
        # age as the litter above, so the only thing separating them is the name.
        legacy = self._p("target.tmp")
        with open(legacy, "w") as fh:
            fh.write("an operator's own backup by the pre-atomicio spelling")
        os.utime(legacy, (time.time() - 2 * DOCUMENTED_GRACE_SECONDS,) * 2)

        # Twelfth pass, F1: this one satisfied BOTH the prefix test and `endswith(".tmp")`, so
        # the wider predicate claimed an operator's own backup as litter. Only the narrow
        # generated-tail test saves it -- `backup` is not a tail this module emits.
        double = self._p("target.tmp.backup.tmp")
        with open(double, "w") as fh:
            fh.write("an operator's backup, not litter")
        os.utime(double, (time.time() - 2 * DOCUMENTED_GRACE_SECONDS,) * 2)

        # Twenty-first pass, F2: `date +%Y%m%d%H%M` is TWELVE characters and every one of them is
        # valid lowercase hex, so the width test alone claimed it and an operator's
        # `cp target target.tmp.$(date +%Y%m%d%H%M)` died an hour after they took it. Planted at
        # the same age as the litter above, so the only thing separating them is the tail's
        # alphabet: the writer's tails carry at least one of `abcdef` and this one does not.
        timestamped = self._p("target.tmp.202608041200")
        with open(timestamped, "w") as fh:
            fh.write("an operator's timestamped backup")
        os.utime(timestamped, (time.time() - 2 * DOCUMENTED_GRACE_SECONDS,) * 2)

        # An operator's own file that shares the target's prefix. Prefix-scoping alone calls this
        # ours and deletes it; only the name-shape check saves it.
        foreign = self._p("target.tmp.md")
        with open(foreign, "w") as fh:
            fh.write("notes, not litter")
        os.utime(foreign, (time.time() - 2 * DOCUMENTED_GRACE_SECONDS,) * 2)

        atomic_write_text(p, "v2")

        for f in litter:
            self.assertFalse(os.path.exists(f), f"stale litter {f} must be reclaimed")
        self.assertTrue(os.path.exists(fresh), "a fresh temp may be a write in flight")
        self.assertTrue(os.path.exists(other), "another target's temp is not ours to remove")
        self.assertTrue(os.path.exists(foreign),
                        "a file sharing the target's prefix but not the module's name shape is "
                        "the operator's, not litter -- prefix-scoping alone would delete it")
        self.assertTrue(os.path.exists(double),
                        "a file ending .tmp that also carries the target's prefix is still the "
                        "operator's -- the tail is what marks litter, not the extension")
        self.assertTrue(os.path.exists(legacy),
                        "a bare <target>.tmp must SURVIVE: this writer never produces that name, "
                        "so deleting it can only ever destroy a file of the operator's -- the "
                        "exact-match clause that did so was removed on 2026-08-04")
        self.assertTrue(os.path.exists(timestamped),
                        "a 12-DIGIT tail is valid hex by width but carries no letter, so it is "
                        "not a name this writer emits: an operator's `date +%Y%m%d%H%M` backup, "
                        "and the litter reclaimed above is the proof this test can see a delete")

    def test_a_temp_the_writer_really_wrote_with_an_all_decimal_tail_is_not_reclaimed(self):
        """The COST of the letter condition, pinned rather than only written down.

        `is_generated_temp_name` is narrower than the writer's own output: about one tail in 281
        ((10/16)**12) comes out all-decimal, and those are outside the one-hour reclaim. In the
        embedding-cache directory `QQ_CACHE_GC_DAYS` still reaches them; in the state directory
        nothing does, and the file stays. That is the price paid for not deleting an operator's
        `date +%Y%m%d%H%M` backup, and it is under-deletion — the direction chosen on purpose.

        The fixture is from the WRITER (rule 7), with the one thing forced that makes it the case
        under test: `os.urandom` handing back bytes whose hex is all digits. A hand-spelled name
        would prove nothing about what the writer can emit.
        """
        import quintessence.atomicio as mod
        p = self._p("target")
        atomic_write_text(p, "v1")

        all_decimal = bytes.fromhex("202608041200")
        self.assertEqual(len(all_decimal), mod._TEMP_TOKEN_BYTES, "fixture guard: token width")
        with unittest.mock.patch.object(mod.os, "urandom", lambda n: all_decimal):
            fd, temp = mod._open_unique_temp(self.tmp, "target")
        os.close(fd)
        self.assertTrue(os.path.basename(temp).endswith(".tmp.202608041200"),
                        f"fixture guard: the writer must have emitted the decimal tail: {temp}")
        os.utime(temp, (time.time() - 2 * DOCUMENTED_GRACE_SECONDS,) * 2)

        # Positive control in the same run: a temp with a letter in its tail, planted the same
        # way and aged the same, IS reclaimed. Without it "the file survived" cannot be told from
        # "the sweep never ran".
        with unittest.mock.patch.object(mod.os, "urandom",
                                        lambda n: bytes.fromhex("a02608041200")):
            fd, with_letter = mod._open_unique_temp(self.tmp, "target")
        os.close(fd)
        os.utime(with_letter, (time.time() - 2 * DOCUMENTED_GRACE_SECONDS,) * 2)

        atomic_write_text(p, "v2")

        self.assertFalse(os.path.exists(with_letter),
                         "control: an aged temp whose tail carries a letter must be reclaimed, "
                         "or this test cannot see a delete at all")
        self.assertTrue(os.path.exists(temp),
                        "the documented cost of the letter condition: a temp this module really "
                        "wrote is left behind about one run in 281. Change this and you have "
                        "widened "
                        "the delete rule back over `date +%Y%m%d%H%M`")

    def test_the_sweep_runs_in_the_link_target_s_directory_not_the_link_s(self):
        """Sixteenth pass, F6. ASSURANCE told an operator WHICH names die and left out WHERE. The
        reclaim runs on the RESOLVED parent, so a config symlinked into a dotfiles repository —
        the case symlink support was built for — has these deletion rules applied inside that
        repository. The document's carve-out, "anything in a directory qq does not write to",
        read as reassurance exactly where it stops applying.

        The sharp form of the claim is ONE NAME IN TWO DIRECTORIES: the same generated temp name
        beside the REAL file is this module's litter and goes, while beside the LINK it is in a
        directory qq never writes to and stays. A test that only planted the first would pass just
        as well if the sweep ran in both places.

        The pair used to be spelled `config.tmp`, which the legacy exact match claimed. That rule
        was removed on 2026-08-04 (owner's ruling, twentieth pass F2), so a bare `config.tmp`
        would now survive in BOTH directories and pin nothing about where the sweep runs — the
        pair has to carry a name the reclaim still deletes. The bare spelling is planted anyway,
        in the swept directory, as the survivor it now is: the removal has to hold in the dotfiles
        repository too, which is the place an operator's backup is most likely to be."""
        import quintessence.atomicio as mod
        os.makedirs(self._p("dotfiles"))
        real = self._p("dotfiles", "config")
        atomic_write_text(real, "OLD=1\n")
        link = self._p("config")
        os.symlink(real, link)

        old = time.time() - 2 * DOCUMENTED_GRACE_SECONDS
        # The name comes from the WRITER, then the same basename is planted in the other
        # directory. Spelling it by hand would pin whatever the writer emitted the day it was
        # typed (rule 7, and eighth pass F2 in this file's own history).
        # The tail is held still as well (D77): only a tail carrying a hex letter is reclaimed at
        # all, so on the real randomness "the name in the repo is deleted" was a coin flip about
        # one run in 281.
        in_repo = _writer_temp(mod, self._p("dotfiles"), "config")
        beside_link = self._p(os.path.basename(in_repo))       # the same name, the other directory
        with open(beside_link, "w") as fh:
            fh.write("the identical name, in a directory qq never writes to")
        legacy_in_repo = self._p("dotfiles", "config.tmp")     # the operator's `cp config config.tmp`
        with open(legacy_in_repo, "w") as fh:
            fh.write("a backup taken before a hand-edit")
        for p in (in_repo, beside_link, legacy_in_repo):
            os.utime(p, (old, old))
        self.assertTrue(mod.is_generated_temp_name(os.path.basename(in_repo)),
                        "fixture guard: the pair below only pins WHERE the sweep runs while its "
                        "name is one the sweep would delete — a name no rule claims survives in "
                        "both directories for the wrong reason")

        atomic_write_text(link, "NEW=2\n")                     # one ordinary write, through the link

        self.assertFalse(os.path.exists(in_repo),
                         "the rule applies in the LINK TARGET's directory — this is the dotfiles "
                         "repository case, and the disclosure has to say so")
        self.assertTrue(os.path.exists(beside_link),
                        "but not beside the link itself: qq does not write in that directory, so "
                        "the identical name there is untouched")
        self.assertTrue(os.path.exists(legacy_in_repo),
                        "and a bare <target>.tmp survives even in the swept directory — the "
                        "dotfiles repository is exactly where an operator's own backup lives, and "
                        "the exact-match rule that took it was removed on 2026-08-04")
        self.assertTrue(os.path.islink(link), "the link must survive the write")

    def test_the_documented_deletion_examples_are_what_the_reclaim_does(self):
        """ASSURANCE.md tells an operator which names beside a qq target die and which live. That
        is a claim about THIS function written in another file, and nothing executed it: the
        paragraph named only the generated-tail rule and called every other shape safe, while the
        legacy exact `<target>.tmp` match had been deleting a bare `config.tmp` since it was added
        (fifteenth pass, F2). So the document's own examples are run here.

        The two target basenames are the ones the paragraph writes its examples against -- and the
        distinction matters: `configuration.tmp` survives BESIDE a target called `config`, and
        would not survive beside one called `configuration`.

        ONE delete condition, and the document must not exemplify a second. The legacy exact
        `<target>.tmp` match was removed on 2026-08-04 (owner's ruling, twentieth pass F2), so the
        assertions below are the fifteenth pass's inverted: every name the paragraph lists as
        removed has to carry a tail this module emits, and a bare `<target>.tmp` has to appear
        among the SURVIVORS. Both halves are checked against the module's own predicate rather
        than by eye. A document that quietly re-listed `config.tmp` under "removed" -- or dropped
        it from "left alone" -- would go red here rather than becoming a disclosure of a rule the
        code no longer has.

        WHAT THIS DOES NOT REACH: the rules as stated, only the names the paragraph offers as
        examples of them, and only for the two target basenames it writes them against. A SECOND
        delete condition added to the function tomorrow would go unexemplified here unless it
        happened to claim one of these names; nothing derives the number of conditions from the
        code.
        """
        import quintessence.atomicio as mod
        removed, kept = _documented_temp_examples()
        self.assertGreaterEqual(len(removed), 2, f"ASSURANCE.md now names {removed} as the temps "
                                                 f"a reclaim removes; that reads as a disclosure "
                                                 f"lost, not a fixture shrunk")
        self.assertGreaterEqual(len(kept), 2, f"ASSURANCE.md now names {kept} as the temps a "
                                              f"reclaim leaves alone; same question")
        self.assertEqual([n for n in removed if not mod.is_generated_temp_name(n)], [],
                         f"ASSURANCE.md lists a name without a generated tail among the temps a "
                         f"reclaim removes: {removed}. The generated tail is now the ONLY delete "
                         f"condition -- a document showing another one is disclosing a rule this "
                         f"module does not have.")
        targets = ("config", "notes")
        self.assertTrue({t + ".tmp" for t in targets} & set(kept),
                        f"the paragraph shows no bare `<target>.tmp` among the names a reclaim "
                        f"leaves alone: {kept}. It has to be one of {targets} -- a bare "
                        f"`configuration.tmp` survives for the unrelated reason that its basename "
                        f"is not a target here, and would satisfy a looser check while the name "
                        f"the removed rule actually claimed went unexemplified. That spelling was "
                        f"deleted until 2026-08-04 and its survival is the change an upgrading "
                        f"operator most needs told.")
        old = time.time() - 2 * DOCUMENTED_GRACE_SECONDS
        for name in removed + kept:
            path = self._p(name)
            with open(path, "w") as fh:
                fh.write("planted from the document")
            os.utime(path, (old, old))

        for target in targets:
            mod._reclaim_stale_temps(self.tmp, target)

        for name in removed:
            self.assertFalse(os.path.exists(self._p(name)),
                             f"ASSURANCE.md lists {name} among the names a reclaim removes, and "
                             f"it survived -- the disclosure is now stronger than the code")
        for name in kept:
            self.assertTrue(os.path.exists(self._p(name)),
                            f"ASSURANCE.md lists {name} among the names a reclaim leaves alone, "
                            f"and it was deleted -- an operator was told a file was safe")

    def test_the_grace_period_is_the_hour_the_documents_promise(self):
        """Nineteenth pass, F2. `TEMP_GRACE_SECONDS` is a promise made to operators in prose:
        ASSURANCE.md tells them three times that a temp beside a target survives for an hour, and
        CONFIG.md says it twice more. Nothing executed any of it. Changing the constant to 1 left
        the whole suite green, because the reclaim fixtures aged themselves at `2 *
        mod.TEMP_GRACE_SECONDS` — a fixture that moves with the number it is supposed to hold
        still. Every COUNT in the same document is gated by test_assurance_counts; this timing
        claim was the one number on the page nothing read.

        The seam is a document asserting a property of code in another file, so it is executed the
        way the deletion examples above are: the promise is read OUT of the documents, and the
        seconds it means are written HERE, in this file, once. Re-deriving them from the module
        would restate the code rather than check it — which is exactly the anti-pattern the commit
        that found this named in setup.sh ("re-deriving it from a constant the code under test
        also reads is not that") while the engine's own tests still had it.

        BOTH implementations are checked, because the promise covers both: the engine's constant
        and setup.sh's open-coded mirror, which ASSURANCE.md's wiring paragraph makes the same
        hour-long promise about. The mirror is read as text, since setup.sh cannot be imported.
        """
        import quintessence.atomicio as mod
        promises = {}
        for doc in ("ASSURANCE.md", "CONFIG.md"):
            with open(os.path.join(ENGINE, doc), encoding="utf-8") as f:
                promises[doc] = _GRACE_PROMISE_RE.findall(f.read())
        for doc, found in promises.items():
            self.assertTrue(found,
                            f"{doc} no longer states the temp grace in a form this test can read. "
                            f"If the wording changed, teach the reader here — an ungated timing "
                            f"promise is how this was found in the first place.")
            self.assertEqual({unit for _, unit in found}, {"hour"},
                             f"{doc} promises a grace of {found}; this suite knows how to hold the "
                             f"module to an hour and nothing else")
            self.assertLessEqual({quantity for quantity, _ in found}, {"an", "a", "one", "1"},
                                 f"{doc} states the grace as {found} — some number of hours other "
                                 f"than one, which this suite cannot hold the module to")

        self.assertEqual(mod.TEMP_GRACE_SECONDS, DOCUMENTED_GRACE_SECONDS,
                         f"the engine reclaims after {mod.TEMP_GRACE_SECONDS}s while the documents "
                         f"promise an operator {DOCUMENTED_GRACE_SECONDS}s. An operator's own "
                         f"`<target>.tmp`, or a slow write's in-flight temp, is reclaimable before "
                         f"the page they were told to trust says it can be.")

        with open(os.path.join(ENGINE, "setup.sh"), encoding="utf-8") as f:
            mirror = re.search(r"^TEMP_GRACE_SECONDS = (\d+)$", f.read(), re.M)
        self.assertIsNotNone(mirror,
                             "setup.sh no longer states its grace as a constant this test can "
                             "read; the installer's own reclaim is under the same promise")
        self.assertEqual(int(mirror.group(1)), DOCUMENTED_GRACE_SECONDS,
                         "setup.sh's open-coded reclaim sweeps ~/.claude on its own grace, and "
                         "ASSURANCE.md makes the hour-long promise about that sweep too")

    def test_a_temp_is_reclaimed_after_the_documented_hour_and_not_before(self):
        """The same promise as behaviour rather than as a number, and two-sided.

        One assertion cannot do this job. A fixture aged well past the grace goes on being
        reclaimed however far the constant SHRINKS, and a fixture aged well inside it goes on
        surviving however far the constant GROWS — so the pin has to sit either side of the hour
        the documents state, at ages this file spells in seconds. Five minutes short of the hour
        the temp is a write that may still be in flight; five minutes past it, it is litter from a
        hard kill."""
        import quintessence.atomicio as mod
        p = self._p("target")
        atomic_write_text(p, "v1")

        # Both tails held still (D77). AGE has to be the only thing separating these two, and on
        # the real randomness it was not: an all-decimal tail is refused by the predicate
        # whatever its age, so `past` survived about one run in 281 and the pin read as a
        # too-wide grace.
        inside = _writer_temp(mod, self.tmp, "target", seq=0)
        past = _writer_temp(mod, self.tmp, "target", seq=1)
        now = time.time()
        os.utime(inside, ((now - DOCUMENTED_GRACE_SECONDS + 300),) * 2)
        os.utime(past, ((now - DOCUMENTED_GRACE_SECONDS - 300),) * 2)

        atomic_write_text(p, "v2")

        self.assertTrue(os.path.exists(os.path.join(self.tmp, os.path.basename(inside))),
                        "a temp 55 minutes old is inside the hour the documents promise and may "
                        "be a write still in flight — reclaiming it deletes another writer's "
                        "in-flight file and breaks the promise an operator was given")
        self.assertFalse(os.path.exists(past),
                         "a temp 65 minutes old is past the promised hour and is litter from a "
                         "hard kill; leaving it means the litter never self-limits")

    def test_ordinary_symlink_still_written_through(self):
        """The ELOOP guard must not catch the normal case it sits next to."""
        os.makedirs(self._p("repo"))
        real_file = self._p("repo", "f")
        open(real_file, "w").close()
        link = self._p("f")
        os.symlink(real_file, link)
        self.assertEqual(atomic_write_text(link, "v"), real_file)
        self.assertTrue(os.path.islink(link))


class TestTheLetterConditionIsStatedWhereverTheTailIs(unittest.TestCase):
    """The reclaim's tail rule has two halves, and every place that states one must state both.

    Since `62edff3` a name is claimed only if its twelve-character tail is lowercase hex AND
    carries at least one of `abcdef` -- deliberately NARROWER than what the writer emits, because
    twelve digits are twelve valid hex characters and `date +%Y%m%d%H%M` is how people name
    backups. A page that gives the width alone and calls it "exactly what this module writes and
    nothing wider" is false in the narrower direction, and it was contradicted 110 lines further
    down the same file by `is_generated_temp_name`'s own docstring (twenty-second pass, F5).

    Prose has classes too, so this is the class rather than the one clause the finding quoted: the
    estate is ENUMERATED, and every statement of the tail near a `.tmp` must carry the letter
    condition with it. Tests are excluded -- they quote old wordings on purpose, to say what
    changed -- and that exclusion is what this test does not reach.
    """

    TAIL = re.compile(r"(?:12|twelve) lowercase hex", re.I)
    LETTER = re.compile(r"abcdef|hex letter|one of them a letter", re.I)
    WINDOW = 800
    # Named so a rename cannot quietly empty the enumeration: these are the places that state the
    # rule today, and a member disappearing is as much a finding as a member failing.
    EXPECTED = {"ASSURANCE.md", "CONFIG.md", "RELEASE-NOTES.md", "setup.sh", "atomicio.py",
                "config.py", "search.py"}

    def _statements(self):
        """(relative path, window) for every statement of the tail width beside a `.tmp`."""
        skip_dirs = {".git", "tests", "__pycache__", ".pytest_cache", "node_modules"}
        for parent, dirs, names in os.walk(ENGINE):
            dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
            for name in sorted(names):
                path = os.path.join(parent, name)
                try:
                    if os.path.getsize(path) > 2 << 20:
                        continue
                    with open(path, encoding="utf-8") as fh:
                        text = fh.read()
                except (OSError, UnicodeDecodeError):
                    continue      # binary, or unreadable: nothing to read a claim out of
                for match in self.TAIL.finditer(text):
                    window = text[max(0, match.start() - self.WINDOW):match.end() + self.WINDOW]
                    if ".tmp" not in window:
                        continue  # `refs.py`'s "7-12 lowercase hex" is a git commit token
                    yield os.path.relpath(path, ENGINE), window

    def test_every_statement_of_the_tail_states_the_letter_condition_too(self):
        found = list(self._statements())
        files = {os.path.basename(rel) for rel, _window in found}
        self.assertGreaterEqual(len(found), len(self.EXPECTED),
                                f"the scan found only {len(found)} statements of the tail rule; an "
                                f"enumeration that finds nothing certifies nothing")
        self.assertEqual(self.EXPECTED - files, set(),
                         f"a document that used to state the tail rule no longer does in a form "
                         f"this scan can read: {sorted(self.EXPECTED - files)}. If the wording "
                         f"changed, teach the reader here rather than leaving it unchecked")
        silent = sorted({rel for rel, window in found if not self.LETTER.search(window)})
        self.assertEqual(silent, [],
                         f"these state the reclaim's tail as twelve lowercase hex without the "
                         f"letter condition beside it, which claims a rule wider than the one "
                         f"that runs: {silent}")

    def test_the_scan_can_see_a_statement_that_omits_the_condition(self):
        """Rule 3: the instrument must be able to fail. The green above is only worth something
        if a silent statement would be caught, so one is fed to the same predicate."""
        silent = "the reclaim takes `<target>.tmp.<12 lowercase hex>` and nothing wider."
        self.assertTrue(self.TAIL.search(silent), "the scan recognises the claim")
        self.assertIsNone(self.LETTER.search(silent), "and reports it as silent on the condition")


if __name__ == "__main__":
    unittest.main()
