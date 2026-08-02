# Quintessence – onboarding for an agent session

Quintessence is a session-continuity store: per-topic **HEAD** files (rich re-entry notes) under
your store directory (`$QUINTESSENCE_DIR`, default `~/quintessence`), plus an append-only
**journal** of snapshots. You *read* HEADs to resume a thread and *update* them as you work. The
tree is shared by multiple agent sessions at once, so you don't edit the files directly – you
drive everything through one command, `qq <verb>`. Pick the verb for what you want; the common
write is the safe one.

## The verbs

| you want to… | command |
|---|---|
| see what threads exist | `qq menu` (full) · `qq digest` (recent, ranked) · `qq list` |
| read / resume a thread | `qq brief <topic>` – essence + newest update-lines + RE-ENTER, the HEAD's own "start here" pointer section (see `RUBRIC.md`). This is the default. Reach for `qq show <topic>` (full HEAD, several at once: `qq show A B`) only when you want a whole thread verbatim, or right before writing to it. On a limited reader session (model not matching `QQ_SAFE_MODEL_PREFIX`), a topic on the `QQ_REDACT_FILE` list is refused with zero content; override with `--unredacted` or `QQ_SHOW_UNREDACTED=1`. A full-access reader is unaffected. |
| find by meaning (not browse) | `qq search "<query>"` |
| ask a question, get a cited answer | `qq ask "<question>"` – retrieval-as-QA: cited answer from a local model over HEADs/docs/memory; answers are point-in-time snapshots – verify load-bearing facts at source |
| add a note to a HEAD (the common write) | `qq update <topic> "<text>"` |
| refresh the essence line | `qq essence <topic> "<text>"` |
| start a new thread | `qq new <topic> "<essence>"` |
| snapshot to the journal | `qq finalize <topic>` (aliases `qq checkpoint`, `qq save`) |
| fold an over-long HEAD | `qq compact <topic>` |
| replace a whole HEAD (rare) | `qq rewrite <topic> < full_content` |
| retire a HEAD (recoverable) | `qq delete <topic>` (alias `rm`) – journals the final state first, same as `compact` |

`qq update` is what you'll use 90% of the time. It's **merge-safe** (inserts your line under the
lock, never rewrites the body, auto-stamps the date) – you cannot clobber anything with it, and
concurrent sessions can't clobber each other. Reach for it freely, as you work, not just at the
end. `qq help` lists everything.

## The two things to remember

1. **Verbs, not direct edits.** Don't edit the HEAD store's files directly. This scopes to `$QUINTESSENCE_DIR`
   (the HEAD tree) only. The atomic-fact **`memory/`** store is a separate tree with a deliberately
   different discipline: you edit those files directly with `Edit`/`Write` (see "Don't confuse
   quintessence with…" below) – that is not an exception to this rule, it's a different store. For
   HEADs: a raw `git` commit in the store is refused (by the hooks `qq init` installs); a direct
   `Edit`/`Write` is not refused at write time, but it is not a write either – it sits
   unattributed and gets silently folded into the next `qq` commit. Drive every HEAD
   change through a verb so it lands with authorship and history.
2. **Update vs rewrite.** `qq update` for a note; `qq rewrite` only to replace a whole HEAD. `rewrite` takes the
   *entire* file on stdin (seed from `qq show <topic>`), and if another session changed the HEAD
   since this command started, it refuses (exit 3) instead of clobbering –
   re-read and retry. (The check's window opens at the rewrite call, not at your earlier read.)
   You almost never need it; `update`/`essence`/`compact` cover normal work.

## If a write warns "claim may be born stale"

Writes bind the artifacts they mention (files, commits, ports, units) to the claim, checked on
the spot. That warning means the referent does not exist as written – the write still landed,
but the claim was wrong at birth. Fix the text (typo'd path, wrong host, stale name) or accept it
knowingly (a to-be-created path is a legitimate reason). Don't just ignore it: born-stale claims
are exactly the notes that mislead a future session. For a referent the extractor won't guess,
bind it explicitly: `qq update <topic> --ref file:/path "text"`. `QQ_BIND=0` disables binding
entirely if it's ever in the way.

## Why one locked tool (so you don't try to "fix" it)

Several sessions share one tree / index / branch (and mirror set, if configured). With no
coordination that races: one session commits another's half-edit, and pushes clobber. So every
write to the store serializes under a kernel `flock`, held for the seconds it takes to
write→commit and then
released; two git hooks (`pre-commit`, `pre-push`) refuse any commit or push that didn't come
through it. Mirroring to a remote is not part of the shipped write path. It happens only if a
deployment adds a post-commit hook, and that hook runs after the commit returns, outside the
lock. `qq` handles all of this – you just call verbs.

## Don't confuse quintessence with…

- **`memory/`** (an atomic-fact store) – atomic *facts*, surfaced by associative recall. A reasoning
  thread / live decision-state is **quintessence** (a HEAD), not a memory note. It lives at
  `$QQ_MEMDIR` for the user store; inside a **project store** (a per-project `.quintessence/`; see
  the README's *Per-project stores* section) a project's facts belong under `<project>/.quintessence/memory`
 – run `qq memdir` to print the right directory for where you are (add `--global` for the user store).
  If a `[T2 stale?]` finding appears at session start (`qq check` tags its findings by tier:
  `[T1 ...]` checks are deterministic, `[T2 ...]` ones are similarity-paired and need judgement),
  a HEAD may have outgrown a memory fact: read
  both, then edit the memory fact if it is superseded, or `qq waveoff <memory> <head>` if compatible
  (`qq waveoff --head <head>` bulk-clears one HEAD's pairs). The checker only pairs by similarity +
  dates; the stale-vs-compatible call is yours. `qq findings next` steps through the pending
  findings one at a time, each with the commands that resolve it.
- **Finished docs** – human-facing, polished, published. Quintessence is your *working* context
  and calibration notes, not the finished artifact.

## MCP tools (optional)

The stdio search MCP server, `qq-search-mcp`, is not auto-wired by either install path – register
it yourself with `claude mcp add qq-search -- uv run --script <path>/qq-search-mcp`, where
`<path>` is your clone of this repository. Once
registered it exposes six tools mirroring the CLI verbs above: `search_continuity` (≈ `qq
search`), `ask_continuity` (≈ `qq ask`), `resume_brief` (≈ `qq brief`), `show_head` (≈ `qq show`),
`update_head` (≈ `qq update` – the common write), and a generic `qq` passthrough for anything
else. A separate `qq-remote-mcp` server serves a remote caller and is deliberately narrower
(search/ask/brief only, no writes, no `show_head`) – see `CONFIG.md`'s remote-access rows if you're wiring
a remote interface.

## Pointers

- Schema + design rationale for HEAD *content*: `RUBRIC.md` (in this repo; read before your first HEAD).
- The concurrency mechanism: `qq-lib.sh` (the lock + scoped-commit primitive).
- Skill (triggers + how-to): `skills/quintessence/SKILL.md` (in this repo).
- Browse what's in your store: `qq menu` (full list) or `qq show <your-topic>` (a specific HEAD).

> One CLI: everything is a `qq` verb. (`qq-search` and `qq-ask` sit in the engine directory
> purely as internal exec targets for `qq search`/`qq ask` and the hooks – they are not on
> PATH and are never called directly.)
