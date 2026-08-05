# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.atomicio — the one atomic file write, symlink-aware and streaming.

Every *replace-the-whole-file* write in the package goes through `atomic_write`: open a sibling
temp file, let the caller stream into it, then `os.replace` it into place. A reader sees either
the whole old file or the whole new one, never a half-written one. (Not literally every write:
`write.py` puts HEAD bodies and the index down with `Path.write_text`, which is non-atomic but
does follow symlinks, so the clobber problem below does not reach them; they are covered by
`qq_lock` and by git.)

WHY THIS IS A MODULE AND NOT AN INLINE IDIOM. `os.replace` does not follow symlinks -- it replaces
the *link* with a regular file. Nine modules had each hand-rolled the same three lines, so a config
or state file the operator had symlinked into a git repo (the normal way to version-control a
dotfile) was silently detached the first time qq wrote it: the link became a real file, the repo
copy kept the old contents, and nothing reported it. Resolving the destination first makes qq
behave the way a shell redirect or an editor does -- write *through* the link to the real file.

The temp file is created beside the RESOLVED destination, which is what keeps `os.replace` atomic:
it is only atomic within a single filesystem, and the link may well point across one.

WHY A CONTEXT MANAGER RATHER THAN `write(path, text)`. The callers include the embedding cache,
which is ~350 MB here. Serialising to a string first and writing that costs roughly two extra
copies of the file in RSS (measured: a 35 MB payload cost +71 MB peak). So the primitive hands out
a file handle and callers stream into it -- `json.dump`, not `fh.write(json.dumps(...))`.

That trade has a cost on the other side, deliberately accepted: `json.dump` runs about 2.1x slower
than `json.dumps` plus one write (85 MB payload: 2.31 s vs 1.09 s), and `_save_cache` runs inside a
held flock, so a checkpoint holds that lock some seconds longer than it used to. Seconds of lock
hold in exchange for not spiking ~700 MB of RSS on a machine that also runs local models.

A failed write removes its temp file -- including when the failure is an interrupt rather than an
ordinary exception, and including an interrupt that lands during the cleanup itself. The
hand-rolled idiom this replaces left `<path>.tmp` behind on any exception.

This module also DELETES, which is worth knowing before reading further: after each write it
sweeps the RESOLVED target's own directory for stale litter -- through a symlink that is the
directory the link points into, not the one the link sits in, so a config symlinked into a
dotfiles repo has this rule applied inside that repo. There is exactly ONE name it takes: the
generated `<target>.tmp.<12 lowercase hex>`, and of those only the ones whose tail carries at
least one of `abcdef` -- nothing WIDER than what this module writes, and deliberately a little
NARROWER. The letter is required because twelve digits are twelve valid hex characters, so
`date +%Y%m%d%H%M` -- how a person names a backup -- sat inside the width; the price is that
about one generated temp in 281 has an all-decimal tail and is not reclaimed here. Under-deletion,
on purpose, and `is_generated_temp_name` states the same two conditions where it applies them.
It was once a WINDOW of 8-or-more alphanumerics, on the belief that mkstemp-era temps from an
earlier build might be on disk; publication history says none can be, and the window was deleting
operators' own `report.tmp.20260804`-shaped files an hour after they were written.

A SECOND rule used to sit beside it: the legacy `<target>.tmp` exactly, the spelling the
pre-atomicio idiom left behind on any exception. It was removed on 2026-08-04 (owner's ruling,
twentieth pass F2). Nothing in that name separates the old idiom's litter from an operator's own
`cp config config.tmp`, so it was a standing licence to delete a file this package never writes,
bought for a one-time migration -- and a permanent deletion rule is the wrong price for a
transient need. Litter a pre-atomicio install really does hold is reclaimed by
`tools/reclaim_legacy_temps.py`, once, when its operator chooses to: an opt-in pass that lists
before it deletes, which is what a one-time job should look like. RELEASE-NOTES.md says so to
anyone upgrading. So a name this module deletes is now, without exception, a name this module
wrote. `_reclaim_stale_temps` gives the rule in full, and ASSURANCE.md states it for an operator.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
import sys
import time


TEMP_INFIX = ".tmp."      # a temp is spelled "<target-basename>.tmp.<random>"

# The random tail this module appends. Named here so the writer and the recogniser below cannot
# drift apart if the width ever changes.
_TEMP_TOKEN_BYTES = 6                          # -> 12 hex characters

