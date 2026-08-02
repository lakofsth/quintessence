# Register – how we name and write quintessence

This describes the vocabulary and the voice the project uses. It is a positive guide: read it and
write in it, rather than writing however and translating afterwards.

## What this project is, in one sentence

quintessence is a notebook for long-running work: a store of plain Markdown files, kept in git,
where a session writes down the reasoning it would otherwise lose and a later session reads it
back to carry on.

Keeping notes and finding them again is the whole domain. Parts get named the way a person
keeping a working notebook would name them.

## The register, by area

**The store.** The store is a directory of Markdown files under git. Each file is a HEAD, and the
name you call it by is its topic – so you write to a topic and you are editing its HEAD. Both
words are load-bearing and neither replaces the other: the commands take a topic, the format
defines a HEAD. A HEAD has an essence (one line saying what the thread is), a body, and
update-lines in newest-first order. Finalizing puts a dated copy into the journal. Compacting
moves older update-lines into the journal and leaves the newest in place. A memory fact is
something else: one small stable fact in its own file, which this tool never writes.

Why "HEAD"? Nobody wrote the reason down, and by the time anyone asked, no record could say.
Two readings fit, and both are probably at work: the store is a git repository, and the HEAD is
the current tip of a thread while the journal holds its history; and update-lines run
newest-first, so the current state sits literally at the head of the file. The gap itself is
the best argument this tool has: the project that exists to record why things are the way they
are could not answer that question about its own central term.

**Writing.** All writes go through the one tool – many verbs, one write path, and nothing else
writes to the store. You add an update-line, which is the ordinary
write; set the essence; start a topic; or replace a whole file, which is rare. A write takes the
write-lock so two sessions cannot interleave. A write that fails rolls back, so a half-written
file never lands.

**Reading.** You show a topic in full or read a brief, which is the newest update-lines plus the
re-entry note. The menu lists every topic. The digest ranks open threads by recency. Search finds
topics by meaning rather than exact words, using an index built from embeddings. Ask puts a
question to a local model and returns a cited answer from the same material. Results are hits.
The passages behind an answer are its evidence.

**Staying true to reality.** A note can name something real: a file, a commit, a service, a port,
a host. Those are referents. Binding records enough about one to re-check it later. When a
referent has changed since the note was written, the note is stale and says so.

**What a reader receives.** Some material is worth keeping but not worth handing to every reader.
The store keeps a list of withheld topics. A session with a limited reader profile gets recall
with those left out; a full-access reader gets everything. Withholding covers material that
arrives unasked, meaning recall injected at session start and search results the session did not
ask for. Naming a topic outright is a deliberate act and is answered. Write-trust is separate: it
decides whether a session may author on protected topics alone, or whether its text is queued for
a trusted session to ratify. The remote interface keeps its own never-shared list. Nothing lifts
that one, and it fails closed: if it cannot be read, nothing is served and the interface refuses
to start.

**Keeping the store honest.** Checks run over the store and raise findings to resolve. Doctor
reports the install's health. Reconcile compares configured settings against what is in force.

**Fitting into a session.** Hooks run at session start, before compaction, and at session end.
The plugin packages the commands and skills for a coding agent. The remote interface serves a
read-only subset to a caller holding a credential.

## Words we reach for

store · HEAD · topic · thread · note · entry · update-line · essence · body · journal · finalize ·
compact · memory fact · write · add · set · start · replace · write-lock · roll back · show ·
brief · menu · digest · resume · re-enter · recall · search · ask · index · embedding · hit ·
evidence · cited · referent · bind · re-check · stale · reader · reader profile · receive ·
withhold · withheld topic · leave out · unasked · deliberate · write-trust · propose · queue ·
ratify · trusted session · never-shared · fail closed · check · finding · resolve · doctor ·
reconcile · publish · refuse · hook · plugin ·
skill · remote interface · credential · session · agent · operator · install · config key ·
default · in force

## Naming something new

Ask what the thing does in the work of keeping a notebook, and name it that. If a word from
another world is the first that comes to mind – a fortress, a courtroom, a battlefield – it is
describing the shape of the mechanism rather than its purpose here. Reach past it for the word a
person would use for the same move in ordinary work: a list of topics a reader does not receive,
a check that refuses, a note that has gone stale.

Where a term of art carries properties the plain words do not, state those properties at the
definition: what it guarantees, why it cannot be bypassed. A reader who does not know the
literature then gains the meaning, and nothing is lost but the jargon.

**Deliberately kept:** none. If you keep an off-register word because its meaning cannot survive
translation, note it here with the reason.

## Voice

This governs the files a stranger reads first: `README.md`, `INSTALL.md`, `ONBOARDING.md`,
`CONFIG.md`, `CONTRACT.md`.

**Write for someone who just arrived.** They have not read the history and do not know the
internal names of past decisions. They want to know what a thing does and when they would want
it.

**Reference, not changelog.** Describe the behaviour as it is now. A setting's entry says what it
controls, what the default does, and when to change it. It does not say what the default used to
be or who decided. Words like *previously*, *this was changed*, *flagged for review* and
*judgment call* mean internal deliberation has leaked onto a reference page.

**No citations a reader cannot follow.** Do not point at an internal spec, an audit, or a
numbered decision. If the reason matters, say it in a clause. If it does not, drop it.

**State limitations plainly**, in the same voice as everything else. A stated limitation is worth
more than a claim the reader later finds was optimistic.

**Keep the specificity.** Plain is not vague. Name the file, the command, the setting, the
failure.

## Plain prose

Write like a person who knows the tool explaining it to someone who does not. Some habits to
watch, because they creep in especially when the text is drafted by a model:

- **Say it once.** If a sentence restates the previous one in better words, keep the better one
  and delete the other.
- **Two examples, not three.** Lists of three parallel items are a rhythm, not information.
- **Prefer a full stop to a dash.** One aside per sentence at most. Where a dash earns its place,
  use a spaced en dash – like this – not an em dash. The rule governs prose only: fenced code
  blocks keep their bytes as they are, and a format specimen (text the tool itself writes, like
  the HEAD header line in RUBRIC.md's template) must match what the tool emits, em dash and all.
- **Skip the closing flourish.** A paragraph can end on its last fact.
- **Drop sentences that only announce.** "It is worth noting", "The aim is", "This means that".
- **Bold to define a term, not to stress a point.**
- **Don't hedge for tone.** Write "this fails" when it fails, not "this may sometimes fail".

Short and specific beats balanced and complete.
