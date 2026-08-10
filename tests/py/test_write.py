# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.write:
the transaction (qq_lock/commit_push), the qq-write engine port (_execute_write and its shrink
guard / --base optimistic-concurrency / prepend merge / essence merge), and the six ported verbs
(update/essence/new/rewrite/finalize/checkpoint-save) plus the `qq check --write` INDEX
auto-fix. Each test drives a REAL git-initialized fixture store — never the live
~/quintessence — matching the phase's absolute rule (write-path testing only ever happens on
throwaway fixtures).

Cross-engine byte-parity against qq-legacy was once covered by tests/test-write-parity.sh (a
bash suite driving both real binaries); that suite left the tree with the bash engine itself,
and no cross-engine parity coverage exists anymore — the python engine is the only writer.
This file pins the PYTHON ENGINE's own behavior in isolation, including branches a parity diff
alone wouldn't reach on every run (lock timeouts, marker-scoping, the raw shrink-guard path no
`qq` verb exercises today since `rewrite` always passes --replace)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence import heads
from quintessence import write as w
from quintessence.config import Config
from quintessence.findings import Finding
from quintessence.heads import UpdateItem, count_update_markers, parse as parse_head
from quintessence.store import LockTimeout, Store


# Read at IMPORT, before any test's setUp or addCleanup can touch the process environment.
# The first version of the guard below read os.environ at assertion time and so could never fail:
# TestAgentMarkerOnUpdateLines.setUp registers an addCleanup that pops the variable, and that
# class sorts first, so by the time the guard ran the variable was always gone.
_AMBIENT_SESSION_AT_IMPORT = os.environ.get("CLAUDE_CODE_SESSION_ID")


