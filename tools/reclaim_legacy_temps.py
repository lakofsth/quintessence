#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""reclaim_legacy_temps.py – find (and, if you say so, delete) pre-atomicio `<target>.tmp` litter.

Before `quintessence/atomicio.py` existed, every durable write in this package hand-rolled the
same idiom: write `<path>.tmp`, then rename it over `<path>`. On any exception the rename never
happened and `<path>.tmp` stayed on disk forever, because nothing swept the state directory. If
you installed before 2026-08-04 and a write was ever interrupted, you may have one.

`atomicio` used to reclaim those by exact name, an hour after they were written. That rule was
removed (see RELEASE-NOTES.md): the current writer cannot produce a bare `<target>.tmp`, so the
rule could only ever delete a file this package did not write – including your own
`cp config config.tmp` beside the config `qq config set` writes. A permanent file-deleting rule
was the wrong price for a one-time migration, so the migration is this script: run once, by you,
listing before it deletes.

WHAT IT LOOKS FOR: a file named `<target>.tmp` where `<target>` is a file the old idiom actually
wrote, in the directory it actually wrote it in. Both halves are the condition. The spelling
alone is not enough – `memoir.tmp` beside your `memoir` is spelled the same way and is yours –
so the name has to be one of qq's own write targets, derived below from your `Config` the same
way the idiom sites derived theirs. `<name>.tmp.md`, `<name>.tmp.bak` and `<name>.tmp.<12 hex>`
are different names and are never listed – the last of those is a live atomicio temp, which
`atomicio` reclaims itself.

Findings are reported in two groups, by whether a file named `<target>` sits beside the temp:

  * WITH a sibling – the ordinary case, since the old idiom wrote its temp beside the file it
    was replacing. `--delete` removes the aged ones.
  * WITHOUT a sibling – an interrupted FIRST write. The old idiom was
    `os.makedirs(..., exist_ok=True)` → write `<path>.tmp` → `os.replace(...)`, which never
    required `<path>` to exist already, so an install killed during its first write of a target
    leaves a `<target>.tmp` with nothing beside it. These are listed for you to look at, and are
    removed only under the separate `--delete-orphans`.

A sibling means a regular FILE named `<target>` – following a symlink, since a target you version
by symlinking is still a target. A DIRECTORY of that name is not corroboration and the temp goes
in the second group: `os.path.lexists` counted one, which is how `..tmp` and `...tmp`, whose
"target" is the search directory and its parent, reached the first group (twenty-third pass, F1).

The sibling is not a fact about the old idiom – it is a corroborating signal, and the split is
a deliberate safety TRADE. A `<target>.tmp` with a `<target>` beside it at least matches the
shape the idiom left behind most of the time; a sibling-less one is equally consistent with a
scratch file of your own that you happened to name after one of qq's files, and there is no
second signal to weigh. Under-deleting is the safe direction and silence is not, so the tool
lists both and puts the second group behind its own flag.

WHERE IT LOOKS, and it is a short list because the subject is short. Every one of the fourteen
places this package used the old idiom (thirteen in the package, one in `setup.sh`, read at
`9b1c711` – the last commit before `atomicio`) writes into one of these:

  * the config file's own directory, for `<config>.tmp` (`admin.py`'s `qq config set`);
  * the state directory, for `pending-findings.md.tmp` (`authgate.py`, `checks.py`,
    `procprobe.py`) and for the three path settings that default into it – the xref content
    store, the xref waveoffs, the reconcile snapshot;
  * `<state>/refs`, one level down, for `refs.jsonl.tmp` and `events.jsonl.tmp` (`refs.py`,
    `refsresolve.py`). That is the only subdirectory any idiom site reached, and it is reached
    by name;
  * the embedding cache's directory, for the cache, its identity-scoped siblings and their
    `.orphan-ages.json` sidecars (`search.py`);
  * `~/.claude`, for `settings.json.tmp` (`setup.sh`).

NOT the note store. `QUINTESSENCE_DIR` used to be in this list, walked whole, on the strength of
a claim that `admin.py` wrote a `.gitignore` through the idiom; it does not, at any revision –
that write is a plain `open(..., "w")`. No idiom site resolves under the store, so there is no
litter there for this script to claim, and an operator's `memoir.tmp` beside their `memoir` was
the only thing the walk could ever find (twenty-second pass, F1).

