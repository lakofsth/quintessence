# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Tests for tools/reclaim_legacy_temps.py — the one-off cleanup that replaced a standing rule.

`atomicio` used to delete a bare `<target>.tmp` beside every write target, an hour after it was
written. That rule was removed on 2026-08-04 (owner's ruling, twentieth pass F2) because it could
only ever delete a file this package did not write. The litter it was aimed at is real, though —
a pre-atomicio install whose write was interrupted has one — so the migration became this script.

What the script must get right is exactly what the rule got wrong: the predicate. A bare
`<target>.tmp` and an operator's own backup are spelled identically, so the script cannot tell
them apart either — what it can do is LIST before it deletes, delete only on `--delete`, and
never claim a name that is not that spelling at all. The lookalikes below are the cases the old
rule's neighbours were argued over: `notes.tmp.md` (twelfth pass, F1) and a generated
`<target>.tmp.<12 hex>` (which atomicio reclaims itself and this script must not race).

A `<name>.tmp` with no `<name>` beside it is NOT a lookalike — an interrupted first write leaves
exactly that (twenty-first pass, F1). It is reported in its own group and deleted only under its
own flag, which is a corroboration trade and not a statement about what the old idiom could
write; `TestFirstWriteOrphans` holds that line.
"""
import io
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

_ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ENGINE)
sys.path.insert(0, os.path.join(_ENGINE, "tools"))

import reclaim_legacy_temps as tool  # noqa: E402
from quintessence import atomicio  # noqa: E402
from quintessence.config import KEYS, Config  # noqa: E402
from quintessence.search import (CHUNKER_VERSION, cache_identity,  # noqa: E402
                                 identity_cache_path)

# The grace an operator is promised, WRITTEN OUT rather than imported from the module under test,
# for the same reason tests/py/test_atomicio.py writes it out: a fixture that derives its ages
# from the constant it is holding still moves with any drift and pins nothing.
DOCUMENTED_GRACE_SECONDS = 60 * 60


class _Fixture(unittest.TestCase):
    """A scratch $HOME with the qq directories in it, and litter of every shape planted."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        self.cfg_dir = os.path.join(self.home, ".config", "quintessence")
        self.state_dir = os.path.join(self.home, ".local", "state", "quintessence")
        self.cache_dir = os.path.join(self.home, ".cache", "qq-search")
        for d in (self.cfg_dir, os.path.join(self.state_dir, "refs"), self.cache_dir,
                  os.path.join(self.home, ".claude")):
            os.makedirs(d)
        self.env = {
            "HOME": self.home,
            "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
            "XDG_STATE_HOME": os.path.join(self.home, ".local", "state"),
            "QQ_CONFIG": os.path.join(self.cfg_dir, "config"),
            "QQ_STATE_DIR": self.state_dir,
            "QQ_CACHE": os.path.join(self.cache_dir, "embeddings.json"),
            "QUINTESSENCE_DIR": os.path.join(self.home, "store"),
        }
        # Target names taken from the package that derives them, not spelled here: a fixture that
        # invents a name proves nothing about the names qq actually wrote (rule 7). `xref-content`
        # is `xref.save_content_store`'s target, and the identity-scoped cache is the one
        # `SearchIndex.__init__` would compute for this configuration.
        cfg = Config(env=dict(self.env))
        self.xref_content = cfg.get_path("QQ_XREF_CONTENT")
        self.xref_waveoffs = cfg.get_path("QQ_XREF_WAVEOFFS")
        self.identity_cache = identity_cache_path(
            cfg.get_path("QQ_CACHE"),
            cache_identity(cfg.get_path("QQ_KB_ROOT"), cfg.get_str("QQ_EMBED_MODEL"),
                           CHUNKER_VERSION))

    def scan(self):
        """`(with_sibling, orphans)` as sets of paths, over the spots the tool derives — the
        whole search, assembled the way `main` assembles it."""
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            spots, _unsearchable = tool.search_spots(tool.Config(), [])
        with_sibling, orphans = set(), set()
        for spot in spots:
            found, orphaned = tool.legacy_temps(spot)
            with_sibling |= {p for p, _age in found}
            orphans |= {p for p, _age in orphaned}
        return with_sibling, orphans

    def _plant(self, path: str, age_seconds: float, body: str = "x") -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(body)
        when = time.time() - age_seconds
        os.utime(path, (when, when))
        return path

    def plant_all(self):
        """The fixture tree, and the names it is made of returned by role."""
        aged = 2 * DOCUMENTED_GRACE_SECONDS
        young = 60

        # A target, and beside it the pre-atomicio idiom's leftover. THE case.
        config = self._plant(os.path.join(self.cfg_dir, "config"), aged, "QQ_ASK_MIN_SIM=0.35\n")
        legacy_aged = self._plant(config + ".tmp", aged)

        # The same spelling, five minutes into its life: something may still be writing it.
        refs = self._plant(os.path.join(self.state_dir, "refs", "refs.jsonl"), aged)
        legacy_young = self._plant(refs + ".tmp", young)

        # Lookalikes, none of which the old idiom could have written.
        lookalike = self._plant(config + ".tmp.md", aged, "notes, not litter")
        # An interrupted FIRST write of the xref content store: a real target name, with no
        # target beside it because the `os.replace` that would have created it never ran.
        orphan = self._plant(self.xref_content + ".tmp", aged)

        # A LIVE atomicio temp, from the writer rather than spelled here (rule 7). atomicio
        # reclaims this one itself; the script must not race it.
        fd, generated = atomicio._open_unique_temp(self.cache_dir, "embeddings.json")
        os.close(fd)
        when = time.time() - aged
        os.utime(generated, (when, when))
        self._plant(os.path.join(self.cache_dir, "embeddings.json"), aged, "{}")

        return {"legacy_aged": legacy_aged, "legacy_young": legacy_young,
                "lookalike": lookalike, "orphan": orphan, "generated": generated,
                "config": config, "refs": refs}

    def run_tool(self, *argv):
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, self.env, clear=True), \
             unittest.mock.patch("sys.stdout", out):
            rc = tool.main(list(argv))
        return rc, out.getvalue()


