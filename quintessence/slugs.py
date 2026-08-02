# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.slugs — L1: SlugResolver, THE link/slug resolution semantics.

Promoted verbatim from `qq run_check`'s KNOWN-set (see `qq`'s run_check function, the
"known-slug set (normalized)" block) — one SlugResolver, imported everywhere. It exists
because a second tool diverged from the first: `tools/topology_scan.py` reimplemented
filenames-only resolution and produced 144 false findings that `run_check`'s richer
resolution did not, because run_check's semantics were never extracted into anything a
second tool could import. run_check's behaviour IS the contract this module ports, not a
design invented here — see the flat-KNOWN-set block quoted in this
module's git history for the original one-namespace shape.

PER-TIER NAMESPACE (P4.5 design ruling, 2026-07-03 — "global namespace is a pain, arguably
a bug"): the flat KNOWN-set above treated HEAD basenames, memory basenames/`name:`
fields, AND kb-doc basenames as ONE namespace, so two legitimately-named files living in
different repos (a HEAD `roadmap` and docs/roadmap.md; docs/backlog.md and
infra/backlog.md; docs/README.md and infra/README.md — all realistic collisions) silently
tied at the same normalized key. A tie there was invisible (resolve() returned None, but
is_known() — the only thing run_check's port, checks.link_findings, ever asked — was already
True, so the ambiguity was never surfaced) OR, worse, got reported as store-hygiene/collision
noise indistinguishable from an actually-broken link. Both are wrong: same-basename-different-
repo is not a defect to rename away, and "silently invisible" is not "resolved."

THE FIX — two separate namespaces, not one flat dict:
  STORE  (HEAD slugs + memory basenames + memory `name:` fields) — these genuinely share one
         namespace (a HEAD and a memory really can collide on the SAME meaning of a bare
         [[slug]]), so store resolution is computed exactly as before (strength-tiered: exact
         identity beats a prefix-stripped derivation) and, when it yields a SINGLE exact-
         identity (strength 0) candidate, that answer is AUTHORITATIVE — final, no kb
         consultation at all. This is what "dissolves" the roadmap-style case: a HEAD literally
         named `roadmap` IS what a bare [[roadmap]] means, full stop, regardless of whatever
         else in the kb tree happens to share that basename. A genuine STORE-internal tie (a
         HEAD and a memory both hitting the SAME best tier) is still a real ambiguity — that
         was already true before this change (resolve() already returned None for it); the
         change is that it now gets its OWN reported Finding class instead of silent invisibility.
  KB-DOC (every *.md at depth 2 under QQ_KB_ROOT) — a separate namespace. A bare basename that
         matches ONLY kb docs resolves if unique across kb sources (i.e. exactly one DISTINCT
         real file, symlink-farm mirrors deduped as before); 2+ distinct kb docs (different
         source repos) sharing a basename is a genuine cross-repo collision, reported as an
         ambiguity, never silently resolved to a first-seen winner.
  CONTESTED MIDDLE — store's OWN best hit is not an exact identity but only a DERIVED/weak
         guess (the prefix-stripped tier — e.g. `[[device_thing]]` matching a memory via its
         un-prefixed form, not its actual filename): that guess is not "authoritative" the way
         an exact identity is, so if a kb doc ALSO independently matches the same basename, the
         two are put up against each other as an ambiguity rather than letting the weaker store
         guess win by default. Judgment call (flagged for review): this is the reading that
         reconciles "store resolution is authoritative and unambiguous" (true for a store's own
         EXACT identity hits) with "a store item AND kb docs matching is a new ambiguous-
         reference Finding, not silently resolved" (true whenever the store side of that pairing
         is only a derived guess) — see the P4.5 build report for the alternative readings
         considered and why this one was picked.
  QUALIFIED PATH — a `[[source/basename]]` token (kb source directory + basename, `.md`
         optional) resolves directly against that exact kb file, bypassing all ambiguity
         machinery — the mechanism for a caller who already knows which of several same-named
         kb docs they mean, without renaming either side.

normalize(): the exact `_norm()` from run_check — lowercase + underscore->hyphen (tr, ASCII
only, matching the C-locale `tr '[:upper:]_' '[:lower:]-'`), then trim surrounding whitespace.

Candidates are deduplicated by realpath — the KB corpus root is commonly a symlink farm, so a
HEAD and its identical kb-doc mirror must count as ONE candidate, not a false collision. Dedup
is scoped PER NAMESPACE (store vs kb), matching the two separate `_known` dicts below.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .memory import parse as parse_memory
from .store import Store, _locale_sort_key

_PREFIXES = ("feedback", "project", "reference", "user", "session-state")
_PREFIX_RE = re.compile(r"^(" + "|".join(_PREFIXES) + r")-")

_STORE = "store"
_KB = "kb"


def normalize(token: str) -> str:
    """Exact port of run_check's `_norm`: `tr '[:upper:]_' '[:lower:]-'` (ASCII upper->lower,
    underscore->hyphen, single pass) then trim surrounding whitespace."""
    out = []
    for ch in token:
        if ch == "_":
            out.append("-")
        elif "A" <= ch <= "Z":
            out.append(chr(ord(ch) + 32))
        else:
            out.append(ch)
    return "".join(out).strip()


