# Invariants — quintessence

One sentence per invariant, the rule that derives every site it binds, and the test that
quantifies over that derivation. A fix diff is judged against this register before it is
priced.

Entries are keyed by **subject**, not by a running integer: a number read off the tail
collides the moment a branch is a second writer, and this repo is worked on branches. Cite
an entry by its slug from commit messages, briefs and ledger rows.

## `the-store-face-defines-no-write-verb`

**`qq-remote-mcp` serves exactly `search_continuity`, `ask_continuity` and `resume_brief`, and
never grows a fourth.** Its safety argument is structural rather than configural — "a file that
never DEFINES those tools cannot serve them, whatever the config says" — and that argument is
retired the moment any write verb is added beside them, however carefully guarded.

*Instance that earned it:* a hosted feature needed a write verb (2026-08-27). Adding it here
would have falsified nine claims across four files, including the `instructions` string a
hosted session actually reads. Thomas ruled for a second process instead, which is why this
entry exists to say what that ruling bought.

- Site derivation: the `@mcp.tool()` decorations in `qq-remote-mcp`, read by AST rather than by
  grep — `from quintessence import (a, b)` split over lines is the form these scripts use and
  the form a grep misses.
- Pin: `TestEnumeratedVerbNoLocalLeakage.test_all_verbs_both_profiles` in
  `tests/py/test_remote_mcp_policy_wiring.py`, which asserts the REGISTERED verb set equals the
  three exactly under both profiles; a non-empty expected set is its own positive control.

## `a-named-endpoint-is-certified-against-what-it-says-it-serves`

**An endpoint whose request will name a model passes its probe only when that model is in the
list the endpoint itself publishes; a process answering is not by itself a certification.**
The question a probe answers is whether the request the ladder is about to send will succeed
there, so what it checks is whatever the server makes that request depend on: a server that
answers with whatever it loaded (llama-server) needs only to be up and past loading, while a
server that selects by name (ollama) needs to hold the name, and a name it does not hold is a
404 on the completion.

*Instance that earned it:* the probe returned on the HTTP status alone, so `QQ_ASK_MODELS`
could name a model the endpoint had never pulled and that rung passed its probe every time.
The model name landed in config at `8bec28e` (2026-08-18) without anything validating it,
and the rung's 404 was reported as "healthy but the completion failed".

- Site derivation: the arms of `Ask.probe_endpoint`, one per request shape, enumerated from
  `Ask._model_for` — the same reading `Ask._complete` builds the request body from, so a third
  request shape added tomorrow has one place to answer for and not two.
- Pin: `tests/py/test_endpoint_model_certification.py` (landed by `e326a63`; certify `done/20260902T030532Z-certify-certify-a-named-ask-endpoint-is-certifie`).

## `a-rung-that-fails-to-complete-falls-to-the-next`

**Every configured endpoint is asked in turn until one answers: a rung that certifies and then
fails to complete is followed by the next rung, raw retrieval is what a question gets only when
every rung has failed, and the rung that answered is named on a surface a person or a check can
read.** A ladder is configured because rungs differ — a fast tier and a slower one that can
still answer — so one rung's timeout is a fact about that rung, not the ladder's verdict; and
an answer from the third rung reads exactly like an answer from the first unless the rung is
named. Which surface is a property of the face: the CLI and the local MCP tool are told in the
rendered answer, the remote face is told in its own audit line, because there the endpoint is
an internal address it carries none of.

*Instance that earned it:* `ask()` picked one endpoint and any exception from that single
completion call returned raw retrieval with the rest of the ladder untried, under a message
("no completion endpoint healthy") that named the wrong thing. Measured 2026-07-13: a CPU
endpoint spent 114s in prefill and was cancelled, and the user was sent to look for a dead
server.

- Site derivation: `Ask.certified_endpoints`, which is the one enumeration of the ladder that
  both `Ask.ask` and `Ask.pick_endpoint` read, over the whole of `Ask.endpoints`; and every
  return path out of `ask()`'s ladder loop. For the naming clause, every face that turns an
  `ask()` result into something read — enumerated from the callers of `quintessence.ask.render`,
  which is the one renderer.
- Pin: `tests/py/test_ask_rung_ladder.py` (landed by `06c112c`; certify `done/20260902T030532Z-certify-certify-an-ask-rung-that-fails-to-comple`).

## `a-test-that-needs-a-user-bus-skips-and-says-so`

**No test's verdict may depend on whether the process that invoked it has a user D-Bus: a test
whose question only a live systemd *user manager* can answer declares that dependency and skips
when nothing answers it — and still runs, and may still fail, when something does.**

