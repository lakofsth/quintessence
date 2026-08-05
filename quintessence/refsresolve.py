# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.refsresolve — B3 triggered invalidation (the "push" side of reality binding).

Two event sources feed this resolver:
  - a post-commit hook in a bound repo (the deployment installs it) invokes
    `refs-resolve.py commit --repo <top>` — the RESOLVER derives {repo, sha,
    changed_paths} (git diff-tree), appends the event to $QQ_STATE_DIR/refs/events.jsonl, and
    marks matching `file`/`git` refs `suspect`. Resolver logic lives HERE (a standalone script
    the hook calls), never inline in the hook — the B3 hard constraint.
  - user-scope systemd .path units watching explicitly-named DIRECTORIES (editors atomic-save
    via rename, which kills a file watch) fire
    `refs-resolve.py pathwatch --dir <dir>` for the non-git-mediated referents.

TRANSITION RULES (the ONLY writer of `suspect` besides the audit sweep; the B2 read path never
writes — see quintessence.refsview):
  - a candidate ref is RE-FINGERPRINTED, and flips suspect only when the current fingerprint
    actually differs from the recorded one — a commit that touches-but-reverts a file, or a
    save that rewrites identical bytes, marks nothing (debounce: latest fingerprint wins).
  - an already-suspect ref NEVER re-flips: a hot file updates the record's `suspect_fp`/
    `suspect_asof`/`suspect_src` in place when reality moves again (latest fp wins), but stays
    one suspect awaiting adjudication, not one per commit — UNLESS reality reverts to exactly
    the written claim (fp_now == the recorded write-time `fp`), which AUTO-CLEARS the suspect
    back to its pre-suspect value semantics (amendment iii: fingerprint equality is
    mechanical, not adjudication — see _transition).
  - `ok`/`waived`/`missing-at-write` are the suspectable statuses (an appearance after a
    born-stale write IS a change to the claim's ground truth); `fp-error` records have no
    comparison basis and stay for the sweep; `retired` is TERMINAL — adjudication's
    off-switch for a real-but-useless-to-track referent (hot dirs defeat suspect-once) — the
    resolver never touches one, and file records under QQ_BIND_EXCLUDE_ROOTS are never
    candidates at all (same hygiene as extraction; records bound under a runtime root before the
    exclusion existed stop
    churning even before they are retired).
  - the recorded write-time `fp` is never touched — it stays the comparison basis the B2 read
    side re-hashes against; only suspect_* fields are added/updated.

FAIL-SOFT: every entry point catches everything — a refs failure must never fail a commit (the
hook) or wedge a .path unit; errors append one line to $QQ_STATE_DIR/refs/resolver.log and the
CLI always exits 0. QQ_BIND=0 disables the resolver entirely. refs.jsonl is rewritten
atomically (tmp + rename in the same dir) under the A1 state_lock, and ONLY when a record
actually changed; unparseable lines are preserved verbatim, never dropped. No housekeeping here
ever touches another file in refs/ — in particular *.lock files (carried A1 condition) — the
only files opened for write are refs.jsonl, events.jsonl and resolver.log, each by exact name.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

from .atomicio import atomic_write_lines
from .refs import (RefDriverError, _fp_file, _fp_git, bind_corpus_files, container_ids,
                   parse_bind_corpus, parse_bind_hosts, parse_exclude_roots,
                   parse_repo_aliases, path_excluded)
from .store import Store, state_lock

# events.jsonl is append-per-commit across every hooked repo; bound it. Trim (under the state
# lock, oldest-first) only past the high-water mark so the common append never rewrites.
EVENTS_MAX_BYTES = 1 << 20      # 1 MiB high-water mark
EVENTS_KEEP_LINES = 4000        # retained on trim (a sweep consumes far fewer)

SUSPECTABLE = ("ok", "waived", "missing-at-write")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refs_dir(store: Store):
    return store.state_dir / "refs"


def _log(store: Store, message: str) -> None:
    """One-line error/event note, append-only, best-effort (never raises)."""
    try:
        d = _refs_dir(store)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "resolver.log", "a", encoding="utf-8") as fh:
            fh.write(f"{_now()}  {message}\n")
    except Exception:
        pass


