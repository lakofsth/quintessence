# quintessence

> *Everything not saved will be lost.*

A session-continuity store for AI coding agents: plain-Markdown working memory for the
threads your agent is actually thinking through.

## Why this exists

A session dies mid-thread: a half-made decision, an open question. The next session
starts cold, spends its first minutes re-deriving, and sometimes settles on a different
decision than the one already made.

The two standard places to persist context don't hold this. `AGENTS.md` – `CLAUDE.md` in
Claude Code – is standing instructions: how to behave, always. Memory files are atomic
facts: small, stable, one per file. What gets lost is the reasoning thread: what was
decided and why, what was ruled out, what is still open. Quintessence is where that goes.

One Markdown file per topic (a **HEAD**): the topic's essence, dated update-lines (newest
first), and an explicit re-entry point. Resuming is one command, and it hands back the
decisions, the dead ends and the open loops rather than a summary of them:

```sh
qq brief auth-refactor        # resume: essence + newest updates + RE-ENTER point
# ... work happens ...
qq update auth-refactor "chose keyed framing over TLVs — parser stays 40 lines; next: MTU probe"
qq finalize auth-refactor     # snapshot the thread to the append-only journal
```

On top of that:

- `qq search` and `qq ask` find prior decisions by meaning, and a hook surfaces a matching
  thread when a new prompt looks like old work.
- Consistency checks (`qq check` plus a staleness cross-reference) raise notes that have
  drifted out of sync with each other. A recalled note is a point-in-time snapshot, not
  ground truth, and the checks say so.
- Write-time **reality binding**. A note that names a file, commit, port, or systemd unit
  gets that referent fingerprinted on the spot: a claim about something that doesn't exist
  warns immediately ("born stale"), and the recorded refs give the staleness checks
  something concrete to compare against later. Binding never blocks or alters a write;
  `QQ_BIND=0` switches it off.
- A locked, merge-safe write path, so several agent sessions can share one store without
  clobbering each other.

Constraints: everything is plain Markdown in a git repo you own, greppable and diffable,
readable by anything that reads text. The core engine is stdlib-only Python plus bash;
there is nothing to pip-install (the optional MCP servers, `qq-search-mcp`/`qq-remote-mcp`,
run via `uv` and pull in the `mcp` package, plus `uvicorn` for the remote one). Everything
degrades rather than stopping: no embedder means keyword search, not silence. Nothing
phones home; the store lives wherever you point it. (The name is the old word for the
fifth essence, the substance that persists when everything else changes.)

> Status: alpha, Linux-first. Runs my own sessions daily; [ASSURANCE.md](ASSURANCE.md)
> records what is checked and what deliberately is not; CI installs it from scratch into a
> fresh `$HOME` on every push. macOS is untested: the shell hook glue assumes util-linux
> `flock` and GNU `readlink -f`. Your notes stay in the directory you point the store at.

## Dependencies

All of them, in one place:

| | binary (package) | without it |
|---|---|---|
| **the store** (required) | `bash` (bash) · `git` (git) · `jq` (jq) · `python3` (python3; stdlib only, nothing to pip-install) · `flock` (util-linux – the reason for "Linux-first") | doesn't run |
| semantic recall – `qq search` + the recall hooks | [Ollama](https://ollama.com) with an embedding model pulled (default `qwen3-embedding:0.6b`); vectors only, nothing generative | keyword search, and it says so |
| cited answers – `qq ask` | additionally, a local chat-capable completion endpoint (`QQ_ASK_ENDPOINTS`, see CONFIG.md) | raw retrieval hits, with a hint |
| binding (optional) + test suite | `ss` (iproute2) | port-referent fingerprinting degrades quietly (never blocks a write); one test suite fails noisily |

`qq doctor` reports the state of all of it.

## Getting started

From a clone:

```sh
bash setup.sh        # symlink the CLIs into ~/.local/bin, create a store, write the config,
                     # run the doctor + test suite — idempotent, nothing destructive
qq menu              # empty menu on a fresh store
```

Or by hand. `qq` has to be on your PATH first – `setup.sh` symlinks it into
`~/.local/bin`, or call it by its full path out of the clone:

```sh
export QUINTESSENCE_DIR=~/my-notes      # where HEADs live
export QQ_MEMDIR=~/my-facts             # optional: your agent's atomic-fact memory dir
export QQ_STATE_DIR=~/my-notes-state    # runtime state (activity log, pending findings, refs) —
                                        #   independent of QUINTESSENCE_DIR; pin it too for a
                                        #   fully isolated/sandboxed run (default: XDG state dir)
qq init                                 # create the store (git repo + write-path hooks)
                                        #   and record both paths in the config file
qq doctor                               # check deps / store / embedder
```

A fresh model has no prior familiarity with qq, so a small always-injected contract
teaches each session how to drive the store: how to resume and recall, which verb to write
with, what belongs in a thread versus a fact. For Claude Code that is either the bundled
plugin or four hooks wired by `setup.sh --wire-claude` – see [INSTALL.md](INSTALL.md).
[ONBOARDING.md](ONBOARDING.md) is how to drive it day to day.

## Everyday use

| you want to… | command |
|---|---|
| see what threads exist | `qq menu` · `qq digest` (recent, ranked) |
| resume a thread | `qq brief <topic>` – or `qq show <topic>` for the whole HEAD |
| add a note (the common write) | `qq update <topic> "<text>"` |
| start a new thread | `qq new <topic> "<essence>"` |
| find a prior decision by meaning | `qq search "<query>"` |
| ask and get a cited answer | `qq ask "<question>"` |
| snapshot to the journal | `qq finalize <topic>` |

`qq update` is 90% of writes. It inserts your line under the lock, never rewrites the
body, and auto-stamps the date, so concurrent sessions can't clobber each other.

A HEAD is a living file – updated, eventually compacted or retired – so history lives in
the **journal**: `qq finalize` snapshots the current state to it, append-only, inside the
store. `qq compact` folds an overgrown HEAD and `qq delete` retires one; both snapshot to
the journal first.

The one rule of the store: use a verb, never edit the files or run git in the store
directly (the write-path hooks refuse a raw commit or push). `qq help` lists everything;
ONBOARDING.md is the full tour, and [RUBRIC.md](RUBRIC.md) specifies what a good HEAD
looks like.

## Configuration

Settings resolve **environment variable > config file > built-in default** (the engine can
pass a programmatic override above all three; CONFIG.md states the full order). The
durable place is the config file (`~/.config/quintessence/config`); `qq init` and
`setup.sh` write it, and you manage it with `qq config set/show/get/path`. Env vars
override for one-off runs.

The keys you're most likely to touch:

| Var | Meaning | Default |
|-----|---------|---------|
| `QUINTESSENCE_DIR` | HEAD store directory | `~/quintessence` |
| `QQ_MEMDIR` | your agent's atomic-fact memory directory | `~/.quintessence-memory` |
| `QQ_KB_ROOT` | recall corpus root (a symlink farm: drop a symlink in to add a source) | `~/kb` |
| `QQ_EMBED_MODEL` | Ollama embedding model | `qwen3-embedding:0.6b` |
| `QQ_OLLAMA_URL` | Ollama endpoint | `http://localhost:11434` |

`QQ_MEMDIR` isn't a second memory system. Point it at the atomic-fact store your agent
already keeps – in Claude Code's case, that *is* Claude's own memory directory – and
quintessence watches the seam between facts and threads: the staleness cross-reference
flags a memory fact that a HEAD has likely outgrown. Add a symlink to it under
`QQ_KB_ROOT` and the facts join semantic recall too.

[CONFIG.md](CONFIG.md) is the full reference: every key, type, default and scope,
generated straight from the code so it can't drift (a test asserts it's current).

