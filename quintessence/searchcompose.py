# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.searchcompose — RECALL COMPOSITION over a multi-store search path.

Companion to quintessence.storepath: where storepath.resolve_store_path() answers "which
stores are on the search path" for the HEAD/memory read verbs (quintessence.store.
CompositeStore), this module answers the same question for SEMANTIC recall (qq-search /
qq-ask / qq-search-mcp) — one quintessence.search.SearchIndex per store, a query embedded
ONCE and scored against every index, merged by score with a store-provenance label attached.

GOLDEN GATE (ratified by the reviewing judge, not engineered here but a hard constraint on
every caller): with NO project store on the search path (the degenerate case — e.g. running
from $HOME, or an --global/-g override), an entrypoint must NEVER import/construct anything
from this module and must instead build a bare `SearchIndex(config)` exactly as before P8 —
so degenerate output is byte-identical to pre-P8, not merely "close" (composed_search() always
tags every hit with a 'store' key; a bare SearchIndex.search() call never does, and nothing in
this module runs unless the caller has already checked `len(storepath.path) > 1`). See
qq-search / qq-ask / qq-search-mcp for the branch that keeps this true.

ASSUMPTION (flagged, not enforced): every store on a resolved search path shares the same
embed model/endpoint (QQ_EMBED_MODEL, QQ_OLLAMA_URL) — build_indexes() only ever overrides
QQ_KB_ROOT for a project store, nothing embed-related, so "embed the query once, reuse the
vector everywhere" is sound today. If a future project store ever needed a DIFFERENT embed
model, embedding-once would silently score that store's corpus with the wrong model's vector
space — this module has no guard against that because nothing in the current design lets a
project store diverge on embed config; revisit if that ever changes.