Directories are searched but NOT walked: one `os.scandir` each, no recursion. The one depth the
subject needs, `<state>/refs`, is a search directory in its own right.

Paths are taken as CONFIGURED (with `~` expanded), not resolved. This is the direction the old
idiom wrote in: `admin.py`'s target was `Config.config_path` and `search.py`'s was `cache_path`,
neither of which is realpath'd, so `open(path + ".tmp", "w")` created the temp beside the LINK.
A config you version by symlinking it into a dotfiles repository therefore has its litter in the
config directory, and the repository is not searched at all – it holds your files, not qq's, and
resolving first is how `--delete` came to remove an `init.lua.tmp` from one.

WHAT IT DOES NOT REACH, worth saying because a clean listing is not proof of a clean disk:

  * A target under a path setting you have since repointed – the litter is beside where the
    file WAS, and this script only knows where it is now. Likewise a store you no longer have
    configured, and any directory outside the ones above. If you know of one, pass `--dir`:
    a directory you name is searched for ANY `<name>.tmp` (still without recursing), because
    outside qq's own settings the script has no target names to go on. That widening is yours
    to ask for, and the listing says which directories carry it.
  * A `<target>.tmp` whose whole directory is gone (an interrupted first write into a state
    subdirectory you have since deleted takes its litter with it – nothing to reach). A
    directory your settings name and this host does not have is REPORTED as unsearched rather
    than dropped from the listing: a shorter search must not read as a cleaner disk.
  * A `<target>.tmp` in the directory a symlinked target RESOLVES into. The old idiom never
    wrote there; only the current writer does, and its temps are `<target>.tmp.<12 hex>`, which
    `atomicio` reclaims itself. `--dir` covers you if you believe otherwise.
  * An identity-scoped embedding cache belonging to a corpus root or embed model you no longer
    have configured, AND whose cache file is no longer beside its temp. The identity for your
    current settings is derived exactly; the others are recognised only by shape, and only when
    the cache itself sits beside the temp to corroborate it.
  * WHICH of the sibling-less ones are actually this package's. The script cannot tell, and
    says so rather than guessing: it reports the group and leaves the judgement with you. A
    run that deletes nothing from that group is not evidence the group was clean.
  * Anything not spelled `<target>.tmp`. The old idiom produced only that spelling, so this is a
    complete reach over ITS litter – but any other tool's leftovers are none of this script's
    business and are not looked for.
  * A DIRECTORY named `<target>.tmp`. The old idiom wrote its temp with `open()`, so a directory
    of that name is not its litter, and `os.unlink` could not remove one anyway – it used to be
    listed and then refused, which is a line in the report that no flag can act on. Symlinks
    ARE listed: `os.unlink` removes the link, and a dangling `<target>.tmp` link is real litter.

Ages are reported for everything found. Both delete flags remove only what is older than the
same one-hour grace `atomicio` uses, because a younger one may be another tool's file in
flight; the younger ones are listed and left, and the script says so rather than passing over
them silently.

Usage:
    python3 tools/reclaim_legacy_temps.py                     # list what is there, delete nothing
    python3 tools/reclaim_legacy_temps.py --delete            # delete the aged ones with siblings
    python3 tools/reclaim_legacy_temps.py --delete-orphans    # delete the aged sibling-less ones
    python3 tools/reclaim_legacy_temps.py --dir PATH ...      # search PATH too, for any `.tmp`
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import types
from collections import namedtuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINE = os.path.dirname(_HERE)
if _ENGINE not in sys.path:
    sys.path.insert(0, _ENGINE)

from quintessence.atomicio import TEMP_GRACE_SECONDS  # noqa: E402
from quintessence.config import KEYS, Config  # noqa: E402
from quintessence.search import (CHUNKER_VERSION, SearchIndex, cache_identity,  # noqa: E402
                                 identity_cache_path)

TEMP_SUFFIX = ".tmp"

# Path settings whose value names a FILE qq replaces wholesale, so the old idiom would have left
# its temp beside that file. Named rather than taken from every path key in the registry, because
# most of the others name a directory you own – `QQ_KB_ROOT`, `QQ_DOCDIR` – where a `notes.tmp`
# beside `notes` is your business and nothing to do with this package. Checked against the
# registry at startup, so a key renamed out from under this list is a loud failure and not a
# silently shorter search.
FILE_TARGET_KEYS = (
    "QQ_CONFIG",                # cli.py's `qq config set` (the value read is Config.config_path,
                                # which is what admin.py wrote beside; the key must still exist)
    "QQ_CACHE",                 # search.py's embedding cache (and its identity-scoped siblings)
    "QQ_XREF_CONTENT",          # xref.py's content-hash store
    "QQ_XREF_WAVEOFFS",         # xref.py's waveoffs
    "QQ_RECONCILE_SNAPSHOT",    # reconcile.py's snapshot
)