# ---- event log ---------------------------------------------------------------------------------
def append_event(store: Store, event: dict) -> None:
    """Append one event line to refs/events.jsonl under the state lock; trim past the
    high-water mark (events are a downstream-sweep convenience + audit trail, not the source
    of truth — refs.jsonl carries the actual suspect state)."""
    with state_lock(store):
        d = _refs_dir(store)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "events.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        try:
            if path.stat().st_size > EVENTS_MAX_BYTES:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
                atomic_write_lines(path, lines[-EVENTS_KEEP_LINES:])
        except Exception:
            pass   # a failed trim only defers to the next one


# ---- suspect marking ----------------------------------------------------------------------------
def _expand(ident: str) -> str:
    return os.path.expanduser(ident)


def _file_candidate(ident: str, changed_abs: list, dirs_mode: bool = False) -> bool:
    """Does a file ref's id plausibly cover any changed path? Over-matching is fine (the
    re-fingerprint decides); missing is what must not happen. Matches: the exact path, a ref-id
    DIRECTORY containing a changed path, and (pathwatch) anything under the watched dir or an
    ancestor dir of it (a dir fingerprint covers direct entries, so an ancestor's listing can
    move when the watched dir itself is replaced)."""
    p = _expand(ident)
    for ch in changed_abs:
        if p == ch or ch.startswith(p + "/"):
            return True
        if dirs_mode and p.startswith(ch + "/"):
            return True
    return False


def _transition(rec: dict, fp_now, src: str) -> bool:
    """Apply the suspect transition for a re-fingerprinted candidate. Returns True if the
    record changed. `fp_now` is the current fingerprint (None = referent absent now).

    AMENDMENT (iii) — detection is MECHANICAL, the LLM only adjudicates: an already-suspect
    record whose re-fingerprint comes back EQUAL to the recorded write-time `fp` means reality
    has reverted to exactly the written claim — the suspicion is moot by fingerprint equality
    alone, no judgment involved, so it AUTO-CLEARS: status back to the pre-suspect value
    semantics and the suspect_* fields dropped. The
    pre-suspect status isn't stored, but it is recoverable from `fp` itself: a real
    fingerprint could only have been recorded as `ok`/`waived` (both mean "matched at write";
    `ok` is the honest post-revert value for either — a waiver adjudicated a MISMATCH fine,
    and there is no mismatch anymore), while fp None was recorded as `missing-at-write` (the
    referent that appeared has vanished again — back to the born-stale claim, not to `ok`)."""
    if rec.get("status") == "suspect":
        if fp_now == rec.get("fp"):
            rec["status"] = "ok" if fp_now is not None else "missing-at-write"
            for k in ("suspect_fp", "suspect_asof", "suspect_src"):
                rec.pop(k, None)
            return True
        if fp_now != rec.get("suspect_fp"):
            rec["suspect_fp"] = fp_now
            rec["suspect_asof"] = _now()
            rec["suspect_src"] = src
            return True
        return False
    if fp_now == rec.get("fp"):
        return False   # reality still matches the written claim — nothing to flag
    rec["status"] = "suspect"
    rec["suspect_fp"] = fp_now
    rec["suspect_asof"] = _now()
    rec["suspect_src"] = src
    return True


