# Assurance

The verification record for the release: what is checked automatically, and what is
deliberately left unchecked. Every standing gate below is one you can run yourself on a fresh
clone, without taking the author's word for it.

## Standing gates

Every gate below runs in CI on each push and pull request (`.github/workflows/ci.yml`), and you
can run each one yourself on a fresh clone. The two counts in the table are not maintained by
hand: `tests/py/test_assurance_counts.py` re-derives them (pytest's own collection, and the
`tests/test-*.sh` glob that `tests/run.sh` iterates) and fails if this document has drifted from
them. `CONFIG.md` is pinned to the config registry the same way.

| Gate | Command | What it covers |
|---|---|---|
| Shell suites | `bash tests/run.sh` | 22 end-to-end suites against a throwaway store: the write path, locking, multi-store composition, recall, the consistency checks, redaction, the frozen output surfaces |
| Python tests | `python3 -m pytest tests/py -q` | 844 tests over the engine modules. Two skip on a machine with no real store to compare against; the rest are hermetic. Includes a generated-docs parity test: `CONFIG.md` must be byte-identical to a fresh render of the config registry, so a documented setting cannot drift from the code |
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
