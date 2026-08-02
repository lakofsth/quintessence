# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.checks — L2: Tier-1 deterministic consistency checks.

Port of qq-legacy's `run_check` onto L1 objects (Store, SlugResolver, heads/memory parsing)
instead of ad-hoc grep/awk over the store tree — the same promotion P1 did for slug resolution
(D1: "one SlugResolver, imported everywhere") applied to the rest of run_check's checks. Each
`*_findings` method below is a literal-behavior port of one numbered section of run_check (see
the section comments, which name the corresponding qq-legacy line range); `tier1_findings()`
concatenates them in run_check's own emission order, because that order is what lands in the
TIER1 findings-queue section verbatim (the auto-resolve section replace is order-
preserving) and what a non-`--write` `qq check` prints.

NOT ported here (deliberate P4/P5 boundary): run_check's ONE sanctioned auto-fix, the INDEX.md
reindex + git-tree commit under the lock when `qq check --write` finds INDEX stale. That is
write-path machinery (the git-tree lock + path-scoped commit protocol qq-lib.sh owns), so it
stays P5 territory even now that P5 has landed: `index_staleness_finding()` below is
UNCHANGED — it still always returns the flag-only "[T1 index] ... run `qq reindex`" finding,
in BOTH read and write mode, keeping this module L2/read-only (L2 returns structured data;
L3 renders it). The actual auto-fix lives in `quintessence.write.index_autofix_finding` (a real
git-tree write, L0) and is wired from the `qq` dispatcher's `_cmd_check`, which substitutes that
function's result for this one's flag-only finding — by Finding identity (cls="T1 index",
kind="stale"), not position — whenever `--write` is given. See write.py's own docstring for the
substitution and its degrade-on-lock-timeout behavior.

T2 staleness-xref (quintessence.xref.Xref, absorbed in P3) and config-drift
(quintessence.reconcile.Reconcile, also P3) are NOT re-implemented here either — this module
only covers what run_check itself computed; the `qq check` dispatcher orchestrates all three
producers into one TIER1/XREF write, matching qq-legacy's own `check)` case.