# The directory whose contents qq names itself rather than configuring. `QUINTESSENCE_DIR` is NOT
# here: no idiom site ever wrote under the store (see the docstring).
DIR_TARGET_KEYS = ("QQ_STATE_DIR",)

# Read only to re-derive the embedding cache's identity, exactly as `SearchIndex.__init__` does.
IDENTITY_KEYS = ("QQ_KB_ROOT", "QQ_EMBED_MODEL")

# Targets under the state directory whose names come from the package, not from a setting, with
# the idiom site each was read from. `(relative directory, basename)` – the empty relative
# directory is the state directory itself, and `refs` is the one level down any idiom site
# reached.
STATE_TARGETS = (
    ("", "pending-findings.md"),    # authgate.py, checks.py, procprobe.py
    ("refs", "refs.jsonl"),         # refs.py, refsresolve.py (two sites)
    ("refs", "events.jsonl"),       # refsresolve.py
)

# `~/.claude` has no entry in the registry at all – it comes from setup.sh's `CLAUDE_SETTINGS`,
# whose default is spelled here.
CLAUDE_SETTINGS_DEFAULT = os.path.join("~", ".claude", "settings.json")

# What `search.cache_identity` builds: a 16-character sha256 prefix, a slug of the embed model
# with every run of non-alphanumerics collapsed to `-`, and the chunker version. Written out
# because an identity for a corpus root or model you no longer have configured cannot be
# re-derived – only recognised – and CHECKED against the live function at startup, so this
# spelling cannot drift away from it silently.
IDENTITY_SHAPE = r"[0-9a-f]{16}-[A-Za-z0-9-]+-v[0-9]+"

# A search directory, the target names to claim in it, and (for the embedding cache) the shape a
# name may match INSTEAD, when a file of that name sits beside the temp to corroborate it.
# `basenames is None` means "any name": a directory the operator named with --dir, where there
# are no target names to go on.
Spot = namedtuple("Spot", "directory basenames sibling_shape")


def orphan_ages_suffix() -> str:
    """What `search.py` appends to a cache path for its sidecar, asked of `search.py`.

    Spelling it here would be a second copy of a literal that lives in
    `SearchIndex._orphan_ages_path`, and the copy would go stale silently -- this script would
    then look for a name nothing writes and report a clean cache directory. The method reads
    `self.cache_path` and nothing else, so an empty one returns exactly the suffix. If that stops
    being true it raises here, at startup, which is the failure this script wants.
    """
    try:
        suffix = SearchIndex._orphan_ages_path(types.SimpleNamespace(cache_path=""))
    except Exception as exc:      # the method grew a dependency this cannot supply
        raise SystemExit(f"reclaim_legacy_temps: cannot ask search.py what it names the "
                         f"orphan-ages sidecar ({exc}). This script's search would silently "
                         f"miss that file; fix the derivation rather than guessing the name.")
    if not suffix or suffix.startswith(os.sep):
        raise SystemExit(f"reclaim_legacy_temps: search.py's orphan-ages sidecar is no longer a "
                         f"suffix on the cache path (got {suffix!r}); this script's derivation "
                         f"has drifted from the package.")
    return suffix