def _mark_suspects(store: Store, src: str, changed_abs: list, dirs_mode: bool,
                    git_repo: "str | None" = None) -> int:
    """One locked read-modify-write pass over refs.jsonl. Returns the number of records
    updated (fresh flips + latest-fp refreshes)."""
    repo_map = parse_repo_aliases(store.config)
    exclude_roots = parse_exclude_roots(store.config)
    git_aliases = set()
    if git_repo:
        real = os.path.realpath(git_repo)
        git_aliases = {a for a, p in repo_map.items() if os.path.realpath(p) == real}

    fp_cache: dict = {}

    def file_fp(ident: str):
        if ident not in fp_cache:
            try:
                fp_cache[ident] = _fp_file(ident)[0]
            except Exception:
                fp_cache[ident] = "<fp-error>"
        return fp_cache[ident]

    n_changed = 0
    with state_lock(store):
        path = _refs_dir(store) / "refs.jsonl"
        if not path.is_file():
            return 0
        out_lines = []
        dirty = False
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw_lines = fh.readlines()
        for raw in raw_lines:
            try:
                rec = json.loads(raw)
                assert isinstance(rec, dict)
            except Exception:
                out_lines.append(raw)   # foreign/torn line: preserved verbatim, never dropped
                continue
            kind, status = rec.get("kind"), rec.get("status")
            updated = False
            if status in SUSPECTABLE or status == "suspect":
                # `retired` is neither suspectable nor suspect, so it can never enter here —
                # terminal by construction. Excluded-root file records (hot runtime dirs) are
                # additionally barred from candidacy so older records stop churning.
                if (kind == "file"
                        and not path_excluded(rec.get("id") or "", exclude_roots)
                        and _file_candidate(rec.get("id") or "", changed_abs, dirs_mode)):
                    fp_now = file_fp(rec.get("id") or "")
                    if fp_now != "<fp-error>":
                        updated = _transition(rec, fp_now, src)
                elif kind == "git" and git_aliases:
                    ident = rec.get("id") or ""
                    if ident.partition("@")[0].lower() in git_aliases:
                        try:
                            fp_now = _fp_git(ident, repo_map)[0]
                        except Exception:   # RefDriverError + anything else: no basis, skip
                            fp_now = "<fp-error>"
                        if fp_now != "<fp-error>":
                            updated = _transition(rec, fp_now, src)
            if updated:
                n_changed += 1
                dirty = True
                out_lines.append(json.dumps(rec, ensure_ascii=False) + "\n")
            else:
                out_lines.append(raw)
        if dirty:
            atomic_write_lines(path, out_lines)
    return n_changed


# ---- entry points (each: fail-soft, returns records-updated count) ------------------------------
def resolve_commit(store: Store, repo: str, sha: "str | None" = None) -> int:
    """The post-commit trigger: derive changed paths for `sha` (default: HEAD), append the
    {repo, sha, changed_paths} event, mark matching file/git refs suspect."""
    try:
        if not store.config.get_bool("QQ_BIND"):
            return 0
        repo = os.path.abspath(_expand(repo))
        if sha is None:
            r = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                                capture_output=True, text=True)
            if r.returncode != 0:
                _log(store, f"commit-resolve: rev-parse failed in {repo}")
                return 0
            sha = r.stdout.strip()
        r = subprocess.run(["git", "-C", repo, "diff-tree", "--root", "--no-commit-id",
                             "--name-only", "-r", sha], capture_output=True, text=True)
        changed_rel = [ln for ln in r.stdout.splitlines() if ln] if r.returncode == 0 else []
        append_event(store, {"ts": _now(), "repo": repo, "sha": sha,
                              "changed_paths": changed_rel})
        changed_abs = [os.path.join(repo, p) for p in changed_rel]
        n = _mark_suspects(store, src=f"{repo}@{sha[:9]}", changed_abs=changed_abs,
                            dirs_mode=False, git_repo=repo)
        if n:
            _log(store, f"commit-resolve: {n} ref(s) updated by {repo}@{sha[:9]}")
        # A commit to a configured CORPUS repo also rebinds the
        # changed *.md files' claims. Same fail-soft rule — a corpus-bind failure must never
        # fail the commit or the mirror, so it gets its own catch and only logs.
        try:
            real = os.path.realpath(repo)
            for cname, croot in parse_bind_corpus(store.config).items():
                if os.path.realpath(croot) == real:
                    nb = bind_corpus_files(store, cname, repo, changed_rel,
                                            src=f"{repo}@{sha[:9]}")
                    if nb:
                        _log(store, f"corpus-bind: {nb} record(s) rebound for corpus "
                                     f"'{cname}' by {sha[:9]}")
                    break
        except Exception as e:
            _log(store, f"corpus-bind: error: {e!r}")
        return n
    except Exception as e:
        _log(store, f"commit-resolve: error: {e!r}")
        return 0


