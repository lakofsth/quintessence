# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.search: Config plumbing (no ambient environ reads),
cache identity derivation + legacy-cache migration, the QQ_EMBED_NUM_GPU options hook, the
periodic mid-build cache checkpoint (grounded finding 2026-07-03: a killed long run must not
lose already-embedded vectors), the orphan-decay + reap-guard, and build_index()'s corpus-
signature revalidation (the property the long-lived MCP server depends on)."""
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence.config import Config
from quintessence import search as searchmod
from quintessence.search import SearchIndex, cache_identity, identity_cache_path


def make_index(base: str, **overrides) -> SearchIndex:
    over = {"QQ_KB_ROOT": os.path.join(base, "kb"),
            "QQ_CACHE": os.path.join(base, "cache", "embeddings.json"),
            "QQ_EMBED_MODEL": "qwen3-embedding:0.6b"}
    over.update(overrides)
    cfg = Config(env={}, config_file="/nonexistent", overrides=over)
    return SearchIndex(cfg)


def write_corpus(base: str, n: int = 6, source: str = "docs") -> None:
    d = os.path.join(base, "kb", source)
    os.makedirs(d, exist_ok=True)
    for i in range(n):
        with open(os.path.join(d, f"doc{i}.md"), "w") as f:
            f.write(f"# doc {i}\nunique content sentinel number {i}\n")


class FakeEmbedder:
    """Deterministic stand-in for SearchIndex.embed: returns a 1-vector keyed off the text so
    equal text -> equal vector, no network. Call count is inspectable."""
    def __init__(self):
        self.calls = 0

    def __call__(self, text, prefix):
        self.calls += 1
        h = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        return [float(h % 997) / 997.0, 1.0]


class TestConfigPlumbing(unittest.TestCase):
    def test_resolves_once_no_ambient_leakage(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base)
            idx = make_index(base)
            self.assertEqual(idx.kb_root, os.path.join(base, "kb"))
            self.assertEqual(idx.embed_model, "qwen3-embedding:0.6b")
            # mutating os.environ AFTER construction must not affect an already-built instance
            with unittest.mock.patch.dict(os.environ, {"QQ_KB_ROOT": "/should/not/apply"}):
                self.assertEqual(idx.kb_root, os.path.join(base, "kb"))

    def test_two_instances_independent(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base)
            idx1 = make_index(base, QQ_EMBED_MODEL="model-a")
            idx2 = make_index(base, QQ_EMBED_MODEL="model-b")
            self.assertNotEqual(idx1.embed_model, idx2.embed_model)
            self.assertNotEqual(idx1.cache_path, idx2.cache_path)

    def test_num_gpu_defaults_unset(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base)
            self.assertIsNone(idx.num_gpu)

    def test_num_gpu_reads_configured_int(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_EMBED_NUM_GPU="4")
            self.assertEqual(idx.num_gpu, 4)


class TestCacheIdentity(unittest.TestCase):
    def test_identity_varies_with_kb_root_model_and_chunker_version(self):
        base_id = cache_identity("/a/kb", "model-x", 1)
        self.assertNotEqual(base_id, cache_identity("/b/kb", "model-x", 1))
        self.assertNotEqual(base_id, cache_identity("/a/kb", "model-y", 1))
        self.assertNotEqual(base_id, cache_identity("/a/kb", "model-x", 2))

    def test_identity_cache_path_sits_beside_legacy_path(self):
        p = identity_cache_path("/home/x/.cache/qq-search/embeddings.json", "abc123-v1")
        self.assertTrue(p.startswith("/home/x/.cache/qq-search/embeddings."))
        self.assertTrue(p.endswith(".json"))
        self.assertIn("abc123-v1", p)

    def test_search_index_cache_path_is_identity_scoped_not_the_legacy_path(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base)
            self.assertNotEqual(idx.cache_path, idx.legacy_cache_path)


class TestLegacyCacheMigration(unittest.TestCase):
    """D4 migration: a legacy (pre-identity) cache is copied into the new identity path IF an
    empirical sample of the CURRENT corpus's chunk keys already hits it under the CURRENT
    model — avoiding a full re-embed at cutover."""

    def _legacy_cache_for(self, idx: SearchIndex, model: str) -> dict:
        cache = {}
        for _label, _path, _title, text in idx.chunks():
            key = hashlib.sha256(f"{model}\0{text}".encode()).hexdigest()
            cache[key] = [0.1, 0.2, 0.3]
        return cache

    def test_matching_model_migrates_and_avoids_reembedding(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=5)
            idx = make_index(base)
            legacy = self._legacy_cache_for(idx, idx.embed_model)   # same model -> keys match
            os.makedirs(os.path.dirname(idx.legacy_cache_path), exist_ok=True)
            with open(idx.legacy_cache_path, "w") as f:
                json.dump(legacy, f)
            self.assertFalse(os.path.exists(idx.cache_path))

            stub = FakeEmbedder()
            idx.embed = stub
            idx.build_index()
            self.assertTrue(os.path.exists(idx.cache_path), "migration did not create the identity cache")
            self.assertEqual(stub.calls, 0, "migrated cache should have made every chunk a hit — no re-embed")

    def test_mismatched_model_does_not_migrate(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=5)
            idx = make_index(base)
            legacy = self._legacy_cache_for(idx, "a-totally-different-model")
            os.makedirs(os.path.dirname(idx.legacy_cache_path), exist_ok=True)
            with open(idx.legacy_cache_path, "w") as f:
                json.dump(legacy, f)

            stub = FakeEmbedder()
            idx.embed = stub
            idx.build_index()
            self.assertGreater(stub.calls, 0, "a model mismatch must not migrate — every chunk should embed fresh")

    def test_no_legacy_cache_no_migration_attempted(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=3)
            idx = make_index(base)
            self.assertFalse(os.path.exists(idx.legacy_cache_path))
            stub = FakeEmbedder()
            idx.embed = stub
            idx.build_index()
            self.assertGreater(stub.calls, 0)
            self.assertFalse(os.path.exists(idx.legacy_cache_path))


class TestNumGpuHook(unittest.TestCase):
    """The Ollama embed request's `options` gains 'num_gpu' ONLY when configured; unset must
    stay byte-identical to the pre-P3 request shape ({'num_ctx': 2048})."""

    class _FakeResponse:
        def __init__(self, body: bytes):
            self._body = body
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def test_options_unset_matches_legacy_shape_exactly(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base)
            captured = {}

            def fake_urlopen(req, timeout=None):
                captured["body"] = json.loads(req.data.decode())
                return self._FakeResponse(json.dumps({"embedding": [1.0, 2.0]}).encode())

            with unittest.mock.patch.object(searchmod.urllib.request, "urlopen", fake_urlopen):
                idx._embed_call("hello", "")
            self.assertEqual(captured["body"]["options"], {"num_ctx": 2048})

    def test_options_gains_num_gpu_when_configured(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_EMBED_NUM_GPU="7")
            captured = {}

            def fake_urlopen(req, timeout=None):
                captured["body"] = json.loads(req.data.decode())
                return self._FakeResponse(json.dumps({"embedding": [1.0, 2.0]}).encode())

            with unittest.mock.patch.object(searchmod.urllib.request, "urlopen", fake_urlopen):
                idx._embed_call("hello", "")
            self.assertEqual(captured["body"]["options"], {"num_ctx": 2048, "num_gpu": 7})


class TestCheckpointing(unittest.TestCase):
    """Grounded finding (2026-07-03): a long build TERM'd mid-run must not lose everything —
    build_index() checkpoints the cache every CHECKPOINT_INTERVAL new embeddings, ADD-only
    (no reap mid-build), via the same atomic tmp+replace save as the final write."""

    def test_interrupted_build_keeps_checkpointed_vectors_and_resumes_the_remainder(self):
        with tempfile.TemporaryDirectory() as base:
            n_chunks = 8
            write_corpus(base, n=n_chunks)
            idx = make_index(base)
            old_interval = searchmod.CHECKPOINT_INTERVAL
            searchmod.CHECKPOINT_INTERVAL = 3
            try:
                calls = {"n": 0}

                class Boom(Exception):
                    pass

                def flaky_embed(text, prefix):
                    calls["n"] += 1
                    if calls["n"] > 4:   # dies partway through the 2nd checkpoint window
                        raise Boom("simulated TERM mid-build")
                    h = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
                    return [float(h % 997) / 997.0, 1.0]

                idx.embed = flaky_embed
                with self.assertRaises(Boom):
                    idx.build_index()

                # the FIRST checkpoint (3 embeds) must be on disk despite the later crash
                self.assertTrue(os.path.exists(idx.cache_path))
                with open(idx.cache_path) as f:
                    checkpointed = json.load(f)
                self.assertEqual(len(checkpointed), 3, "only the first full checkpoint window should be persisted")

                # a fresh build resumes: only the REMAINING (uncached) chunks are embedded
                idx2 = make_index(base)
                idx2.config = idx.config
                idx2.cache_path = idx.cache_path
                stub = FakeEmbedder()
                idx2.embed = stub
                idx2.build_index()
                self.assertEqual(stub.calls, n_chunks - 3,
                                 "resume must only embed the chunks the interrupted run never reached")
            finally:
                searchmod.CHECKPOINT_INTERVAL = old_interval

    def test_checkpoint_is_atomic_write(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base)
            idx._save_cache({"a": [1, 2, 3]})
            self.assertTrue(os.path.exists(idx.cache_path))
            self.assertFalse(os.path.exists(idx.cache_path + ".tmp"), "tmp file must not survive a completed save")
            with open(idx.cache_path) as f:
                self.assertEqual(json.load(f), {"a": [1, 2, 3]})


class TestConcurrentCacheLocking(unittest.TestCase):
    """A1 remainder (LOW severity): the cache's load-merge-save spans are now guarded by a
    per-identity `_cache_lock()` (SearchIndex._cache_lock, keyed on cache_path — see its
    docstring) so two concurrent same-identity builders merge instead of clobbering one
    another. Two properties, tested separately: (1) `_checkpoint_locked`/`_final_save_locked`
    actually MERGE a concurrent builder's earlier save (the fix itself — locking alone doesn't
    get this for free, see those methods' docstrings) and (2) `_cache_lock()` genuinely
    serializes concurrent holders (mutual exclusion, the same property test_store.py's
    TestStateLock proves for the state-dir lock)."""

    def test_checkpoint_merges_a_concurrent_builders_earlier_save_not_clobbers_it(self):
        """The core lost-update fix, proven without needing a real thread race: builder A
        checkpoints first; builder B's OWN local view never saw A's key (exactly the "started
        from a stale/empty snapshot" shape of the race) — a checkpoint that just persisted its
        own local dict would silently drop A's key. Reload-merge-save must preserve it."""
        with tempfile.TemporaryDirectory() as base:
            idxA = make_index(base)
            idxB = make_index(base)
            self.assertEqual(idxA.cache_path, idxB.cache_path, "same identity -> same cache file")

            merged_a = idxA._checkpoint_locked({}, {"keyA": [1.0]})
            self.assertEqual(merged_a, {"keyA": [1.0]})

            # B started from an empty view too (never saw A's save) -- its checkpoint must
            # still preserve keyA, merged in via the on-disk reload, not clobber it.
            merged_b = idxB._checkpoint_locked({}, {"keyB": [2.0]})
            self.assertEqual(merged_b, {"keyA": [1.0], "keyB": [2.0]},
                              "B's checkpoint must MERGE with A's on-disk save, not clobber it")

            with open(idxA.cache_path) as f:
                on_disk = json.load(f)
            self.assertEqual(on_disk, {"keyA": [1.0], "keyB": [2.0]})

    def test_final_save_merges_a_concurrent_checkpoint(self):
        """Same property at the FINAL (reap) span: A's final save must not drop B's
        already-checkpointed key. Realistic only when both builders share the same live corpus
        view (guaranteed for genuinely concurrent SAME-IDENTITY builders — same kb_root, same
        model, same chunker version, per D4); modeled here by including keyB in A's own `live`
        set, matching what A's OWN corpus walk would produce since it's the same corpus B saw."""
        with tempfile.TemporaryDirectory() as base:
            idxA = make_index(base)
            idxB = make_index(base)

            idxB._checkpoint_locked({}, {"keyB": [9.0]})   # B's mid-build checkpoint lands first

            idxA._final_save_locked(pending={"keyA": [1.0]}, live={"keyA", "keyB"},
                                     miss=1, n_before=0)
            with open(idxA.cache_path) as f:
                on_disk = json.load(f)
            self.assertEqual(on_disk, {"keyA": [1.0], "keyB": [9.0]},
                              "A's final save must merge B's checkpoint, not overwrite it")

    def test_cache_lock_serializes_concurrent_holders(self):
        """Mutual exclusion itself (parallel to test_store.py's TestStateLock): N threads
        racing `_cache_lock()` must never interleave inside the critical section."""
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base)
            log: list = []
            errors: list = []

            def worker(tag):
                try:
                    with idx._cache_lock():
                        log.append((tag, "enter"))
                        time.sleep(0.05)
                        log.append((tag, "exit"))
                except Exception as e:  # pragma: no cover - would fail the assertions below anyway
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
            self.assertEqual(errors, [])
            for i in range(0, len(log), 2):
                self.assertEqual(log[i][1], "enter")
                self.assertEqual(log[i + 1][1], "exit")
                self.assertEqual(log[i][0], log[i + 1][0])

    def test_second_acquirer_blocks_then_times_out(self):
        """`_cache_lock()` shares state_lock's timeout mechanics (both built on the SAME
        `acquire_flock` primitive, D1) -- pinned here for the cache lock specifically."""
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base)
            got_lock = threading.Event()
            release = threading.Event()

            def holder():
                with idx._cache_lock():
                    got_lock.set()
                    release.wait(5)

            t = threading.Thread(target=holder)
            t.start()
            self.assertTrue(got_lock.wait(2), "holder thread never acquired the cache lock")
            # idx2: a second instance, same identity -> same lock file, but a SHORT QQ_LOCK_WAIT
            # so it times out well before the holder's own 5s release() timeout releases it.
            idx2 = make_index(base, QQ_LOCK_WAIT=1)
            start = time.monotonic()
            with self.assertRaises(searchmod.LockTimeout):
                with idx2._cache_lock():
                    pass  # pragma: no cover - unreachable, idx holds the lock
            elapsed = time.monotonic() - start
            self.assertGreaterEqual(elapsed, 0.8)
            self.assertLess(elapsed, 4, "must time out on its OWN short wait, not the holder's")
            release.set()
            t.join(5)

    def test_two_identities_never_contend_on_the_same_lock_file(self):
        """A DIFFERENT cache identity (different kb_root -> different cache_path) must not
        share a lock file with another identity -- the reason `_cache_lock` is colocated
        per-identity rather than the single shared $QQ_STATE_DIR/.state.lock (see its
        docstring): an embed run can take minutes, and two unrelated corpora must never make
        each other wait on that account."""
        with tempfile.TemporaryDirectory() as base1, tempfile.TemporaryDirectory() as base2:
            idx1 = make_index(base1)
            idx2 = make_index(base2)
            self.assertNotEqual(idx1.cache_path, idx2.cache_path)
            got1 = threading.Event()
            release1 = threading.Event()

            def holder():
                with idx1._cache_lock():
                    got1.set()
                    release1.wait(5)

            t = threading.Thread(target=holder)
            t.start()
            self.assertTrue(got1.wait(2))
            try:
                start = time.monotonic()
                with idx2._cache_lock():
                    pass
                self.assertLess(time.monotonic() - start, 1.0,
                                 "a different identity's lock must acquire immediately, not queue")
            finally:
                release1.set()
                t.join(5)