RULING on an open question — "qq new's scaffold and update-on-missing-HEAD creation rule
disagree, pick one rule" — recorded here (not implemented; this is write-path territory) so the
decision doesn't have to be re-litigated when P5 ports qq-write/qq-legacy's
`new`/`update`/`rewrite` cases: today there are actually THREE distinct behaviors when a topic
doesn't exist yet, not two — `qq new <topic>` enforces the scaffold (title/`> updated: ...
(created)`/essence/RE-ENTER/Notes); `qq update <topic>` on a missing HEAD REFUSES (qq-write's
own `--prepend-update needs an existing <rel>` guard); `qq rewrite <topic>` on a missing HEAD
SILENTLY CREATES from raw stdin with ZERO scaffold enforcement (qq-write's plain/REPLACE path
has no existence check at all). The ruling: keep `qq new` as the SOLE creation entrypoint;
`rewrite` should be changed to REFUSE-and-point-at-`qq new` on a missing topic too, matching
`update`'s existing behavior — NOT the other way around (making `update`/`rewrite`
auto-create). Rationale: the HEAD scaffold (title/essence/RE-ENTER) is part of the frozen
store-format contract every other reader (menu/digest/brief/essence-lag/mem-health-adjacent
checks) assumes; silently creating an unscaffolded HEAD from a typo'd topic name in `rewrite`
is a worse failure mode (silent fragmentation into a malformed near-duplicate topic) than
requiring one extra `qq new` call. `rewrite`'s current silent-create is the wart to fix, not
`update`'s refusal.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from .config import Config
from .findings import Finding, FindingsFile
from .heads import count_update_markers, head_meta, parse as parse_head
from .slugs import SlugResolver, normalize
from .store import Store, _locale_sort_key, state_lock

# run_check's own "syntax / doc example" stoplist — tokens that appear in prose ABOUT the
# [[link]] syntax itself, not as real references. Literal port of run_check's STOP string.
_STOP_TOKENS = {"links", "link", "slug", "name", "their-name", "topic", "foo-bar", "bar"}
_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_MD_REF_RE = re.compile(r"\]\(([^)]+\.md)\)")
_ESSENCE_LAG_KEYWORDS = ("SUPERSEDE", "CORRECTION", " STALE", "NO LONGER", " WRONG",
                         "DEPRECATED", "OUTDATED")
_QUOTED_PHRASE_RE = re.compile(r"'([^']{16,})'")


# ---- shared "fresh index text" (also used by qq menu's staleness comparison + qq reindex) ---
def compute_index_text(store: Store) -> str:
    """The canonical INDEX.md content for the store's CURRENT HEADs — exact port of qq-legacy's
    `reindex_to`. Shared by quintessence.cli.render_menu (serves INDEX.md's cached content but
    regenerates it if missing) and this module's `index_staleness_finding` (the [T1 index]/
    [T1 fix] staleness diff), matching run_check's own reuse of `reindex_to` for both."""
    lines = [
        "# Quintessence — exit-note menu",
        "_Pick one to continue (`qq show <topic>`). Saving is automatic; resuming is your choice._",
        "",
        f"{'TOPIC':<26} | {'UPDATED':<20} | ESSENCE",
        f"{'-' * 26}-+-{'-' * 20}-+-------",
    ]
    for slug in store.list_head_slugs():
        text = store.read_head(slug)
        updated, essence = head_meta(text)
        lines.append(f"{slug:<26} | {updated:<20} | {essence}")
    return "\n".join(lines) + "\n"


@dataclass
class Checks:
    """Read-only Tier-1 checks over a Store. `resolver`, if not supplied, is built lazily (once)
    from the Store on first use by link_findings() — pass one in explicitly to share it with a
    caller that already built one (e.g. `qq check` alongside a link-topology tool in the same
    process)."""
    config: Config
    store: Store
    resolver: Optional[SlugResolver] = field(default=None)

    def _get_resolver(self) -> SlugResolver:
        if self.resolver is None:
            self.resolver = SlugResolver(self.store)
        return self.resolver

    # ---- 1) link integrity — [T1 link] / [T1 ambiguous] ----------------------------------
    def link_findings(self) -> list[Finding]:
        """Unresolved [[token]] tokens across memory + HEAD files (INDEX.md/RUBRIC.md excluded,
        matching run_check's own file list — MEMORY.md is NOT excluded, matching run_check,
        which scans `"$MEMDIR"/*.md "$QDIR"/*.md` verbatim). One summary finding for the
        genuinely-unresolved set (or none), PLUS (P4.5, the namespace design ruling) one
        `T1 ambiguous` finding per token whose normalized form matches more than one DISTINCT
        real file (SlugResolver.resolve_all() per-tier rules — a store-internal HEAD-vs-memory
        tie, a bare basename shared by 2+ kb docs from different source repos, or a derived/
        weak store guess contested by an independent kb doc). Ambiguous tokens are explicitly
        NOT part of the unresolved-link finding (SlugResolver already has at least one real
        candidate for them — resolve_all() is non-empty — so `is_known` would call them known;
        they were simply invisible before P4.5, not absent) and same-basename-different-tier is
        no longer reported as a collision/hygiene problem — it's this distinct, lower-severity
        class instead."""
        resolver = self._get_resolver()
        unresolved: dict[str, None] = {}   # normalized token -> None; insertion order irrelevant,
        # final list is locale-sorted below (matches run_check's `sort` over its assoc-array keys)
        ambiguous: dict[str, list] = {}    # normalized token -> its resolve_all() candidates
        paths = ([self.store.memory_path(s) for s in self.store.list_memory_slugs()]
                  + [self.store.memdir / "MEMORY.md"]
                  + [self.store.head_path(s) for s in self.store.list_head_slugs()])
        for path in paths:
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in _LINK_RE.finditer(text):
                nt = normalize(m.group(1))
                if not nt or nt in _STOP_TOKENS:
                    continue
                cands = resolver.resolve_all(nt)
                if not cands:
                    unresolved[nt] = None
                elif len(cands) > 1:
                    ambiguous.setdefault(nt, cands)
        out: list[Finding] = []
        if unresolved:
            tokens = sorted(unresolved, key=_locale_sort_key)
            out.append(Finding(cls="T1 link", fields={"tokens": tokens}))
        for tok in sorted(ambiguous, key=_locale_sort_key):
            # kb-doc candidates need their source directory in the label — two DISTINCT kb docs
            # sharing a basename (the whole point of this finding) also share `slug` (the
            # basename alone), so "kb-doc:backlog" twice would be indistinguishable; head/memory
            # candidates have no kb_source and keep the plain "source:slug" label.
            candidates = [f"{c.source}:{c.kb_source}/{c.slug}" if c.kb_source else f"{c.source}:{c.slug}"
                          for c in ambiguous[tok]]
            out.append(Finding(cls="T1 ambiguous", fields={"token": tok, "candidates": candidates}))
        return out

    # ---- 2) MEMORY.md <-> memory/*.md bijection — [T1 index] -----------------------------
    def memory_index_findings(self) -> list[Finding]:
        memindex_path = self.store.memdir / "MEMORY.md"
        if not memindex_path.is_file():
            return []
        try:
            memindex_text = memindex_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        out: list[Finding] = []
        for slug in self.store.list_memory_slugs():
            basename = f"{slug}.md"
            if f"({basename})" not in memindex_text:
                out.append(Finding(cls="T1 index", fields={"kind": "orphan", "basename": basename}))
        for ref in _MD_REF_RE.findall(memindex_text):
            if not ref:
                continue
            if not (self.store.memdir / ref).is_file():
                out.append(Finding(cls="T1 index", fields={"kind": "dangling", "ref": ref}))
        return out

    # ---- 2b) MEMORY.md structural health — [T1 mem-health] -------------------------------
    def mem_health_findings(self) -> list[Finding]:
        if not self.config.get_bool("QQ_MEM_HEALTH"):
            return []
        memindex_path = self.store.memdir / "MEMORY.md"
        if not memindex_path.is_file():
            return []
        try:
            text = memindex_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        out: list[Finding] = []
        max_bytes = self.config.get_int("QQ_MEMINDEX_MAX_BYTES")
        max_chars = self.config.get_int("QQ_MEMLINE_MAX_CHARS")
        nbytes = os.path.getsize(memindex_path)
        if nbytes >= max_bytes:
            out.append(Finding(cls="T1 mem-health",
                                fields={"kind": "size", "bytes": nbytes, "max_bytes": max_bytes}))
        over: list[tuple[str, int]] = []
        for line in text.split("\n"):
            if not _MD_REF_RE.search(line):
                continue
            if len(line) > max_chars:
                label = re.sub(r"^\s*-\s*\[", "", line)
                label = re.sub(r"\].*", "", label)
                over.append((label, len(line)))
        if over:
            out.append(Finding(cls="T1 mem-health", fields={"kind": "line", "over": over,
                                                              "max_chars": max_chars}))
        # dup check: EVERY `](ref.md)` occurrence across the whole file (grep -oE is per-match,
        # not per-line — a line with two such refs contributes two occurrences), matching
        # `grep -oE ... | sort | uniq -d`.
        all_refs = _MD_REF_RE.findall(text)
        dups = sorted({r for r in all_refs if all_refs.count(r) > 1}, key=_locale_sort_key)
        if dups:
            out.append(Finding(cls="T1 mem-health", fields={"kind": "dup", "dups": dups}))
        return out

    # ---- 3) HEAD meta presence — [T1 head] ------------------------------------------------
    def head_meta_findings(self) -> list[Finding]:
        out: list[Finding] = []
        for slug in self.store.list_head_slugs():
            text = self.store.read_head(slug)
            updated, essence = head_meta(text)
            if not updated:
                out.append(Finding(cls="T1 head", fields={"kind": "missing-updated", "head": slug}))
            if not essence:
                out.append(Finding(cls="T1 head", fields={"kind": "missing-essence", "head": slug}))
        return out

    # ---- 4) memory frontmatter validity — [T1 fm] -----------------------------------------
    def memory_frontmatter_findings(self) -> list[Finding]:
        """Literal port of run_check's own regexes over the WHOLE raw file (`grep -q '^name:'`
        etc.) rather than quintessence.memory.MemoryFact's frontmatter-scoped accessors — kept
        deliberately loose/matching, not routed through MemoryFact, so a malformed memory file
        (missing its `---` fences entirely) is diagnosed the same way run_check would."""
        out: list[Finding] = []
        name_re = re.compile(r"^name:", re.M)
        desc_re = re.compile(r"^description:", re.M)
        type_re = re.compile(r"^[ \t]*type:", re.M)
        for slug in self.store.list_memory_slugs():
            text = self.store.read_memory(slug)
            if not name_re.search(text):
                out.append(Finding(cls="T1 fm", fields={"kind": "missing-name", "basename": slug}))
            if not desc_re.search(text):
                out.append(Finding(cls="T1 fm", fields={"kind": "missing-description", "basename": slug}))
            if not type_re.search(text):
                out.append(Finding(cls="T1 fm", fields={"kind": "missing-type", "basename": slug}))
        return out

    # ---- 5) HEAD bloat — [T1 size] ---------------------------------------------------------
    def head_size_findings(self) -> list[Finding]:
        out: list[Finding] = []
        hard_bytes = self.config.get_int("QQ_COMPACT_HARD_BYTES")
        hard_lines = self.config.get_int("QQ_COMPACT_HARD_LINES")
        body_hard_bytes = self.config.get_int("QQ_BODY_HARD_BYTES")
        for slug in self.store.list_head_slugs():
            path = self.store.head_path(slug)
            text = self.store.read_head(slug)
            sz = os.path.getsize(path)
            ul = count_update_markers(text)
            ulb = parse_head(text).update_line_region_bytes
            body = sz - ulb
            if ulb > hard_bytes or ul > hard_lines:
                out.append(Finding(cls="T1 size", fields={
                    "head": slug, "kind": "update-lines", "kb": ulb // 1024, "lines": ul}))
            elif body > body_hard_bytes:
                out.append(Finding(cls="T1 size", fields={
                    "head": slug, "kind": "body", "kb": body // 1024}))
        return out

    # ---- 6) essence-lag — [T1 essence-lag] -------------------------------------------------
    def essence_lag_findings(self) -> list[Finding]:
        if not self.config.get_bool("QQ_ESSENCE_LAG"):
            return []
        out: list[Finding] = []
        for slug in self.store.list_head_slugs():
            text = self.store.read_head(slug)
            _, essence = head_meta(text)
            if not essence:
                continue
            flagged = False
            for line in text.split("\n"):
                if flagged:
                    break
                if not line.startswith("> updated:"):
                    continue
                umark = line.upper()
                if not any(kw in umark for kw in _ESSENCE_LAG_KEYWORDS):
                    continue
                for phrase in _QUOTED_PHRASE_RE.findall(line):
                    if phrase in essence:
                        out.append(Finding(cls="T1 essence-lag",
                                            fields={"head": slug, "phrase": phrase}))
                        flagged = True
                        break
        return out

    # ---- 7) INDEX staleness — [T1 index]/kind=stale ----------------------------------------
    def index_staleness_finding(self) -> list[Finding]:
        """Flag-only in this port — see the module docstring for why the git-tree
        reindex-and-commit auto-fix (legacy `qq check --write`'s ONE sanctioned auto-fix) is
        NOT performed here (P5 boundary, flagged for review)."""
        fresh = compute_index_text(self.store)
        try:
            current = self.store.read_index()
        except OSError:
            current = None
        if current != fresh:
            return [Finding(cls="T1 index", fields={"kind": "stale"})]
        return []

    # ---- run_check's own emission order -----------------------------------------------------
    def tier1_findings(self) -> list[Finding]:
        """All of run_check's fast, deterministic findings, concatenated in run_check's own
        emission order (1 link [now T1 link + P4.5's T1 ambiguous, both from link_findings()],
        2 index-bijection, 2b mem-health, 3 head-meta, 4 frontmatter, 5 size, 6 essence-lag,
        7 index-staleness). Does NOT include config-drift (a separate
        producer — quintessence.reconcile.Reconcile — merged into the same TIER1 section by the
        caller, matching qq-legacy's `check)` case) or T2 xref (the slow section)."""
        out: list[Finding] = []
        out += self.link_findings()
        out += self.memory_index_findings()
        out += self.mem_health_findings()
        out += self.head_meta_findings()
        out += self.memory_frontmatter_findings()
        out += self.head_size_findings()
        out += self.essence_lag_findings()
        out += self.index_staleness_finding()
        return out


# ---- state-dir persistence (`qq check --write`) ------------------------------------------
def persist_findings(store: Store, tier1_lines: list[str],
                      xref_lines: Optional[list[str]] = None) -> None:
    """Write `qq check --write`'s findings to the state-dir pending-findings.md queue, under
    the DEDICATED state-dir lock (`quintessence.store.state_lock`). TIER1 is always
    full-replaced (T1 + config-drift, matching qq-legacy's `check --write` merging
    config-reconcile's output into the same section before persisting). XREF is full-replaced
    ONLY when `xref_lines` is not None — a skipped (`--fast`/`--no-xref`) or FAILED xref run
    passes None, leaving the section untouched, matching qq-legacy's own "on FAILURE the XREF
    section is left untouched" contract (a transient embedder outage can't silently clear real
    findings)."""
    path = os.path.join(store.config.get_path("QQ_STATE_DIR"), "pending-findings.md")
    with state_lock(store):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                ff = FindingsFile.parse(fh.read())
        except OSError:
            ff = FindingsFile()
        ff.set_section("TIER1", tier1_lines)
        if xref_lines is not None:
            ff.set_section("XREF", xref_lines)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(ff.serialize())
        os.replace(tmp, path)