def cache_target_names(cfg: "Config") -> tuple:
    """`(basenames, sibling_shape)` for the embedding cache directory.

    `search.py` writes four shapes of file there through the old idiom: the legacy
    (non-identity-scoped) cache named by `QQ_CACHE`, the identity-scoped cache for the corpus
    root and embed model in force, and an `.orphan-ages.json` sidecar beside each. The identity
    is re-derived here from the same three inputs `SearchIndex.__init__` uses, through the same
    function, so it is exact rather than pattern-matched.

    An identity for a corpus root or model NO LONGER configured cannot be re-derived. Those are
    matched by shape instead -- and only when the cache file itself sits beside the temp, which
    is a second signal that the name is one search.py made rather than one you did.
    """
    legacy = cfg.get_path("QQ_CACHE")
    identity = cache_identity(cfg.get_path("QQ_KB_ROOT"), cfg.get_str("QQ_EMBED_MODEL"),
                              CHUNKER_VERSION)
    if not re.fullmatch(IDENTITY_SHAPE, identity):
        raise SystemExit(f"reclaim_legacy_temps: search.cache_identity now produces {identity!r}, "
                         f"which this script's IDENTITY_SHAPE does not describe. A cache from an "
                         f"identity you no longer have configured would go unrecognised; fix the "
                         f"shape rather than searching a shorter estate quietly.")
    sidecar = orphan_ages_suffix()
    names = set()
    for path in (legacy, identity_cache_path(legacy, identity)):
        base = os.path.basename(path)
        names.add(base)
        names.add(base + sidecar)
    # The same construction `identity_cache_path` performs, with the identity left open.
    stem, ext = os.path.splitext(os.path.basename(legacy))
    ext = ext or ".json"
    shape = re.compile("%s\\.(?:%s)%s(?:%s)?" % (re.escape(stem), IDENTITY_SHAPE, re.escape(ext),
                                                 re.escape(sidecar)))
    return names, shape


def search_spots(cfg: "Config", extra=()) -> tuple:
    """`(spots, unsearchable)`: every directory to search with the target names to claim in it,
    in a stable order, and every directory the settings NAME but this host cannot offer.

    Paths are taken as configured (`~` expanded, symlinks NOT resolved) because that is where the
    old idiom wrote -- see the module docstring. Directories are de-duplicated by real path, so
    two settings pointing at one directory are searched once with both their names.

    A directory that does not exist is not searched -- there is nothing to search -- but it IS
    returned, because dropping it in silence is how a shorter search comes to read as a cleaner
    disk. The caller prints them: this tool's whole product is a list an operator trusts, and a
    spot missing from that list without a word is the false clean report it exists to prevent
    (twenty-third pass, F5, where a literal `~` made one vanish including from the header).
    """
    missing = [k for k in FILE_TARGET_KEYS + DIR_TARGET_KEYS + IDENTITY_KEYS
               if k not in {kd.name for kd in KEYS}]
    if missing:
        raise SystemExit(f"reclaim_legacy_temps: {', '.join(missing)} is no longer in the config "
                         f"registry. This script's list of write targets has drifted from the "
                         f"package; fix the list rather than searching a shorter estate quietly.")

    found = []      # (directory, basenames or None, sibling_shape or None)
    for key in FILE_TARGET_KEYS:
        # `qq config set` wrote beside `Config.config_path`, which is the config file this
        # process is reading -- not necessarily what `QQ_CACHE`-style resolution would give.
        value = cfg.config_path if key == "QQ_CONFIG" else cfg.get_path(key)
        if not value:
            continue
        if key == "QQ_CACHE":
            names, shape = cache_target_names(cfg)
            found.append((os.path.dirname(value), names, shape))
        else:
            found.append((os.path.dirname(value), {os.path.basename(value)}, None))
    for key in DIR_TARGET_KEYS:
        value = cfg.get_path(key)
        if value:
            for relative, basename in STATE_TARGETS:
                found.append((os.path.join(value, relative) if relative else value,
                              {basename}, None))
    # The expansion is applied to the ENV value as well as to the default. It used to wrap only
    # the default, so `CLAUDE_SETTINGS='~/.claude/settings.json'` -- a literal tilde, which is
    # what a quoted assignment or an env file hands over -- gave `os.path.dirname` the string
    # `~/.claude`, no directory of that name existed, and the spot was dropped without a word
    # (twenty-third pass, F5). Every other path this script searches is expanded: `Config`
    # expands the ones it hands over, and `--dir` values are expanded below. This is the same
    # rule, applied to the one path that comes from the environment directly.
    #
    # SEAM, and it cuts the other way: setup.sh reads the same variable and does NOT expand it,
    # so a literal tilde makes IT write into a directory named `~` beside wherever it ran. That
    # is setup.sh's to fix; searching the operator's real home is the right reading of the
    # setting here, and `--dir` covers anyone who finds a literal `~` directory on disk.
    settings = os.path.expanduser(os.environ.get("CLAUDE_SETTINGS")
                                  or CLAUDE_SETTINGS_DEFAULT)
    found.append((os.path.dirname(settings), {os.path.basename(settings)}, None))
    # A directory the operator named: no target names to go on, so any `<name>.tmp` in it. Still
    # one directory, still not walked.
    found.extend((os.path.expanduser(d), None, None) for d in extra)

    ordered, by_real, unsearchable = [], {}, {}
    for directory, names, shape in found:
        if not directory or not os.path.isdir(directory):
            # Named by a setting, not searchable here. Kept and reported rather than dropped:
            # "the config file's directory does not exist" is a fact about this host that
            # changes what the listing below means, and the operator is the one who knows
            # whether a setting is pointed somewhere it should not be.
            unsearchable[directory or "(the value names no directory)"] = (
                "not searched: no such directory on this host")
            continue
        real = os.path.realpath(directory)
        if real not in by_real:
            by_real[real] = len(ordered)
            ordered.append([directory, None if names is None else set(names), shape])
            continue
        merged = ordered[by_real[real]]
        if names is None or merged[1] is None:
            merged[1] = None                    # "any name" wins: the operator asked for it
        else:
            merged[1] |= names
        merged[2] = merged[2] or shape
    # `unsearchable` is keyed by the directory string, so two settings under one absent parent
    # are reported once -- the same collapsing the searched list gets from its realpath keys.
    return ([Spot(directory, None if names is None else frozenset(names), shape)
             for directory, names, shape in ordered],
            sorted(unsearchable.items()))