class TestMCPServerPattern(unittest.TestCase):
    """Mirrors qq-search-mcp exactly: ONE Config + ONE SearchIndex built at process start,
    reused across many `search_continuity`/`ask_continuity` tool calls over the server's
    lifetime (commit b3dd7bf). A HEAD written mid-session must be visible to the very
    next tool call, not just to a freshly-constructed instance."""

    def test_long_lived_instance_sees_a_head_written_after_first_call(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=2)
            idx = make_index(base)   # constructed ONCE, like qq-search-mcp's module-level _index
            idx.embed = FakeEmbedder()

            hits_before = idx.search("sentinel", k=10)
            paths_before = {h["path"] for h in hits_before}
            self.assertFalse(any("late" in p for p in paths_before))

            time.sleep(0.01)
            with open(os.path.join(base, "kb", "docs", "late.md"), "w") as f:
                f.write("# late\nsentinel content written mid-session\n")

            hits_after = idx.search("sentinel", k=10)   # the NEXT tool call on the SAME instance
            paths_after = {h["path"] for h in hits_after}
            self.assertTrue(any("late" in p for p in paths_after),
                             "a long-lived SearchIndex must revalidate and see a HEAD written "
                             "after the first call, not serve its startup snapshot forever")


class TestOrphanDecayAndReapGuard(unittest.TestCase):
    def test_reap_guard_blocks_bulk_drop_without_force(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base)
            cache = {f"k{i}": [0.0] for i in range(10)}
            live = {"k0", "k1"}   # would drop 8/10 (>50%)
            out = idx._reap(cache, live)
            self.assertEqual(len(out), 10, "guard should refuse the bulk drop")

    def test_reap_guard_force_overrides(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_REAP_FORCE="1")
            cache = {f"k{i}": [0.0] for i in range(10)}
            live = {"k0", "k1"}
            out = idx._reap(cache, live)
            self.assertEqual(set(out), {"k0", "k1"})

    def test_orphan_decay_force_drops_a_persistently_orphaned_key_past_the_guard(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base)
            cache = {f"k{i}": [0.0] for i in range(10)}
            live = {"k0", "k1"}   # 8 orphans, > 50% -> guard would normally block
            long_ago = time.time() - (searchmod.ORPHAN_DECAY_SECONDS + 3600)
            ages = {f"k{i}": long_ago for i in range(2, 10)}
            os.makedirs(os.path.dirname(idx.cache_path), exist_ok=True)
            with open(idx._orphan_ages_path(), "w") as f:
                json.dump(ages, f)
            out = idx._reap(cache, live)
            self.assertEqual(set(out), {"k0", "k1"},
                              "every orphan aged past ORPHAN_DECAY_SECONDS must be force-dropped "
                              "even though the bulk guard would otherwise refuse")

    def test_freshly_orphaned_key_is_tracked_not_dropped(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base)
            cache = {"k0": [0.0], "k1": [0.0]}
            live = {"k0"}   # k1 newly orphaned this run -> 1/2 == 50%, not > 50%, guard passes-through
            out = idx._reap(cache, live)
            self.assertEqual(set(out), {"k0"})
            with open(idx._orphan_ages_path()) as f:
                ages = json.load(f)
            self.assertEqual(ages, {}, "reap fired (guard didn't block) so nothing is left to track")