def sweep(store: Store) -> dict:
    """Revalidation sweep: ONE pass over live records re-fingerprinting the kinds that
    have no other revalidation path. Coverage: `unit` (with the legacy `sha256:`->`sha256e:`
    equality-proven format upgrade — the documented sweep-only exception to fp-never-touched),
    `port` (one ss table), `rfile` (one ssh per referent; UNREACHABLE = skip, not churn),
    `git`, `probe` (the sweep is not the read path), CORPUS `file` records (HEAD file records
    revalidate on the B2 read path — the sweep touches those only when already `suspect`, so
    a revert-without-trigger stops annotating), `fp-error` fill-on-success, and the amendment
    (iii) auto-clear for suspects of every kind. `retired`, exclude-roots and container ids
    are skipped (resolver/extraction parity). Probes run UNLOCKED (seconds of subprocess
    work must not hold the state lock); the transition pass re-reads under the lock and
    applies `_transition` verbatim — no new statuses. Returns the count dict it logs."""
    counts = {"swept": 0, "suspected": 0, "cleared": 0, "upgraded": 0, "filled": 0,
              "probe_errors": 0, "fp_error_left": 0}
    try:
        if not store.config.get_bool("QQ_BIND"):
            return counts
        repo_map = parse_repo_aliases(store.config)
        host_map = parse_bind_hosts(store.config)
        exclude_roots = parse_exclude_roots(store.config)
        containers = container_ids(repo_map, parse_bind_corpus(store.config))
        path = _refs_dir(store) / "refs.jsonl"
        if not path.is_file():
            return counts

        def _wants_probe(rec: dict) -> bool:
            kind, status = rec.get("kind"), rec.get("status")
            if status == "retired" or kind not in ("file", "git", "unit", "port",
                                                     "rfile", "probe"):
                return False
            if kind == "file":
                ident = rec.get("id") or ""
                if path_excluded(ident, exclude_roots):
                    return False
                if os.path.realpath(os.path.expanduser(ident)) in containers:
                    return False
                # corpus file records: all statuses; HEAD-bound: suspect-only (B2 owns them)
                return bool(rec.get("corpus")) or status == "suspect"
            return True

        with open(path, encoding="utf-8", errors="replace") as fh:
            snapshot = []
            for raw in fh:
                try:
                    rec = json.loads(raw)
                    assert isinstance(rec, dict)
                except Exception:
                    continue
                if _wants_probe(rec):
                    snapshot.append(rec)

        # ---- probe phase, unlocked, memoized per (kind, id) --------------------------------
        from .refs import (_fingerprint, _fp_unit_bases)
        probed: dict = {}   # (kind,id) -> ("fp", fp_now) | ("unit", new, legacy, status) | ("error",)
        ctx: dict = {}      # shared batching scratch (the D3 ss table)
        for rec in snapshot:
            key = (rec.get("kind"), rec.get("id"))
            if key in probed:
                continue
            kind, ident = key
            try:
                if kind == "unit":
                    fp_new, fp_legacy, status = _fp_unit_bases(ident)
                    probed[key] = ("unit", fp_new, fp_legacy, status)
                else:
                    fp_now, _status = _fingerprint(kind, ident, repo_map, host_map, ctx)
                    probed[key] = ("fp", fp_now)
            except Exception:   # RefDriverError + anything else: no basis this pass, skip
                probed[key] = ("error",)
                counts["probe_errors"] += 1

        # ---- transition phase, locked, re-read (probes may be seconds stale — the daily
        # sweep is drift detection, not a race with the triggers) ----------------------------
        src = f"sweep:{_now()}"
        with state_lock(store):
            with open(path, encoding="utf-8", errors="replace") as fh:
                raw_lines = fh.readlines()
            out_lines, dirty = [], False
            for raw in raw_lines:
                try:
                    rec = json.loads(raw)
                    assert isinstance(rec, dict)
                except Exception:
                    out_lines.append(raw)
                    continue
                result = probed.get((rec.get("kind"), rec.get("id")))
                if result is None or result[0] == "error" or not _wants_probe(rec):
                    if rec.get("status") == "fp-error" and rec.get("kind") in (
                            "file", "git", "unit", "port", "rfile", "probe"):
                        counts["fp_error_left"] += 1
                    out_lines.append(raw)
                    continue
                counts["swept"] += 1
                before = rec.get("status")
                updated = False
                if result[0] == "unit":
                    _tag, fp_new, fp_legacy, _pstatus = result
                    legacy = (rec.get("fp") or "").startswith("sha256:") and \
                        not (rec.get("fp") or "").startswith("sha256e:")
                    if legacy and fp_legacy is not None and rec.get("fp") == fp_legacy:
                        # format migration: SAME basis proven by legacy-format equality —
                        # re-encode in place, and moot any suspicion (equality is mechanical)
                        rec["fp"] = fp_new
                        if before == "suspect":
                            rec["status"] = "ok"
                            for k in ("suspect_fp", "suspect_asof", "suspect_src"):
                                rec.pop(k, None)
                        counts["upgraded"] += 1
                        updated = True
                    else:
                        fp_now = fp_new
                        updated = _transition(rec, fp_now, src) or updated
                else:
                    fp_now = result[1]
                    if before == "fp-error":
                        if fp_now is not None:   # first-ever basis obtained: fill, not touch
                            rec["fp"] = fp_now
                            rec["status"] = "ok"
                            rec["filled_asof"] = _now()
                            counts["filled"] += 1
                            updated = True
                        else:
                            counts["fp_error_left"] += 1
                    else:
                        updated = _transition(rec, fp_now, src) or updated
                if updated:
                    after = rec.get("status")
                    if before != "suspect" and after == "suspect":
                        counts["suspected"] += 1
                    elif before == "suspect" and after != "suspect":
                        counts["cleared"] += 1
                    dirty = True
                    out_lines.append(json.dumps(rec, ensure_ascii=False) + "\n")
                else:
                    out_lines.append(raw)
            if dirty:
                atomic_write_lines(path, out_lines)
        _log(store, "sweep: {swept} swept / {suspected} suspected / {cleared} cleared / "
                     "{upgraded} upgraded / {filled} filled / {probe_errors} probe-errors / "
                     "{fp_error_left} fp-error left".format(**counts))
        return counts
    except Exception as e:
        _log(store, f"sweep: error: {e!r}")
        return counts


