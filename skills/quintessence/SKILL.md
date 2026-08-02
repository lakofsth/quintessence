---
name: quintessence
description: Capture or resume durable session continuity ("quintessence" HEADs) – the rich working context that outlives a conversation. Use when wrapping up substantive work, when the user says /quintessence or asks to save/checkpoint/distill a session, or to resume/continue a prior thread. ALSO trigger the instant you are about to offer to "save/remember/note" anything for a future session: if it is a reasoning thread or decision-state it is quintessence (a HEAD), not a one-off note – route it here and create the HEAD early, not at exit. Distinct from atomic memory facts and end-result docs.
---

# Quintessence – capture and resume

A continuity store that outlives the conversation: one **HEAD** file per topic, plus an
append-only journal. A HEAD is a rich re-entry note. Aim for ~2k tokens or under; that is
guidance for you as author, and what the store enforces is a byte-count nudge and a flag.
The harness's own context compaction is salvage: it runs after the fact and loses things. A HEAD
is written while your context is still full and you still *know* what the essence is. Drive the
store through `qq <verb>`, one tool. Never edit the store files or run git in the store directly:
raw commits are hook-blocked, and a raw edit is not a write, so it gets silently folded,
unattributed, into the next `qq` commit.

**New here?** Read `ONBOARDING.md` then `RUBRIC.md` (the HEAD schema) before writing your
first one.

## Resume a thread
- `qq menu` / `qq list` – browse topics; `qq show <topic>` – load a HEAD to re-enter it;
  `qq brief <topic>` – orient on a big HEAD cheaply (essence + newest update-lines + RE-ENTER).
- **Multi-topic load.** `qq show <topicA> <topicB> …` composes several HEADs at once, so you can
  recombine threads discussed separately and find the connections instead of resuming just one.
- `qq search "<query>"` – recall by meaning across HEADs, docs, and facts (degrades to keyword
  search if no embedder).
- **Resume is mostly deliberate:** the resume hook may surface a matching snippet unasked, but
  full re-entry isn't automatic. When the user's topic matches a menu entry's slug or essence,
  `qq brief <topic>` before proceeding rather than starting cold.

## Capture as you work (saving is unconditional – it builds the journal)
- Add a note (the common as-you-go write): `qq update <topic> "<text>"` – merge-safe, inserts under
  the lock, can't clobber. Use it freely as you work, not only at exit.
- Start a thread: `qq new <topic> "<essence>"`. Reset the essence, which is the line the menu
  shows, with `qq essence <topic> "<text>"` the moment an update outdates it.
- Rewrite a whole HEAD (rare; refuses if another session changed the HEAD): `qq rewrite <topic> < full_content`.
- Snapshot to the journal: `qq finalize <topic>` (aliases: `checkpoint`, `save`) – a STATE checkpoint, not
  "thread closed". Fold an overgrown HEAD (past ~32kB / ~12 update-lines): `qq compact <topic>`.
- If a `session_state_<topic>.md` exists for this thread, fold it in and treat the HEAD as its
  successor – don't maintain both.

## When to capture (threshold)
A decision reached by weighing alternatives (a why-X-over-Y worth not arguing out again), an open
loop or unfinished state that outlives the session, or ~3+ substantive exchanges on one topic that
isn't already a fact or doc. Skip below that (single lookups, quick resolved edits). When unsure,
capture – a small HEAD is cheap, a lost thread is not.

## Boundaries / routing
- The common mistake when starting cold: a reasoning thread / live decision-state → a quintessence
  HEAD (`qq`); an **atomic durable fact** → the memory store. Never write a thread into memory, and
  never let a vague "save a note for later" default to memory when the thing is really a thread.
  Create the HEAD as the topic emerges, not at exit.
- Quintessence = working context + calibration (the expensive, non-portable half). The finished
  write-up a person reads belongs in your docs tree. Keep the HEAD bounded: richness comes from
  precision and from pointers to where the depth lives, never from length.

## Multi-store (per-project continuity)
The store may be a **search path**: a global user store plus, when you're inside a project that has
one, a project store (`<project>/.quintessence/`, discovered by walking up from the cwd). It's
transparent – the verbs above just work:
- Reads compose most-specific-first (`qq show`/`brief`/`path` resolve project→user; `qq list`
  unions; `--global` restricts to the user store). Writes (`new`/`update`/`finalize`) go to the
  project store when you're in one.
- Atomic **facts** follow the same layering: write a project's facts under the dir `qq memdir`
  prints (`<project>/.quintessence/memory`); `qq memdir --global` is the user store's. Create a
  project store with `qq init --project`.

## Health
`qq doctor` checks dependencies, the store, and the embedder. `qq check` surfaces consistency
findings (deterministic + a memory↔HEAD staleness cross-check).

## Local enrichment (optional)
A deployment may add its own conventions: `qq config get QQ_QUINTESSENCE_EXTRA` – if it returns a
path to an existing file, read it and fold its guidance into the above (store layout, house docs
tree, project-specific routing).
