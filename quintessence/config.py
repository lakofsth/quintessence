# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.config – the KEYS registry and the Config object.

Implements design ruling D3 ("Config is a schema'd object, not ambient environment") and
closes a known architecture debt ("the config layer is resolved three times and guarded zero
times") for the python side: ONE registry
(name, type, default, scope, description), ONE resolver (constructor overrides > environment
> the dotenv config file > the registry default), computed ONCE per Config instance – so a
long-lived process (a future MCP server) can re-resolve on demand by constructing a fresh
Config, and a one-off caller (a future `--transcripts`-style corpus switch) uses constructor
overrides instead of pre-import os.environ surgery.

The dotenv parser (_parse_dotenv) is BYTE-COMPATIBLE with the existing bash loader it must stay
interchangeable with while both engines are live during the migration (qq-config.sh's
`_qq_load_config`): same quote-stripping, same CRLF/whitespace trim, same malformed-key skip,
same "first occurrence in the file wins, environment always wins over the file" precedence – 
and, like that original, it is NOT restricted to registered QQ_* names (any valid-identifier
KEY=value line loads), so a deployment can carry incidental settings (e.g. XDG_STATE_HOME)
through the same file.

Defaults in KEYS are GENERIC: no deployment-specific ports, no site-specific name prefixes.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Union

HOME = os.path.expanduser("~")

# ---- bool parsing flavors --------------------------------------------------------------
# Three genuinely different conventions are alive in today's bash/python split (see each
# KeyDef's description for which script uses which). Preserved PER-KEY rather than
# homogenized: collapsing them to one convention would itself be a new silent divergence
# from current behaviour, which is exactly the debt class (C) this whole package exists to
# retire – so the parity test (tests/py/test_config_parity.py) checks each flavor literally.
BOOL_DEFAULT_TRUE_FALSY_ZERO = "bool_default_true_falsy_zero"  # unset/anything-but-"0" -> True
BOOL_TRUTHY_EXACT_ONE = "bool_truthy_exact_one"                # True iff value == "1"
BOOL_TRUTHY_NONEMPTY = "bool_truthy_nonempty"                  # True iff set to any non-empty string

_BOOL_TYPES = (BOOL_DEFAULT_TRUE_FALSY_ZERO, BOOL_TRUTHY_EXACT_ONE, BOOL_TRUTHY_NONEMPTY)

_BOOL_PARSERS: dict[str, Callable[[str], bool]] = {
    BOOL_DEFAULT_TRUE_FALSY_ZERO: lambda raw: raw != "0",
    BOOL_TRUTHY_EXACT_ONE: lambda raw: raw == "1",
    BOOL_TRUTHY_NONEMPTY: lambda raw: bool(raw),
}
# The "nothing configured anywhere" case for each flavor (mirrors each flavor's own unset
# behaviour: default-true unless "0"; default-false unless exactly "1"; default-false unless
# any non-empty value at all).
_BOOL_UNSET_DEFAULT: dict[str, bool] = {
    BOOL_DEFAULT_TRUE_FALSY_ZERO: True,
    BOOL_TRUTHY_EXACT_ONE: False,
    BOOL_TRUTHY_NONEMPTY: False,
}


DefaultValue = Union[Any, Callable[["Config"], Any]]


@dataclass(frozen=True)
class KeyDef:
    name: str
    # "str" | "int" | "float" | "path" | "csv" | "csvpath" | one of the BOOL_*
    #
    # `csvpath` is a csv whose MEMBERS ARE PATHS: each element gets the same `~`-expansion a
    # `path` key gets, because a tilde-written member is otherwise carried literally and quietly
    # matches nothing – an exemption list that reads as protection and does none
    # (2026-07-29 verification round, F5). The other csv keys stay plain `csv` on purpose:
    # QQ_ASK_ENDPOINTS / QQ_ASK_BUDGETS (URLs, `url=chars`), QQ_REMOTE_ALLOWED_HOSTS /
    # QQ_REMOTE_ALLOWED_ORIGINS (host and origin strings), QQ_PROC_PROBE_EXCLUDE (unit names),
    # QQ_BIND_CORPUS_EXCLUDE (`corpus:glob`), QQ_AUTHOR_GATE_SLUGS (slugs) and
    # QQ_XREF_GENERIC_TYPES (frontmatter type words) never hold a filesystem path; and the two
    # MAP-shaped ones, QQ_BIND_REPOS and QQ_BIND_CORPUS, hold `name=path` pairs whose ELEMENT
    # does not start with `~` – the path sits after the `=`, and their consumer (quintessence
    # .refs) expands it there, so element-level expansion would not help and splitting values
    # here would move that parsing away from the code that owns it.
    type: str
    default: DefaultValue  # a literal, or a callable(cfg) -> literal for cross-key-derived defaults
    scope: str
    description: str


def _home(cfg: "Config") -> str:
    """HOME resolved through the INSTANCE's own layers (overrides > env > file), falling back
    to the process home. Every HOME-anchored default derives through this, so a Config built
    with a synthetic env (e.g. a hermetic test or per-request server Config passing
    env={'HOME': ...}) resolves its store paths under THAT home, honoring the documented
    isolation contract – previously these defaults froze the real process home at import time
    (2026-07-29 review, engine-002).

    An EMPTY HOME means "unset", the same reading the XDG bases already take here and bash
    takes with `${HOME:-...}` – otherwise every HOME-anchored default lands at the filesystem
    ROOT (`~/mem` -> `/mem`, and a bare join -> a CWD-relative path), which is not what any
    caller means and is silently destructive (2026-07-29 verification round, F5)."""
    return cfg.resolve_raw("HOME", HOME) or HOME


def _expanduser(cfg: "Config", path: str) -> str:
    """`~`-expansion against the INSTANCE's own home (see _home), so a Config built with a
    synthetic env expands XDG bases under THAT home. `~user` forms fall through to the stdlib.

    Trailing slashes on the home are dropped before joining, so HOME=/synthetic/ yields
    /synthetic/mem rather than a doubled separator; `or "/"` mirrors the stdlib's own handling
    of a root home (both: 2026-07-29 verification round, F5)."""
    if path == "~" or path.startswith("~/"):
        return (_home(cfg).rstrip("/") + path[1:]) or "/"
    return os.path.expanduser(path)


def _state_dir_default(cfg: "Config") -> str:
    # `or`-style for the same reason as _config_base_default below: bash reads this as
    # `${XDG_STATE_HOME:-$HOME/.local/state}` (qq-lib.sh), so a present-but-empty value is
    # "unset" on both sides rather than a CWD-relative state dir on the python side.
    base = cfg.resolve_raw("XDG_STATE_HOME") or os.path.join(_home(cfg), ".local", "state")
    return os.path.join(_expanduser(cfg, base), "quintessence")


def _config_base_default(cfg: "Config") -> str:
    """Base dir for the config-file family (config, redact-slugs).

    `or`-style, NOT "is it present": an XDG_CONFIG_HOME that is present but EMPTY means unset,
    exactly as bash's `${XDG_CONFIG_HOME:-$HOME/.config}` reads it in qq-config.sh – and as
    Config._bootstrap_config_path already read it here. Before
    (2026-07-29 verification round, F3) an empty value resolved these paths CWD-RELATIVE while
    the shell side resolved them under $HOME, so the two engines looked for the same file in two
    different places. `~` is expanded for the same reason: the shell base expands it too."""
    base = cfg.resolve_raw("XDG_CONFIG_HOME") or os.path.join(_home(cfg), ".config")
    return _expanduser(cfg, base)


KEYS: list[KeyDef] = [
    # ---- L0 store locations -------------------------------------------------------------
    KeyDef("QUINTESSENCE_DIR", "path", lambda cfg: os.path.join(_home(cfg), "quintessence"), "store",
           "The store: a git-backed directory of `<topic>.md` files plus `journal/`."),
    KeyDef("QQ_MEMDIR", "path", lambda cfg: os.path.join(_home(cfg), ".quintessence-memory"), "store",
           "Memory-fact store, one small stable fact per file. Feeds slug resolution and the "
           "T2 staleness-xref."),
    KeyDef("QQ_STATE_DIR", "path", _state_dir_default, "store",
           "Runtime state: the activity log, the pending-findings queue, the xref and "
           "reconcile caches, wave-offs, and the state-dir lock. Kept outside the store and "
           "the engine directory so it survives a plugin update or a read-only mount. It is "
           "independent of QUINTESSENCE_DIR: pinning the store alone does not redirect this "
           "state, so an isolated or sandboxed run must pin QQ_STATE_DIR too, or `qq digest` "
           "and `qq check` read the default install's state."),
    KeyDef("QQ_CONFIG", "path", lambda cfg: os.path.join(_config_base_default(cfg), "quintessence", "config"),
           "bootstrap",
           "Path to the durable dotenv config file itself. Resolved from constructor "
           "overrides, the environment and XDG only – this key cannot be set from the file "
           "it names."),

    # ---- recall corpus / embedding cache -------------------------------------------------
    KeyDef("QQ_KB_ROOT", "path", lambda cfg: os.path.join(_home(cfg), "kb"), "recall",
           "Root of the recall corpus: a directory of symlinks, one per source repo or "
           "directory."),
    KeyDef("QQ_CORPUS_CONFIG", "path",
           lambda cfg: os.path.join(cfg.get_path("QQ_KB_ROOT"), ".qq-corpus.json"), "recall",
           "Optional JSON file overriding the corpus's source labels, whole-file sources and "
           "excludes."),
    KeyDef("QQ_CACHE", "path", lambda cfg: os.path.join(_home(cfg), ".cache", "qq-search", "embeddings.json"), "recall",
           "Embedding vector cache file, keyed by content hash. Scoping one cache per identity "
           "(corpus root, model, chunker version) happens in quintessence.search, not through "
           "this key."),
    KeyDef("QQ_EMBED_MODEL", "str", "qwen3-embedding:0.6b", "recall", "Ollama embedding model id."),
    KeyDef("QQ_OLLAMA_URL", "str", "http://localhost:11434", "recall",
           "Ollama endpoint for embeddings + completions."),
    KeyDef("QQ_KEEP_ALIVE", "str", "24h", "recall",
           "Ollama keep_alive duration string for the resident embedding model."),
    KeyDef("QQ_EMBED_NUM_GPU", "int", None, "recall",
           "Ollama `options.num_gpu` for the embedding call. Unset by default: the embed "
           "request's `options` stays exactly {'num_ctx': 2048}. Set it to push the resident "
           "embedding model onto GPU layers; on this model CPU and GPU vectors agree to a "
           "minimum cosine of 0.999392."),
    KeyDef("QQ_REAP_FORCE", BOOL_TRUTHY_EXACT_ONE, False, "recall",
           "Set to 1 to override the cache reap-guard, which otherwise refuses to drop more "
           "than 50% of the cache in one save."),
    KeyDef("QQ_SEARCH_MIN_SIM", "float", 0.35, "recall",
           "Cosine floor for search results: hits scoring below it are dropped before a "
           "caller sees them, so rock-bottom-similarity noise never reaches an agent as if "
           "it were a real match. Never applied to keyword-fallback (degraded) hits, whose "
           "score is a term-overlap fraction on a different scale. Set to 0 to disable "
           "filtering and return every scored hit. The ask path (QQ_ASK_MIN_SIM) applies its "
           "own, independent evidence floor and retrieves with this floor disabled."),
    KeyDef("QQ_CACHE_GC_DAYS", "int", 60, "recall",
           "Housekeeping for the embedding cache directory: a per-identity cache file (or its "
           ".orphan-ages.json sidecar) whose mtime is older than this many days is removed at "
           "the start of build_index. This clears identities left behind by one-off corpus "
           "roots – a --transcripts probe, an ad-hoc QQ_KB_ROOT override – that will never be "
           "revisited. Set 0 to disable the sweep. It runs at most once per SearchIndex "
           "instance and costs one listdir plus a stat over the cache directory. It never "
           "removes a *.lock file (deleting a held lock breaks mutual exclusion), a temp file "
           "from an atomic write in flight – meaning one spelled the way this package generates "
           "them, `<name>.tmp.<12 lowercase hex, at least one of them a letter>` (an "
           "8-or-more-character window until 2026-08-04, which put foreign "
           "`report.tmp.20260804`-shaped files on the one-hour deadline; the letter is required "
           "because twelve digits are also valid hex, so `date +%Y%m%d%H%M` sat inside the "
           "width – the cost is that roughly one temp in 281 has an all-decimal tail and waits "
           "for the sweep above instead) – the "
           "legacy non-identity-scoped QQ_CACHE path, or this instance's own identity files. "
           "A temp left behind by a hard kill IS reclaimed, once it is older than an hour, and "
           "again only that spelling. Any other `.tmp`-shaped name – a bare `<name>.tmp`, or "
           "`notes.tmp.md` – is treated as an ordinary file of yours: never reclaimed at the "
           "one-hour grace, because a directory sweep does not know the write targets and cannot "
           "tell that name from a file of your own, but swept at the age above like anything "
           "else in this directory. It used to be skipped outright, which exempted it from the "
           "age sweep permanently. The atomic writer reclaimed a bare `<name>.tmp` beside the "
           "file it was writing until 2026-08-04; that rule is gone, so nothing in qq deletes "
           "that spelling any more (see ASSURANCE.md for the one delete condition that remains, "
           "and `tools/reclaim_legacy_temps.py` for clearing litter a pre-atomicio install "
           "left)."),

    # ---- ask (retrieval-as-QA) -----------------------------------------------------------
    KeyDef("QQ_ASK_ENDPOINTS", "csv", [], "ask",
           "Completion endpoints for `qq ask`, comma-separated; the first healthy one answers. "
           "Empty by default, so with nothing configured `ask` falls back to plain retrieval "
           "plus a one-line hint rather than probing local ports that belong to something "
           "else. Set it to the endpoint serving your own model."),
    KeyDef("QQ_ASK_MIN_SIM", "float", 0.35, "ask",
           "Cosine floor for the evidence pack: if every embedder-ranked hit scores below it, "
           "`ask` answers 'nothing relevant in the store' and never calls a completion "
           "endpoint, rather than sending weak passages and trusting the prompt to disown "
           "them. The default sits just above the noise ceiling measured with "
           "qwen3-embedding:0.6b on a live store, where off-topic probe queries scored "
           "0.24-0.33 and genuinely relevant hits scored 0.45 and up even when weak – so it "
           "catches true no-match cases and leaves the weak-but-real ones to the "
           "grounded-or-refuse prompt. Never applied to keyword-fallback (`degraded`) hits, "
           "whose score is a term-overlap fraction on a different scale, not a cosine "
           "similarity. Raise it to refuse more readily, lower it to let thinner evidence "
           "through."),
    KeyDef("QQ_ASK_BUDGETS", "csv", [], "ask",
           "Per-endpoint evidence-pack size, as `url=chars` entries (e.g. "
           "'http://127.0.0.1:8095=8000'). Overrides the built-in per-endpoint table; an "
           "endpoint named nowhere gets 36000 chars. Set a small pack for a slow endpoint: on "
           "a 4B Q8 model at 8 CPU threads, prefill runs ~99 tok/s and decays to ~73 tok/s as "
           "context grows, so a 36k pack (~10k tokens) spends 114s in prefill alone and the "
           "completion timeout cancels it before it emits a token. Which model serves which "
           "port is a fact about your install, so it is configured here rather than "
           "hardcoded."),
    KeyDef("QQ_ASK_UNREDACTED", BOOL_TRUTHY_EXACT_ONE, False, "ask",
           "Set to 1 to lift QQ_REDACT_FILE filtering for one `qq ask` call, so a topic on "
           "that list is still answered from. Off by default."),
    KeyDef("QQ_SEARCH_UNREDACTED", BOOL_TRUTHY_EXACT_ONE, False, "ask/redaction",
           "The `search` counterpart of QQ_ASK_UNREDACTED: set to 1 to lift QQ_REDACT_FILE "
           "filtering for one retrieval call. The auto-recall hook (resume-match.sh) works out "
           "the session's reader profile itself before it calls `qq search`, sets this once it "
           "has established the reader has full access, and applies its own per-hit rule. Off "
           "by default, so a bare `qq search` or `search_continuity` filters the way `ask` "
           "does rather than being a hole in the same list."),
    KeyDef("QQ_SHOW_UNREDACTED", BOOL_TRUTHY_EXACT_ONE, False, "ask/redaction",
           "The `show`/`brief` counterpart of QQ_ASK_UNREDACTED: set to 1 to lift the "
           "withheld-topics refusal for an explicit `qq show` or `qq brief` call. On a "
           "session whose model does not match QQ_SAFE_MODEL_PREFIX, `show` and `brief` of a "
           "topic on the QQ_REDACT_FILE list refuse with zero content and name this override "
           "and the `--unredacted` verb flag. A full-access reader (QQ_SAFE_MODEL_PREFIX "
           "match) is unaffected. Off by default."),
    KeyDef("QQ_REDACT_FILE", "path",
           lambda cfg: os.path.join(_config_base_default(cfg), "quintessence", "redact-slugs"), "ask",
           "The withheld-topics list: newline-delimited slugs or paths left out of what "
           "arrives unasked (auto-recall, the SessionStart digest) and out of the retrieval "
           "verbs `qq ask` and `qq search` that feed those surfaces. On a limited reader "
           "session (model not matching QQ_SAFE_MODEL_PREFIX), an explicit `qq show` or "
           "`qq brief` of a listed topic also refuses, emitting zero content and naming the "
           "override (`--unredacted` flag or QQ_SHOW_UNREDACTED=1); a full-access reader "
           "is unaffected. This list protects the reader rather than "
           "confining the material, and is easy to mistake for the other thing: it keeps "
           "content a limited reader should not receive out of what arrives unasked, "
           "so it depends on who is reading and is liftable by design, per call through "
           "QQ_ASK_UNREDACTED, QQ_SEARCH_UNREDACTED and QQ_SHOW_UNREDACTED – which is how "
           "the auto-recall hook "
           "restores full recall once it has established the reader has full access. If what "
           "you want is 'this must never leave the box through the remote interface, whatever "
           "model is asking', that is QQ_REMOTE_DENY_FILE: a different list on a different "
           "surface, liftable by nothing. This list is also applied on the remote interface's "
           "standard profile, and lifted there by the wide-profile credential – the same "
           "reader-dependent rule, remotely. Empty or absent means the filtering is off."),
    KeyDef("QQ_REMOTE_DENY_FILE", "path",
           lambda cfg: os.path.join(_config_base_default(cfg), "quintessence", "remote-deny-slugs"),
           "remote",
           "The never-shared list for the remote interface (quintessence.remotepolicy): slugs "
           "or paths that must never leave the box into a hosted context, whatever model is "
           "asking. Separate from QQ_REDACT_FILE, which is about limited readers and is lifted "
           "by presenting the remote interface's wide-profile credential; this list is lifted "
           "by nothing. This is the list to use for 'must not leave the box' – the other one "
           "protects a limited reader and lifts for a full-access one, and operators reach "
           "for the wrong one because both read "
           "as 'hide this'. Consulted only by the remote interface: the local CLI and the "
           "stdio MCP server are unaffected. It fails closed – an absent or unreadable file "
           "denies everything and the remote server refuses to start, while an empty but "
           "present file is an explicit 'nothing is never-shared' and allows."),
    KeyDef("QQ_REMOTE_ALLOWED_HOSTS", "csv", "", "remote",
           "Extra Host header values accepted by the remote interface, beyond the built-in "
           "loopback set (127.0.0.1[:*], localhost[:*]) – e.g. the public vhost name when the "
           "reverse proxy preserves Host. Comma-separated; empty = loopback only."),
    KeyDef("QQ_REMOTE_ALLOWED_ORIGINS", "csv", "https://claude.ai", "remote",
           "Origin header values accepted by the remote interface, which is what stops a "
           "browser page on another site from reaching it by DNS rebinding. Comma-separated. "
           "The default allows Anthropic's hosted surface; add your public vhost origin if a "
           "browser client fronts it."),
    KeyDef("QQ_WRITE_TRUSTED_MODEL", "str", "", "ask/redaction",
           "A model id that carries write-trust while still reading on a limited reader "
           "profile. Only the write path reads this key: a session on this model may author on "
           "the topics QQ_AUTHOR_GATE_SLUGS protects, as a session matching "
           "QQ_SAFE_MODEL_PREFIX may. On the read path it changes nothing. What a session "
           "receives is decided by QQ_SAFE_MODEL_PREFIX, and the withholding is switched on by "
           "QQ_REDACT_FILE having entries, so a model named here is read exactly like an "
           "unrecognized one. Empty by default, so only QQ_SAFE_MODEL_PREFIX carries "
           "write-trust and every other session's writes on a protected topic are queued as "
           "proposals for a trusted session to ratify. Name a model here when it should author "
           "protected topics directly without waiting to be ratified."),
    KeyDef("QQ_SAFE_MODEL_PREFIX", "str", "claude-opus-", "ask/redaction",
           "Model-id prefix read as a full-access reader profile – full recall, nothing left "
           "out – once withholding is on, meaning QQ_REDACT_FILE is populated. Every other "
           "model id, including an empty or unrecognized one, fails closed to withheld. The "
           "same prefix also carries write-trust in the authoring gate: a session matching it "
           "may author on the topics QQ_AUTHOR_GATE_SLUGS protects. The default names a public "
           "Claude model tier; which tier counts as full-access is a policy choice, so change "
           "it if yours differs. On an install that never populates QQ_REDACT_FILE, and never "
           "lists a protected topic, the setting is inert either way."),

    # ---- L0 write-path (lock) -------------------------------------------------------------
    KeyDef("QQ_LOCK_WAIT", "int", 30, "write-lock",
           "Seconds to block acquiring the store write-lock before giving up."),

    # ---- HEAD size thresholds (shared by the soft write-time nudge and the hard check flag) --
    KeyDef("QQ_COMPACT_SOFT_BYTES", "int", 32768, "size-thresholds",
           "Update-line-region byte size that triggers the write path's advisory compaction nudge."),
    KeyDef("QQ_COMPACT_SOFT_LINES", "int", 12, "size-thresholds",
           "Update-line count that triggers the write path's advisory compaction nudge."),
    KeyDef("QQ_COMPACT_HARD_BYTES", "int", 49152, "size-thresholds",
           "Update-line-region byte size that `qq check` flags as a [T1 size] finding."),
    KeyDef("QQ_COMPACT_HARD_LINES", "int", 15, "size-thresholds",
           "Update-line count that `qq check` flags as a [T1 size] finding."),
    KeyDef("QQ_BODY_HARD_BYTES", "int", 49152, "size-thresholds",
           "Body (the `##` sections) byte size that `qq check` flags. Compacting cannot shrink "
           "a body; only a deliberate rewrite can."),

    # ---- reality binding ---------------------------------------------------------------
    KeyDef("QQ_BIND", BOOL_DEFAULT_TRUE_FALSY_ZERO, True, "binding",
           "Binding at write time: update, essence, rewrite and new extract file, commit, "
           "unit, port and remote-host path referents from the text being written, "
           "fingerprint them, warn loudly about a "
           "referent that does not exist (a note born stale, caught as it is written), and "
           "record the refs to $QQ_STATE_DIR/refs/refs.jsonl. Binding never blocks or alters a "
           "write. Set QQ_BIND=0 to turn binding off entirely."),
    KeyDef("QQ_BIND_REPOS", "csv", [], "binding",
           "Repo-alias map for commit-token binding: comma-separated alias=path entries (e.g. "
           "dist=~/quintessence-dist,infra=~/infra). A commit sha in prose binds when prefixed "
           "by one of these alias words ('dist 7ac40b3'), or as a bare 7-12-hex token when "
           "exactly one of these repos resolves it as a commit (one batched `git cat-file "
           "--batch-check` per unique repo per write; if none or several resolve it, nothing "
           "binds, so prose hex-lookalikes never bind by shape alone). Repo-relative paths "
           "('kernel/backup.py') go through the same test and bind as file refs when exactly "
           "one of these worktrees has the file; they are recorded under the absolute worktree "
           "path, so the commit and path-watch triggers cover them. Empty by default: an "
           "install binds no git refs until you name your own repos."),
    KeyDef("QQ_BIND_HOSTS", "csv", [], "binding",
           "Remote-host map for host:path binding: comma-separated host=ssh-destination "
           "entries (e.g. nas=user@nas). Prose like 'nas:/tank/ca-state' binds as an `rfile` "
           "ref when the host prefix matches a configured entry. Fingerprinting one referent "
           "takes one ssh invocation (BatchMode=yes, ConnectTimeout=5): a content sha256 for "
           "files up to the 64MiB hash cap, size and mtime above it, a listing hash for "
           "directories – the same algorithm the local file driver applies, run remotely "
           "(coarser mtime precision, and the remote stat follows symlinks; the two kinds of "
           "fingerprint are never compared to each other). An "
           "unreachable host or a timeout is recorded quietly as a fingerprint error, so a "
           "write is never blocked and never delayed past the timeout; a missing remote path "
           "gets the loud born-stale warning. Re-checking happens in the sweep only – the read "
           "side never runs ssh, it just marks the ref suspect. Empty by default: no remote "
           "binding until you name your hosts."),
    KeyDef("QQ_BIND_EXCLUDE_ROOTS", "csvpath", ["/run", "/proc", "/sys", "/tmp", "/dev"], "binding",
           "Top-level roots the file extractor and the trigger-side resolver "
           "(refs-resolve.py) ignore: comma-separated absolute directories. `~/...` members "
           "are expanded, as in any path key, since an unexpanded `~` would match nothing and "
           "silently switch the exclusion off. Paths under these roots hold process-runtime "
           "state: in prose they are nearly always examples, and several are busy enough that "
           "marking a ref suspect once never settles – the churn re-suspects it, and no "
           "adjudication can quiet the resulting stream of annotations. This applies to "
           "automatic extraction and to what the resolver will consider, not to an explicit "
           "`--ref file:/run/x`, which is a deliberate act and still binds. Set it empty "
           "(QQ_BIND_EXCLUDE_ROOTS=) to exclude nothing."),
    KeyDef("QQ_BIND_CORPUS", "csv", [], "binding",
           "Corpus repos whose *.md files are bound at commit time: comma-separated name=path "
           "entries (e.g. memory=~/memory,docs=~/docs). When `refs-resolve.py commit --repo "
           "<path>` fires for a configured corpus root, the commit's changed *.md files are "
           "rebound: previous records for each file are replaced by fresh extraction and "
           "fingerprinting, using the same drivers and statuses as topic binding. These "
           "records carry {corpus, file} rather than {head, line_ts}, so the read-side "
           "re-check skips them and the SessionStart digest is where they show up. MEMORY.md "
           "at a corpus root is skipped, being a derived index. `refs-resolve.py corpus-seed` "
           "backfills once. Empty by default: no corpus binding until you name your corpora."),
    KeyDef("QQ_BIND_CORPUS_EXCLUDE", "csv", [], "binding",
           "Corpus files that never bind: comma-separated corpus:glob entries (e.g. "
           "docs:backlog.md). Use it for documents that describe what is planned – a backlog "
           "names things that do not exist yet, so a born-stale warning there is simply wrong. "
           "An excluded file still takes part in the drop phase of a rebind, so its old "
           "records clear on the next commit or corpus-seed, but it binds nothing fresh. "
           "Which documents are aspirational is a judgement, so it is configured here rather "
           "than inferred."),
    KeyDef("QQ_PROC_PROBE_EXCLUDE", "csv", [], "binding",
           "Service units the proc-vs-disk probe skips: comma-separated unit names, fnmatch "
           "globs allowed (e.g. 'dev-*.service'). The probe raises a finding in the "
           "pending-findings PROC section for any running service whose ExecStart script, "
           "listed dependency or unit file has an mtime later than its "
           "ExecMainStartTimestamp – the running process predates a change on disk. A restart "
           "resolves the finding on the next probe run. Exclude units where that gap is "
           "expected, such as a service run from a live working tree."),

    # ---- authoring gate (write-side model trust – quintessence/authgate.py) -----------------
    KeyDef("QQ_AUTHOR_GATE", BOOL_DEFAULT_TRUE_FALSY_ZERO, True, "authoring-gate",
           "Write-trust for protected topics. Separate from what a session receives on the "
           "read side, which is QQ_REDACT_FILE's business. When this is on and "
           "QQ_AUTHOR_GATE_SLUGS matches the target topic, a write verb (update, new, "
           "rewrite, essence) from a session whose last-known model is not trusted to author "
           "protected topics alone does not land in the topic file: the proposed text is "
           "queued verbatim as a [PROPOSED write] entry in the pending-findings "
           "PROPOSED-WRITES section for a trusted session to ratify, and the command exits 0 "
           "with a one-line notice on stderr. Trusted means the session's model matches "
           "QQ_SAFE_MODEL_PREFIX or QQ_WRITE_TRUSTED_MODEL; an empty or undetectable model fails "
           "closed to not trusted. On by default with an empty slug list, so a fresh install "
           "queues nothing. Set QQ_AUTHOR_GATE=0 to turn the gate off entirely."),
    KeyDef("QQ_AUTHOR_GATE_SLUGS", "csv", [], "authoring-gate",
           "The topic slugs the authoring gate protects: comma-separated, with a trailing '*' "
           "meaning prefix match – the same matching the withheld-topics list uses. Matched "
           "against the write target's basename with '.md' stripped; otherwise the match is "
           "exact, never a substring. Empty by default, which leaves the gate inert."),
    KeyDef("QQ_MODEL_TRANSCRIPT", "path", "", "authoring-gate",
           "Explicit transcript path for the authoring gate's model-identity probe, which "
           "reads the last `message.model` line – the same last-known-model signal, and the "
           "same caveat, as qq-redact.sh's qq_model_mode. Empty by default, which derives the "
           "session transcript from $CLAUDE_CODE_SESSION_ID under "
           "~/.claude/projects/*/<id>.jsonl; if nothing resolves, the model is unknown and the "
           "session is not trusted. Mostly a seam for tests and integration."),

    # ---- contract version -----------------------------------------------------------------
    KeyDef("QQ_CONTRACT_VERSION", "int", 2, "internal-constant",
           "The version marker ('qq-contract vN') the installed CONTRACT.md must carry. Not "
           "overridable from the environment on the bash side, where qq-lib.sh assigns "
           "`QQ_CONTRACT_VERSION=2` outright rather than `${VAR:-2}`. Registered here so a "
           "check of CONTRACT.md has one source of truth to compare against."),

    # ---- Tier-1 deterministic check toggles/budgets ---------------------------------------
    KeyDef("QQ_ESSENCE_LAG", BOOL_DEFAULT_TRUE_FALSY_ZERO, True, "checks",
           "Turn the [T1 essence-lag] check on or off. Only the exact value '0' turns it off, "
           "matching the bash `!= 0` test; any other value, including unset, leaves it on."),
    KeyDef("QQ_MEM_HEALTH", BOOL_DEFAULT_TRUE_FALSY_ZERO, True, "checks",
           "Turn the [T1 mem-health] structural checks on MEMORY.md on or off: size, line "
           "budget, duplicate entries."),
    KeyDef("QQ_MEMINDEX_MAX_BYTES", "int", 22000, "checks",
           "MEMORY.md byte size that triggers the warning that it is nearing the cap where it "
           "is silently truncated."),
    KeyDef("QQ_MEMLINE_MAX_CHARS", "int", 240, "checks",
           "Per-entry MEMORY.md index line character budget."),

    # ---- T2 staleness-xref -----------------------------------------------------------------
    KeyDef("QQ_XREF_DISABLE", BOOL_TRUTHY_NONEMPTY, False, "xref",
           "Skip the T2 staleness-xref, the slow part of `qq check`. Any non-empty value skips "
           "it, matching the bash `${VAR:+1}` test rather than just '1'; `qq check --fast` and "
           "`--no-xref` do the same for one run."),
    KeyDef("QQ_XREF_SIM", "float", 0.55, "xref",
           "Minimum cosine similarity for a memory-fact/topic pair to be a candidate."),
    KeyDef("QQ_XREF_DAYS", "float", 3, "xref",
           "How many days newer a topic's content must be than a memory fact's mtime before "
           "the pair fires."),
    KeyDef("QQ_XREF_MAX", "int", 5, "xref",
           "Most [T2 stale?] findings queued per run, newest content move first."),
    KeyDef("QQ_XREF_GENERIC_TYPES", "csv", ["feedback"], "xref",
           "Memory-fact frontmatter `type:` values read as general working-style guidance and "
           "skipped by the xref, unless a fact overrides it with `xref: track` or "
           "`xref: skip`."),
    KeyDef("QQ_XREF_CONTENT", "path",
           lambda cfg: os.path.join(cfg.get_path("QQ_STATE_DIR"), "xref-content"), "xref",
           "Runtime state: the content-hash fingerprint per topic, which anchors when that "
           "topic's content last moved."),
    KeyDef("QQ_XREF_WAVEOFFS", "path",
           lambda cfg: os.path.join(cfg.get_path("QQ_STATE_DIR"), "xref-waveoffs"), "xref",
           "Runtime state: wave-off adjudications, which stay quiet until the topic's content "
           "changes again."),

    # ---- config-drift reconcile (deployment opt-in) ----------------------------------------
    KeyDef("QQ_RECONCILE_REGISTRY", "path", "", "reconcile",
           "INI file listing the settings reconcile tracks. Unset or absent makes reconcile a "
           "silent no-op, so an install that does not use it is unaffected."),
    KeyDef("QQ_RECONCILE_SNAPSHOT", "path",
           lambda cfg: os.path.join(cfg.get_path("QQ_STATE_DIR"), "reconcile-snapshot.json"), "reconcile",
           "Runtime state: the last-seen value in force for each tracked setting."),
    KeyDef("QQ_DOCDIR", "path", lambda cfg: os.path.join(_home(cfg), "docs"), "reconcile",
           "Directory of prose docs scanned for present-tense claims that no longer match the "
           "settings in force. Set it empty to skip doc scanning."),

    # ---- SessionStart / hook seams ----------------------------------------------------------
    KeyDef("QQ_CONTRACT_EXTRA", "path",
           lambda cfg: os.path.join(cfg.get_path("QQ_STATE_DIR"), "contract-extra.md"), "hooks",
           "Optional local text appended verbatim after CONTRACT.md at SessionStart."),
    KeyDef("QQ_SESSIONSTART_DIGEST", BOOL_TRUTHY_EXACT_ONE, False, "hooks",
           "Set to 1 to append the open-threads digest to what the SessionStart hook injects. "
           "Off by default."),
    KeyDef("QQ_RECALL_PRIME", "path",
           lambda cfg: os.path.join(cfg.get_path("QQ_STATE_DIR"), "recall-prime.txt"), "hooks",
           "Optional replacement for the once-per-session note that tells a session the store "
           "is there and how to search it."),
    KeyDef("QQ_DIGEST_PIN", "str", "", "digest",
           "A topic slug – a roadmap or portfolio topic, say – shown first in `qq digest` "
           "whatever else ranks."),

    # ---- skill/deployment extension keys ----------------------------------------------------
    # No code-level default or consumer IN THIS ENGINE – read via `qq config get` by the named
    # skill. Registered so `qq config set` doesn't warn them as unknown (they are legitimate,
    # just deployment-defined rather than engine-defined).
    KeyDef("QQ_QUINTESSENCE_EXTRA", "path", "", "skill-extension",
           "Path to your install's local conventions, read by the /quintessence skill."),
    KeyDef("QQ_WRAP_DOCS", "path", "", "skill-extension",
           "Your docs tree, read by the /wrap skill when it suggests which docs to backfill."),
    KeyDef("QQ_WRAP_REPOS", "str", "", "skill-extension",
           "The repo set the /wrap skill sweeps for uncommitted work: a colon-separated, "
           "PATH-style list of repo paths, kept a plain string because the skill splits on "
           "':'. Empty means the skill checks only the repos touched this session."),
    KeyDef("QQ_WRAP_EXTRA", "path", "", "skill-extension",
           "Extra end-of-session steps for the /wrap skill, local to your install."),
]

KEYS_BY_NAME: dict[str, KeyDef] = {k.name: k for k in KEYS}


class ConfigError(Exception):
    pass


@dataclass
class ValidationResult:
    ok: bool
    message: Optional[str] = None


_TEMP_ROOTS = ("/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp")


def _is_temp_path(path: str) -> bool:
    """True if `path` resolves under a temp-directory-class root: tempfile.gettempdir(),
    $TMPDIR, or the common hardcoded /tmp-class roots (belt-and-braces: gettempdir() already
    consults TMPDIR/TEMP/TMP, but a config value can name a DIFFERENT temp root than the
    process's own env, e.g. a stray /tmp path on a host where $TMPDIR points elsewhere)."""
    p = os.path.realpath(os.path.expanduser(path))
    roots = {os.path.realpath(tempfile.gettempdir())}
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        roots.add(os.path.realpath(tmpdir))
    roots.update(_TEMP_ROOTS)
    for root in roots:
        root = root.rstrip("/")
        if p == root or p.startswith(root + "/"):
            return True
    return False


_GUARDED_PATH_KEYS = ("QUINTESSENCE_DIR", "QQ_MEMDIR")


class Config:
    """Resolves QQ_* settings ONCE per instance: constructor overrides > environment > the
    dotenv config file > the KEYS registry default. Construct a fresh Config to
    re-resolve (e.g. a long-lived server noticing a config change, or a one-off caller
    switching corpus root via `overrides` instead of mutating os.environ pre-import)."""

    def __init__(self,
                 env: Optional[Mapping[str, str]] = None,
                 config_file: Optional[str] = None,
                 overrides: Optional[Mapping[str, Any]] = None):
        self._env: Mapping[str, str] = dict(env) if env is not None else dict(os.environ)
        self._overrides: dict[str, Any] = dict(overrides) if overrides else {}
        # QQ_CONFIG resolves from overrides/env/XDG only – never from the file it names.
        self._config_path = config_file if config_file is not None else self._bootstrap_config_path()
        self._file_values: dict[str, str] = self._parse_dotenv(self._config_path)
        self._cache: dict[str, Any] = {}

    # ---- bootstrap: locate the config file (mirrors _qq_config_file / qqsearch_core) --------
    def _bootstrap_config_path(self) -> str:
        home = str(self._overrides.get("HOME") or self._env.get("HOME") or HOME)

        def _tilde(p: str) -> str:
            # A literal `~` – a quoted export, a systemd unit – would otherwise be read as a
            # directory name and the config file silently not found, while every downstream
            # path expanded it and resolved elsewhere. The shell side expands it too.
            if p == "~":
                return home
            return os.path.join(home, p[2:]) if p.startswith("~/") else p

        if self._overrides.get("QQ_CONFIG"):
            return _tilde(str(self._overrides["QQ_CONFIG"]))
        if self._env.get("QQ_CONFIG"):
            return _tilde(self._env["QQ_CONFIG"])
        xdg = _tilde(self._env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"))
        return os.path.join(xdg, "quintessence", "config")

    @property
    def config_path(self) -> str:
        return self._config_path

    # ---- P8 recall composition: derive a sibling Config for a project-store SearchIndex -------
    def with_overrides(self, **overrides: Any) -> "Config":
        """A fresh Config sharing this instance's env/config-file layers, with `overrides`
        layered on top at the SAME (highest) precedence any other constructor override has.
        Used by the multi-store recall composition (quintessence.searchcompose) to build a
        project-store SearchIndex's Config from the same resolved env view – os.environ is
        not re-read; the config FILE at the same path is parsed again by the constructor –
        differing only in the keys named here (e.g. QQ_KB_ROOT pointed at a project store's
        own qdir)."""
        merged = dict(self._overrides)
        merged.update(overrides)
        return Config(env=self._env, config_file=self._config_path, overrides=merged)

    # ---- dotenv parser: byte-compatible with qq-config.sh + qqsearch_core._load_config_file ---
    @staticmethod
    def _parse_dotenv(path: str) -> dict[str, str]:
        """Line-by-line dotenv parse, NEVER exec'd. Mirrors both existing parsers exactly:
          - blank lines and '#'-comment lines are skipped
          - a line without '=' is skipped
          - key/value are trimmed of surrounding whitespace (this also drops a trailing \\r
            from a CRLF file, since \\r is whitespace to str.strip())
          - a malformed key (not a valid identifier) is skipped, never mangled
          - one layer of surrounding matching quotes (' or ") is stripped from the value
          - the FIRST occurrence of a given key in the file wins (matches bash's
            `[ -z "${!key+x}" ] && export` and python's `os.environ.setdefault`)
        """
        out: dict[str, str] = {}
        try:
            with open(path, newline="") as f:
                raw = f.read()
        except OSError:
            return out
        for line in raw.split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if not k.isidentifier():
                continue
            out.setdefault(k, v)
        return out

    # ---- provenance of a resolved value (which layer supplied it) ---------------------------
    def raw_source(self, name: str) -> str:
        """Which resolution layer currently supplies `name`: 'override' | 'env' | 'file' |
        'default'. Lets a caller distinguish a DELIBERATE one-off pin (override/env) from the
        durable config-file value or the built-in default – needed by the multi-store resolver
        (storepath): an env/CLI `QUINTESSENCE_DIR` pins to a single store, but the value `qq init`
        records in the config FILE is merely the user-store location and must NOT disable
        per-project discovery."""
        if name in self._overrides and self._overrides[name] is not None:
            return "override"
        if name in self._env:
            return "env"
        if name in self._file_values:
            return "file"
        return "default"

    # ---- generic (unregistered-name-safe) raw resolution ------------------------------------
    def resolve_raw(self, name: str, builtin_default: Optional[str] = None) -> Optional[str]:
        """env/file/overrides resolution for ANY name (registered or not) – used internally for
        cross-key derived defaults (e.g. QQ_STATE_DIR's default reads XDG_STATE_HOME this way,
        matching both bash's `${XDG_STATE_HOME:-...}` and the python core's equivalent, neither
        of which route XDG_* through the QQ_* registry either)."""
        if name in self._overrides and self._overrides[name] is not None:
            return str(self._overrides[name])
        if name in self._env:
            return self._env[name]
        if name in self._file_values:
            return self._file_values[name]
        return builtin_default

    def _coerce(self, type_: str, raw: str) -> Any:
        """Instance method, not a static one, because `path` must expand `~` against THIS
        Config's home (_expanduser -> _home), not the process home. It was static until the
        2026-07-29 verification round (F3): a Config built with env={'HOME': '/synthetic'}
        then resolved its HOME-derived DEFAULTS under /synthetic while a `~/...` value coming
        from that same env or the config file expanded under the REAL home – one instance,
        two homes, contradicting the isolation contract _home's docstring states."""
        if type_ == "str":
            return raw
        if type_ == "path":
            return _expanduser(self, raw)
        if type_ == "int":
            return int(raw)
        if type_ == "float":
            return float(raw)
        if type_ == "csv":
            return [x.strip() for x in raw.split(",") if x.strip()]
        if type_ == "csvpath":
            return [_expanduser(self, x.strip()) for x in raw.split(",") if x.strip()]
        if type_ in _BOOL_PARSERS:
            return _BOOL_PARSERS[type_](raw)
        raise ConfigError(f"unknown KeyDef type {type_!r}")

    def get(self, name: str) -> Any:
        """Typed accessor for a REGISTERED key: overrides > env > file > registry default,
        coerced per KeyDef.type. Raises KeyError for a name not in KEYS (use resolve_raw for
        an arbitrary/unregistered name)."""
        if name in self._cache:
            return self._cache[name]
        kd = KEYS_BY_NAME.get(name)
        if kd is None:
            raise KeyError(f"{name!r} is not a registered quintessence config key")
        raw = self.resolve_raw(name)
        if raw is None:
            default = kd.default(self) if callable(kd.default) else kd.default
            if kd.type in _BOOL_TYPES and default is None:
                # "nothing configured anywhere" for this bool flavor (not a string to parse).
                value: Any = _BOOL_UNSET_DEFAULT[kd.type]
            elif isinstance(default, str):
                # A registered default WRITTEN AS A STRING is in the same shape as a value
                # arriving from env or the config file, so it goes through the same _coerce
                # those take. Before (2026-07-29 verification round, F1) only `path` was
                # handled here, so a csv default given as a string came back as the bare
                # string – QQ_REMOTE_ALLOWED_ORIGINS returned "https://claude.ai" instead of
                # ["https://claude.ai"], and a caller iterating it got one character per item.
                # `path` keeps its ~-expansion (that is what _coerce does for path).
                value = self._coerce(kd.type, default)
            elif kd.type == "csvpath" and isinstance(default, list):
                # A csvpath default written as a list literal is already the target SHAPE but
                # not the target VALUE: a `~` in one of its entries would reach a caller
                # unexpanded and quietly match nothing, while the same default written as a
                # string goes through _coerce and expands. Expand element-wise so the two
                # spellings of a default mean the same thing. QQ_BIND_EXCLUDE_ROOTS reaches this
                # branch today and passes through unchanged, since its entries are absolute; the
                # expansion only does work once a default carries a `~`.
                value = [_expanduser(self, str(p)) for p in default]
            else:
                # Already the target type – int 30, float 0.55, list literals, True/False,
                # or an int key's None ("unset" is meaningful there). Left untouched.
                value = default
        else:
            value = self._coerce(kd.type, raw)
        self._cache[name] = value
        return value

    # ---- ergonomic typed wrappers (thin; `get` is the source of truth) ----------------------
    def get_str(self, name: str) -> str:
        return str(self.get(name))

    def get_path(self, name: str) -> str:
        return self.get(name)

    def get_int(self, name: str) -> int:
        return self.get(name)

    def get_float(self, name: str) -> float:
        return self.get(name)

    def get_bool(self, name: str) -> bool:
        return self.get(name)

    def get_list(self, name: str) -> list:
        return self.get(name)

    # ---- D3: `qq config set` validation guard -------------------------------------------------
    def validate_set(self, key: str, value: str, force: bool = False) -> ValidationResult:
        """Unknown key -> warn (ok=True, a message to surface, but the set proceeds – an install
        may legitimately carry a skill-defined key not yet in KEYS). QUINTESSENCE_DIR/QQ_MEMDIR
        pointed at a temp-directory-class path -> REFUSE (ok=False) unless force=True – this is
        the config-leak class from the 2026-07 incident (an agent's `qq config set` pointed the
        live store at a scratch dir for ~1h)."""
        kd = KEYS_BY_NAME.get(key)
        if kd is None:
            return ValidationResult(True, f"unknown config key '{key}' (not in the KEYS "
                                           f"registry) – set anyway, but check for a typo")
        if key in _GUARDED_PATH_KEYS and not force and _is_temp_path(value):
            return ValidationResult(False,
                f"refusing to set {key}={value!r} – resolves under a temp-directory root "
                f"(the config-leak class: a scratch path pointed the live store away, "
                f"possibly permanently). Pass force=True to override if this is intended.")
        return ValidationResult(True, None)

    # ---- introspection (feeds a future generated docs table / `qq doctor`) -------------------
    def describe(self) -> list[dict]:
        rows = []
        for kd in KEYS:
            try:
                resolved = self.get(kd.name)
            except Exception:
                resolved = None
            rows.append({
                "name": kd.name, "type": kd.type, "scope": kd.scope,
                "description": kd.description, "default": kd.default if not callable(kd.default) else "<derived>",
                "resolved": resolved,
            })
        return rows