# What the recogniser will accept as that tail: EXACTLY what the writer emits, and nothing wider.
# Both halves derived from _TEMP_TOKEN_BYTES, so the writer and the recogniser cannot drift.
#
# It used to be a window -- 8 or more characters of mixed-case alphanumerics and underscore --
# justified by mkstemp temps "still on disk after an upgrade" from an earlier build. That
# justification was never checked against publication history, and it does not survive the check:
# the mkstemp call existed only between `27dff8a` and `73e2050`, and the only install that ever
# ran that code is this machine's own dist mirror, whose reflog puts it at those commits from
# 10:40 to 15:54 on 2026-08-03. No operator anywhere holds an 8-character-tail temp written by
# this module, so the window bought nothing -- and it cost the shapes an operator DOES have:
# `report.tmp.20260804`, `backup.tmp.snapshot` and `other-tool.tmp.a1b2c3d4` all satisfied it and
# were deleted an hour after they were written, in a directory whose documented age policy is 60
# days. Beside a target, `notes.tmp.markdown` died while `notes.tmp.md` -- the case the docstring
# highlighted protecting -- survived, which is the whole argument that the window was drawn in the
# wrong place (eighteenth pass, F5).
#
# A bound's justification is a claim like any other and owes a source check. This one is written
# down so the next person to widen it has to falsify something specific.
_TEMP_TOKEN_CHARS = 2 * _TEMP_TOKEN_BYTES      # -> 12 hex characters, the writer's exact output
_TEMP_TOKEN_ALPHABET = frozenset("0123456789abcdef")

# And one more condition, which is NOT a description of the writer: at least one hex LETTER.
# Twelve digits are twelve valid lowercase hex characters, so `date +%Y%m%d%H%M` lands exactly
# inside the width -- `settings.json.tmp.202608041200`, the shape an operator gets from
# `cp settings.json settings.json.tmp.$(date +%Y%m%d%H%M)`, was deleted an hour after they took
# it (twenty-first pass, F2). No other rule here could see it: 8-digit `20260804` is out on width
# and 12-digit is not.
#
# This is a FALSE-POSITIVE SUPPRESSOR, and it costs something real. The writer emits an
# all-decimal tail (10/16)^12 of the time, about one in 281, and such a temp is now outside the
# one-hour reclaim: in the embedding-cache directory it still falls to the QQ_CACHE_GC_DAYS
# sweep, but in the state directory nothing else sweeps and it stays. That is under-deletion, the
# direction this project has twice ruled for -- a rule that deletes an operator's file is the one
# failure that costs them something they cannot get back (fifteenth pass F2; owner's ruling on
# the twentieth). The writer is deliberately NOT changed to guarantee a letter: that would close
# the gap, but it would put a second invariant across the python engine and setup.sh's shell
# mirror, and divergence between those two is its own recurring defect family here.
_TEMP_TOKEN_LETTERS = frozenset("abcdef")

# How long a temp file is assumed to belong to a write still in progress. A real one lives for the
# duration of one write; the slowest here is the ~350 MB embedding cache, seconds. An hour is a
# wide margin. Past it, a temp is litter from a hard kill and a sweeper may reclaim it -- which
# something must do, because unique names do not self-limit the way the old reused "<target>.tmp"
# did: every SIGKILL during a checkpoint leaves another one behind.
TEMP_GRACE_SECONDS = 3600


def is_temp_name(name: str) -> bool:
    """True for any name of EITHER temp spelling -- the generated `<target>.tmp.<tail>` and the
    legacy `<target>.tmp` -- without asking whether the tail is one this module emits.

    NOT a licence to skip. An earlier version of this docstring told a directory sweeper to skip
    everything this matches, and search.py's cache sweep did: the skip exited early, so an
    operator's `notes.tmp.md` sitting in the embedding-cache directory was carried past the
    QQ_CACHE_GC_DAYS age check as well and exempted from it permanently, in the one directory that
    sweep exists to bound (sixteenth pass, F2). Breadth is only free on a skip that falls through
    to the directory's ordinary policy; this one did not.

    A sweeper that does not know the write targets should use `is_generated_temp_name`, which is
    what search.py now does, and this predicate has NO caller in the package as a result. Nothing
    DELETES on it, and since 2026-08-04 nothing deletes on the legacy half at all: this is a
    recogniser, and the legacy spelling is still a real thing to recognise -- a pre-atomicio
    install can hold one, `tools/reclaim_legacy_temps.py` exists to clear it, and answering False
    here would make the predicate's own name a lie. Kept as the honest answer to "is this name
    either temp spelling", which `_reclaim_stale_temps` no longer needs asked.
    """
    base = os.path.basename(name)
    return base.endswith(".tmp") or TEMP_INFIX in base


