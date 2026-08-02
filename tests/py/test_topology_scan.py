# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for tools/topology_scan.py's P4.5 additions: the stdlib-only greedy
agglomerative chunk clustering (`cluster_chunks`, the new STRUCTURE section's engine),
`--sample-k` argument parsing, and `head_vectors`' sample_k=0 (unsampled) vs sample_k>0
(degraded/sampled) chunk selection — exercised against a FAKE SearchIndex-like stub so no
embedder/network call is needed. Does not touch the real corpus or run a live embed."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools"))

import topology_scan as ts  # noqa: E402


def unit(*coords):
    n = math.sqrt(sum(c * c for c in coords))
    return [c / n for c in coords]


class TestClusterChunks(unittest.TestCase):
    def test_two_tight_orthogonal_groups_split_into_two_clusters(self):
        # group A: near (1,0,0); group B: near (0,1,0) — orthogonal, so cosine(A,B) ~ 0, well
        # under the default stop_sim, and each group is internally near-identical (cosine ~1).
        # max_clusters=2 forces the merge all the way down to the two real groups (the default
        # max_clusters=3 would otherwise leave one group split into two singletons, since
        # nothing stops it from continuing to "merge" toward the ceiling — see the
        # do-not-over-split test below for that boundary case in isolation).
        vecs = [unit(1, 0.02, 0), unit(1, -0.01, 0), unit(0.99, 0.01, 0),
                unit(0, 1, 0.02), unit(0.01, 1, -0.02), unit(-0.01, 0.99, 0)]
        clusters = ts.cluster_chunks(vecs, cosine=ts.SearchIndex.cosine, max_clusters=2)
        self.assertEqual(len(clusters), 2)
        # first-appearance order preserved: cluster containing index 0 comes first
        self.assertIn(0, clusters[0])
        self.assertIn(3, clusters[1])
        self.assertEqual(set(clusters[0]), {0, 1, 2})
        self.assertEqual(set(clusters[1]), {3, 4, 5})

    def test_uniformly_similar_chunks_do_not_over_split(self):
        """All chunks already cohesive (every pairwise cosine well above stop_sim) — with
        max_clusters=1 (the only thing stopping a full merge is stop_sim), clustering should
        merge all the way down to a single cluster rather than stopping partway: there is no
        genuine boundary anywhere in this data for stop_sim to catch."""
        vecs = [unit(1, 0.01 * i, 0) for i in range(5)]
        clusters = ts.cluster_chunks(vecs, cosine=ts.SearchIndex.cosine, max_clusters=1, stop_sim=0.75)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(set(clusters[0]), {0, 1, 2, 3, 4})

    def test_low_max_clusters_does_not_bypass_a_real_boundary(self):
        """Even asked for max_clusters=1, a genuine low-similarity boundary between two tight
        groups still stops the merge early via stop_sim — the ceiling is a MAXIMUM, not a target
        that overrides a real dissimilarity signal."""
        vecs = [unit(1, 0.02, 0), unit(1, -0.01, 0), unit(0.99, 0.01, 0),
                unit(0, 1, 0.02), unit(0.01, 1, -0.02), unit(-0.01, 0.99, 0)]
        clusters = ts.cluster_chunks(vecs, cosine=ts.SearchIndex.cosine, max_clusters=1, stop_sim=0.75)
        self.assertEqual(len(clusters), 2)

    def test_respects_max_clusters_ceiling(self):
        # four well-separated orthogonal-ish directions; cap at 2 clusters. stop_sim=-2.0 (below
        # any real cosine value) disables the early-stop entirely, isolating max_clusters as the
        # only stopping condition under test.
        vecs = [unit(1, 0, 0, 0), unit(0, 1, 0, 0), unit(0, 0, 1, 0), unit(0, 0, 0, 1)]
        clusters = ts.cluster_chunks(vecs, cosine=ts.SearchIndex.cosine, max_clusters=2, stop_sim=-2.0)
        self.assertEqual(len(clusters), 2)

    def test_single_chunk_is_one_cluster(self):
        self.assertEqual(ts.cluster_chunks([unit(1, 0)]), [[0]])

    def test_empty_is_no_clusters(self):
        self.assertEqual(ts.cluster_chunks([]), [])

    def test_cluster_order_is_first_appearance_not_size_or_similarity(self):
        vecs = [unit(0, 1), unit(0, 0.99), unit(1, 0), unit(0.99, 0)]
        clusters = ts.cluster_chunks(vecs, cosine=ts.SearchIndex.cosine, max_clusters=2)
        self.assertEqual(clusters[0][0], min(clusters[0]))
        self.assertLess(min(clusters[0]), min(clusters[1]))