class TestThePredicate(_Fixture):
    def test_a_bare_target_tmp_is_the_only_shape_claimed_and_the_split_is_on_the_sibling(self):
        """The fixture guard for everything below: `legacy_temps` finds the three bare
        `<target>.tmp` files and NEITHER lookalike, and splits them on the sibling.

        Each lookalike is here for a reason and each would be a different defect. `.tmp.md`
        beside a target is an operator's file the old rule's wider ancestors deleted (twelfth
        pass, F1). The generated `<target>.tmp.<12 hex>` is a temp `atomicio` reclaims on its own
        one-hour grace, possibly from a write in flight, and nothing else may touch it.

        `xref-content.tmp`, with no `xref-content` beside it, is the third bare temp and not a
        lookalike: the old idiom wrote its temp BEFORE the `os.replace` that creates the target,
        so an interrupted first write leaves this. It goes in the second group, not out of the
        result.
        """
        names = self.plant_all()
        found, orphaned = self.scan()
        self.assertIn(names["legacy_aged"], found)
        self.assertIn(names["legacy_young"], found)
        self.assertEqual(orphaned, {names["orphan"]},
                         "a bare `<name>.tmp` with no `<name>` is an orphan, not a non-result")
        for role, why in (("lookalike",
                           "`<target>.tmp.md` is an operator's file, not the old spelling"),
                          ("generated",
                           "a generated `<target>.tmp.<12 hex>` is atomicio's own temp — it may "
                           "be a write in flight, and atomicio reclaims it on its own grace")):
            self.assertNotIn(names[role], found, why)
            self.assertNotIn(names[role], orphaned, why)
        self.assertEqual(len(found), 2, f"nothing else may be claimed: {found}")

    def test_the_age_is_the_files_own_and_a_symlink_is_not_followed(self):
        """The reclaim this replaces reads `entry.stat(follow_symlinks=False)`, and the two
        reclaim paths disagreeing on exactly that property was a finding of its own (twentieth
        pass, F3). A dangling link named `<target>.tmp` must be reported, not raise."""
        target = self._plant(os.path.join(self.state_dir, "pending-findings.md"), 0)
        link = target + ".tmp"
        os.symlink(os.path.join(self.state_dir, "nowhere-at-all"), link)
        found, _orphans = self.scan()
        self.assertIn(link, found, "a dangling `<target>.tmp` symlink must still be reported — "
                                   "following it would raise and drop it silently")


class TestWhatIsListedIsSomethingItCanDelete(_Fixture):
    """A DIRECTORY named `<target>.tmp`, which the old idiom could not have written.

    Nothing asked what kind of entry it was: the scan filtered on the name alone, so a directory
    called `mydata.tmp` was listed as litter and then refused by `os.unlink` -- `! could not
    delete ...: Is a directory`, `Deleted 0 of 1, 1 refused`, and a non-zero exit (twenty-third
    pass, F4). No data can be lost that way, since `unlink` cannot remove a directory; what is
    lost is the listing's meaning. This tool's whole value is a list an operator can read and
    act on, and a line in it that no flag can act on is noise in the one place noise is expensive.

    The filter is "not a directory", with the entry's own type (`lstat`), rather than "a regular
    file": a directory is precisely what `os.unlink` refuses, and every other kind of entry it
    removes. Narrowing to regular files would also drop a SYMLINK wearing a temp name, and one
    of those is pinned two classes up as a thing that must be reported (twentieth pass, F3) --
    `os.unlink` removes the link itself, which is the right outcome for it.
    """

    def test_a_directory_wearing_a_temp_name_is_not_listed_as_litter(self):
        """In a derived directory, at a name the tool really does claim: `pending-findings.md` is
        an idiom site, so the name is doing none of the work here -- only the entry's type is."""
        d = os.path.join(self.state_dir, "pending-findings.md.tmp")
        os.makedirs(d)
        os.utime(d, tuple([time.time() - 2 * DOCUMENTED_GRACE_SECONDS] * 2))
        self._plant(os.path.join(self.state_dir, "pending-findings.md"),
                    2 * DOCUMENTED_GRACE_SECONDS)
        rc, out = self.run_tool("--delete")
        self.assertEqual(rc, 0, f"a run that listed only a directory reported a refusal:\n{out}")
        self.assertNotIn(d, out, f"a directory is not a `<target>.tmp` the old idiom wrote:\n{out}")
        self.assertNotIn("refused", out, "nothing was claimed, so nothing can be refused")
        self.assertTrue(os.path.isdir(d), "and it is still there")

    def test_a_directory_wearing_a_temp_name_is_not_listed_in_a_directory_you_named(self):
        """The `--dir` widening claims any `<name>.tmp`, so it is where the name condition stops
        holding this back at all."""
        base = os.path.join(self.home, "elsewhere")
        d = os.path.join(base, "mydata.tmp")
        os.makedirs(d)
        os.utime(d, tuple([time.time() - 2 * DOCUMENTED_GRACE_SECONDS] * 2))
        rc, out = self.run_tool("--delete", "--dir", base)
        self.assertEqual(rc, 0, f"the run exited non-zero over a refusal it caused itself:\n{out}")
        self.assertNotIn(d, out, f"listed a directory it cannot delete:\n{out}")
        self.assertNotIn("Is a directory", out)
        self.assertTrue(os.path.isdir(d))

    def test_a_symlink_wearing_a_temp_name_is_still_listed(self):
        """The other side of the filter, and the reason it is not "regular files only": a
        symlink named `<target>.tmp` is something `os.unlink` removes, and a dangling one has
        been a required listing since the twentieth pass, F3. The filter must separate a
        directory from the rest, not a regular file from the rest."""
        target = self._plant(os.path.join(self.state_dir, "pending-findings.md"), 0)
        link = target + ".tmp"
        os.symlink(os.path.join(self.state_dir, "nowhere-at-all"), link)
        os.utime(link, tuple([time.time() - 2 * DOCUMENTED_GRACE_SECONDS] * 2),
                 follow_symlinks=False)
        rc, out = self.run_tool()
        self.assertEqual(rc, 0)
        self.assertIn(link, out, f"a `<target>.tmp` symlink is deletable and must be listed:\n"
                                 f"{out}")