class TestCorpusSignatureRevalidation(unittest.TestCase):
    """Spec A3: a long-lived caller (the MCP server) must see a changed corpus rebuild instead
    of serving its first-build snapshot forever."""

    def test_unchanged_corpus_returns_cached_index_without_reembedding(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=3)
            idx = make_index(base)
            stub = FakeEmbedder()
            idx.embed = stub
            idx.build_index()
            first_calls = stub.calls
            idx.build_index()   # unchanged corpus -> signature match -> serves the singleton
            self.assertEqual(stub.calls, first_calls, "an unchanged corpus must not re-walk/re-embed")

    def test_new_file_in_corpus_triggers_rebuild(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=3)
            idx = make_index(base)
            stub = FakeEmbedder()
            idx.embed = stub
            idx.build_index()
            first_calls = stub.calls
            time.sleep(0.01)
            with open(os.path.join(base, "kb", "docs", "new.md"), "w") as f:
                f.write("# new\nfresh sentinel content\n")
            idx.build_index()
            self.assertGreater(stub.calls, first_calls, "a corpus change must trigger a rebuild")


class TestSearchAndGrepFallback(unittest.TestCase):
    def test_search_returns_ranked_hits_shape(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=4)
            idx = make_index(base)
            idx.embed = FakeEmbedder()
            hits = idx.search("sentinel", k=2)
            self.assertLessEqual(len(hits), 2)
            for h in hits:
                self.assertIn("score", h)
                self.assertIn("path", h)
                self.assertIn("source", h)

    def test_embedder_down_degrades_to_keyword_fallback(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=2, source="docs")
            with open(os.path.join(base, "kb", "docs", "doc0.md"), "w") as f:
                f.write("# doc0\nxyzzy-marker unique term\n")
            idx = make_index(base)
            idx.embed = lambda text, prefix: None   # simulate embedder unreachable
            hits = idx.search("xyzzy-marker", k=5)
            self.assertTrue(hits)
            self.assertTrue(all(h.get("degraded") for h in hits))