def is_generated_temp_name(name: str) -> bool:
    """True only for a name `_open_unique_temp` could have produced -- `<target>.tmp.` and then
    exactly twelve lowercase hex characters -- and, of those, only the ones carrying at least one
    of `abcdef`.

    So this is narrower than the writer's own output, deliberately, and the gap is stated where
    the constants are: twelve digits are twelve valid hex characters, which put an operator's
    `date +%Y%m%d%H%M` backup inside the width, and the price of excluding it is that about one
    generated temp in 281 is no longer reclaimed at the one-hour grace. Every name this answers
    True to is still a name this module wrote; the reverse no longer holds and is not claimed.

    Narrower than `is_temp_name` by more than the infix: it also refuses the legacy
    `<target>.tmp` spelling, and that refusal is what decides what a caller may DELETE.

    `<target>.tmp` carries no random tail, so nothing in the name separates the old idiom's litter
    from an operator's own `session.tmp` or another tool's scratch file. Knowing the target
    basename does not fix that -- an operator's `cp config config.tmp` beside the config qq writes
    matches an exact test as readily as the litter does -- so since 2026-08-04 NOTHING in this
    package deletes on the legacy spelling: not this predicate, not a directory sweep, and not
    `_reclaim_stale_temps`, which used to claim it by exact match (owner's ruling, twentieth pass
    F2). Applying the broader predicate in a directory sweep deleted sibling identity caches and
    foreign files an hour after they were written, where the blanket skip it replaced had left
    them alone (eleventh pass, F1); the exact match was the same mistake with a shorter reach.
    """
    _, sep, token = os.path.basename(name).rpartition(TEMP_INFIX)
    return (bool(sep)
            and len(token) == _TEMP_TOKEN_CHARS
            and all(c in _TEMP_TOKEN_ALPHABET for c in token)
            and any(c in _TEMP_TOKEN_LETTERS for c in token))


def _reclaim_stale_temps(parent: str, basename: str) -> None:
    """Remove leftover temps for THIS target that are older than the grace period.

    Unique temp names do not self-limit the way the old reused `<target>.tmp` did: every hard kill
    leaves another one. Sweeping here, at the primitive, covers all fourteen atomic-write call
    sites and any future one -- the alternative was a sweeper per directory, and the first
    attempt at that covered only the embedding cache while most sites write to the state
    directory, which nothing swept at all. (That number is gated: tests/py/test_assurance_counts
    derives it from the code and checks every document that states it, including this one, which
    is why the phrasing is fixed.)

    Two conditions, not one. The prefix alone was the original guard, and it is not enough: beside
    a target named `notes` it also matches the operator's own `notes.tmp.md`, which this function
    would then delete an hour later. Prefix says "belongs to this target"; `is_generated_temp_name`
    says "carries a tail this module actually emits". A real file needs to fail only one of them.

    The second condition is the NARROW predicate. A wider one stood here until `eb6f3e0` deleted
    it -- `is_own_temp_name`, which answered True to anything merely ending `.tmp` -- and even
    combined with the prefix it over-claimed: `notes.tmp.backup.tmp` starts with `notes.tmp.` and
    ends `.tmp`, so an operator's own backup was reclaimed as litter (twelfth pass, F1). Widening
    this condition back out that way is the mutation that restores that defect.

    A BARE `<target>.tmp` SURVIVES -- it fails BOTH conditions, carrying neither the `<target>.tmp.`
    prefix nor a generated tail. Until 2026-08-04 a third clause matched it exactly, on the
    grounds that the pre-atomicio idiom left `<path>.tmp` behind on any exception and nothing
    else would ever sweep the state directory (ninth pass, F2). True, and still the wrong trade
    (owner's ruling, twentieth pass F2): the current writer cannot produce that name, so the
    clause could only ever delete a file this package did not write, and an operator's
    `cp config config.tmp` beside their config is indistinguishable from the litter it was aimed
    at -- reproduced end to end through `qq config set`. A permanent deletion rule for a one-time
    migration is the wrong shape; the migration is `tools/reclaim_legacy_temps.py`, run once, by
    choice, listing before it deletes, and announced in RELEASE-NOTES.md.

    Silent, deliberately: reclaiming litter is housekeeping and must not turn a successful write
    into a failure.
    """
    prefix = basename + TEMP_INFIX
    cutoff = time.time() - TEMP_GRACE_SECONDS
    with contextlib.suppress(OSError):
        for entry in os.scandir(parent):
            if not (entry.name.startswith(prefix) and is_generated_temp_name(entry.name)):
                continue
            with contextlib.suppress(OSError):
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(entry.path)