class TestFirstWriteOrphans(_Fixture):
    """The interrupted FIRST write: `<target>.tmp` with no `<target>`, because the target never
    landed even once.

    The old idiom is `os.makedirs(..., exist_ok=True)` -> write `<path>.tmp` -> `os.replace`,
    with no requirement that `<path>` already exist. Read at `9b1c711`: `xref._persist_waveoffs`,
    `xref.save_content_store` and `search._save_cache` all have that shape. So a sibling-less
    `<target>.tmp` CAN be this package's litter, and in the state directory nothing else sweeps
    it. The names below are those very targets, taken from `Config` rather than invented: with no
    sibling to corroborate it, the NAME is the only thing tying such a file to this package, and
    a fixture that made one up would be pinning a claim the tool no longer makes.
    """

    def test_a_sibling_less_temp_is_listed_for_review(self):
        """The reviewer's fixture (twenty-first pass, F1): an aged `xref-content.tmp` with no
        `xref-content` beside it. It used to be invisible — the run printed "Nothing to do" over
        a directory that had litter in it, which is the false clean report the tool exists to
        prevent."""
        orphan = self._plant(self.xref_content + ".tmp", 2 * DOCUMENTED_GRACE_SECONDS)
        rc, out = self.run_tool()
        self.assertEqual(rc, 0)
        self.assertIn(orphan, out,
                      "an interrupted first write leaves no sibling; listing it is the whole "
                      f"point of the section:\n{out}")
        self.assertIn("no sibling", out, "the listing must say WHY it is set apart")
        self.assertNotIn("Nothing to do", out,
                         "a directory holding an orphan is not a clean estate")
        self.assertTrue(os.path.exists(orphan), "the default run deletes nothing")

    def test_plain_delete_leaves_orphans_alone(self):
        """The trade, pinned: `--delete` acts only where the sibling corroborates. An orphan has
        no second signal at all, so it needs its own flag."""
        orphan = self._plant(self.xref_content + ".tmp", 2 * DOCUMENTED_GRACE_SECONDS)
        rc, out = self.run_tool("--delete")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(orphan),
                        "--delete must not touch a sibling-less temp; --delete-orphans does")
        self.assertIn("--delete-orphans", out, "the run must name the flag that would claim it")

    def test_delete_orphans_removes_the_aged_orphan_and_not_the_young_one(self):
        aged = self._plant(self.xref_content + ".tmp", 2 * DOCUMENTED_GRACE_SECONDS)
        young = self._plant(self.xref_waveoffs + ".tmp", 60)
        rc, out = self.run_tool("--delete-orphans")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(aged), f"the aged orphan is what the flag is for:\n{out}")
        self.assertTrue(os.path.exists(young),
                        "the one-hour grace applies to orphans too — a young one may be a write "
                        "in flight from a tool that is still running")

    def test_delete_orphans_does_not_claim_the_lookalikes(self):
        """The orphan section widens the spelling `<target>.tmp` to names with no sibling; it
        must not widen the spelling itself. A generated `<target>.tmp.<12 hex>` whose target is
        absent is still atomicio's, and `<target>.tmp.md` is still an operator's."""
        target = os.path.basename(self.xref_content)          # a real target, absent on disk
        fd, generated = atomicio._open_unique_temp(self.state_dir, target)
        os.close(fd)
        when = time.time() - 2 * DOCUMENTED_GRACE_SECONDS
        os.utime(generated, (when, when))
        lookalike = self._plant(self.xref_content + ".tmp.md", 2 * DOCUMENTED_GRACE_SECONDS)
        rc, out = self.run_tool("--delete-orphans")
        self.assertEqual(rc, 0)
        for path in (generated, lookalike):
            self.assertTrue(os.path.exists(path), f"--delete-orphans removed {path}")
            self.assertNotIn(path, out)