class TestRedaction(unittest.TestCase):
    """recall-02: QQ_REDACT_FILE used to be honored ONLY by quintessence.ask.Ask — a bare
    SearchIndex.search()/grep_fallback() call (qq-search, search_continuity, and each per-store
    fallback inside quintessence.searchcompose) read straight past it, so a slug an operator
    configured as redacted still came back verbatim from the near-identical `search` path. Both
    the embedder-path (search) and the keyword-fallback path (grep_fallback) must apply it."""

    def _write_redact_file(self, base: str, *slugs: str) -> str:
        path = os.path.join(base, "redact-slugs")
        with open(path, "w") as f:
            f.write("\n".join(slugs) + "\n")
        return path

    def test_search_drops_redacted_hit_via_embedder_path(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=1, source="docs")
            with open(os.path.join(base, "kb", "docs", "secret-topic.md"), "w") as f:
                f.write("# secret-topic\nxyzzy-marker withheld content\n")
            redact_file = self._write_redact_file(base, "secret-topic")
            idx = make_index(base, QQ_REDACT_FILE=redact_file)
            idx.embed = FakeEmbedder()
            hits = idx.search("xyzzy-marker", k=10)
            self.assertTrue(hits, "expected the non-redacted docs to still surface")
            self.assertFalse(any("secret-topic" in h["path"] for h in hits))

    def test_grep_fallback_drops_redacted_hit(self):
        with tempfile.TemporaryDirectory() as base:
            docs = os.path.join(base, "kb", "docs")
            os.makedirs(docs, exist_ok=True)
            # both docs share the query TERM so grep_fallback's keyword match keeps both as
            # candidates -- the redaction filter, not the term match, is what's under test here.
            with open(os.path.join(docs, "public-topic.md"), "w") as f:
                f.write("# public-topic\nxyzzy-marker public content\n")
            with open(os.path.join(docs, "secret-topic.md"), "w") as f:
                f.write("# secret-topic\nxyzzy-marker withheld content\n")
            redact_file = self._write_redact_file(base, "secret-topic")
            idx = make_index(base, QQ_REDACT_FILE=redact_file)
            idx.embed = lambda text, prefix: None   # embedder unreachable -> keyword fallback
            hits = idx.search("xyzzy-marker", k=10)
            self.assertTrue(hits, "expected the non-redacted doc to still surface")
            self.assertTrue(all(h.get("degraded") for h in hits))
            self.assertFalse(any("secret-topic" in h["path"] for h in hits))

    def test_no_redact_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=2, source="docs")
            idx = make_index(base)
            idx.embed = FakeEmbedder()
            hits = idx.search("sentinel", k=10)
            self.assertTrue(hits)


