# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.write — L0/L2: the write transaction.

This is qq-write + the qq-legacy `essence`/`new`/`rewrite`/`finalize|save|checkpoint` case
handlers, ported faithfully — "a port, not a redesign". The engine primitive
(`_execute_write`, a line-for-line port of the qq-write script) is what `update`/`new`/`rewrite`
drive; `essence`/`finalize` mirror their OWN qq-legacy case bodies directly (those two never
went through qq-write in bash either — see each function's docstring).

PORTED (this phase): update, essence, new, rewrite, finalize/checkpoint/save, and `qq check
--write`'s INDEX auto-fix (`index_autofix_finding`, wired from the `qq` dispatcher).

PORTED (P9, the release-gate legacy-port phase — this docstring previously deferred these as
"DELIBERATELY NOT PORTED"): `compact`, `delete`/`rm`, and the bare `qq reindex` verb, each
built on the transaction primitives above with the same care budget as update/rewrite; every
departure from the legacy case bodies is flagged inline in its own docstring (see each). `qq
init`/`config`/`doctor` live in quintessence.admin — setup/diagnostics, not L0 write-path.

CARRIED RULINGS implemented here (already ratified, see the P5 brief — not re-litigated):
  - `qq new` is the sole creation entrypoint; `rewrite` on a MISSING topic now REFUSES and
    points at `qq new` (sanctioned behavior CHANGE from legacy's silent-create-unscaffolded
    — see `rewrite()`).
  - A2: `QQ_WRITE_TXN` is scoped to the ACTUAL `git commit` subprocess invocation only (an env
    dict passed to that one subprocess.run call) — never `os.environ[...] = ...`, so it is
    never process-wide and nothing else this process spawns inherits it. This is narrower than
    bash's `export QQ_WRITE_TXN="$$"` (which taints the rest of that bash process's lifetime);
    see `commit_push`.
  - A1 remainder: state-dir mutations the write path performs (the activity log) go under the
    DEDICATED `state_lock` (quintessence.store), not the git-tree `.qq.lock` — a HEAD write and
    a state-dir write never contend. NOTE this is a genuine FIX, not a bug-for-bug port: bash's
    `>> "$ACTLOG"` is itself unlocked today (the exact A1 race class). See `_append_activity_log`.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from . import authgate
from .checks import compute_index_text
from .findings import Finding
from .heads import UpdateItem, count_update_markers, parse as parse_head
from .refs import bind_write
from .store import LockTimeout, Store, acquire_flock, state_lock


class WriteError(Exception):
    """A refused/failed write. `exit_code` mirrors qq-write's/qq-legacy's own exit status for
    the equivalent bash refusal, so the `qq` dispatcher can reproduce it exactly."""

    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


class WriteGateDiverted(WriteError):
    """The AUTHORING GATE (quintessence.authgate) diverted this write: the proposed text was
    queued as a [PROPOSED write] pending-findings entry instead of landing in the HEAD.
    exit_code 0 ON PURPOSE — a diversion is a SUCCESS to the caller (the content is safely
    queued for ratification, nothing failed), and the `qq` dispatcher's existing
    `except WriteError: print to stderr, return exit_code` handler therefore renders it as
    required with no dispatcher change: one-line stderr notice, empty stdout, exit 0."""

    def __init__(self, message: str):
        super().__init__(message, exit_code=0)


# ---- the authoring gate check the write verbs call (see quintessence/authgate.py) -------------
def _gate_divert(store: Store, verb: str, topic: str, content: str, model: str) -> None:
    """Queue the proposal and raise WriteGateDiverted. Called only after the verb's own
    would-refuse guards, so the gate never converts a write that would have FAILED into a
    silent success (error behavior stays identical even for gated slugs). A queue-write
    failure is loud (nonzero WriteError), never a silent drop of the proposed content."""
    try:
        notice = authgate.queue_proposal(store, verb, topic, content, model)
    except Exception as e:
        raise WriteError(
            f"qq {verb}: AUTHORING GATE – '{topic}' is security-tagged and this session's "
            f"model ({model}) may not author it alone, and queueing the proposal FAILED "
            f"({e!r}). Nothing was written anywhere; retry, or hand the text to a trusted "
            f"session.", 2)
    raise WriteGateDiverted(notice)


def _gate_target_exists(store: Store, target: str) -> bool:
    """Existence pre-check for the gate's update path, via the SAME target normalization the
    engine applies — a normalization refusal here returns False so the engine raises its own
    (identical) error on the normal path instead."""
    try:
        rel = _normalize_target(store, target)
    except WriteError:
        return False
    return (store.qdir / rel).is_file()


# ---- the git-tree transaction lock (the same .qq.lock the bash writer uses) ------------------
@contextlib.contextmanager
def qq_lock(store: Store, wait: Optional[float] = None):
    """flock on `$QUINTESSENCE_DIR/.qq.lock` — the SAME lock file qq-lib.sh's
    `qq_lock_acquire` uses, so an old (bash) and new (this) writer serialize against each other
    correctly during the migration — the concurrency protocol is frozen across both. Reuses
    `quintessence.store.acquire_flock`, the primitive `state_lock` is also built on (D1: one
    flock-with-timeout mechanism, not two copies to keep in sync)."""
    if wait is None:
        wait = float(store.config.get_int("QQ_LOCK_WAIT"))
    store.qdir.mkdir(parents=True, exist_ok=True)
    lock_path = store.qdir / ".qq.lock"
    with acquire_flock(lock_path, wait,
                        f"qq: timed out after {wait:g}s waiting for the qq lock "
                        f"(another session is writing)"):
        yield


# ---- git plumbing ------------------------------------------------------------------------------
# libc is resolved ONCE, at import, so the post-fork child never has to run the dynamic loader
# (a dlopen between fork and exec can deadlock if any other thread held the loader lock at fork
# time). None on a platform without prctl -> the tie-off is simply skipped.
try:
    import ctypes as _ctypes
    _LIBC = _ctypes.CDLL(None, use_errno=True)
    _LIBC.prctl   # resolve the symbol now, not in the child
except Exception:   # pragma: no cover - non-Linux / no libc
    _LIBC = None

_PR_SET_PDEATHSIG = 1


def _git_child_dies_with_us():
    """preexec for the MUTATING git subprocesses (add/commit): Linux PR_SET_PDEATHSIG, so if the
    qq process is killed mid-transaction (SIGKILL, OOM, harness timeout) the git child receives
    SIGTERM instead of surviving as an unsupervised orphan that holds `.git/index.lock` against
    the next writer after the flock has auto-released (2026-07-29 review, lock-4). SIGKILL cannot
    be caught, so a parent-death signal is the only tie-off available for that case.

    Runs between fork and exec, where almost nothing is safe to do: it makes exactly one call
    through the already-resolved handle above. `_preexec()` decides whether it is used at all —
    a threaded process (an MCP server) never takes this path."""
    try:
        _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except Exception:
        pass