def corpus_seed(store: Store) -> int:
    """One-time backfill: bind every currently-TRACKED *.md of every configured
    corpus, so existing files don't wait for their next commit. Idempotent — rebinding a file
    replaces its records. Returns records written."""
    try:
        if not store.config.get_bool("QQ_BIND"):
            return 0
        total = 0
        for cname, croot in parse_bind_corpus(store.config).items():
            croot = os.path.abspath(_expand(croot))
            r = subprocess.run(["git", "-C", croot, "ls-files", "--", "*.md"],
                                capture_output=True, text=True)
            if r.returncode != 0:
                _log(store, f"corpus-seed: ls-files failed for '{cname}' ({croot})")
                continue
            files = [ln for ln in r.stdout.splitlines() if ln]
            n = bind_corpus_files(store, cname, croot, files, src=f"seed:{_now()}")
            _log(store, f"corpus-seed: {n} record(s) bound for corpus '{cname}' "
                         f"({len(files)} file(s))")
            total += n
        return total
    except Exception as e:
        _log(store, f"corpus-seed: error: {e!r}")
        return 0


def resolve_pathwatch(store: Store, watch_dir: str) -> int:
    """The .path-unit trigger: a watched DIRECTORY changed; re-fingerprint the file refs it
    plausibly covers, mark the actually-moved ones suspect, record the event."""
    try:
        if not store.config.get_bool("QQ_BIND"):
            return 0
        watch_dir = os.path.abspath(_expand(watch_dir))
        append_event(store, {"ts": _now(), "repo": None, "sha": None,
                              "changed_paths": [watch_dir], "source": "pathwatch"})
        n = _mark_suspects(store, src=f"pathwatch:{watch_dir}", changed_abs=[watch_dir],
                            dirs_mode=True, git_repo=None)
        if n:
            _log(store, f"pathwatch: {n} ref(s) updated under {watch_dir}")
        return n
    except Exception as e:
        _log(store, f"pathwatch: error: {e!r}")
        return 0
