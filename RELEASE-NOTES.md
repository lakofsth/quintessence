<!-- SPDX-License-Identifier: AGPL-3.0-or-later
     Copyright (C) 2026 Thomas Lakofski -->

# Release notes

Changes that ask something of you, newest first. Everything else is in the commit log; this file
is for the things you would want to know before or just after pulling.

## Unreleased — config paths resolve the same in the shell scripts and in `qq`

### What changed

- **`~` in a config path now means your home in the shell scripts too.** Every hook and script that
  reads a path key (`QUINTESSENCE_DIR`, `QQ_STATE_DIR`, `QQ_CONFIG`, `XDG_CONFIG_HOME` and the
  rest; CONFIG.md marks them) expands a leading `~` or `~/` against `$HOME`, as `qq` itself always
  did, and in each member of a comma-separated path list. Before, a value that reached a hook
  unexpanded — from `qq config set`, a settings.json `"env"` block or a systemd `Environment=`
  line, none of which expand `~` — named `./~/…` under the hook's working directory, so hooks read
  and wrote somewhere other than the store `qq` used. `~user` is still left as written.
- **`setup.sh` reads your config file before choosing the store.** A store or memory directory set
  with `qq config set QUINTESSENCE_DIR …` (or `QQ_MEMDIR`) is now what a re-run of the installer
  initialises and wires. Before, a re-run looked only at the environment, created an empty store
  at `~/quintessence`, and pointed the engines at it.
- **An empty `QQ_REDACT_FILE` turns redaction off in the shell scripts too.** CONFIG.md has always
  said "Empty or absent means the filtering is off", and `qq` honoured it; the shell side read an
  empty value as unset and kept withholding the topics in the default list.
- **Every Python entry point now exits 141, quietly, when the reader of its output closes early**
  (`qq menu | head -1`, say), on Python 3.14 as on 3.13, including `refs-resolve.py`, which used
  to exit 0 always; its own caller, `memory-commit.sh`, discards its output.

### What it asks of you

- If you set a path with a literal `~` and things worked anyway, something was compensating —
  most often a store at `./~/…` under a hook's working directory. Look for a directory literally
  named `~` there; anything written into it since belongs in your real store.
- If you moved your store with `qq config set` and later re-ran `setup.sh`, check for an empty
  `~/quintessence` the installer may have created, and re-run `bash setup.sh` so it wires the
  store your config names.
- If you set `QQ_REDACT_FILE=` (empty) expecting the default list to keep applying, it no longer
  does: point it at the list explicitly.

## Unreleased — idle-checkpoint autosave hook

### What changed

A new async Stop hook, `idle-autosave.sh`, wakes a session that has gone quiet so it can capture its
synthesis into a HEAD while its context is still in view and the prompt cache is still warm — before
Claude Code's idle compaction (lossy) and the cache edge. It fires only after
`QQ_IDLE_AUTOSAVE_DELAY` seconds (default 3000) of continuous transcript quiet, only when the
session had genuine new activity since its last checkpoint, and never on its own wake. See
[INSTALL.md](INSTALL.md#idle-checkpoint-autosave).

### What it asks of you

- **Plugin installs get it automatically** (a seventh bundled hook). It is silent unless it fires;
  when it does, the session sees a short "auto-woken" instruction and is told to checkpoint lightly
  or do nothing. Set `QQ_IDLE_AUTOSAVE_DISABLE=1` to turn it off.
- **Option B installs:** re-run `bash setup.sh --wire-claude`; it now manages five hooks and adds
  this one with `async`, `asyncRewake`, `timeout` 3600 and `rewakeMessage`. The timeout must stay
  above the delay. If you wired a hand-written copy, the installer repoints it and adds any of
  those fields it lacks, keeping the rest of your entry.
- Four new config keys (`QQ_IDLE_AUTOSAVE_DELAY/POLL/STATE/DISABLE`, see CONFIG.md). The state
  directory defaults to `$QQ_STATE_DIR/idle-autosave`.
- Needs `jq` (and `python3` for the quoted-mention check). Without `jq` the hook exits silently.
- Do not raise the delay toward 3600: idle compaction fires at 54–55 minutes of quiet and its
  transcript write resets the hook's clock.

## Unreleased — `qq ask` endpoints, stricter text channels, installer paths

### What changed

- **`qq ask` endpoints.** `QQ_ASK_ENDPOINTS` are tried in the order written, and an endpoint that
  passes its probe but then fails to complete (a timeout, a context overflow, an empty answer)
  now steps down to the next one instead of ending the ask. Two new per-endpoint keys, both
  `url=value` lists: `QQ_ASK_MODELS` names the model for a server that selects by name (ollama),
  and `QQ_ASK_SLOTS` pins a slot on a resident multi-slot llama-server shared with other clients.
  An endpoint with a named model passes its probe only if it actually lists that model.
- **Text is landed or refused, never dropped.** `qq new` with no text argument and data waiting
  on stdin exits 2 instead of creating the HEAD with a placeholder essence; `qq new` and
  `qq essence` read their text from arguments only. `qq rewrite` given text as an argument exits 2;
  its text comes on stdin. `qq update` with both takes the argument and does not read stdin.
- **Installer path overrides.** `setup.sh` expands a leading `~` or `~/` in `BIN_DIR`,
  `QUINTESSENCE_DIR`, `QQ_MEMDIR`, `SKILLS_DIR` and `CLAUDE_SETTINGS`; a quoted `~` used to build
  a literal `./~` directory under the working directory. `~user` is refused, naming the variable.

### What it asks of you

- An `ask` rung naming a model its server has not pulled now fails its probe and is skipped,
  where it used to pass the probe and fail every completion. `qq doctor` names the endpoint
  `ask` will use; each answer ends with the one that answered.
- Scripts that pipe text into `qq new`, or pass `qq rewrite` its text as an argument, now get
  exit 2: pass `qq new` its text as an argument and pipe `qq rewrite` its text.

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