*Instance that earned it:* `TestUnitDriver.test_nonexistent_unit_is_missing` asked the real unit
driver about a unit that does not exist. `_fp_unit_bases` probes both scopes and treats a scope
that answers anything other than the absence diagnostic as an error — correctly, since a unit it
cannot read must not be reported absent, and absence is the loud path — so on a host with
`systemctl` installed and no session bus the whole suite came back red for the environment of the
invoker, not for the tree. Measured at `ed478f5`:
`env -u XDG_RUNTIME_DIR -u DBUS_SESSION_BUS_ADDRESS bash tests/run.sh` →
`Ran 1112 tests ... FAILED (errors=1, skipped=2)`, and green again on the spot with
`XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`. This is
ledger **D40** and **D104** (open since the first contact: "environment-dependent gate", R6's own
note being that this suite's guards were *hand-added per environment*), and it is not a disclosure
that stays cosmetic, because it is the standing shape of every unattended run on this host:
the queue worker unit is a SYSTEM unit and no fragment under `/etc/systemd/system/` sets
either variable, so a queue worker has no user bus by construction. The bash half of the suite
already answered this way — `tests/test-tsk.sh` prints `SKIP: no systemd user manager on this
host` and ran it in the same measurement — and this row is that precedent, derived instead of
hand-added. And it becomes a LANDING condition the moment the estate's merge gate resolves this
repository's own `tests/run.sh`: measured 2026-09-11 at the deployed gate revision, only
the queue's own `tests/run.sh` is resolved today and the widening is on an unlanded branch — but that
gate takes ABSOLUTE GREEN, so on the day it lands the environment of whoever runs the suite becomes
a landing condition. Measured in the meantime, the same stripped environment makes `qq-promote
--go`'s own default gate (`python3 -m pytest tests/py -q` over the dev tip) come back
`1 failed, 1109 passed, 2 skipped` at `ed478f5`, which fails safe — prod does not move — but does
not move at all.

- Site derivation: `_systemctl_surface()` in `tests/py/test_refs.py` — every function in
  `quintessence/refs.py` that passes a literal `systemctl` argv to a subprocess entry point, plus
  exactly two layers of callers within that module (`_fp_unit_bases` → `_fp_unit` →
  `_fingerprint`), read from refs.py's own AST; crossed with
  `_unmocked_surface_callers()`, which walks every `tests/py/test_*.py` and keeps the test methods
  that call a member outside a `with` block installing a double (`mock.patch`/`patch.object`, or
  this repo's `fake_*` convention). Measured at this commit: surface
  `['_fingerprint', '_fp_unit', '_fp_unit_bases']`; eight surface calls across `tests/py`, seven of
  them inside test methods — six under a double and correctly excluded, the seventh
  (`TestUnitDriver`) unmocked and guarded — and the eighth in the helper
  `TestUnitEnablementFold._fp`, which is outside the walk by construction.
  **Bound of the derivation, stated rather than assumed:** it reaches functions that shell out to
  `systemctl` *unconditionally*, reached *directly from a test method*. Two shapes sit outside it:
  a test that arrived at a unit fingerprint through `bind_write` with unit prose and no double
  (including `bind_write` would flag every bind test in the file, almost none of which names a
  unit), and a driver call hidden behind a test-local helper — the `_fp` helper above is exactly
  that shape and is safe only because the helper installs its own double, so a helper that
  installed nothing would be invisible to this pin. Reopening condition: one observed test of
  either shape.
- Pin: `TestUserManagerGuard.*` (the predicate both ways on doubles — the refusal text captured
  verbatim, the older `Failed to connect to bus:` spelling, an answering manager, a manager that
  answers *and disagrees* which must NOT skip, and an exploding `systemctl`) and
  `TestBusGuardCoversEveryUnmockedDriverSite` (quantifies over the derivation).
  **Mutations, each run separately and confirmed on disk:** reverting the guard leaves the suite
  `FAILED (failures=1, errors=1)` without a bus — the original error plus the pin naming the
  site — and the pin fails on its own *with* a bus present; forcing the predicate to
  always-answer gives `failures=2, errors=1`; forcing it to always-refuse gives `failures=2` with
  the guarded test skipping on a host that HAS a manager, which is the vacuous-skip shape D115
  names and the reason the predicate is pinned in both directions rather than one.
- **Residual, not covered:** the guard asks only about the USER scope. A host where `systemctl` is
  installed but systemd is not PID 1 could refuse in the *system* scope too, and the driver would
  raise there with this guard satisfied. Not claimed either way — every host this was measured on
  answered the system scope from disk without a bus. Reopening condition: one observed run where
  `systemctl cat -- <absent>` does not print the absence diagnostic.

## `a-per-endpoint-directive-is-sent-only-where-config-names-a-well-formed-one`

**Every per-endpoint directive `Ask._complete` merges into the completion body is sent only for
an endpoint whose configuration names a well-formed value for it, and is otherwise left out of
the body entirely — never sent as a placeholder, a default, a zero, or the raw text the config
line happened to carry.** A directive is an instruction to the SERVER about how to serve this
particular request, so a wrong one is not a slightly worse answer, it is a refused request: a
404 for a model name the endpoint does not hold, a rejection for a slot it does not have. `ask`
reads any such refusal as "this rung failed" and steps down its ladder, so a single malformed
character in a config line silently costs the answer a healthy tier — and the answer that
eventually comes back from a slower rung looks exactly like an ordinary one.

*Instance that earned it:* `qq ask` had no way to name a slot at all. On a resident multi-slot
llama-server that left the server to place the request, which on an idle server means slot 0 —
the slot HA voice is pinned to, and the only slot `ha-model-router` asks about when deciding
the backend is busy (`ha-model-router/tests/INVARIANTS.md` I2). A `qq ask` landing there makes a
voice question wait behind a 200,000-character evidence pack or be sent to the 4B ollama floor
with the 125B loaded and idle: measured 2026-09-10, a trivial voice question answered after 32s
behind another client's generation. The fix is a slot number read from configuration — which is
exactly where a malformed one would come from, so both halves of the rule are the same change.

- Site derivation: `Ask._BODY_DIRECTIVES` — the one table naming each directive and the
  accessor that derives it, which `_complete` BUILDS the body by walking rather than consulting
  alongside a hand-written sequence of `if`s. A directive added later is held to the rule by
  the existing pin without amending it, because the pin quantifies over the table.
- Pin: `TestPerEndpointDirectives` in `tests/py/test_ask.py` (quantifies over the derivation:
  every directive's accessor exists, and with nothing configured for an endpoint no directive
  key reaches the body) and `TestSlotPinning.*` (the `id_slot` instance in both directions —
  sent for an endpoint that names a slot, an int on the wire and not a string, slot 0 sent
  rather than dropped as falsy, absent for an endpoint that names none, absent for each of
  seven malformed values, and that endpoint's budget/model lookups and `/health` probe left
  untouched).
  **Mutations, each run separately and confirmed on disk:** removing the `id_slot` row from
  `_BODY_DIRECTIVES` — the fix itself — leaves `3 failed` (sent, int-on-the-wire, slot 0), and
  no other test in the file notices, which is the measure of how narrow the old behaviour was;
  testing the directive for truthiness instead of `is not None` leaves `1 failed`, slot 0 alone,
  the whole reason that test exists; passing the config text through unparsed leaves `12 failed`
  — the three malformed subtests that can only fail this way, plus the budget assertions
  (`'8000' != 8000`), which is the shared parser proving it is shared; dropping the
  non-negative guard leaves exactly the `-1` subtest failing; and writing the guard as
  `isdigit()` then `int()` — the shape the budgets parser used before this change — leaves the
  `²` subtest failing with `ValueError` raised out of `Ask.__init__`, i.e. `qq ask` dying at
  construction rather than ignoring one bad line.
- **Residual, not covered:** no test here reaches a real llama-server, so what this pins is the
  REQUEST — that a well-formed slot goes out as `id_slot` and nothing goes out otherwise. That
  the server then honours it, and refuses a slot it does not have, is llama-server's behaviour
  and is taken from its own documentation and from the sibling implementation that already
  sends the field (`ha-model-router`, which pins voice the same way). Reopening condition: one
  observed run where a request carrying a valid `id_slot` is served on a different slot.

## `a-url-int-config-value-is-read-only-when-spelled-in-plain-ascii-digits`

**A `url=<whole number>` config entry is read only when its value is spelled in plain ASCII
digits, and every other spelling — a digit separator (`1_0`), a sign (`+1`), a Unicode decimal
digit — is ignored exactly like any other malformed entry, leaving the endpoint unconfigured —
because `int()`'s wider grammar reads those as numbers, and a config line whose value only a
Python cast recognises has not named a whole number, so honouring it sends the server a value
the operator never wrote.**

*Instance that earned it:* the shared `_url_int_entries` parser (QQ_ASK_SLOTS, QQ_ASK_BUDGETS)
recognised whole numbers with `int()` inside a `try`, so `QQ_ASK_SLOTS=http://127.0.0.1:8090=1_0`
— a hand-typo of `1` — was read as slot 10, contradicting CONFIG.md's documented "an entry whose
value is not a whole number is IGNORED": the mistyped pin went on the wire, llama-server refused
the request for a slot it does not have, and `ask` read the refusal as the rung being down and
stepped past the 125B tier to a slower one, with only the answering endpoint changed.

- Site derivation: `_WHOLE_NUMBER_RE.fullmatch` in `_url_int_entries` — the one recognition
  point for both keys shaped `url=<whole number>` (`QQ_ASK_SLOTS` and `QQ_ASK_BUDGETS`), so a
  third key of this shape added later inherits the grammar without a new site.
- Pin: `tests/py/test_ask.py::TestWholeNumberValueSpellings` (landed by `9b42d10`; certify `done/20260929T003639Z-certify-certify-a-url-whole-number-config-value`).

## `a-typed-integer-read-from-configuration-is-ascii-decimal-digits-only`

**A typed integer read from configuration is accepted only as ASCII decimal digits; every other spelling is refused with the field named, the same way a malformed `url=` is.**

*Instance that earned it (D893):* `Config._coerce`'s int branch was a bare `int(raw)`, so `QQ_LOCK_WAIT=1_000`, `+7`, ` 5` or Arabic-Indic digits were accepted as numbers the operator may not have written, while the `url=<whole number>` grammar (D890) refused the same spellings — two grammars in one file.

- Site derivation: `Config._coerce`'s int branch (`_WHOLE_NUMBER_RE` / `_SIGNED_INT_RE` in `quintessence/config.py`), reached by every registered `int` key via `get`. The only key admitting a leading `-` is `QQ_EMBED_NUM_GPU` (Ollama `num_gpu`, -1 = automatic). Refusal is a `ConfigError` naming the key.
- Sweep: other `int(` sites take regex-derived or non-config text (`ask._url_int_entries` is already grammar-guarded; refs/findings/cli parse regex groups or `--ref` text), so none reads raw config text.
- Pin: `tests/py/test_config.py::TestTypedIntegerGrammar`; with the bare `int(raw)` restored 22 tests fail.

## `a-hook-never-decides-permission`

**A quintessence hook that injects context emits `additionalContext` only; no hook in this tree
ever emits a `permissionDecision`.** The PreToolUse hooks were written as "never block" and
spelled that as `permissionDecision: "allow"`, which in Claude Code means "skip the prompt": a
privileged shell command that happened to match the recall regex and score a hit ran unprompted
(posture review 2026-10-08, H1). Silence is the hook's only verdict.

- Site derivation: every `*.sh` under the repo root and `hooks/` that prints a
  `hookSpecificOutput` object — enumerated by grep in the pin, not hand-listed.
- Pin: `tests/test-trust-edges.sh` (`no-permission-decision`).

## `the-unattended-audit-holds-only-the-tools-its-runbook-names`

**The scheduled audit agent is launched with a tool allowlist scoped to the verbs the runbook
asks of it and the one file it writes; a bare `Bash` or `Write` grant is a defect.** The agent
reads store text any session can write, so its grant is the blast radius of a planted
instruction (H2). The baseline file it reads is parsed as `key=value`, never sourced.

- Site derivation: the `--allowedTools` argument in `consistency-audit.sh`; the `. "$BASELINE"`
  idiom (grep for `\. "\$BASELINE"` must be empty).
- Pin: `tests/test-trust-edges.sh` (`audit-tools-scoped`, `audit-baseline-parsed`).

## `the-mcp-passthrough-cannot-reach-a-shell`

**The stdio MCP `qq` tool runs only verbs on its allowlist, refuses a `probe:` reference in any
flag position, never forwards `config`/`init`, and every subprocess it starts gets
`stdin=DEVNULL`; `update_head` passes `--` before the text so prose can never parse as a flag.**
Without this, allowlisting the "notes" MCP tool granted persistent shell execution through
`--ref probe:<cmd>` (M1), and prose starting with `--` either errored or read the JSON-RPC
transport as the HEAD text (M8).

- Site derivation: `quintessence.mcpedge.check_passthrough` is the single gate; the pin execs
  the real `qq-search-mcp` body and drives its tool functions.
- Pin: `tests/py/test_trust_boundaries.py::TestMcpEdge`.

## `config-set-writes-one-line-of-one-registered-shape`

**`qq config set` accepts a key matching `^[A-Z][A-Z0-9_]*$` and a value with no CR/LF, so one
call changes exactly one line; `qq-config.sh` exports only `QQ_*` and `QUINTESSENCE_DIR` into
the hooks' environment.** A value with an embedded newline appended further lines and bypassed
the relocation guard (M2, reproduced); exporting arbitrary identifiers reached `PATH`,
`PYTHONPATH`, `BASH_ENV`.

- Site derivation: `admin.config_set` (the only writer); `_qq_load_config` (the only exporter).
- Pin: `tests/py/test_trust_boundaries.py::TestConfigSet`,
  `tests/test-trust-edges.sh` (`config-export-filter`).

## `the-authoring-gate-reads-its-policy-from-the-file`

**The authoring gate's own keys (`QQ_AUTHOR_GATE`, `QQ_AUTHOR_GATE_SLUGS`,
`QQ_SAFE_MODEL_PREFIX`, `QQ_WRITE_TRUSTED_MODEL`, `QQ_MODEL_TRANSCRIPT`) resolve from
constructor overrides, the config file and the registry default — never from the environment —
and `qq config set` changes them only from an interactive terminal.** An env prefix on the gated
verb switched the gate off or borrowed trust (M3). Residual, stated in the module docstring: the
gate is structural, not adversarial — a session holding a shell can still edit the file or point
`QQ_CONFIG` elsewhere.

- Site derivation: every `config.get*` call in `quintessence/authgate.py` (AST walk in the pin).
- Pin: `tests/py/test_trust_boundaries.py::TestAuthgateEnvBlind`, `tests/test-authgate.sh`.

## `a-write-lands-inside-the-store-after-resolving`

**Every HEAD write resolves its target (and the target's parent) with symlinks followed and
refuses unless the result is inside the store; a symlink is never written through.** The
`update` verb trusted the lexical path, so `qq update X.md` with `X.md -> ~/.bashrc` inserted an
update-line into the shell rc (M4, reproduced). Topic strings carry no control characters
(L2/M9): a newline in a topic split the line-oriented findings file.

- Site derivation: `write._execute_write` (the single file-I/O site for HEAD writes);
  `Store._within_qdir` / `Store.memory_path` for the read verbs and `qq fact` (L1).
- Pin: `tests/py/test_trust_boundaries.py::TestWriteTargets`.

## `a-project-store-is-one-this-operator-scaffolded`

**Walk-up discovery accepts a `.quintessence/` only when it carries the scaffold
`qq init --project` writes (a `.git` directory whose pre-commit hook is the write-lock backstop,
and no `hooksPath`/`fsmonitor` in its config); when `QQ_PROJECT_STORES` is set, the project
root must also be listed. Corpus walks never follow a symlink out of their root.** A cloned
third-party repo shipping `.quintessence/` became the write target, the recall source and a
git repo the Stop hook committed into (M5).

- Site derivation: `storepath._find_project_store._accept` (the single acceptance point);
  the two `os.walk(..., followlinks=True)` sites in `search.py`.
- Pin: `tests/py/test_trust_boundaries.py::TestProjectStoreTrust`, `tests/py/test_storepath.py`.

## `store-text-leaves-only-for-the-endpoint-named`

**Every HTTP call that carries store text (embedding, endpoint probe, completion) goes through
an opener with no proxy handler, so `http_proxy` in the environment never receives it.** urllib
honours `http_proxy` for loopback unless `no_proxy` says otherwise (M6, verified).

- Site derivation: `urllib.request.urlopen` must not appear in `quintessence/*.py` outside
  `httpdirect.py` (grep in the pin).
- Pin: `tests/py/test_trust_boundaries.py::TestDirectHttp`.

## `the-remote-face-names-only-what-policy-allows`

**The hosted MCP face strips "Did you mean" name hints from a brief, clamps `k` to `[1, 50]`,
and its ASGI gate passes only `lifespan` and authenticated `http` scopes — a websocket or any
other scope is closed, not forwarded.** A near-miss topic leaked withheld slugs (M7); `k` was
unbounded (L5); non-HTTP scopes bypassed the bearer check (L4).

- Site derivation: `remoteauth.auth_wrapper`; the `resume_brief`/`search_continuity`/
  `ask_continuity` tools in `qq-remote-mcp`.
- Pin: `tests/py/test_trust_boundaries.py::TestRemoteFace`.

## `a-ready-command-comes-from-the-template`

**`qq findings next` offers as a ready command only a `qq <verb> <slug…>` of the shape the
producers' own templates emit; a backticked span inside an LLM-written or proposed text is
never promoted.** PROPOSED-write and AUDIT lines embed untrusted text verbatim (M9).

- Site derivation: every `qq` command `cli.render_findings_next` emits — the T2-stale `waveoff`, the T1-size `compact`/`brief`, the PROPOSED-write `qq {verb} {head}` and the generic backtick extraction — all pass through `cli._ready` (`_READY_CMD_RE.fullmatch`). The generic backtick extraction is reached only by a line that is NOT a PROPOSED write: the branch is chosen by the line's `[PROPOSED write]` class marker, not by whether the structured parse succeeded, so a PROPOSED line whose parse fails (e.g. a model name with a space) offers no command and says so (D1059; before, it fell through to the generic branch).
- Pin: `tests/py/test_trust_boundaries.py::TestReadyCommands`; `tests/py/test_ready_command_template.py::TestReadyCommandFromTemplate::test_proposed_unparseable_line_does_not_promote_backticks_in_text` (the unparseable-PROPOSED case).

## `a-ready-proposed-verb-is-a-gated-verb`

**Every verb `write._gate_divert` can queue as a PROPOSED write is offered by `qq findings next` for a slug-safe head — as a ready command, or, for a verb whose named channel has no stdin, as its argv form (see `a-bare-ratify-command-is-ready-only-on-a-stdin-channel`); the allowed set is `authgate.GATED_VERBS`, not a hand-written list.**

- Site derivation: the `_gate_divert` call sites in `write.py` (update/new/rewrite/essence) and `cli._READY_CMD_RE`, built from `authgate.GATED_VERBS`; the PROPOSED branch of `cli.render_findings_next` offers a parsed line's verb only when it is in `authgate.GATED_VERBS` (before, it offered any verb of the ready vocabulary, e.g. `show` or `finalize`).
- Check: `tests/py/test_cert_ready_proposed_verb.py` (landed by `ffa7082`). Unit pins: `TestReadyCommands.test_proposed_write_offers_every_gated_verb`; `tests/py/test_ready_command_template.py::TestReadyCommandFromTemplate::test_proposed_branch_offers_only_a_gated_verb` (the allowed set, with a ready-shaped non-gated verb as its control).

## `a-ratification-never-drops-the-proposed-text`

**Every command qq offers as ready for ratifying a proposal carries the proposal's text by the channel that command reads; a command given its text only on a channel it does not read refuses with a non-zero exit (stdin into `qq new` once it has arrived, see Limit), and argv text a command does not read is refused whatever stdin carries, so a ratification never succeeds with the proposed text dropped.**

- Offered as printed: running each command `qq findings next` offers for a queued PROPOSED write of a gated verb, exactly as printed with empty stdin, exits non-zero (or no bare command is offered: for a verb whose named channel has no stdin, `new` and `essence`, the argv form is printed as a `run:` line, see `a-bare-ratify-command-is-ready-only-on-a-stdin-channel`). Check: `tests/py/test_cert_offered_ready_empty_stdin.py` (landed by `838be61`; red at `5385949`, green at `530e88c`). Unit pin: `tests/py/test_ratify_channel.py::OfferedCommandIsRunAsPrinted`.
- Site derivation: the verbs are `authgate.GATED_VERBS` (the verbs `queue_proposal` is called
  with); the channel each one reads is the one the PROPOSED line itself names, read back off a
  line `queue_proposal` wrote; a text can arrive on argv or on stdin.
- Check: `tests/py/test_ratify_channel.py` — every named channel lands the text through the
  real `qq` script; every (verb, channel) pair either lands it or exits non-zero; argv text,
  alone or with data on stdin together, is landed or refused by every verb; and planted
  commands that drop stdin, or drop the argv text while stdin carries data, are caught by the
  same checker (Rule 3 controls).
- Argv and stdin together: argv text is always addressed to the command, so a verb that does
  not read argv refuses it whatever stdin carries (`qq rewrite <topic> <text>` exits 2; before,
  it wrote stdin and dropped the argv, exit 0). A verb that reads argv (`update`, `new`,
  `essence`) lands the argv text and does not read stdin, and does not refuse it either: stdin
  beside argv text may be inherited rather than given (a `while read` loop calling
  `qq update t "$line"` holds the loop's own input on fd 0), so refusing it would break that
  loop in normal use. For `update`, which reads both, argv wins.
- Limit: `qq new` (argv only) refuses stdin data that has ARRIVED, after a short grace for a
  pipe whose writer has not written yet; a writer slower than that is not seen. The line names
  argv for `new`, which has no such window.
  Pin: `NewStdinGrace` in the same file. A pipe writer 0.15 s after spawn is refused (the grace
  starts only after qq's own startup, so 0.15 s is inside it on any load); one 1.5 s after spawn
  gets exit 0 and the placeholder essence. The tests above cannot see the window, because
  `subprocess.run(input=)` writes before qq has started.
  **Mutations, each run separately and confirmed on disk:** grace 0.2 → 0.0 (the first-contact
  review's M3, all tests green before this pin) turns the 0.15 s test red, 3 of 3 runs at load
  ~5.5; grace 0.2 → 2.0 turns the 1.5 s test red. Neither delay is a measured boundary: the
  review measured refusals at ≤0.19 s and placeholders at ≥0.25 s.
- Inherited stdin: `while read t; do qq new "$t"; done < topics` hands each `qq new` the loop's
  own input, so every line but the last is refused (exit 2, no HEAD, no text lost). The refusal
  names the remedy, `qq new <topic> </dev/null`; pinned by
  `NewStdinGrace.test_refusal_names_dev_null_for_inherited_stdin`.

## `a-bare-ratify-command-is-ready-only-on-a-stdin-channel`

**`qq findings next` offers the bare `qq <verb> <head>` as a ready command for a PROPOSED write only when the text channel the line names includes stdin; for every other gated verb it offers no ready command and prints that verb's argv form instead.**

- Site derivation: the PROPOSED branch of `cli.render_findings_next`; the channel is read off the line by `cli._proposed_channel` (before the line's first `; text: `, falling back to `findings.RATIFY_CHANNEL`), never from the verb's name. Before (`c3639bc`) the special case was `verb == "new"`, so `qq essence <head>` was still offered as ready and could only exit 2 with a usage error.
- Check: none yet — certify pending.

## `a-printed-argv-ratification-passes-the-text-whole`

**The argv form `qq findings next` prints for a PROPOSED write (its `run:` line), run by bash exactly as printed, passes the proposed text to the verb as one argv element equal to the text, so the verb lands it or refuses with a non-zero exit.**

- Site derivation: the argv-only arm of `cli.render_findings_next`, quoting through `cli._bash_quote` (a `$'...'` word: `\` and `'` escaped, every non-printable character spelled as its UTF-8 bytes) after `--`. Before, the line was the template `qq new <head> '<the proposed text, decoded>'`, and a text with an apostrophe filled into it landed with its apostrophes gone, exit 0. A text holding a NUL cannot go on argv, and no `run:` line is printed for it.
- Check: none yet — certify pending.

## `an-empty-argv-text-is-refused`

**A write verb that reads argv (`update`, `new`, `essence`) refuses, with a non-zero exit and nothing written, argv text that is given but is empty or whitespace-only once joined.**

- Site derivation: `qq`'s `_blank_argv_text`, called by `_cmd_update`, `_cmd_essence` and `_cmd_new` after their flags and `--` are consumed; `rewrite` already refuses any argv text. Leaving the text off (`qq new <topic>`, `… | qq update <topic>`) is a different form and is unaffected. Before, `qq essence t ''` blanked the HEAD's essence with exit 0, and `qq new t ''` and `qq update t ''` wrote an empty essence or update-line, each dropping any text on stdin.
- Check: none yet — certify pending.

## `scaffold-never-replaces-a-foreign-hook`

**`qq init` writes its pre-commit/pre-push hooks only into an empty slot or over an earlier
copy of its own hook (recognised by the `QQ_WRITE_TXN` marker); a foreign hook is refused with
its path named.** (L6)

- Pin: `tests/py/test_trust_boundaries.py::TestInitHooks`.

## `durable-writes-reach-the-platter-before-the-rename`

**`atomicio` flushes and `fsync`s the temp before `os.replace`.** (L3)

- Pin: `tests/py/test_trust_boundaries.py::TestAtomicFsync`.

## `generated-shell-quotes-every-interpolated-path`

**A path interpolated into generated shell (`tsk`'s runner script, `setup.sh`'s hook command)
is `%q`/`shlex.quote`d at the interpolation.** (L7, L8)

- Pin: `tests/test-trust-edges.sh` (`tsk-state-quoted`, `setup-hook-quoted`).

## `an-installer-path-override-means-what-the-shell-would-have-meant`

**Every path override `setup.sh` reads (`BIN_DIR`, `QUINTESSENCE_DIR`, `QQ_MEMDIR`,
`CLAUDE_SETTINGS`, `SKILLS_DIR`) has a leading `~` or `~/` expanded against `$HOME` before
any path is built from it, and a `~user` form is refused loudly; no override ever produces a
literal `~` directory under the working directory.** (ledger D92: a quoted
`CLAUDE_SETTINGS='~/.claude/settings.json'` used to wire `./~/.claude/settings.json` and say
"Done." with the real settings untouched.)

- Site derivation: the `# Env overrides:` header line of `setup.sh`, the same list
  `tests/test-setup-wire.sh` derives its isolation pin from.
- Pin: `tests/test-setup-wire.sh`, the literal-tilde case (every override at once, under the
  suite's `env -i` allowlist).
- Not reached: `QQ_CONFIG` and `XDG_CONFIG_HOME` are read by `qq-config.sh`, not by
  `setup.sh`, and are not expanded by this rule.

## `a-failed-tsk-restore-is-said-not-swallowed`

**`tsk`'s `stash_back` checks both restore `mv`s and, when either fails, names the stranded
`:prev` file on stderr and returns non-zero, instead of returning 0 with the previous result
left where `status`, `rc` and `wait` do not look.** (ledger D274, the mirror of the CHECKED
forward stash.)

- Site derivation: the two `mv`s in `stash_back`; its callers (`install_failed`, `run`'s EXIT
  and INT/TERM traps) keep their own exit status.
- Pin: `tests/test-tsk.sh` H7 (hermetic, before the manager gate).

## `the-printed-ask-answer-names-the-rung-that-answered`

**The text `qq ask` prints (`ask.render()`) names the rung that actually answered — the same
value as `result["endpoint"]` — on every path that prints an answer, including a step-down
after a failed rung, and never the rung the ladder started at.** (ledger D424 residual: the
`answered by` line could be deleted with the suite green.)

- Site derivation: the `answered by` line in `render()`, fed by the `endpoint` that `ask()`
  returns from the rung it stopped at.
- Pin: `tests/py/test_ask_rung_ladder.py`, `TestThePrintedAnswerNamesTheRungThatAnswered`
  (stepped-down ladder, plus the first-rung-answers control); red with the line deleted and red
  with it printing the first-picked rung.

## `the-count-tool-writes-what-the-gate-reads-or-nothing`

**`tools/sync_assurance_counts.py` either leaves ASSURANCE.md stating exactly the python count
and the shell-suite count the count gate (`tests/py/test_assurance_counts.py`) checks, or exits
non-zero naming the cause and leaves ASSURANCE.md byte-identical — it never writes a figure
taken from a failed collection.**

*Instance that earned it:* the figures were hand-typed, so every commit that added a python test
was red on `test_python_test_count_matches_collection` until a follow-up commit fixed the number
— six count-only mainline commits between 1 and 18 October 2026, and 92cb076 needed a hand
landing because the commit before it was red. The gate stays exactly as strict; the tool is the
author's same-commit route to making it true, and the gate's failure message names it.

- Site derivation: the gate and the tool share one definition of what is counted and where it is
  read (`COLLECT_ARGV`, `COLLECTED`, `PYTHON_ROW`, `SHELL_ROW` in the tool, imported by the gate),
  so there is one collection and no second copy to drift.
- Pin: `tests/py/test_sync_assurance_counts_certify.py` (landed by `18b65ff`; certify `done/20261022T015228Z`, red end by mutation: the earlier certify `stuck/20261018T223105Z` was parked because the tool does not exist at its pre_fix).

**Amendment, 21 Oct 2026 (the clean-checkout clause): the count the tool writes and the gate
checks is the count a clean checkout of the commit gives — the gate fails, naming the cause, on
every collection the tool refuses (a non-zero pytest exit, or a summary reporting an error), a
`tests/py` test file or `tests/test-*.sh` suite that git does not track (untracked or ignored)
is left out of both counts, and pytest's singular `1 test collected` is read as 1.**

*Instance that earned it:* the round-1 review of 0b27e7d (a private review record, N2/N3/N5) measured all three: pytest printed
`1197 tests collected, 1 error` and the gate passed on 1197 while the tool refused; one
untracked probe test made the tool write 1198 and the gate pass in that tree while a clean
checkout was red; and a one-test collection would be refused as a failed collection.

- Site derivation (supersedes the bullet above): the gate calls the tool's
  `collect_python_count` and `count_shell_suites` and catches its `Refused`, so the gate
  refuses exactly what the tool refuses and counts exactly what it counts. "Not tracked" is
  `git ls-files --others` under the engine's own `.git`; an engine with no `.git` of its own
  (an unpacked release) has nothing untracked and is counted whole.
- Not reached (closed by the index clause below): a TRACKED test file's uncommitted edits are
  still counted, so a figure written from an unstaged added test function in an existing file
  can still be red in a clean checkout. Closing that means collecting the index, not the
  working tree.
- Checks: `tests/py/test_assurance_counts.py` `TestTheGateJudgesWhatACleanCheckoutCollects`
  (the gate on fixture engines, the clean-checkout claim on a fresh clone with a control) and
  `tests/py/test_sync_assurance_counts.py` (the tool, `read_collected`).
- Pin (amendment): `tests/py/test_count_gate_clean_checkout.py` (landed by `0af2c12`; certify `done/20261020T231632Z`).

**Amendment, 21 Oct 2026 (the index clause, amending the clean-checkout clause): the tool and
the gate count what a clean checkout of the commit gives by counting the index — collection
runs over a temporary checkout of the engine's index, and the suites are the index's
`tests/test-*.sh` — so an unstaged edit to a tracked test file, a tracked file deleted only from
the working tree, and an untracked or ignored file change neither count, while a staged file
counts in both, being part of the commit about to be made; and every git call either makes runs
with `GIT_*` stripped from its environment, so a `GIT_INDEX_FILE` or `GIT_DIR` set by a hook
cannot name another index.**

*Instance that earned it:* the clean-checkout clause above disclosed that an author who adds a
test function to an existing tracked file and runs the tool before staging writes a figure a
clean checkout of the commit cannot reproduce, so the gate goes red at the landing; and the
`GIT_*` stripping that clause relied on was pinned by nothing (done/20261020T230413Z notes[0],
notes[1]).

- Choice: collect a checkout of the index, not refuse a dirty tree. The gate shares the tool's
  collection, so a refusal on dirty tracked test files would redden the whole suite whenever an
  author is mid-edit; the tool instead names each left-out file on stderr ("has unstaged
  changes", "git does not track it"), so an edit meant for this commit is `git add`ed and the
  tool run again. In a clean checkout — the landing — the index is the commit, so the gate
  judges the commit; before a commit it judges the commit being made, the same tree the tool
  counted, and the two cannot disagree. An engine with no `.git` of its own has no index and is
  counted as it stands.
- Site derivation: `counted_tree` (`git checkout-index --all --prefix=<tmp>/`) feeds
  `collect_python_count`; `count_shell_suites` reads `git ls-files`; `untracked_tests` and
  `unstaged_tests` name what is left out; all four go through the tool's one `_git`, which
  strips `GIT_*`.
- Not reached (closed by the summary-line clause below): the ASSURANCE.md the gate reads is the
  working-tree copy, not the index's, so a
  figure the tool wrote and the author did not stage still passes the gate before the commit;
  the commit itself is then red, which the gate reports at the landing.
- Checks: `tests/py/test_count_gate_index.py` (black-box on copies of the engine: an unstaged
  added test function, with the tool's figure green in a fresh clone and a control; a tracked
  test and suite deleted from the working tree; a staged new test file; a committed
  `.gitignore` rule, and a file force-added past it; `GIT_INDEX_FILE` naming another index).
- Pin (index clause): `tests/py/test_count_gate_counts_the_index.py` (landed by `00f56e1`; certify `done/20261024T041355Z`). Not covered by the check: `GIT_WORK_TREE` and other `GIT_*` variables beyond `GIT_INDEX_FILE` and `GIT_DIR`, and a staged deletion of a tracked test file whose working-tree copy is restored.

**Amendment, 21 Oct 2026 (the unresolved-index clause, amending the index clause): the tool and
the gate refuse, naming the files and writing nothing, while the index holds an intent-to-add
(`git add -N`) or an unmerged (conflict stage 1 to 3) entry for a `tests/py` test file or a
`tests/test-*.sh` suite — never a figure that counts such a file in one count and not the
other.**

*Instance that earned it:* the round-3 review of 3b1dac0 (a private review record): with a new suite
and a new 2-test file added with `-N`, the tool wrote 25 suites and 1228 tests, which no commit
holds (`ls-files` counts an intent-to-add entry, `checkout-index` writes it empty); and with a
test file unmerged mid-rebase, `checkout-index` skipped it and the tool wrote a figure leaving
out the whole file, saying it was "counted as staged".

- Site derivation: every read of the index in the tool goes through `counted_tree` or
  `count_shell_suites`, and both call `refuse_unresolved_index` first; `unstaged_tests` is
  de-duplicated.
- Pin (unresolved-index clause): the intent-to-add and unmerged cases of `tests/py/test_count_gate_index.py` (landed by `b9c2ffd`; certify `done/20261021T062026Z-commissioned-certify-the-count-tool-and-gate-refuse-a`, which ran them red at `ce71167` and green at `5374ebe` and added no check of its own).

**Amendment, 26 Oct 2026 (the index-read clause, amending the unresolved-index clause): the tool
finds an intent-to-add entry by reading the index itself — what `ls-files` lists under tests/
and a tree written from the same index does not record — so an intent-to-add entry is refused
whatever the working tree holds for it, one a rename pairs with a deleted tracked file and one
whose working-tree copy is deleted included.**

*Instance that earned it:* the first-contact review of the units landed since round 4
(a private review record): the clause above found intent-to-add entries as
`git diff --diff-filter=A`, a working-tree view. `mv` a suite and `git add -N` the new name, and
`diff` reports a rename, the filter lists nothing, and the tool wrote 25 suites where both a
plain commit and `commit -a` hold 24; `git add -N` a new suite and `rm` it, and `diff` reports a
deletion, and `--check` said "stale" instead of refusing.

- Site derivation: `_intent_to_add` is the one read of intent-to-add, and
  `refuse_unresolved_index` its only caller (after unmerged entries, which `write-tree` refuses).
  `write-tree` runs on a copy of the index into a scratch object directory, so the engine's index
  and objects are untouched. The other working-tree `diff` of tests/, `unstaged_tests`, pins
  `--no-renames`, so a renamed file's old path is named as left out.
- Pin (index-read clause): `tests/py/test_count_tool_ita_index_read_certify.py` (landed by the commit that carries it; certify `stuck/20261026T015652Z-certify-certify-the-count-tool-refuses-an-intent`, red at `4d3b8fd` 4 of 7, green at `e07141c` 7 of 7).

**Amendment, 21 Oct 2026 (the summary-line clause, amending the index clause): the tool and the
gate read the collected count only from pytest's final summary line, matched whole — a node id
or any other line of the output that contains "N tests collected" never supplies the figure, and
output whose last line is not a summary is refused — and they refuse, naming it, a summary that
says tests were deselected (`S/N tests collected (D deselected)`), an error in that shape
included; and the figures the counts are held to are read from the same index as the counts, so
the gate and the tool's `--check` fail on a figure written and not staged, and name
ASSURANCE.md's unstaged copy.**

*Instance that earned it:* the round-2 review of 409b0c5 (a private review record, N1, N7): one tracked test parametrized with the id
`7 tests collected` made the tool write 7 and the gate pass while pytest's summary read 1223,
because the pattern took its first match anywhere in output that lists every node id above the
summary; and the deselected shape was read as its total with the error group missed. The index
clause above disclosed the other half: a figure the tool wrote and the author did not stage
passed the gate before the commit and was red at the landing.

- Site derivation: `read_collected` is the one reader of pytest's output (the gate reaches it
  through `collect_python_count`); it matches `COLLECTED` whole against the last non-empty line
  of stdout. `COLLECT_ARGV` passes `--color=no`, so a forced colour cannot make the summary
  unreadable. The gate's `_documented` reads `stated_doc` (the index's ASSURANCE.md, `git show
  :ASSURANCE.md`; the file itself outside a checkout), as does the tool's `--check`; the tool's
  write still edits the working-tree file, and says so when the result is unstaged. Class sweep
  (21 Oct 2026): no other reader of pytest's summary in `tools/`, `tests/` or `qq-promote`, which
  reads pytest's exit code only; `tests/run.sh` reads suite exit codes.
- Checks: `tests/py/test_sync_assurance_counts.py` (a fixture engine with the id
  `[7 tests collected]`; `read_collected` over output shapes, by subTest so no sample becomes an
  id; the deselected shape, unit and by a deselecting conftest; `--check` on an unstaged figure)
  and `tests/py/test_assurance_counts.py` `TestTheGateJudgesWhatACleanCheckoutCollects` (the
  same three on the gate).
- Pin (summary-line clause): `tests/py/test_count_summary_line_certify.py` (landed by `bee7f97`; certify `done/20261021T064715Z`).

## `an-idle-session-is-woken-once-per-stretch-of-new-work-and-never-by-its-own-checkpoint`

**`idle-autosave.sh` wakes a session (exit 2, message on stderr) only after
`QQ_IDLE_AUTOSAVE_DELAY` seconds of uninterrupted transcript quiet AND only when the session has
had genuine user activity since its last checkpoint — where a rewake delivery, slash-command
scaffolding and the session's own command-position `qq` write are never genuine activity — and a
newer arm of the same session stands the older one down by its stored PID alone.**

*Instance that earned it:* the hook's two measured failures in use (HEAD `session-idle-autosave`):
at a 3300 s delay Claude Code's idle compaction (54m19s–55m24s of quiet, 24 of 24 records,
2026-10-23) wrote the transcript first and reset the quiet clock, so the autosave lost the race 22
of 24 times; and a bare `/clear` injected scaffolding records that were counted as activity, so a
cleared session woke hourly only to decline.

- Site derivation: the fire is the single `exit 2` after the poll loop in `idle-autosave.sh`; the
  gate is `cur > base` over `actcount` (one boolean per user record); the rebaseline is
  `own_checkpoint_since`, which reads the session's own transcript through `quote-mask.py` (the
  bash-aware, length-preserving mask) and anchors `qq <write-verb>` to command position; supersede
  reads `timer-<sid>.pid` and verifies `/proc/<pid>/cmdline` before signalling. Class sweep: the
  hook signals no process by name or pattern anywhere.
- Checks: `tests/test-idle-autosave.sh` cases (a)–(h) (fixture transcript, DELAY=1/POLL=1; the
  clock-reset case runs DELAY=4); `tests/test-setup-wire.sh` pins that `--wire-claude` emits
  `async`/`asyncRewake`/`timeout`/`rewakeMessage` and is idempotent.

## `the-quote-mask-reads-heredoc-bodies-as-bash-reads-them`

**`quote-mask.py` masks a command the way bash lexes it, heredoc bodies included — a `qq` write-verb
inside a body never reaches command position in the mask and a quote inside a body never masks code
after the terminator — and a scan whose output is not its input's length raises `MaskMismatch`, the
CLI printing nothing for such a command so the caller reads no match and errs toward waking once
more rather than toward suppressing a wake.**

*Instance that earned it:* the 3a10cd1 port predated the estate mask's 2026-10-26 heredoc reader, so
a `cat > f <<'EOF'` body with a line starting `qq update` folded to `;qq update …`, matched the
command-position grep, and absorbed the session's genuine activity at the next Stop (and an
apostrophe in a body masked a real write after the `EOF`).

- Site derivation: the single scanner in `quote-mask.py` (`_scan` and `_bodies`), reached only from
  the CLI `main()` and its one grep consumer `own_checkpoint_since` in `idle-autosave.sh`; the
  length check is in `_run`, the refusal in `main()`.
- Check: none yet — certify pending.

## `switching-off-the-idle-autosave-stands-down-the-armed-waiter`

**A Stop run with `QQ_IDLE_AUTOSAVE_DISABLE=1` performs the stored-PID supersede for its session
(cmdline-verified, never by pattern) before exiting 0, so turning the autosave off mid-session never
leaves the waiter armed by the previous Stop to wake the session later.**

*Instance that earned it:* the disable exit was the hook's first act, ahead of the supersede block,
so the user RELEASE-NOTES tells to set `QQ_IDLE_AUTOSAVE_DISABLE=1` to turn it off was still woken
once more, up to ~50 minutes later, by the waiter armed before the config edit.

- Site derivation: the one path from sourcing `qq-config.sh` to the disable exit in
  `idle-autosave.sh`; the supersede is read-only on state, so a disabled hook still creates no
  state dir (pinned by `tests/test-idle-autosave.sh` (g), which this sentence does not re-pin).
- Check: none yet — certify pending.

## `an-armed-idle-waiter-stands-down-when-the-autosave-is-switched-off`

**An armed idle-autosave waiter re-reads `QQ_IDLE_AUTOSAVE_DISABLE` (environment over config file,
the file through `qq-config.sh`'s own parser) after every poll, so it never exits 2 once the key
it would resolve at that moment is `1`, whether or not another Stop runs in its session.**

*Instance that earned it:* eb06838 moved the supersede ahead of the disable exit, which stands an
armed waiter down only when a LATER Stop runs; the waiter's poll loop never read the key again, so
a user who set `QQ_IDLE_AUTOSAVE_DISABLE=1` in the config file while a session was already idle was
still woken once more, up to ~50 minutes later (review round 2, probe-disable-r2.sh P3: rc 2).

- Site derivation: every path in `idle-autosave.sh` from arming (`echo "$$" > "$pidf"`) to the
  `exit 2` passes through the poll loop's one `break`, so the loop body's check after each `sleep`
  is the one site; the start-of-hook disable exit uses the same `disabled_now`.
- Check: none yet — certify pending.

## `digest-order-is-the-same-on-every-host`

**What `qq` prints, and what a test of it concludes, does not depend on which coreutils or which
locale the host has: no ordering qq shows and no verdict a test reaches is left to a host
program's unspecified behaviour — `sort`'s tie order, how much `head` reads before it exits.**

*Instance that earned it:* the 7cc174e release went red on GitHub on 23 Oct 2026 when
`ubuntu-latest` became ubuntu-26.04, whose coreutils package is `9.5-1ubuntu2+0.0.0~ubuntu25`
— uutils, by inference from that version string; rust-coreutils 0.0.30 reproduces both here.
`_rank_by_epoch` shelled out to `sort -t$'\t' -k1,1nr` to copy the bash engine's tie order, which
is `sort`'s last-resort whole-line compare: GNU sort in en_US.UTF-8 lists `extra10` before
`extra1`, GNU sort in a C locale and uutils sort in any locale list it after. And
`test_menu_piped_into_a_closing_reader_dies_quietly_like_bash` piped into `head -1`: GNU head
exits with qq still blocked at 73 KB, uutils head reads all 112 KB first, so qq never meets a
closed pipe and exits 0.
The full suite run with uutils first on PATH found a third, latent one that CI's runner did not
show: `test-idle-autosave.sh` (d) made its bystander with `exec -a idle-autosave-bystander sleep`,
and multicall uutils picks its tool from argv[0], so that bystander exited at once and read as
killed by the hook.

- Site derivation: every `subprocess` call in `quintessence/` and `qq` (all are git, systemctl,
  ss, ssh or bash probes of the host itself, none a text filter qq's output passes through), and
  every external program a test's verdict passes through (found by running the whole suite with
  uutils first on PATH, not by a hand-list). Ties now break by slug in-process
  (`sorted(rows, key=lambda r: (-r[0], r[1]))`); the pipe test's reader takes one byte and exits,
  and the test asserts the menu outgrows the pipe (`F_GETPIPE_SZ`) rather than assuming it.
- Not covered, on purpose: `store._locale_sort_key` orders menu and list output by the process's
  own LC_COLLATE. That is deterministic for a given locale and is the documented behaviour, not an
  implementation's accident.
- Checks: `tests/py/test_cli.py` `TestDigestOrderIsTheSameOnEveryHost` (subtests for LC_ALL
  en_US.UTF-8, C.UTF-8 and C, and for a reversing `sort` first on PATH; red at the base under
  en_US.UTF-8 and the stub); `digest-over-max-quirk.golden` regenerated for slug order;
  `tests/py/test_dispatcher.py` `test_menu_piped_into_a_closing_reader_dies_quietly_like_bash`
  (red with the `except BrokenPipeError` handler mutated away, green 50 of 50 runs, green with
  uutils head first on PATH); `tests/test-idle-autosave.sh` (d) precondition, which asserts the
  bystander is alive with a cmdline naming idle-autosave before the kill check reads it (red with
  the renamed-`sleep` bystander under uutils, green under GNU and uutils). Whole-suite sweep:
  `tests/run.sh` with `/usr/lib/cargo/bin/coreutils` (rust-coreutils 0.0.30) first on PATH, under
  the en_US.UTF-8 that `run.sh` pins (the digest subtests cover the other locales). The one
  remaining uutils-only red, test-tsk `stop after lost race left no marker`, is a timing race in
  that test's start, not this class: the loser renames its `.run` into place before the live
  unit's bash has opened its own, so the live unit runs `true`; waiting for the live job's start
  line before racing turns it 0 of 5 red from 2 of 3.

## `the-shell-and-python-loaders-name-the-same-config-file`

**For every value of `HOME`, `QQ_CONFIG`, `XDG_CONFIG_HOME` and `QQ_REDACT_FILE`, every shell
function that builds a path of the config-file family names the same file the python `Config`
resolves for it, a leading `~` or `~/` expanded against `HOME` on both sides.**

*Instance that earned it:* 34d19e1 taught `Config._bootstrap_config_path` to expand a literal
`~` and said "the shell side expands it too", which was false. With `QQ_CONFIG='~/qq.conf'`
reaching hooks unexpanded (a settings.json `env` value, a systemd `Environment=` line), `qq config
set QQ_IDLE_AUTOSAVE_DISABLE 1` wrote `$HOME/qq.conf` and reported success while every bash entry
point read `./~/qq.conf` under its cwd, so the armed idle waiter still exited 2 (fix-review
qq-idle-autosave-2026-10 second leg, `tests/review-instruments/idle-autosave-switch-off-routes-probe.sh`
R5: rc 2 after 8 s; rc 0 after 1 s with the fix) and every other key in that file was ignored by bash.

- Site derivation: `derived_shell_sites()` in `tests/py/test_config_path_parity.py` — every
  function in a `git ls-files` shell file outside `tests/` (`*.sh` or an `sh`/`bash` shebang) whose
  non-comment lines expand `QQ_CONFIG`, `XDG_CONFIG_HOME` or `QQ_REDACT_FILE`; today
  `qq-config.sh:_qq_config_file` and `qq-redact.sh:_qq_redact_file`, both through `_qq_untilde`.
- Check: `tests/py/test_config_path_parity.py` (site set equals the parity table; 768-case matrix
  of bash vs `Config.config_path` / `get_path("QQ_REDACT_FILE")`; a `~/` QQ_CONFIG key reaches
  bash), red at ce958e0 (481 failed with both loaders reverted, 240 with `qq-redact.sh` alone).
  The matrix walks every family variable at `""` (pinned by
  `test_every_family_variable_is_walked_empty`), and
  `test_an_empty_qq_redact_file_switches_shell_withholding_off` asks `qq_redact_active` /
  `qq_slug_redacted` with a populated default list on disk; `tests/test-config-family-path-parity.sh`
  (the certifier's check, 42 cases) asks the same both ways. Red at 0f35e8c: 97 failed (96 matrix
  cells with `QQ_REDACT_FILE=""`, plus the empty-case behaviour subtest), and the shell check 41/1,
  because `_qq_redact_file` read `${QQ_REDACT_FILE:-…}` and named the default list where `Config`
  names no file. The fix reads `${QQ_REDACT_FILE-…}`; `QQ_CONFIG` and `XDG_CONFIG_HOME` keep `:-`
  because `Config` reads their empty value as unset too.
- Not covered, on purpose: `~user` forms, on which `config.py` is itself split
  (`_bootstrap_config_path` keeps them literal, `_expanduser` expands them); bash keeps them literal.
  Nor path keys other than these three that bash reads raw (e.g. `QUINTESSENCE_DIR` in `qq-lib.sh`)
  — SUPERSEDED: those are now held by `every-shell-site-of-a-path-key-names-the-path-config-resolves`.

## `every-shell-site-of-a-path-key-names-the-path-config-resolves`

**For every `path` or `csvpath` key in the `KEYS` registry, and for the XDG bases `config.py`
expands into a path default, every shell site outside `tests/` that reads the key names the path
`Config.get` resolves for the same environment and config file, a leading `~` or `~/` expanded
against `HOME` on both sides.**

*Instance that earned it:* the sibling of `the-shell-and-python-loaders-name-the-same-config-file`,
whose "Not covered" line named it. With `QUINTESSENCE_DIR=~/notes` in the config file (`QQ_ALLOW_RELOCATE=1
qq config set QUINTESSENCE_DIR '~/notes'` wrote it and returned 0) or in an unexpanded settings.json
`env` / systemd `Environment=` value, python used `$HOME/notes` while `qq-lib.sh`'s `QDIR`,
`memory-commit.sh`'s `QQ_MEMDIR` and every `QQ_STATE_DIR` reader named `./~/notes` under the hook's
cwd (fix-review qq-idle-autosave-2026-10 first-contact, `path-key-tilde-sibling-probe.sh`). The
repair is one normalisation, `_qq_untilde_path_keys` in `qq-config.sh`, over both layers; and
`prime-primary-sources.sh`, the one site that never sourced `qq-config.sh`, now does.

- Site derivation: `derived_sites()` in `tests/py/test_path_key_tilde_parity.py` — every non-comment
  line of a `git ls-files` shell file outside `tests/` (`*.sh` or an `sh`/`bash` shebang) that expands
  a key of `KEYS` with type `path`/`csvpath` or an XDG base matched by `resolve_raw("XDG_…")` in
  `quintessence/config.py`; the normalised key lists are compared with the same registry.
- Check: `tests/py/test_path_key_tilde_parity.py` — the key lists equal the registry; every key, at
  seven value shapes (five csv shapes for `csvpath`), in the env and the config-file layer, under two
  spellings of `HOME`, reaches bash as `Config.get` resolves it; every derived `${KEY:-…}`/`${KEY:=…}`
  site expression evaluated by bash equals `Config.get(KEY)`; and every site file sources
  `qq-config.sh` before its first site, or is sourced only by files that do. `setup.sh` is held to
  this like every other file. This bullet first exempted it, on the strength of its own `_untilde`
  loop; the second instance below is what that exemption let through, and e4a3aae removed it from
  the test. Red at d61c812: 490 failed of 4 tests and 824
  subtests. Mutations, each red alone: the normalisation call removed (488), `prime-primary-sources.sh`'s
  source removed (1), one key dropped from the list (21), the csvpath list emptied (33), the XDG bases
  dropped (53).
- Not covered, on purpose: `~user` forms (python defers them to the stdlib, which expands only an
  existing user; bash keeps them literal), and the EMPTY value, on which the engines split on
  defaulting rather than on `~` (`Config` returns `''` for an empty `QUINTESSENCE_DIR`; bash's `:-`
  takes `$HOME/quintessence`).

- *Second instance, from the layer this row's first check exempted:* `setup.sh` read
  `QUINTESSENCE_DIR` and `QQ_MEMDIR` from the environment alone, and the check above let it, on the
  strength of its own `_untilde` loop. A store moved with `qq config set QUINTESSENCE_DIR …` was then
  named `$HOME/quintessence` by a re-run of the installer, and `qq init` made a fresh empty store
  there (certify 20261101T185146Z, not held at 6ec8def3, 6 of 1173 red). `setup.sh` now sources
  `qq-config.sh` after taking its explicit-in-the-environment flags, so it is held to the same rule
  as every other site, and the exemption is gone.
- Site derivation (second check): `tests/test-shell-path-site-parity.sh`, step 1. The keys come from
  `KEYS` (type `path`/`csvpath`), the XDG bases from `re.findall(r"\bXDG_[A-Z_]+\b")` over
  `quintessence/config.py`, and the shell files from `git ls-files` outside `tests/` (`*.sh` or a
  bash/sh shebang). A site is any non-comment line that reads one of those names through a value
  expansion. A presence test (`${K+x}`) reads no value and is not a site, in this check and in
  `derived_sites()` above.
- Check (second): `tests/test-shell-path-site-parity.sh`, the certifier's own check, carried as it
  was written. It runs each site in its script's own context (loaders sourced, earlier assignments
  replayed, functions called) under `env -i` with a throwaway HOME, and compares it with
  `Config().get` over 51 environment/config-file cases. Red at 7aed6ca, with the `setup.sh` fix
  reverted: 6 failed of 1173, all of them `setup.sh:22`/`:23` with the key in the config file.
  Red at d61c812: 123 failed. Mutation of the fix alone (`setup.sh`'s `. "$HERE/qq-config.sh"`
  replaced by `:`): this check 6 failed, and
  `test_every_site_file_normalises_before_its_first_site` red on `setup.sh`.
- Check (third): `tests/test-certify-path-key-shell-sites.sh`, which runs
  `tests/review-instruments/certify-path-key-shell-sites.py`. That certify used to skip `setup.sh` in
  the config-file layer, on the ground that the installer defers to the file through `qq config get`.
  A scratch-HOME install at 7aed6ca refuted the ground, and the skip kept the certify green under the
  mutation above. The skip is gone. Under that mutation the certify now reports 10 disagreements, all
  at `setup.sh:29`/`:30` with the key in the config file; at the tip it reports 2178 agreements and 0
  disagreements.
- What the config file holds after an install. The row is about which path a site NAMES, and it
  does not promise that the file keeps the user's spelling. A file holding
  `QUINTESSENCE_DIR=~/my-store` comes out of `setup.sh` holding `$HOME/my-store` written out in full,
  plus a `QQ_MEMDIR=$HOME/.quintessence-memory` line. The writer is `qq init`, which `setup.sh` calls
  in step 2 (`quintessence/admin.py` `init`). It records `config.get_path("QUINTESSENCE_DIR")`,
  the resolved path, and records `QQ_MEMDIR` because `setup.sh` exports that default into its
  environment. `setup.sh`'s own `_cfg_pref` finds both keys set and writes nothing. Bare `qq init`
  rewrites a `~` value in the same way. Both spellings name the same directory under one `HOME`; a
  config file shared across homes loses the `~`.

## `a-tsk-unit-executes-the-runner-its-own-start-wrote`

**A unit `tsk run` starts executes the runner script that same start wrote, and the job's
`.run`/`.cmd` records describe that start, whatever any other start of the same name does: no
file a start writes before `systemd-run` has created its unit is one another start's unit reads
or another start's records hold.**

*Instance that earned it:* ledger D122, D271 and D272. A start that lost the deep duplicate-start
race (a manager mis-reporting the live unit as inactive) renamed its runner onto the shared
`$STATE/<name>.run` the live unit executes, inside the winner's fork-to-first-open gap, and wrote
`.cmd` over the winner's: the refused caller's command ran under the winner's name. Test 4k's
end-to-end marker went red only when the loser won the open (1/200 under the stub, more under
load), and the stress lane counted the substitution and left it out of its exit status.

- Site derivation: the files the unit reads are named by `tsk run`'s `systemd-run` argv (the
  runner, which writes `.log`/`.rc`/`.done` itself); the files a start writes before
  `systemd-run` are the redirect and `mv` targets above that call in `tsk`'s `run` branch —
  at this fix `$tmp` (`<name>:run.tmp.<pid>`), `$cmdtmp` (`<name>:cmd.tmp.<pid>`) and `$runner`
  (`<name>:runner.<pid>.<EPOCHREALTIME>`), all in the `:` namespace no job name can reach;
  `.cmd` and `.run` are written only after `systemd-run` returns.
- Check: `tests/test-tsk.sh` 4k — after the lied-manager race, the file the live unit's
  `ExecStart` names (read from `systemctl --user show`, not from tsk's own record) still holds
  the winner's command, `.cmd` still names it, the `.run` record's inode is unchanged, and the
  stop marker reads 143. Red at 736270e (3 of 76: the unit's runner and `.cmd` held the loser's
  `true`, the `.run` inode moved), deterministically, where the end-to-end marker had passed.
  Mutations, each red alone: the unique runner path reverted to `$STATE/<name>.run` with the
  `.run` record write removed (the unit's runner holds the loser's command; with the new
  `install_failed` cleanup the loser also removes the shared `.run`, 4 failed); `.cmd` written
  directly before `systemd-run` (1 failed). `tests/stress/stress-lied-manager-race.sh` now fails
  on a substitution (ceiling 0): 200 iterations at load average ~21 on 2026-11-02, 1 substitution
  (red) with main's `tsk` at 736270e and 0 with the fix.
- Not covered, on purpose: a start SIGKILLed after writing its runner and before `systemd-run`
  leaves that runner until the next successful start of the name (or `tsk clean`) removes it;
  no age sweep touches runners, since a live job's runner is as old as the job.

## `a-test-oracle-over-paths-does-not-read-the-scratch-directory-name`

**A test's oracle over paths must not depend on the ambient scratch directory's name: an
assertion about which fixture file a path names compares the part of the path the fixture
created (its basename, or a name relative to the fixture base), or a nonce name that cannot
occur in TMPDIR, never a fixed word searched for anywhere in the full path.**

*Instance that earned it:* ledger D1344. `test_long_lived_instance_sees_a_head_written_after_first_call`
asserted that `"late"` occurred in no full hit path, so it was red at b1130d8 under any TMPDIR
containing `late` (a worker scratch named `...later-round...`) and green under a neutral one.

- Site derivation: the assertions in `tests/py/` that apply `in` to a hit path or to a set of them,
  found with `grep -nE 'in (p|h\["path"\]) for' tests/py/*.py` at 61e15ec. That found six
  assertions: `test_search.py` (the `late` pair above, plus the two `secret-topic` assertions in
  `TestRedaction`) and `test_searchcompose.py` (`ptopic` and `u.md` in
  `test_results_carry_the_correct_store_label`, which a TMPDIR containing either word made
  vacuously green). Each now compares `os.path.basename` against the fixture's own file name.
  The redaction tests also use `_nonce_slug`, because the product's matcher
  (`SearchIndex._slug_redacted`) matches a redact entry as a substring of the whole path, so a
  fixed slug under a TMPDIR of the same name redacts every hit, the public fixture included.
- Check: running the two files under a TMPDIR whose name holds the word. Red at 61e15ec with
  `TMPDIR=<scratch>/scratch-later-round` (1 failed, the `late` test) and with
  `TMPDIR=<scratch>/secret-topic-x` (2 failed, both redaction tests); green after the change
  under those two, under `ptopic-u.md-x`, and under a neutral name (89 passed each time).
  Mutations, each red alone: the test writes `early.md` instead of `late.md` (1 failed, so the
  basename oracle still sees the file's absence); the redact file lists a slug other than the
  nonce (2 failed).
- Not covered, on purpose: no lint enforces the shape. The derivation is the grep above, re-run
  by hand. In production the redaction matcher's substring rule is applied to a HOME-relative
  path, and whether it should stay a substring rule is the open redaction-scope question in
  `_filter_redacted`'s docstring, not something this test change decides.

## `a-reader-that-closes-early-ends-every-entry-point-with-141-and-no-message`

**Every Python entry point in this tree ends through `quintessence.pipeexit.run`, so when the
reader of its stdout closes early it exits 141 with nothing on stderr, on Python 3.13 and 3.14
alike, whichever way its `main` ends — return, `sys.exit`, or an exception.**

*Instance that earned it:* CI on GitHub's ubuntu-26.04 runner (system Python 3.14), run
`41907305771` at the published 883712f: `test_menu_piped_into_a_closing_reader_dies_quietly_like_bash`
read exit 120, and `qq menu | <closing reader>` printed `Exception ignored while flushing
sys.stdout: BrokenPipeError`. A write the pipe cannot take whole returns once its tail fits stdout's
buffer, and 3.14 raised `io.DEFAULT_BUFFER_SIZE` from 8 KiB to 128 KiB: the menu's one ~112 KB
write returned, `main()` returned 0, and the EPIPE surfaced in the interpreter's shutdown flush,
outside every handler. On 3.13 the same write raised inside `main()`, where `qq`'s `except
BrokenPipeError` caught it, so the suite was green on the author's host — but 3.13 has the same
hole for a write a few hundred bytes past the pipe (66,000 bytes into a 64 KiB pipe returns on
both). `qq` was the only entry point that handled EPIPE at all, and none flushed inside a handler.

- Site derivation: every git-tracked file outside `tests/` whose module body holds an
  `if __name__ == "__main__":` block, parsed with `ast` (extensionless entry points included).
  At this change that is 17 files: the repo-root scripts and `tools/*.py`.
- Check: `tests/py/test_pipeexit.py`. `TestEveryEntryPointEndsThroughPipeexit` derives the entry
  points through `tests/py/_treefiles.py` and requires each `__main__` block to end in
  `pipeexit.run(...)` with only imports and `sys.path` calls before it (its control refuses
  `sys.exit(main())`, a bare `main()`, work before the call, an `else:` and a different `run`).
  `TestRunUnderAClosedReader` runs `pipeexit.run` under a reader that takes one byte, for a write
  just past the pipe (returns, `sys.exit(3)`, raises) and four pipes (one write, many prints):
  141 and empty stderr each time, and with a reader that drains everything the main's own exit
  (0, 3, 1 with its traceback). Run it under every supported Python; 3.13.5 (system) and 3.14.6
  (uv) both pass. Red at 7499986 in a plain copy on both Pythons: the derivation test names the
  entry points, and on 3.14 `test_dispatcher`'s menu test reads 120. Mutations, each red alone
  on both Pythons unless noted: the flush in `run`'s `finally` removed (the three near-pipe
  cases; on 3.14 also the menu test); the repoint to `/dev/null` removed (the near-pipe cases;
  on 3.14 also many-prints and the menu test); `qq`'s block back to `sys.exit(_main_guarded())`
  (the derivation test and the menu test).
- Not covered, on purpose: inline Python a shell script runs. `qq-redact.sh`'s `python3 -c` writes into a
  command substitution, which reads to EOF, so no reader closes early; `setup.sh`'s `python3 - <<'PY'`
  writes a few lines to the installer's own stdout, where bash takes the SIGPIPE first. And
  `refs-resolve.py`'s "never fail the caller" now means exit 0 except 141 under a closed reader;
  its one caller, `memory-commit.sh`, sends its stdout to /dev/null.

## `the-suite-derives-the-same-files-with-or-without-git`

**A test that derives its sites from the engine tree lists the tree through
`tests/py/_treefiles.engine_files`, which names the same files in a git checkout (`git ls-files`)
and in a plain copy such as an export or a tarball (a walk that leaves out `.git` and what the
top-level `.gitignore` names).**

*Instance that earned it:* the release gate of 2026-11-04 ran `tests/run.sh` in the gated export,
before its `git init`: `test_config_path_parity.py` and `test_path_key_tilde_parity.py` called
`git ls-files`, which exited 128, and `test-py.sh` failed with 3 errors on a tree that was correct.

- Site derivation: `grep -rn 'ls-files' tests/py/*.py` — at this change the two parity tests
  (now through `engine_files`), the count-gate tests (which build their own checkout fixtures and
  ask git about an index on purpose) and `test_refsresolve.py` (which runs git in a fixture repo).
  `tests/test-shell-path-site-parity.sh` already walks the tree when git lists nothing.
- Check: `tests/py/test_treefiles.py`. In a checkout the walk equals git's tracked-and-on-disk
  files plus its untracked-not-ignored ones; the listing holds `qq`, `qq-config.sh` and
  `quintessence/cli.py` and no `__pycache__` or `.git/` path; a fixture's `.gitignore` drops
  `*.log`, `*.pyc` and a non-`.pyc` file inside `__pycache__/`. Red at 7499986: the two parity
  tests in a plain copy (3 errors, `ls-files` exit 128). Mutation: the walk applies no
  directory pattern (the fixture test, red).
- Not covered, on purpose: `.gitignore` syntax beyond one fnmatch pattern per line, with a
  trailing `/` for directories (no negation, no anchored or nested-directory patterns), and
  nested `.gitignore` files; the checkout comparison fails if this repo's file outgrows that.