class TestStemsThatAreNotTargetNames(_Fixture):
    """Files whose stem is not a NAME at all -- `.tmp`, `..tmp`, `...tmp` -- and the signal they
    broke.

    `name[:-len(".tmp")]` gives `""`, `"."` and `".."`. `os.path.join(directory, stem)` for those
    three names no entry in the directory: the first two ARE the directory and the third is its
    parent, and all three always exist. So the sibling test -- the tool's one corroborating
    signal -- could not answer anything but True for them, which put all three in the
    WITH-A-SIBLING group, the one plain `--delete` empties, past the `--delete-orphans` gate the
    design puts uncorroborated files behind (twenty-second pass F4 for `""`; twenty-third pass F1
    for the other two, left open by the fix that closed the first).

    Those three are the whole class, because a filename cannot contain `os.sep`: every other stem
    names a distinct entry in this directory, and whether that entry is there is a real answer.
    `....tmp` is the boundary on the other side -- stem `...`, an ordinary name that can be absent
    -- and it is pinned below as an ORPHAN rather than as a refusal, because refusing it would be
    a second guess in place of the signal.

    None of the three can be this package's litter under any configuration: the old idiom wrote
    `<target> + ".tmp"` and every one of its targets had a name. So they are refused on the name,
    before the sibling question is asked, in every kind of search directory -- including a
    `--dir` the operator named, which is where the reach fix left the case live, and including
    the one configuration that could otherwise put `""` in a derived directory's target names, a
    path setting written with a trailing slash.

    The name rule is not the only thing holding this, and deliberately so: the mechanism the
    three exploited was a corroboration test that said True for a DIRECTORY, so the sibling test
    now asks for a regular FILE. Either rule alone closes all three; the last test here holds
    the second one at a stem that is an ordinary name, where the first cannot reach.
    """

    def test_a_bare_dot_tmp_is_not_claimed_in_a_directory_the_operator_named(self):
        mine = self._plant(os.path.join(self.home, "elsewhere", ".tmp"),
                           2 * DOCUMENTED_GRACE_SECONDS)
        rc, out = self.run_tool("--delete", "--dir", os.path.join(self.home, "elsewhere"))
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(mine),
                        f"a file named `.tmp` was deleted by plain --delete; its 'sibling' is the "
                        f"directory it sits in, which always exists:\n{out}")
        self.assertNotIn(mine, out, "it is not litter of ours in either group")

    def test_a_bare_dot_tmp_is_not_claimed_in_a_derived_directory(self):
        """The same boundary where the target names come from `Config`. `""` is not one of them,
        so this already holds -- and it is asserted rather than assumed, because the name
        condition and the empty-name guard are two different reasons and either could be removed
        without the other going red."""
        mine = self._plant(os.path.join(self.state_dir, ".tmp"), 2 * DOCUMENTED_GRACE_SECONDS)
        rc, out = self.run_tool("--delete")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(mine), f"`.tmp` in the state directory was deleted:\n{out}")

    def test_a_trailing_slash_on_a_path_setting_does_not_make_dot_tmp_a_target(self):
        """`QQ_XREF_CONTENT=<dir>/` gives `os.path.basename` an empty string, which would put
        `""` in that directory's target names and claim `.tmp` again through the derivation
        rather than through the sibling. Same file, same delete, a different route to it."""
        self.env["QQ_XREF_CONTENT"] = self.state_dir + "/"
        mine = self._plant(os.path.join(self.state_dir, ".tmp"), 2 * DOCUMENTED_GRACE_SECONDS)
        rc, out = self.run_tool("--delete")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(mine),
                        f"a path setting with a trailing slash made `.tmp` a target:\n{out}")

    def test_the_whole_navigation_class_survives_a_plain_delete_and_a_deeper_one_is_an_orphan(self):
        """All three stems that always resolve, plus the first stem that does not.

        `.tmp` (stem `""`) was fixed one pass earlier; `..tmp` (stem `"."`, the directory again)
        and `...tmp` (stem `".."`, the PARENT of the search directory) were the same defect left
        open, and both were reported as "legacy temp(s) past the one-hour grace" and removed by a
        plain `--delete`. `....tmp` (stem `"..."`) is the control that keeps this a class fix and
        not a longer hand-list: it is an ordinary name, nothing is called `...` beside it, and it
        must still be LISTED as an orphan for the operator to judge.
        """
        base = os.path.join(self.home, "elsewhere")
        os.makedirs(base)
        aged = 2 * DOCUMENTED_GRACE_SECONDS
        planted = {stem: self._plant(os.path.join(base, stem + ".tmp"), aged)
                   for stem in ("", ".", "..", "...")}
        rc, out = self.run_tool("--delete", "--dir", base)
        self.assertEqual(rc, 0)
        for stem, path in planted.items():
            self.assertTrue(os.path.exists(path),
                            f"`{stem}.tmp` was deleted by a plain --delete; a stem of "
                            f"{stem!r} is not a target name, and for the first three the "
                            f"'sibling' checked is a directory that always exists:\n{out}")
        for stem in ("", ".", ".."):
            self.assertNotIn(planted[stem], out,
                             f"`{stem}.tmp` is not litter of ours in either group")
        self.assertIn(planted["..."], out,
                      f"`....tmp` has an ordinary stem `...` with nothing beside it — an orphan "
                      f"for the operator to read, not a refusal:\n{out}")
        self.assertIn("no sibling", out, "and it is listed in the group that says why")

    def test_a_navigation_stem_is_refused_in_a_derived_directory_too(self):
        """The same class where the target names come from `Config` rather than from `--dir`.
        `"."` is not one of them, so the name condition does the work here -- asserted rather
        than assumed, because the derivation and the guard are two different reasons and either
        could be removed without the other going red."""
        mine = self._plant(os.path.join(self.state_dir, "..tmp"), 2 * DOCUMENTED_GRACE_SECONDS)
        rc, out = self.run_tool("--delete")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(mine),
                        f"`..tmp` in the state directory was deleted:\n{out}")
        self.assertNotIn(mine, out, "it is not litter of ours in either group")

    def test_a_sibling_that_is_a_directory_does_not_corroborate(self):
        """The MECHANISM the three navigation stems rode, at a stem that is an ordinary name.

        The sibling is the tool's one corroborating signal and it means "a file named `<target>`
        sits beside the temp" -- `os.path.lexists` answered True for a directory of that name,
        which is precisely how a stem that resolves to a directory reached the group plain
        `--delete` empties. Nothing about dots is load-bearing in it: a `--dir` holding a
        directory `mydata` and a file `mydata.tmp` is the same false corroboration without a
        single dot in the stem, and the name rule above cannot see this case at all.
        """
        base = os.path.join(self.home, "elsewhere")
        os.makedirs(os.path.join(base, "mydata"))
        mine = self._plant(os.path.join(base, "mydata.tmp"), 2 * DOCUMENTED_GRACE_SECONDS)
        rc, out = self.run_tool("--delete", "--dir", base)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(mine),
                        f"a DIRECTORY named `mydata` corroborated `mydata.tmp` into the group a "
                        f"plain --delete empties; the signal is a file beside it:\n{out}")
        self.assertIn(mine, out, "it is still listed — uncorroborated, for the operator to read")
        self.assertIn("no sibling", out, "and in the group that says why it is set apart")