class TestQueryVectorParam(unittest.TestCase):
    """P8 recall composition: search(query_vector=...) scores a PRECOMPUTED vector against this
    index without embedding the query again — quintessence.searchcompose embeds a query once
    and reuses it across every store's SearchIndex. Default (query_vector=None) must reproduce
    the exact pre-P8 code path (self.embed(query, self._query_prefix())) byte-for-byte."""

    def test_default_none_embeds_as_before(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=3)
            idx = make_index(base)
            idx.embed = FakeEmbedder()
            hits_a = idx.search("sentinel", k=5)
            hits_b = idx.search("sentinel", k=5, query_vector=None)
            self.assertEqual(hits_a, hits_b)

    def test_precomputed_vector_skips_embed_call_entirely(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=3)
            idx = make_index(base)
            idx.embed = FakeEmbedder()   # would raise/record if called for the QUERY
            # build the index once (this DOES call embed() for the docs)
            idx.build_index()
            doc_calls_before = idx.embed.calls
            precomputed = [0.5, 1.0]
            hits = idx.search("irrelevant text never embedded", k=5, query_vector=precomputed)
            self.assertEqual(idx.embed.calls, doc_calls_before,
                              "a precomputed query_vector must not trigger any additional embed() call")
            self.assertTrue(hits)

    def test_precomputed_vector_scores_correctly(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=1)
            idx = make_index(base)
            idx.embed = FakeEmbedder()
            index = idx.build_index()
            # score against the FIRST chunk's own vector -> perfect cosine similarity (1.0)
            target_vec = index[0][4]
            hits = idx.search("doesn't matter", k=1, query_vector=target_vec)
            self.assertAlmostEqual(hits[0]["score"], 1.0, places=3)