def _preexec():
    """The preexec_fn to pass to git, or None. None when libc/prctl is unavailable, and — the
    load-bearing case — whenever this process has more than one thread: preexec_fn forks and then
    runs Python in the child, which CPython documents as unsafe in the presence of threads. The
    CLI is single-threaded so it gets the tie-off; a long-lived concurrent host (qq-search-mcp /
    qq-remote-mcp, which serve write verbs too) does not, and falls back to the behaviour every
    release before this one had — plus the clean `.git/index.lock` error in commit_push. Correct
    degradation beats a deadlock in a server."""
    if _LIBC is None or threading.active_count() > 1:
        return None
    return _git_child_dies_with_us


def _rev_parse_head(qdir: Path) -> str:
    r = subprocess.run(["git", "-C", str(qdir), "rev-parse", "HEAD"],
                        capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "none"


def _show_diff(qdir: Path, rev: str, rel: str) -> str:
    """Colored diff of what a commit just changed at `rel` — qq-write's own visual-review
    echo (`git --no-pager show --color=always --format='' <rev> -- <rel>`), `|| true` on
    failure (matches bash: never fatal)."""
    r = subprocess.run(["git", "-C", str(qdir), "--no-pager", "show", "--color=always",
                         "--format=", rev, "--", rel], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def commit_push(store: Store, msg: str, paths: list[str]) -> tuple[bool, Optional[str]]:
    """Stage + commit ONLY `paths` (path-scoped, per I2 — never `-A`/a bare `git commit`), under
    the caller-held `qq_lock`. Returns (committed, note): note is the "qq: no changes to commit"
    line qq-lib.sh's `qq_commit_push` itself prints when the pathspec has nothing staged/dirty
    (idempotent no-op, matching bash exactly — some callers, e.g. qq-write's REPLACE-mode
    no-op, print BOTH this note and their own follow-up line).

    A2 (carried ruling): `QQ_WRITE_TXN` is set ONLY in the `env` dict passed to the `git commit`
    subprocess call below — never assigned into `os.environ`, so this process's own environment
    (and anything else it might spawn) never carries the marker. git's own hook subprocesses
    (pre-commit/pre-push, and anything a post-commit hook execs, e.g. a mirror push) are children
    of THIS `git commit` invocation and inherit ITS env, so they still see the marker — the
    narrowing is that nothing else does."""
    qdir = str(store.qdir)
    status = subprocess.run(["git", "-C", qdir, "status", "--porcelain", "--", *paths],
                             capture_output=True, text=True)
    if not status.stdout.strip():
        return False, f"qq: no changes to commit for {' '.join(paths)}"
    add = subprocess.run(["git", "-C", qdir, "add", "--", *paths], capture_output=True,
                          preexec_fn=_preexec())
    if add.returncode != 0:
        err = (add.stderr or b"").decode("utf-8", "replace").strip()
        if "index.lock" in err:
            raise WriteError(
                f"qq: git is mid-operation in the store (`.git/index.lock` held) – most likely a "
                f"previous writer died mid-commit and its git child is still finishing (or died "
                f"uncleanly).\n"
                f"  Wait a moment and retry; if it persists with no git process running for this "
                f"store, remove {store.qdir / '.git' / 'index.lock'} and retry.", 1)
        raise WriteError(f"qq: git add failed (exit {add.returncode}):\n{err}", 1)
    env = dict(os.environ)
    env["QQ_WRITE_TXN"] = str(os.getpid())
    commit = subprocess.run(["git", "-C", qdir, "commit", "--quiet", "-m", msg, "--", *paths],
                            env=env, capture_output=True, preexec_fn=_preexec())
    if commit.returncode != 0:
        # The file is staged but the commit failed. The overwhelmingly common cause on a fresh
        # box/container/CI is a missing git author identity (git exits 128 with "please tell me
        # who you are"). Translate to a clean, actionable WriteError instead of leaking git's raw
        # stderr as an uncaught CalledProcessError traceback.
        err = (commit.stderr or b"").decode("utf-8", "replace").strip()
        low = err.lower()
        if "tell me who you are" in low or "empty ident" in low or "user.email" in low:
            raise WriteError(
                "qq: git has no author identity configured, so the write could not be committed.\n"
                "  Set it once (quintessence commits every write, so git needs an author):\n"
                "    git config --global user.name  \"Your Name\"\n"
                "    git config --global user.email you@example.com\n"
                "  then re-run the write.", 1)
        if "index.lock" in err:
            raise WriteError(
                f"qq: git is mid-operation in the store (`.git/index.lock` held) – most likely a "
                f"previous writer died mid-commit and its git child is still finishing (or died "
                f"uncleanly).\n"
                f"  Wait a moment and retry; if it persists with no git process running for this "
                f"store, remove {store.qdir / '.git' / 'index.lock'} and retry.", 1)
        raise WriteError(f"qq: git commit failed (exit {commit.returncode}):\n{err}", 1)
    return True, None


# ---- state-dir writes (A1: owner-locked, not the bash original's unlocked append) -------------
def _unstage(store: Store, paths: list[str]) -> None:
    """Return `paths` in the git INDEX to their committed state. Every rollback below restores
    working-tree BYTES, but commit_push's own `git add` has already staged the failed content —
    so without this the index stays diverged from both HEAD and the restored file, and the next
    successful write silently commits the failed content along with its own (2026-07-29
    post-fix hunt). Best-effort: a rollback must never raise over its own cleanup."""
    try:
        subprocess.run(["git", "-C", str(store.qdir), "reset", "--quiet", "--", *paths],
                        capture_output=True)
    except Exception:
        pass


def _append_activity_log(store: Store, topic: str) -> None:
    """Records a top-level HEAD write for the digest's "last touched" footer. Under
    `state_lock` (a DEDICATED lock from the git-tree `.qq.lock` — a state-dir mutation never
    contends with a HEAD write) — a deliberate FIX over bash's `>> "$ACTLOG"`, which is
    genuinely unlocked today. Fail-open (`|| true` in
    bash) preserved: a lock timeout or I/O error here never aborts the write it's attached to."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with state_lock(store):
            store.state_dir.mkdir(parents=True, exist_ok=True)
            with open(store.state_dir / "activity.log", "a", encoding="utf-8") as fh:
                fh.write(f"{ts}\t{topic}\n")
    except Exception:
        pass


def _write_index_file(store: Store) -> None:
    """Unlocked local write of INDEX.md's fresh content — matches bash's `reindex()`
    (`reindex_to "$INDEX"`), which is itself lock-free (the CALLER commits it under a lock,
    e.g. `finalize`'s own `qq_lock_acquire`, or the check --write auto-fix below)."""
    store.index_path.write_text(compute_index_text(store), encoding="utf-8")


# ---- qq-write engine (a port of the qq-write transaction; the legacy binary stays as an
# escape hatch) ----------------------------------------------------------------------------------
_ISO_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")
# Caller-supplied timestamp forms stripped so qq (not the writer) owns the update-line stamp.
_UPDATED_MARKER_RE = re.compile(r"^\s*>\s*updated:\s*", re.IGNORECASE)
_LEADING_ISO_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}T[0-9:]+(?:\.\d+)?Z?\s*")
# A rewrite whose content carries an update stamp THIS far in the future is treated as fabricated,
# not clock skew (real fabrications were 10-16h ahead; this tolerates ordinary skew).
_FUTURE_STAMP_GRACE = timedelta(minutes=5)


def _strip_caller_stamp(content: str) -> str:
    """Remove any caller-supplied timestamp from the FIRST line so `qq update` (not the caller)
    owns the stamp: a leading `> updated: <ts>` marker or a bare leading ISO8601 timestamp is
    stripped, leaving bare prose for `_normalize_prepend_first_line` to stamp with now(). This
    closes the fabrication vector where a writer (esp. a weak model mimicking `qq show`) hand-authors
    a FUTURE `> updated:` line that then wins the newest-line sort and misreports HEAD state
    (2026-07-09, flowtun-stack). Continuation lines are untouched; kept separate from `_normalize`
    so that helper stays idempotent (the engine re-applies it and must return its own output verbatim)."""
    parts = content.split("\n", 1)
    first = parts[0]
    rest = ("\n" + parts[1]) if len(parts) > 1 else ""
    stripped = _UPDATED_MARKER_RE.sub("", first, count=1)
    stripped = _LEADING_ISO_RE.sub("", stripped, count=1)
    return f"{stripped}{rest}"


def _future_stamp_lines(content: str) -> "list[str]":
    """`> updated: <ISO8601Z>` lines in `content` whose stamp is more than the grace window in the
    FUTURE vs now() — the fabricated-timestamp guard for `qq rewrite` (a future stamp wins the
    newest-line sort). Malformed/unparseable stamps are ignored (not this guard's job)."""
    cutoff = datetime.now(timezone.utc) + _FUTURE_STAMP_GRACE
    bad = []
    for ln in content.splitlines():
        m = re.match(r"^\s*>\s*updated:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z", ln)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts > cutoff:
            bad.append(ln.strip())
    return bad


def _normalize_target(store: Store, target: str) -> str:
    """Exact port of qq-write's target-normalization `case`: an absolute path already inside
    $QUINTESSENCE_DIR is relativized; any other absolute path is refused; a path with a '/' or an
    explicit '.md' filename is taken literally; every other bare topic — INCLUDING a name that
    merely contains a dot ('v1.2-plan') — gets '.md' (a dotted name is a topic, not an extension,
    else the HEAD is written without .md and becomes invisible to every read verb). Then the
    `..`-traversal guard."""
    qdir = str(store.qdir)
    prefix = qdir + "/"
    if target.startswith(prefix):
        rel = target[len(prefix):]
    elif target.startswith("/"):
        raise WriteError(f"qq-write: {target} is outside {qdir}", 2)
    elif "/" in target:
        rel = target            # has a slash -> literal relpath
    elif target.endswith(".md"):
        rel = target            # already a .md filename -> literal
    else:
        rel = target + ".md"    # bare topic (INCLUDING a dotted name like 'v1.2-plan') -> <topic>.md
    if "/../" in f"/{rel}/":
        raise WriteError("qq-write: path may not traverse (..)", 2)
    if rel.startswith("-"):
        raise WriteError(
            f"qq-write: {rel!r} looks like a command-line flag, not a topic"
            " — qq verbs take no flag in the topic position (see `qq help`)", 2)
    return rel


def _normalize_prepend_first_line(content: str) -> str:
    """qq-write's prepend "safety net": the FIRST piped line must start with '> updated:
    <ISO8601>'. Already '>'-prefixed -> verbatim. Starts with an ISO8601 date+T -> just add the
    '> updated: ' marker, keep the caller's timestamp. Anything else (bare prose) -> stamp with
    now. Only the first line is ever touched; continuation lines ride along verbatim (this is
    why the whole `content` string is manipulated, not just its first line in isolation)."""
    first = content.split("\n", 1)[0]
    if first.startswith(">"):
        return content
    if _ISO_PREFIX_RE.match(first):
        return f"> updated: {content}"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"> updated: {ts} {content}"


def _shrink_guard(abs_path: Path, new_content: str, rel: str, topic_hint: str) -> None:
    """REPLACE-mode-only guard against the classic mistake (piping only a new paragraph, which
    silently wipes the rest of a HEAD): refuse a >50% shrink of a non-trivial (>800B) file UNLESS
    the H1 title line is unchanged (a deliberate compaction/rewrite always preserves the H1;
    an accidental fragment-clobber drops or changes it) — exact port of qq-write's own test."""
    old_bytes = abs_path.stat().st_size
    new_bytes = len(new_content.encode("utf-8"))
    if old_bytes > 800 and new_bytes < old_bytes // 2:
        old_text = abs_path.read_text(encoding="utf-8", errors="replace")
        old_h1 = old_text.split("\n", 1)[0]
        new_h1 = new_content.split("\n", 1)[0]
        if new_h1 == old_h1 and new_h1.startswith("# "):
            return
        raise WriteError(
            f"qq-write: REFUSING — new content ({new_bytes} B) is <half the current {rel} "
            f"({old_bytes} B) and the H1 title was dropped/changed.\n"
            f"  Looks like an accidental clobber (append-instead-of-rewrite?). Compose the FULL "
            f"file (qq show {topic_hint}), or pass --replace if the shrink is intentional.", 2)


def _check_base(store: Store, base: str, before: str, rel: str, topic_hint: str) -> None:
    """--base optimistic-concurrency guard, checked UNDER the lock against the live HEAD: refuse
    if `rel` moved since `base` (a concurrent session wrote it) — path-scoped on purpose (a
    commit to a DIFFERENT HEAD doesn't trip it). Exact port of qq-write's own two checks."""
    qdir = str(store.qdir)
    valid = subprocess.run(["git", "-C", qdir, "cat-file", "-e", f"{base}^{{commit}}"],
                            capture_output=True)
    if valid.returncode != 0:
        raise WriteError(f"qq-write: --base '{base}' is not a valid commit", 2)
    diff = subprocess.run(["git", "-C", qdir, "diff", "--quiet", base, before, "--", rel],
                           capture_output=True)
    if diff.returncode != 0:
        raise WriteError(
            f"qq-write: REFUSING — {rel} changed since --base {base} (a concurrent session "
            f"wrote it).\n"
            f"  Re-read the current HEAD (qq show {topic_hint}) and rebuild your edit from it, "
            f"then retry.", 3)


def _prepend_insert(existing_text: str, insert_text: str) -> str:
    """Merge-safe insert: `insert_text` lands right after the FIRST '# ' title line — literal
    port of qq-write's awk (line-oriented, NOT fence-aware; matching today's engine exactly is
    the point of this phase, not fixing the fence-awareness wart here). No '# ' line at all
    (a malformed HEAD)
    -> raw prepend at the very top (`cat tmp abs`, byte concatenation, not line-reprocessed)."""
    if not re.search(r"(?m)^# ", existing_text):
        return insert_text + existing_text
    lines = existing_text.split("\n")
    if existing_text.endswith("\n"):
        lines = lines[:-1]
    insert_lines = insert_text.split("\n")
    if insert_text.endswith("\n"):
        insert_lines = insert_lines[:-1]
    out: list[str] = []
    done = False
    for line in lines:
        if not done and line.startswith("# "):
            out.append(line)
            out.extend(insert_lines)
            done = True
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def _essence_merge(text: str, essence_new: str) -> str:
    """Refresh the FIRST '> essence:' line, or CREATE one right before the first '## ' body
    section if none exists yet, or append at EOF if there's no '## ' section either — literal
    port of the awk block shared by qq-write's `--essence` flag and qq-legacy's own `essence)`
    case (both use the identical snippet; this is the one shared implementation). The '## '
    branch does NOT consume that line (the awk falls through to its own `{print}` rule), so the
    '## ' line itself is still appended, right after the new essence line + a blank line."""
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
    out: list[str] = []
    done = False
    for line in lines:
        if not done and line.startswith("> essence:"):
            out.append(f"> essence: {essence_new}")
            done = True
            continue
        if not done and line.startswith("## "):
            out.append(f"> essence: {essence_new}")
            out.append("")
            done = True
            out.append(line)
            continue
        out.append(line)
    if not done:
        out.append(f"> essence: {essence_new}")
    return "\n".join(out) + "\n"


def _post_write_notes(store: Store, rel: str, new_text: str) -> list[str]:
    """Only for a top-level HEAD write (`rel` has no '/' and ends '.md' — a journal snapshot or
    INDEX.md write never triggers this): records the activity-log entry, then the advisory
    compaction nudge if the update-line region has grown past the SOFT threshold. Exact port of
    qq-write's post-commit block, minus the activity-log's own locking (see
    `_append_activity_log`)."""
    if "/" in rel or not rel.endswith(".md"):
        return []
    topic = rel[:-3]
    _append_activity_log(store, topic)
    head = parse_head(new_text)
    ulb = head.update_line_region_bytes
    ul = count_update_markers(new_text)
    soft_bytes = store.config.get_int("QQ_COMPACT_SOFT_BYTES")
    soft_lines = store.config.get_int("QQ_COMPACT_SOFT_LINES")
    if ulb > soft_bytes or ul > soft_lines:
        return [f"qq-write: NOTE – {topic} update-lines now {ulb // 1024}kB / {ul} lines; "
                f"consider `qq compact` (folds old update-lines to the journal, keeps newest ~5, "
                f"body untouched)"]
    return []


def _execute_write(store: Store, target: str, content: str, *, msg: Optional[str] = None,
                    base: Optional[str] = None, replace: bool = False, prepend: bool = False,
                    essence_text: Optional[str] = None,
                    transform: Optional[Callable[[str], str]] = None,
                    gate_check: Optional[Callable[[], None]] = None) -> str:
    """The qq-write engine itself — line-for-line port of the bash script's own control flow
    (see the module docstring for the ordering rationale). `essence_text` is the `--essence`
    flag qq-write exposes for direct/scripted use (rides on `prepend` only); no verb wired from
    the `qq` CLI passes it today — `qq essence` is its own, simpler, non-qq-write case (see
    `essence()` below) — kept here for a faithful, complete port of the primitive.

    `transform` (2026-07-29 review, lock-1) is REPLACE mode computed UNDER the lock: the new
    content is derived from the file's live bytes at write time (`transform(current_text)`),
    not from an earlier unlocked read — so a concurrent update that lands between a caller's
    read and this write is IN the input, never silently dropped. A transform caller passes
    empty `content` and needs no `--base` (the read and write happen under one lock hold).

    `gate_check` is a zero-arg callable invoked under the write lock before any file I/O.
    The `update` verb uses it to re-evaluate the authoring gate's target-existence input
    under the lock, closing the window where a concurrent `qq new` between the pre-lock
    check and the lock could let an untrusted update land on a newly-created gated topic."""
    if essence_text is not None and not prepend:
        raise WriteError("qq-write: --essence only works with --prepend-update (REPLACE mode "
                          "composes the full file anyway)", 2)
    if transform is not None and prepend:
        raise WriteError("qq-write: transform is REPLACE-mode only", 2)

    rel = _normalize_target(store, target)
    abs_path = store.qdir / rel
    topic_hint = rel[:-3] if rel.endswith(".md") else rel

    if not content and transform is None:
        raise WriteError(f"qq-write: refusing to write empty content to {rel}", 2)
    if prepend and not abs_path.is_file():
        raise WriteError(f"qq-write: --prepend-update needs an existing {rel}", 2)
    if msg is None:
        msg = f"qq-write(prepend): {rel}" if prepend else f"qq-write: {rel}"

    if prepend:
        content = _normalize_prepend_first_line(content)

    if not prepend and not replace and abs_path.is_file():
        _shrink_guard(abs_path, content, rel, topic_hint)

    with qq_lock(store):
        if gate_check is not None:
            gate_check()

        # Re-check existence UNDER the lock (2026-07-29 review, lock-3): the pre-lock checks
        # above (and the verbs' own) can be invalidated by a concurrent `qq delete` while we
        # waited for the lock — refuse cleanly instead of tracebacking on the read below.
        if (prepend or transform is not None) and not abs_path.is_file():
            raise WriteError(
                f"qq-write: {rel} disappeared while waiting for the write lock (a concurrent "
                f"session deleted it) — nothing written. See journal/{topic_hint}/ to recover.", 1)

        # Absorb an out-of-band edit before REPLACE overwrites it (2026-07-29 review, lock-2):
        # an uncommitted stray edit to `rel` would otherwise be destroyed with no trace — not
        # even in git history. Committing it here is the "silently folded into the next
        # legitimate qq commit" the docs promise; a `--base`-guarded caller then refuses below
        # (rel moved since base), so the writer re-reads and sees the absorbed content.
        if not prepend and abs_path.is_file():
            dirty = subprocess.run(
                ["git", "-C", str(store.qdir), "status", "--porcelain", "--", rel],
                capture_output=True, text=True)
            if dirty.stdout.strip():
                commit_push(store, f"qq: absorb out-of-band edit to {rel}", [rel])

        before = _rev_parse_head(store.qdir)
        if base is not None:
            _check_base(store, base, before, rel, topic_hint)

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        orig_text = (abs_path.read_text(encoding="utf-8", errors="replace")
                     if abs_path.is_file() else None)
        if prepend:
            new_text = _prepend_insert(orig_text, content)
            if essence_text is not None:
                new_text = _essence_merge(new_text, essence_text)
        elif transform is not None:
            new_text = transform(orig_text)
        else:
            new_text = content
        abs_path.write_text(new_text, encoding="utf-8")

        try:
            committed, note = commit_push(store, msg, [rel])
        except WriteError:
            # Roll the file back so disk matches HEAD (2026-07-29 review, engine-003 / lock-4):
            # a failed commit must not leave the new line sitting dirty in the tree, where a
            # retry would land it a second time (duplicated update-line).
            if orig_text is None:
                abs_path.unlink(missing_ok=True)
            else:
                abs_path.write_text(orig_text, encoding="utf-8")
            _unstage(store, [rel])
            raise
        after = _rev_parse_head(store.qdir)

    out: list[str] = []
    if note:
        out.append(note)
    if before != after:
        diff = _show_diff(store.qdir, after, rel)
        if diff:
            out.append(diff.rstrip("\n"))
        out.append(f"qq-write: {rel} committed + mirrored")
        out.extend(_post_write_notes(store, rel, new_text))
    else:
        out.append(f"qq-write: {rel} – no change to commit")
    return "\n".join(out) + "\n"


# ---- the intent-named write verbs (one tool: writing folds into `qq <verb>`) -----------------
def update(store: Store, topic: str, content: str, refs: Optional[list[str]] = None) -> str:
    """`qq update <topic>` — add an update-line, merge-safe. `content` is the ALREADY-resolved
    text (arg-joined-with-space or raw stdin; see the `qq` dispatcher, which does that argv/
    stdin resolution — this function only knows "the text to prepend", matching qq-write's own
    `--prepend-update` contract). Default commit message intentionally reproduces qq-write's own
    shape (`qq-write(prepend): <rel>`, NOT "qq update: ...") — `qq update` has always been a
    thin wrapper over `"$ENGINE/qq-write" "$t" --prepend-update` with no `-m`, so this is the
    real git-log message today; changing it would be a silent behavior change this port must
    not make.

    B1 reality binding rides AFTER the engine call, outside the lock: the write's own
    stdout/store behavior is untouched (bind_write is fail-soft and only ever adds stderr
    warnings + a state-dir record), and a REFUSED write (WriteError above) never binds. Runs
    on the no-op path too — re-asserting an identical line still asserts its claims.

    B1 amendment (i), ratified into B2: the update-line's stamp is resolved HERE (the same
    first-line normalization the engine applies, run early — idempotent, so the engine's own
    pass returns it verbatim) and threaded into bind_write as `line_ts`, so the REF record's
    line_ts equals the stamp a reader sees on the rendered line (B2's join key); `asof` stays
    the bind time. Empty content is NOT pre-normalized — the engine's own empty-content
    refusal must still see it empty.

    AUTHORING GATE (quintessence.authgate): checked after normalization (a queued proposal
    carries the same stamped line a direct write would have landed) and ONLY for a write that
    would otherwise proceed (non-empty content, existing target) — every refusal path stays
    identical even for gated slugs. Gated + untrusted -> the line is queued as a PROPOSED
    write, never touches the HEAD; the raised WriteGateDiverted (exit 0) also means bind_write
    below never runs — nothing landed, so nothing binds. When the target does NOT exist at the
    pre-lock check but the slug IS gated and the model untrusted, a gate_recheck closure is
    passed to _execute_write and re-evaluated under the write lock — a concurrent `qq new`
    that creates the topic in the window between the pre-lock check and the lock is caught
    and diverted to the queue instead of landing."""
    gate_recheck = None
    if content:
        content = _strip_caller_stamp(content)   # qq owns the stamp — ignore any caller timestamp
        content = _normalize_prepend_first_line(content)
        reason = authgate.gate_reason(store.config, topic)
        if reason is not None:
            if _gate_target_exists(store, topic):
                _gate_divert(store, "update", topic, content, reason)
            def gate_recheck():
                if _gate_target_exists(store, topic):
                    _gate_divert(store, "update", topic, content, reason)
    out = _execute_write(store, topic, content, prepend=True, gate_check=gate_recheck)
    line_ts = UpdateItem(marker=content.split("\n", 1)[0]).timestamp
    bind_write(store, topic, content, explicit=refs, line_ts=line_ts)
    return out


def new(store: Store, topic: str, essence_arg: str, refs: Optional[list[str]] = None) -> str:
    """`qq new <topic> [essence text...]` — the SOLE creation entrypoint (carried ruling). Own
    existence check BEFORE the engine call (matches bash: `new)`'s own `[ -f "$f" ] && refuse`,
    a separate guard from the engine's, which has none for the missing-file case since REPLACE
    mode has nothing to guard). Scaffold text is an exact port of the bash `printf` template."""
    f = store.head_path(topic)
    if f.is_file():
        raise WriteError(f"qq new: HEAD '{topic}' already exists (use qq update / qq rewrite)", 1)
    # AUTHORING GATE: after the duplicate refusal (identical error behavior), before the
    # scaffold is composed. The queued text is the ESSENCE ARG (the only free text `new`
    # takes — possibly empty); a trusted session ratifies by replaying `qq new <topic> <text>`,
    # which regenerates the deterministic scaffold itself.
    reason = authgate.gate_reason(store.config, topic)
    if reason is not None:
        _gate_divert(store, "new", topic, essence_arg, reason)
    ess = essence_arg if essence_arg else "<one-line essence: what this thread is about>"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = (f"# Quintessence — {topic}\n"
               f"> updated: {ts} (created)\n"
               f"> essence: {ess}\n\n"
               f"## RE-ENTER HERE\n\n"
               f"## Notes\n")
    out = _execute_write(store, topic, content)
    # B1: the scaffold's only free text is the essence arg; binding the whole scaffold is
    # equivalent (the template itself contains no extractable referents). line_ts = the
    # scaffold's own "(created)" update-line stamp (amendment i).
    bind_write(store, topic, content, explicit=refs, line_ts=ts)
    return out


def rewrite(store: Store, topic: str, content: str, refs: Optional[list[str]] = None,
            allow_future: bool = False) -> str:
    """`qq rewrite <topic>` — RARE whole-file replace from stdin, auto-guarded: `--base`
    (checked under the lock; an uncommitted out-of-band edit to the HEAD is absorbed as its
    own commit first and then trips this same guard — see `_execute_write`, review lock-2).
    NOTE the byte-shrink guard does NOT run on this path (`replace=True` skips it, as legacy's
    `--replace` did); the fragment-clobber protection here is the base guard + git history.

    CARRIED RULING (sanctioned behavior CHANGE from legacy — see checks.py's P4 docstring and
    the P5 brief): a MISSING topic now REFUSES and points at `qq new`, matching `update`'s
    existing missing-HEAD refusal, instead of legacy's silent, unscaffolded create (qq-write's
    plain/REPLACE path has no existence check at all — a typo'd topic used to silently fork a
    malformed near-duplicate HEAD). `qq new` is the only path that can create a HEAD now.

    B1 binding scope: only lines NOVEL vs the pre-rewrite file bind. A rewrite recomposes the
    whole HEAD, so binding the full text would re-fingerprint (and born-stale-warn on) every
    HISTORICAL update-line's referents — noise the warn-first rollout can't afford; those lines
    bound at their own write time. The line-set diff is deliberately crude (exact-line
    membership): a merely-edited line counts as novel and re-binds, which errs toward binding —
    the safe direction.

    B1 amendment (i): novel lines bind PER OWNING UPDATE-LINE STAMP (see _novel_line_buckets),
    so a recomposed update-line's refs keep a line_ts that matches its rendered stamp (B2's
    join key). Novel lines under no update-line (body/meta) and explicit --ref values bind
    with line_ts = bind time, as before. Each bucket is its own bind_write call (each capped
    at MAX_REFS independently — rewrite is the rare verb, the cap exists per-line-of-claims)."""
    if not store.has_head(topic):
        raise WriteError(f"qq rewrite: no HEAD '{topic}' (qq new {topic} first)", 1)
    # FUTURE-STAMP GUARD: refuse a whole-file replace carrying a fabricated FUTURE '> updated:'
    # stamp (it would win the newest-line sort and misreport state). --allow-future is the escape
    # hatch for a DELIBERATE timestamp repair/migration (e.g. correcting an earlier bad stamp).
    if content and not allow_future:
        fut = _future_stamp_lines(content)
        if fut:
            shown = "\n  ".join(fut[:5])
            more = f"\n  …and {len(fut) - 5} more" if len(fut) > 5 else ""
            raise WriteError(
                f"qq rewrite: refusing — {len(fut)} '> updated:' line(s) are stamped in the "
                f"FUTURE (fabricated timestamps win the newest-line sort and misreport HEAD "
                f"state). Fix the stamp(s), or pass --allow-future for a deliberate timestamp "
                f"repair/migration:\n  {shown}{more}", 2)
    # AUTHORING GATE: after the missing-HEAD refusal, before anything is read/locked. Empty
    # content falls through to the engine's own empty-content refusal (identical error path);
    # otherwise the WHOLE proposed file is queued verbatim — the ratifier replays it through
    # `qq rewrite` in a trusted session, where the shrink/--base guards apply as usual.
    if content:
        reason = authgate.gate_reason(store.config, topic)
        if reason is not None:
            _gate_divert(store, "rewrite", topic, content, reason)
    try:
        old_lines = set(store.read_head(topic).splitlines())
    except OSError:
        old_lines = set()   # fail-soft: unreadable old content just widens binding to all lines
    base = _rev_parse_head(store.qdir)
    out = _execute_write(store, topic, content, replace=True, base=base)
    for line_ts, novel_text in _novel_line_buckets(content, old_lines):
        bind_write(store, topic, novel_text, line_ts=line_ts)
    if refs:
        bind_write(store, topic, "", explicit=refs)
    return out


def _novel_line_buckets(content: str, old_lines: set) -> "list[tuple[Optional[str], str]]":
    """Group a rewrite's NOVEL lines by the stamp of the update-line that owns them: a
    '> updated:' line opens a bucket keyed by its own timestamp (None if unstamped); any other
    '> ' meta line or a section header closes it (continuation lines in between ride their
    marker's bucket). Crude and line-oriented — NOT fence-aware — matching the novel-line diff's
    own err-toward-binding direction; a pasted example in the body can at worst bind under a
    stray stamp, never lose a binding. Buckets come back in first-seen order, each as one text
    blob for bind_write."""
    buckets: "dict[Optional[str], list[str]]" = {}
    order: "list[Optional[str]]" = []
    cur: "Optional[str]" = None
    for ln in content.splitlines():
        if ln.startswith("> updated:"):
            cur = UpdateItem(marker=ln).timestamp
        elif ln.startswith("> ") or ln.startswith("#"):
            cur = None
        if ln in old_lines:
            continue
        if cur not in buckets:
            buckets[cur] = []
            order.append(cur)
        buckets[cur].append(ln)
    return [(ts, "\n".join(buckets[ts])) for ts in order]


# ---- essence (qq-legacy's OWN case, never went through qq-write) -----------------------------
def essence(store: Store, topic: str, text: str, refs: Optional[list[str]] = None) -> str:
    """`qq essence <topic> <text>` — refresh (or create) the one-line essence. NOT built on
    `_execute_write`: qq-legacy's `essence)` case never called qq-write either — it locks,
    merges, and commits inline with its own simpler message/no-diff-echo/no-activity-log
    contract (ported here verbatim, including the wart that it always prints "updated" even on
    a true no-op — matching bash exactly, not newly introduced)."""
    f = store.head_path(topic)
    if not f.is_file():
        raise WriteError(f"qq essence: no HEAD '{topic}' (qq new {topic} first)", 1)
    # AUTHORING GATE: after the missing-HEAD refusal; the essence text (even an empty one —
    # legacy would happily write '> essence: ') is queued rather than landed.
    reason = authgate.gate_reason(store.config, topic)
    if reason is not None:
        _gate_divert(store, "essence", topic, text, reason)
    with qq_lock(store):
        if not f.is_file():   # re-check under the lock (review lock-3): concurrent delete
            raise WriteError(f"qq essence: no HEAD '{topic}' (deleted by a concurrent session "
                             f"while waiting for the lock)", 1)
        old_text = f.read_text(encoding="utf-8", errors="replace")
        new_text = _essence_merge(old_text, text)
        f.write_text(new_text, encoding="utf-8")
        try:
            _, note = commit_push(store, f"qq essence: {topic}", [f"{topic}.md"])
        except WriteError:
            f.write_text(old_text, encoding="utf-8")   # review engine-003: disk matches HEAD
            _unstage(store, [f"{topic}.md"])
            raise
    # B1: the new essence text's own claims. line_ts stays bind-time (amendment i does not
    # apply: an essence line carries no rendered stamp for B2 to join on — sweep-owned refs).
    bind_write(store, topic, text, explicit=refs)
    out: list[str] = []
    if note:
        out.append(note)
    out.append(f"qq essence: {topic} updated")
    return "\n".join(out) + "\n"


# ---- finalize / save / checkpoint (journal snapshot, unchanged semantics) --------------------
def finalize(store: Store, topic: str) -> str:
    """`qq finalize <topic>` (aliases: `save`, `checkpoint`) — snapshot the current HEAD into
    the append-only journal, reindex, commit+mirror both under one lock. Exact port of
    qq-legacy's `finalize|save|checkpoint)` case, including the activity-log append being
    UNCONDITIONAL here (a fresh journal filename is always new content, so `commit_push` always
    has something to commit in practice) and the message being printed regardless."""
    f = store.head_path(topic)
    if not f.is_file():
        raise WriteError(f"no HEAD for '{topic}' to finalize (write {f} first)", 1)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with qq_lock(store):
        if not f.is_file():   # re-check under the lock (review lock-3): concurrent delete
            raise WriteError(f"no HEAD for '{topic}' to finalize (deleted by a concurrent "
                             f"session while waiting for the lock)", 1)
        jdir = store.journal_subdir(topic)   # guarded (defense-in-depth; head_path above already
        jdir.mkdir(parents=True, exist_ok=True)   # rejects a traversal topic via the snapshot source)
        snap = _fresh_snapshot_path(jdir, ts)
        try:
            shutil.copy(f, snap)   # plain `cp`, not `cp -p`/copy2 — matches bash (no mtime carry)
        except OSError as e:
            snap.unlink(missing_ok=True)   # don't leave a partial snapshot behind
            raise WriteError(
                f"qq finalize: could not write journal snapshot {snap} ({e.strerror or e}) — "
                f"check the journal directory is writable ({jdir}). Nothing was committed; "
                f"'{topic}' HEAD is unchanged.", 1) from e
        _write_index_file(store)
        try:
            commit_push(store, f"qq finalize: {topic}",
                        [f"journal/{topic}/{snap.name}", "INDEX.md"])
        except WriteError:
            snap.unlink(missing_ok=True)   # don't leave an uncommitted snapshot behind
            _unstage(store, [f"journal/{topic}/{snap.name}", "INDEX.md"])
            raise
        _append_activity_log(store, topic)
    return f"journaled: journal/{topic}/{snap.name}  | reindexed + mirrored\n"


def _fresh_snapshot_path(jdir: Path, ts: str) -> Path:
    """A journal snapshot filename that NEVER overwrites an existing snapshot (2026-07-29
    review, engine-001): the stamp has 1-second resolution, and two finalize/compact calls in
    the same second used to silently replace each other's snapshot — `-2`, `-3`, … suffixes
    disambiguate. Callers hold the qq lock, so exists() here is race-free."""
    snap = jdir / f"{ts}.md"
    n = 1
    while snap.exists():
        n += 1
        snap = jdir / f"{ts}-{n}.md"
    return snap


# ---- reindex / delete / compact (P9: the last legacy-owned store mutations) --------------------
# The trio write.py's own module docstring deferred ("DELIBERATELY NOT PORTED (this phase)"),
# now ported with the same care budget: built ON the transaction primitives above, faithful to
# each qq-legacy case body, every departure flagged inline rather than made silently.

def reindex(store: Store) -> str:
    """`qq reindex` — regenerate INDEX.md in place. Exact port of qq-legacy's `reindex)` case
    (`reindex; echo "reindexed $INDEX"`): UNLOCKED and UNCOMMITTED, deliberately — bash's
    `reindex()` is lock-free and the bare verb never committed; the callers that need
    durability run it under their own lock and name INDEX.md in their commit (finalize, delete,
    `qq check --write`'s index_autofix_finding). The fresh INDEX.md sits in the working tree
    until the next write-path commit picks it up. Adding a lock/commit here would turn "refresh
    my local menu" into a write transaction — a behavior change this port must not make."""
    _write_index_file(store)
    return f"reindexed {store.index_path}\n"


def delete(store: Store, topic: str) -> str:
    """`qq delete <topic>` (alias: `rm`) — retire a HEAD: snapshot its FINAL state to the
    journal (a recovery point), THEN remove the live HEAD + reindex, all under the lock.
    Archive-then-remove, not destruction — the journal copy + git history keep it recoverable,
    and the journal dir is left intact on purpose. Exact port of qq-legacy's `delete|rm)` case:
    same timestamp shape, same commit message + path list, same final message; the topic guard
    rides head_path/journal_subdir (StorePathError), the same choke point _qq_guard_topic fed.
    commit_push's no-op note is printed before the message when present, matching bash (where
    qq_commit_push prints its own note inline) — unreachable in practice here, since the rm'd
    HEAD always dirties the pathspec.

    JUDGMENT CALL (flagged for review, not silently decided): like legacy, delete does NOT
    consult the authoring gate (quintessence.authgate). The gate protects what gets WRITTEN
    into a HEAD; whether an untrusted session may RETIRE a gated HEAD is a policy question
    legacy never answered (its delete predates the gate). Ported bug-for-bug — raise at review
    if retirement should gate too."""
    f = store.head_path(topic)
    if not f.is_file():
        raise WriteError(f"qq delete: no HEAD '{topic}'", 1)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with qq_lock(store):
        if not f.is_file():   # re-check under the lock (review lock-3): concurrent delete won
            raise WriteError(f"qq delete: no HEAD '{topic}' (already deleted by a concurrent "
                             f"session)", 1)
        jdir = store.journal_subdir(topic)
        jdir.mkdir(parents=True, exist_ok=True)
        snap = _fresh_snapshot_path(jdir, ts)
        try:
            shutil.copy(f, snap)   # plain `cp` — matches bash (no mtime carry)
        except OSError as e:
            snap.unlink(missing_ok=True)   # don't leave a partial snapshot behind
            raise WriteError(
                f"qq delete: could not write journal snapshot {snap} ({e.strerror or e}) — "
                f"check the journal directory is writable ({jdir}). Nothing was deleted; "
                f"'{topic}' HEAD is unchanged.", 1) from e
        f.unlink()
        _write_index_file(store)
        try:
            _, note = commit_push(store, f"qq delete: {topic} (final state journaled)",
                                  [f"journal/{topic}/{snap.name}", f"{topic}.md", "INDEX.md"])
        except WriteError:
            # Roll back (review engine-003): restore the live HEAD from the snapshot we just
            # took, so a failed commit doesn't leave the store half-deleted on disk.
            shutil.copy(snap, f)
            snap.unlink(missing_ok=True)
            _write_index_file(store)
            _unstage(store, [f"journal/{topic}/{snap.name}", f"{topic}.md", "INDEX.md"])
            raise
    out: list[str] = []
    if note:
        out.append(note)
    out.append(f"qq delete: removed HEAD '{topic}' – final state journaled at "
               f"journal/{topic}/{snap.name} (recover via the journal or git)")
    return "\n".join(out) + "\n"


def _compact_transform(text: str, keep_n: int) -> str:
    """Line-for-line port of `compact)`'s awk program. The update-line region is everything
    between the title and `> essence:`; an update-line BLOCK (a `> updated:` line plus its
    un-prefixed continuation lines) inherits one keep/drop decision, so a dropped block takes
    its continuations with it (the stranded-continuation bug the awk's comment records fixing).
    Essence + body (## sections) are always kept; the first `# ` line is the title and keeps
    any pre-update preamble flowing (keep=1); anything BEFORE the title matches no rule and is
    dropped — exactly awk's fall-through `{next}`."""
    out: list[str] = []
    seen_title = False
    body = False
    keep = False
    u = 0
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()   # awk sees records, not a trailing empty field
    for ln in lines:
        if not seen_title and ln.startswith("# "):
            out.append(ln); seen_title = True; keep = True; continue
        if ln.startswith("> essence:"):
            body = True; out.append(ln); continue
        if body:
            out.append(ln); continue
        if ln.startswith("> updated:"):
            u += 1
            keep = u <= keep_n
            if keep:
                out.append(ln)
            continue
        if keep:
            out.append(ln)
    return "\n".join(out) + "\n"


def compact(store: Store, topic: str, keep_n: int = 5) -> str:
    """`qq compact <topic> [keep-N=5]` — trim a HEAD's update-line stack to the newest N,
    folding the older ones to the journal: finalize FIRST (the full pre-trim state becomes a
    journal snapshot), then whole-file replace through the REPLACE path with the trim computed
    UNDER the write lock from the file's live bytes (`transform`, 2026-07-29 review lock-1 —
    legacy, and this port until then, read the file and captured `--base` in separate unlocked
    steps, so a concurrent `qq update` landing in the gap was silently dropped from the
    compacted HEAD; with the locked transform a racing update is in the trim's input and, being
    newest, always survives). Lines the trim folds are always older than the just-taken
    snapshot, so nothing the journal doesn't hold is ever removed. Same <=N short-circuit
    message (exit 0), finalize's own output suppressed (`>/dev/null` in legacy), the engine
    write's output NOT suppressed, same trailing trim message.

    Like delete: no authoring-gate consult (legacy had none, and compact introduces no new
    free text — every surviving line is already in the HEAD; the folded ones are in the
    finalize snapshot). Flagged, not silent."""
    f = store.head_path(topic)
    if not f.is_file():
        raise WriteError(f"qq compact: no HEAD '{topic}'", 1)
    ul = count_update_markers(store.read_head(topic))
    if ul <= keep_n:
        return f"qq compact: {topic} has {ul} update-line(s) (<= {keep_n}) – nothing to trim\n"
    finalize(store, topic)   # output discarded — matches `"$0" finalize "$t" >/dev/null`
    trimmed = {"from": ul}
    def _trim(current: str) -> str:
        trimmed["from"] = count_update_markers(current)
        return _compact_transform(current, keep_n)
    out = _execute_write(store, topic, "", replace=True, transform=_trim)
    return (out + f"qq compact: {topic} trimmed {trimmed['from']} -> {keep_n} update-lines "
                  f"(older folded to journal)\n")


# ---- `qq check --write`'s ONE sanctioned auto-fix -------------------------------------------
def index_autofix_finding(store: Store, wait: Optional[float] = None) -> Optional[Finding]:
    """Port of run_check's section 7 in WRITE mode: reindex + commit INDEX.md under the SAME
    transaction lock/marker discipline as every other write, when (and only when) it's actually
    stale. Falls back to the flag-only "[T1 index]" finding if the lock can't be acquired within
    `wait` seconds (default: QQ_LOCK_WAIT) — degrade, don't block/fail the whole `qq check
    --write` run, matching bash's non-fatal `qq_lock_acquire 2>/dev/null` inside run_check.
    Returns None if the index was already fresh (no finding at all, matching
    `Checks.index_staleness_finding`). `wait` is exposed only so a lock-timeout degrade can be
    tested on a fast clock (tests/py/test_write.py); the `qq` dispatcher never passes it.

    Deliberately NOT a method on `Checks` (quintessence.checks, L2/read-only) — this performs a
    real git-tree write, which is L0 write-path territory; `checks.py`'s own docstring reserved
    exactly this seam for P5. Called from the `qq` dispatcher's `_cmd_check`, which substitutes
    this finding for `Checks.index_staleness_finding()`'s flag-only one when `--write` is given."""
    fresh = compute_index_text(store)
    try:
        current = store.read_index()
    except OSError:
        current = None
    if current == fresh:
        return None
    try:
        with qq_lock(store, wait=wait):
            store.index_path.write_text(fresh, encoding="utf-8")
            commit_push(store, "qq check: reindex stale INDEX", ["INDEX.md"])
        return Finding(cls="T1 fix", fields={})
    except LockTimeout:
        return Finding(cls="T1 index", fields={"kind": "stale"})
