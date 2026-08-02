# Installing quintessence

Two parts: (1) put the tools on `PATH` and create a store; (2) optionally wire it into your
agent harness so a fresh session is *taught* the write-path and resumes automatically.

## 1. Tools + store (works with any agent, or none)

```sh
# from a clone of this repo (call its path $QQ_HOME)
export QQ_HOME="$PWD"
# put the CLIs on PATH (symlink, so updates track the repo)
mkdir -p ~/.local/bin
ln -sf "$QQ_HOME/qq" ~/.local/bin/qq # one CLI; search/ask/write are qq verbs

# create a store and check the environment
export QUINTESSENCE_DIR=~/quintessence        # where HEADs live
export QQ_MEMDIR=~/.quintessence-memory        # optional: atomic-fact store (for the xref)
export QQ_STATE_DIR=~/.local/state/quintessence # runtime state (activity log, pending findings,
                                                #   refs) — independent of QUINTESSENCE_DIR; pin
                                                #   it too for a fully isolated/sandboxed install
qq init
qq doctor                                      # should be all-green for required deps
```

Semantic recall is optional. For it, run [Ollama](https://ollama.com) and
`ollama pull qwen3-embedding:0.6b` (override with `QQ_EMBED_MODEL` / `QQ_OLLAMA_URL`). Without
it the store works fully; `qq search` degrades to keyword matching and says so. `qq ask`'s
cited answers are a separate, additional dependency: a chat-capable completion endpoint, set
via `QQ_ASK_ENDPOINTS` (see CONFIG.md). Unset, `ask` returns raw retrieval with a hint.
`qq doctor` reports the state of both.

## 2. Claude Code integration (recommended)

Why wire it into the harness: a fresh model has no prior familiarity with qq, so the
operational contract is the only thing teaching it the write-path. Inject `CONTRACT.md` at
session start (small, always on); keep `ONBOARDING.md`/`RUBRIC.md` as on-demand depth.

### Option A – install as a plugin (one step)

This repo is a Claude Code plugin (`.claude-plugin/plugin.json` + `hooks/hooks.json` + two
skills: `/quintessence` and `/wrap`). `/wrap` is a session-end sweep (finalize HEADs, run the
consistency check, verify/commit the session's work) that pairs with `/quintessence`'s
write-as-you-work capture; `bash setup.sh --wire-claude` also symlinks both skills into
`~/.claude/skills` for Option B (manual wiring) installs. The plugin bundles six hooks so you
don't hand-edit `settings.json`: SessionStart contract injection, resume (UserPromptSubmit) /
prederive (PreToolUse) / finalize (Stop), a PreCompact nudge (capture a HEAD before lossy
context compaction), and a
SessionEnd `memory-commit` (commits the atomic-fact memory store if it changed – a local
commit only; pushing to an off-box mirror is a deployment-specific extension you add after
`memory-commit.sh`). Of the last two, `memory-commit.sh` is silent when unused (no memory
store, or nothing to commit); `precompact-nudge.sh` always emits its nudge, which is additive
rather than silent.

- **Marketplace** (this repo ships `.claude-plugin/marketplace.json`):
  `/plugin marketplace add <your-repo-url>` then `/plugin install quintessence@quintessence`.
- **Local, no marketplace:** drop the repo into a Claude Code skills directory
  (e.g. `~/.claude/skills/quintessence`); it loads next session as `quintessence@skills-dir`.

**Post-install (one step).** The plugin wires the hooks, but the CLIs still need to be on `PATH`
and you need a store. Run `bash setup.sh` from the repo – or just ask your agent to. It links
`qq` into `~/.local/bin`, runs `qq init`, writes a durable config file
(`~/.config/quintessence/config`, managed with `qq config`), runs `qq doctor` and the test
suite (`tests/run.sh`), and prints the `PATH` line only if `~/.local/bin` isn't already on it. It never edits your shell profile.
Override `BIN_DIR` / `QUINTESSENCE_DIR` / `QQ_MEMDIR` for non-defaults. The plugin does not set
the `Edit/Write` deny rule (Option B); the `qq init` git hooks hold the write-path either way.

> ⚠ Don't enable the plugin on a machine that already wires these hooks manually (Option B) – the
> hooks would fire twice. Pick one.

### Option B – manual `settings.json` wiring

Easiest: `bash setup.sh --wire-claude` does this idempotently. It repoints a drifted or old
qq hook, adds a missing one, and leaves every non-qq hook untouched, backing up `settings.json`
before any change. Re-running changes only what has drifted, and names each hook it changes; if
there is nothing to do it says so in one line. It
manages exactly the four qq hooks below; SessionStart is wired to `hooks/inject-contract.sh` (so the
[optional seams](#optional-local-seams-personalize-without-forking) apply). The optional
`PreCompact` (`precompact-nudge.sh`) and `SessionEnd` (`memory-commit.sh`) hooks are not
auto-wired here – add them by hand if you want them under Option B (the plugin bundles them), so
this never clobbers an existing PreCompact/SessionEnd hook of your own. Or merge by hand –
note the example below wires SessionStart to the minimal inline `jq` line; point it at
`<QQ_HOME>/hooks/inject-contract.sh` instead to get the [optional
seams](#optional-local-seams-personalize-without-forking):

```json
{
  "permissions": {
    "allow": ["Bash(qq:*)"],
    "deny":  ["Edit(<QUINTESSENCE_DIR>/**)", "Write(<QUINTESSENCE_DIR>/**)"]
  },
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command",
        "command": "jq -n --arg c \"$(cat <QQ_HOME>/CONTRACT.md)\" '{hookSpecificOutput:{hookEventName:\"SessionStart\",additionalContext:$c}}'" } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "<QQ_HOME>/resume-match.sh" } ] }
    ],
    "PreToolUse": [
      { "matcher": "Bash", "hooks": [ { "type": "command", "command": "<QQ_HOME>/prederive-recall.sh" } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "<QQ_HOME>/finalize-check.sh" } ] }
    ]
  }
}
```

- **`deny` Edit/Write on the store** is what forces writes through the `qq` write verbs – a
  second layer alongside the git hooks `qq init` installs.
- **`resume-match.sh`** surfaces a likely HEAD to resume when your prompt matches one.
- **`prederive-recall.sh`** nudges you to check for an existing runbook before reinventing one.
- **`finalize-check.sh`** fires on the Stop event, snapshots an unsaved HEAD, and refreshes
  the pending findings (`qq check --write`).

> Option A (the plugin) does this same wiring in one step. Keep `CONTRACT.md` in sync with the
> installed `qq` – it hard-codes the verb names; `qq doctor` flags a version mismatch.

## Optional local seams (personalize without forking)

Three optional seams let a deployment add local detail without modifying the shipped tool. Each
file or variable is read only if present, so the generic default is unchanged when it's absent:

- **Extra SessionStart context** – `hooks/inject-contract.sh` appends `$QQ_STATE_DIR/contract-extra.md`
  (override path: `QQ_CONTRACT_EXTRA`) after `CONTRACT.md`. Put a richer discipline / house-style
  block there. (Applies when SessionStart runs `inject-contract.sh` – plugin Option A, or point
  Option B's SessionStart at `<QQ_HOME>/hooks/inject-contract.sh` instead of the inline `jq`.)
- **Open-loops digest at SessionStart** – set `QQ_SESSIONSTART_DIGEST=1`; `inject-contract.sh` then
  also appends `qq digest` (the recency-ranked open loops).
- **Custom recall-prime** – the once-per-session line telling the session its recall corpus
  exists. `resume-match.sh` reads it from `$QQ_STATE_DIR/recall-prime.txt` (override:
  `QQ_RECALL_PRIME`), else uses its built-in default.

This is how a personalized install (richer prompt, local corpus framing) stays a thin overlay on the
generic package rather than a fork.

## Other harnesses

No hooks? Paste `CONTRACT.md` into your system prompt / project instructions. That alone gives a
model the write-path; the `resume` and `finalize` nudges just won't be automatic.