class TestSourceRoots(unittest.TestCase):
    """P8 recall composition: `source_roots` bypasses the "one source per kb_root SUBDIRECTORY"
    scan so a project store's own top-level *.md HEADs (not nested under a subdirectory) are
    indexed — the exact gap test-ask.sh's LEGACY BUG comment documents for the old engine."""

    def test_loose_top_level_files_are_indexed_under_source_roots(self):
        with tempfile.TemporaryDirectory() as base:
            proj = os.path.join(base, "proj-store")
            os.makedirs(proj)
            with open(os.path.join(proj, "sometopic.md"), "w") as f:
                f.write("# sometopic\nxyzzy-project-sentinel unique text\n")
            cfg = Config(env={}, config_file="/nonexistent",
                         overrides={"QQ_KB_ROOT": proj,
                                    "QQ_CACHE": os.path.join(base, "cache", "embeddings.json")})
            idx = SearchIndex(cfg, source_roots={"qq": proj})
            chunks = list(idx.chunks())
            self.assertTrue(any("xyzzy-project-sentinel" in text for *_junk, text in chunks),
                             "a loose top-level .md file under source_roots must be indexed")

    def test_default_mode_ignores_loose_top_level_files_unchanged(self):
        """The pre-P8 gap, pinned as a regression guard: WITHOUT source_roots, a file sitting
        directly at kb_root's own top level (not inside a subdirectory) is still never walked."""
        with tempfile.TemporaryDirectory() as base:
            kb = os.path.join(base, "kb")
            os.makedirs(kb)
            with open(os.path.join(kb, "loose.md"), "w") as f:
                f.write("# loose\nxyzzy-loose-sentinel\n")
            idx = make_index(base)
            chunks = list(idx.chunks())
            self.assertFalse(any("xyzzy-loose-sentinel" in text for *_junk, text in chunks))

    def test_source_roots_entries_are_never_whole_file(self):
        """Every source_roots entry is chunked SECTIONED (heading-split), never whole-file —
        matching how the user store's own 'quintessence' source (not in _DEFAULT_WHOLE) is
        chunked today, so a multi-section project HEAD produces multiple chunks, not one."""
        with tempfile.TemporaryDirectory() as base:
            proj = os.path.join(base, "proj-store")
            os.makedirs(proj)
            with open(os.path.join(proj, "multi.md"), "w") as f:
                f.write("# multi\n\n## section one\ncontent one\n\n## section two\ncontent two\n")
            cfg = Config(env={}, config_file="/nonexistent",
                         overrides={"QQ_KB_ROOT": proj,
                                    "QQ_CACHE": os.path.join(base, "cache", "embeddings.json")})
            idx = SearchIndex(cfg, source_roots={"qq": proj})
            chunks = [c for c in idx.chunks() if c[1].endswith("multi.md")]
            self.assertGreater(len(chunks), 1, "a multi-section HEAD must be sectioned, not whole-file")

    def test_corpus_signature_uses_source_roots_too(self):
        """_corpus_signature must walk the SAME sources as chunks() (shared via _iter_sources) —
        a signature computed only from kb_root's subdirectories would never change when a
        source_roots-mode file is added/edited, breaking signature revalidation for a project
        store's own index."""
        with tempfile.TemporaryDirectory() as base:
            proj = os.path.join(base, "proj-store")
            os.makedirs(proj)
            with open(os.path.join(proj, "a.md"), "w") as f:
                f.write("# a\nfirst\n")
            cfg = Config(env={}, config_file="/nonexistent",
                         overrides={"QQ_KB_ROOT": proj,
                                    "QQ_CACHE": os.path.join(base, "cache", "embeddings.json")})
            idx = SearchIndex(cfg, source_roots={"qq": proj})
            sig1 = idx._corpus_signature()
            time.sleep(0.01)
            with open(os.path.join(proj, "b.md"), "w") as f:
                f.write("# b\nsecond\n")
            sig2 = idx._corpus_signature()
            self.assertNotEqual(sig1, sig2)