_TEMP_TRIES = 100

# What a temp name costs beside the target: the infix plus the hex tail. DERIVED from the two
# constants above rather than written down, so widening the tail moves this and the refusal
# message with it. The idiom this module replaced spent 4 (`.tmp`), so 13 bytes of an install's
# name room went away with the unique names -- see _name_budget_error and ASSURANCE.md.
_TEMP_NAME_OVERHEAD = len(TEMP_INFIX) + 2 * _TEMP_TOKEN_BYTES        # -> 17

_NAME_MAX_FALLBACK = 255        # every filesystem qq is documented against; used only if the
                                # kernel will not answer pathconf for this directory


class NameTooLong(OSError):
    """The target's own name leaves no room for its temp, so no write here can ever succeed.

    An OSError subclass, so every existing `except OSError` keeps catching it and no caller
    changes behaviour by accident. It exists so a caller can tell this failure apart from the
    others: an ENOSPC, an EACCES or a read-only mount may all be gone by the next attempt, and a
    housekeeping write that swallows them is tolerating a hiccup. This one is a property of the
    configured name -- it will fail identically on every attempt until an operator shortens the
    name -- so swallowing it hides a misconfiguration instead. `best_effort_write` below is the
    shape that keeps the first behaviour without the second.
    """


def _name_budget_error(parent: str, basename: str, cand: str) -> "NameTooLong | None":
    """The ENAMETOOLONG a too-long TARGET name causes, said in full -- or None if the name fits.

    None matters: ENAMETOOLONG also comes from the whole path exceeding PATH_MAX, which is a
    different problem with a different fix, and answering it with a basename budget would send
    the operator to the wrong place. So the budget is checked before the message is claimed.
    """
    limit = _NAME_MAX_FALLBACK
    with contextlib.suppress(OSError, ValueError, AttributeError):
        limit = os.pathconf(parent, "PC_NAME_MAX")
    used = len(os.fsencode(basename))
    if used + _TEMP_NAME_OVERHEAD <= limit:
        return None
    # "QQ_CACHE is the one qq target whose name an install chooses" is what this said until
    # 2026-08-04, and it is false: QQ_CONFIG, QQ_RECONCILE_SNAPSHOT, QQ_XREF_CONTENT and
    # QQ_XREF_WAVEOFFS all name a file an operator sets and this module writes. Sending an
    # operator whose QQ_RECONCILE_SNAPSHOT is too long to look at QQ_CACHE is worse than saying
    # nothing, so the message states the rule instead of naming one setting out of five.
    return NameTooLong(errno.ENAMETOOLONG,
                       f"an atomic write needs {_TEMP_NAME_OVERHEAD} bytes of temp-name room "
                       f"beside the target: this name is {used} bytes and the directory allows "
                       f"{limit}, so a file written atomically here may be at most "
                       f"{limit - _TEMP_NAME_OVERHEAD} bytes of name. Shorten it -- the name "
                       f"comes from a path setting you chose, and qq may derive a longer one "
                       f"from it than you wrote",
                       cand)


