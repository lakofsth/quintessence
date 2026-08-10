<!-- qq-contract v3 – keep this SHORT; it is injected into context every session. -->
# Quintessence – operational contract (always-on)

*Everything not saved will be lost.* A future instance of you starts blank, and this store is
the only channel to it. Record what a future you would need, and read it back to resume.

**The write path:**
- **Ask the store a question**: `qq ask "<question>"` (or the `ask_continuity` MCP tool) – a
  short cited answer from a local model in a disposable context. Prefer this over chaining
  `qq show`/file reads when you need specific facts or decisions; answers are point-in-time
  snapshots, verify load-bearing facts at source.
- **Resume** a topic: `qq brief <topic>` (essence + newest update-lines + RE-ENTER). Go to
  `qq show <topic>` (the full HEAD) only when you need a whole thread verbatim, or right
  before writing to it. Browse: `qq menu` / `qq list`; recall by meaning: `qq search "<query>"`.
- **Start a topic** (first write on a new thread; `update` needs one that exists): `qq new <topic> "<essence>"`
- **Add an update-line** (the common write): `qq update <topic> "<text>"` (or `echo "<text>" | qq update <topic>`) – merge-safe, can't clobber.
- **Rewrite a HEAD** (rare; stdin = the WHOLE file, not a diff): `qq rewrite <topic> < full_content`
- **Snapshot** to the journal: `qq finalize <topic>` (aliases: `qq checkpoint`, `qq save`).
- Never edit files in the store directly, and never run git there. A raw `git commit` or
  `push` in the store is blocked: the `qq init` git hooks reject any commit that lacks the
  write-lock marker. A raw file edit is not blocked at write time, but it is not a write either –
  the next `qq` write to that file commits it separately as an unattributed "absorb out-of-band
  edit" before its own change, so hand-edited content survives but is visibly NOT qq's. Drive
  every write through `qq`, which holds the lock and records authorship.

**What goes where:**
- A **reasoning thread / decision-state / open loop** → a quintessence **HEAD** (`qq update`).
- An **atomic durable fact** → the memory store (one fact per file), not a HEAD.

**What the store does not record:** it records what the writing session wrote. It cannot tell
text quoted from somewhere else apart from a decision the user actually made – both arrive as
the same bytes. The attribution convention in update-lines (which model wrote it, when, who is
being quoted) is a discipline you keep; the tool neither enforces nor verifies it.

**Naming referents (binding to reality):** name an artifact on another host with the host in
front – `nas:/tank/x`, `homelab-pi:/etc/pihole/y` – so the claim binds and can be re-checked
over ssh. That holds only while the host is listed in `QQ_BIND_HOSTS`, which is empty by
default, so a deployment must configure it; otherwise the prefix is an unbound label like any
other. A bare local-shaped path for an artifact on another host stays unbound either way.
Don't name a directory that is written continuously (session-transcript directories, live
mirrors) as a referent: it looks changed on every sweep, and dropping it again is a person
editing the record, not something that happens on its own.

**When to capture (threshold):** a decision weighed against alternatives, a loop that outlives
the session, or ~3+ substantive exchanges on one topic that isn't already a fact or doc. Below
that (lookups, quick edits), skip. When unsure, capture.

**Resume is mostly deliberate:** the resume hook may surface a matching HEAD/memory snippet
unasked, but full re-entry isn't automatic. If the user's topic matches a menu entry, proactively
`qq brief <topic>` (or `qq ask`) before proceeding.

Deeper docs on demand: `ONBOARDING.md` (day-to-day use, verb by verb), `RUBRIC.md` (HEAD format).