class FakeSearchIndex:
    """Minimal stand-in for quintessence.search.SearchIndex: just enough surface for
    head_vectors() to drive (chunks/embed_model/embed/_doc_prefix/_load_cache/_save_cache),
    with a deterministic fake embedder (no network, no ollama) — the vector is derived from the
    text itself so identical text always embeds identically, matching the real cache's content-
    hash-keyed semantics."""

    embed_model = "fake-model"

    def __init__(self, chunk_rows):
        self._rows = chunk_rows   # list of (label, path, title, text)
        self._cache = {}
        self.save_calls = 0
        self.embed_calls = 0

    def chunks(self):
        yield from self._rows

    def _doc_prefix(self):
        return ""

    def _load_cache(self):
        return dict(self._cache)

    def _save_cache(self, cache):
        self._cache = dict(cache)
        self.save_calls += 1

    def embed(self, text, prefix):
        self.embed_calls += 1
        # deterministic 2-D vector from the text's own length/hash — good enough to exercise
        # the plumbing without asserting anything about embedding QUALITY.
        h = sum(ord(c) for c in text) % 97
        return [float(h), float(len(text) % 31)]


class TestHeadVectors(unittest.TestCase):
    def _rows(self, n_heads=2, chunks_per_head=5):
        rows = []
        for h in range(n_heads):
            for c in range(chunks_per_head):
                rows.append(("qq", f"/fake/qq/head{h}.md", f"section {c}", f"head{h} chunk{c} body"))
            # a non-"qq" label must be ignored entirely
            rows.append(("mem", f"/fake/mem/head{h}.md", "(fact)", f"unrelated memory {h}"))
        return rows

    def test_sample_k_caps_chunks_per_head_in_file_order(self):
        idx = FakeSearchIndex(self._rows(n_heads=2, chunks_per_head=5))
        by_head, by_head_titles, total = ts.head_vectors(idx, sample_k=2, workers=2)
        self.assertEqual(total["head0"], 5)
        self.assertEqual(len(by_head["head0"]), 2)
        self.assertEqual(by_head_titles["head0"], ["section 0", "section 1"])
        self.assertEqual(len(by_head["head1"]), 2)

    def test_sample_k_zero_takes_every_chunk(self):
        idx = FakeSearchIndex(self._rows(n_heads=1, chunks_per_head=7))
        by_head, by_head_titles, total = ts.head_vectors(idx, sample_k=0, workers=2)
        self.assertEqual(total["head0"], 7)
        self.assertEqual(len(by_head["head0"]), 7)
        self.assertEqual(by_head_titles["head0"], [f"section {c}" for c in range(7)])

    def test_non_qq_labels_are_excluded(self):
        idx = FakeSearchIndex(self._rows(n_heads=1, chunks_per_head=3))
        by_head, _titles, _total = ts.head_vectors(idx, sample_k=0, workers=2)
        self.assertEqual(set(by_head.keys()), {"head0"})

    def test_cache_hit_skips_a_redundant_embed_call(self):
        rows = self._rows(n_heads=1, chunks_per_head=1)
        idx = FakeSearchIndex(rows)
        ts.head_vectors(idx, sample_k=0, workers=2)
        first_calls = idx.embed_calls
        self.assertGreater(first_calls, 0)
        # second pass over the SAME idx (cache now warm) must not re-embed
        ts.head_vectors(idx, sample_k=0, workers=2)
        self.assertEqual(idx.embed_calls, first_calls)


class TestParseArgs(unittest.TestCase):
    def test_default_sample_k(self):
        self.assertEqual(ts.parse_args([]).sample_k, ts.DEFAULT_SAMPLE_K)

    def test_explicit_sample_k(self):
        self.assertEqual(ts.parse_args(["--sample-k", "0"]).sample_k, 0)
        self.assertEqual(ts.parse_args(["--sample-k", "10"]).sample_k, 10)


if __name__ == "__main__":
    unittest.main()