## Per-project stores

By default quintessence uses one global store. It also supports per-project stores: two
layers, your user store plus a `.quintessence/` inside a project, discovered the way git
finds `.git` – by walking up from the current directory to the nearest one (the walk stops
below `$HOME`; the home level *is* the user store's layer):

```sh
cd my-project
qq init --project        # creates ./.quintessence
```

From inside that project tree, reads compose most-specific-first (`qq show`/`brief`
resolve against the project store, falling through to the user store), writes go to the
project store, and `--global` (`-g`) on any verb targets the user store instead. A write
naming a HEAD that exists only in the user store refuses with a hint rather than silently
forking it. Semantic recall composes the same way: with a project store present, `qq
search`/`qq ask` merge both indexes by score and tag each hit `(qq/project)` vs `(qq/user)`.

Limits: the staleness-xref and the auto-recall hooks still run over the user corpus only;
a project's `memory/` isn't part of recall yet (its HEADs are); and the MCP search server
resolves its store path at startup, so a project store created mid-session needs a server
restart. An explicit `QUINTESSENCE_DIR` in the environment pins to that one store and
skips discovery – a deliberate one-off override.

## Under the hood

The engine is a thin Python dispatcher (`qq`) over a stdlib-only package (`quintessence/`).
The store format and the lock discipline predate the current engine, so a store created by
an older version works unmodified.

The docs split by reader: you read to judge the tool – what it does, what is checked, what
could go wrong; your agent reads to use it.

For you:

| Doc | What it's for |
|-----|---------------|
| [INSTALL.md](INSTALL.md) | install + Claude Code wiring (plugin or manual) |
| [DESIGN-NOTES.md](DESIGN-NOTES.md) | why it's built this way |
| [ASSURANCE.md](ASSURANCE.md) | what is verified before a release, and what is deliberately not |
| [SECURITY.md](SECURITY.md) | scope, and how to report a vulnerability privately |
| [CONFIG.md](CONFIG.md) | full configuration reference (generated) |
| [REGISTER.md](REGISTER.md) | the project's naming register – the practice comes from [lakofsth/register](https://github.com/lakofsth/register) |

For your agent – what a session is given, or told to read:

| Doc | What it's for |
|-----|---------------|
| [CONTRACT.md](CONTRACT.md) | the small always-injected operational contract |
| [ONBOARDING.md](ONBOARDING.md) | day-to-day use, verb by verb |
| [RUBRIC.md](RUBRIC.md) | the HEAD format spec |
| [audit-runbook.md](audit-runbook.md) | the optional LLM consistency audit (bring-your-own reality-snapshot probe) |

## tsk – jobs that outlive the session

The store solves continuity for *knowledge*; `tsk` solves it for *running work*. It is a
tiny task broker over `systemd-run --user` transient units: a job submitted with `tsk run
<name> -- <command>` is owned by the user systemd manager (enable lingering), so it
survives agent-harness shell reaps, session ends, and SSH drops. `tsk
status/wait/log/rc/stop/clean` cover the lifecycle; logs are plain files plus journald
(`tsk-<name>`). Installed onto PATH by `setup.sh` alongside `qq`.

## Testing and contributing

`bash tests/run.sh` runs the full suite; it needs only the required deps plus `ss`, which
one suite uses to probe ports – no third-party packages, no network. CI runs it on every
push, plus a cold install into a fresh `$HOME`.

Known gaps, PRs welcome: macOS portability (`flock` and GNU `readlink -f` are assumed).

## License

**AGPL-3.0-or-later** – see `LICENSE`. Copyright (C) 2026 Thomas Lakofski.

Strong network copyleft: if you modify quintessence and let others use it over a network,
you must offer them your modified source. Source files carry an
`SPDX-License-Identifier: AGPL-3.0-or-later` header.