def _open_unique_temp(parent: str, basename: str, create_mode: int = 0o666) -> "tuple[int, str]":
    """Create a uniquely-named temp beside the target; return (fd, path).

    O_CREAT|O_EXCL with mode 0o666 so the KERNEL applies the umask, atomically, exactly as the
    plain `open(tmp, "w")` this module replaced did.

    `create_mode` is that mode, and a caller who already knows what the finished file must be
    passes it here rather than only chmod'ing afterwards: the kernel still applies the umask on
    top, so the temp is born no wider than intended and there is no window in which a wider mode
    is visible. The default is the plain-open behaviour, for a caller with nothing to carry.

    NOT tempfile.mkstemp, which forces 0600 and so left us needing to know the umask -- and there
    is no read-only umask syscall. Reading it means SETTING it and restoring it, and os.umask is
    process-global: during that window any other thread creating a file gets the wrong mode, and
    any child forked in the window inherits it for its whole lifetime (umask survives fork+exec).
    That is not hypothetical here -- the MCP servers run sync tool handlers in a threadpool and
    shell out to `qq`. Reproduced 2026-08-03: under `umask 077`, a concurrent thread created a
    file 0o644.

    O_EXCL also keeps the two properties mkstemp gave us: a unique name, so concurrent writers to
    one destination cannot truncate each other's in-flight file, and no following a symlink
    planted at the temp path.

    NAME LENGTH. The temp is 17 bytes longer than the target, so a name the filesystem accepts
    can still have no room for its own temp: at NAME_MAX 255, targets of 239-255 bytes fail here
    where a plain `open(target, "w")` succeeds -- 255, not 251, which is where the REPLACED idiom
    stopped (it spent 4 bytes on `.tmp`) and not where the filesystem does. That envelope is
    REFUSED, loudly and with the number, rather than worked around. Truncating the basename to fit
    would buy the write at the cost of both rules the reclaim runs on -- the temp must carry the
    target's basename as a prefix, and an operator reading ASSURANCE.md must be able to recognise
    `<target>.tmp.<tail>` -- and a truncated temp would be litter no sweep can claim, or worse,
    litter matching a DIFFERENT target's prefix in the same directory. The refusal is a one-line
    fix for the few operators who can hit it (a name this long has to come from one of the five
    path settings that name a file, QQ_CACHE above all, since qq derives 58 bytes on top of that
    one -- 63 when the configured name has no extension, identity-scoping having to supply one to
    insert before); a silent truncation would be a permanent rule change for everyone. Disclosed in
    ASSURANCE.md under the atomic-write limitations, and pinned at both ends of the band in
    tests/py/test_atomicio.py, which also reads the document's numbers back against the module's.
    """
    for _ in range(_TEMP_TRIES):
        cand = os.path.join(parent, f"{basename}{TEMP_INFIX}{os.urandom(_TEMP_TOKEN_BYTES).hex()}")
        try:
            return os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY, create_mode), cand
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno != errno.ENAMETOOLONG:
                raise
            budget = _name_budget_error(parent, basename, cand)
            if budget is None:
                raise                       # the whole PATH is too long, not this basename
            raise budget from exc
    raise OSError(errno.EEXIST, "could not create a unique temp file", parent)


def resolve_for_write(path: str | os.PathLike) -> str:
    """The real file a write to `path` should land on, with symlinks followed.

    A path that does not exist yet resolves to itself (with any symlinked parent directories
    resolved), so first-time creation behaves exactly as before.
    """
    return os.path.realpath(os.fspath(path))


@contextlib.contextmanager
def atomic_write(path: str | os.PathLike, encoding: str = "utf-8", mode: int | None = None):
    """Yield a writable handle whose contents replace `path` atomically on clean exit.

    Follows symlinks; creates the destination directory; removes the temp file if the body
    raises, so a failed write leaves no `.tmp` litter and the old file intact.

    `mode` is for a caller COPYING a file: the permission bits a file created here should get,
    for a caller that has a source file whose mode must travel with the contents. Without it a
    fresh file lands at the umask default, which is right for a file this package originates and
    wrong for a copy -- `shutil.copy2`, the idiom such callers replace, carries the source's
    mode, and dropping it published a deliberately-0600 embedding cache at 0644 (fourteenth
    pass, F1). An EXISTING destination's own mode still wins over this argument: keeping a
    rewrite from changing permissions is the older and stronger promise, and the one caller that
    passes `mode` cannot have an existing destination anyway. The other three wrappers do not
    take it because nothing copies through them; they can gain it the same way if that changes.
    """
    with _atomic_write_to(resolve_for_write(path), encoding, mode) as fh:
        yield fh