def strip_type_prefix(normalized: str) -> str:
    """Drop ONE leading (feedback|project|reference|user|session-state)- prefix, matching
    run_check's `sed -E 's/^(feedback|project|...)-//' `. A no-op if no prefix matches."""
    return _PREFIX_RE.sub("", normalized, count=1)


@dataclass(frozen=True)
class SlugMatch:
    source: str    # "head" | "memory" | "kb-doc"
    slug: str      # the real, un-normalized basename (without .md)
    path: Path
    kb_source: Optional[str] = None   # kb-doc only: the top-level dir name under kb_root this
                                       # file lives in (e.g. "docs", "infra") — what makes two
                                       # same-basename kb docs "different sources" for ambiguity
                                       # purposes. Always None for head/memory matches.


class SlugResolver:
    """Build once from a Store (+ optional kb_root override); resolve() is then a pure
    in-memory lookup. Read-only — never touches the write path."""

    def __init__(self, store: Store, kb_root: Path | str | None = None,
                 include_kb_docs: bool = True):
        self.store = store
        self.kb_root = Path(kb_root) if kb_root is not None else Path(
            store.config.get_path("QQ_KB_ROOT"))
        # Two SEPARATE per-tier namespaces (see module docstring's "THE FIX"). Each maps a
        # normalized key -> [(strength, SlugMatch), ...]. strength 0 = EXACT identity (a file's
        # own filename, a memory's own `name:` frontmatter field, a kb-doc's own basename) —
        # what the file itself claims to be called. strength 1 = DERIVED (the type-prefix-
        # stripped filename variant) — a deliberately weaker heuristic fallback, never meant to
        # outrank an exact identity match WITHIN its own namespace, and — new in P4.5 — not
        # authoritative enough to silently beat an independent kb-doc match either (see
        # resolve_all's "contested middle" branch).
        self._store_known: dict[str, list[tuple[int, SlugMatch]]] = {}
        self._kb_known: dict[str, list[tuple[int, SlugMatch]]] = {}
        self._seen_realpaths: dict[tuple[str, str], set[str]] = {}
        self._build(include_kb_docs)

    # ---- construction ------------------------------------------------------------------
    def _add(self, namespace: str, key: str, match: SlugMatch, strength: int = 0) -> None:
        if not key:
            return
        rp = str(match.path.resolve()) if match.path else f"{match.source}:{match.slug}"
        seen = self._seen_realpaths.setdefault((namespace, key), set())
        if rp in seen:
            return   # same underlying file already a candidate for this key (symlink-farm dup)
        seen.add(rp)
        target = self._store_known if namespace == _STORE else self._kb_known
        target.setdefault(key, []).append((strength, match))

    def _build(self, include_kb_docs: bool) -> None:
        for slug in self.store.list_memory_slugs():
            path = self.store.memory_path(slug)
            nf = normalize(slug)
            self._add(_STORE, nf, SlugMatch("memory", slug, path), strength=0)
            stripped = strip_type_prefix(nf)
            self._add(_STORE, stripped, SlugMatch("memory", slug, path), strength=1)
            try:
                fact = parse_memory(path.read_text(encoding="utf-8", errors="replace"))
                if fact.name:
                    self._add(_STORE, normalize(fact.name), SlugMatch("memory", slug, path), strength=0)
            except OSError:
                pass

        for slug in self.store.list_head_slugs():
            path = self.store.head_path(slug)
            self._add(_STORE, normalize(slug), SlugMatch("head", slug, path), strength=0)

        if include_kb_docs and self.kb_root.is_dir():
            # P2 caught the same class of bug in Store.list_head_slugs/list_memory_slugs: plain
            # sorted() is codepoint order, but run_check's own bash glob (`for f in
            # "${QQ_KB_ROOT:-$HOME/kb}"/*/*.md`) is LOCALE-collated (LC_COLLATE) — see
            # store._locale_sort_key's docstring for the concrete en_US.UTF-8 example
            # ("selfhood-two-bootstraps" vs "self-hosted-claude-code"). This resolver's own
            # construction order doesn't change WHICH candidates exist (that's a dict keyed by
            # normalized name, order-independent for correctness), but _add()'s de-dup-by-realpath
            # means the FIRST path seen for a given key wins ties silently — so glob order is
            # still observable behavior here, not just cosmetic, and must match bash's. Sort key
            # is the RELATIVE "subdir/file.md" path (not just the basename): bash's own
            # `*/*.md` expansion is two glob components, and while its cross-directory tie-break
            # is an implementation detail this doesn't chase, sorting the full relative path
            # reduces to a basename sort whenever two candidates share a directory (the common,
            # tie-break-relevant case) and stays a defensible approximation otherwise.
            for path in sorted(self.kb_root.glob("*/*.md"),
                                key=lambda p: _locale_sort_key(str(p.relative_to(self.kb_root)))):
                if not path.is_file():
                    continue
                rel = path.relative_to(self.kb_root)
                kb_source = rel.parts[0] if len(rel.parts) > 1 else None
                self._add(_KB, normalize(path.stem),
                          SlugMatch("kb-doc", path.stem, path, kb_source=kb_source), strength=0)

    # ---- qualified-path kb resolution ----------------------------------------------------
    @staticmethod
    def _split_qualified(token: str) -> Optional[tuple[str, str]]:
        """A `source/basename` token (kb source directory + basename; a trailing `.md` is
        tolerated) — the escape hatch for a caller who already knows which of several same-
        named kb docs they mean. None if `token` isn't in that shape (no "/", or more than one)."""
        t = token.strip()
        if t.endswith(".md"):
            t = t[:-3]
        parts = [p for p in t.split("/") if p]
        if len(parts) != 2:
            return None
        return normalize(parts[0]), normalize(parts[1])

    def _resolve_qualified(self, token: str) -> Optional[SlugMatch]:
        split = self._split_qualified(token)
        if split is None:
            return None
        source, base = split
        # kb source directory names aren't normalized on disk (they're real directory names),
        # but every real one in this deployment is already lowercase-hyphenated, so comparing
        # against normalize(dirname) costs nothing and buys case/underscore tolerance for free.
        for path in self.kb_root.glob("*/*.md") if self.kb_root.is_dir() else []:
            rel = path.relative_to(self.kb_root)
            if len(rel.parts) != 2:
                continue
            if normalize(rel.parts[0]) == source and normalize(path.stem) == base and path.is_file():
                return SlugMatch("kb-doc", path.stem, path, kb_source=rel.parts[0])
        return None

    @staticmethod
    def _is_qualified(token: str) -> bool:
        return "/" in token.strip()

    # ---- resolution ---------------------------------------------------------------------
    def is_known(self, token: str) -> bool:
        """membership test matching run_check's KNOWN[...] check (used for the [T1 link]
        unresolved-link finding) — true if at least one candidate exists at ANY strength, in
        EITHER namespace, ambiguity/tiering aside (run_check's flat KNOWN set never distinguished
        strength or namespace for this purpose; only resolve()/resolve_all() do)."""
        if self._is_qualified(token):
            return self._resolve_qualified(token) is not None
        key = normalize(token)
        return bool(self._store_known.get(key)) or bool(self._kb_known.get(key))

    def resolve_all(self, token: str, best_tier_only: bool = True) -> list[SlugMatch]:
        """Candidates for `token`, per the per-tier rules (module docstring's "THE FIX"):

          qualified (`source/basename`) -> that exact kb file, or [] if it doesn't exist (no
              fuzzy fallback — a qualified reference means exactly what it says).
          store's best tier is EXACT (strength 0) -> store wins outright; kb is not even
              consulted. A single such candidate resolves cleanly (roadmap-style "dissolved");
              MULTIPLE distinct exact store candidates (a HEAD and a memory both hitting
              strength 0) is a genuine store-internal ambiguity, returned as-is (kb still not
              consulted — that tie is real regardless of what kb also contains).
          store's best tier is DERIVED (strength 1, no exact hit) -> a weaker guess, put up
              against any kb candidates at kb's own best tier: no kb candidates -> the derived
              guess resolves alone (unchanged from pre-P4.5 behaviour, e.g. a `feedback-`-
              prefixed memory linked by its bare topic); kb candidates present -> combined list
              (ambiguous unless it collapses to exactly one candidate).
          no store candidates at all -> pure kb-tier resolution: kb's best-tier candidates,
              unique across kb sources -> resolves; 2+ distinct real files -> ambiguous.

        With best_tier_only=False, every candidate in both namespaces is returned regardless of
        tier or the above short-circuiting (diagnostic use — "show me literally everything a
        token could conceivably mean")."""
        if self._is_qualified(token):
            m = self._resolve_qualified(token)
            return [m] if m else []
        key = normalize(token)
        store_cands = self._store_known.get(key, [])
        kb_cands = self._kb_known.get(key, [])
        if not best_tier_only:
            return [m for _, m in store_cands] + [m for _, m in kb_cands]
        if store_cands:
            best = min(s for s, _ in store_cands)
            store_best = [m for s, m in store_cands if s == best]
            if best == 0:
                return store_best   # exact store identity: authoritative, kb not consulted
            if kb_cands:
                kb_best_s = min(s for s, _ in kb_cands)
                return store_best + [m for s, m in kb_cands if s == kb_best_s]
            return store_best
        if kb_cands:
            best = min(s for s, _ in kb_cands)
            return [m for s, m in kb_cands if s == best]
        return []

    def resolve(self, token: str) -> SlugMatch | None:
        """The unique BEST-TIER match for `token`, or None if unresolved OR genuinely
        ambiguous (more than one DISTINCT real file ties, per resolve_all's per-tier rules —
        a real naming collision, not the symlink-farm dedup case). Callers that want to see and
        report an ambiguity themselves should use resolve_all()."""
        cands = self.resolve_all(token)
        return cands[0] if len(cands) == 1 else None
