# Quintessence – distillation rubric

The *essence* of a working session, written by the session that is ending while its context
is still full, so a later session can pick the working state back up without reloading the
transcript or paying for a compaction pass. Compaction is salvage: after the fact, lossy,
guessing what mattered. This is written ahead of that, while the session still knows what the
essence is.

## Two artifacts, one file
- **HEAD** (`<topic>.md`) – the re-entry point, updated through the session and finalized at
  exit. Keep it small: aim for ~2k tokens or less as authoring guidance, though the check that
  actually fires counts bytes (see below). Richness comes from precision and pointers to
  depth, not from length. This is the file loaded to continue, and each version supersedes the
  last.
- **JOURNAL** (`journal/<topic>/<ts>.md`) – snapshots of the HEAD at each session's end,
  appended and never overwritten. The history of the work as it accumulated. It may *inform*
  your finished docs but never replaces them: those are written independently by a person, and
  they remain the account of the end result at a point in time.

Saving is unconditional; it builds the journal. Resuming is the selective act: you pick a HEAD
from the menu (`qq menu`) instead of reopening the old conversation itself (your harness's
session-resume, whatever it is called there).

## HEAD structure (write in this order; omit a section only if truly empty)

```
# Quintessence — <topic>
> updated: <ISO8601> [<model>, session <id8>] <what changed and why it matters>
> essence: <ONE line — the single sentence that re-orients the next session. shown in the menu.>
> session: <session_id>  |  transcript: <path>

## RE-ENTER HERE
The highest-value block. 2–5 sentences: where we are *right now*, what was mid-thought,
what the next move was about to be. Write it so a cold model can pick up the pen.

## Live working set / open loops
In-flight items, pending decisions, anything unfinished or awaiting a result. Bullet each
with enough state to act (IDs, which way it was leaning, what it's blocked on).

## Calibration (how to work this thread)
How the user thinks about this; preferences & framings surfaced THIS session; what "good"
looks like here; tone. The stuff that makes collaboration efficient and never survives in
a decision log. (e.g. show a diff before substantive edits; small reviewable steps.)

## Settled — do NOT re-derive
Decisions reached this session, so the next session doesn't reopen them. One line each, with the
"why" compressed.

## Concrete state
Cheap facts: files touched (paths), commands, running jobs + IDs, ports, hosts, keys. The
easily-portable layer — keep it but don't let it crowd out the above.
Binding note: write another host's artifact with the host in front (`nas:/tank/x`) so it binds
and can be re-checked over ssh; a bare local-shaped path for an artifact on another host stays
unbound by design.

## Next actions
Prioritized. The first one should match RE-ENTER HERE.

## Depth pointers
Where the full detail lives, to fetch when needed instead of copying it in: transcript path +
rough location, doc files, memory slugs, code paths. Lets the HEAD stay small while keeping
the richness one hop away.
```

**Do not type the `[<model>, session <id8>]` marker — `qq` derives it.** `qq update` and `qq new`
read the writing session's model from its own transcript and insert the marker after the stamp;
off-harness (a human at a terminal, cron) nothing is inserted and the line is exactly as it always
was. It is a record rather than a claim, which a typed one cannot be: the field a model is least
reliable about is its own identifier. The commit carrying the write is stamped from the same
identity **where a `prepare-commit-msg` hook is installed to do it** — that hook is not part of
this package, so unless you have added one, expect no trailer and check the line alone.

## Discipline
- Update the HEAD as you go, not only at exit – an abrupt close should still leave a
  near-current entry.