@contextlib.contextmanager
def _atomic_write_to(target: str, encoding: str, mode: int | None = None):
    """`atomic_write` on an ALREADY-resolved target, so the wrappers below resolve exactly once.

    Resolution is a path walk under whatever lock the caller holds, and these are hot paths
    (the embedding cache checkpoints repeatedly mid-build while holding its flock), so doing it
    twice per write was pure waste.
    """
    # realpath gives up on a symlink cycle and hands back the path unchanged -- which is still a
    # link, so os.replace would clobber it: exactly the silent detachment this module exists to
    # prevent. Refuse instead. (The hand-rolled idiom this replaced clobbered it silently.)
    if os.path.islink(target):
        raise OSError(errno.ELOOP,
                      "cannot resolve symlink for an atomic write (cycle?)", target)
    parent = os.path.dirname(target) or "."
    os.makedirs(parent, exist_ok=True)

    # A UNIQUE temp, created O_EXCL, rather than a predictable "<target>.tmp". Two things follow:
    #   - Two writers to one destination can no longer truncate each other's in-flight temp. They
    #     race only on the rename, so the loser's complete file is replaced by the winner's
    #     complete file -- last-writer-wins, never a splice of both. That matters because several
    #     callers write without holding a lock (xref's content store from candidates(), config_set,
    #     reconcile's snapshot), and adding a lock inside those paths risks the self-deadlock
    #     xref.py already documents.
    #   - O_EXCL means a symlink planted at the temp path cannot be followed, so a payload cannot
    #     be redirected and the destination cannot be turned into a link to somewhere else.
    # temp creation and fdopen are INSIDE the try. They were outside it, which stranded the temp if
    # fdopen raised (an unknown `encoding=` is enough) -- the same structural flaw as the chmod
    # placement fixed one revision earlier, on the lines immediately above it.
    fd = fh = tmp = None
    try:
        # THE MODE IS DECIDED BEFORE THE TEMP EXISTS, so the temp is never created wider than the
        # file it will become. A REWRITE keeps the destination's existing mode, so it does not
        # silently change permissions -- and, through a symlink, does not change them on the real
        # file. A FRESH file gets the umask default, which is right for a file this package
        # originates; the exception is a caller COPYING a file, which passes the source's mode as
        # `mode`, because the umask default is the wrong answer for a copy and dropping it
        # published a deliberately-0600 embedding cache at 0644 (fourteenth pass, F1). An
        # existing destination outranks `mode`, so the rewrite promise is unchanged -- which is
        # why the stat comes SECOND.
        # BEST-EFFORT: os.stat raises FileNotFoundError when there is no destination yet, and
        # that is the ordinary fresh-file case, not an error.
        carry = mode
        with contextlib.suppress(OSError):
            carry = stat.S_IMODE(os.stat(target).st_mode)

        fd, tmp = _open_unique_temp(parent, os.path.basename(target),
                                    0o666 if carry is None else carry)
        try:
            fh = os.fdopen(fd, "w", encoding=encoding)
        finally:
            # The handoff completes whether fdopen RETURNS OR RAISES, which is why this is a
            # finally and not the next statement. os.fdopen is io.open(fd, ...) with closefd
            # true, and io.open closes the descriptor itself when it fails part-way -- so
            # clearing `fd` only on success left the cleanup below closing an already-closed
            # number a second time (spied: two closes on fd 3, the second EBADF). An unknown
            # `encoding=` is enough to get there. In the MCP servers, which run sync handlers in
            # a threadpool, closing a number the process no longer owns can close another
            # thread's file -- the failure mode is silent and belongs to someone else's code.
            #
            # The trade is deliberate: if some future io.open failed WITHOUT closing, this leaks
            # one descriptor until the process exits. A leak is bounded and diagnosable; a
            # double close corrupts an unrelated file handle.
            fd = None

        # The create above applied `carry & ~umask`, which is never WIDER than `carry` and can be
        # narrower; this chmod puts back the bits the umask took, and it runs before the caller
        # writes a single byte. So the payload is never readable at a mode wider than the one the
        # destination ends up with -- which is what the comment here used to claim outright while
        # the temp was created at `0o666 & ~umask` and narrowed only afterwards. Instrumented at
        # the reviewed tip: `secret.tmp.*` existed at 0o644 before becoming 0o600, and a reader
        # who opened it in that window kept its access as the payload streamed in (sixteenth
        # pass, F7). The file is empty for the whole of the remaining window.
        # BEST-EFFORT: a filesystem with no unix modes (vfat, exfat, some CIFS/NFS) refuses
        # chmod, and that may not abort a write that used to succeed. A refusal now leaves the
        # file at the create mode, which is at most as permissive as intended -- it used to leave
        # it at the umask default, which could be wider.
        if carry is not None:
            with contextlib.suppress(OSError):
                os.chmod(tmp, carry)
        yield fh
        fh.close()
        os.replace(tmp, target)
    except BaseException:
        # Suppress failures in the cleanup itself: a close() or unlink() error here would
        # replace the caller's real exception with a misleading one. Covers a failed
        # os.replace too -- the temp goes, the existing file is left untouched.
        #
        # EVERY step runs, including after a step raises something that is not an Exception.
        # This suppressed `Exception` alone, so a KeyboardInterrupt arriving during the
        # cleanup's `fh.close()` flush -- an operator's second Ctrl-C while a ~350 MB cache
        # is being flushed -- escaped before `os.unlink` and stranded the temp, under a module
        # docstring promising that a failed write removes it (sixteenth pass, F1). An
        # interrupt is exactly when this matters: unique names mean the litter no longer
        # self-limits, so each one strands a fresh file.
        #
        # The interrupt is caught, not swallowed. The first one is re-raised once the unlink
        # has run, carrying the caller's real exception as its `__context__` -- the ordering a
        # `finally:` block gives, and dropping a Ctrl-C or a SystemExit would be its own defect.
        interrupt = None
        for _close in (lambda: fh.close() if fh is not None else None,
                       lambda: os.close(fd) if fd is not None else None,
                       lambda: os.unlink(tmp) if tmp is not None else None):
            try:
                _close()
            except Exception:
                pass
            except BaseException as exc:      # KeyboardInterrupt, SystemExit
                if interrupt is None:
                    interrupt = exc
        if interrupt is not None:
            raise interrupt
        raise
    _reclaim_stale_temps(parent, os.path.basename(target))