SCOPE (ratified v1): a project store's recall corpus is its own HEADs (top-level *.md,
SECTIONED — same heading-split chunking as the user store's own "quintessence" source), NOT
its `memory/` (atomic facts) or `journal/` (snapshots) — "the project store's index corpus =
the project store dir itself (its HEADs; journal/ and state are excluded)" per the ratified
design. This is narrower than the user-store corpus, which DOES include memory (kb/memory ->
QQ_MEMDIR, whole-file, shortlabel "mem") — a project's own atomic facts are not yet
semantically searchable. Flagged as a real scope gap, not an oversight: revisit in a later
phase if project-level atomic facts need recall too (P4 already gives them a home —
<project>/.quintessence/memory — recall just doesn't reach in there yet).
"""
from __future__ import annotations

import os
from typing import Optional

from .config import Config
from .search import SearchIndex
from .storepath import StoreLocation, StorePath

# The label a project store's own HEADs are filed under in composed results — DELIBERATELY the
# same "qq" label the user store's own "quintessence" source already uses (see search.py's
# _DEFAULT_SHORTLABEL): a project HEAD and a user HEAD are the same content TYPE, so `-s qq`
# keeps meaning "HEAD-type chunks" across the whole composed path, not just the user side.
PROJECT_SOURCE_LABEL = "qq"

# Excluded from a project store's own corpus (see the module docstring's SCOPE note): journal/
# and .git/ are already covered by SearchIndex's own _DEFAULT_EXCLUDE; memory/ is added here
# per-instance (NOT globally — the user-side kb/memory farm entry must stay searchable).
_PROJECT_EXTRA_EXCLUDE = ("/memory/",)


def build_indexes(storepath: StorePath, config: Config) -> "list[tuple[StoreLocation, SearchIndex]]":
    """One SearchIndex per store on `storepath`, most-specific first (mirrors storepath's own
    ordering). The user (and any 'explicit'-pinned) store's index is built from `config`
    UNCHANGED — same QQ_KB_ROOT, same D4 cache identity, same cache file as a bare
    `SearchIndex(config)` would use — so its cache is reused byte-for-byte, never rebuilt on
    account of composition existing. A project store's index is built from a SIBLING Config
    (`config.with_overrides(QQ_KB_ROOT=<project qdir>)`) with `source_roots` pointed at the
    store dir itself under the PROJECT_SOURCE_LABEL, and `/memory/` added to its exclude list
    (see module docstring's SCOPE note)."""
    out: "list[tuple[StoreLocation, SearchIndex]]" = []
    for loc in storepath.path:
        if loc.kind == "project":
            proj_config = config.with_overrides(QQ_KB_ROOT=loc.qdir)
            idx = SearchIndex(proj_config, source_roots={PROJECT_SOURCE_LABEL: loc.qdir})
            idx.exclude = idx.exclude + _PROJECT_EXTRA_EXCLUDE
        else:
            idx = SearchIndex(config)
        out.append((loc, idx))
    return out


def composed_search(indexes: "list[tuple[StoreLocation, SearchIndex]]", query: str, k: int = 6,
                     source: Optional[str] = None, min_sim: Optional[float] = None,
                     with_stats: bool = False):
    """Embed `query` ONCE (via the first index's embed config — see the module docstring's
    embed-model-parity assumption), then score that SAME vector against every index and merge
    by score, descending, truncated to the global top-`k` (not top-`k` PER store — see below
    for why taking each store's own top-k first is still globally correct). Each hit gains a
    'store' key (the owning StoreLocation.kind: 'project' | 'user' | 'explicit') — provenance
    that ONLY ever appears here, never from a bare SearchIndex.search() call (the golden gate).

    If the embedder is unreachable (embed() returns None), every store degrades to its own
    grep_fallback — matching each index's OWN degrade ladder, not a special composed one.

    `with_stats`: when True, return `(hits, {"floored": int})` — the total floored count
    across all stores, taken from each per-call return value (not the shared attribute).

    Per-store top-k is provably sufficient for a correct GLOBAL top-k: each `idx.search(k=k)`
    call already returns that store's own highest-scoring k hits in descending order; the true
    global top-k can contain at most k hits from any single store (there are only k slots
    total), and every one of THOSE would have been within that store's own top-k — so nothing
    outside each store's individually-computed top-k could ever need to appear in the merge."""
    if not indexes:
        if with_stats:
            return [], {"floored": 0}
        return []
    primary_idx = indexes[0][1]
    qv = primary_idx.embed_query(query)
    merged: list = []
    total_floored = 0
    for loc, idx in indexes:
        if qv is None:
            # grep_fallback never runs the floor, so clear any count left by an
            # earlier vector search on this index — a stale value would be summed
            # into the composed floored count and misattribute the empty result.
            idx.last_search_floored = 0
            hits = idx.grep_fallback(query, k=k, source=source)
        else:
            if with_stats:
                hits, per_stats = idx.search(query, k=k, source=source, query_vector=qv,
                                             min_sim=min_sim, with_stats=True)
                total_floored += per_stats["floored"]
            else:
                hits = idx.search(query, k=k, source=source, query_vector=qv, min_sim=min_sim)
        for h in hits:
            tagged = dict(h)
            tagged["store"] = loc.kind
            merged.append(tagged)
    merged.sort(key=lambda h: -h["score"])
    result = merged[:k]
    if with_stats:
        return result, {"floored": total_floored}
    return result


class ComposedSearchIndex:
    """Adapts a `build_indexes()` result to the single-`SearchIndex` `.search()`/`build_index()`
    interface that `quintessence.ask.Ask` and the qq-search-mcp warm-call already expect, so
    NEITHER needs any multi-store awareness of its own — they just get handed either a bare
    `SearchIndex` (degenerate case) or one of these (composed case) and can't tell the
    difference except by the 'store' key composed_search() adds to every hit."""

    def __init__(self, indexes: "list[tuple[StoreLocation, SearchIndex]]"):
        self.indexes = indexes
        self.last_search_floored: int = 0

    def search(self, query: str, k: int = 6, source: Optional[str] = None,
               min_sim: Optional[float] = None, with_stats: bool = False):
        if with_stats:
            result, stats = composed_search(self.indexes, query, k=k, source=source,
                                            min_sim=min_sim, with_stats=True)
            self.last_search_floored = stats["floored"]
            return result, stats
        result = composed_search(self.indexes, query, k=k, source=source, min_sim=min_sim)
        self.last_search_floored = sum(idx.last_search_floored for _loc, idx in self.indexes)
        return result

    def build_index(self, force: bool = False) -> list:
        """Rebuild every underlying store's index (used by qq-search's --reindex and
        qq-search-mcp's startup warm call). Return value is a list of per-store index lists —
        callers that use this only for the rebuild SIDE-EFFECT (both current callers do)."""
        return [idx.build_index(force=force) for _loc, idx in self.indexes]

    def probe_embedder(self):
        """Delegates to the primary (most-specific) store's probe — every store on the path
        shares the same embed endpoint/model (see module docstring's assumption), so any one
        index's probe answers for the whole composition."""
        if not self.indexes:
            return False, "no store on the search path"
        return self.indexes[0][1].probe_embedder()
