# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.write:
the transaction (qq_lock/commit_push), the qq-write engine port (_execute_write and its shrink
guard / --base optimistic-concurrency / prepend merge / essence merge), and the six ported verbs
(update/essence/new/rewrite/finalize/checkpoint-save) plus the `qq check --write` INDEX
auto-fix. Each test drives a REAL git-initialized fixture store — never the live
~/quintessence — matching the phase's absolute rule (write-path testing only ever happens on
throwaway fixtures).

Cross-engine byte-parity against qq-legacy (same write sequence, two stores, diffed) and the
legacy/python-interop story (alternating writers on ONE store) are covered separately by
tests/test-write-parity.sh (a bash suite driving both real binaries) — this file pins the
PYTHON ENGINE's own behavior in isolation, including branches parity testing alone wouldn't
reach on every run (lock timeouts, marker-scoping, the raw shrink-guard path no `qq` verb
exercises today since `rewrite` always passes --replace)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence import write as w
from quintessence.config import Config
from quintessence.findings import Finding
from quintessence.store import LockTimeout, Store


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