class TestListingAndDeleting(_Fixture):
    def test_listing_names_the_right_set_and_deletes_nothing(self):
        names = self.plant_all()
        rc, out = self.run_tool()
        self.assertEqual(rc, 0)
        self.assertIn(names["legacy_aged"], out)
        self.assertIn(names["legacy_young"], out)
        self.assertNotIn(names["lookalike"], out)
        self.assertNotIn(names["generated"], out)
        self.assertIn(names["orphan"], out,
                      "the sibling-less one is LISTED — it may be an interrupted first write, "
                      "and the one thing it must never be is invisible")
        self.assertIn("no sibling, review by hand", out)
        self.assertIn("Nothing was deleted", out)
        for path in names.values():
            self.assertTrue(os.path.lexists(path),
                            f"the default run must not touch {path} — listing is the default "
                            f"precisely because this spelling cannot be told from your own file")

    def test_delete_removes_the_aged_legacy_temp_and_only_that(self):
        names = self.plant_all()
        rc, out = self.run_tool("--delete")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(names["legacy_aged"]),
                         "the aged `<target>.tmp` is what --delete is for")
        self.assertIn(f"deleted {names['legacy_aged']}", out)
        for role in ("legacy_young", "lookalike", "orphan", "generated", "config", "refs"):
            self.assertTrue(os.path.lexists(names[role]),
                            f"--delete removed {role} ({names[role]}); it removes ONLY a bare "
                            f"`<target>.tmp` past the one-hour grace")
        self.assertIn("inside the grace were left", out,
                      "a temp inside the grace is left AND said out loud — a silent skip reads "
                      "as 'there was nothing there'")

    def test_a_clean_estate_says_so_rather_than_printing_nothing(self):
        """Rule 3: a run that finds nothing must be distinguishable from a run that searched
        nothing. The directories it searched are printed either way."""
        os.makedirs(self.cfg_dir, exist_ok=True)
        self._plant(os.path.join(self.cfg_dir, "config"), 0)
        rc, out = self.run_tool()
        self.assertEqual(rc, 0)
        self.assertIn("Nothing to do", out)
        self.assertIn(self.cfg_dir, out, "the search must name the directories it covered")