def atomic_write_text(path: str | os.PathLike, text: str, encoding: str = "utf-8") -> str:
    """Write `text` to `path` atomically, following symlinks. Returns the real path written."""
    target = resolve_for_write(path)
    with _atomic_write_to(target, encoding) as fh:
        fh.write(text)
    return target


def atomic_write_lines(path: str | os.PathLike, lines, encoding: str = "utf-8") -> str:
    """`atomic_write_text` for an iterable of already-newline-terminated strings, streamed.

    Takes any iterable, so a generator of lines never has to be materialised as one string.
    """
    target = resolve_for_write(path)
    with _atomic_write_to(target, encoding) as fh:
        fh.writelines(lines)
    return target


def atomic_write_json(path: str | os.PathLike, obj, **dumps_kw) -> str:
    """`atomic_write_text` for JSON, streamed via `json.dump`. Kwargs pass to `json.dump`."""
    target = resolve_for_write(path)
    with _atomic_write_to(target, "utf-8") as fh:
        json.dump(obj, fh, **dumps_kw)
    return target


@contextlib.contextmanager
def best_effort_write(what: str, path: str | os.PathLike):
    """For a housekeeping write that must not fail the work around it -- but must not go silent.

    Three writes keep bookkeeping beside the real thing: the search index's orphan-ages sidecar,
    the reconcile snapshot, and xref's content-hash store (that one at its three callers, since
    the write itself carries no handler). None is worth failing a search or a check over, so all
    of them wrote `except OSError: pass`, and that was right for what OSError meant when they
    were written -- a full disk, a permission, a read-only mount, all of them possibly gone by
    the next run.

    Then this module started raising ENAMETOOLONG on a target name it cannot build a temp
    beside, and `except OSError: pass` swallowed that too: with a long enough QQ_CACHE the
    sidecar stopped being written and nothing said so, which made ASSURANCE.md's "the refusal is
    loud" false at the only call site that could reach it (seventeenth pass, F1). The refusal
    was loud at the primitive and silent one frame up.

    So the two failures are told apart here rather than at each call site: a `NameTooLong` is a
    standing property of the configured name and gets one line on stderr naming the arithmetic;
    every other OSError keeps the old best-effort silence. The work continues either way -- a
    warning, not a raise, because a sidecar that cannot be written is still not a reason to fail
    the search that would have written it.
    """
    try:
        yield
    except NameTooLong as exc:
        sys.stderr.write(f"qq: {what} not written ({os.fspath(path)}): {exc.strerror}\n")
    except OSError:
        pass
