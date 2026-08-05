# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.refs — B1 write-time reality binding.

Claims about reality (files, commits, systemd units) decay silently; this module binds them to
their referents AT WRITE TIME: extract candidate referents from the text a write verb is about
to commit, fingerprint each, warn LOUDLY on a referent that does not exist (born-stale caught
at the keyboard, not by a nightly audit), and persist one REF record per (write, referent) to
`$QQ_STATE_DIR/refs/refs.jsonl` under the A1 `state_lock`. Read-side annotation (B2) lives in
quintessence.refsview, the trigger resolver (B3) in quintessence.refsresolve, the proc probe
(B4) in quintessence.procprobe — this module stays write path (+ the shared record shape and
fingerprint drivers those siblings import).

INVARIANTS (enforced by construction + tests/py/test_refs.py, tests/test-bind.sh):
  - FAIL-SOFT: `bind_write` never raises — any exception in extraction/fingerprinting/
    persisting degrades to exactly today's behavior. The ONE loud stderr surface is referent
    ABSENCE ("claim may be born stale"); a fingerprint DRIVER error gets a quiet one-liner at
    most. Binding can never block, corrupt, or fail a write.
  - REFS ARE DEPLOYMENT-LOCAL: everything lands under $QQ_STATE_DIR/refs/ — never inside the
    mirrored store, no store-format change (reversal path = an explicit
    store-sidecar contract change post-publish).
  - `QQ_BIND=0` disables binding ENTIRELY: byte-identical stdout/stderr/state to a build
    without this module (the warn-first rollout's escape hatch).
  - CONSERVATIVE extraction: a non-match is fine. Only `~`/`/`-rooted paths (whose top-level
    root actually exists — filters URL-path prose like "/api/prometheus"), repo-relative
    paths (RESOLUTION-GATED — see _RELPATH_RE), commit tokens (alias-gated, OR bare
    7-12-hex tokens RESOLUTION-GATED — see _BARE_SHA_RE), bare *.service/*.timer
    names, listener ports (":11435" / loopback host:port — see _PORT_BARE_RE), and
    remote-host paths ("nas:/tank/x", host GATED on a QQ_BIND_HOSTS entry — see
    _RHOST_RE) bind automatically; anything else needs an explicit `--ref kind:id`.
  - RESOLUTION-GATING: where a prose-shape guess would be too
    collision-prone (bare hex: "ed25519", uuid prefixes, dates; relative paths: "and/or.x"),
    extraction is deliberately PERMISSIVE and the bind is gated on the token actually
    RESOLVING against known reality (exactly one QQ_BIND_REPOS repo knows it as a commit /
    has the file in its worktree); zero or several repos = silent skip. Write budget:
    resolution is O(unique repos) subprocesses per write (ONE batched
    `git cat-file --batch-check` per repo, all tokens fed at once; D2 path checks are
    stat-only), never O(tokens × repos).
  - EXCLUDE-ROOTS: rooted paths under QQ_BIND_EXCLUDE_ROOTS (default
    /run /proc /sys /tmp /dev — process-runtime state, near-always prose EXAMPLES, several
    of them hot dirs that defeat suspect-once) are never auto-extracted, and the B3 resolver
    never treats records under them as candidates. Explicit `--ref file:...` is exempt — an
    explicit ref is a deliberate act, exclusion is extraction hygiene.
  - `retired` STATUS: a TERMINAL record status adjudication can set (direct
    state-dir edit of refs.jsonl is the sanctioned mechanism — refs are deployment-local,
    not store content). The resolver never transitions a retired record and the read side
    never annotates one, both by construction (neither path handles the status) and both
    pinned by test. This is the off-switch for a record whose referent is real but useless
    to track (hot dir: waive -> re-fingerprint -> churn -> re-suspect forever).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .atomicio import atomic_write_lines
from .store import Store, state_lock

# Kinds a write-time driver exists for. `proc` is
# the sweep's process-probe kind — no write-time driver by design (its fingerprint is a
# running-process property), so it is not accepted here; the sweep owns it.
VALID_KINDS = ("file", "git", "unit", "probe", "port", "rfile")

MAX_REFS = 20                       # per write — bounds fingerprint cost on a pathological line
_HASH_CAP_BYTES = 64 * 1024 * 1024  # above this, fp = stat:<size>:<mtime_ns> (perf budget:
                                    # <100ms typical write; a line naming a model blob must not
                                    # trigger a multi-GB sha256)
_PROBE_TIMEOUT = 10                 # explicit --ref probe: commands only; never auto-extracted


class RefDriverError(Exception):
    """A fingerprint driver could not answer (unknown repo alias, systemctl absent, unreadable
    file). Distinct from referent ABSENCE: absence warns loudly + records missing-at-write; a
    driver error warns quietly + records fp-error."""


# ---- extraction (conservative by design) -------------------------------------------
# Path token: `~`- or `/`-rooted, ending before whitespace/quotes/brackets/comma/colon — the
# char class deliberately excludes shell-glob/expansion chars (* ? [ ] { } $), so a globby
# mention ("~/store/*.md") or an unexpanded variable never becomes a bindable claim. Must be
# preceded by start-of-text or a prose delimiter, which keeps URL innards ("http://host/x") and
# mid-word slashes ("and/or", "w/") out. Optional `:NN`/`:NN-MM` file:line suffix is matched so
# it can be STRIPPED (the file is the referent, the line is prose).
_PATH_RE = re.compile(
    r"(?:^|(?<=[\s`'\"(\[=,;]))"
    r"(~?/[A-Za-z0-9._+@%=#-][A-Za-z0-9._+@%=#/-]*)"
    r"(:\d+(?:[-:]\d+)*)?")

# Commit token: 7-12 lowercase hex, REQUIRED repo-alias prefix word (optionally "alias @ sha",
# a real prose shape: "dist @ 8ec6349"). Alias-gating is what keeps this conservative — the
# live store's own update-lines contain 7-8-hex lookalikes ("ed25519", session-uuid prefixes,
# "commit <sha>" with no repo named) that must NOT bind; only a word that maps through
# QQ_BIND_REPOS does.
_COMMIT_RE = re.compile(
    r"(?:^|(?<=[\s`'\"(\[,;]))"
    r"([A-Za-z][A-Za-z0-9_.+-]*)\s+(?:@\s+)?([0-9a-f]{7,12})\b")

# Repo-relative path: multi-segment, '/'-joined, final segment carrying an extension
# ("kernel/backup.py", "profiling/x.json"), delimiter-preceded — so a segment inside a
# `~`/`/`-rooted path or a URL ("http://host/a/b.py") never matches ('/' is not a delimiter),
# and "and/or" without an extension never matches at all. The optional `:NN`/`:NN-MM` suffix is
# matched to be STRIPPED, like _PATH_RE's. Like _BARE_SHA_RE this is PERMISSIVE by design
# ("and/or.x" matches): the bind is resolution-gated in _resolve_relpaths — exactly one
# QQ_BIND_REPOS worktree must have the file.
_RELPATH_RE = re.compile(
    r"(?:^|(?<=[\s`'\"(\[=,;]))"
    r"((?:[A-Za-z0-9._+@%=#-]+/)+[A-Za-z0-9._+@%=#-]*\.[A-Za-z0-9]{1,12})"
    r"(:\d+(?:[-:]\d+)*)?")

# Bare commit token: 7-12 lowercase hex with NO alias prefix, delimiter-preceded (the
# same class as _COMMIT_RE, so a sha inside a path or after "sha256:" never matches — '/' and
# ':' are not delimiters) and \b-bounded (a 7-12-hex WINDOW of a longer hex run has word chars
# adjacent, so full 40/64-hex strings never yield a match; full shas deliberately don't bind —
# prose convention here is the abbreviated form). Extraction is PERMISSIVE on purpose —
# "ed25519", uuid prefixes and all-digit dates all match — because the bind is resolution-gated
# in _resolve_bare_shas: only a token exactly one configured repo resolves as a commit binds
# (a date binds only if it IS a commit somewhere; ambiguity = silent skip).
_BARE_SHA_RE = re.compile(
    r"(?:^|(?<=[\s`'\"(\[,;]))"
    r"([0-9a-f]{7,12})\b")

# Bare unit name. A unit name inside a path ("/etc/systemd/system/x.service") never matches
# (preceded by '/', not a delimiter) — the path itself binds as kind=file instead.
_UNIT_RE = re.compile(
    r"(?:^|(?<=[\s`'\"(\[=,;]))"
    r"([A-Za-z0-9][A-Za-z0-9:_@.\\-]*\.(?:service|timer))\b")

# Remote-host path: `host:(/path|~/path)` where the host word maps through
# QQ_BIND_HOSTS. The path core is _PATH_RE's own char class, but the colon-preceded form
# deliberately does NOT match _PATH_RE (':' is not a prose delimiter there) — this extractor
# OWNS that form, and an UNCONFIGURED host's path therefore stays entirely unbound (silent).
# No local root-existence check, obviously — the path lives on the remote host.
_RHOST_RE = re.compile(
    r"(?:^|(?<=[\s`'\"(\[=,;]))"
    r"([A-Za-z][A-Za-z0-9._-]*):"
    r"(~?/[A-Za-z0-9._+@%=#-][A-Za-z0-9._+@%=#/-]*)")

# Listener port: ":NNNN"/":NNNNN", 4-5 digits, value <= 65535, not digit-adjacent.
# Two prose shapes:
#   - the store's dominant BARE form ("substrate :11435"): the colon preceded by start-of-text
#     or a char that is neither a word char nor another colon — which kills times/ratios
#     ("19:22", "1:5000" — digit before the colon), IPv6 ("::1" — colon before the colon), and
#     file:line ("x.py:1532" — word char before the colon) at the regex layer;
#   - host:port where the host is LOOPBACK/WILDCARD only ("localhost:8080", "127.0.0.1:8090",
#     "0.0.0.0:8085"). A NAMED host's port ("workstation:9090", "grafana.example.net:3000") is a
#     claim about THAT host, and the `ss` probe is local — so named-host forms deliberately do
#     not bind (a not-listening-here result would be a false loud warning). URL innards
#     ("http://localhost:11434/api") don't bind either: '/' precedes the host and '/' is not a
#     prose delimiter.
#
# recall-05 (narrowed, not re-scoped): this is the ONE extractor in the file that fires with NO
# resolution gate at all — a match shells out to a REAL `ss` probe against the live host's
# actual listening sockets (see _fp_port/_ss_listen_table), unsandboxed by any QQ_KB_ROOT/HOME
# override. The bare lookbehind used to be `[^\w:]` — ANY non-word, non-colon character —
# considerably wider than the curated prose-delimiter set (whitespace/quote/paren/bracket/
# equals/comma/semicolon) every OTHER extractor in this file uses, so it fired after stray
# punctuation (markdown table pipes, dashes, bangs, slashes...) that was never a deliberate
# prose boundary. Narrowed to that same curated set; the bracketed IPv6 form ("[::]:8085") has
# a dedicated _PORT_BRACKET_RE rather than widening the bare lookbehind. NOTE: this
# narrowing does NOT change whether a plain, space-preceded mention like the module's own
# documented "substrate :11435" convention still triggers the real host probe — that IS the
# literal, intentional, spec'd shape (this extractor's whole point), and no regex can distinguish "a
# genuine port claim" from "prose that happens to describe the same convention" without
# understanding the sentence; per the review's own verifier, that residual gap is a docs
# gotcha (call it out in public docs), not a code defect this extractor can fix.
_PORT_BARE_RE = re.compile(r"(?:^|(?<=[\s`'\"(\[=,;])):(\d{4,5})(?!\d)")
_PORT_HOST_RE = re.compile(
    r"(?:^|(?<=[\s`'\"(\[=,;]))"
    r"(?:localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0):(\d{4,5})(?!\d)")
_PORT_BRACKET_RE = re.compile(r"\[[^\]]+\]:(\d{4,5})(?!\d)")

_GLOBBY_NEXT = "*?[{"

# Fallback when no config is in reach (direct extract_referents callers): mirrors the
# QQ_BIND_EXCLUDE_ROOTS registry default — the config key is the source of truth at bind time.
_DEFAULT_EXCLUDE_ROOTS = ("/run", "/proc", "/sys", "/tmp", "/dev")


def parse_exclude_roots(config) -> "tuple[str, ...]":
    """QQ_BIND_EXCLUDE_ROOTS (csv of absolute dirs) -> normalized tuple. Non-absolute
    entries are dropped (fail-soft — a relative 'root' cannot anchor a rooted-path exclusion);
    trailing slashes stripped so matching is uniform. Empty config value = no exclusions."""
    out = []
    for entry in config.get_list("QQ_BIND_EXCLUDE_ROOTS"):
        entry = entry.strip().rstrip("/")
        if entry.startswith("/") and entry:
            out.append(entry)
    return tuple(out)


def path_excluded(ident: str, exclude_roots) -> bool:
    """Is a file ref id (rooted or ~-form) under one of the excluded roots? ~-forms expand
    against the LOCAL home first — exclusion compares real filesystem locations, and a home
    under /tmp (throwaway fixture stores) must exclude like any other /tmp path."""
    p = os.path.expanduser(ident)
    return any(p == root or p.startswith(root + "/") for root in exclude_roots)


def parse_repo_aliases(config) -> dict[str, str]:
    """QQ_BIND_REPOS ("alias=path" csv) -> {alias(lower): expanded path}. Malformed entries are
    skipped, not fatal (fail-soft). Empty by default: a generic install binds no git refs until
    the deployment names its own repos (no hardcoded paths in the defaults)."""
    out: dict[str, str] = {}
    for entry in config.get_list("QQ_BIND_REPOS"):
        alias, sep, path = entry.partition("=")
        alias, path = alias.strip().lower(), path.strip()
        if sep and alias and path:
            out[alias] = os.path.expanduser(path)
    return out


def parse_bind_hosts(config) -> dict[str, str]:
    """QQ_BIND_HOSTS ("host=ssh-destination" csv) -> {host(lower): destination}. Same
    fail-soft parse discipline as parse_repo_aliases; empty by default — a generic install
    never runs ssh until the deployment names its hosts."""
    out: dict[str, str] = {}
    for entry in config.get_list("QQ_BIND_HOSTS"):
        host, sep, dest = entry.partition("=")
        host, dest = host.strip().lower(), dest.strip()
        if sep and host and dest:
            out[host] = dest
    return out


def parse_bind_corpus(config) -> dict[str, str]:
    """QQ_BIND_CORPUS ("name=path" csv) -> {name: expanded path}.
    Same fail-soft parse discipline as the sibling maps; empty by default — a generic install
    binds no corpora until the deployment names them."""
    out: dict[str, str] = {}
    for entry in config.get_list("QQ_BIND_CORPUS"):
        name, sep, path = entry.partition("=")
        name, path = name.strip(), path.strip()
        if sep and name and path:
            out[name] = os.path.expanduser(path)
    return out


def container_ids(repo_map: dict[str, str],
                   corpus_map: "dict[str, str] | None" = None) -> "set[str]":
    """The realpaths whose dir-ITSELF binds are skipped at extraction — $HOME,
    every configured repo worktree, every configured corpus root. A claim naming one of these
    names a guaranteed-churning CONTAINER (any activity moves its listing fingerprint), which
    no adjudication can stabilize — the D6 hot-dir problem one level up, live-observed as the
    2026-07-05 29-suspect wall (~/docs and /home/user dir binds). CHILDREN under them bind
    exactly as before; explicit `--ref` stays exempt (extraction hygiene only). Derived, zero
    config."""
    out = {os.path.realpath(os.path.expanduser("~"))}
    for path in (repo_map or {}).values():
        out.add(os.path.realpath(path))
    for path in (corpus_map or {}).values():
        out.add(os.path.realpath(path))
    return out


def parse_corpus_excludes(config) -> "list[tuple[str, str]]":
    """QQ_BIND_CORPUS_EXCLUDE ("corpus:glob" csv) -> [(corpus, glob)]. Aspirational
    docs (a backlog names artifacts that do not exist YET) opt out of binding — born-stale is
    semantically wrong there. Config, not inference: deciding which docs are aspirational is
    cognition, and principle 5 keeps cognition out of the binder."""
    out: "list[tuple[str, str]]" = []
    for entry in config.get_list("QQ_BIND_CORPUS_EXCLUDE"):
        corpus, sep, glob = entry.partition(":")
        corpus, glob = corpus.strip(), glob.strip()
        if sep and corpus and glob:
            out.append((corpus, glob))
    return out


def _unique_repos(repo_map: dict[str, str]) -> list[tuple[str, str]]:
    """(alias, path) per REALPATH-unique repo, first-configured alias winning — several aliases
    can name one worktree (e.g. kernel/ca/my-notes all = one repo), and resolution
    gating counts REPOS, not aliases, so an alias fan-out must not fake ambiguity (D1) or
    multiple owners (D2)."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for alias, path in repo_map.items():
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        out.append((alias, path))
    return out


def _resolve_bare_shas(tokens: list[str], repo_map: dict[str, str]) -> dict[str, str]:
    """{token: alias} for every token that resolves as a commit in EXACTLY ONE unique repo
    (the resolution gate; zero or >=2 repos = absent = silent skip). ONE batched
    `git cat-file --batch-check` subprocess per unique repo per write — all tokens fed at once
    (the O(repos) write budget) — expecting one output line per input line: "<full> commit
    <size>" on a hit, "<input> missing"/"<input> ambiguous" otherwise. Any subprocess failure
    or unparseable batch = that repo resolves nothing (fail-soft, quiet — a broken/absent repo
    must not warn on every write)."""
    hits: dict[str, list[str]] = {t: [] for t in tokens}
    payload = "".join(f"{t}^{{commit}}\n" for t in tokens)
    for alias, path in _unique_repos(repo_map):
        try:
            r = subprocess.run(["git", "-C", path, "cat-file", "--batch-check"],
                                input=payload, capture_output=True, text=True,
                                timeout=_PROBE_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode != 0:
            continue
        lines = r.stdout.splitlines()
        if len(lines) != len(tokens):
            continue
        for tok, line in zip(tokens, lines):
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "commit":
                hits[tok].append(alias)
    return {t: aliases[0] for t, aliases in hits.items() if len(aliases) == 1}


def _resolve_relpaths(tokens: list[str], repo_map: dict[str, str]) -> dict[str, str]:
    """{token: absolute path} for every repo-relative token EXACTLY ONE unique repo's WORKTREE
    has as a regular file (the resolution gate; zero or >=2 owning repos = silent skip).
    Pure filesystem existence checks — zero subprocesses, so D2 costs nothing against the
    O(repos) write budget. The bind records the ABSOLUTE path (file kind) so the existing B3
    commit/pathwatch triggers and the B2 read-side re-hash cover it with no new machinery."""
    out: dict[str, str] = {}
    repos = _unique_repos(repo_map)
    for tok in tokens:
        found = [os.path.join(path, tok) for _alias, path in repos
                 if os.path.isfile(os.path.join(path, tok))]
        if len(found) == 1:
            out[tok] = os.path.abspath(found[0])
    return out


def extract_referents(text: str, repo_map: dict[str, str],
                       host_map: "dict[str, str] | None" = None,
                       exclude_roots: "tuple[str, ...] | None" = None,
                       containers: "set[str] | None" = None) -> list[tuple[str, str]]:
    """Candidate (kind, id) referents from `text`, deduped in first-seen order, capped at
    MAX_REFS. String work plus three classes of reality check: a `/`-rooted path binds only
    when its top-level component is an existing directory on this host — that is what
    separates a filesystem claim ("/data/llama.cpp-turboquant") from URL-path prose
    ("/api/prometheus"), and a failed root check is a silent non-match, never a warning —
    repo-relative paths are resolution-gated against the QQ_BIND_REPOS worktrees (stat-only;
    see _resolve_relpaths), and bare commit tokens are resolution-gated against the same repos
    (one batched git subprocess per unique repo, only when bare tokens are present; see
    _resolve_bare_shas). `host_map` (QQ_BIND_HOSTS via parse_bind_hosts) gates host:path
    extraction — None/empty means no rfile candidates ever. `exclude_roots`
    (QQ_BIND_EXCLUDE_ROOTS via parse_exclude_roots) drops rooted paths under process-runtime
    roots — None means the registry DEFAULT (hygiene on for direct callers too); pass () to
    disable. `containers` (via container_ids) drops dir-ITSELF binds of container
    dirs — None derives HOME + the repo worktrees (callers with corpus config pass the full
    set); pass an empty set to disable."""
    return _extract_uncapped(text, repo_map, host_map or {}, exclude_roots,
                              containers)[:MAX_REFS]


def _extract_uncapped(text: str, repo_map: dict[str, str],
                       host_map: "dict[str, str] | None" = None,
                       exclude_roots: "tuple[str, ...] | None" = None,
                       containers: "set[str] | None" = None) -> list[tuple[str, str]]:
    """extract_referents without the MAX_REFS cap — `_bind` uses this so it can COUNT the
    full candidate set and print the amendment-(ii) truncation note when the cap bites; the
    public extract_referents keeps its original capped contract."""
    host_map = host_map or {}
    if exclude_roots is None:
        exclude_roots = _DEFAULT_EXCLUDE_ROOTS
    if containers is None:
        containers = container_ids(repo_map)
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, ident: str) -> None:
        key = (kind, ident)
        if key not in seen:
            seen.add(key)
            out.append(key)

    for m in _PATH_RE.finditer(text):
        nxt = text[m.end():m.end() + 1]
        if nxt and nxt in _GLOBBY_NEXT:   # nonempty guard: "" is `in` every string
            continue
        tok = m.group(1).rstrip(".")
        if len(tok) > 1:
            tok = tok.rstrip("/") or tok
        if tok in ("~", "~/", "/", ""):
            continue
        # Fragment guard (B5 second wave, from the live corpus seed): "..." is prose ellipsis
        # ("/usr/lib/.../libykcs11.so"), "%" is a systemd template specifier ("/home/%i/x"),
        # and a trailing "-"/"_" is a truncated stem ("/data/llama.cpp-", "session_state_") —
        # none is ever a real referent path; binding one manufactures a false born-stale.
        if "..." in tok or "%" in tok or tok[-1] in "-_":
            continue
        # recall-03: the char class above deliberately excludes whitespace by design,
        # so a REAL path containing a space ("/var/tmp/qtest space dir/notes.txt") gets
        # truncated at the first one into a shorter token ("/var/tmp/qtest") that does not exist
        # — which then gets loudly reported born-stale for a file that is real and unchanged.
        # Widening the char class itself was rejected: unlike every other delimiter here, a
        # space is ALSO the ordinary word-boundary of English prose, so a permissive class would
        # over-match past the path into following sentence text far more often than it correctly
        # reconstructs a genuinely spaced path — the wrong tradeoff for the module's own
        # CONSERVATIVE-extraction invariant. Instead: try a BOUNDED, GATED reconstruction — only
        # when the plain token does not exist, greedily re-join it to up to a few more
        # space-separated path-safe segments and accept the extension ONLY the moment a
        # candidate actually exists on disk. This fires precisely on the true positive (the
        # extended candidate is a real path) and is a no-op on the overwhelmingly common case of
        # an ordinary complete path followed by prose (extending "/a/b.txt for details" through
        # "for"/"for details" never resolves, so `tok` is used exactly as before) — see
        # tests/py/test_refs.py::TestExtraction::test_spaced_path_reconstructed_when_it_exists
        # and ::test_nonexistent_path_followed_by_prose_is_not_reconstructed.
        if nxt == " " and not os.path.exists(os.path.expanduser(tok)):
            candidate, end = tok, m.end(1)
            for _ in range(4):   # bounded: never an unbounded prose scan
                seg = re.match(r" [A-Za-z0-9._+@%=#/-]+", text[end:])
                if not seg:
                    break
                candidate, end = candidate + seg.group(0), end + seg.end()
                trimmed = candidate.rstrip(".")
                if len(trimmed) > 1:
                    trimmed = trimmed.rstrip("/") or trimmed
                if os.path.exists(os.path.expanduser(trimmed)):
                    tok = trimmed
                    break
        if not tok.startswith("~"):
            root = tok.split("/", 2)[1]
            if not os.path.isdir("/" + root):
                continue
        if path_excluded(tok, exclude_roots):   # prose-example runtime paths
            continue
        if os.path.realpath(os.path.expanduser(tok)) in containers:   # container-dir skip
            continue
        add("file", tok)

    # Remote-host paths: only a host that maps through QQ_BIND_HOSTS binds; the same
    # trailing-punctuation/glob discipline as the local path extractor. Id keeps the ~-form
    # path verbatim (the DRIVER expands ~ against the REMOTE $HOME, never the local one).
    if host_map:
        for m in _RHOST_RE.finditer(text):
            host = m.group(1).lower()
            if host not in host_map:
                continue
            nxt = text[m.end():m.end() + 1]
            if nxt and nxt in _GLOBBY_NEXT:
                continue
            path = m.group(2).rstrip(".")
            if len(path) > 1:
                path = path.rstrip("/") or path
            if path in ("~", "~/", "/", ""):
                continue
            add("rfile", f"{host}:{path}")

    # Resolution-gated repo-relative paths (file kind, ABSOLUTE id). Dot segments are
    # rejected outright ("./x.py" is not a repo-relative claim; "../x.py" could escape the
    # worktree join); everything else is the resolver's call.
    if repo_map:
        rel: list[str] = []
        for m in _RELPATH_RE.finditer(text):
            nxt = text[m.end():m.end() + 1]
            if nxt and nxt in _GLOBBY_NEXT:
                continue
            tok = m.group(1)
            if any(seg in (".", "..") for seg in tok.split("/")):
                continue
            if tok not in rel:
                rel.append(tok)
        if rel:
            for tok, abspath in _resolve_relpaths(rel, repo_map).items():
                if path_excluded(abspath, exclude_roots):
                    continue
                if os.path.realpath(abspath) in containers:
                    continue
                add("file", abspath)

    gated_shas: set[str] = set()
    for m in _COMMIT_RE.finditer(text):
        alias = m.group(1).lower()
        if alias in repo_map:
            add("git", f"{alias}@{m.group(2)}")
            gated_shas.add(m.group(2))

    # Resolution-gated bare shas. Dedup against the alias-gated pass FIRST (the sha in
    # "dist 3c070f9" also matches _BARE_SHA_RE — its alias-gated bind already owns it), then
    # gate the remainder on exactly-one-repo resolution. No configured repos = no candidates =
    # zero subprocesses.
    if repo_map:
        bare: list[str] = []
        for m in _BARE_SHA_RE.finditer(text):
            tok = m.group(1)
            if tok not in gated_shas and tok not in bare:
                bare.append(tok)
        if bare:
            resolved = _resolve_bare_shas(bare, repo_map)
            for tok in bare:
                alias = resolved.get(tok)
                if alias is not None:
                    add("git", f"{alias}@{tok}")

    for m in _UNIT_RE.finditer(text):
        add("unit", m.group(1))

    # Listener ports. No resolution gate at extraction (the regex guards are the FP
    # filter); the fingerprint decides listen state — a bound-but-dead listener is exactly the
    # loud born-stale class.
    for regex in (_PORT_HOST_RE, _PORT_BARE_RE, _PORT_BRACKET_RE):
        for m in regex.finditer(text):
            if int(m.group(1)) <= 65535:
                add("port", m.group(1))

    return out


def parse_ref_arg(raw: str) -> "tuple[str, str] | None":
    """Explicit `--ref kind:id` -> (kind, id), or None if malformed/unknown-kind (the caller
    warns + skips; a bad flag value must not fail the write — fail-soft)."""
    kind, sep, ident = raw.partition(":")
    if not sep or kind not in VALID_KINDS or not ident:
        return None
    return kind, ident


# ---- kind drivers (write-side fingerprints only) -------------------------------------------------
def _fp_file(ident: str) -> "tuple[str | None, str]":
    p = Path(os.path.expanduser(ident))
    # AMENDMENT (iv): absence must be PROVEN, not inferred from a failed probe. lexists()
    # returns False for EACCES on an unreadable parent exactly like ENOENT — which misfiled
    # driver blindness as missing-at-write (LOUD born-stale) instead of fp-error (quiet).
    # Seen live during the CA own-user M1 write: /home/ca/.ssh warned born-stale from a
    # pre-membership shell while /home/ca itself correctly degraded to fp-error (its listdir
    # raises, lexists doesn't). ENOENT/ENOTDIR = provable absence; any other stat failure =
    # the driver cannot see, and a blind driver must never shout.
    try:
        os.lstat(p)
    except (FileNotFoundError, NotADirectoryError):
        return None, "missing-at-write"
    except OSError as e:
        raise RefDriverError(f"cannot stat {ident}: {e}")
    if p.is_symlink():
        # dangling symlink: the link is there but the artifact is gone — that IS the born-stale
        # class, not a driver error. Same absence-vs-blindness discrimination on the TARGET
        # (Path.exists() would swallow EACCES into False, re-creating the misclassification).
        try:
            os.stat(p)
        except (FileNotFoundError, NotADirectoryError):
            return None, "missing-at-write"
        except OSError as e:
            raise RefDriverError(f"cannot stat symlink target of {ident}: {e}")
    if p.is_dir():
        try:
            entries = sorted(os.listdir(p))
        except OSError as e:
            raise RefDriverError(f"cannot list directory {ident}: {e}")
        h = hashlib.sha256()
        for name in entries:
            try:
                st = os.lstat(p / name)
            except OSError:
                continue   # entry vanished mid-listing — fingerprint the survivors
            h.update(f"{name}\t{st.st_size}\t{st.st_mtime_ns}\n".encode())
        return "dirsha256:" + h.hexdigest(), "ok"
    try:
        st = os.stat(p)
        if st.st_size > _HASH_CAP_BYTES:
            return f"stat:{st.st_size}:{st.st_mtime_ns}", "ok"
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return "sha256:" + h.hexdigest(), "ok"
    except OSError as e:
        raise RefDriverError(f"cannot read {ident}: {e}")


def _default_branch(repo: str) -> "str | None":
    try:
        r = subprocess.run(["git", "-C", repo, "symbolic-ref", "--quiet", "--short",
                             "refs/remotes/origin/HEAD"], capture_output=True, text=True,
                             timeout=_PROBE_TIMEOUT)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().partition("/")[2] or r.stdout.strip()
        r = subprocess.run(["git", "-C", repo, "symbolic-ref", "--quiet", "--short", "HEAD"],
                            capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def _fp_git(ident: str, repo_map: dict[str, str]) -> "tuple[str | None, str]":
    """id = `alias@abbrev-sha`. Fingerprint = full sha + whether the default
    branch contains it (`default`/`unmerged`; `git:<sha>` alone when no default branch is
    determinable, e.g. detached HEAD, mirror with no origin)."""
    alias, _, sha = ident.partition("@")
    repo = repo_map.get(alias.lower())
    if not repo:
        raise RefDriverError(f"unknown repo alias '{alias}' – add it to QQ_BIND_REPOS")
    if not os.path.isdir(repo):
        raise RefDriverError(f"repo path for '{alias}' not found: {repo}")
    try:
        r = subprocess.run(["git", "-C", repo, "cat-file", "--batch-check"],
                            input=sha + "^{commit}\n",
                            capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise RefDriverError(
            f"git cat-file failed for {sha} in '{alias}': {e}")
    if r.returncode != 0:
        raise RefDriverError(
            f"git cat-file returned {r.returncode} for {sha} in '{alias}': "
            f"{(r.stderr or '').strip()[:120]}")
    if "missing" in r.stdout:
        return None, "missing-at-write"
    try:
        rp = subprocess.run(["git", "-C", repo, "rev-parse", sha + "^{commit}"],
                              capture_output=True, text=True,
                              timeout=_PROBE_TIMEOUT)
        if rp.returncode != 0:
            raise RefDriverError(
                f"git rev-parse failed for {sha} in '{alias}' "
                f"(batch-check did not report missing): "
                f"{(rp.stderr or '').strip()[:120]}")
        full = rp.stdout.strip()
        branch = _default_branch(repo)
        if not branch:
            return f"git:{full}", "ok"
        contained = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor",
                                     full, branch],
                                    capture_output=True,
                                    timeout=_PROBE_TIMEOUT).returncode == 0
        return f"git:{full}:{'default' if contained else 'unmerged'}", "ok"
    except (OSError, subprocess.SubprocessError) as e:
        raise RefDriverError(
            f"git subprocess failed after proven existence of {sha} in '{alias}': {e}")


def _fp_unit_bases(name: str) -> "tuple[str | None, str | None, str]":
    """(fp_new, fp_legacy, status): unit-file hash + ENABLEMENT state via `systemctl cat` /
    `is-enabled` (system scope first, then --user; `is-enabled` runs in whichever scope `cat`
    found the unit — the same scope-resolution order, so the two probes can't disagree about
    which unit they describe). `fp_legacy` is the legacy `sha256:` encoding of the SAME cat
    output — the staleness sweep needs both to migrate legacy records by PROVEN equality (see
    refsresolve.sweep); every other caller uses `_fp_unit` and sees only the current format.
    Enablement is folded into the fingerprint because "authored but NOT enabled" is a
    real drift class a unit-file hash alone can't see; Result/ExecMainStatus (last-run state)
    stay deliberately excluded — flappy, the staleness sweep's axis.

    FORMAT VERSIONING (mass-flip guard): the fp is prefixed `sha256e:` (e = enablement
    folded), hashing cat-stdout + "\\n[enabled-state] <state>\\n". Legacy-format records carry
    `sha256:` fps that LACK the enablement component — a comparer must treat that prefix
    mismatch as no-comparison-basis (re-fingerprint and adjudicate), never as a reality
    change, so the format switch cannot mass-flip the existing unit records. The staleness sweep
    is that comparer: legacy-equal upgrades the stored fp, legacy-unequal suspects.

    `is-enabled` conveys state via stdout even on nonzero exit ("disabled" exits 1); an empty/
    failed enablement probe degrades to state "unknown" rather than a driver error — the unit
    file itself WAS found, and a flaky secondary probe must not discard the primary basis.
    Both scopes failing to FIND the unit = absence; systemctl itself unavailable = driver
    error."""
    err: "Exception | None" = None
    ran = False
    scope_errored = False
    lc_env = {**os.environ, "LC_ALL": "C"}
    for scope_args in ((), ("--user",)):
        try:
            r = subprocess.run(["systemctl", *scope_args, "cat", "--", name],
                                capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
                                env=lc_env)
        except (OSError, subprocess.SubprocessError) as e:
            err = e
            scope_errored = True
            continue
        ran = True
        if r.returncode == 0:
            state = "unknown"
            try:
                en = subprocess.run(["systemctl", *scope_args, "is-enabled", "--", name],
                                     capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
                state = en.stdout.strip().split("\n")[0] or "unknown"
            except (OSError, subprocess.SubprocessError):
                pass
            basis = r.stdout + f"\n[enabled-state] {state}\n"
            return ("sha256e:" + hashlib.sha256(basis.encode()).hexdigest(),
                    "sha256:" + hashlib.sha256(r.stdout.encode()).hexdigest(),
                    "ok")
        elif "No files found" not in r.stderr:
            scope = "user" if "--user" in scope_args else "system"
            err = RuntimeError(
                f"systemctl cat exited {r.returncode} in {scope} scope: "
                f"{r.stderr.strip()[:120]}")
            scope_errored = True
    if not ran:
        raise RefDriverError(f"systemctl unavailable: {err}")
    if scope_errored:
        raise RefDriverError(
            f"unit '{name}' not found in reachable scopes but one scope errored: {err}")
    return None, None, "missing-at-write"


def _fp_unit(name: str) -> "tuple[str | None, str]":
    fp, _legacy, status = _fp_unit_bases(name)
    return fp, status


def _ss_listen_table() -> "dict[int, list[str]]":
    """{port: sorted owning process comms} for every LISTENing TCP socket (IPv4+IPv6), from
    ONE `ss -ltnpH` invocation. A port whose owner isn't visible (no -p permission over
    another user's socket) maps to an empty list — the fingerprint degrades gracefully to bare
    listen state. `ss` missing or failing raises RefDriverError: a driver error (quiet), never
    the loud absence path."""
    try:
        r = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True,
                            timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise RefDriverError(f"ss unavailable: {e}")
    if r.returncode != 0:
        raise RefDriverError(f"ss failed: {(r.stderr or '').strip()[:120]}")
    table: "dict[int, set]" = {}
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if len(parts) < 4:
            continue
        port_s = parts[3].rsplit(":", 1)[-1]   # local addr:port; ':' split survives [::]:80
        if not port_s.isdigit():
            continue
        table.setdefault(int(port_s), set()).update(re.findall(r'"([^"]+)"', ln))
    return {p: sorted(c) for p, c in table.items()}


def _fp_port(ident: str, ctx: dict) -> "tuple[str | None, str]":
    """id = decimal TCP port. Fingerprint = listen state + owning comm(s):
    "listen:<comm[,comm...]>", or bare "listen" without process visibility. NOT listening at
    write time = missing-at-write — loud and correct, the claim names a dead listener.
    Revalidation is the sweep's (no B3 event source for sockets); the read side annotates
    suspect-only, same rule as every non-file kind — NO subprocess on read. The listen table
    is computed ONCE per bind_write across all extracted ports (ctx-cached, failure cached
    too) — one `ss` per write, never one per port."""
    if "ss_table" not in ctx:
        try:
            ctx["ss_table"] = _ss_listen_table()
        except RefDriverError:
            ctx["ss_table"] = None
            raise
    if ctx["ss_table"] is None:
        raise RefDriverError("ss unavailable (cached from this write's first port probe)")
    try:
        port = int(ident)
    except ValueError:
        raise RefDriverError(f"port id must be decimal: {ident!r}")
    comms = ctx["ss_table"].get(port)
    if comms is None:
        return None, "missing-at-write"
    return "listen" + (":" + ",".join(comms) if comms else ""), "ok"


# Remote-host probe: mirrors _fp_file's semantics on the remote host in ONE ssh invocation
# (rare referents; batching not worth a remote-side protocol). Requires GNU userland
# (stat -c, sha256sum); ~-paths expand against
# the REMOTE $HOME; a dangling symlink is MISSING like the local driver's; a dir emits a
# name/size/mtime listing (hashed locally); a file emits its sha256 up to the hash cap, else
# size+mtime. Any remote stat/hash failure exits 9 -> RefDriverError (quiet).
_RFILE_SCRIPT = r"""
p=$1
case "$p" in "~") p=$HOME ;; "~/"*) p=$HOME/${p#"~/"} ;; esac
if [ ! -e "$p" ]; then
  # amendment (iv), remote analog: `! -e` is true for EACCES-under-unreadable-parent exactly
  # like real absence. Walk to the nearest EXISTING ancestor; if it is a dir we cannot
  # traverse, absence is unprovable -> exit 9 (driver error, quiet), not MISSING (loud).
  d=$(dirname "$p")
  while [ ! -e "$d" ] && [ "$d" != / ] && [ "$d" != . ]; do d=$(dirname "$d"); done
  if [ -d "$d" ] && { [ ! -r "$d" ] || [ ! -x "$d" ]; }; then exit 9; fi
  echo MISSING; exit 0
fi
if [ -d "$p" ]; then
  echo DIR
  cd "$p" || exit 9
  ls -A -- . | LC_ALL=C sort | while IFS= read -r n; do
    stat -c "%n	%s	%Y" -- "$n" 2>/dev/null || true
  done
  exit 0
fi
sz=$(stat -c %s -- "$p") || exit 9
if [ "$sz" -gt CAPBYTES ]; then
  mt=$(stat -c %Y -- "$p") || exit 9
  echo "STAT $sz $mt"
else
  h=$(sha256sum -- "$p") || exit 9
  echo "SHA ${h%% *}"
fi
""".replace("CAPBYTES", str(_HASH_CAP_BYTES))

_RFILE_CONNECT_TIMEOUT = 5    # unreachable host costs at most this, never blocks a write
_RFILE_TIMEOUT = 30           # overall cap (connect + remote hash of a <=64MiB file)


def _fp_rfile(ident: str, host_map: dict[str, str]) -> "tuple[str | None, str]":
    """id = `host:path`. One `ssh -o BatchMode=yes -o ConnectTimeout=5` probe running
    _RFILE_SCRIPT remotely. MISSING on a reachable host = missing-at-write (loud — the claim
    names a dead remote artifact, the same born-stale class as a local file); an unreachable
    host, a timeout, an unknown host word or an unparseable probe = RefDriverError = quiet
    fp-error. Revalidation is sweep-only; the read side NEVER runs ssh."""
    host, sep, path = ident.partition(":")
    if not sep or not path:
        raise RefDriverError(f"rfile id must be host:path, got {ident!r}")
    dest = host_map.get(host.lower())
    if not dest:
        raise RefDriverError(f"unknown bind host '{host}' – add it to QQ_BIND_HOSTS")
    # ssh does not carry an argv vector: it joins the command words into ONE string that the far
    # end's login shell re-parses. So the path is quoted HERE, in the driver, and the driver owns
    # that guarantee — it must never rest on the caller's character set. Today the prose
    # extractor's pattern happens to exclude shell-active characters, but `--ref rfile:host:path`
    # takes an unrestricted value, and any future widening of the extractor (e.g. to admit paths
    # containing spaces) would otherwise silently reach this line. Quoting is the invariant;
    # upstream filtering is not.
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes",
                             "-o", f"ConnectTimeout={_RFILE_CONNECT_TIMEOUT}",
                             "--", dest, "sh", "-s", "--", shlex.quote(path)],
                            input=_RFILE_SCRIPT, capture_output=True, text=True,
                            timeout=_RFILE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise RefDriverError(f"ssh probe failed for {host}: {e}")
    if r.returncode != 0:
        raise RefDriverError(f"ssh probe failed for {host} (rc {r.returncode}): "
                              f"{(r.stderr or '').strip()[:120]}")
    first, _, rest = r.stdout.partition("\n")
    first = first.strip()
    if first == "MISSING":
        return None, "missing-at-write"
    if first == "DIR":
        return "rdirsha256:" + hashlib.sha256(rest.encode()).hexdigest(), "ok"
    if first.startswith("SHA "):
        digest = first[4:].strip()
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            return "sha256:" + digest, "ok"
    if first.startswith("STAT "):
        parts = first.split()
        if len(parts) == 3 and parts[1].isdigit():
            return f"stat:{parts[1]}:{parts[2]}", "ok"
    raise RefDriverError(f"unparseable ssh probe output for {host}: {first[:60]!r}")


def _fp_probe(cmd: str) -> "tuple[str | None, str]":
    """Explicit `--ref probe:<command>` only (never auto-extracted): sha256 of the command's
    trailing-whitespace-normalized stdout. A nonzero exit = the probed condition doesn't hold =
    missing-at-write (the loud warn is exactly right for a hand-bound probe that fails at write
    time)."""
    try:
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise RefDriverError(f"probe command failed to run: {e}")
    if r.returncode != 0:
        return None, "missing-at-write"
    norm = b"\n".join(line.rstrip() for line in r.stdout.splitlines())
    return "sha256:" + hashlib.sha256(norm).hexdigest(), "ok"


def _fingerprint(kind: str, ident: str, repo_map: dict[str, str],
                  host_map: "dict[str, str] | None" = None,
                  ctx: "dict | None" = None) -> "tuple[str | None, str]":
    """`ctx` is one bind_write's shared scratch (per-write batching: the D3 ss table lives
    there so N port refs still cost one subprocess); None = a caller fingerprinting a single
    ref gets a private one."""
    if ctx is None:
        ctx = {}
    if kind == "file":
        return _fp_file(ident)
    if kind == "git":
        return _fp_git(ident, repo_map)
    if kind == "unit":
        return _fp_unit(ident)
    if kind == "probe":
        return _fp_probe(ident)
    if kind == "port":
        return _fp_port(ident, ctx)
    if kind == "rfile":
        return _fp_rfile(ident, host_map or {})
    raise RefDriverError(f"no driver for kind '{kind}'")


# ---- the write-path entry point ------------------------------------------------------------------
def bind_write(store: Store, head: str, text: str,
               explicit: "list[str] | None" = None,
               line_ts: "str | None" = None) -> None:
    """Bind the write of `text` to HEAD `head`: extract + fingerprint + warn + persist. NEVER
    raises (fail-soft is the module invariant, so the guard lives here at the seam rather than
    in every write verb); with QQ_BIND=0 it returns before touching anything.

    `line_ts` (B1 amendment i, ratified into B2): the TRUE timestamp of the update-line these
    claims live on, threaded from quintessence.write — so the record's `line_ts` matches the
    stamp a reader sees on the rendered line (B2's ref->rendered-line join key), while `asof`
    stays the bind time. None (a caller with no meaningful line stamp: `essence`, a direct
    library call) falls back to bind time, the original B1 behavior."""
    try:
        _bind(store, head, text or "", list(explicit or ()), line_ts)
    except Exception:
        pass


def _bind(store: Store, head: str, text: str, explicit: list[str],
          line_ts: "str | None" = None) -> None:
    if not store.config.get_bool("QQ_BIND"):
        return
    repo_map = parse_repo_aliases(store.config)
    host_map = parse_bind_hosts(store.config)
    auto = _extract_uncapped(text, repo_map, host_map, parse_exclude_roots(store.config),
                              container_ids(repo_map, parse_bind_corpus(store.config)))
    explicit_refs: list[tuple[str, str]] = []
    for raw in explicit:
        parsed = parse_ref_arg(raw)
        if parsed is None:
            print(f"qq: --ref '{raw}' ignored – want kind:id with kind one of "
                  f"{'/'.join(VALID_KINDS)}", file=sys.stderr)
            continue
        if parsed not in explicit_refs:
            explicit_refs.append(parsed)
    refs = explicit_refs + [ref for ref in auto if ref not in explicit_refs]
    n_explicit = len(explicit_refs)
    # B1 amendment (ii): the MAX_REFS cap was a SILENT truncation — keep the cap (it bounds
    # fingerprint cost on a pathological line) but say so, quietly, so an unbound remainder is
    # never a mystery. Quiet one-liner, NOT the loud born-stale surface.
    total = len(refs)
    refs = refs[:MAX_REFS]
    if total > MAX_REFS:
        dropped = total - MAX_REFS
        explicit_dropped = max(0, n_explicit - MAX_REFS)
        explicit_note = f" ({explicit_dropped} explicit)" if n_explicit else ""
        print(f"qq: ref cap – bound {MAX_REFS} of {total} referents on this write; "
              f"dropped {dropped}{explicit_note} "
              f"(split the line or bind the rest explicitly with --ref)",
              file=sys.stderr)
    if not refs:
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line_ts = line_ts or ts   # amendment (i): true line stamp when threaded, bind time otherwise
    records = []
    ctx: dict = {}   # per-write batching scratch shared across this write's fingerprints
    for kind, ident in refs:
        try:
            fp, status = _fingerprint(kind, ident, repo_map, host_map, ctx)
        except Exception as e:
            # driver error != absence: quiet one-liner, record kept for the sweep
            print(f"qq: ref fingerprint error ({kind}:{ident}): {e}", file=sys.stderr)
            fp, status = None, "fp-error"
        if status == "missing-at-write":
            print(f"qq: referent does not exist: {ident} – claim may be born stale",
                  file=sys.stderr)
        records.append({"head": head, "line_ts": line_ts, "kind": kind, "id": ident,
                        "fp": fp, "asof": ts, "status": status})

    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    with state_lock(store):
        refs_dir = store.state_dir / "refs"
        refs_dir.mkdir(parents=True, exist_ok=True)
        with open(refs_dir / "refs.jsonl", "a", encoding="utf-8") as fh:
            fh.write(payload)


# ---- corpus binding ----------------------------------------------------
def bind_corpus_files(store: Store, corpus: str, root: str, changed_rel: list,
                       src: str) -> int:
    """REBIND the given corpus files (repo-relative paths under `root`): each file's previous
    records are dropped and fresh extraction+fingerprinting appended — replace-on-rebind under
    the state lock; a deleted file just drops its records. Records carry {"corpus", "file"}
    instead of {"head", "line_ts"}, which is the WHOLE join-key extension: the B2 read side
    skips them by construction (its loader requires head+line_ts), while the B3 resolver and
    the sweep see them BY KIND — every existing trigger covers corpus referents with no new
    invalidation code, and amendments (iii)/(iv), exclude-roots and `retired` apply verbatim.

    Only *.md files bind; MEMORY.md at the corpus root is skipped (a derived index, per its
    own header). Born-stale referents record missing-at-write as always, but there is no
    keyboard to warn at (this runs inside post-commit/SessionEnd hooks) — the SessionStart
    digest is the loud surface. Returns the number of records written; never
    raises past the caller's fail-soft seam (refsresolve catches everything).

    `retired` interplay: replace-on-rebind DROPS a retired record when its file is recommitted
    — deliberate. Retirement silences a record for a claim that still stands in the text; once
    the FILE is rewritten the old adjudication no longer describes what the text claims, and a
    still-noisy referent will re-suspect and can be re-retired."""
    repo_map = parse_repo_aliases(store.config)
    host_map = parse_bind_hosts(store.config)
    corpus_map = parse_bind_corpus(store.config)
    exclude_roots = parse_exclude_roots(store.config)
    containers = container_ids(repo_map, corpus_map)
    excludes = [g for c, g in parse_corpus_excludes(store.config) if c == corpus]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    targets = [p for p in changed_rel
               if p.endswith(".md") and os.path.basename(p) != "MEMORY.md"]
    if not targets:
        return 0
    # An excluded (aspirational) file still participates in the DROP phase — its
    # stale records clear on the commit/seed that excludes it — but binds nothing fresh.
    bindable = [p for p in targets
                if not any(fnmatch.fnmatch(p, g) for g in excludes)]

    ctx: dict = {}   # one rebind's batching scratch (D3 ss table etc.) shared across files
    fresh: list = []
    for rel in bindable:
        try:
            with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue   # deleted (or unreadable) file: old records drop below, nothing rebinds
        refs = _extract_uncapped(text, repo_map, host_map, exclude_roots,
                                  containers)[:MAX_REFS]
        for kind, ident in refs:
            try:
                fp, status = _fingerprint(kind, ident, repo_map, host_map, ctx)
            except Exception:
                fp, status = None, "fp-error"
            fresh.append({"corpus": corpus, "file": rel, "kind": kind, "id": ident,
                          "fp": fp, "asof": ts, "status": status, "src": src})

    changed_set = set(targets)
    with state_lock(store):
        refs_dir = store.state_dir / "refs"
        refs_dir.mkdir(parents=True, exist_ok=True)
        path = refs_dir / "refs.jsonl"
        out_lines: list = []
        if path.is_file():
            with open(path, encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    try:
                        rec = json.loads(raw)
                        assert isinstance(rec, dict)
                    except Exception:
                        out_lines.append(raw)   # foreign/torn line preserved, never dropped
                        continue
                    if rec.get("corpus") == corpus and rec.get("file") in changed_set:
                        continue   # superseded by this rebind
                    out_lines.append(raw)
        out_lines.extend(json.dumps(r, ensure_ascii=False) + "\n" for r in fresh)
        atomic_write_lines(path, out_lines)
    return len(fresh)