class TestTheReachIsWhereTheIdiomWrote(_Fixture):
    """The reach is derived from the fourteen pre-atomicio idiom sites, and stops there.

    Read at `9b1c711`, the last commit before `atomicio`, every one of those sites resolves into
    the config file's directory, the state directory (`pending-findings.md`, and `refs/` one level
    down), the embedding cache's directory, or `~/.claude/settings.json`. NONE of them is under
    `QUINTESSENCE_DIR`: the note store is written by `qq`'s HEAD writers, which never used the
    tmp+rename idiom, and the inline claim that `admin.py` wrote a `.gitignore` that way was
    false — it is a plain `open(..., "w")` at every revision.

    So the store is not searched at all, no search directory is walked into, and a name that is
    not one of those targets is not claimed. The three cases below are the reviewer's own
    reproductions (twenty-second pass, F1), each of which ended in a deleted file of the
    operator's under a plain `--delete`.
    """

    def test_the_note_store_is_not_searched(self):
        """`QUINTESSENCE_DIR` was in the searched set and was walked whole, so an operator's
        scratch file anywhere under their notes was claimed WITH a sibling — the plain `--delete`
        group, not the guarded one."""
        store = os.path.join(self.home, "store", "topics", "private")
        self._plant(os.path.join(store, "memoir"), 2 * DOCUMENTED_GRACE_SECONDS)
        mine = self._plant(os.path.join(store, "memoir.tmp"), 2 * DOCUMENTED_GRACE_SECONDS)
        rc, out = self.run_tool("--delete")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(mine),
                        f"--delete removed a file inside the note store:\n{out}")
        self.assertNotIn(mine, out, "the store holds no litter this tool can claim — no idiom "
                                    "site ever wrote under it, so it is not searched at all")
        self.assertNotIn(os.path.join(self.home, "store"), out,
                         "the store must not even be named as a searched directory")

    def test_a_symlinked_configs_dotfiles_repo_is_not_searched(self):
        """The old idiom wrote `<configured path> + '.tmp'` — `admin.py`'s `f` is
        `Config.config_path`, which is expanded but never resolved — so a config symlinked into a
        dotfiles repository left its litter beside the LINK. Resolving first put the whole
        repository in the search, where `--delete` took two files that were nothing to do with
        this package.
        """
        repo = os.path.join(self.home, "dotfiles")
        real = self._plant(os.path.join(repo, "config"), 2 * DOCUMENTED_GRACE_SECONDS)
        os.symlink(real, os.path.join(self.cfg_dir, "config"))
        self._plant(os.path.join(repo, "nvim", "init.lua"), 2 * DOCUMENTED_GRACE_SECONDS)
        theirs_litter = self._plant(os.path.join(repo, "nvim", "init.lua.tmp"),
                                    2 * DOCUMENTED_GRACE_SECONDS)
        # The second file the reviewer lost, and the sharper one: its name matches a target
        # basename exactly. It survives because this repository is not a directory the old idiom
        # ever wrote into, not because of the name.
        self._plant(os.path.join(repo, "ssh", "config"), 2 * DOCUMENTED_GRACE_SECONDS)
        theirs_named_like_ours = self._plant(os.path.join(repo, "ssh", "config.tmp"),
                                             2 * DOCUMENTED_GRACE_SECONDS)
        ours = self._plant(os.path.join(self.cfg_dir, "config.tmp"),
                           2 * DOCUMENTED_GRACE_SECONDS)

        rc, out = self.run_tool("--delete")
        self.assertEqual(rc, 0)
        for path in (theirs_litter, theirs_named_like_ours):
            self.assertTrue(os.path.exists(path),
                            f"--delete removed {path} in the operator's dotfiles repository:"
                            f"\n{out}")
        self.assertNotIn(repo, out, "the dotfiles repository is not where the old idiom wrote")
        self.assertFalse(os.path.exists(ours),
                         f"the litter is beside the LINK, in the config directory, and that is "
                         f"the one this tool exists to clear:\n{out}")

    def test_a_config_in_the_home_directory_does_not_drag_home_in(self):
        """`QQ_CONFIG=$HOME/qqconfig` is an ordinary choice, and it made the search directory
        `$HOME`: every `.tmp` anywhere under it was listed and offered for deletion. The
        directory is still searched — that is where the config's litter would be — but only for
        the config's own name."""
        self.env["QQ_CONFIG"] = os.path.join(self.home, "qqconfig")
        self._plant(os.path.join(self.home, "qqconfig"), 2 * DOCUMENTED_GRACE_SECONDS)
        ours = self._plant(os.path.join(self.home, "qqconfig.tmp"),
                           2 * DOCUMENTED_GRACE_SECONDS)
        self._plant(os.path.join(self.home, "Documents", "thesis"),
                    2 * DOCUMENTED_GRACE_SECONDS)
        theirs = self._plant(os.path.join(self.home, "Documents", "thesis.tmp"),
                             2 * DOCUMENTED_GRACE_SECONDS)
        beside = self._plant(os.path.join(self.home, "notes.tmp"),
                             2 * DOCUMENTED_GRACE_SECONDS)

        rc, out = self.run_tool()
        self.assertIn(ours, out, "the configured config's own `<name>.tmp` is exactly the litter "
                                 "this tool is for, wherever the operator put the config")
        for path, why in ((theirs, "a subdirectory of $HOME is not searched at all"),
                          (beside, "a `.tmp` in the searched directory whose name qq never "
                                   "wrote is not this package's litter")):
            self.assertNotIn(path, out, why)
            self.assertTrue(os.path.exists(path), why)
        self.assertEqual(rc, 0)

    def test_a_state_subdirectory_the_idiom_never_wrote_into_is_not_walked(self):
        """`refs/` is one level down and IS searched, because two idiom sites wrote there. That
        is the depth the subject needs — not a licence to walk the rest of the tree."""
        # Named after a target of the state directory ITSELF, so only the depth separates it from
        # litter the tool claims — the name cannot be doing the work.
        deeper = self._plant(os.path.join(self.state_dir, "archive", "pending-findings.md.tmp"),
                             2 * DOCUMENTED_GRACE_SECONDS)
        reachable = self._plant(os.path.join(self.state_dir, "refs", "refs.jsonl.tmp"),
                                2 * DOCUMENTED_GRACE_SECONDS)
        rc, out = self.run_tool()
        self.assertEqual(rc, 0)
        self.assertIn(reachable, out, "refs/refs.jsonl is an idiom site — its litter is in reach")
        self.assertNotIn(deeper, out, "no idiom site wrote into a state subdirectory of the "
                                      "operator's own, and the tool does not walk into one")

    def test_only_the_target_names_the_idiom_wrote_are_claimed(self):
        """The basename condition, at the directory the idiom really did write into: a scratch
        file of the operator's in the state directory is not claimed just for being there."""
        ours = self._plant(os.path.join(self.state_dir, "pending-findings.md.tmp"),
                           2 * DOCUMENTED_GRACE_SECONDS)
        self._plant(os.path.join(self.state_dir, "pending-findings.md"),
                    2 * DOCUMENTED_GRACE_SECONDS)
        self._plant(os.path.join(self.state_dir, "scratch"), 2 * DOCUMENTED_GRACE_SECONDS)
        theirs = self._plant(os.path.join(self.state_dir, "scratch.tmp"),
                             2 * DOCUMENTED_GRACE_SECONDS)
        rc, out = self.run_tool("--delete")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(ours), f"pending-findings.md is an idiom site:\n{out}")
        self.assertTrue(os.path.exists(theirs), f"`scratch` is not a name qq writes:\n{out}")

    def test_the_listing_says_what_it_searches_for_and_how_deep(self):
        """Rule 3 on the tool's own report: the line an operator reads before passing `--delete`
        must describe the search that actually runs. It said "searching 5 directories" while
        walking five subtrees for any name at all."""
        rc, out = self.run_tool()
        self.assertEqual(rc, 0)
        self.assertIn("not their subdirectories", out,
                      f"the listing must say the search is not recursive:\n{out}")
        self.assertIn("config", out, "each directory is listed with the target names searched "
                                     "for in it, which is the whole of the scope")