def _claimed(spot: "Spot", stem: str, has_sibling: bool) -> bool:
    """Is `<stem>.tmp` in this directory a name the old idiom could have left here?"""
    if stem in ("", os.curdir, os.pardir):
        # A stem that is not a NAME. These three are the complete class, and the class is
        # complete because a filename cannot contain `os.sep`: for every other stem,
        # `os.path.join(directory, stem)` names a distinct entry IN this directory, which may or
        # may not be there -- so the sibling test is asking a real question. For these three it
        # is not: `""` and `"."` join back to the directory itself and `".."` to its PARENT, all
        # of which always exist, so the one corroborating signal this tool has could only ever
        # answer True. That put all three in the with-a-sibling group, the one plain `--delete`
        # empties, past the `--delete-orphans` gate this design puts uncorroborated files behind.
        #
        # `""` alone was closed at the twenty-second pass, F4 -- by a guard spelled `if not
        # stem`, which is the instance and not the class, and `..tmp` and `...tmp` went on being
        # deleted (twenty-third pass, F1). Naming the class here is what makes the answer
        # checkable: the question "which stems make the join always exist" has exactly three
        # answers and all three are on this line.
        #
        # `....tmp` (stem `"..."`) is NOT refused, and that is the boundary on the other side: it
        # is an ordinary name, nothing named `...` need sit beside it, and the sibling test
        # answers honestly for it. It lands in the orphan group like any other name qq never
        # wrote, for the operator to judge -- refusing it as well would be this tool guessing
        # where it has a signal.
        #
        # The old idiom wrote `<target> + ".tmp"` and every one of its targets had a name, so
        # none of the three can be its litter under any configuration. Refused on the name,
        # before the sibling question is asked: in a `--dir` the operator named, and in a derived
        # directory whose target names would otherwise include `""` -- a path setting written
        # with a trailing slash gives `os.path.basename` exactly that.
        return False
    if spot.basenames is None:
        return True
    if stem in spot.basenames:
        return True
    return bool(has_sibling and spot.sibling_shape and spot.sibling_shape.fullmatch(stem))