class TestGCStaleIdentityCaches(unittest.TestCase):
    """GC rider: a per-identity cache file (or its .orphan-ages.json sidecar) older
    than QQ_CACHE_GC_DAYS is removed at build_index start — cleans up orphans left behind by
    throwaway corpus roots. HARD CONSTRAINT: never touches a *.lock file."""

    def _age(self, path: str, days: float) -> None:
        old = time.time() - days * 86400
        os.utime(path, (old, old))

    def test_stale_identity_json_and_sidecar_removed(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_CACHE_GC_DAYS="60")
            cache_dir = os.path.dirname(idx.cache_path)
            os.makedirs(cache_dir, exist_ok=True)
            stale_json = os.path.join(cache_dir, "embeddings.deadbeef-model-v1.json")
            stale_sidecar = os.path.join(cache_dir, "embeddings.deadbeef-model-v1.json.orphan-ages.json")
            for p in (stale_json, stale_sidecar):
                with open(p, "w") as f:
                    f.write("{}")
                self._age(p, 90)   # older than the 60-day default
            idx.build_index()
            self.assertFalse(os.path.exists(stale_json))
            self.assertFalse(os.path.exists(stale_sidecar))

    def test_lock_file_never_removed_regardless_of_age(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_CACHE_GC_DAYS="1")
            cache_dir = os.path.dirname(idx.cache_path)
            os.makedirs(cache_dir, exist_ok=True)
            stale_lock = os.path.join(cache_dir, "embeddings.deadbeef-model-v1.json.lock")
            with open(stale_lock, "w") as f:
                pass
            self._age(stale_lock, 9999)
            idx.build_index()
            self.assertTrue(os.path.exists(stale_lock),
                             "GC must NEVER remove a *.lock file (inode-swap mutual-exclusion break)")

    def test_recent_file_not_removed(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_CACHE_GC_DAYS="60")
            cache_dir = os.path.dirname(idx.cache_path)
            os.makedirs(cache_dir, exist_ok=True)
            fresh_json = os.path.join(cache_dir, "embeddings.freshid-model-v1.json")
            with open(fresh_json, "w") as f:
                f.write("{}")
            idx.build_index()
            self.assertTrue(os.path.exists(fresh_json))

    def test_gc_days_zero_disables_gc(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_CACHE_GC_DAYS="0")
            cache_dir = os.path.dirname(idx.cache_path)
            os.makedirs(cache_dir, exist_ok=True)
            ancient = os.path.join(cache_dir, "embeddings.ancient-model-v1.json")
            with open(ancient, "w") as f:
                f.write("{}")
            self._age(ancient, 9999)
            idx.build_index()
            self.assertTrue(os.path.exists(ancient), "QQ_CACHE_GC_DAYS=0 must disable GC entirely")

    def test_own_current_identity_files_never_swept(self):
        """This instance's own cache/orphan-ages/lock files must survive GC even if their mtime
        happens to predate the cutoff -- they're about to be read/written by THIS very call."""
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=1)
            idx = make_index(base, QQ_CACHE_GC_DAYS="1")
            idx.embed = FakeEmbedder()
            idx.build_index()   # creates idx.cache_path for real
            self._age(idx.cache_path, 9999)
            idx.build_index(force=True)   # a second call; _gc_ran is already True, but verify anyway
            self.assertTrue(os.path.exists(idx.cache_path))

    def test_legacy_non_identity_cache_path_never_swept(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_CACHE_GC_DAYS="1")
            os.makedirs(os.path.dirname(idx.legacy_cache_path), exist_ok=True)
            with open(idx.legacy_cache_path, "w") as f:
                f.write("{}")
            self._age(idx.legacy_cache_path, 9999)
            idx.build_index()
            self.assertTrue(os.path.exists(idx.legacy_cache_path),
                             "the legacy (non-identity-scoped) QQ_CACHE path must never be GC'd")

    def test_gc_runs_at_most_once_per_instance(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_CACHE_GC_DAYS="60")
            cache_dir = os.path.dirname(idx.cache_path)
            os.makedirs(cache_dir, exist_ok=True)
            idx.build_index()   # first call: self._gc_ran flips True, sweeps the (empty) dir
            # a file created AFTER the first call, backdated, must survive a SECOND build_index
            # call on the SAME instance -- the once-per-instance throttle, not a bug.
            late_stale = os.path.join(cache_dir, "embeddings.latecomer-model-v1.json")
            with open(late_stale, "w") as f:
                f.write("{}")
            self._age(late_stale, 9999)
            idx.build_index(force=True)
            self.assertTrue(os.path.exists(late_stale),
                             "GC must not re-run within the same instance's lifetime")

    def test_tmp_file_never_removed(self):
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_CACHE_GC_DAYS="1")
            cache_dir = os.path.dirname(idx.cache_path)
            os.makedirs(cache_dir, exist_ok=True)
            tmp = os.path.join(cache_dir, "embeddings.inflight-model-v1.json.tmp")
            with open(tmp, "w") as f:
                f.write("{")
            self._age(tmp, 9999)
            idx.build_index()
            self.assertTrue(os.path.exists(tmp), "an in-flight atomic-write .tmp file must never be swept")


