# Design notes

Background, not required reading. Why quintessence is shaped the way it is – so the operational
docs (`ONBOARDING.md`, `RUBRIC.md`) can stay terse and you can judge whether the design fits your
situation before adopting it.

## The problem

An agent with a bounded context window loses its working state at every reset, compaction, or new
session. The expensive loss isn't *facts* (those can be re-fetched) – it's the **working context**:
what was mid-thought, which alternatives were already ruled out, the half-built plan, the calibration
for *how* to work a given thread. Re-deriving that each session is slow, lossy, and quietly
reopens settled decisions.

Quintessence fills that gap: a place to keep the working context outside the session, so a later
session can re-enter a thread instead of cold-starting it.

## The four primitives (why the pieces are what they are)

Come at the continuity problem from any direction and you arrive at the same four pieces. Every
part of qq is one of them:

1. **A durable store** – per-topic **HEAD** files: rich, structured re-entry notes (not a flat log),
   plus an append-only **journal** of snapshots. Structured because the point is fast *re-orientation*:
   an `essence` line and a `RE-ENTER HERE` block let a cold reader get back to work in seconds.
2. **On-demand recall** – semantic search (`qq-search`) over the store. Resume is not associative:
   a cold session won't *remember* to read the right HEAD, so recall has to be askable by meaning
   rather than depending on the model recalling a slug. Falls back to keyword search if no
   embedder is present.
3. **Consolidation** – folding accreted update-lines back into the body, splitting/merging topics.
   Notes that only ever grow become unreadable; the discipline is *reconcile, don't just accrete*.
4. **Integrity checking** – drift between what a note claims and current reality (or between two
   notes) is the failure that erodes trust. A deterministic checker flags suspect pairs, and it
   flags without ever fixing: a person decides, because "reality wins" is not always right (a
   note may record a deliberate state, or reality may be the thing that's wrong).

## Key decisions

- **The locked write path.** This is the load-bearing idea: multiple sessions can share one store
  concurrently, which races: one session commits another's mid-edit, updates are lost, pushes
  collide. There are two kernel `flock`s, not one: the store's own `.qq.lock` serializes HEAD writes,
  and a separate lock on `QQ_STATE_DIR` serializes the runtime-state mutations (findings/xref/
  reconcile) – so state-dir traffic never has to contend with, or accidentally block on, a HEAD
  write. Every mutation still funnels through its owning lock, and commits are **path-scoped**
  (only the named files, never `git add -A`), so a concurrent session's scratch is never swept in.
  An in-lock marker that git hooks enforce means nothing reaches shared state except under the lock – 
  a stray direct write just stays local scratch until a real write reconciles it. This is why you
  drive the store through `qq update` / `qq rewrite` / `qq finalize` and never edit it or run git in
  it directly. `qq`'s python engine (`quintessence/write.py`) implements the lock, the marker and
  the path-scoping on the store's `.qq.lock` – the same lock file the bash helpers in `qq-lib.sh`
  take, so a shell-side writer and the engine cannot interleave on one store.

- **Dumb plumbing, smart model.** The scripts do the mechanical, race-free parts (lock, commit,
  index, snapshot). *What* to write – the judgment about what a future session needs – is left to the
  model, guided by `RUBRIC.md`. Keeping intelligence out of the plumbing keeps the plumbing correct.

- **Bind claims to reality at write time.** A continuity store's quiet failure mode is that its
  claims decay silently: a note names a file, a commit, a port, a unit – and the artifact moves on
  while the note stands still. Consistency checks between notes can't catch that; the drift is
  between the note and the *world*. So the write path extracts referents from what's being written
  and fingerprints them right then: a referent that doesn't exist warns immediately ("born stale" – 
  wrong at the moment of writing, the cheapest possible time to learn it), and the persisted refs
  let later checks diff a claim against what its subject has since become, when something triggers
  the check rather than in a periodic sweep. Three constraints keep it honest: **fail-soft**
  (binding can never block, alter,
  or fail a write – an exception degrades to exactly the unbound behavior), **conservative
  extraction** (a missed referent is fine, a false bind is noise; ambiguous shapes only bind when
  they actually resolve against a known repo, and anything else takes an explicit `--ref`), and
  **deployment-local records** (refs live in the state dir, never inside the mirrored store – the
  store format stays plain Markdown, and *your* binding state doesn't follow the store to another
  machine where it would be wrong).

- **Two stores, two retrieval modes.** Quintessence HEADs are primarily *chosen* (you pick a topic
  to resume via `qq menu`/`brief`/`show`); atomic **facts** in a separate memory store *recall
  themselves* by associative relevance. HEADs also surface associatively to some degree – the
  resume hook semantically matches a prompt against the corpus and can prime a relevant HEAD
  unasked – but deliberate pick remains the primary re-entry path, since the hook only fires on a
  clear match. A reasoning thread or live decision-state is a HEAD; a standalone durable fact is a
  memory note. The integrity checker watches the seam between them (a fact can outlive a HEAD that
  changed it). Its pairwise-similarity pass is quadratic in the number of notes – deliberately
  simple, and fine at the corpus sizes a personal store reaches; revisit if a store grows by
  orders of magnitude.

- **Hooks fail open, never block.** The harness integration (resume-surfacing, finalize safety-net)
  only ever *adds* context and is silent on any failure.

- **Externalize state to work around a working-set limit.** The same trick the tool gives the model, applied
  to the model itself: a bounded-context reasoner holds a large plan coherently by *writing it down*,
  not by holding it in its head. HEADs are that, for a thread.

## Epistemic stance

Recalled notes are a point-in-time snapshot, not ground truth: they reflect what was true when
written. Verify against current reality before relying on them, with extra skepticism for
self-authored recall (it can carry a past guess forward as a present premise). The tool surfaces prior
context; it does not certify it. That is why recall is labelled "prior context, may be stale", why
the integrity checker exists, and why writes bind their claims to the artifacts they name: if a
note's subject can drift, record enough at write time to notice when it does.

## What this is not

Not a knowledge base of finished documents, and not a chat-history archive. It is *working*
context and calibration for an agent that would otherwise start cold.
