# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.refsresolve (B3 triggered invalidation, the "push" side):
commit-event derivation + events.jsonl append, the suspect transition
(flips ONLY on a real fingerprint change; touch-and-revert marks nothing), the debounce
(suspect once, latest-fp-wins on subsequent changes), git-ref invalidation via the repo-alias
map, pathwatch mode, and the fail-soft/QQ_BIND=0/preserve-foreign-lines guarantees. The
hook-driven end-to-end trigger probe is tests/test-reval.sh's job."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence import refsresolve as rr
from quintessence.config import Config
from quintessence.store import Store

TS = "2026-07-01T10:00:00Z"


def make_store(base: str, **overrides) -> Store:
    over = {"QUINTESSENCE_DIR": os.path.join(base, "store"),
            "QQ_MEMDIR": os.path.join(base, "mem"),
            "QQ_STATE_DIR": os.path.join(base, "state"),
            # fixture repos live under mktemp (= /tmp): neutralize the D6 default exclusion
            # so the transition tests keep driving real file candidates; D6's own tests pass
            # None (registry default) or explicit lists.
            "QQ_BIND_EXCLUDE_ROOTS": ""}
    over.update(overrides)
    return Store(Config(env={}, config_file="/nonexistent", overrides=over))


def write_refs(store: Store, records: list) -> str:
    d = store.state_dir / "refs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "refs.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return str(path)


def read_refs(store: Store) -> list:
    path = store.state_dir / "refs" / "refs.jsonl"
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.startswith("{")]


def read_events(store: Store) -> list:
    path = store.state_dir / "refs" / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln]


def rec(head="T", line_ts=TS, kind="file", ident="/x", fp=None, status="ok", **extra):
    d = {"head": head, "line_ts": line_ts, "kind": kind, "id": ident,
         "fp": fp, "asof": TS, "status": status}
    d.update(extra)
    return d


def sha_fp(path: str) -> str:
    import hashlib
    with open(path, "rb") as fh:
        return "sha256:" + hashlib.sha256(fh.read()).hexdigest()


class ResolveHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = os.path.join(self.tmp, "workrepo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.name", "t"], check=True)
        self.tracked = os.path.join(self.repo, "deploy.sh")
        self._commit("v1\n", "one")
        self.store = make_store(self.tmp,
                                 QQ_BIND_REPOS=f"work={self.repo}")

    def _commit(self, content: str, msg: str) -> str:
        with open(self.tracked, "w") as fh:
            fh.write(content)
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", msg], check=True)
        return subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip()


