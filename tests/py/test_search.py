# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.search: Config plumbing (no ambient environ reads),
cache identity derivation + legacy-cache migration, the QQ_EMBED_NUM_GPU options hook, the
periodic mid-build cache checkpoint (grounded finding 2026-07-03: a killed long run must not
lose already-embedded vectors), the orphan-decay + reap-guard, and build_index()'s corpus-
signature revalidation (the property the long-lived MCP server depends on)."""
import errno
import hashlib
import io
import json
import os
import stat
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


def _letter_bearing_token(width: int, seq: int) -> bytes:
    """Bytes to hand a forced `os.urandom`: `width` of them, distinct per `seq`, and always
    spelling a hex tail that carries one of `abcdef`.

    WHY A FIXTURE HERE MAY NOT USE THE REAL RANDOMNESS. `atomicio.is_generated_temp_name` — the
    predicate this sweep deletes on — is deliberately NARROWER than the writer: on top of twelve
    lowercase hex it requires a hex LETTER, because twelve digits are also twelve valid hex
    characters and an operator's `date +%Y%m%d%H%M` backup sat inside the width (twenty-first
    pass, F2). About one tail in 281 therefore comes out all-decimal and is never claimed, so
    every fixture below that needs its temp RECOGNISED was asserting a coin flip: four of the nine
    such pins in the estate are in this file (D77).

    NOT a name spelled by hand: only the randomness is pinned, and the temp is still opened by
    `_open_unique_temp` itself (rule 7). Width comes from whatever `os.urandom` was asked for, so
    a change to the writer's token width moves the fixture rather than stranding it.

    Twinned by an identical helper in tests/py/test_atomicio.py — the two files have the same
    fixtures and no import path to each other. Every use site guards itself with the real
    predicate, so a divergence surfaces as a red fixture guard.
    """
    return b"\xaa" + seq.to_bytes(width - 1, "big")


def _writer_temp(atomicio, parent: str, basename: str, seq: int = 0) -> str:
    """A temp opened by the WRITER with its tail forced to one the sweep's delete predicate
    claims. Returns the path; the descriptor is closed. `seq` must differ between temps sharing a
    parent and basename — the forced token IS the name, so two of them collide on O_EXCL."""
    with unittest.mock.patch.object(atomicio.os, "urandom",
                                    lambda n: _letter_bearing_token(n, seq)):
        fd, path = atomicio._open_unique_temp(parent, basename)
    os.close(fd)
    return path


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

    def test_migration_writes_the_identity_cache_atomically(self):
        """Thirteenth pass, F2. The migration used shutil.copy2, which truncates the destination
        and streams into it, so a concurrent `qq search` reading the identity cache during a D4
        cutover saw truncated JSON — the reviewer measured 44 torn reads against 2 clean while
        copying 1.3 MB. _load_cache swallows the ValueError and returns {}, so the visible cost
        was a silent full re-embed, which is why it went unnoticed for so long.

        This pins the MECHANISM rather than a reproduced tear: a tear is timing-dependent and a
        test that waits for one is a test that flakes. Revert the migration to copy2 and this
        goes red, which is the property that matters — the destination is never truncated in
        place."""
        with tempfile.TemporaryDirectory() as base:
            write_corpus(base, n=5)
            idx = make_index(base)
            legacy = self._legacy_cache_for(idx, idx.embed_model)
            os.makedirs(os.path.dirname(idx.legacy_cache_path), exist_ok=True)
            with open(idx.legacy_cache_path, "w") as f:
                json.dump(legacy, f)

            used = []
            real = searchmod.atomic_write

            def spy(path, *a, **k):
                used.append(path)
                return real(path, *a, **k)

            idx.embed = FakeEmbedder()
            with unittest.mock.patch.object(searchmod, "atomic_write", spy):
                idx.build_index()

            self.assertIn(idx.cache_path, used,
                          "the migration must write the identity cache through atomicio, not "
                          "truncate it in place")
            with open(idx.cache_path) as f:
                self.assertEqual(json.load(f), legacy, "content must survive the copy intact")

    def test_migration_carries_the_legacy_cache_s_permission_bits(self):
        """Fourteenth pass, F1. `shutil.copy2` carried the source's MODE as well as its mtime,
        and replacing it with `atomic_write` accounted for only the mtime. The primitive's mode
        carry-across stats the DESTINATION, and `_maybe_migrate_legacy_cache` returns early
        unless the destination is absent — so there was never anything to stat, the chmod raised
        FileNotFoundError into a `suppress`, and the temp kept `0o666 & ~umask`. An operator who
        had deliberately `chmod 600`'d their embedding cache got it published at 0644 in a 0755
        directory by a model cutover, permanently: every later `_save_cache` is a REWRITE, which
        carries the widened mode forward.

        Asserted in BOTH directions, so neither a hardcoded mode nor the process umask can pass
        it: 0600 under umask 022, where the wrong answer WIDENS to 0644, and 0640 under umask
        077, where the wrong answer NARROWS to 0600. The assertion is made after a full
        `build_index`, which ends in a `_save_cache` rewrite, so it also pins the carry-forward.
        Drop the `mode=` argument in `_maybe_migrate_legacy_cache` and both cases go red."""
        for legacy_mode, mask in ((0o600, 0o022), (0o640, 0o077)):
            with self.subTest(legacy_mode=oct(legacy_mode), umask=oct(mask)):
                with tempfile.TemporaryDirectory() as base:
                    write_corpus(base, n=5)
                    idx = make_index(base)
                    legacy = self._legacy_cache_for(idx, idx.embed_model)
                    os.makedirs(os.path.dirname(idx.legacy_cache_path), exist_ok=True)
                    with open(idx.legacy_cache_path, "w") as f:
                        json.dump(legacy, f)
                    os.chmod(idx.legacy_cache_path, legacy_mode)

                    idx.embed = FakeEmbedder()
                    old = os.umask(mask)
                    try:
                        idx.build_index()
                    finally:
                        os.umask(old)

                    self.assertTrue(os.path.exists(idx.cache_path), "migration did not run")
                    self.assertEqual(
                        stat.S_IMODE(os.stat(idx.cache_path).st_mode), legacy_mode,
                        f"a {legacy_mode:04o} legacy cache must migrate as {legacy_mode:04o}, "
                        f"not as the umask default {0o666 & ~mask:04o} — copy2 carried mode and "
                        f"the atomic write has to carry it too")
                    self.assertEqual(
                        stat.S_IMODE(os.stat(idx.legacy_cache_path).st_mode), legacy_mode,
                        "the legacy source's own mode must be left alone")

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

    def _index_whose_sidecar_is_one_byte_too_long(self, base: str):
        """(index, sidecar_length, edge, limit) for a QQ_CACHE whose SIDECAR lands one byte past
        the atomic-write name budget while the identity cache beside it still fits.

        The lengths come from the producers — pathconf for NAME_MAX, `_TEMP_NAME_OVERHEAD` for the
        budget, and `_orphan_ages_path()` itself for what identity-scoping plus the suffix cost —
        rather than from the 181 the review measured. Renaming the embed model or widening the
        temp tail moves every one of those numbers, and a hand-written 181 would then be testing
        a band the code no longer has.
        """
        import quintessence.atomicio as atomicio
        cache_dir = os.path.join(base, "cache")
        os.makedirs(cache_dir, exist_ok=True)
        limit = os.pathconf(cache_dir, "PC_NAME_MAX")
        edge = limit - atomicio._TEMP_NAME_OVERHEAD

        probe = make_index(base, QQ_CACHE=os.path.join(cache_dir, "embeddings.json"))
        derived = len(os.path.basename(probe._orphan_ages_path())) - len("embeddings.json")

        stem = "c" * (edge + 1 - derived - len(".json"))
        idx = make_index(base, QQ_CACHE=os.path.join(cache_dir, stem + ".json"))
        sidecar = os.path.basename(idx._orphan_ages_path())
        self.assertEqual(len(sidecar), edge + 1,
                         "fixture must sit exactly one byte past the budget, not merely beyond it")
        self.assertLessEqual(len(os.path.basename(idx.cache_path)), edge,
                             "the identity cache itself must still be writable — the whole point "
                             "is that the cache lands and only the sidecar is refused")
        return idx, edge, limit

    def test_a_sidecar_too_long_to_write_says_so_instead_of_vanishing(self):
        """Seventeenth pass, F1. `_save_orphan_ages` swallowed every OSError, including the
        name-length refusal the atomic write had just been taught to make loud. Reproduced at
        the tip before the fix: a QQ_CACHE basename of 181 bytes writes its identity cache and
        silently stops writing the sidecar, with nothing on stderr — so orphan-vector age
        tracking degraded invisibly and ASSURANCE's "the refusal is loud" was false at the only
        call site that could reach it.

        Best-effort is still the right shape for this write, and that is the second assertion:
        the reap must complete and return its cache. What changes is that the operator hears
        about a name that can never work."""
        with tempfile.TemporaryDirectory() as base:
            idx, edge, limit = self._index_whose_sidecar_is_one_byte_too_long(base)
            err = io.StringIO()
            with unittest.mock.patch.object(sys, "stderr", err):
                idx._save_orphan_ages({"k": time.time()})

            self.assertFalse(os.path.exists(idx._orphan_ages_path()),
                             "the write is genuinely refused — this test is about the silence, "
                             "not about making the write succeed")
            line = err.getvalue()
            self.assertIn("orphan-ages sidecar", line,
                          f"the refusal must reach the operator, not die in a `pass`: {line!r}")
            for number in ("17", str(edge + 1), str(limit), str(edge)):
                self.assertIn(number, line,
                              f"and must carry the arithmetic — {number} missing from {line!r}")

    def test_a_reap_still_finishes_when_its_sidecar_cannot_be_written(self):
        """The other half of the same finding: the `except OSError: pass` exists for a reason.
        Sweep housekeeping must not fail a search, so the loud refusal is a warning and not a
        raise, and `_reap` returns its result with the too-long name in force."""
        with tempfile.TemporaryDirectory() as base:
            idx, _edge, _limit = self._index_whose_sidecar_is_one_byte_too_long(base)
            cache = {"k0": [0.0], "k1": [0.0]}
            with unittest.mock.patch.object(sys, "stderr", io.StringIO()):
                out = idx._reap(cache, {"k0"})
            self.assertEqual(set(out), {"k0"}, "the reap's own result is unaffected")

    def test_a_transient_sidecar_failure_keeps_its_silence(self):
        """The control that separates this fix from the wrong one. Warning on every OSError
        would pass the test above as well, and would put a full disk or a read-only mount on
        stderr in the middle of every search — the failures the `pass` was written to absorb,
        every one of which may be gone by the next run."""
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base)
            os.makedirs(os.path.dirname(idx.cache_path), exist_ok=True)

            def _no_space(*a, **kw):
                raise OSError(errno.ENOSPC, "No space left on device")

            err = io.StringIO()
            with unittest.mock.patch.object(searchmod, "atomic_write_json", _no_space):
                with unittest.mock.patch.object(sys, "stderr", err):
                    idx._save_orphan_ages({"k": time.time()})
            self.assertEqual(err.getvalue(), "", "a transient failure stays quiet")


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

    def test_stale_temps_are_reclaimed_but_in_flight_ones_are_not(self):
        """V2 from the 2026-08-03 sandboxed pass. The old predictable "<target>.tmp" was
        self-limiting — a hard kill left one file and the next write truncated that same path.
        Unique temp names leave a NEW orphan per kill, so repeated OOM-kills during a
        checkpoint would accumulate 350 MB files in the very directory this reaper bounds.
        Age is the discriminator: an in-flight write is seconds old."""
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base)
            cache_dir = os.path.dirname(idx.cache_path)
            os.makedirs(cache_dir, exist_ok=True)
            # From the WRITER, not spelled by hand. These fixtures were once six-character tails,
            # a shape `_open_unique_temp` cannot produce, so they exercised nothing once the
            # reaper narrowed to names it recognises; hand-writing twelve hex characters fixed
            # that instance and left the same trap armed for the next width change. Asking the
            # producer removes it (Rule 7) — and the same fixture-shape drift is what left the
            # reclaim's prefix condition unpinned in test_atomicio (sixteenth pass, F5).
            # The TAIL is held still as well (D77): the delete predicate claims only tails
            # carrying a hex letter, so on the real randomness `stale` was unreclaimable about
            # one run in 281 and this went red for a reason that is ruled behaviour.
            import quintessence.atomicio as atomicio
            temps = []
            for i in range(2):
                path = _writer_temp(atomicio, cache_dir,
                                    "embeddings.someid-model-v1.json", seq=i)
                with open(path, "w") as fh:
                    fh.write("{}")
                temps.append(path)
            stale, live = temps
            # 2 HOURS, not 9999 days. Ageing it past the general 60-day cutoff made the test
            # pass whether or not the temp branch exists — the general sweep reclaimed it anyway,
            # so the branch's actual contribution (reclaim at an hour rather than 60 days, a
            # 1440x difference) was invisible. This window is reclaimable ONLY by the temp branch.
            self._age(stale, 2 / 24)        # crash litter
            idx.build_index()
            self.assertFalse(os.path.exists(stale), "an hours-old temp is litter and must be reclaimed")
            self.assertTrue(os.path.exists(live), "a fresh temp may be a write in flight")

    def test_a_dangling_temp_named_symlink_is_reclaimed_like_any_other_litter(self):
        """Twentieth pass, F3. The two reclaim paths disagreed on ONE property: the primitive
        reads the age with `entry.stat(follow_symlinks=False)`, this sweep read it with
        `os.path.getmtime`, which follows. On a DANGLING symlink there is nothing to follow, so
        `getmtime` raised ENOENT, `contextlib.suppress(OSError)` absorbed it, and the `continue`
        on the next line carried the entry past the general age check as well — the shape of the
        sixteenth pass's F2 (a wide skip exiting with `continue`) surviving in one narrow case.
        The link then sat in the cache directory forever, in the one directory this sweep exists
        to bound.

        The link's own mtime is a real, sweepable fact, and it is the fact the primitive would
        have used beside any of the fourteen atomic-write targets. Reading it here makes the two
        paths agree: a name this module could have written is reclaimed at the grace whether the
        inode behind it is a file, a dangling link, or nothing at all.

        FIXTURE FROM THE PRODUCER (Rule 7): the NAME comes from `_open_unique_temp`, so the
        twelve-hex spelling cannot drift out from under the pin the way the six-character tails
        did. Only the name is borrowed — the real file is removed and a symlink to a
        never-created path is put in its place.

        Aged two hours ON THE LINK (`follow_symlinks=False`; the class helper would raise ENOENT
        chasing the missing target): past TEMP_GRACE_SECONDS, far inside the general 60-day
        cutoff, so ONLY the temp branch could remove it. Put `os.path.getmtime` back and the
        first assertion goes red.

        The control comes after the measurement: a FRESH dangling link of the same shape
        survives, so what this pins is an age check that now reads the link, not a new rule that
        deletes every broken symlink it sees."""
        import quintessence.atomicio as atomicio
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_CACHE_GC_DAYS="60")
            cache_dir = os.path.dirname(idx.cache_path)
            os.makedirs(cache_dir, exist_ok=True)

            links = []
            for i in range(2):
                # Tail held still (D77) — the fixture guard below holds only for a tail carrying
                # a hex letter, which is all but about one tail in 281.
                generated = _writer_temp(atomicio, cache_dir, "embeddings.json", seq=i)
                os.unlink(generated)
                os.symlink(os.path.join(base, "no-such-target"), generated)
                self.assertTrue(atomicio.is_generated_temp_name(os.path.basename(generated)),
                                "fixture guard: this pin only has teeth while the name is one the "
                                "sweep's temp branch claims")
                self.assertFalse(os.path.exists(generated), "fixture guard: the link must dangle")
                self.assertTrue(os.path.islink(generated))
                links.append(generated)
            stale, fresh = links
            old = time.time() - 2 * 3600
            os.utime(stale, (old, old), follow_symlinks=False)

            idx.build_index()

            self.assertFalse(os.path.lexists(stale),
                             "a dangling link wearing a name this module writes is litter with a "
                             "readable age, and the sweep must reclaim it rather than let the "
                             "unreadable target exempt it from every age check there is")
            self.assertTrue(os.path.lexists(fresh),
                            "a seconds-old temp may be a write in flight whatever the inode "
                            "behind its name is — the grace still governs")

    def test_a_foreign_temp_lookalike_is_not_put_on_the_one_hour_deadline(self):
        """Seventh pass, F2. `is_temp_name` matches any basename CONTAINING ".tmp.", so a file
        like `notes.tmp.md` read as a temp here and was deleted at an hour rather than at the
        directory's own policy. The reaper now deletes only what it can recognise as its own.

        Aged two hours: past TEMP_GRACE_SECONDS (the window the temp branch reclaims in) but far
        short of the general cutoff, so ONLY the temp branch could remove it.

        MUTATION, performable as written and verified by performing it: add `is_temp_name` to
        this module's import from `.atomicio`, and in `_gc_stale_identity_caches` widen the temp
        branch's condition from `if is_generated_temp_name(name):` to `if is_temp_name(name):`.
        That is the seventh-pass F2 behaviour exactly — `is_temp_name` matches any basename
        CONTAINING `.tmp.`, so `notes.tmp.md` is claimed — and this test goes red. Note that the
        LOOSER-looking `name.endswith(".tmp")` does NOT turn it red: this fixture ends `.md`.
        (The instruction here used to say "revert the `is_own_temp_name` guard"; that function
        was deleted at `eb6f3e0`, so the instruction had become uncarryable in a suite whose
        whole method is mutation — twenty-first pass, F5.)

        This is HALF of the property; the other half is the test below. "Not on the one-hour
        deadline" must not mean "exempt from the sweep", which is what it silently meant while
        the skip was wide."""
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base)
            cache_dir = os.path.dirname(idx.cache_path)
            os.makedirs(cache_dir, exist_ok=True)
            foreign = os.path.join(cache_dir, "notes.tmp.md")
            with open(foreign, "w") as fh:
                fh.write("not ours")
            self._age(foreign, 2 / 24)
            idx.build_index()
            self.assertTrue(os.path.exists(foreign),
                            "a file this reaper does not own must not get a one-hour deadline")

    def test_an_eight_character_tail_is_a_foreign_file_not_our_litter(self):
        """Eighteenth pass, F5. The delete predicate accepted any tail of 8+ `[A-Za-z0-9_]`, not
        the twelve lowercase hex the writer emits, and justified the width by mkstemp temps from
        an earlier build "still on disk after an upgrade". No such disk exists: that code ran only
        on the author's own mirror, for five hours on 2026-08-03.

        What the window did reach are shapes an operator really has. The reviewer measured all
        three of these deleted after ONE HOUR in the embedding-cache directory, whose documented
        age policy is sixty days — a 1440x difference, applied to somebody else's files.

        Aged two hours: past TEMP_GRACE_SECONDS but far short of the general cutoff, so only the
        temp branch could remove them. Restore the 8-character floor and every one of these goes
        red; that is the fail-first for the narrowing, and it is here rather than only against the
        predicate because a predicate test cannot show the 1440x.

        The sibling of this test, one target over, is in test_atomicio.py: `notes.tmp.markdown`
        was reclaimed beside a target called `notes` while `notes.tmp.md` — the case the
        docstring highlighted protecting — survived. A rule that protects the two-letter
        extension and not the eight-letter one is drawn in the wrong place."""
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_CACHE_GC_DAYS="60")
            cache_dir = os.path.dirname(idx.cache_path)
            os.makedirs(cache_dir, exist_ok=True)
            foreign = []
            for name in ("report.tmp.20260804", "backup.tmp.snapshot", "other-tool.tmp.a1b2c3d4"):
                p = os.path.join(cache_dir, name)
                with open(p, "w") as fh:
                    fh.write("somebody else's file")
                self._age(p, 2 / 24)
                foreign.append(p)
            idx.build_index()
            for p in foreign:
                self.assertTrue(os.path.exists(p),
                                f"{os.path.basename(p)} carries a tail this module cannot write, "
                                f"so it is an ordinary file in this directory and must live to "
                                f"the directory's own 60-day policy, not to a one-hour deadline")

    def test_a_foreign_temp_lookalike_is_still_swept_at_the_directory_s_own_age(self):
        """Sixteenth pass, F2. The protect predicate was the WIDE `is_temp_name` — true for any
        basename containing `.tmp.` — and the branch exits with `continue`, which carried such a
        file past the general age check as well. So a `.tmp`-shaped foreign file in the embedding
        cache directory was exempt from QQ_CACHE_GC_DAYS *permanently*, in the one directory this
        sweep exists to bound (the grounded figure: ~80 orphan-ages sidecars vs 3 real caches on a
        live install). Measured at the reviewed tip: a `notes.tmp.md` aged 400 days survived
        forever, where the pre-atomicio commit reaped it.

        Narrowing the protect predicate to `is_generated_temp_name` left all 63 tests in this file
        green — which is the finding restated: nothing here pinned the width in either direction.
        This is the missing assertion. Both `.tmp`-ish spellings a foreign file can wear are
        planted, because both were exempt: a name containing `.tmp.` and the bare legacy one.

        Aged 400 days against the 60-day default, so ONLY the general age check can remove them —
        the temp branch's own window is an hour and it no longer claims these names at all."""
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_CACHE_GC_DAYS="60")
            cache_dir = os.path.dirname(idx.cache_path)
            os.makedirs(cache_dir, exist_ok=True)
            aged = []
            for name in ("notes.tmp.md", "notes.tmp"):
                p = os.path.join(cache_dir, name)
                with open(p, "w") as fh:
                    fh.write("a foreign file, 400 days old")
                self._age(p, 400)
                aged.append(p)
            idx.build_index()
            for p in aged:
                self.assertFalse(os.path.exists(p),
                                 f"{os.path.basename(p)} is not a temp this module generates, so "
                                 f"it is an ordinary file here and the QQ_CACHE_GC_DAYS sweep must "
                                 f"reach it — a `.tmp`-shaped name must not buy permanent exemption")

    def test_a_cache_path_ending_in_tmp_is_still_this_instance_s_own(self):
        """Tenth pass, F1. The `own` set is a HARD CONSTRAINT — it protects the cache this very
        call is about to use and the legacy migration source. It used to be checked AFTER the
        temp branch, which exits with `continue`, so a QQ_CACHE the operator had named
        `…/cache.tmp` never reached it: the delete predicate then in force, `is_own_temp_name`
        (deleted at `eb6f3e0`), called anything ending `.tmp` ours, and the reclaim deleted both
        the live cache and the legacy source an hour after they were written. Plausible
        configuration — the embedding cache really is regenerable scratch.

        Aged two hours: past TEMP_GRACE_SECONDS, far inside the general cutoff, so only the temp
        branch could remove them.

        This test NO LONGER ARMS THE ORDERING, and the claim that it does was false from
        `a056170` onward: narrowing the delete side to `is_generated_temp_name` means a bare
        `embeddings.tmp` is skipped by the temp branch either way, so the `own` check can be
        moved below it and this stays green (fourteenth pass, F2). What survives here is still
        worth pinning — a cache the operator named `.tmp` is not litter — but the ordering is
        pinned by the test below, whose fixture the delete predicate actually matches."""
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_CACHE=os.path.join(base, "cache", "embeddings.tmp"))
            cache_dir = os.path.dirname(idx.cache_path)
            os.makedirs(cache_dir, exist_ok=True)
            for p in (idx.cache_path, idx.legacy_cache_path):
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as fh:
                    fh.write("{}")
                self._age(p, 2 / 24)
            idx.build_index()
            self.assertTrue(os.path.exists(idx.cache_path),
                            "this instance's own cache must survive however it is named")
            self.assertTrue(os.path.exists(idx.legacy_cache_path),
                            "the legacy migration source must survive however it is named")

    def test_a_generated_tail_cache_name_is_still_this_instance_s_own(self):
        """Fourteenth pass, F2 — the same HARD CONSTRAINT as the test above, re-armed with a
        fixture the delete predicate actually matches.

        The ordering (`own` BEFORE the temp branch, which exits with `continue`) went unpinned
        the moment `a056170` narrowed the delete side from `is_own_temp_name` to
        `is_generated_temp_name`: the older fixture's `embeddings.tmp` stopped matching, so the
        mutation the docstring named stopped being visible. The constraint itself stayed
        load-bearing — a QQ_CACHE whose basename carries a generated tail reaches the delete
        branch, and moving the `own` check below it removes the legacy D4 migration source an
        hour after it was written, silently, with the whole suite green.

        The name comes from `_open_unique_temp` itself rather than being spelled by hand (Rule
        7): the shape that matters is the one the writer emits, and the assertion below fails
        loudly if a future change to the predicate or the token width voids this pin the way it
        voided the last one. Only the LEGACY path can be armed — `identity_cache_path` always
        inserts `.<identity>` before the extension, and a token containing a dot is not a
        generated tail — so the identity cache is asserted as documentation, not as a trap.

        Aged two hours: past TEMP_GRACE_SECONDS, far inside the general cutoff, so only the temp
        branch could remove it. Put the `own` test back after the temp branch and this goes
        red."""
        import quintessence.atomicio as atomicio
        with tempfile.TemporaryDirectory() as base:
            cache_dir = os.path.join(base, "cache")
            os.makedirs(cache_dir, exist_ok=True)
            # Tail held still (D77): the fixture guard below wants a name the DELETE predicate
            # claims, and about one tail in 281 is all-decimal, which it does not.
            generated = _writer_temp(atomicio, cache_dir, "embeddings")
            os.unlink(generated)             # the NAME is the fixture; the cache is written below

            idx = make_index(base, QQ_CACHE=generated)
            self.assertTrue(
                atomicio.is_generated_temp_name(os.path.basename(idx.legacy_cache_path)),
                "fixture guard: this pin only has teeth while the cache basename is a name the "
                "sweep's DELETE predicate claims — that is exactly what silently stopped being "
                "true last time")

            for p in (idx.cache_path, idx.legacy_cache_path):
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as fh:
                    fh.write("{}")
                self._age(p, 2 / 24)
            idx.build_index()

            self.assertTrue(os.path.exists(idx.legacy_cache_path),
                            "the legacy migration source must survive a sweep that reads its "
                            "generated-looking name as litter — it is in the `own` set")
            self.assertTrue(os.path.exists(idx.cache_path),
                            "this instance's own cache must survive however it is named")

    def test_a_directory_sweep_claims_only_temps_it_can_recognise(self):
        """Eleventh pass, F1. The previous fix rescued the `own` set and nothing else, so every
        OTHER name ending `.tmp` here was still deleted an hour after it was written: sibling
        identity caches (when the operator names the cache `.tmp`, which is the configuration the
        previous commit's own message called plausible) and any foreign file sharing the
        directory. Both survived at the base commit, protected by the blanket skip this branch
        replaced — so the fix was a regression on everything it did not name.

        A sweep does not know the target basenames, so the only names it can honestly claim are
        the ones carrying the random tail _open_unique_temp generates.

        MUTATION, performable as written and verified by performing it: in
        `_gc_stale_identity_caches`, widen the temp branch's condition from
        `if is_generated_temp_name(name):` to
        `if name.endswith(".tmp") or is_generated_temp_name(name):`, which is what
        `is_own_temp_name` did before `eb6f3e0` deleted it. Both of the first two assertions go
        red (each measured on its own, since the run stops at the first) and the third stays
        green — the litter the writer really made is still reclaimed. (The instruction here used
        to say "put `is_own_temp_name` back" — naming a function that no longer exists, in a
        suite whose whole method is mutation; twenty-first pass, F5.)

        Aged two hours: past TEMP_GRACE_SECONDS, far inside the general cutoff, so only the temp
        branch could remove them."""
        import quintessence.atomicio as atomicio
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_CACHE=os.path.join(base, "cache", "embeddings.tmp"))
            cache_dir = os.path.dirname(idx.cache_path)
            os.makedirs(cache_dir, exist_ok=True)

            sibling = os.path.join(cache_dir, "embeddings.deadbeefcafe0123-qwen3-v1.tmp")
            foreign = os.path.join(cache_dir, "someothertool-session.tmp")
            for p in (sibling, foreign):
                with open(p, "w") as fh:
                    fh.write("not litter")
                self._age(p, 2 / 24)

            # From the producer, not spelled by hand (Rule 7) — this is the one shape a sweep
            # MAY claim, and the reclaim half has to keep working. The tail is held still with
            # it (D77): a sweep may claim only tails carrying a hex letter, so the third
            # assertion was a coin flip about one run in 281.
            litter = _writer_temp(atomicio, cache_dir, "embeddings.json")
            self._age(litter, 2 / 24)

            idx.build_index()

            self.assertTrue(os.path.exists(sibling),
                            "a sibling identity cache is not litter however the operator named it")
            self.assertTrue(os.path.exists(foreign),
                            "a foreign file sharing the cache directory is not this reaper's")
            self.assertFalse(os.path.exists(litter),
                             "a temp the writer actually generated is still reclaimed")

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

    def test_in_flight_tmp_file_never_removed(self):
        """The hard constraint is that a write IN PROGRESS is never swept. This used to age the
        file 9999 days and still demand it survive, which conflated "named .tmp" with "in
        flight" — a 27-year-old temp is not a write in progress, it is litter from a hard kill,
        and unique temp names mean that litter no longer self-limits. Narrowed 2026-08-03 to
        the property actually being protected; the reclaim half is
        test_stale_temps_are_reclaimed_but_in_flight_ones_are_not."""
        with tempfile.TemporaryDirectory() as base:
            idx = make_index(base, QQ_CACHE_GC_DAYS="1")
            cache_dir = os.path.dirname(idx.cache_path)
            os.makedirs(cache_dir, exist_ok=True)
            tmp = os.path.join(cache_dir, "embeddings.inflight-model-v1.json.tmp")
            with open(tmp, "w") as f:
                f.write("{")
            idx.build_index()          # fresh: a write could genuinely be in progress
            self.assertTrue(os.path.exists(tmp),
                            "a temp from a write in flight must never be swept")


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
