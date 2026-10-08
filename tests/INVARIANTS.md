# Invariants — quintessence

One sentence per invariant, the rule that derives every site it binds, and the test that
quantifies over that derivation. A fix diff is judged against this register before it is
priced. Entries are keyed by **subject**; cite one by its slug.

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

- Site derivation: `cli.render_findings_next`'s generic branch (the only extraction site).
- Pin: `tests/py/test_trust_boundaries.py::TestReadyCommands`.

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