- **Write path (safe for several sessions at once):** the tree is shared by concurrent Claude
  sessions, so HEADs are not edited in place. Drive everything through `qq <verb>`; each write
  serializes under the store's `flock` and commits path-scoped. Mirroring to a remote is not part
  of the shipped write path – it is a deployment-added post-commit hook that, if present, runs
  after the commit returns, outside the lock (see `qq-lib.sh`). A raw Edit/Write to a store file
  is refused only if you add the optional settings rule described in `INSTALL.md` (Option B);
  under the recommended plugin install it is not refused – adding that rule yourself is
  worth doing under either install path. Even then the `qq init` git hooks block
  a raw commit, and any stray edit is absorbed unattributed into the next `qq` write, so treat
  direct edits as unsupported either way.
  - **The common as-you-go update – `qq update <topic> "<text>"`** – adds a `> updated:` line
    merge-safely (reads the HEAD inside the lock, inserts after the H1, never rewrites the body,
    auto-stamps the date). Use it freely; it can't clobber and can't be clobbered.
  - **Essence:** `qq essence <topic> "<text>"` sets or refreshes the one-line essence, which is
    what the menu and the digest show. Refresh it the moment an update outdates it.
  - **Full rewrite (rare):** `qq rewrite <topic> < full_content` (seed from `qq show`); it
    auto-captures the base and refuses (exit 3) if the HEAD moved since – no manual base, no clobber.
  - **Size discipline:** a write nudges you once the update-line region passes 32kB or 12
    update-lines by default (both thresholds are settings – see CONFIG.md), and `qq check`
    flags the worst cases. `qq compact <topic>` folds old update-lines to the journal (newest five kept by default – adjustable per call, not a config setting;
    body untouched; git and the journal keep the originals). `qq brief <topic>` reads an
    un-compacted giant cheaply.
- At wrap-up: `qq finalize <topic>` snapshots the HEAD to the journal, reindexes and mirrors (also
  locked, also path-scoped). Finalize is a checkpoint of the state, never "thread closed".
- **Reconciliation, not accretion.** When updating, fold new info into the right sections and bring
  RE-ENTER / Next-actions into line with it – don't keep stacking `>` notes at the top while the body
  goes stale. A cold reader must never hit a header that contradicts the body. Do this routinely at finalize.
- A session may touch several topics → update each topic's HEAD; every finalize appends a
  journal entry. The unit of a topic is a project/thread, not a single session. Spin up a new
  HEAD when a distinct topic emerges mid-session – don't let one HEAD become a session catch-all.
- Retroactive refactor is supported and cheap. HEADs are just markdown – split/merge/rename
  freely (the model decides where the blurry boundaries fall; then `qq reindex`). Finer per-topic
  granularity makes multi-select (`qq show A B`) compose more precisely; residual cross-topic
  overlap is fine – load the related HEADs together rather than over-fragmenting.

## Relationship to the other continuity layers (a layer, not a replacement)
The names below come from the author's own harness; your setup will have its own equivalents.
- **breadcrumbs** (`~/.claude/session-snapshots/session-log.jsonl` + hooks) – cheap event
  spine; quintessence rides on top, doesn't replace it.
- **memory/** (`MEMORY.md` + atomic files) – durable atomic *facts*, surfaced by associative
  recall. Untouched. Quintessence is *chosen* (picker), memory *recalls itself* by relevance.
  HEADs can silently supersede facts (a stale fact outliving a decision the HEAD changed). The
  staleness-xref check (`staleness-xref.py`, run by `qq check`) pairs memory against HEAD via the
  qq-search embedding index and queues `[T2 stale?]` findings (tier 2: similarity-paired, needs
  judgement – unlike the deterministic tier-1 `[T1 ...]` checks) for the session model to settle:
  edit the memory, or `qq waveoff <memory> <head>` (quiet until the HEAD updates again).
- **session_state_\*** – if your harness keeps session-state files like these, the HEAD
  replaces them. It holds the same in-flight checkpoint, plus
  calibration and journaling. Don't maintain both for the same thread.
- **Finished docs** – written independently by a person, and the account of the end result; the
  journal may inform them but never replaces them.
- quintessence holds the *working context and calibration* – the expensive half that none of the
  others keep.
