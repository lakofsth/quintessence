# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.store — L0: locate the store/state dirs from Config, provide the state-dir
lock, and read-only HEAD access.

READ SIDE ONLY. The write transaction (flock + path-scoped git commit + the QQ_WRITE_TXN
marker over $QUINTESSENCE_DIR/.qq.lock, currently qq-lib.sh's qq_lock_acquire/qq_commit_push)
is explicitly NOT ported here — that is P5, reviewed with maximum care as its own phase.
What IS new here is `state_lock()`: review found the RUNTIME STATE dir
($QQ_STATE_DIR — the pending-findings queue, the embedding cache, wave-offs, the activity log)
is unlocked shared mutable state today (`findings_set_section` does a read-modify-write of the
whole file with no serialization; two concurrent `qq check --write` can silently lose one
side's section). The invariant — every shared mutable file has an owner-locked write path —
covers the state dir too, on a DEDICATED lock file distinct from the store's own
`.qq.lock` — so a state-dir mutation never has to contend with (or accidentally block on) a
HEAD write, and vice versa. Nothing in P1 performs a write through this lock; it is
infrastructure for P4 (findings/reconcile) and P5 to use, unit-tested here so its acquire/
block/timeout/release behaviour is pinned before anything depends on it.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import locale
import os
import time
from pathlib import Path
from typing import Iterator, Optional

from .config import Config


class LockTimeout(Exception):
    """Raised by state_lock() when the lock could not be acquired within the timeout."""


class StorePathError(ValueError):
    """A topic/slug that would resolve to a path outside the store tree (traversal guard).
    Subclasses ValueError so existing `except ValueError` sites still catch it; the qq
    dispatcher surfaces it as a clean exit-2 usage error."""


def _locale_sort_key(name: str) -> bytes:
    """Sort key matching bash glob order (`for f in "$QDIR"/*.md`), which is LOCALE-aware
    (LC_COLLATE), NOT plain codepoint order — caught by live-store parity testing: a real
    store under en_US.UTF-8 orders "selfhood-two-bootstraps" before
    "self-hosted-claude-code" (collation gives punctuation like '-' little/no weight at the
    primary comparison level, so it compares as if "selfhood..." vs "selfhosted..."), while
    Python's default `sorted()` (codepoint order, hyphen 0x2D < letters) puts the hyphenated
    name first — a real, silent divergence in HEAD/memory listing order on a store with names
    that differ only by punctuation placement. `locale.strxfrm` under the process's OWN
    environment locale (inherited from whatever LC_COLLATE/LANG the shell — and therefore
    qq-legacy's glob — would also see) reproduces bash's ordering."""
    try:
        locale.setlocale(locale.LC_COLLATE, "")
    except locale.Error:
        pass   # locale not installed on this host -> falls back to "C" (== codepoint order),
               # which is also what bash's own glob would degrade to in that same situation
    return locale.strxfrm(name)


class Store:
    """Read-only view of a quintessence install, located from a Config."""

    def __init__(self, config: Config, qdir: Optional[str] = None, memdir: Optional[str] = None):
        self.config = config
        # `qdir` override: point this Store at a specific HEAD tree (a
        # project store on the search path) instead of the Config-resolved user store. journal/
        # INDEX derive from it (a project store has its own journal + INDEX). state_dir stays
        # user-global (runtime state is not layered). qdir=None is the pre-P2 behavior.
        # `memdir` override: a project store's atomic-fact memory lives under the
        # project (<project>/.quintessence/memory); the user store uses the Config-resolved
        # QQ_MEMDIR. memdir=None keeps the pre-P4 Config value, byte-identical.
        self.qdir = Path(qdir) if qdir is not None else Path(config.get_path("QUINTESSENCE_DIR"))
        self.journal_dir = self.qdir / "journal"
        self.state_dir = Path(config.get_path("QQ_STATE_DIR"))
        self.memdir = Path(memdir) if memdir is not None else Path(config.get_path("QQ_MEMDIR"))
        self.index_path = self.qdir / "INDEX.md"

    # ---- HEADs (read-only) -------------------------------------------------------------
    _RESERVED = {"RUBRIC", "INDEX"}

    def _within_qdir(self, p: Path, subject: str) -> Path:
        """Traversal guard: `p` must resolve inside `qdir`. Raises StorePathError otherwise.
        This is the shared choke point the write path already enforces via
        `write._normalize_target`; putting it in `head_path`/`journal_subdir` means the verbs
        that build a path straight from a caller-supplied topic (essence/finalize and the read
        verbs show/brief/path) inherit the same guard instead of trusting the string. Topics are
        agent-supplied, so a `../`-shaped topic must not escape the store — and a `-`-leading
        topic is always a guessed/leaked CLI flag, never a real topic (a real `--help.md` HEAD
        has been minted this way; the "no HEAD (qq new <t> first)" advice then compounds it)."""
        if subject.startswith("-"):
            raise StorePathError(
                f"quintessence: topic {subject!r} looks like a command-line flag, not a topic"
                " – qq verbs take no flag in the topic position (see `qq help`)")
        try:
            p.resolve().relative_to(self.qdir.resolve())
        except ValueError:
            raise StorePathError(
                f"quintessence: topic {subject!r} may not traverse outside the store")
        return p

    def head_path(self, slug: str) -> Path:
        return self._within_qdir(self.qdir / f"{slug}.md", slug)

    def journal_subdir(self, topic: str) -> Path:
        """Guarded `journal_dir / topic` — same traversal guard as head_path (finalize builds
        this straight from the topic string)."""
        return self._within_qdir(self.journal_dir / topic, topic)

    def has_head(self, slug: str) -> bool:
        return self.head_path(slug).is_file()

    def list_head_slugs(self) -> list[str]:
        """Every <topic>.md in the store dir (excluding RUBRIC/INDEX), in the SAME order as
        `for f in "$QDIR"/*.md` (bash glob) — locale-collated (see _locale_sort_key), not
        plain codepoint order."""
        if not self.qdir.is_dir():
            return []
        out = []
        for p in sorted(self.qdir.glob("*.md"), key=lambda p: _locale_sort_key(p.name)):
            slug = p.stem
            if slug in self._RESERVED:
                continue
            out.append(slug)
        return out

    def read_head(self, slug: str) -> str:
        return self.head_path(slug).read_text(encoding="utf-8", errors="replace")

    def read_index(self) -> str:
        """Raw content of INDEX.md (the menu cache) — matches `cat "$INDEX"`, byte-for-byte,
        no re-derivation. See quintessence.cli.render_menu for why this cached content, rather
        than a fresh re-render, is `qq menu`'s actual (and correct-to-preserve) contract."""
        return self.index_path.read_text(encoding="utf-8", errors="replace")

    # ---- memory (read-only) --------------------------------------------------------------
    def list_memory_slugs(self) -> list[str]:
        """Locale-collated, matching bash glob order (see list_head_slugs/_locale_sort_key)."""
        if not self.memdir.is_dir():
            return []
        out = []
        for p in sorted(self.memdir.glob("*.md"), key=lambda p: _locale_sort_key(p.name)):
            if p.stem == "MEMORY":
                continue
            out.append(p.stem)
        return out

    def memory_path(self, slug: str) -> Path:
        return self.memdir / f"{slug}.md"

    def read_memory(self, slug: str) -> str:
        return self.memory_path(slug).read_text(encoding="utf-8", errors="replace")

    # ---- journal (read-only) --------------------------------------------------------------
    def journal_snapshots(self, slug: str) -> list[Path]:
        d = self.journal_dir / slug
        if not d.is_dir():
            return []
        return sorted(d.glob("*.md"))


class CompositeStore:
    """Read-only view over an ORDERED search path of Stores, most-specific first. Used for
    FETCH verbs (show/brief/path) so a HEAD named from inside a project
    resolves against the project store first, then falls through to the user store — the
    CLAUDE.md-style "most-specific wins, user store is the shared fallback" behaviour.

      * has_head/read_head/head_path  -> the FIRST store on the path that has the slug (project
        shadows user on an exact-slug collision).
      * list_head_slugs               -> union across the path, project-first, deduped by slug.
      * read_memory/memory_path/list_memory_slugs -> the SAME discipline for atomic facts:
        most-specific-first fetch, union list. (Consumed by the check/xref/recall layers as
        they move onto the composite in P5/P6; no read verb reads memory today.)
      * anything else (config, qdir, state_dir, memdir, index_path, read_index, journal readers)
        -> delegated to the most-specific store (stores[0]) via __getattr__, so this is a
        transparent drop-in wherever a single Store is expected.

    A single-element path degenerates to pure delegation, indistinguishable from that one Store
    The `qq` entry point does not even construct a CompositeStore in the
    single-store case (it passes the bare Store), so the degenerate path is byte-identical; this
    class only ever wraps >= 2 stores in practice, but the len==1 delegation is kept correct and
    tested so the class is safe to use unconditionally."""

    def __init__(self, stores: "list[Store]"):
        if not stores:
            raise ValueError("CompositeStore needs at least one store")
        self.stores = list(stores)

    def __getattr__(self, name: str):
        # only reached for names NOT defined on CompositeStore itself (the composed methods below
        # shadow this). Delegate everything else to the most-specific store.
        if name == "stores":
            raise AttributeError(name)   # guard against recursion before __init__ assigns it
        return getattr(self.stores[0], name)

    def _first_with(self, slug: str) -> "Optional[Store]":
        for s in self.stores:
            if s.has_head(slug):
                return s
        return None

    def has_head(self, slug: str) -> bool:
        return any(s.has_head(slug) for s in self.stores)

    def read_head(self, slug: str) -> str:
        s = self._first_with(slug)
        # On a genuinely-absent slug, delegate to the most-specific store so the caller sees the
        # same FileNotFoundError (from Path.read_text) it would get from a bare Store.
        return (s or self.stores[0]).read_head(slug)

    def head_path(self, slug: str) -> Path:
        s = self._first_with(slug)
        return (s or self.stores[0]).head_path(slug)

    def list_head_slugs(self) -> list[str]:
        return self._union_slugs(lambda s: s.list_head_slugs())

    # ---- memory composition: same most-specific-first / union discipline as HEADs ----------
    def _first_with_memory(self, slug: str) -> "Optional[Store]":
        for s in self.stores:
            if s.memory_path(slug).is_file():
                return s
        return None

    def read_memory(self, slug: str) -> str:
        s = self._first_with_memory(slug)
        return (s or self.stores[0]).read_memory(slug)

    def memory_path(self, slug: str) -> Path:
        s = self._first_with_memory(slug)
        return (s or self.stores[0]).memory_path(slug)

    def list_memory_slugs(self) -> list[str]:
        return self._union_slugs(lambda s: s.list_memory_slugs())

    def _union_slugs(self, get) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for s in self.stores:
            for slug in get(s):
                if slug in seen:
                    continue
                seen.add(slug)
                out.append(slug)
        return out


@contextlib.contextmanager
def acquire_flock(lock_path: Path, wait: float, timeout_message: str) -> Iterator[None]:
    """Shared flock-with-timeout primitive (extracted from `state_lock` during P5 — one
    flock-with-timeout mechanism, not two copies to keep in sync): fcntl.flock on
    `lock_path`, blocking (via a bounded poll, not a signal-based alarm) up to `wait` seconds
    before raising `LockTimeout(timeout_message)`. Crash-safe: the lock auto-releases when the
    fd closes, so a killed holder never leaves a stale lock. Used by `state_lock` below (the
    state-dir lock) and by `quintessence.write.qq_lock` (the git-tree `.qq.lock`
    transaction) — same mechanism, different lock files/messages, so it lives exactly once.
    Behavior-preserving extraction: `state_lock`'s own signature/contract/message text are
    unchanged (see tests/py/test_store.py's existing TestStateLock suite)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(timeout_message)
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def state_lock(store: Store, wait: float | None = None) -> Iterator[None]:
    """Owner-locked write path for $QQ_STATE_DIR mutations, on a DEDICATED lock file
    ($QQ_STATE_DIR/.state.lock) separate from the store's own git-tree `.qq.lock` — a findings/
    wave-off/reconcile-snapshot write never contends with a HEAD write. fcntl.flock, so it is
    crash-safe (auto-released when the fd closes) exactly like the existing qq_lock_acquire.

    `wait` seconds to block before raising LockTimeout (default: QQ_LOCK_WAIT, the same knob
    the git-tree lock uses, so both locks share one operator-facing timeout setting).
    """
    if wait is None:
        wait = float(store.config.get_int("QQ_LOCK_WAIT"))
    lock_path = store.state_dir / ".state.lock"
    with acquire_flock(lock_path, wait,
                        f"timed out after {wait:g}s waiting for the state lock ({lock_path})"):
        yield