class TestCommitResolve(ResolveHarness):
    def test_changed_file_ref_flips_suspect_and_event_recorded(self):
        write_refs(self.store, [rec(ident=self.tracked, fp=sha_fp(self.tracked))])
        sha = self._commit("v2\n", "two")
        n = rr.resolve_commit(self.store, self.repo)
        self.assertEqual(n, 1)
        r = read_refs(self.store)[0]
        self.assertEqual(r["status"], "suspect")
        self.assertEqual(r["suspect_fp"], sha_fp(self.tracked))
        self.assertEqual(r["suspect_src"], f"{self.repo}@{sha[:9]}")
        self.assertNotEqual(r["fp"], r["suspect_fp"])   # write-time basis untouched
        ev = read_events(self.store)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["repo"], self.repo)
        self.assertEqual(ev[0]["sha"], sha)
        self.assertEqual(ev[0]["changed_paths"], ["deploy.sh"])

    def test_touch_and_revert_marks_nothing(self):
        # the commit names the file but its content fingerprint is back to the bound one —
        # latest-fp-wins says reality matches the claim, so no suspect.
        self._commit("v2\n", "two")
        write_refs(self.store, [rec(ident=self.tracked, fp=sha_fp(self.tracked))])
        self._commit("v3\n", "three")
        self._commit("v2\n", "revert")
        n = rr.resolve_commit(self.store, self.repo)
        self.assertEqual(n, 0)
        self.assertEqual(read_refs(self.store)[0]["status"], "ok")
        self.assertEqual(len(read_events(self.store)), 1)   # event still recorded

    def test_debounce_suspect_flips_once_latest_fp_wins(self):
        write_refs(self.store, [rec(ident=self.tracked, fp=sha_fp(self.tracked))])
        self._commit("v2\n", "two")
        rr.resolve_commit(self.store, self.repo)
        first = read_refs(self.store)[0]
        self._commit("v3\n", "three")
        rr.resolve_commit(self.store, self.repo)
        second = read_refs(self.store)[0]
        self.assertEqual((first["status"], second["status"]), ("suspect", "suspect"))
        self.assertEqual(second["suspect_fp"], sha_fp(self.tracked))   # latest fp won
        self.assertNotEqual(first["suspect_fp"], second["suspect_fp"])
        self.assertEqual(len(read_refs(self.store)), 1)   # one record, updated in place

    def test_touch_suspect_revert_auto_clears_to_ok(self):
        # amendment (iii): change -> suspect, then reality reverts to
        # EXACTLY the written claim -> the re-fingerprint equals the recorded fp and the
        # suspect auto-clears mechanically (no adjudication), dropping the suspect_* fields.
        write_refs(self.store, [rec(ident=self.tracked, fp=sha_fp(self.tracked))])
        self._commit("v2\n", "two")
        rr.resolve_commit(self.store, self.repo)
        self.assertEqual(read_refs(self.store)[0]["status"], "suspect")
        self._commit("v1\n", "revert")
        n = rr.resolve_commit(self.store, self.repo)
        self.assertEqual(n, 1)                       # the auto-clear counts as a change
        r = read_refs(self.store)[0]
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["fp"], sha_fp(self.tracked))   # write-time basis still untouched
        for k in ("suspect_fp", "suspect_asof", "suspect_src"):
            self.assertNotIn(k, r)
        # and a LATER real change flips it suspect again, from a clean slate
        self._commit("v4\n", "four")
        rr.resolve_commit(self.store, self.repo)
        self.assertEqual(read_refs(self.store)[0]["status"], "suspect")

    def test_suspect_missing_at_write_reverts_to_missing_not_ok(self):
        # the None-fp corner of amendment (iii): a born-stale (missing-at-write) referent
        # appeared (-> suspect), then vanished again — fp_now (None) == recorded fp (None) is
        # the same mechanical equality, but the pre-suspect value semantics are
        # missing-at-write, NOT ok (nothing was ever verified to match at write time).
        ghost = os.path.join(self.repo, "ghost.conf")
        write_refs(self.store, [rec(ident=ghost, fp=None, status="missing-at-write")])
        with open(ghost, "w") as fh:
            fh.write("appeared\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "appear"], check=True)
        rr.resolve_commit(self.store, self.repo)
        self.assertEqual(read_refs(self.store)[0]["status"], "suspect")
        os.unlink(ghost)
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "vanish"], check=True)
        n = rr.resolve_commit(self.store, self.repo)
        self.assertEqual(n, 1)
        r = read_refs(self.store)[0]
        self.assertEqual(r["status"], "missing-at-write")
        for k in ("suspect_fp", "suspect_asof", "suspect_src"):
            self.assertNotIn(k, r)

    def test_dir_ref_covering_a_changed_path_reflags(self):
        write_refs(self.store, [rec(ident=self.repo, fp="dirsha256:stale")])
        self._commit("v2\n", "two")
        n = rr.resolve_commit(self.store, self.repo)
        self.assertEqual(n, 1)
        self.assertEqual(read_refs(self.store)[0]["status"], "suspect")

    def test_unrelated_ref_untouched(self):
        other = os.path.join(self.tmp, "elsewhere.txt")
        with open(other, "w") as fh:
            fh.write("z")
        write_refs(self.store, [rec(ident=other, fp="sha256:whatever-stale")])
        self._commit("v2\n", "two")
        self.assertEqual(rr.resolve_commit(self.store, self.repo), 0)
        self.assertEqual(read_refs(self.store)[0]["status"], "ok")

    def test_git_ref_alias_reprint_on_commit(self):
        # bound fp says the sha sits on the default branch; a history rewrite (hard reset)
        # changes containment -> suspect. Cheap simulation: bind a WRONG fp and let any commit
        # in the aliased repo re-fingerprint it.
        sha = self._commit("v2\n", "two")
        write_refs(self.store, [rec(kind="git", ident=f"work@{sha[:7]}",
                                     fp="git:not-the-real-fp:default")])
        self._commit("v3\n", "three")
        n = rr.resolve_commit(self.store, self.repo)
        self.assertEqual(n, 1)
        r = read_refs(self.store)[0]
        self.assertEqual(r["status"], "suspect")
        self.assertTrue(r["suspect_fp"].startswith("git:"))

    def test_git_ref_unchanged_stays_ok(self):
        sha = self._commit("v2\n", "two")
        write_refs(self.store, [rec(kind="git", ident=f"work@{sha[:7]}",
                                     fp=f"git:{sha}:default")])
        self._commit("v3\n", "three")
        self.assertEqual(rr.resolve_commit(self.store, self.repo), 0)
        self.assertEqual(read_refs(self.store)[0]["status"], "ok")

    def test_missing_at_write_appearance_flips_suspect(self):
        newfile = os.path.join(self.repo, "born-later.md")
        write_refs(self.store, [rec(ident=newfile, fp=None, status="missing-at-write")])
        with open(newfile, "w") as fh:
            fh.write("now real\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "birth"], check=True)
        n = rr.resolve_commit(self.store, self.repo)
        self.assertEqual(n, 1)
        r = read_refs(self.store)[0]
        self.assertEqual(r["status"], "suspect")
        self.assertIsNone(r["fp"])
        self.assertTrue(r["suspect_fp"].startswith("sha256:"))

    def test_d2_relpath_bound_ref_flips_on_commit(self):
        # Repo-relative acceptance: a repo-relative extraction ("conf/app.py" prose) is
        # recorded as a file ref under the ABSOLUTE worktree path — which is exactly what
        # makes the existing commit trigger cover it with no resolver change. Bind via the
        # real extractor, then commit a change and watch it flip.
        from quintessence import refs as refs_mod
        nested = os.path.join(self.repo, "conf", "app.py")
        os.makedirs(os.path.dirname(nested))
        with open(nested, "w") as fh:
            fh.write("v1\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "nest"], check=True)
        got = refs_mod.extract_referents("tweaked conf/app.py today", {"work": self.repo},
                                         exclude_roots=())
        self.assertEqual(got, [("file", nested)])
        write_refs(self.store, [rec(ident=nested, fp=sha_fp(nested))])
        with open(nested, "w") as fh:
            fh.write("v2\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "change"], check=True)
        self.assertEqual(rr.resolve_commit(self.store, self.repo), 1)
        self.assertEqual(read_refs(self.store)[0]["status"], "suspect")

    def test_qq_bind_zero_inert(self):
        store0 = make_store(self.tmp, QQ_BIND="0", QQ_BIND_REPOS=f"work={self.repo}")
        write_refs(store0, [rec(ident=self.tracked, fp="sha256:stale")])
        self._commit("v2\n", "two")
        self.assertEqual(rr.resolve_commit(store0, self.repo), 0)
        self.assertEqual(read_refs(store0)[0]["status"], "ok")
        self.assertEqual(read_events(store0), [])

    def test_no_refs_file_event_still_logged_no_crash(self):
        self._commit("v2\n", "two")
        self.assertEqual(rr.resolve_commit(self.store, self.repo), 0)
        self.assertEqual(len(read_events(self.store)), 1)

    def test_foreign_lines_preserved_verbatim(self):
        path = write_refs(self.store, [rec(ident=self.tracked, fp="sha256:stale")])
        with open(path, "a") as fh:
            fh.write("!! not json — must survive the rewrite\n")
        self._commit("v2\n", "two")
        self.assertEqual(rr.resolve_commit(self.store, self.repo), 1)
        with open(path) as fh:
            content = fh.read()
        self.assertIn("!! not json — must survive the rewrite\n", content)

    def test_not_a_repo_fails_soft(self):
        self.assertEqual(rr.resolve_commit(self.store, self.tmp), 0)
        log = self.store.state_dir / "refs" / "resolver.log"
        self.assertTrue(log.is_file())


class TestPathwatch(ResolveHarness):
    def test_file_under_watched_dir_flips(self):
        cfg_dir = os.path.join(self.tmp, "livecfg")
        os.makedirs(cfg_dir)
        target = os.path.join(cfg_dir, "app.conf")
        with open(target, "w") as fh:
            fh.write("a=1\n")
        write_refs(self.store, [rec(ident=target, fp=sha_fp(target))])
        with open(target, "w") as fh:
            fh.write("a=2\n")
        n = rr.resolve_pathwatch(self.store, cfg_dir)
        self.assertEqual(n, 1)
        r = read_refs(self.store)[0]
        self.assertEqual(r["status"], "suspect")
        self.assertEqual(r["suspect_src"], f"pathwatch:{cfg_dir}")
        ev = read_events(self.store)
        self.assertEqual(ev[0]["changed_paths"], [cfg_dir])
        self.assertEqual(ev[0]["source"], "pathwatch")

    def test_unchanged_file_under_watched_dir_stays_ok(self):
        cfg_dir = os.path.join(self.tmp, "livecfg")
        os.makedirs(cfg_dir)
        target = os.path.join(cfg_dir, "app.conf")
        with open(target, "w") as fh:
            fh.write("a=1\n")
        write_refs(self.store, [rec(ident=target, fp=sha_fp(target))])
        self.assertEqual(rr.resolve_pathwatch(self.store, cfg_dir), 0)
        self.assertEqual(read_refs(self.store)[0]["status"], "ok")

    def test_watched_dir_ref_itself_flips(self):
        cfg_dir = os.path.join(self.tmp, "livecfg")
        os.makedirs(cfg_dir)
        write_refs(self.store, [rec(ident=cfg_dir, fp="dirsha256:stale")])
        self.assertEqual(rr.resolve_pathwatch(self.store, cfg_dir), 1)


class TestEventsTrim(ResolveHarness):
    def test_events_trimmed_past_high_water(self):
        d = self.store.state_dir / "refs"
        d.mkdir(parents=True, exist_ok=True)
        pad = json.dumps({"ts": TS, "repo": "/r", "sha": "x" * 40,
                           "changed_paths": ["p" * 200]})
        with open(d / "events.jsonl", "w") as fh:
            for _ in range(rr.EVENTS_MAX_BYTES // len(pad) + 10):
                fh.write(pad + "\n")
        rr.append_event(self.store, {"ts": TS, "repo": "/r", "sha": "last",
                                      "changed_paths": []})
        lines = (d / "events.jsonl").read_text().splitlines()
        self.assertLessEqual(len(lines), rr.EVENTS_KEEP_LINES)
        self.assertIn('"last"', lines[-1])
        # the trim never touched any sibling file (carried A1 condition: *.lock kept)
        lock = d / "probe.lock"
        lock.write_text("held")
        rr.append_event(self.store, {"ts": TS, "repo": "/r", "sha": "again",
                                      "changed_paths": []})
        self.assertTrue(lock.is_file())


class CorpusHarness(unittest.TestCase):
    """Corpus fixture: a corpus git repo of *.md fact-files + a work repo
    holding a real artifact the corpus claims about."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.corpus = os.path.join(self.tmp, "memory")
        self.work = os.path.join(self.tmp, "workrepo")
        for repo in (self.corpus, self.work):
            os.makedirs(repo)
            subprocess.run(["git", "init", "-q", repo], check=True)
            subprocess.run(["git", "-C", repo, "config", "user.email", "t@t"], check=True)
            subprocess.run(["git", "-C", repo, "config", "user.name", "t"], check=True)
        self.artifact = os.path.join(self.work, "deploy.sh")
        with open(self.artifact, "w") as fh:
            fh.write("v1\n")
        subprocess.run(["git", "-C", self.work, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.work, "commit", "-qm", "one"], check=True)
        self.store = make_store(self.tmp,
                                 QQ_BIND_CORPUS=f"mem={self.corpus}",
                                 QQ_BIND_REPOS=f"work={self.work}")

    def _commit_corpus(self, name: str, text: "str | None") -> None:
        p = os.path.join(self.corpus, name)
        if text is None:
            subprocess.run(["git", "-C", self.corpus, "rm", "-q", name], check=True)
        else:
            with open(p, "w") as fh:
                fh.write(text)
            subprocess.run(["git", "-C", self.corpus, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.corpus, "commit", "-qm", "snap"], check=True)

    def _corpus_recs(self, store=None):
        store = store or self.store
        if not (store.state_dir / "refs" / "refs.jsonl").is_file():
            return []
        return [x for x in read_refs(store) if x.get("corpus")]


class TestCorpusBind(CorpusHarness):
    def test_commit_to_corpus_repo_binds_changed_md(self):
        gone = os.path.join(self.tmp, "gone.conf")
        self._commit_corpus("fact.md", f"tool lives at {self.artifact}; old at {gone}\n")
        rr.resolve_commit(self.store, self.corpus)
        recs = {x["id"]: x for x in self._corpus_recs()}
        self.assertEqual(recs[self.artifact]["status"], "ok")
        self.assertEqual(recs[self.artifact]["corpus"], "mem")
        self.assertEqual(recs[self.artifact]["file"], "fact.md")
        self.assertNotIn("head", recs[self.artifact])
        self.assertNotIn("line_ts", recs[self.artifact])
        self.assertEqual(recs[gone]["status"], "missing-at-write")

    def test_memory_index_is_skipped(self):
        self._commit_corpus("MEMORY.md", f"- index line naming {self.artifact}\n")
        rr.resolve_commit(self.store, self.corpus)
        self.assertEqual(self._corpus_recs(), [])

    def test_rebind_replaces_and_delete_drops(self):
        gone = os.path.join(self.tmp, "gone.conf")
        self._commit_corpus("fact.md", f"claims {self.artifact} and {gone}\n")
        rr.resolve_commit(self.store, self.corpus)
        self.assertEqual(len(self._corpus_recs()), 2)
        self._commit_corpus("fact.md", f"claims only {self.artifact} now\n")
        rr.resolve_commit(self.store, self.corpus)
        recs = self._corpus_recs()
        self.assertEqual([x["id"] for x in recs], [self.artifact])   # gone.conf superseded
        self._commit_corpus("fact.md", None)
        rr.resolve_commit(self.store, self.corpus)
        self.assertEqual(self._corpus_recs(), [])

    def test_unconfigured_repo_never_corpus_binds(self):
        store = make_store(self.tmp, QQ_BIND_CORPUS="", QQ_BIND_REPOS=f"work={self.work}")
        self._commit_corpus("fact.md", f"claims {self.artifact}\n")
        rr.resolve_commit(store, self.corpus)
        self.assertEqual(self._corpus_recs(store), [])

    def test_existing_triggers_suspect_corpus_records(self):
        # the whole point of the shared refs.jsonl: a corpus-bound referent changing flips
        # suspect through the EXISTING commit trigger, zero new invalidation code.
        self._commit_corpus("fact.md", f"tool lives at {self.artifact}\n")
        rr.resolve_commit(self.store, self.corpus)
        with open(self.artifact, "w") as fh:
            fh.write("v2\n")
        subprocess.run(["git", "-C", self.work, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.work, "commit", "-qm", "two"], check=True)
        rr.resolve_commit(self.store, self.work)
        rec = [x for x in self._corpus_recs() if x["id"] == self.artifact][0]
        self.assertEqual(rec["status"], "suspect")

    def test_seed_binds_tracked_md_and_is_idempotent(self):
        self._commit_corpus("a.md", f"claims {self.artifact}\n")
        self._commit_corpus("b.md", "no referents in this one\n")
        # wipe what the commit-path bind wrote; seed must rebuild from ls-files alone
        refs_path = self.store.state_dir / "refs" / "refs.jsonl"
        if refs_path.is_file():
            refs_path.unlink()
        n1 = rr.corpus_seed(self.store)
        self.assertEqual(n1, 1)
        n2 = rr.corpus_seed(self.store)
        self.assertEqual(n2, 1)
        self.assertEqual(len(self._corpus_recs()), 1)   # replaced, not duplicated


class TestRetiredAndExcluded(ResolveHarness):
    """`retired` is TERMINAL (the resolver never transitions one — it is neither
    suspectable nor suspect, pinned here so the by-construction skip can't regress), and file
    records under QQ_BIND_EXCLUDE_ROOTS are never candidates (hot-dir records bound before the
    exclusion existed stop
    churning even before they are retired)."""

    def test_retired_record_never_transitions(self):
        write_refs(self.store, [rec(ident=self.tracked, fp=sha_fp(self.tracked),
                                     status="retired")])
        self._commit("v2\n", "two")
        self.assertEqual(rr.resolve_commit(self.store, self.repo), 0)
        r = read_refs(self.store)[0]
        self.assertEqual(r["status"], "retired")
        self.assertNotIn("suspect_fp", r)

    def test_excluded_root_record_is_not_a_candidate(self):
        # same fixture tree, but the store's exclusion covers the repo's root: the changed
        # tracked file would flip suspect without D6 (it does, in every sibling test) — with
        # its root excluded it must stay untouched.
        store = make_store(self.tmp, QQ_BIND_REPOS=f"work={self.repo}",
                            QQ_BIND_EXCLUDE_ROOTS=self.tmp)
        write_refs(store, [rec(ident=self.tracked, fp=sha_fp(self.tracked))])
        self._commit("v2\n", "two")
        self.assertEqual(rr.resolve_commit(store, self.repo), 0)
        self.assertEqual(read_refs(store)[0]["status"], "ok")


def fake_systemctl_run(unit_text="[Unit]\nX=1\n", enabled_state="enabled"):
    """Patch quintessence.refs.subprocess.run with a systemctl double (sweep tests)."""
    def runner(argv, **kw):
        if "cat" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=unit_text, stderr="")
        rc = 0 if enabled_state == "enabled" else 1
        return subprocess.CompletedProcess(argv, rc, stdout=enabled_state + "\n", stderr="")
    from unittest import mock
    return mock.patch("quintessence.refs.subprocess.run", side_effect=runner)


class SweepHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.store = make_store(self.tmp)

    def sweep(self):
        return rr.sweep(self.store)

    def one(self):
        recs = read_refs(self.store)
        self.assertEqual(len(recs), 1)
        return recs[0]


class TestSweepUnits(SweepHarness):
    """Sweep: legacy sha256: unit records migrate by PROVEN equality; unequal = real change."""

    LEGACY_TEXT = "[Unit]\nX=1\n"

    def _legacy_fp(self, text=None):
        import hashlib
        return "sha256:" + hashlib.sha256((text or self.LEGACY_TEXT).encode()).hexdigest()

    def test_legacy_equal_upgrades_in_place(self):
        write_refs(self.store, [rec(kind="unit", ident="x.service",
                                     fp=self._legacy_fp(), status="ok")])
        with fake_systemctl_run(unit_text=self.LEGACY_TEXT):
            c = self.sweep()
        r_ = self.one()
        self.assertTrue(r_["fp"].startswith("sha256e:"))
        self.assertEqual(r_["status"], "ok")
        self.assertEqual(c["upgraded"], 1)
        self.assertEqual(c["suspected"], 0)

    def test_legacy_unequal_suspects(self):
        write_refs(self.store, [rec(kind="unit", ident="x.service",
                                     fp=self._legacy_fp("[Unit]\nOLD=1\n"), status="ok")])
        with fake_systemctl_run(unit_text=self.LEGACY_TEXT):
            c = self.sweep()
        r_ = self.one()
        self.assertEqual(r_["status"], "suspect")
        self.assertTrue(r_["suspect_fp"].startswith("sha256e:"))
        self.assertEqual(c["suspected"], 1)

    def test_legacy_suspect_equal_clears_and_upgrades(self):
        write_refs(self.store, [rec(kind="unit", ident="x.service",
                                     fp=self._legacy_fp(), status="suspect",
                                     suspect_fp="sha256e:whatever",
                                     suspect_asof=TS, suspect_src="t")])
        with fake_systemctl_run(unit_text=self.LEGACY_TEXT):
            c = self.sweep()
        r_ = self.one()
        self.assertEqual(r_["status"], "ok")
        self.assertTrue(r_["fp"].startswith("sha256e:"))
        self.assertNotIn("suspect_fp", r_)
        self.assertEqual(c["upgraded"], 1)

    def test_current_format_unchanged_unit_is_a_noop(self):
        with fake_systemctl_run():
            from quintessence.refs import _fp_unit
            fp_now, _ = _fp_unit("x.service")
        write_refs(self.store, [rec(kind="unit", ident="x.service",
                                     fp=fp_now, status="ok")])
        with fake_systemctl_run():
            c = self.sweep()
        self.assertEqual(self.one()["status"], "ok")
        self.assertEqual(c["suspected"] + c["cleared"] + c["upgraded"], 0)


class TestSweepFilesAndErrors(SweepHarness):
    def test_corpus_file_suspect_reverted_auto_clears(self):
        p = os.path.join(self.tmp, "artifact.txt")
        with open(p, "w") as fh:
            fh.write("v1\n")
        write_refs(self.store, [rec(kind="file", ident=p, fp=sha_fp(p), status="suspect",
                                     suspect_fp="sha256:stale", suspect_asof=TS,
                                     suspect_src="t", corpus="mem", file="f.md")])
        c = self.sweep()
        self.assertEqual(self.one()["status"], "ok")
        self.assertEqual(c["cleared"], 1)

    def test_corpus_file_changed_flips_suspect(self):
        p = os.path.join(self.tmp, "artifact.txt")
        with open(p, "w") as fh:
            fh.write("v1\n")
        old = sha_fp(p)
        with open(p, "w") as fh:
            fh.write("v2\n")
        write_refs(self.store, [rec(kind="file", ident=p, fp=old, status="ok",
                                     corpus="mem", file="f.md")])
        c = self.sweep()
        self.assertEqual(self.one()["status"], "suspect")
        self.assertEqual(c["suspected"], 1)

    def test_head_file_ok_records_are_not_swept_but_head_suspects_clear(self):
        p = os.path.join(self.tmp, "artifact.txt")
        with open(p, "w") as fh:
            fh.write("v1\n")
        stale_fp = "sha256:not-current"
        write_refs(self.store, [
            rec(kind="file", ident=p, fp=stale_fp, status="ok"),          # HEAD ok: B2's job
            rec(kind="file", ident=p, fp=sha_fp(p), status="suspect",     # HEAD suspect,
                suspect_fp=stale_fp, suspect_asof=TS, suspect_src="t",     # reverted referent
                line_ts="2026-07-01T11:00:00Z"),
        ])
        c = self.sweep()
        recs = read_refs(self.store)
        self.assertEqual(recs[0]["status"], "ok")           # untouched (fp still stale-looking)
        self.assertEqual(recs[0]["fp"], stale_fp)
        self.assertEqual(recs[1]["status"], "ok")           # amendment-iii cleared by sweep
        self.assertEqual(c["cleared"], 1)

    def test_fp_error_fills_on_success_and_ages_on_failure(self):
        p = os.path.join(self.tmp, "late.txt")
        with open(p, "w") as fh:
            fh.write("appeared\n")
        gone = os.path.join(self.tmp, "still-gone.txt")
        write_refs(self.store, [
            rec(kind="file", ident=p, fp=None, status="fp-error", corpus="mem", file="a.md"),
            rec(kind="file", ident=gone, fp=None, status="fp-error", corpus="mem",
                file="b.md"),
        ])
        c = self.sweep()
        recs = read_refs(self.store)
        self.assertEqual(recs[0]["status"], "ok")
        self.assertEqual(recs[0]["fp"], sha_fp(p))
        self.assertIn("filled_asof", recs[0])
        self.assertEqual(recs[1]["status"], "fp-error")     # missing != a basis: stays
        self.assertEqual(c["filled"], 1)
        self.assertGreaterEqual(c["fp_error_left"], 1)

    def test_retired_and_excluded_are_never_probed(self):
        p = os.path.join(self.tmp, "artifact.txt")
        with open(p, "w") as fh:
            fh.write("v1\n")
        store = make_store(self.tmp, QQ_BIND_EXCLUDE_ROOTS="/definitely-not-tmp")
        write_refs(store, [
            rec(kind="file", ident=p, fp="sha256:stale", status="retired",
                corpus="mem", file="a.md"),
        ])
        c = rr.sweep(store)
        self.assertEqual(read_refs(store)[0]["status"], "retired")
        self.assertEqual(c["swept"], 0)

    def test_rfile_unreachable_is_skip_not_churn(self):
        from unittest import mock
        write_refs(self.store, [rec(kind="rfile", ident="nas:/tank/x",
                                     fp="sha256:aa", status="ok")])
        store = make_store(self.tmp, QQ_BIND_HOSTS="nas=user@nas")
        def dead_ssh(argv, **kw):
            return subprocess.CompletedProcess(argv, 255, stdout="", stderr="timed out")
        with mock.patch("quintessence.refs.subprocess.run", side_effect=dead_ssh):
            c = rr.sweep(store)
        self.assertEqual(read_refs(store)[0]["status"], "ok")   # unchanged
        self.assertEqual(c["probe_errors"], 1)


if __name__ == "__main__":
    unittest.main()
