# Assurance

The verification record for the release: what is checked automatically, and what is
deliberately left unchecked. Every standing gate below is one you can run yourself on a fresh
clone, without taking the author's word for it.

## Standing gates

Every gate below runs in CI on each push and pull request (`.github/workflows/ci.yml`), and you
can run each one yourself on a fresh clone. The two counts in the table are not maintained by
hand: `tests/py/test_assurance_counts.py` re-derives them (pytest's own collection, and the
`tests/test-*.sh` glob that `tests/run.sh` iterates) and fails if this document has drifted from
them. The same file derives the atomic-write call-site count quoted under *Known limitations*,
by parsing the package, and checks it against every document that states it — that number was
hand-written until a commit added a site and left two documents saying the old one.
`CONFIG.md` is pinned to the config registry the same way.

| Gate | Command | What it covers |
|---|---|---|
| Shell suites | `bash tests/run.sh` | 23 end-to-end suites against a throwaway store: the write path, locking, multi-store composition, recall, the consistency checks, redaction, the frozen output surfaces |
| Python tests | `python3 -m pytest tests/py -q` | 1033 tests over the engine modules. A few skip rather than run, and how many depends on the host: store-dependent ones (whether there is a real store to compare against), the count gate itself when `pytest` is not installed, and collation-dependent ones when the `en_US.UTF-8` locale is missing — measured here as two with a real store and a full locale, six on a stripped host with neither. No total is quoted deliberately: the count gate below pins the collected total and the suite count, not the skips, so a number written here would be the one claim on this page nothing checks. Includes a generated-docs parity test: `CONFIG.md` must be byte-identical to a fresh render of the config registry, so a documented setting cannot drift from the code |
| Plugin manifest | `claude plugin validate . --strict` | The plugin and marketplace manifests are well-formed and complete |

A cold-install job also runs the whole thing the way a stranger would: fresh `$HOME`,
`setup.sh`, then a real write/read/finalize cycle against a store created from scratch.

## Known limitations

These are deliberate, not oversights.

**The store cannot tell quoted text from decided text.** It records what the writing session
wrote, and a quotation and a decision arrive as identical bytes. The attribution convention in
update-lines – which model wrote it, when, who is being quoted – is a discipline the writer
keeps, not a property the tool enforces or verifies. See `CONTRACT.md`
("What the store does not record"). Anything downstream that treats a stored line as evidence
of what a person decided is trusting the writer, not the tool.

**Delete is not covered by write-trust.** Write-trust decides who may author on protected
topics; the store's delete verb is not behind it. This is a policy choice: the store is a git
repository, so a delete is recoverable from history, and putting a verb that removes things
behind the same mechanism would make an ordinary cleanup depend on trust state. If you need
delete covered, restrict it at the filesystem or repository layer.

**A queued write lives in one place, and that place is not the store.** When write-trust diverts
a write, the proposed text is held in the pending-findings file under your state directory until
a trusted session ratifies it. Every other section of that file is derived and regenerates on the
next check; a queued write does not. It is not in git, it is not in the store, and nothing rotates
or backs it up, so an edit to that file loses it silently. Ratify or discard proposals rather than
letting them sit, and if you rely on the gate, back up the state directory alongside the store.

**Some writes are atomic, and none are crash-durable.** The fourteen replace-the-whole-file writes
in the Python engine go through `quintessence/atomicio.py`, which writes a sibling temp and
`os.replace`s it into position, so a concurrent reader of one of those sees either the whole old
file or the whole new one.

The rest do not. Re-deriving that list needs **four** forms searched, not two — `Path.write_text`,
`open(…, "w")`, `shutil.copy`/`copy2`, and `shutil.copyfileobj` — and an earlier version of this
section named only the first two, which silently omitted every `shutil` write. Two further traps,
and they run in opposite directions. A regex over `open(` misses `admin.py`'s `.gitignore` write,
whose path argument contains brackets — the search under-reports. And `search.py`'s
`shutil.copyfileobj` is a hit the search over-reports: it streams the D4 cache migration *into*
`atomic_write`, so it is one of the atomic writes above, not one of the non-atomic ones below.
That sentence used to say the opposite, and it was true when it was written — the same commit
replaced the `shutil.copy2` it described with the streaming form and left the description behind.
A four-form search gives you candidates; each one still has to be read: the destination is what
decides, not the call:

- **`write.py`, six sites** — HEAD bodies and the index, via `Path.write_text`, which truncates in
  place, so a concurrent reader *can* catch a partial file. Covered by `qq_lock` and by git rather
  than by atomicity.