class TestWhereItLooks(_Fixture):
    def test_the_target_key_lists_are_checked_against_the_registry(self):
        """Rule 6: the enumeration must assert it found work. If a key is renamed in
        `quintessence/config.py`, this script would quietly search a shorter estate and report a
        clean disk — so it refuses instead, and this pins the refusal in both directions."""
        for key in tool.FILE_TARGET_KEYS + tool.DIR_TARGET_KEYS + tool.IDENTITY_KEYS:
            self.assertIn(key, {kd.name for kd in KEYS},
                          f"{key} is not in the config registry; the script's list of write "
                          f"targets has drifted from the package")
        with unittest.mock.patch.object(tool, "FILE_TARGET_KEYS", ("QQ_RENAMED_AWAY",)):
            with self.assertRaises(SystemExit) as caught:
                tool.search_spots(unittest.mock.MagicMock())
        self.assertIn("QQ_RENAMED_AWAY", str(caught.exception))

    def test_the_state_targets_are_still_names_the_package_writes(self):
        """The three state-directory targets are named by the package, not by a setting, so the
        registry check above cannot cover them. If one is renamed, this script would look for a
        name nothing writes and report a clean state directory — the same silent shortening the
        registry check exists to prevent, on the axis that check does not reach."""
        package = os.path.join(_ENGINE, "quintessence")
        source = ""
        for name in sorted(os.listdir(package)):
            if name.endswith(".py"):
                with open(os.path.join(package, name), encoding="utf-8") as fh:
                    source += fh.read()
        self.assertIn("pending-findings.md", source, "the instrument reads the package at all")
        for relative, basename in tool.STATE_TARGETS:
            self.assertIn(basename, source,
                          f"{os.path.join(relative, basename)} is no longer a name the package "
                          f"writes; this script's state-directory targets have drifted from it")

    def test_the_identity_shape_is_checked_against_the_function_that_builds_it(self):
        """`IDENTITY_SHAPE` recognises identities this script cannot re-derive — a corpus root or
        embed model no longer configured. It is a written-out copy of what `cache_identity`
        builds, so it gets a live check against that function, and the check must be able to
        fail."""
        with unittest.mock.patch.object(tool, "IDENTITY_SHAPE", r"nothing-like-an-identity"):
            with self.assertRaises(SystemExit) as caught:
                tool.cache_target_names(Config(env=dict(self.env)))
        self.assertIn("IDENTITY_SHAPE", str(caught.exception))
        names, shape = tool.cache_target_names(Config(env=dict(self.env)))
        self.assertIn(os.path.basename(self.identity_cache), names,
                      "the identity for the CONFIGURED corpus root and model is derived exactly, "
                      "not matched by shape")

    def test_an_identity_cache_from_another_corpus_is_claimed_only_beside_its_cache(self):
        """The one place a name is matched by shape instead of derived. An install that has
        changed corpus root or embed model has caches this script cannot name — but the cache
        file sitting beside the temp corroborates the name, which is the same signal the
        with-sibling group already rests on. With no cache beside it, nothing does, and the
        script does not guess."""
        other = identity_cache_path(os.path.join(self.cache_dir, "embeddings.json"),
                                    cache_identity("/some/other/corpus", "some-other-model:1b",
                                                   CHUNKER_VERSION))
        self._plant(other, 2 * DOCUMENTED_GRACE_SECONDS, "{}")
        corroborated = self._plant(other + ".tmp", 2 * DOCUMENTED_GRACE_SECONDS)
        alone = self._plant(os.path.join(self.cache_dir, "embeddings.deadbeefdeadbeef-gone-v9"
                                                         ".json.tmp"),
                            2 * DOCUMENTED_GRACE_SECONDS)
        found, orphans = self.scan()
        self.assertIn(corroborated, found, "the cache beside it is the corroboration")
        self.assertNotIn(alone, found | orphans,
                         "a name this script cannot derive, with nothing beside it to stand for "
                         "it, is not claimed")

    def test_a_literal_tilde_in_claude_settings_is_expanded_like_every_other_path(self):
        """`CLAUDE_SETTINGS` is the one search path that comes from the environment rather than
        from `Config`, and it was the one that skipped `~`-expansion: `os.path.dirname` gave the
        literal `~/.claude`, which is not a directory, so the whole spot was dropped -- silently,
        including from the "searching N directories" header (twenty-third pass, F5). That is a
        false clean report on one of the fourteen documented idiom sites, in a tool whose entire
        value is the list. `Config` expands every path it hands over and `--dir` values are
        expanded on the way in; this one now matches them.

        The instrument is proved in the same run: the litter planted here is found, so an empty
        result would be a real absence and not a scan that ran somewhere else.
        """
        self.env["CLAUDE_SETTINGS"] = os.path.join("~", ".claude", "settings.json")
        settings = self._plant(os.path.join(self.home, ".claude", "settings.json"),
                               2 * DOCUMENTED_GRACE_SECONDS, "{}")
        litter = self._plant(settings + ".tmp", 2 * DOCUMENTED_GRACE_SECONDS)
        rc, out = self.run_tool()
        self.assertEqual(rc, 0)
        self.assertIn(os.path.join(self.home, ".claude"), out,
                      f"a literal `~` dropped ~/.claude out of the search entirely:\n{out}")
        self.assertIn(litter, out, f"and with it the litter setup.sh's idiom leaves there:\n{out}")

    def test_a_configured_directory_this_host_does_not_have_is_reported_not_omitted(self):
        """The empty-enumeration rule, applied to the search itself. A directory that is named by
        a setting but cannot be searched is exactly the thing an operator must hear about: the
        run they are reading is their evidence about the disk, and a spot dropped without a word
        makes a shorter search look like a cleaner one. It used to be dropped in silence, which
        is how the `~` case above could vanish without trace."""
        gone = os.path.join(self.home, "moved-away")
        self.env["QQ_XREF_CONTENT"] = os.path.join(gone, "xref-content")
        rc, out = self.run_tool()
        self.assertEqual(rc, 0)
        self.assertIn(gone, out,
                      f"a configured directory that does not exist was dropped without a word; "
                      f"the litter it may hold is not in this listing and the operator cannot "
                      f"know that:\n{out}")
        self.assertIn("not searched", out, "and the line must say what happened to it")
        # The control: a spot that IS searchable is not reported as missing.
        self.assertNotIn(f"{self.cfg_dir} — does not exist", out)

    def test_an_extra_directory_can_be_named_and_is_not_double_reported(self):
        """`--dir` covers what the derivation cannot know about — a target under a setting the
        operator has since repointed. Passing a directory already searched must not report the
        same file twice, or `--delete` would try to unlink it twice and print a spurious refusal.
        """
        names = self.plant_all()
        rc, out = self.run_tool("--dir", self.cfg_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(out.count(names["legacy_aged"]), 1,
                         f"reported more than once:\n{out}")

    def test_a_named_directory_is_searched_for_any_name_but_still_not_walked(self):
        """The deliberate widening, and its limit. Outside qq's own settings there are no target
        names to go on, so a directory the operator NAMES is searched for any `<name>.tmp` — that
        is what asking for it means. It is still one directory: `--dir` on an ancestor does not
        walk the tree, which is the property the default search lost (twenty-second pass, F1)."""
        theirs = self._plant(os.path.join(self.home, "elsewhere", "anything.tmp"),
                             2 * DOCUMENTED_GRACE_SECONDS)
        deeper = self._plant(os.path.join(self.home, "elsewhere", "down", "deeper.tmp"),
                             2 * DOCUMENTED_GRACE_SECONDS)
        rc, out = self.run_tool("--dir", os.path.join(self.home, "elsewhere"))
        self.assertEqual(rc, 0)
        self.assertIn(theirs, out, "a directory you name is searched for any `<name>.tmp`")
        self.assertIn("--dir", out, "the listing must say which directories carry the widening")
        self.assertNotIn(deeper, out, "naming a directory is not asking for its subtree")


if __name__ == "__main__":
    unittest.main()
