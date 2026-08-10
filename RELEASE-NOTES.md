<!-- SPDX-License-Identifier: AGPL-3.0-or-later
     Copyright (C) 2026 Thomas Lakofski -->

# Release notes

Changes that ask something of you, newest first. Everything else is in the commit log; this file
is for the things you would want to know before or just after pulling.

## Unreleased — update-lines say which agent wrote them

### What changed

`qq update` and `qq new` now put the writing session's identity into the update-line they compose,
between the timestamp and your text:

    > updated: 2026-08-09T06:42:13Z [claude-opus-5, session abcd1234] what changed and why

The model is read from that session's own transcript and the id from the harness environment.
Nothing is typed, which is the point: a marker an agent writes by hand is a claim, and the field a
model is least reliable about is its own identifier.

**Off-harness nothing is inserted.** With no agent session in the environment — a human at a
terminal, cron, a script — the line is byte-for-byte what it always was. There is no setting to
turn this on or off; the presence of an agent is the switch.

### What it asks of you

- **Stop typing `[Opus 5, ...]` markers into update-lines** if that was your convention; you will
  get two. Existing lines are untouched — nothing rewrites history.
- If you assert on update-line text in your own tooling, note that the marker sits *after* the
  timestamp. The stamp still leads the line, so anything keyed on it (the refs view's join, the
  digest's age ranking) is unaffected.
- If your test suite runs under an agent session and pins update-line bytes, neutralize
  `CLAUDE_CODE_SESSION_ID` for the run, or the same suite gives different answers depending on who
  invoked it. This package's own harness does exactly that (`tests/run.sh`).

A commit can carry the same identity as a git trailer, which lets a line's attribution be
cross-checked with `git log --format='%(trailers:key=Agent,valueonly=true)'` — but that needs a
`prepare-commit-msg` hook, and **no such hook ships with this package**. Without one, expect no
trailer and read the line on its own. Note also that where a subagent makes the write, the two can
legitimately disagree: the update-line names the subagent's model, a trailer written by the
estate's own hook names the parent's.

## Unreleased — the crash-litter sweep no longer touches a bare `<target>.tmp`

**If you cloned on or around 2026-08-02, read the last section — you may have one file to look
at.**

### What changed

Every whole-file write in this package goes through one atomic primitive
(`quintessence/atomicio.py`): write a sibling temp, then rename it over the target, so a reader
sees the whole old file or the whole new one and never a half-written one. Because the temp name
is unique, a process killed mid-write leaves a new temp behind each time rather than reusing one
path — so after each successful write the primitive sweeps its target's directory and removes
stale temps.

That sweep used to recognise **two** names beside a target:

- `<target>.tmp.<12 lowercase hex, at least one of them a letter>` — the name this package
  generates, and
- `<target>.tmp` — the name the *older*, pre-atomicio idiom left behind.

The second is gone. The sweep now removes only names the current writer can actually produce. A
bare `<target>.tmp` is left alone by this sweep, wherever it is and however old it is. `setup.sh`
open-codes the same sweep for `~/.claude/settings.json` and changed with it, so a bare
`settings.json.tmp` survives there too.

**One directory has a policy of its own, and it moved the other way in this same release.** The
embedding cache's directory is swept by age: a file in it older than `QQ_CACHE_GC_DAYS` (60 days
by default) is removed, barring a `.lock`, a temp of the current write, and the cache files this
install is using. A bare `<target>.tmp` there used to be *skipped outright* by that sweep, which
exempted it from the age policy permanently — it is no longer skipped, so a
`~/.cache/qq-search/config.tmp` you left there goes once it is 60 days old, like anything else in
that directory. Set `QQ_CACHE_GC_DAYS=0` to disable the sweep entirely. Nowhere else in qq
deletes that spelling at any age. `CONFIG.md`'s `QQ_CACHE_GC_DAYS` entry states the rule in full.

### Why

Because the rule could only ever delete a file this package did not write. Today's writer never
produces a bare `<target>.tmp`, so every file that rule could match came from somewhere else —
the old idiom, another tool, or you. And nothing in the name tells those apart: an operator who
keeps a backup with `cp config config.tmp` beside `~/.config/quintessence/config` — which
`qq config set` writes atomically — lost it on the next such write an hour or more later. That
was reproduced end to end before the rule was removed.

It was a real fix for a real problem: a pre-atomicio install whose write was interrupted does
hold litter by that name, in a state directory nothing else sweeps. But a permanent
file-deleting behaviour is the wrong price for a one-time migration. The migration is now a
script you run once, yourself, that shows you the list first.

### What a pre-atomicio install might be holding

The 2026-08-02 publication predates `atomicio`. On that code every durable write was hand-rolled
as *write `<path>.tmp`, rename it over `<path>`* — and on any exception, the rename never
happened and the temp stayed. So if a `qq` command was ever interrupted on that install (a
Ctrl-C, an OOM kill, a full disk, a laptop lid), you may have a leftover file beside your
config, the pending-findings queue, the refs log or the embedding cache, named exactly
`<that file's name>.tmp`. Not beside a note: no durable write into the store ever used that
idiom. It is bounded: at most one stale file per target that was ever interrupted mid-write, and
it is inert — nothing reads it.

If you cloned after 2026-08-04, or if no `qq` write was ever interrupted, you have none of this
and there is nothing to do.

### The one command

```
python3 tools/reclaim_legacy_temps.py                   # list what is there, with ages. Deletes nothing.
python3 tools/reclaim_legacy_temps.py --delete          # remove the aged ones that have a sibling
python3 tools/reclaim_legacy_temps.py --delete-orphans  # remove the aged ones with no sibling
```

It searches the directories the old idiom actually wrote into, and only those: the config
file's own directory, the state directory and its `refs/` subdirectory, the embedding cache's
directory, `~/.claude`, and the directory holding any path setting you have pointed elsewhere.
Each is one directory, read once — no subtree is walked — and paths are used as you configured
them rather than resolved, because that is where the old idiom put its temp: `open(path +
".tmp", "w")` on a config you version by symlinking into a dotfiles repository created the file
beside the LINK, so the config directory is where the litter is and your repository is not
searched at all.

**It is not looking for `.tmp` files. It is looking for qq's own leftovers.** In each of those
directories it claims only `<target>.tmp` where `<target>` is a file qq writes there — the names
come from your own configuration, the same way the old code derived them. Your `memoir.tmp`
beside your `memoir` is not one of them, and your note store is not searched at all: no write in
the pre-atomicio code ever used the idiom under it. It never lists `<name>.tmp.md`,
`<name>.tmp.bak`, or a live `<name>.tmp.<12 hex>` either.

`--dir PATH` searches a directory you name for ANY `<name>.tmp` in it — outside qq's own
settings there are no target names to go on. That is the one wide search, you ask for it
explicitly, and the run prints which directories carry it.

Findings come in two groups. Most litter has a `<name>` beside it, because the old idiom wrote
its temp next to the file it was replacing; `--delete` handles those. But the idiom did not
require the target to exist — it made the directory, wrote the temp, then renamed — so a write
interrupted the FIRST time it ever ran leaves a `<name>.tmp` with nothing beside it. Those are
listed separately, as *possible orphans*, and removed only under `--delete-orphans`. The
separation is a caution, not a verdict: with no sibling there is nothing corroborating that the
file is ours rather than a scratch copy of your own, so you get to look before anything goes.

**Read the list before passing either flag**: the old idiom's litter and a backup you took by
hand are spelled identically, which is exactly why this is a command you run rather than a rule
the package applies to you.

`ASSURANCE.md` states the surviving deletion rule in full, with the names it takes and the names
it leaves.