- **`cli.py`'s `render_menu`, one site** — writes the index during a *read* verb and is
  deliberately lock-free (its own docstring calls it "the one exception to 'no writes' in the L3
  read-verb layer"), so neither atomicity nor the lock covers this one. It is regenerated on the
  next call.
- **`admin.py`, at `qq init`** — the git hooks and `.gitignore`, written into a directory being
  created, where there is no concurrent reader to protect.
- **`write.py`'s HEAD snapshots, three `shutil.copy` sites** — the pre-edit backup at two sites
  and the restore at one. The restore (`shutil.copy(snap, f)`) is a seventh non-atomic write of a
  HEAD body, so "six sites via `Path.write_text`" above is the `write_text` count, not the count
  of non-atomic HEAD writes.
- **The shell helpers `findings.sh` and `consistency-audit.sh`** — still `mktemp` + `mv`, which is
  neither symlink-safe nor atomic across filesystems.

**An atomically-written file has 17 bytes less name room than the filesystem gives you.** The temp
is `<target>.tmp.<12 hex>` and has to fit in the same directory, so where `NAME_MAX` is 255 — ext4,
btrfs, xfs, tmpfs — the longest basename qq can write atomically is **238 bytes**. From 239 to 255
a plain `open()` still succeeds and this refuses, so the band you lose to atomicity is 17 bytes
wide, not the 13 an earlier version of this paragraph gave. Part of that band is an upgrade taking
name room away and part of it was never yours: the hand-rolled idiom this module replaced spent 4
bytes on `.tmp` and so reached 251, so 239 to 251 is room a qq that used to work no longer has,
while 252 to 255 is room only a non-atomic `open()` ever had. Measured here on tmpfs and on btrfs,
both `NAME_MAX` 255: `open()` succeeds at 238, 239, 251, 252 and 255 and fails only at 256, while
the atomic write takes 238 and refuses everything above it. 255 is the filesystem's limit; 251 was
the old idiom's.

The targets whose names you choose are the five settings that name a file outright — `QQ_CACHE`,
`QQ_CONFIG`, `QQ_RECONCILE_SNAPSHOT`, `QQ_XREF_CONTENT` and `QQ_XREF_WAVEOFFS`. Every other target
is a fixed short basename in a directory you configure, and a directory name runs against
`PATH_MAX` rather than this budget. `QQ_CACHE` is the one to watch, because the file actually
written is not the name you set. Identity-scoping inserts `.<identity>` before the
extension, and the orphan-ages sidecar appends `.orphan-ages.json` to that, so the longest name qq
derives from `QQ_CACHE` runs **58 bytes** longer than the one you configure when that name has an
extension, and **63 bytes** longer when it does not: identity-scoping has to insert before
something, so where there is no extension it supplies `.json` (`search.py`'s `ext = ext or
".json"`) and you carry those five bytes whether you asked for them or not. Measured with the
default embed model: 41 bytes for the identity, 17 for the sidecar, and 5 more for the extension
it supplies; the identity carries the model name, so a longer model name costs more. Budget for
that, not for the 238: with this model the last configured cache basename that works is **180
bytes** with an extension and **175 bytes** without one, and one byte more fails on the sidecar
write. Until 2026-08-04 this paragraph gave only the 58 and the 180, so an extensionless 180 read
as one byte inside the limit and lost its sidecar anyway. It loses it loudly — that write goes
through `best_effort_write`, so every build prints the refusal with the arithmetic in it, and what
stops is the orphan-decay bookkeeping, not the cache.

`setup.sh` open-codes the same write for `~/.claude/settings.json` (it runs before the package is
importable) and has **one consumer of name room the engine does not**: the `.qqbak-<UTC stamp>`
backup it takes before any change, which costs **23** bytes, not 17. So the budget that binds
there depends on whether the file already exists — 23 for a rewrite, 17 for a fresh one — and both
are checked. Until 2026-08-04 neither was: a 245-byte `CLAUDE_SETTINGS` basename gave a raw
`OSError: [Errno 36]` traceback out of `shutil.copy2`, where the 4-byte `.tmp` idiom it replaced
had succeeded.

The refusal is loud and states the arithmetic ("an atomic write needs 17 bytes of temp-name room
beside the target: this name is N bytes and the directory allows 255, so a file written atomically
here may be at most 238 bytes of name"), because the kernel's own
`ENAMETOOLONG` names a temp path you never chose. The alternative — silently trimming the basename
to fit — was refused deliberately: the temp would no longer carry the target's basename as a
prefix, which is one of the two conditions the reclaim below runs on, and an operator could no
longer recognise it from the shape documented there. The boundary is pinned in
`tests/py/test_atomicio.py`, with `open()` on the same name as the control.

**Where "loud" means a warning rather than a failure.** Three writes are bookkeeping kept beside
the real thing: the orphan-ages sidecar next to the embedding cache, the reconcile snapshot, and
the xref content-hash store. None is worth failing a search or a check over. All three used to
discard every write failure alike, so at a long enough `QQ_CACHE` the sidecar simply stopped
appearing and nothing said so. They now print the arithmetic above to stderr and carry on. A
transient failure keeps the old silence, because a full disk or a read-only mount may be gone by
the next run; a name that can never work is said out loud every time it is tried.

**A temp older than an hour beside a target is deleted, and exactly ONE name counts as a temp.**
`atomicio` reclaims litter left by a hard kill, and it recognises litter by name. After each
atomic write it looks in the real file's own directory (see *where*, below — through a symlink
that is not the directory you named), at files older than an hour whose name is built from that
file's basename, and removes this and nothing else:

- **`<target>.tmp.<tail>`**, where the tail is **exactly 12 lowercase hex characters, at least
  one of them a letter** — nothing wider than the name this package writes, and, in that last
  condition, deliberately a little narrower than it. *Both* conditions, every time this rule is
  stated: the width alone claims your `notes.tmp.202608041200`, which is why the letter is there
  (see the bounds section below for what that costs). Until 2026-08-04 the rule accepted any tail of 8 or more
  `[A-Za-z0-9_]` characters, on the stated grounds that temps from an earlier build used `mkstemp`
  tails. That rationale was wrong: the `mkstemp` code existed only between two commits, and the
  only install that ever ran it was the author's own mirror, for five hours on 2026-08-03. Nobody
  else's disk can hold such a file. Meanwhile the window was deleting files that really do exist —
  a `report.tmp.20260804`, a `backup.tmp.snapshot`, another tool's `other-tool.tmp.a1b2c3d4` —
  one hour after they were written, in a directory whose documented age policy is 60 days. Those
  are now left alone.

Everything else is left alone: a bare **`config.tmp`** with no tail at all (see below — this rule
was removed on 2026-08-04 and used to cost you that file), a tail that is not shaped like a
generated one (`notes.tmp.md`, `notes.tmp.bak`, `notes.tmp.backup.tmp`, and since 2026-08-04 also
`notes.tmp.20260803`, `notes.tmp.original` and `notes.tmp.markdown`, all of which the old 8+
window claimed), a different basename (`configuration.tmp` beside a target named `config`), a
temp younger than the hour, and anything in a directory qq does not write to — which is a smaller
carve-out than it sounds, because a symlink moves that directory.

**The bare `<target>.tmp` rule is gone, and if you installed before 2026-08-04 you may want to
clear its litter by hand.** A second rule used to sit beside the one above: `<target>.tmp`
exactly, with no tail. That is the pre-atomicio spelling — the hand-rolled idiom `atomicio`
replaced left `<path>.tmp` behind on any exception — so the rule was aimed at real litter a
crashed pre-atomicio install really can hold. It was still the wrong trade, and it was removed:
the current writer never produces that name, so the rule could only ever delete a file this
package did not write, and nothing in the name tells the old idiom's litter apart from your own
`cp config config.tmp` beside `~/.config/quintessence/config`. Reproduced end to end through
`qq config set` before removal — the backup was gone after one write an hour or more later. A
permanent file-deleting rule was too much to pay for a one-time migration, so the migration is a
one-time job you run: `python3 tools/reclaim_legacy_temps.py` lists every `<target>.tmp` whose
`<target>` is a file the old idiom wrote, in the directory it wrote it in — one directory each,
never a subtree, and never your note store, which no idiom site ever wrote into — with its age,
in two groups. Ones with a `<target>` beside them are the ordinary case and `--delete` removes
the aged ones. Ones with no sibling are listed as possible
orphans and need `--delete-orphans`: the old idiom did not require its target to exist, so an
interrupted first write leaves one, but nothing corroborates that such a file is ours rather
than yours. See RELEASE-NOTES.md.

The generated-tail rule that remains is bounded, and the bound is stated here rather than
implied. An earlier version of this section justified the width with a compatibility story —
mkstemp-era temps needing to be reclaimable — which publication history falsifies; a later one
called the result closed, on the grounds that nothing you can plausibly own collides with twelve
hex characters. That was wrong, and specifically: `date +%Y%m%d%H%M` is twelve characters and
every one of them is valid lowercase hex, so `settings.json.tmp.202608041200` — a backup of your
settings named the way people name backups — was deleted an hour after you took it, by the
engine and by `setup.sh` alike (twenty-first pass, F2).

So the rule now carries one more condition: the tail must contain at least one of `abcdef`.
That is deliberately narrower than what the writer emits, and it costs something you should know
about. About one generated temp in 281 gets an all-decimal tail, and those are no longer
reclaimed at the one-hour grace: in the embedding-cache directory the `QQ_CACHE_GC_DAYS` sweep
still reaches them, in the state directory nothing does. Under-deleting is the direction chosen
on purpose — a rule that deletes a file of yours costs you something you cannot get back, and a
temp left behind costs bytes.

What is true, and all that is claimed: a name this package deletes is a name this package wrote.
The converse is not claimed. A tail that is twelve lowercase hex characters *with* a letter in
it — a short git hash, say — is still a name you could in principle choose, and beside a file qq
writes, at least an hour old, it would be claimed. The rules are written out here because
deletion deserves saying out loud; an earlier version named only the generated rule and closed by
calling names outside it safe, which was wrong in the one direction that costs an operator a file
(fifteenth pass, F2).

The rule applies beside `~/.claude/settings.json` too, and until 2026-08-04 it did not.
`setup.sh` adopted the unique temp names without the sweep that makes them safe: the old idiom
reused one `settings.json.tmp` and the next run overwrote it, so litter self-limited to a single
file, while unique names leave a NEW one per interrupted `--wire-claude` and nothing anywhere
swept them. If you have run `--wire-claude` and killed it, look for `settings.json.tmp.*` in
`~/.claude`; the next successful wiring removes any older than an hour — **every** successful
wiring, including one that finds the hooks already wired and changes nothing. That last part was
not true when it was first written: the sweep ran only on a run that rewrote `settings.json`, and
since `--wire-claude` is idempotent, an install that had settled never swept again. The sequence
that leaves the litter made it permanent, because the retry that does have work to do sweeps
while the temp is still seconds old and inside the grace (twenty-second pass, F2). `setup.sh`
open-codes the same one rule the engine runs, and a bare `settings.json.tmp` survives there too.

**WHERE this rule applies is decided by the symlink, not by the path you configured.** The sweep
runs in the RESOLVED parent: the temp is created beside the real file and `os.replace`d onto it, so
the directory swept is the one the link points into. If you version `~/.config/quintessence/config`
by symlinking it into a dotfiles repository — the exact case this module's symlink support exists
for — then everything above happens inside THAT repository, beside the real file. A generated-tail
temp you keep there is deleted an hour or more later, the next time `qq config set` writes.
Reproduced end to end, and pinned in `tests/py/test_atomicio.py` by the sharpest form of it: the
same name in the link's own directory survives, because qq never writes there. "A directory qq
does not write to" always meant the resolved one. The cleanup script resolves the same way, so it
looks for legacy litter where the writer would have left it.

Measured against the function itself on 2026-08-04 — ten names planted beside targets called
`config` and `notes` and aged two hours: two removed (`config.tmp.a1b2c3d4e5f6`,
`notes.tmp.0f1e2d3c4b5a`), eight untouched. Those two lists are read back out of this section and
re-run against `_reclaim_stale_temps` by `tests/py/test_atomicio.py`, so the examples above are
executed rather than asserted. The same measurement one revision earlier removed a third name,
`config.tmp`, and the one before the tail narrowing removed four of thirteen, including
`config.tmp.20260803` and `config.tmp.original`.

Atomicity is in any case not durability: nothing calls `fsync` on the file or on its directory, so
an unlucky power loss or kernel crash in the moments after a write can lose it. A killed process cannot — once the
bytes reach the page cache they survive the writer's death.

Whether a crash can leave a *truncated* file rather than cleanly reverting to the old one depends
on the filesystem. ext4 in its default `data=ordered` mode and btrfs (copy-on-write, journalled
metadata) both give the old-or-new outcome; ext4 `data=writeback`, overlayfs and network mounts do
not. Check yours with `findmnt -no FSTYPE,OPTIONS <path>` rather than assuming — the list above is
not exhaustive, and the answer for the filesystem you are on beats the answer for the ones we
happened to enumerate. The exposure is small in practice because the largest files written this
way are regenerable caches and the store itself is a git repository. Whatever the filesystem,
treat committed history rather than the working tree as the durable record.

**The outward text filter recognizes patterns, not intent.** On the remote read path the primary
protection is the policy filter, which drops whole hits on withheld or never-shared topics before
anything is rendered. The second layer – the sanitizer that scrubs what remains – works by
pattern: it strips absolute filesystem paths and `qq` command references from note content. It
cannot enumerate every local-only tool name an operator might write into an allowed topic, so a
command reference other than `qq` (a service-manager or log invocation, say) passes through if it
carries no absolute path. If a note in an allowed topic must mention local commands you consider
sensitive, put the topic on the withheld list rather than relying on the sanitizer to recognize
the command.

## Release rule

A release is not produced by a green pipeline. It requires both:

1. The maintainer's explicit decision to release, and
2. An independent verification pass run in a fresh session, with no context from the work
   being verified.