def legacy_temps(spot: "Spot") -> tuple:
    """`(with_sibling, orphans)`: every claimed `<target>.tmp` in `spot.directory`, split on
    whether `<target>` is there. Both are lists of `(path, age_seconds)`, sorted.

    The split is not a claim about which files the old idiom could produce – it wrote its temp
    before the `os.replace` that creates the target, so an interrupted first write leaves an
    orphan. It is a claim about how much corroboration each group carries, and the two groups
    are offered for deletion under different flags because of it.

    One `scandir`, no recursion: the directories come from the idiom's own targets and so does
    the one depth it needs (`<state>/refs` is its own search directory). Symlinks are stat'd
    without following, the way the reclaim this replaces did: a dangling one would otherwise
    raise, and a link's own age is the age of the link.
    """
    now = time.time()
    with_sibling, orphans = [], []
    try:
        entries = list(os.scandir(spot.directory))
    except OSError as exc:
        print(f"  ? {spot.directory} (cannot read: {exc.strerror})")
        return [], []
    for entry in entries:
        if not entry.name.endswith(TEMP_SUFFIX):
            continue
        stem = entry.name[:-len(TEMP_SUFFIX)]
        path = os.path.join(spot.directory, entry.name)
        # NOT a directory. The old idiom wrote its temp with `open(...)`, so a directory named
        # `<target>.tmp` is not its litter under any configuration -- and listing one is not a
        # harmless extra line: it went into the aged group, `--delete` tried it, `os.unlink`
        # refused with `Is a directory`, and the run exited non-zero over a file it had claimed
        # itself (twenty-third pass, F4). Nothing can be LOST that way, since unlink cannot
        # remove a directory; what is lost is the meaning of a listing this tool exists to
        # produce.
        #
        # The test is "not a directory", not "a regular file", because a directory is exactly
        # what `os.unlink` refuses and it removes every other kind of entry. A SYMLINK named
        # `<target>.tmp` is deletable -- unlink takes the link -- and a dangling one has been a
        # required listing since the twentieth pass, F3, so narrowing to regular files here
        # would drop a case an earlier round put in on purpose. `lstat` semantics for the same
        # reason the age below uses them: a link is judged as itself, not as what it points at.
        try:
            if entry.is_dir(follow_symlinks=False):
                continue
        except OSError as exc:
            print(f"  ? {path} (cannot stat: {exc.strerror})")
            continue
        # A regular FILE beside it, not merely something of that name. The sibling is the only
        # corroboration this tool has, and what it stands for is "the target the old idiom was
        # replacing is here" -- a target is a file. `os.path.lexists` answered True for a
        # DIRECTORY of that name, which is the mechanism the not-a-name stems above rode into the
        # corroborated group, and it is not about dots: a `--dir` holding a directory `mydata`
        # and a file `mydata.tmp` is the same false corroboration with no dot in it. Demoting
        # those to orphans is the safe direction -- they are listed, under the flag that says
        # nothing corroborates them.
        #
        # Symlinks ARE followed here, unlike the temp's own stat below. The two ask different
        # questions: the temp's age is the age of the link itself, while the sibling question is
        # whether a target file is there, and a config symlinked into a dotfiles repository --
        # the case this tool's path handling is built around -- answers yes. A DANGLING sibling
        # link answers no, and correctly: there is no target beside the temp to corroborate it.
        has_sibling = os.path.isfile(os.path.join(spot.directory, stem))
        if not _claimed(spot, stem, has_sibling):
            continue
        try:
            age = now - entry.stat(follow_symlinks=False).st_mtime
        except OSError as exc:
            print(f"  ? {path} (cannot stat: {exc.strerror})")
            continue
        (with_sibling if has_sibling else orphans).append((path, age))
    return sorted(with_sibling), sorted(orphans)


def _age(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f} minutes old"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} hours old"
    return f"{seconds / 86400:.1f} days old"


