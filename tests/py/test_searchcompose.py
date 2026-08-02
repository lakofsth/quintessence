# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.searchcompose (recall composition over a multi-store
search path): build_indexes() constructs one SearchIndex per store (a project store's own
top-level HEADs indexed, memory/journal excluded, the user store byte-identical to a bare
SearchIndex(config)); composed_search() embeds the query ONCE and merges by score with a
'store' provenance tag; ComposedSearchIndex adapts that to the single-index interface Ask and
the qq-search-mcp warm call expect."""
import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence.config import Config
from quintessence.search import SearchIndex
from quintessence.storepath import StoreLocation, StorePath
from quintessence.searchcompose import (
    PROJECT_SOURCE_LABEL,
    build_indexes,
    composed_search,
    ComposedSearchIndex,
)


class FakeEmbedder:
    """Deterministic stand-in for SearchIndex.embed (same shape as test_search.py's) — equal
    text -> equal vector, no network."""
    def __init__(self):
        self.calls = 0

    def __call__(self, text, prefix):
        self.calls += 1
        h = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        return [float(h % 997) / 997.0, 1.0]


def make_config(base: str, **overrides) -> Config:
    over = {"QQ_KB_ROOT": os.path.join(base, "kb"),
            "QQ_CACHE": os.path.join(base, "cache", "embeddings.json"),
            "QQ_EMBED_MODEL": "qwen3-embedding:0.6b"}
    over.update(overrides)
    return Config(env={}, config_file="/nonexistent", overrides=over)


def write_user_corpus(base: str, text: str = "user sentinel content") -> None:
    d = os.path.join(base, "kb", "docs")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "u.md"), "w") as f:
        f.write(f"# u\n{text}\n")


def write_project_store(base: str, text: str = "project sentinel content") -> str:
    proj = os.path.join(base, "work", "repo", ".quintessence")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "ptopic.md"), "w") as f:
        f.write(f"# ptopic\n{text}\n")
    # journal/ and memory/ must never leak into the corpus (P8 scope)
    os.makedirs(os.path.join(proj, "journal", "ptopic"), exist_ok=True)
    with open(os.path.join(proj, "journal", "ptopic", "snap.md"), "w") as f:
        f.write("journal-only-sentinel, must never surface in recall\n")
    os.makedirs(os.path.join(proj, "memory"), exist_ok=True)
    with open(os.path.join(proj, "memory", "fact.md"), "w") as f:
        f.write("memory-only-sentinel, must never surface in recall (v1 scope)\n")
    return proj


def make_storepath(base: str, proj_qdir: str, config: Config) -> StorePath:
    user_qdir = config.get_path("QUINTESSENCE_DIR")
    return StorePath([StoreLocation(proj_qdir, "project"), StoreLocation(user_qdir, "user")])


class TestBuildIndexes(unittest.TestCase):
    def test_one_index_per_store_same_order(self):
        with tempfile.TemporaryDirectory() as base:
            config = make_config(base)
            proj = write_project_store(base)
            sp = make_storepath(base, proj, config)
            indexes = build_indexes(sp, config)
            self.assertEqual(len(indexes), 2)
            self.assertEqual([loc.kind for loc, _ix in indexes], ["project", "user"])

    def test_project_index_uses_source_roots_at_its_own_qdir(self):
        with tempfile.TemporaryDirectory() as base:
            config = make_config(base)
            proj = write_project_store(base)
            sp = make_storepath(base, proj, config)
            (_ploc, pidx), (_uloc, uidx) = build_indexes(sp, config)
            self.assertEqual(pidx.source_roots, {PROJECT_SOURCE_LABEL: proj})
            self.assertEqual(pidx.kb_root, proj)
            self.assertIsNone(uidx.source_roots)

    def test_project_index_excludes_memory_in_addition_to_defaults(self):
        with tempfile.TemporaryDirectory() as base:
            config = make_config(base)
            proj = write_project_store(base)
            sp = make_storepath(base, proj, config)
            (_ploc, pidx), (_uloc, uidx) = build_indexes(sp, config)
            self.assertIn("/memory/", pidx.exclude)
            self.assertNotIn("/memory/", uidx.exclude)   # the user kb/memory farm entry stays searchable

    def test_user_index_is_byte_identical_config_to_a_bare_searchindex(self):
        """The user store's index must reuse whatever cache a bare SearchIndex(config) would —
        same cache_path, same D4 identity — composition existing must never force a rebuild."""
        with tempfile.TemporaryDirectory() as base:
            config = make_config(base)
            proj = write_project_store(base)
            sp = make_storepath(base, proj, config)
            (_ploc, _pidx), (_uloc, uidx) = build_indexes(sp, config)
            bare = SearchIndex(config)
            self.assertEqual(uidx.cache_path, bare.cache_path)
            self.assertEqual(uidx.kb_root, bare.kb_root)

    def test_project_and_user_indexes_have_distinct_cache_identities(self):
        with tempfile.TemporaryDirectory() as base:
            config = make_config(base)
            proj = write_project_store(base)
            sp = make_storepath(base, proj, config)
            (_ploc, pidx), (_uloc, uidx) = build_indexes(sp, config)
            self.assertNotEqual(pidx.cache_path, uidx.cache_path)


class TestProjectCorpusShape(unittest.TestCase):
    """Exercises the real chunks()/build_index() pipeline against a REAL store-dir layout
    (top-level HEAD .md + journal/ + memory/ + nothing else) — pins the P8 scope decision:
    a project store's recall corpus is its HEADs only, journal/memory excluded."""

    def test_project_head_indexed_journal_and_memory_excluded(self):
        with tempfile.TemporaryDirectory() as base:
            config = make_config(base)
            proj = write_project_store(base)
            sp = make_storepath(base, proj, config)
            (_ploc, pidx), (_uloc, _uidx) = build_indexes(sp, config)
            texts = [text for *_junk, text in pidx.chunks()]
            joined = "\n".join(texts)
            self.assertIn("project sentinel content", joined)
            self.assertNotIn("journal-only-sentinel", joined)
            self.assertNotIn("memory-only-sentinel", joined)


class TestComposedSearch(unittest.TestCase):
    def test_embeds_query_exactly_once_across_stores(self):
        with tempfile.TemporaryDirectory() as base:
            config = make_config(base)
            proj = write_project_store(base)
            write_user_corpus(base)
            sp = make_storepath(base, proj, config)
            indexes = build_indexes(sp, config)
            for _loc, idx in indexes:
                idx.embed = FakeEmbedder()
            for _loc, idx in indexes:
                idx.build_index()

            call_counts = {}
            for _loc, idx in indexes:
                orig = idx.embed_query
                def counting(query, _idx=idx, _orig=orig):
                    call_counts[id(_idx)] = call_counts.get(id(_idx), 0) + 1
                    return _orig(query)
                idx.embed_query = counting

            composed_search(indexes, "sentinel", k=5)
            self.assertEqual(sum(call_counts.values()), 1,
                              "the query must be embedded exactly ONCE across all composed stores")

    def test_results_carry_the_correct_store_label(self):
        with tempfile.TemporaryDirectory() as base:
            config = make_config(base)
            proj = write_project_store(base, text="unique-project-marker text")
            write_user_corpus(base, text="unique-user-marker text")
            sp = make_storepath(base, proj, config)
            indexes = build_indexes(sp, config)
            for _loc, idx in indexes:
                idx.embed = FakeEmbedder()

            hits = composed_search(indexes, "marker", k=10)
            by_store = {h["store"] for h in hits}
            self.assertEqual(by_store, {"project", "user"})
            project_paths = [h["path"] for h in hits if h["store"] == "project"]
            user_paths = [h["path"] for h in hits if h["store"] == "user"]
            self.assertTrue(any("ptopic" in p for p in project_paths))
            self.assertTrue(any("u.md" in p for p in user_paths))

    def test_merges_globally_by_score_not_per_store(self):
        with tempfile.TemporaryDirectory() as base:
            config = make_config(base)
            proj = write_project_store(base)
            write_user_corpus(base)
            sp = make_storepath(base, proj, config)
            indexes = build_indexes(sp, config)
            for _loc, idx in indexes:
                idx.embed = FakeEmbedder()

            hits = composed_search(indexes, "sentinel content", k=1)
            self.assertEqual(len(hits), 1)
            # the single global-top hit must be the highest score of the UNION, not an
            # arbitrary per-store pick -- assert it's actually the max over an uncapped pull.
            uncapped = composed_search(indexes, "sentinel content", k=10)
            self.assertEqual(hits[0]["score"], max(h["score"] for h in uncapped))

    def test_embedder_down_falls_back_to_grep_for_every_store(self):
        with tempfile.TemporaryDirectory() as base:
            config = make_config(base)
            proj = write_project_store(base, text="xyzzy-grep-marker")
            write_user_corpus(base, text="xyzzy-grep-marker")
            sp = make_storepath(base, proj, config)
            indexes = build_indexes(sp, config)
            for _loc, idx in indexes:
                idx.embed = lambda text, prefix: None   # embedder unreachable

            hits = composed_search(indexes, "xyzzy-grep-marker", k=10)
            self.assertTrue(hits)
            self.assertTrue(all(h.get("degraded") for h in hits))
            self.assertEqual({h["store"] for h in hits}, {"project", "user"})

    def test_embedder_down_clears_stale_floored_count(self):
        # A vector search may leave last_search_floored set on an index; a later
        # composed call that degrades to grep must not sum that stale value into
        # its own floored count and misattribute an empty keyword result.
        with tempfile.TemporaryDirectory() as base:
            config = make_config(base)
            proj = write_project_store(base, text="xyzzy-grep-marker")
            write_user_corpus(base, text="xyzzy-grep-marker")
            sp = make_storepath(base, proj, config)
            indexes = build_indexes(sp, config)
            for _loc, idx in indexes:
                idx.embed = lambda text, prefix: None   # embedder unreachable
                idx.last_search_floored = 3             # stale from an earlier search

            composed_search(indexes, "xyzzy-grep-marker", k=10)
            for _loc, idx in indexes:
                self.assertEqual(idx.last_search_floored, 0)

    def test_empty_indexes_returns_empty(self):
        self.assertEqual(composed_search([], "anything"), [])

    def test_source_filter_applies_per_store(self):
        with tempfile.TemporaryDirectory() as base:
            config = make_config(base)
            proj = write_project_store(base)
            write_user_corpus(base)
            sp = make_storepath(base, proj, config)
            indexes = build_indexes(sp, config)
            for _loc, idx in indexes:
                idx.embed = FakeEmbedder()
            # both the project HEAD and the user 'docs' entry chunk under different labels;
            # project HEADs are filed under PROJECT_SOURCE_LABEL ("qq") -- filter to "docs" and
            # the project store must contribute nothing.
            hits = composed_search(indexes, "sentinel content", k=10, source="docs")
            self.assertTrue(all(h["store"] == "user" for h in hits))


class TestComposedSearchIndexAdapter(unittest.TestCase):
    def test_search_delegates_to_composed_search(self):
        with tempfile.TemporaryDirectory() as base:
            config = make_config(base)
            proj = write_project_store(base)
            write_user_corpus(base)
            sp = make_storepath(base, proj, config)
            indexes = build_indexes(sp, config)
            for _loc, idx in indexes:
                idx.embed = FakeEmbedder()
            composite = ComposedSearchIndex(indexes)
            hits = composite.search("sentinel content", k=5)
            self.assertTrue(hits)
            self.assertTrue(all("store" in h for h in hits))

    def test_build_index_rebuilds_every_underlying_store(self):
        with tempfile.TemporaryDirectory() as base:
            config = make_config(base)
            proj = write_project_store(base)
            write_user_corpus(base)
            sp = make_storepath(base, proj, config)
            indexes = build_indexes(sp, config)
            for _loc, idx in indexes:
                idx.embed = FakeEmbedder()
            composite = ComposedSearchIndex(indexes)
            composite.build_index()
            for _loc, idx in indexes:
                self.assertIsNotNone(idx._index)

    def test_probe_embedder_delegates_to_primary(self):
        with tempfile.TemporaryDirectory() as base:
            config = make_config(base)
            proj = write_project_store(base)
            write_user_corpus(base)
            sp = make_storepath(base, proj, config)
            indexes = build_indexes(sp, config)
            called = {"n": 0}
            def stub():
                called["n"] += 1
                return True, "ok"
            indexes[0][1].probe_embedder = stub
            composite = ComposedSearchIndex(indexes)
            ok, detail = composite.probe_embedder()
            self.assertTrue(ok)
            self.assertEqual(called["n"], 1)

    def test_probe_embedder_on_empty_indexes(self):
        composite = ComposedSearchIndex([])
        ok, detail = composite.probe_embedder()
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