def make_store(base: str, **overrides) -> Store:
    qdir = os.path.join(base, "store")
    over = {"QUINTESSENCE_DIR": qdir,
            "QQ_MEMDIR": os.path.join(base, "mem"),
            "QQ_STATE_DIR": os.path.join(base, "state")}
    over.update(overrides)
    cfg = Config(env={}, config_file="/nonexistent", overrides=over)
    os.makedirs(qdir, exist_ok=True)
    subprocess.run(["git", "init", "-q", qdir], check=True)
    subprocess.run(["git", "-C", qdir, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", qdir, "config", "user.name", "t"], check=True)
    return Store(cfg)


def git_log(store: Store, fmt: str = "%s") -> list[str]:
    r = subprocess.run(["git", "-C", str(store.qdir), "log", f"--format={fmt}"],
                        capture_output=True, text=True)
    return r.stdout.splitlines()


def read(store: Store, slug: str) -> str:
    with open(store.head_path(slug), encoding="utf-8") as f:
        return f.read()


class TestNewVerb(unittest.TestCase):
    def test_scaffolds_and_commits(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "an essence")
            text = read(store, "alpha")
            self.assertTrue(text.startswith("# Quintessence — alpha\n"))
            self.assertIn("> essence: an essence", text)
            self.assertIn("## RE-ENTER HERE", text)
            self.assertIn("## Notes", text)
            self.assertIn("qq-write: alpha.md", git_log(store))

    def test_default_essence_placeholder_when_none_given(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "")
            self.assertIn("<one-line essence: what this thread is about>", read(store, "alpha"))

    def test_duplicate_refuses_without_touching_the_file(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "first")
            before = read(store, "alpha")
            with self.assertRaises(w.WriteError) as cm:
                w.new(store, "alpha", "dup")
            self.assertEqual(cm.exception.exit_code, 1)
            self.assertIn("already exists", str(cm.exception))
            self.assertEqual(read(store, "alpha"), before)

    def test_dotted_topic_name_gets_md_and_stays_visible(self):
        # Regression: a topic whose NAME contains a dot ('v1.2-plan') must still become
        # <topic>.md — not a literal extension-less file invisible to every read verb.
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            # normalization: dotted bare name -> .md; slash/.md stay literal; plain bare -> .md
            self.assertEqual(w._normalize_target(store, "v1.2-plan"), "v1.2-plan.md")
            self.assertEqual(w._normalize_target(store, "plain"), "plain.md")
            self.assertEqual(w._normalize_target(store, "already.md"), "already.md")
            self.assertEqual(w._normalize_target(store, "sub/topic"), "sub/topic")
            # end-to-end: the HEAD is created AS .md and is visible to the read path
            w.new(store, "v1.2-plan", "dotted")
            self.assertTrue(store.head_path("v1.2-plan").is_file())
            self.assertTrue(str(store.head_path("v1.2-plan")).endswith("v1.2-plan.md"))
            self.assertIn("v1.2-plan", store.list_head_slugs())

    def test_missing_git_identity_raises_clean_writeerror(self):
        # Regression: a fresh box/CI with no git author identity must get a clean, actionable
        # WriteError (exit 1), not an uncaught CalledProcessError traceback.
        with tempfile.TemporaryDirectory() as base:
            qdir = os.path.join(base, "store")
            os.makedirs(qdir)
            subprocess.run(["git", "init", "-q", qdir], check=True)  # NO user.name/email set
            cfg = Config(env={}, config_file="/nonexistent", overrides={
                "QUINTESSENCE_DIR": qdir, "QQ_MEMDIR": os.path.join(base, "mem"),
                "QQ_STATE_DIR": os.path.join(base, "state")})
            store = Store(cfg)
            guard = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_AUTHOR_NAME",
                     "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")
            backup = {k: os.environ.get(k) for k in guard}
            os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
            os.environ["GIT_CONFIG_SYSTEM"] = os.devnull
            for k in guard[2:]:
                os.environ.pop(k, None)
            try:
                with self.assertRaises(w.WriteError) as cm:
                    w.new(store, "smoke", "hi")
                self.assertEqual(cm.exception.exit_code, 1)
                self.assertIn("identity", str(cm.exception).lower())
            finally:
                for k, v in backup.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


class TestUpdateVerb(unittest.TestCase):
    def test_prepends_merge_safe_right_after_title(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")
            w.update(store, "alpha", "first update\n")
            w.update(store, "alpha", "second update\n")
            lines = read(store, "alpha").split("\n")
            self.assertEqual(lines[0], "# Quintessence — alpha")
            self.assertIn("second update", lines[1])
            self.assertIn("first update", lines[2])
            self.assertIn("(created)", lines[3])   # the ORIGINAL creation line, pushed down

    def test_missing_topic_refuses(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            with self.assertRaises(w.WriteError) as cm:
                w.update(store, "ghost", "text\n")
            self.assertEqual(cm.exception.exit_code, 2)
            self.assertIn("needs an existing", str(cm.exception))

    def test_bare_prose_gets_a_fresh_timestamp_stamp(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")
            w.update(store, "alpha", "bare prose, no prefix\n")
            line2 = read(store, "alpha").split("\n")[1]
            self.assertTrue(line2.startswith("> updated: "))
            self.assertIn("bare prose, no prefix", line2)

    def test_already_prefixed_line_is_kept_verbatim(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")
            w.update(store, "alpha", "> note: a verbatim marker line\n")
            self.assertIn("> note: a verbatim marker line", read(store, "alpha").split("\n")[1])

    def test_empty_content_refuses(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")
            with self.assertRaises(w.WriteError) as cm:
                w.update(store, "alpha", "")
            self.assertEqual(cm.exception.exit_code, 2)

    def test_commit_message_shape_matches_legacy_qq_write_prepend(self):
        """`qq update` has always been a thin `qq-write ... --prepend-update` wrapper with no
        `-m` — the real git-log message is qq-write's own default, not "qq update: ..."."""
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")
            w.update(store, "alpha", "text\n")
            self.assertIn("qq-write(prepend): alpha.md", git_log(store))


class TestEssenceVerb(unittest.TestCase):
    def test_refreshes_existing_essence(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")
            w.essence(store, "alpha", "refreshed")
            self.assertIn("> essence: refreshed", read(store, "alpha"))
            self.assertIn("qq essence: alpha", git_log(store))

    def test_creates_essence_line_before_first_body_section_if_absent(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")
            # strip the essence line to simulate a HEAD that never had one
            stripped = "\n".join(ln for ln in read(store, "alpha").split("\n")
                                  if not ln.startswith("> essence:"))
            with open(store.head_path("alpha"), "w", encoding="utf-8") as f:
                f.write(stripped)
            subprocess.run(["git", "-C", str(store.qdir), "commit", "-am", "strip essence"],
                            check=True, capture_output=True)
            w.essence(store, "alpha", "brand new essence")
            text = read(store, "alpha")
            idx_essence = text.index("> essence: brand new essence")
            idx_body = text.index("## RE-ENTER HERE")
            self.assertLess(idx_essence, idx_body)

    def test_missing_head_refuses(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            with self.assertRaises(w.WriteError) as cm:
                w.essence(store, "ghost", "text")
            self.assertEqual(cm.exception.exit_code, 1)
            self.assertIn("qq new ghost first", str(cm.exception))


class TestRewriteVerb(unittest.TestCase):
    def test_replaces_whole_file(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")
            w.rewrite(store, "alpha", "# Quintessence — alpha\n> essence: seed\n\n## Notes\nnew\n")
            self.assertEqual(read(store, "alpha"),
                              "# Quintessence — alpha\n> essence: seed\n\n## Notes\nnew\n")

    def test_missing_topic_refuses_and_points_at_new(self):
        """CARRIED RULING (sanctioned deviation from legacy): a missing topic now REFUSES,
        matching `update`'s behavior, instead of qq-write's old silent unscaffolded create."""
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            with self.assertRaises(w.WriteError) as cm:
                w.rewrite(store, "ghost", "whatever\n")
            self.assertEqual(cm.exception.exit_code, 1)
            self.assertIn("qq new ghost first", str(cm.exception))
            self.assertFalse(store.has_head("ghost"))

    def test_base_stale_refuses_a_concurrent_clobber(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")
            stale_base = w._rev_parse_head(store.qdir)
            w.update(store, "alpha", "a concurrent session's edit\n")   # moves alpha.md
            with self.assertRaises(w.WriteError) as cm:
                w._execute_write(store, "alpha", "stale replace\n", replace=True, base=stale_base)
            self.assertEqual(cm.exception.exit_code, 3)
            self.assertIn("changed since --base", str(cm.exception))
            self.assertIn("a concurrent session's edit", read(store, "alpha"))

    def test_base_matching_current_head_succeeds(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")
            current = w._rev_parse_head(store.qdir)
            w.rewrite(store, "alpha", "# Quintessence — alpha\n> essence: seed\n\nnew body\n")
            self.assertIn("new body", read(store, "alpha"))


class TestShrinkGuard(unittest.TestCase):
    """Exercised via `_execute_write` directly (no `--replace`): the raw qq-write-style call no
    `qq` CLI verb makes today (`rewrite` always passes --replace; see write.py's module
    docstring) — the guard still protects a direct/scripted invocation of the engine, matching
    qq-write's own documented "compose a draft, pipe it back" flow."""

    def test_refuses_when_h1_changes_on_a_big_shrink(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            big = ("# Quintessence — alpha\n> essence: big\n\n## Notes\n" + "x" * 900 + "\n")
            w.new(store, "alpha", "seed")
            w._execute_write(store, "alpha", big, replace=True, base=w._rev_parse_head(store.qdir))
            with self.assertRaises(w.WriteError) as cm:
                w._execute_write(store, "alpha", "# Different Title\nsmall\n")
            self.assertEqual(cm.exception.exit_code, 2)
            self.assertIn("REFUSING", str(cm.exception))
            self.assertIn("big", read(store, "alpha"))   # untouched

    def test_allows_a_big_shrink_when_h1_is_preserved(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            big = ("# Quintessence — alpha\n> essence: big\n\n## Notes\n" + "x" * 900 + "\n")
            w.new(store, "alpha", "seed")
            w._execute_write(store, "alpha", big, replace=True, base=w._rev_parse_head(store.qdir))
            small_same_h1 = "# Quintessence — alpha\n> essence: compacted\n\n## Notes\nsmall\n"
            w._execute_write(store, "alpha", small_same_h1)
            self.assertEqual(read(store, "alpha"), small_same_h1)

    def test_small_files_are_never_guarded(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")   # well under the 800B floor
            w._execute_write(store, "alpha", "# Different Title\ntiny\n")
            self.assertEqual(read(store, "alpha"), "# Different Title\ntiny\n")


class TestFinalizeVerb(unittest.TestCase):
    def test_journals_reindexes_and_commits(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")
            out = w.finalize(store, "alpha")
            self.assertIn("journaled:", out)
            self.assertIn("reindexed + mirrored", out)
            snaps = list((store.journal_dir / "alpha").glob("*.md"))
            self.assertEqual(len(snaps), 1)
            with open(snaps[0], encoding="utf-8") as f:
                self.assertEqual(f.read(), read(store, "alpha"))
            self.assertTrue(store.index_path.is_file())
            self.assertIn("qq finalize: alpha", git_log(store))

    def test_missing_head_refuses(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            with self.assertRaises(w.WriteError) as cm:
                w.finalize(store, "ghost")
            self.assertEqual(cm.exception.exit_code, 1)


class TestIndexAutofix(unittest.TestCase):
    def test_returns_none_when_index_already_fresh(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")
            w.finalize(store, "alpha")   # reindexes + commits INDEX.md
            self.assertIsNone(w.index_autofix_finding(store))

    def test_reindexes_and_commits_when_stale(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")   # no finalize -> INDEX.md never written
            finding = w.index_autofix_finding(store)
            self.assertEqual(finding.cls, "T1 fix")
            self.assertTrue(store.index_path.is_file())
            self.assertIn("qq check: reindex stale INDEX", git_log(store))

    def test_degrades_to_flag_only_when_lock_times_out(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")
            got_lock = threading.Event()
            release = threading.Event()

            def holder():
                with w.qq_lock(store, wait=5):
                    got_lock.set()
                    release.wait(5)

            t = threading.Thread(target=holder)
            t.start()
            self.assertTrue(got_lock.wait(2))
            try:
                finding = w.index_autofix_finding(store, wait=0.2)
                self.assertEqual(finding.cls, "T1 index")
                self.assertEqual(finding.fields.get("kind"), "stale")
            finally:
                release.set()
                t.join(5)


class TestQqLock(unittest.TestCase):
    def test_serializes_two_threads(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            log: list[tuple[int, str]] = []
            errors: list[Exception] = []

            def worker(tag):
                try:
                    with w.qq_lock(store, wait=5):
                        log.append((tag, "enter"))
                        time.sleep(0.05)
                        log.append((tag, "exit"))
                except Exception as e:   # pragma: no cover
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
            self.assertEqual(errors, [])
            # no interleave: every "enter" is immediately followed by ITS OWN "exit"
            for i in range(0, len(log), 2):
                self.assertEqual(log[i][0], log[i + 1][0])

    def test_second_acquirer_times_out(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            got = threading.Event()
            release = threading.Event()

            def holder():
                with w.qq_lock(store, wait=5):
                    got.set()
                    release.wait(5)

            t = threading.Thread(target=holder)
            t.start()
            self.assertTrue(got.wait(2))
            start = time.monotonic()
            with self.assertRaises(LockTimeout):
                with w.qq_lock(store, wait=0.3):
                    pass
            self.assertGreaterEqual(time.monotonic() - start, 0.25)
            release.set()
            t.join(5)


class TestMarkerScoping(unittest.TestCase):
    """A2 (carried ruling): QQ_WRITE_TXN scoped to the `git commit` subprocess invocation ONLY —
    never assigned into os.environ, so nothing else this process spawns inherits it."""

    def test_marker_never_touches_os_environ(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            self.assertNotIn("QQ_WRITE_TXN", os.environ)
            w.new(store, "alpha", "seed")
            self.assertNotIn("QQ_WRITE_TXN", os.environ)
            w.update(store, "alpha", "text\n")
            self.assertNotIn("QQ_WRITE_TXN", os.environ)
            w.essence(store, "alpha", "e2")
            self.assertNotIn("QQ_WRITE_TXN", os.environ)
            w.finalize(store, "alpha")
            self.assertNotIn("QQ_WRITE_TXN", os.environ)

    def test_marker_present_only_on_the_commit_call_not_add_or_status(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")   # get a real commit out of the way first

            calls = []
            real_run = subprocess.run

            def spy(cmd, *args, **kwargs):
                calls.append((list(cmd), kwargs.get("env")))
                return real_run(cmd, *args, **kwargs)

            with mock.patch("quintessence.write.subprocess.run", side_effect=spy):
                w.update(store, "alpha", "another line\n")

            saw_commit_with_marker = False
            for cmd, env in calls:
                if len(cmd) >= 2 and cmd[0] == "git" and "commit" in cmd:
                    self.assertIsNotNone(env)
                    self.assertEqual(env.get("QQ_WRITE_TXN"), str(os.getpid()))
                    saw_commit_with_marker = True
                elif len(cmd) >= 2 and cmd[0] == "git" and ("add" in cmd or "status" in cmd
                                                             or "rev-parse" in cmd
                                                             or "diff" in cmd):
                    if env is not None:
                        self.assertNotIn("QQ_WRITE_TXN", env)
            self.assertTrue(saw_commit_with_marker, "no git commit call observed")


if __name__ == "__main__":
    unittest.main()


class ReviewRegressions20260729(unittest.TestCase):
    """Pins for the 2026-07-29 pre-publication review's write-path findings: lock-1 (compact
    trims from the file's LIVE bytes under the write lock), lock-2 (an uncommitted out-of-band
    edit is absorbed as its own commit and the base guard then refuses — never destroyed),
    engine-001 (same-second journal snapshots never overwrite each other), engine-003 (a failed
    commit rolls the file back, so a retry cannot land a duplicated update-line)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_compact_input_is_lock_time_bytes_and_stray_edit_survives(self):
        w.new(self.store, "t", "e")
        for i in range(8):
            w.update(self.store, "t", f"line {i}")
        # An out-of-band change lands AFTER any pre-read a caller could have done and BEFORE
        # the locked write — the lock-1 race shape. The locked transform must trim the bytes
        # as they are NOW, and the absorb step must put the stray change into history.
        with open(self.store.head_path("t"), "a", encoding="utf-8") as fh:
            fh.write("STRAY-BODY-LINE from a race\n")
        out = w.compact(self.store, "t", 3)
        self.assertIn("trimmed", out)
        self.assertIn("STRAY-BODY-LINE", read(self.store, "t"))
        self.assertTrue(any("absorb out-of-band" in m for m in git_log(self.store)))

    def test_update_absorbs_stray_edit_as_its_own_commit(self):
        """2026-08-09, Thomas's ruling (provenance instead of a visible token): the absorb step
        runs on the PREPEND path too. Until now it was REPLACE-only — data safety was the only
        goal, and a prepend destroys nothing — so a hand edit followed by `qq update` rode the
        update's commit, attributed to the updating session forever. Git is the store's
        provenance layer, so the introducing commit must tell the truth: the stray edit gets
        its own absorb commit, and the update commit's diff carries only what qq composed."""
        w.new(self.store, "t", "e")
        w.update(self.store, "t", "first line")
        with open(self.store.head_path("t"), "a", encoding="utf-8") as fh:
            fh.write("HAND-EDITED note\n")
        w.update(self.store, "t", "second line")
        msgs = git_log(self.store)
        self.assertIn("qq: absorb out-of-band edit to t.md", msgs)
        self.assertLess(msgs.index("qq-write(prepend): t.md"),
                        msgs.index("qq: absorb out-of-band edit to t.md"))
        newest = subprocess.run(["git", "-C", str(self.store.qdir), "log", "-p", "-1",
                                 "--", "t.md"], capture_output=True, text=True).stdout
        self.assertNotIn("HAND-EDITED", newest)     # the update commit is only qq's own write
        self.assertIn("second line", newest)
        self.assertIn("HAND-EDITED note", read(self.store, "t"))   # nothing destroyed

    def test_essence_absorbs_stray_edit_as_its_own_commit(self):
        """Same class, same ruling: `qq essence` commits inline (its own non-engine path), and
        the merge is computed from the live bytes — so without an absorb step the hand edit
        rode the essence commit."""
        w.new(self.store, "t", "e")
        with open(self.store.head_path("t"), "a", encoding="utf-8") as fh:
            fh.write("HAND-EDITED essence-path note\n")
        w.essence(self.store, "t", "a fresh essence")
        msgs = git_log(self.store)
        self.assertIn("qq: absorb out-of-band edit to t.md", msgs)
        newest = subprocess.run(["git", "-C", str(self.store.qdir), "log", "-p", "-1",
                                 "--", "t.md"], capture_output=True, text=True).stdout
        self.assertNotIn("HAND-EDITED", newest)
        self.assertIn("a fresh essence", newest)
        self.assertIn("HAND-EDITED essence-path note", read(self.store, "t"))

    def test_finalize_absorbs_stray_edit_before_snapshotting(self):
        """16479b2 review, finding 1: finalize snapshotted the DIRTY bytes and committed them
        inside its own commit (journal copy), leaving t.md dirty and the hand edit attributed
        to the finalizing session. Absorb-first makes the snapshot a copy of a committed state
        and the HEAD clean afterward."""
        w.new(self.store, "t", "e")
        with open(self.store.head_path("t"), "a", encoding="utf-8") as fh:
            fh.write("HAND-EDITED pre-finalize\n")
        w.finalize(self.store, "t")
        self.assertIn("qq: absorb out-of-band edit to t.md", git_log(self.store))
        head_hist = subprocess.run(["git", "-C", str(self.store.qdir), "log", "-p", "-1",
                                    "--", "t.md"], capture_output=True, text=True).stdout
        self.assertIn("absorb out-of-band", head_hist)   # newest t.md commit IS the absorb
        self.assertIn("HAND-EDITED", head_hist)
        porcelain = subprocess.run(["git", "-C", str(self.store.qdir), "status",
                                    "--porcelain", "--", "t.md"],
                                   capture_output=True, text=True).stdout
        self.assertEqual(porcelain.strip(), "")          # HEAD clean after finalize

    def test_delete_absorbs_stray_edit_so_head_history_keeps_it(self):
        """16479b2 review, finding 1, the worse half: delete removed the dirty HEAD, so the
        hand edit reached history ONLY inside the journal snapshot under the deleting
        session's commit — `git log -p -- t.md` could never answer who wrote it. Absorb-first
        puts it in the HEAD's own history before the removal."""
        w.new(self.store, "t", "e")
        with open(self.store.head_path("t"), "a", encoding="utf-8") as fh:
            fh.write("HAND-EDITED pre-delete\n")
        w.delete(self.store, "t")
        self.assertIn("qq: absorb out-of-band edit to t.md", git_log(self.store))
        head_hist = subprocess.run(["git", "-C", str(self.store.qdir), "log", "-p", "--",
                                    "t.md"], capture_output=True, text=True).stdout
        self.assertIn("HAND-EDITED pre-delete", head_hist)
        self.assertFalse(self.store.head_path("t").is_file())   # still deleted

    def test_finalize_sweeps_every_dirty_head_not_just_its_target(self):
        """2026-08-10 ruling, closing the f1a0a14 review's residual: the INDEX a finalize
        commits derives a line from EVERY head, so path-scoped absorption let the index commit
        carry another topic's hand edit before that topic's own history recorded it. The sweep
        absorbs every dirty head first."""
        w.new(self.store, "t", "e")
        w.new(self.store, "u", "u-essence")
        with open(self.store.head_path("u"), "a", encoding="utf-8") as fh:
            fh.write("HAND-EDITED other-topic note\n")
        w.finalize(self.store, "t")
        msgs = git_log(self.store)
        self.assertIn("qq: absorb out-of-band edit to u.md", msgs)
        self.assertLess(msgs.index("qq finalize: t"),
                        msgs.index("qq: absorb out-of-band edit to u.md"))
        porcelain = subprocess.run(["git", "-C", str(self.store.qdir), "status",
                                    "--porcelain", "--", "u.md"],
                                   capture_output=True, text=True).stdout
        self.assertEqual(porcelain.strip(), "")

    def test_delete_sweeps_every_dirty_head_not_just_its_target(self):
        w.new(self.store, "t", "e")
        w.new(self.store, "u", "u-essence")
        with open(self.store.head_path("u"), "a", encoding="utf-8") as fh:
            fh.write("HAND-EDITED survives t's deletion\n")
        w.delete(self.store, "t")
        self.assertIn("qq: absorb out-of-band edit to u.md", git_log(self.store))
        self.assertIn("HAND-EDITED survives", read(self.store, "u"))

    def test_index_autofix_sweeps_dirty_heads_before_committing_the_index(self):
        w.new(self.store, "t", "e")
        w.update(self.store, "t", "a line")        # update never reindexes -> INDEX now stale
        with open(self.store.head_path("t"), "a", encoding="utf-8") as fh:
            fh.write("HAND-EDITED before autofix\n")
        finding = w.index_autofix_finding(self.store)
        self.assertEqual(finding.cls, "T1 fix")
        msgs = git_log(self.store)
        self.assertIn("qq: absorb out-of-band edit to t.md", msgs)
        self.assertLess(msgs.index("qq check: reindex stale INDEX"),
                        msgs.index("qq: absorb out-of-band edit to t.md"))

    def test_staged_deletion_does_not_wedge_the_sweep(self):
        """0845bb0 review finding: an out-of-band `git rm` (staging is not hook-blocked, only
        commits are) left a staged deletion the sweep could not pass — commit_push blanket
        `git add`-ed a pathspec matching nothing and exited 128, and every sweep-carrying verb
        failed identically until someone resolved the index by hand. A fully-staged change
        needs no add; it needs committing."""
        w.new(self.store, "t", "e")
        w.new(self.store, "victim", "v")
        subprocess.run(["git", "-C", str(self.store.qdir), "rm", "-q", "victim.md"],
                       check=True, capture_output=True)
        out = w.finalize(self.store, "t")          # must NOT raise
        self.assertIn("journal", out)
        self.assertIn("qq: absorb out-of-band edit to victim.md", git_log(self.store))
        porcelain = subprocess.run(["git", "-C", str(self.store.qdir), "status",
                                    "--porcelain"], capture_output=True, text=True).stdout
        self.assertNotIn("victim.md", porcelain)   # deletion committed, store not wedged
        self.assertFalse(self.store.head_path("victim").is_file())

    def test_staged_rename_absorbs_whole_not_half(self):
        """Same finding, the manufactured variant: after an out-of-band `git mv`, absorbing
        only the new name committed the addition half and left the deletion half staged — the
        NEXT sweep verb then wedged. Both halves of a rename land in one absorb commit."""
        w.new(self.store, "t", "e")
        w.new(self.store, "old-name", "o")
        subprocess.run(["git", "-C", str(self.store.qdir), "mv", "old-name.md",
                        "new-name.md"], check=True, capture_output=True)
        w.finalize(self.store, "t")                # must NOT raise
        porcelain = subprocess.run(["git", "-C", str(self.store.qdir), "status",
                                    "--porcelain"], capture_output=True, text=True).stdout
        self.assertNotIn("old-name", porcelain)    # deletion half not left behind
        self.assertNotIn("new-name", porcelain)
        w.finalize(self.store, "t")                # the second verb must not wedge either

    def test_rename_out_of_scope_still_absorbs_the_in_scope_half(self):
        """Post-fb5f3fd review finding: the sweep's scope test ran on the DESTINATION only, so
        a hand `git mv` of a top-level HEAD into a subdirectory was dropped whole — the staged
        rename sat unabsorbed forever (every future sweep discarded it identically), and a
        later `qq new` under the old name silently continued the moved topic's history. The
        in-scope half decides absorption; both halves ride one commit."""
        w.new(self.store, "t", "e")
        w.new(self.store, "leaver", "l")
        (self.store.qdir / "archive").mkdir()
        subprocess.run(["git", "-C", str(self.store.qdir), "mv", "leaver.md",
                        "archive/leaver.md"], check=True, capture_output=True)
        w.finalize(self.store, "t")                # must absorb, not skip
        self.assertIn("qq: absorb out-of-band edit to leaver.md", git_log(self.store))
        porcelain = subprocess.run(["git", "-C", str(self.store.qdir), "status",
                                    "--porcelain"], capture_output=True, text=True).stdout
        self.assertNotIn("leaver", porcelain)      # neither half staged, nothing left to wedge
        w.finalize(self.store, "t")                # and the next sweep verb still runs clean

    def test_rewrite_absorbs_stray_edit_then_refuses(self):
        w.new(self.store, "t", "e")
        w.update(self.store, "t", "first line")
        composed = read(self.store, "t")   # what a session read before the stray edit landed
        with open(self.store.head_path("t"), "a", encoding="utf-8") as fh:
            fh.write("IRREPLACEABLE-STRAY note\n")
        with self.assertRaises(w.WriteError) as cm:
            w.rewrite(self.store, "t", composed)
        self.assertEqual(cm.exception.exit_code, 3)   # base guard: rel moved (the absorb commit)
        self.assertIn("IRREPLACEABLE-STRAY", read(self.store, "t"))   # nothing destroyed
        logp = subprocess.run(["git", "-C", str(self.store.qdir), "log", "-p", "--", "t.md"],
                              capture_output=True, text=True).stdout
        self.assertIn("IRREPLACEABLE-STRAY", logp)    # and it is IN history now

    def test_same_second_snapshots_disambiguate(self):
        w.new(self.store, "t", "e")
        from datetime import datetime as real_dt, timezone as real_tz
        fixed = real_dt(2026, 7, 29, 12, 0, 0, tzinfo=real_tz.utc)
        with mock.patch.object(w, "datetime") as md:
            md.now.return_value = fixed
            w.finalize(self.store, "t")
            w.update(self.store, "t", "between snapshots")
            w.finalize(self.store, "t")
        jdir = self.store.journal_subdir("t")
        snaps = sorted(p.name for p in jdir.iterdir())
        self.assertEqual(len(snaps), 2, snaps)
        self.assertIn("20260729T120000Z.md", snaps)
        self.assertIn("20260729T120000Z-2.md", snaps)
        second = (jdir / "20260729T120000Z-2.md").read_text(encoding="utf-8")
        first = (jdir / "20260729T120000Z.md").read_text(encoding="utf-8")
        self.assertIn("between snapshots", second)
        self.assertNotIn("between snapshots", first)   # the earlier snapshot was not replaced

    def test_failed_commit_leaves_the_index_matching_head(self):
        """Rolling back the working tree is only half of it: commit_push has already `git add`ed
        the failed content, so without unstaging, the index keeps it and the NEXT successful
        write commits the failed content along with its own (2026-07-29 post-fix hunt)."""
        w.new(self.store, "idx", "e")
        hook = self.store.qdir / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        with self.assertRaises(w.WriteError):
            w.update(self.store, "idx", "FAILED LINE")
        hook.unlink()
        staged = subprocess.run(["git", "-C", str(self.store.qdir), "diff", "--cached", "--name-only"],
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(staged, "", f"index still holds the failed write: {staged!r}")
        w.update(self.store, "idx", "GOOD LINE")
        self.assertNotIn("FAILED LINE", read(self.store, "idx"))
        last = subprocess.run(["git", "-C", str(self.store.qdir), "show", "--stat", "--format=%s", "HEAD"],
                              capture_output=True, text=True).stdout
        self.assertNotIn("FAILED", last)

    def test_parent_death_tieoff_is_skipped_in_a_threaded_process(self):
        """preexec_fn forks and then runs Python in the child, which CPython documents as unsafe
        with threads — so the parent-death tie-off must be offered ONLY to a single-threaded
        process (the CLI), never to a concurrent host like an MCP server serving write verbs."""
        import threading as _th
        self.assertIsNotNone(w._preexec())          # single-threaded: tie-off offered
        started, release = _th.Event(), _th.Event()

        def _hold():
            started.set()
            release.wait(5)

        t = _th.Thread(target=_hold)
        t.start()
        try:
            started.wait(5)
            self.assertIsNone(w._preexec())          # threaded: never
        finally:
            release.set()
            t.join()
        # and a write still succeeds either way
        w.new(self.store, "threaded", "e")
        self.assertIn("threaded", read(self.store, "threaded"))

    def test_failed_commit_rolls_back_no_duplicate_on_retry(self):
        w.new(self.store, "t", "e")
        before = read(self.store, "t")
        hook = self.store.qdir / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        with self.assertRaises(w.WriteError):
            w.update(self.store, "t", "DOOMED LINE")
        self.assertEqual(read(self.store, "t"), before)   # disk matches HEAD again
        hook.unlink()
        w.update(self.store, "t", "DOOMED LINE")          # the natural retry
        self.assertEqual(read(self.store, "t").count("DOOMED LINE"), 1)


class JournalSnapshotIOFailure20260729(unittest.TestCase):
    """Verification pass (2026-07-29, post-fix): `shutil.copy(f, snap)` in finalize()/delete()
    was unguarded — a read-only journal directory raised a raw PermissionError traceback
    instead of the clean WriteError every other failure path in this module produces. Chmod-
    based repro (a REAL permission denial), same style as test_failed_commit_leaves_the_index_
    matching_head above (a real failing pre-commit hook, not a mocked subprocess)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = make_store(self.tmp.name)

    def _readonly_journal_dir(self, topic):
        jdir = self.store.journal_subdir(topic)
        jdir.mkdir(parents=True, exist_ok=True)
        jdir.chmod(0o500)   # r-x: listable/stattable, not writable
        self.addCleanup(jdir.chmod, 0o700)   # so TemporaryDirectory cleanup can rmtree it
        return jdir

    def test_finalize_read_only_journal_dir_raises_clean_write_error(self):
        w.new(self.store, "alpha", "seed")
        jdir = self._readonly_journal_dir("alpha")
        with self.assertRaises(w.WriteError) as cm:
            w.finalize(self.store, "alpha")
        self.assertIn("journal snapshot", str(cm.exception))
        self.assertEqual(list(jdir.iterdir()), [])   # no partial snapshot left behind
        self.assertNotIn("qq finalize: alpha", git_log(self.store))   # nothing committed

    def test_compact_propagates_the_same_clean_write_error(self):
        # compact() finalizes first (P9 docstring); the unguarded copy lived on that same path.
        w.new(self.store, "beta", "e")
        for i in range(6):
            w.update(self.store, "beta", f"line {i}")
        jdir = self._readonly_journal_dir("beta")
        with self.assertRaises(w.WriteError) as cm:
            w.compact(self.store, "beta", 3)
        self.assertIn("journal snapshot", str(cm.exception))
        self.assertEqual(list(jdir.iterdir()), [])

    def test_delete_read_only_journal_dir_raises_clean_write_error_and_leaves_head_intact(self):
        w.new(self.store, "gamma", "seed")
        jdir = self._readonly_journal_dir("gamma")
        with self.assertRaises(w.WriteError) as cm:
            w.delete(self.store, "gamma")
        self.assertIn("journal snapshot", str(cm.exception))
        self.assertEqual(list(jdir.iterdir()), [])          # no partial snapshot left behind
        self.assertTrue(self.store.head_path("gamma").is_file())   # never unlinked
        self.assertNotIn("qq delete: gamma", git_log(self.store))   # nothing committed


# ---- the derived agent marker on update-lines (quintessence.agentid) ------------------------
class TestAgentMarkerOnUpdateLines(unittest.TestCase):
    """`qq update` and `qq new` stamp the writing session's identity into the update-line they
    compose. Driven end-to-end through the real verbs with a fake HOME, so what is pinned is the
    bytes that land in a HEAD — the module's own derivation is pinned in test_agentid.py.

    Every test here sets CLAUDE_CODE_SESSION_ID explicitly. The harness unsets it for the whole
    gate (tests/run.sh, tests/test-py.sh, tests/py/conftest.py) precisely so these bytes do not
    depend on whether an agent or a human ran the suite."""

    SID = "abcd1234-5678-90ab-cdef-1234567890ab"
    MARKER = "[claude-opus-5, session abcd1234]"
    LINE_RE = r"^> updated: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z "

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name
        self.home = os.path.join(self.base, "home")
        proj = os.path.join(self.home, ".claude", "projects", "-home-someone")
        os.makedirs(proj)
        entry = {"type": "assistant",
                 "message": {"role": "assistant", "model": "claude-opus-5", "content": []}}
        with open(os.path.join(proj, f"{self.SID}.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        self.store = make_store(self.base)

    def as_agent(self):
        return mock.patch.dict(os.environ,
                                {"HOME": self.home, "CLAUDE_CODE_SESSION_ID": self.SID})

    def as_human(self):
        env = mock.patch.dict(os.environ, {"HOME": self.home})
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_SESSION_ID", None)
        return env

    def newest_line(self, topic: str) -> str:
        for ln in read(self.store, topic).split("\n"):
            if ln.startswith("> updated:"):
                return ln
        raise AssertionError(f"no update-line in {topic}")

    def test_update_line_carries_the_derived_marker(self):
        with self.as_agent():
            w.new(self.store, "alpha", "an essence")
            w.update(self.store, "alpha", "the claim")
        self.assertRegex(self.newest_line("alpha"),
                          self.LINE_RE + re.escape(self.MARKER) + r" the claim$")

    def test_new_scaffolds_its_created_line_with_the_marker(self):
        with self.as_agent():
            w.new(self.store, "alpha", "an essence")
        self.assertRegex(self.newest_line("alpha"),
                          self.LINE_RE + re.escape(self.MARKER) + r" \(created\)$")

    def test_off_harness_the_line_is_exactly_what_it_always_was(self):
        """Negative control. Not `assertNotIn(marker)` — that passes for a line mangled in some
        other way. The whole line is pinned against the pre-marker shape."""
        with self.as_human():
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            w.new(self.store, "beta", "an essence")
            w.update(self.store, "beta", "the claim")
            self.assertRegex(self.newest_line("beta"), self.LINE_RE + r"the claim$")
            first = [ln for ln in read(self.store, "beta").split("\n")
                     if ln.startswith("> updated:")][-1]
            self.assertRegex(first, self.LINE_RE + r"\(created\)$")

    def test_the_negative_control_could_have_failed(self):
        """Rule 3: same store, same verbs, same assertions — only the environment differs."""
        with self.as_agent():
            w.new(self.store, "gamma", "an essence")
            w.update(self.store, "gamma", "the claim")
        self.assertNotRegex(self.newest_line("gamma"), self.LINE_RE + r"the claim$")

    def test_the_marker_does_not_disturb_the_line_stamp(self):
        """The seam. `UpdateItem.timestamp` is the join key the refs view (B2) matches an
        update-line on, and the digest ranks HEADs by the same stamp — both read it off the front
        of a line the marker is now inserted into."""
        with self.as_agent():
            w.new(self.store, "delta", "an essence")
            w.update(self.store, "delta", "the claim")
        line = self.newest_line("delta")
        ts = UpdateItem(marker=line).timestamp
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertIn(ts, line.split(self.MARKER)[0])
        newest = parse_head(read(self.store, "delta")).updates[0]
        self.assertEqual(newest.timestamp, ts)            # stamp still leads the line
        self.assertTrue(newest.text.startswith(self.MARKER))   # marker sits after it, in the text

    def test_reapplying_the_marker_does_not_double_stamp(self):
        """A second pass over an already-marked line is a no-op, so no re-entry into the write
        path (an engine that normalizes twice, a caller replaying its own content) can stack
        markers."""
        with self.as_agent():
            once = w._insert_agent_marker("> updated: 2026-01-01T00:00:00Z the claim")
            twice = w._insert_agent_marker(once)
        self.assertEqual(once, f"> updated: 2026-01-01T00:00:00Z {self.MARKER} the claim")
        self.assertEqual(twice, once)

    def test_a_first_line_that_is_not_a_stamped_update_line_is_untouched(self):
        with self.as_agent():
            for content in ("no stamp here", "> essence: not an update line", ""):
                with self.subTest(content=content):
                    self.assertEqual(w._insert_agent_marker(content), content)

    def test_continuation_lines_ride_along_untouched(self):
        with self.as_agent():
            out = w._insert_agent_marker("> updated: 2026-01-01T00:00:00Z the claim\ndetail\nmore")
        self.assertEqual(out.split("\n")[1:], ["detail", "more"])


    def test_a_marker_from_another_session_is_replaced_not_stacked(self):
        """The authoring gate's documented ratification is a DIFFERENT session replaying a queued
        proposal through the same verb, so the common re-stamp is cross-session. Comparing against
        this session's own marker (the first version) let exactly that case through."""
        with self.as_agent():
            out = w._insert_agent_marker(
                "> updated: 2026-01-01T00:00:00Z [claude-sonnet-5, session 1111aaaa] drafted")
        self.assertEqual(out, f"> updated: 2026-01-01T00:00:00Z {self.MARKER} drafted")

    def test_a_marker_whose_session_id_is_not_hex_is_still_stripped(self):
        """The strip regex must admit every character `agentid._SID_RE` does. Spelling its session
        half as `[0-9A-Za-z]{1,64}` looked right against uuid fixtures and silently failed for any
        id with a `-` or `_` in its first eight characters — the double-stamp again. Nothing in the
        suite noticed, because every fixture id happened to start with eight hex digits."""
        with self.as_agent():
            out = w._insert_agent_marker(
                "> updated: 2026-01-01T00:00:00Z [claude-opus-5, session my-sess-] drafted")
            out2 = w._insert_agent_marker(
                "> updated: 2026-01-01T00:00:00Z [claude-opus-5, session abcd_123] drafted")
        self.assertEqual(out, f"> updated: 2026-01-01T00:00:00Z {self.MARKER} drafted")
        self.assertEqual(out2, f"> updated: 2026-01-01T00:00:00Z {self.MARKER} drafted")

    def test_the_session_field_is_bounded_to_what_marker_can_emit(self):
        """`marker()` emits `sid[:8]`, so anything longer is not a marker and must survive as
        prose. The bound is the only thing limiting what the stripper eats; without it the field
        ran to 64 characters of ordinary words."""
        with self.as_agent():
            out = w._insert_agent_marker(
                "> updated: 2026-01-01T00:00:00Z [notes, session retrospective] the real text")
        self.assertIn("[notes, session retrospective] the real text", out)

    def test_a_line_already_doubled_by_the_old_bug_comes_back_clean(self):
        """`count=1` half-repaired it, leaving one stale marker behind."""
        with self.as_agent():
            out = w._insert_agent_marker(
                "> updated: 2026-01-01T00:00:00Z [claude-sonnet-5, session 1111aaaa] "
                "[claude-haiku-4-5, session 2222bbbb] drafted")
        self.assertEqual(out, f"> updated: 2026-01-01T00:00:00Z {self.MARKER} drafted")

    def test_a_wiki_link_opening_the_text_is_not_eaten(self):
        """The marker regex is matched by shape, narrowly, so ordinary prose that merely starts
        with a bracket survives."""
        with self.as_agent():
            out = w._insert_agent_marker("> updated: 2026-01-01T00:00:00Z [[some-head]] see this")
        self.assertEqual(out, f"> updated: 2026-01-01T00:00:00Z {self.MARKER} [[some-head]] see this")


class TestHarnessNeutralizesTheAmbientSession(unittest.TestCase):
    def test_the_suite_runs_with_no_ambient_session_id(self):
        """Pins the three `unset CLAUDE_CODE_SESSION_ID` lines (tests/run.sh, tests/test-py.sh,
        tests/py/conftest.py) that the review found unpinned. Without them this suite's
        update-line assertions quietly depend on whether an agent or a human invoked it — and it
        is agents who run it. Delete any of the three and this goes red for them, green for a
        human, which is the asymmetry itself."""
        self.assertIsNone(_AMBIENT_SESSION_AT_IMPORT,
                          "the runner must neutralize CLAUDE_CODE_SESSION_ID before python starts")


class TestCallerCannotFabricateAStamp(unittest.TestCase):
    """`_strip_caller_stamp` removed ONE `> updated: `, and a '>'-leading line is then returned
    verbatim by the normalizer — so a doubled prefix smuggled the caller's own stamp through. A
    2030 stamp wins the newest-line sort, owns the menu's UPDATED column and the digest's age
    ranking, and since the marker landed the line also read as machine-attested."""

    def test_a_doubled_updated_prefix_does_not_smuggle_a_future_stamp(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")
            w.update(store, "alpha",
                     "> updated: > updated: 2030-01-01T00:00:00Z FABRICATED future claim")
            line = [ln for ln in read(store, "alpha").split("\n")
                    if ln.startswith("> updated:")][0]
            self.assertNotIn("2030-01-01", line)
            self.assertIn("FABRICATED future claim", line)

    def test_the_single_prefix_form_was_already_stripped(self):
        """Positive control: the one-deep case the existing surface test covers still behaves,
        so the fixpoint loop did not change what already worked."""
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            w.new(store, "alpha", "seed")
            w.update(store, "alpha", "> updated: 2030-01-01T00:00:00Z single prefix")
            line = [ln for ln in read(store, "alpha").split("\n")
                    if ln.startswith("> updated:")][0]
            self.assertNotIn("2030-01-01", line)


class TestCallerContentCannotBecomeAnUpdateLine(unittest.TestCase):
    """Four review rounds each found another spelling of a caller-injected update-line, because
    each fix RECOGNISED forged stamps and a recogniser must match every reader's grammar — and at
    the time the readers DISAGREED (three stamp grammars across heads/cli). The 2026-08-09
    unification left one reader, and the neutralizer still does not look at stamps: a caller line
    the reader would treat as structure is moved one column right, where the reader treats it as
    prose. Grammar-independent, so a new stamp format cannot reopen it."""

    SPELLINGS = ("2032-01-01", "2032-01-01T00:00Z", "2032-01-01T00:00:00",
                 "2032-01-01T00:00:00.000Z", "2032-01-01T00:00:00+03:00",
                 "2032-01-01t00:00:00Z", "2032-01-01T00:00:00Z")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = make_store(self.tmp.name)
        w.new(self.store, "proj", "seed")

    def newest(self, topic="proj") -> str:
        return [ln for ln in read(self.store, topic).split("\n")
                if ln.startswith("> updated:")][0]

    def test_no_stamp_spelling_can_win_the_newest_line(self):
        """Every spelling, not the one the last guard happened to recognise. Six of these landed
        against that guard; only `…T00:00:00Z` was refused."""
        for spelling in self.SPELLINGS:
            with self.subTest(spelling=spelling):
                w.update(self.store, "proj",
                         f"> essence: real\n> updated: {spelling} FORGED")
                self.assertNotIn("FORGED", self.newest())
                self.assertNotIn(spelling, self.newest())

    def test_the_forged_text_is_kept_verbatim_one_column_in(self):
        """Neutralised, not refused and not deleted — quoting an update-line is legitimate."""
        w.update(self.store, "proj", "note\n> updated: 2032-01-01T00:00:00Z QUOTED")
        self.assertIn(" > updated: 2032-01-01T00:00:00Z QUOTED", read(self.store, "proj"))

    def test_a_caller_line_never_adds_an_update_line_to_the_head(self):
        before = count_update_markers(read(self.store, "proj"))
        w.update(self.store, "proj",
                 "a\n> updated: 2032-01-01 X\n> updated: 2033-01-01T00:00:00Z Y")
        self.assertEqual(count_update_markers(read(self.store, "proj")), before + 1)

    def test_essence_and_new_neutralize_too(self):
        w.essence(self.store, "proj", "real\n> updated: 2032-01-01 FORGED")
        w.new(self.store, "fresh", "seed\n> updated: 2033-01-01 FORGED")
        for topic in ("proj", "fresh"):
            for ln in read(self.store, topic).split("\n"):
                self.assertNotIn("FORGED", ln) if ln.startswith("> updated:") else None

    def test_a_non_update_marker_first_line_still_gets_stamped(self):
        """The root the forgery grew from: any '>'-leading first line passed through verbatim, so
        `> essence: …` produced content qq never stamped, leaving the caller's own line newest."""
        w.update(self.store, "proj", "> essence: not an update line")
        self.assertRegex(self.newest(), r"^> updated: \d{4}-\d{2}-\d{2}T")
        self.assertIn("> essence: not an update line", self.newest())

    def test_ordinary_and_past_stamped_writes_still_land(self):
        """Positive control: nothing is refused, and a quoted past stamp survives as text."""
        w.update(self.store, "proj", "an ordinary note")
        w.update(self.store, "proj", "quoting\n> updated: 2020-01-01T00:00:00Z an old line")
        text = read(self.store, "proj")
        self.assertIn("an ordinary note", text)
        self.assertIn("2020-01-01", text)

    def test_rewrite_still_refuses_a_future_stamp_rather_than_neutralizing(self):
        """`qq rewrite` takes a WHOLE FILE, whose update-lines are legitimately update-lines, so
        neutralising there would corrupt every HEAD it touched. It keeps its refusal and its
        --allow-future escape hatch."""
        whole = read(self.store, "proj").replace("# Quintessence — proj",
                                                  "# Quintessence — proj", 1)
        forged = whole.replace("## RE-ENTER HERE",
                                "> updated: 2032-01-01T00:00:00Z FORGED\n## RE-ENTER HERE", 1)
        with self.assertRaises(w.WriteError) as cm:
            w.rewrite(self.store, "proj", forged)
        self.assertEqual(cm.exception.exit_code, 2)


class TestWritePathSpeaksTheReadersLineModel(unittest.TestCase):
    """The write path's decisions are made on the SAME lines and the SAME grammar the readers
    see (2026-08-09 reader unification). Each test here pins one way the two could differ —
    and each is red if its own defence is reverted (the newline canonicalization, a neutralizer
    branch, the fence close, the guard's reader grammar)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = make_store(self.tmp.name)
        w.new(self.store, "proj", "seed")

    def test_a_lone_cr_cannot_hide_a_forged_update_line(self):
        """Round-five finding (a): readers read text-mode (universal newlines), so a lone \\r IS
        a line break to every reader — a guard that splits on \\n never saw the forged line it
        hid. Canonicalized at ingestion, the neutralizer sees what the readers will."""
        w.update(self.store, "proj", "note\r> updated: 2032-01-01T00:00:00Z FORGED")
        text = read(self.store, "proj")
        self.assertNotIn("\r", text)
        forged = [it for it in heads.update_lines(text) if "FORGED" in it.marker]
        self.assertEqual(forged, [])
        self.assertIn(" > updated: 2032-01-01T00:00:00Z FORGED", text)   # landed, neutralized

    def test_crlf_content_lands_as_lf(self):
        w.update(self.store, "proj", "first\r\nsecond line")
        text = read(self.store, "proj")
        self.assertNotIn("\r", text)
        self.assertIn("second line", text)

    def test_a_caller_body_header_cannot_bury_older_update_lines(self):
        """A column-0 '## ' in caller content would END the header region: every older
        update-line below it becomes body — invisible to count, brief, menu and digest at
        once. Neutralized the same way a forged marker is: one column right."""
        before = count_update_markers(read(self.store, "proj"))
        w.update(self.store, "proj", "note\n## a sneaky section header")
        text = read(self.store, "proj")
        self.assertEqual(count_update_markers(text), before + 1)
        self.assertIn(" ## a sneaky section header", text)

    def test_a_caller_essence_line_cannot_capture_the_essence_column(self):
        """A column-0 '> essence:' in caller content lands ABOVE the real essence, and the
        essence read is first-wins — so before neutralization the caller's line won the menu
        column. `qq essence` is the verb for changing an essence."""
        w.update(self.store, "proj", "note\n> essence: HIJACKED")
        _, ess = heads.head_meta(read(self.store, "proj"))
        self.assertEqual(ess, "seed")
        self.assertIn(" > essence: HIJACKED", read(self.store, "proj"))

    def test_a_fenced_quoted_example_lands_verbatim_and_stays_inert(self):
        """Fencing is the CLEAN way to quote an update-line: inside a caller-supplied balanced
        fence nothing is indented, and the reader treats the example as prose."""
        before = count_update_markers(read(self.store, "proj"))
        w.update(self.store, "proj",
                 "an example:\n```\n> updated: 2032-01-01T00:00:00Z EXAMPLE\n```")
        text = read(self.store, "proj")
        self.assertIn("\n> updated: 2032-01-01T00:00:00Z EXAMPLE\n", text)   # NOT indented
        self.assertEqual(count_update_markers(text), before + 1)

    def test_a_dangling_caller_fence_is_closed_not_left_to_swallow_the_head(self):
        """An unclosed fence in caller content would put every older update-line below it
        inside a fence — invisible to every reader. Indenting cannot neutralize a fence (the
        fence grammar accepts any indent), so the dangling fence is closed with a visible
        closing-fence line."""
        before = count_update_markers(read(self.store, "proj"))
        w.update(self.store, "proj", "quote:\n````\nfenced text with no closing fence")
        text = read(self.store, "proj")
        self.assertEqual(count_update_markers(text), before + 1)
        self.assertIn("fenced text with no closing fence\n````\n", text)

    def test_rewrite_accepts_its_own_rendered_neutralized_quote(self):
        """Round-five finding (b): the future-stamp guard tolerated leading whitespace where no
        reader does, so `qq show | qq rewrite` refused a HEAD whose own neutralized (indented)
        quote carried a future stamp. The guard's grammar is the reader's now."""
        w.update(self.store, "proj", "note\n> updated: 2032-01-01T00:00:00Z QUOTED")
        whole = read(self.store, "proj")
        self.assertIn(" > updated: 2032-01-01T00:00:00Z QUOTED", whole)
        out = w.rewrite(self.store, "proj", whole)   # must NOT raise
        self.assertIn("proj", out)

    def test_rewrite_refuses_a_future_date_only_stamp(self):
        """The old guard's own spelling demanded a full `T..:..:..Z`, so `2032-01-01` — which
        the reader parses and every surface ranks by — slipped through. Reader grammar closes
        it."""
        whole = read(self.store, "proj")
        forged = whole.replace("## RE-ENTER HERE",
                                "> updated: 2032-01-01 FORGED\n## RE-ENTER HERE", 1)
        with self.assertRaises(w.WriteError) as cm:
            w.rewrite(self.store, "proj", forged)
        self.assertEqual(cm.exception.exit_code, 2)

    def test_a_malformed_first_line_date_cannot_become_the_stamp(self):
        """d78810c review, finding 1: the keep-the-caller's-timestamp branch accepted any
        `YYYY-MM-DDT<anything>` prefix (`_ISO_PREFIX_RE`), looser than the reader — so
        `2099-12-31Team offsite planning` landed as a date-only 2099 stamp that owned the
        digest's ranking until 2099. The acceptance now comes from the reader's grammar,
        narrowed to a full, delimited timestamp: everything else is prose and gets stamped
        now()."""
        w.update(self.store, "proj", "2099-12-31Team offsite planning")
        newest = heads.update_lines(read(self.store, "proj"))[0]
        self.assertNotEqual(newest.timestamp, "2099-12-31")
        self.assertRegex(newest.timestamp, r"^20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertIn("2099-12-31Team offsite planning", newest.rest)

    def test_a_wellformed_delimited_caller_stamp_is_still_kept_by_the_composer(self):
        """Positive control on the same branch: the engine-level composer keeps a real,
        whitespace-delimited timestamp (scripted callers reach it without the stripper). An
        abutting or fractional variant is prose to the reader, so it is prose here too."""
        kept = w._normalize_prepend_first_line("2026-01-01T00:00:00Z a note")
        self.assertEqual(kept, "> updated: 2026-01-01T00:00:00Z a note")
        bare = w._normalize_prepend_first_line("2026-01-01T00:00:00Z")
        self.assertEqual(bare, "> updated: 2026-01-01T00:00:00Z")
        for prose in ("2099-12-31Team offsite", "2026-01-01T00:00:00Zx",
                      "2026-01-01T00:00:00.123Z note", "2026-01-01 was a good day"):
            with self.subTest(prose=prose):
                self.assertRegex(w._normalize_prepend_first_line(prose),
                                 rf"^> updated: \d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}Z {re.escape(prose)}$")

    def test_rewrite_accepts_a_fenced_future_example(self):
        """A fenced example is prose to the reader, so quoting a future-dated line in a fence
        is legal in a rewrite — the guard asks the reader, not its own regex."""
        whole = read(self.store, "proj")
        fenced = whole.replace(
            "## RE-ENTER HERE",
            "## RE-ENTER HERE\n\nexample:\n```\n> updated: 2032-01-01T00:00:00Z EXAMPLE\n```", 1)
        out = w.rewrite(self.store, "proj", fenced)   # must NOT raise
        self.assertIn("proj", out)