def _scope_line(spot: "Spot") -> str:
    """What this directory is searched FOR, said in the same names an operator would see on
    disk. The line an operator reads before passing `--delete` has to describe the search that
    actually runs -- "searching 5 directories" described a five-subtree walk for any name at all
    (twenty-second pass, F1)."""
    if spot.basenames is None:
        return f"  · {spot.directory} — any `<name>{TEMP_SUFFIX}` (you named this one with --dir)"
    names = ", ".join(sorted(name + TEMP_SUFFIX for name in spot.basenames))
    if spot.sibling_shape is not None:
        names += (f", and an identity-scoped cache's `<name>{TEMP_SUFFIX}` when the cache itself "
                  f"is beside it")
    return f"  · {spot.directory} — {names}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Find pre-atomicio <target>.tmp litter beside the files qq writes.",
        epilog="Lists by default, in two groups by whether a `<target>` sits beside the temp. "
               "Nothing is deleted unless you pass --delete or --delete-orphans.")
    ap.add_argument("--delete", action="store_true",
                    help="delete the aged ones that have a sibling (default: list only)")
    ap.add_argument("--delete-orphans", action="store_true",
                    help="delete the aged ones with NO sibling; independent of --delete, "
                         "because that group carries nothing corroborating it — read it first")
    ap.add_argument("--dir", action="append", default=[], metavar="PATH",
                    help="search PATH as well, for any `<name>.tmp` in it; repeatable")
    args = ap.parse_args(argv)

    spots, unsearchable = search_spots(Config(), args.dir)

    def _say_unsearchable() -> None:
        """Name every directory the settings reach that this host does not have.

        Said in both branches below, including the one that searches nothing at all. A spot
        dropped in silence makes a shorter search read as a cleaner disk, which is the one
        reading this tool must never invite: it cannot know whether a missing directory is a
        setting pointed somewhere stale (litter beside where the file WAS) or simply a feature
        never used. The operator can.
        """
        if not unsearchable:
            return
        print(f"  {len(unsearchable)} director{'y' if len(unsearchable) == 1 else 'ies'} your "
              f"settings name is not on this host, so nothing there is in this listing:")
        for directory, why in unsearchable:
            print(f"  · {directory} — {why}")

    if not spots:
        print("reclaim_legacy_temps: no qq directory on this host exists yet — nothing to search. "
              "That is not the same as nothing to find: if this is not the machine qq runs on, "
              "run it there.")
        _say_unsearchable()
        return 0

    print(f"reclaim_legacy_temps: searching {len(spots)} directories (not their subdirectories), "
          f"each for the names qq's pre-atomicio idiom wrote in it:")
    for spot in spots:
        print(_scope_line(spot))
    _say_unsearchable()

    # Keyed by path: two search directories can nest (an operator who points QQ_XREF_CONTENT
    # inside the state directory, or passes --dir on a directory already searched), and a file
    # found twice must be reported once and offered for deletion once.
    hits, orphan_hits = {}, {}
    for spot in spots:
        with_sibling, orphans = legacy_temps(spot)
        for path, age in with_sibling:
            hits[path] = age
        for path, age in orphans:
            orphan_hits[path] = age

    def _split(found: dict) -> tuple:
        aged, young = [], []
        for path, age in sorted(found.items()):
            (aged if age >= TEMP_GRACE_SECONDS else young).append((path, age))
        return aged, young

    aged, young = _split(hits)
    orphans_aged, orphans_young = _split(orphan_hits)

    if not (aged or young or orphans_aged or orphans_young):
        print("\nNo `<target>.tmp` of any shape in those directories. Nothing to do.")
        return 0

    def _listing(entries: list, heading: str) -> None:
        if not entries:
            return
        print(f"\n{heading}")
        for path, age in entries:
            print(f"  {path}  ({_age(age)})")

    _listing(aged, f"{len(aged)} legacy temp(s) past the one-hour grace:")
    _listing(young, f"{len(young)} legacy temp(s) INSIDE the one-hour grace, left alone either "
                    f"way — a file this young may belong to something still running:")
    _listing(orphans_aged + orphans_young,
             f"{len(orphans_aged) + len(orphans_young)} possible orphan(s) — no sibling, review "
             f"by hand. An interrupted FIRST write leaves a `<target>.tmp` with no `<target>` "
             f"beside it, so these may be the same litter; a scratch file of your own named "
             f"after one of qq's looks identical, and there is no second signal to tell them "
             f"apart:")

    if not (args.delete or args.delete_orphans):
        print(f"\nNothing was deleted. These are yours to look at first — the old idiom's litter "
              f"and a backup you took by hand are spelled identically, which is why qq no longer "
              f"deletes either. Re-run with --delete to remove the {len(aged)} aged one(s) with a "
              f"sibling"
              + (f", and --delete-orphans for the {len(orphans_aged)} aged one(s) without"
                 if orphans_aged else "") + ".")
        return 0

    claimed = (aged if args.delete else []) + (orphans_aged if args.delete_orphans else [])
    left_by_flag = []
    if orphans_aged and not args.delete_orphans:
        left_by_flag.append(f"{len(orphans_aged)} aged orphan(s) need --delete-orphans")
    if aged and not args.delete:
        left_by_flag.append(f"{len(aged)} aged temp(s) with a sibling need --delete")

    removed = failed = 0
    print()
    for path, _age_s in sorted(claimed):
        try:
            os.unlink(path)
        except OSError as exc:
            print(f"  ! could not delete {path}: {exc.strerror}")
            failed += 1
        else:
            print(f"  deleted {path}")
            removed += 1
    inside_grace = len(young) + len(orphans_young)
    print(f"\nDeleted {removed} of {len(claimed)}"
          + (f", {failed} refused" if failed else "")
          + (f"; {inside_grace} inside the grace were left" if inside_grace else "")
          + (f"; {'; '.join(left_by_flag)}" if left_by_flag else "") + ".")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
