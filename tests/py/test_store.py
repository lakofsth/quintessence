# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.store: Store locate + HEAD/memory read access, and state_lock()
(the dedicated $QQ_STATE_DIR/.state.lock — acquire/block/timeout/release)."""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence.config import Config
from quintessence.store import LockTimeout, Store, StorePathError, state_lock


def make_store(base: str) -> Store:
    cfg = Config(env={}, config_file="/nonexistent", overrides={
        "QUINTESSENCE_DIR": os.path.join(base, "store"),
        "QQ_MEMDIR": os.path.join(base, "mem"),
        "QQ_STATE_DIR": os.path.join(base, "state"),
    })
    return Store(cfg)


class TestLocate(unittest.TestCase):
    def test_paths_derive_from_config(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            self.assertEqual(str(store.qdir), os.path.join(base, "store"))
            self.assertEqual(str(store.state_dir), os.path.join(base, "state"))
            self.assertEqual(str(store.journal_dir), os.path.join(base, "store", "journal"))

    def test_list_and_read_heads(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            os.makedirs(store.qdir)
            (store.qdir / "alpha.md").write_text("# Quintessence — alpha\n")
            (store.qdir / "INDEX.md").write_text("index\n")
            (store.qdir / "RUBRIC.md").write_text("rubric\n")
            self.assertEqual(store.list_head_slugs(), ["alpha"])
            self.assertTrue(store.has_head("alpha"))
            self.assertFalse(store.has_head("nope"))
            self.assertEqual(store.read_head("alpha"), "# Quintessence — alpha\n")

    def test_head_path_traversal_guard(self):
        # Regression: a '..'-shaped or absolute topic must not resolve outside qdir. The read
        # verbs (show/brief/path) and essence/finalize all build their path via head_path/
        # journal_subdir, so guarding here blocks the whole class in one place.
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            os.makedirs(store.qdir)
            qroot = str(store.qdir.resolve())
            # a legitimate topic (incl. a dotted name, and a nested relpath inside the store) is fine
            self.assertTrue(str(store.head_path("normal")).endswith("/store/normal.md"))
            self.assertTrue(str(store.head_path("v1.2-plan")).endswith("v1.2-plan.md"))
            self.assertTrue(str(store.head_path("sub/topic").resolve()).startswith(qroot))
            # head_path (base = qdir): a single `../` already escapes the store -> refused
            for bad in ("../secret", "../../etc/passwd", "sub/../../../escape"):
                with self.assertRaises(StorePathError):
                    store.head_path(bad)
            # journal_subdir (base = qdir/journal, one level deeper): needs one more `../` to escape
            for bad in ("../../secret", "../../../etc/passwd"):
                with self.assertRaises(StorePathError):
                    store.journal_subdir(bad)
            # boundary: the guard blocks ESCAPE, not the mere presence of `..` — a `../` that stays
            # inside the store is allowed (and, for finalize, head_path already refuses first anyway).
            self.assertTrue(str(store.journal_subdir("../inside").resolve()).startswith(qroot))
            self.assertTrue(issubclass(StorePathError, ValueError))

    def test_list_and_read_memory(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            os.makedirs(store.memdir)
            (store.memdir / "foo.md").write_text("---\nname: foo\n---\nbody\n")
            (store.memdir / "MEMORY.md").write_text("# Memory Index\n")
            self.assertEqual(store.list_memory_slugs(), ["foo"])

    def test_journal_snapshots(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            jd = store.journal_dir / "alpha"
            os.makedirs(jd)
            (jd / "20260101T000000Z.md").write_text("snap1\n")
            (jd / "20260102T000000Z.md").write_text("snap2\n")
            snaps = store.journal_snapshots("alpha")
            self.assertEqual([p.name for p in snaps],
                              ["20260101T000000Z.md", "20260102T000000Z.md"])
            self.assertEqual(store.journal_snapshots("no-such-topic"), [])

    def test_empty_store_dir_lists_nothing(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            self.assertEqual(store.list_head_slugs(), [])
            self.assertEqual(store.list_memory_slugs(), [])

    def test_read_index_returns_raw_cached_content(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            os.makedirs(store.qdir)
            (store.qdir / "INDEX.md").write_text("whatever was cached, verbatim\n")
            self.assertEqual(store.read_index(), "whatever was cached, verbatim\n")

    def test_list_head_slugs_locale_collation_matches_bash_glob_order(self):
        """Caught by live-store parity testing: under en_US.UTF-8
        (LC_COLLATE), bash's glob orders "selfhood-two-bootstraps" BEFORE
        "self-hosted-claude-code" — collation gives the hyphen little/no weight at the primary
        comparison level — while plain codepoint sorted() (hyphen 0x2D < letters) puts the
        hyphenated name first. Skips gracefully on a host whose available locale doesn't
        actually distinguish the two orderings (nothing to prove there)."""
        import locale
        try:
            locale.setlocale(locale.LC_COLLATE, "")
        except locale.Error:
            self.skipTest("no locale support on this host")
        names = ["self-hosted-claude-code", "selfhood-two-bootstraps"]
        if sorted(names, key=locale.strxfrm) == sorted(names):
            self.skipTest("this host's locale collates punctuation the same as codepoint "
                           "order — can't distinguish the fix from a plain sorted() here")
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            os.makedirs(store.qdir)
            for n in names:
                (store.qdir / f"{n}.md").write_text("# T\n")
            self.assertEqual(store.list_head_slugs(),
                              ["selfhood-two-bootstraps", "self-hosted-claude-code"])


class TestStateLock(unittest.TestCase):
    def test_acquire_and_release(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            with state_lock(store, wait=2):
                self.assertTrue((store.state_dir / ".state.lock").exists())
            # released cleanly -> immediately re-acquirable
            with state_lock(store, wait=2):
                pass

    def test_second_acquirer_blocks_then_times_out(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            got_inner_lock = threading.Event()
            release = threading.Event()

            def holder():
                with state_lock(store, wait=5):
                    got_inner_lock.set()
                    release.wait(5)

            t = threading.Thread(target=holder)
            t.start()
            self.assertTrue(got_inner_lock.wait(2), "holder thread never acquired the lock")
            start = time.monotonic()
            with self.assertRaises(LockTimeout):
                with state_lock(store, wait=0.3):
                    pass
            self.assertGreaterEqual(time.monotonic() - start, 0.25)
            release.set()
            t.join(5)

    def test_lock_serializes_two_threads(self):
        """Two 'writers' racing state_lock() must never interleave inside the critical
        section — the property this lock exists to provide."""
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            log = []
            errors = []

            def worker(tag):
                try:
                    with state_lock(store, wait=5):
                        log.append((tag, "enter"))
                        time.sleep(0.05)
                        log.append((tag, "exit"))
                except Exception as e:  # pragma: no cover - would fail the test below anyway
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
            self.assertEqual(errors, [])
            # every enter must be immediately followed by ITS OWN exit (no interleaving)
            for i in range(0, len(log), 2):
                self.assertEqual(log[i][1], "enter")
                self.assertEqual(log[i + 1][1], "exit")
                self.assertEqual(log[i][0], log[i + 1][0])


if __name__ == "__main__":
    unittest.main()