class TestSearchMinSimFloor(unittest.TestCase):
    """QQ_SEARCH_MIN_SIM: hits scoring below the configured floor are dropped before a caller
    sees them. Keyword-fallback (degraded) hits are exempt — their score is a term-overlap
    fraction, not a cosine similarity. Floor of zero disables filtering."""

    def test_below_floor_hit_dropped_with_default(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=2)
            idx = make_index(base, QQ_SEARCH_MIN_SIM="0.99")
            idx.embed = FakeEmbedder()
            hits = idx.search("sentinel", k=10)
            self.assertEqual(hits, [], "all hits should fall below a 0.99 floor")
            self.assertGreater(idx.last_search_floored, 0)

    def test_below_floor_hit_kept_with_floor_zero(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=2)
            idx = make_index(base, QQ_SEARCH_MIN_SIM="0.99")
            idx.embed = FakeEmbedder()
            hits = idx.search("sentinel", k=10, min_sim=0)
            self.assertGreater(len(hits), 0, "floor of zero must disable filtering")
            self.assertEqual(idx.last_search_floored, 0)

    def test_degraded_hits_pass_regardless_of_floor(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=2, source="docs")
            with open(os.path.join(base, "kb", "docs", "doc0.md"), "w") as f:
                f.write("# doc0\nxyzzy-marker unique term\n")
            idx = make_index(base, QQ_SEARCH_MIN_SIM="0.99")
            idx.embed = lambda text, prefix: None   # force keyword fallback
            hits = idx.search("xyzzy-marker", k=5)
            self.assertTrue(hits, "degraded hits must bypass the cosine floor")
            self.assertTrue(all(h.get("degraded") for h in hits))

    def test_last_search_floored_counts_dropped_hits(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=4)
            idx = make_index(base, QQ_SEARCH_MIN_SIM="0.99")
            idx.embed = FakeEmbedder()
            idx.search("sentinel", k=10)
            self.assertEqual(idx.last_search_floored, 4,
                             "floored count must equal the number of hits that fell below the floor")

    def test_last_search_floored_zero_when_all_pass(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=2)
            idx = make_index(base, QQ_SEARCH_MIN_SIM="0.0")
            idx.embed = FakeEmbedder()
            idx.search("sentinel", k=10)
            self.assertEqual(idx.last_search_floored, 0)


class TestSearchWithStats(unittest.TestCase):
    """`with_stats=True`: search returns `(hits, {"floored": n})`, the count carried in a
    call-local dict so each caller gets its own number even if the shared attribute is
    overwritten between calls. `with_stats=False` (the default) keeps the bare-list shape.
    Direct unit coverage for the return contract itself — previously only integration-covered
    through the MCP wiring tests."""

    def test_default_shape_is_a_bare_list(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=2)
            idx = make_index(base, QQ_SEARCH_MIN_SIM="0.0")
            idx.embed = FakeEmbedder()
            hits = idx.search("sentinel", k=10)
            self.assertIsInstance(hits, list)

    def test_with_stats_returns_hits_and_floored_count(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=4)
            idx = make_index(base, QQ_SEARCH_MIN_SIM="0.99")
            idx.embed = FakeEmbedder()
            hits, stats = idx.search("sentinel", k=10, with_stats=True)
            self.assertEqual(hits, [], "all four fall below a 0.99 floor")
            self.assertEqual(stats, {"floored": 4})
            self.assertEqual(idx.last_search_floored, 4,
                             "the shared attribute is still written for existing callers")

    def test_with_stats_floor_zero_counts_nothing(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=2)
            idx = make_index(base, QQ_SEARCH_MIN_SIM="0.99")
            idx.embed = FakeEmbedder()
            hits, stats = idx.search("sentinel", k=10, min_sim=0, with_stats=True)
            self.assertGreater(len(hits), 0)
            self.assertEqual(stats, {"floored": 0})

    def test_with_stats_on_keyword_fallback(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=2, source="docs")
            with open(os.path.join(base, "kb", "docs", "doc0.md"), "w") as f:
                f.write("# doc0\nxyzzy-marker unique term\n")
            idx = make_index(base, QQ_SEARCH_MIN_SIM="0.99")
            idx.embed = lambda text, prefix: None   # force keyword fallback
            hits, stats = idx.search("xyzzy-marker", k=5, with_stats=True)
            self.assertTrue(hits, "fallback hits still return under with_stats")
            self.assertTrue(all(h.get("degraded") for h in hits))
            self.assertEqual(stats, {"floored": 0})
            self.assertEqual(idx.last_search_floored, 0)

    def test_stats_dict_is_call_local_not_the_shared_attribute(self):
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=4)
            idx = make_index(base, QQ_SEARCH_MIN_SIM="0.99")
            idx.embed = FakeEmbedder()
            _, stats_first = idx.search("sentinel", k=10, with_stats=True)
            idx.search("sentinel", k=10, min_sim=0)   # second call overwrites the attribute
            self.assertEqual(idx.last_search_floored, 0)
            self.assertEqual(stats_first, {"floored": 4},
                             "the first call's stats must survive a later call's overwrite")


if __name__ == "__main__":
    unittest.main()
