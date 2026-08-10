# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.refs (B1 write-time reality binding): the conservative
extractor (paths/alias-gated commits/units — including the
prose lookalikes that must NOT match), the kind drivers, the REF record shape, the loud
born-stale warning vs the quiet driver-error path, and the two hard invariants — QQ_BIND=0 is
byte-inert, and bind_write NEVER raises (fail-soft even when persistence itself explodes).
End-to-end CLI behavior (parity gate, --ref through real argv, exit codes) is
tests/test-bind.sh's job; this file pins the module in isolation on throwaway fixture stores,
never the live ~/quintessence."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence import refs as r
from quintessence import write as w
from quintessence.config import Config
from quintessence.store import Store


def make_store(base: str, **overrides) -> Store:
    qdir = os.path.join(base, "store")
    over = {"QUINTESSENCE_DIR": qdir,
            "QQ_MEMDIR": os.path.join(base, "mem"),
            "QQ_STATE_DIR": os.path.join(base, "state"),
            # fixture stores live under mktemp (= /tmp): neutralize the D6 default exclusion
            # so bind tests keep exercising extraction; D6's own tests pass None (registry
            # default) or explicit lists.
            "QQ_BIND_EXCLUDE_ROOTS": ""}
    over.update(overrides)
    cfg = Config(env={}, config_file="/nonexistent", overrides=over)
    os.makedirs(qdir, exist_ok=True)
    subprocess.run(["git", "init", "-q", qdir], check=True)
    subprocess.run(["git", "-C", qdir, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", qdir, "config", "user.name", "t"], check=True)
    return Store(cfg)


def read_records(store: Store) -> list[dict]:
    path = store.state_dir / "refs" / "refs.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


@contextlib.contextmanager
def captured_stderr():
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        yield buf


class TestExtraction(unittest.TestCase):
    """The conservative-regex contract: everything asserted NOT to match here is a
    real token shape from the live store's own update-lines (uuid prefixes, key types, URL
    paths, globs) — the false-positive budget's known minefield."""

    def test_tilde_and_slash_rooted_paths(self):
        # exclude_roots=() neutralizes the default exclusions the same way make_store()/test-bind.sh
        # already do for fixture stores — without it this test is NOT hermetic: it passes only
        # by accident on a real dev machine whose $HOME sits outside the default-excluded roots
        # (/run /proc /sys /tmp /dev), and FAILS under any scratch $HOME created via the usual
        # mktemp/TemporaryDirectory pattern (almost always under /tmp), because the ~-expansion
        # then falls under the default /tmp exclusion regardless of what "docs" contains.
        got = r.extract_referents(
            "see ~/docs/a-review.md (read-only) and /etc/hostname.", {}, exclude_roots=())
        self.assertEqual(got, [("file", "~/docs/a-review.md"), ("file", "/etc/hostname")])

    def test_file_line_suffix_stripped(self):
        got = r.extract_referents("bug at ~/infra/x/y.py:42-44, confirmed", {}, exclude_roots=())
        self.assertEqual(got, [("file", "~/infra/x/y.py")])

    def test_relative_paths_never_match(self):
        self.assertEqual(r.extract_referents(
            "boot.py:1532 and eval/results/summary.md and set-rows.cu:490", {}), [])

    def test_url_path_prose_filtered_by_root_check(self):
        # "/api" is not a top-level dir on any sane host — the URL-path prose class
        self.assertEqual(r.extract_referents("HA exposes /api/prometheus for scrape", {}), [])

    def test_url_innards_and_midword_slashes_never_match(self):
        self.assertEqual(r.extract_referents(
            "http://localhost:11434/api w/ and/or 24/7 x1.5 [0.25,4]", {}), [])

    def test_glob_and_variable_tokens_skipped(self):
        self.assertEqual(r.extract_referents(
            "reindex ~/store/*.md then $QQ_STATE_DIR/refs/refs.jsonl", {}), [])

    def test_backticked_and_bracketed_paths_match(self):
        got = r.extract_referents("`/etc/fstab` and [~/docs/x.md]", {}, exclude_roots=())
        self.assertEqual(got, [("file", "/etc/fstab"), ("file", "~/docs/x.md")])

    def test_commit_requires_known_alias(self):
        rm = {"dist": "/nowhere/dist"}
        self.assertEqual(r.extract_referents("dist 0185664 accepted", rm),
                          [("git", "dist@0185664")])
        # no alias / unmapped prefix word / bare sha: all skipped
        self.assertEqual(r.extract_referents(
            "commit e225ad5, session e794791a, tree cef7aed", rm), [])

    def test_commit_alias_at_form_and_case_insensitive(self):
        rm = {"dist": "/nowhere", "kernel": "/nowhere2"}
        self.assertEqual(r.extract_referents("dist @ 8ec6349 reviewed", rm),
                          [("git", "dist@8ec6349")])
        self.assertEqual(r.extract_referents("Kernel d324809 shipped", rm),
                          [("git", "kernel@d324809")])

    def test_hex_lookalike_words_never_match(self):
        # "ed25519" is 7 chars of pure hex — the canonical prose collision
        self.assertEqual(r.extract_referents("only ed25519 keys accepted", {"ed": "/x"}), [])

    def test_units_bare_but_not_path_embedded(self):
        got = r.extract_referents(
            "restart fan-synth.service; file /etc/systemd/system/foo.service", {})
        self.assertEqual(got, [("file", "/etc/systemd/system/foo.service"),
                                ("unit", "fan-synth.service")])

    def test_target_units_out_of_scope(self):
        self.assertEqual(r.extract_referents("pause ca.target freely", {}), [])

    def test_dedup_and_cap(self):
        text = " ".join("/etc/hostname" for _ in range(5))
        self.assertEqual(r.extract_referents(text, {}), [("file", "/etc/hostname")])
        many = " ".join(f"/etc/f{i}.conf" for i in range(r.MAX_REFS + 10))
        self.assertEqual(len(r.extract_referents(many, {})), r.MAX_REFS)

    def test_trailing_punctuation_stripped(self):
        got = r.extract_referents("moved to /etc/hostname. Next", {})
        self.assertEqual(got, [("file", "/etc/hostname")])

    def test_spaced_path_reconstructed_when_it_exists(self):
        """recall-03: the path char class stops at whitespace, so a real file whose path
        contains a space used to get truncated into a shorter, nonexistent token and loudly
        reported born-stale. Extraction now retries a bounded, gated reconstruction across
        embedded spaces, accepting an extension only once it actually resolves on disk — so a
        real spaced path is recovered whole rather than truncated."""
        with tempfile.TemporaryDirectory() as base:
            spaced_dir = os.path.join(base, "qtest space dir")
            os.makedirs(spaced_dir)
            target = os.path.join(spaced_dir, "notes.txt")
            with open(target, "w") as fh:
                fh.write("x")
            got = r.extract_referents(f"see {target} for the real file", {}, exclude_roots=())
            self.assertEqual(got, [("file", target)])

    def test_nonexistent_path_followed_by_prose_is_not_reconstructed(self):
        """The reconstruction attempt is gated on an EXTENDED candidate actually existing — an
        ordinary complete-but-missing path followed by lowercase prose (the common, INTENDED
        born-stale-detection shape: bind the exact path named, let the write-time driver report
        it missing) must bind the path exactly as written, never silently swallowing the next
        few words of prose into the referent id."""
        with tempfile.TemporaryDirectory() as base:
            missing = os.path.join(base, "ghost.txt")
            got = r.extract_referents(f"see {missing} for the real file", {}, exclude_roots=())
            self.assertEqual(got, [("file", missing)])

    def test_complete_existing_path_followed_by_prose_still_binds(self):
        """The reconstruction gate must never suppress the ordinary, extremely common case: a
        complete, real, unspaced path followed by ordinary lowercase prose."""
        got = r.extract_referents("see /etc/hostname for the real file", {}, exclude_roots=())
        self.assertEqual(got, [("file", "/etc/hostname")])


def make_repo(base: str, name: str, filename: str = "a", content: str = "a") -> tuple[str, str]:
    """(repo_path, full_sha) for a one-commit fixture repo."""
    repo = os.path.join(base, name)
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "t"], check=True)
    with open(os.path.join(repo, filename), "w") as fh:
        fh.write(content)
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "one"], check=True)
    sha = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    return repo, sha


class TestBareShaResolution(unittest.TestCase):
    """Bare 7-12-hex tokens bind iff EXACTLY ONE configured repo resolves them as a
    commit — the permissive-extraction/resolution-gate split. The negative cases are the exact
    prose collisions that kept B1 alias-gated (hex-lookalike words, dates, long hex runs)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo_a, self.sha_a = make_repo(self.tmp, "repo-a", "a", "aaa")
        self.repo_b, self.sha_b = make_repo(self.tmp, "repo-b", "b", "bbb")
        self.rm = {"aa": self.repo_a, "bb": self.repo_b}

    def test_unique_resolution_binds(self):
        tok = self.sha_a[:9]
        got = r.extract_referents(f"landed {tok} tonight", self.rm)
        self.assertEqual(got, [("git", f"aa@{tok}")])

    def test_unresolvable_token_silently_skipped(self):
        self.assertEqual(r.extract_referents("landed deadbeef012 tonight", self.rm), [])

    def test_ambiguous_across_repos_skipped(self):
        # a clone shares every commit with its origin: 2 repos resolve -> silent skip
        clone = os.path.join(self.tmp, "clone-a")
        subprocess.run(["git", "clone", "-q", self.repo_a, clone], check=True)
        rm = {"aa": self.repo_a, "cc": clone}
        self.assertEqual(r.extract_referents(f"landed {self.sha_a[:9]}", rm), [])

    def test_alias_fanout_is_one_repo_not_ambiguity(self):
        # several aliases naming ONE worktree (kernel/ca/... pattern) must not fake ambiguity;
        # the first-configured alias owns the bind.
        link = os.path.join(self.tmp, "repo-a-link")
        os.symlink(self.repo_a, link)
        rm = {"first": self.repo_a, "second": link}
        tok = self.sha_a[:8]
        self.assertEqual(r.extract_referents(f"landed {tok}", rm),
                          [("git", f"first@{tok}")])

    def test_dedup_against_alias_gated_ref(self):
        tok = self.sha_a[:9]
        got = r.extract_referents(f"aa {tok} merged; re-verified {tok} later", self.rm)
        self.assertEqual(got, [("git", f"aa@{tok}")])

    def test_hex_lookalike_words_and_dates_do_not_bind(self):
        # permissive extraction, resolution gate: these all MATCH the regex and all fail to
        # resolve — the "date-token residual killed by construction" acceptance
        self.assertEqual(r.extract_referents(
            "only ed25519 keys; snapshot 20260704; window 08131823", self.rm), [])

    def test_long_hex_runs_never_extract(self):
        sha256 = "a" * 64
        self.assertEqual(r.extract_referents(
            f"fp sha256:{sha256} and bare {sha256} and full {self.sha_a}", self.rm), [])

    def test_sha_inside_path_or_after_colon_never_extracts(self):
        tok = self.sha_a[:9]
        self.assertEqual(r.extract_referents(
            f"blob at /nonexistent-root-d1/{tok} and dist@{tok}", self.rm), [])

    def test_no_repos_no_resolution(self):
        with mock.patch("quintessence.refs.subprocess.run",
                         side_effect=AssertionError("no subprocess with empty repo_map")):
            self.assertEqual(r.extract_referents(f"landed {self.sha_a[:9]}", {}), [])

    def test_broken_repo_path_fails_soft(self):
        rm = {"gone": os.path.join(self.tmp, "nope")}
        self.assertEqual(r.extract_referents(f"landed {self.sha_a[:9]}", rm), [])

    def test_one_subprocess_per_repo_not_per_token(self):
        toks = f"{self.sha_a[:9]} {self.sha_b[:9]} deadbeef012 20260704"
        real_run = subprocess.run
        calls = []

        def counting_run(argv, **kw):
            calls.append(argv)
            return real_run(argv, **kw)

        with mock.patch("quintessence.refs.subprocess.run", side_effect=counting_run):
            got = r.extract_referents(f"landed {toks}", self.rm)
        batch = [c for c in calls if "--batch-check" in c]
        self.assertEqual(len(batch), 2)   # one per repo, four tokens fed at once
        self.assertEqual(got, [("git", f"aa@{self.sha_a[:9]}"),
                                ("git", f"bb@{self.sha_b[:9]}")])


class TestRelPathResolution(unittest.TestCase):
    """Multi-segment+extension tokens bind as file refs (ABSOLUTE id) iff exactly one
    unique repo worktree has the file. The adversarial shapes ("and/or.x", URL innards, dot
    segments, extensionless slash-words) are the FP minefield the resolution gate must hold."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo_a, _ = make_repo(self.tmp, "repo-a")
        self.repo_b, _ = make_repo(self.tmp, "repo-b")
        os.makedirs(os.path.join(self.repo_a, "kernel"))
        with open(os.path.join(self.repo_a, "kernel", "backup.py"), "w") as fh:
            fh.write("code\n")
        self.rm = {"aa": self.repo_a, "bb": self.repo_b}
        self.abspath = os.path.join(self.repo_a, "kernel", "backup.py")

    def test_unique_owner_binds_absolute_file_ref(self):
        got = r.extract_referents("patched kernel/backup.py today", self.rm,
                                   exclude_roots=())
        self.assertEqual(got, [("file", self.abspath)])

    def test_line_suffix_stripped(self):
        got = r.extract_referents("bug at kernel/backup.py:42-44 fixed", self.rm,
                                   exclude_roots=())
        self.assertEqual(got, [("file", self.abspath)])

    def test_two_owning_repos_is_ambiguous(self):
        os.makedirs(os.path.join(self.repo_b, "kernel"))
        with open(os.path.join(self.repo_b, "kernel", "backup.py"), "w") as fh:
            fh.write("other\n")
        self.assertEqual(r.extract_referents("patched kernel/backup.py", self.rm), [])

    def test_alias_fanout_dedups_to_one_owner(self):
        link = os.path.join(self.tmp, "repo-a-link")
        os.symlink(self.repo_a, link)
        rm = {"first": self.repo_a, "second": link}
        got = r.extract_referents("patched kernel/backup.py", rm, exclude_roots=())
        self.assertEqual(got, [("file", self.abspath)])

    def test_unresolvable_lookalikes_skipped(self):
        self.assertEqual(r.extract_referents(
            "and/or.x aside, eval/results/summary.md and I/O.txt stay prose", self.rm), [])

    def test_lookalike_that_actually_resolves_binds(self):
        # the gate is resolution, not prose shape: a repo that really HAS "and/or.x" owns it
        os.makedirs(os.path.join(self.repo_a, "and"))
        target = os.path.join(self.repo_a, "and", "or.x")
        with open(target, "w") as fh:
            fh.write("!")
        self.assertEqual(r.extract_referents("weird and/or.x case", self.rm,
                                              exclude_roots=()),
                          [("file", target)])

    def test_extensionless_slash_words_and_url_innards_never_match(self):
        self.assertEqual(r.extract_referents(
            "TCP_CRR/wrk sweep, 24/7, w/ http://host/kernel/backup.py inside a URL", self.rm),
            [])

    def test_rooted_path_owns_its_tail_no_double_extract(self):
        # "~/x/kernel/backup.py" is _PATH_RE's ref; the D2 extractor must not ALSO bind the
        # "kernel/backup.py" tail ('/' before it is not a delimiter). exclude_roots=() per
        # tools-6 — see test_tilde_and_slash_rooted_paths's comment.
        got = r.extract_referents("see ~/elsewhere/kernel/backup.py:9 for detail", self.rm,
                                   exclude_roots=())
        self.assertEqual(got, [("file", "~/elsewhere/kernel/backup.py")])

    def test_dot_segments_rejected_even_when_resolvable(self):
        outside = os.path.join(self.tmp, "escape.py")
        with open(outside, "w") as fh:
            fh.write("x")
        self.assertEqual(r.extract_referents("tricky ../escape.py and ./kernel/backup.py",
                                              self.rm), [])

    def test_directory_with_extension_shape_does_not_bind(self):
        os.makedirs(os.path.join(self.repo_a, "pkg", "mod.d"))
        self.assertEqual(r.extract_referents("under pkg/mod.d now", self.rm), [])

    def test_no_repos_no_binding(self):
        self.assertEqual(r.extract_referents("patched kernel/backup.py", {}), [])


class TestPortExtraction(unittest.TestCase):
    """The port regexes ARE the FP filter (no resolution gate for sockets) — every
    negative here is a shape whose loud missing-at-write warning would be a false alarm."""

    def test_bare_colon_port(self):
        self.assertEqual(r.extract_referents("CA substrate :11435 up", {}),
                          [("port", "11435")])
        self.assertEqual(r.extract_referents(":8085 and (:8000)", {}),
                          [("port", "8085"), ("port", "8000")])

    def test_loopback_and_wildcard_host_forms(self):
        got = r.extract_referents(
            "localhost:8080, 127.0.0.1:8090 and 0.0.0.0:8085 all up", {})
        self.assertEqual(got, [("port", "8080"), ("port", "8090"), ("port", "8085")])

    def test_named_hosts_never_bind(self):
        # a named host's port is a claim about THAT host; ss is local (loud-FP guard)
        self.assertEqual(r.extract_referents(
            "workstation:9090 and grafana.example.net:3000 and nas:51413", {}), [])

    def test_times_ratios_dates_never_bind(self):
        self.assertEqual(r.extract_referents(
            "at 19:22, ratio 1:5000, window 19:2233, stamp 2026-07-04T16:45:32Z", {}), [])

    def test_file_line_suffixes_never_bind(self):
        self.assertEqual(r.extract_referents("boot.py:1532 and set-rows.cu:49022", {}), [])

    def test_url_innards_never_bind(self):
        self.assertEqual(r.extract_referents(
            "probe http://localhost:11434/api and https://127.0.0.1:8443/x", {}), [])

    def test_ipv6_forms(self):
        # "::" never yields a port; the bracketed any-addr form does (dedicated bracket pattern)
        self.assertEqual(r.extract_referents("bound to ::11435 nope", {}), [])
        self.assertEqual(r.extract_referents("bound to [::]:8085 yes", {}),
                          [("port", "8085")])

    def test_length_and_range_limits(self):
        self.assertEqual(r.extract_referents(":123 too short, :99999 too big", {}), [])
        self.assertEqual(r.extract_referents(":65535 edge", {}), [("port", "65535")])

    def test_digit_adjacent_never_binds(self):
        self.assertEqual(r.extract_referents("2026:0704 and 8080:8080 mappings", {}), [])

    def test_dedup(self):
        self.assertEqual(r.extract_referents(":8080 twice :8080", {}),
                          [("port", "8080")])

    def test_bracketed_global_address_with_port(self):
        got = r.extract_referents("listening on [2001:db8::1]:8080 today", {})
        self.assertEqual(got, [("port", "8080")])

    def test_narrowed_delimiter_rejects_stray_punctuation(self):
        """recall-05: the bare-port lookbehind used to be `[^\\w:]` (ANY non-word/non-colon
        char), considerably wider than every other extractor's curated prose-delimiter set —
        so it fired after markdown/table-shaped punctuation that was never a deliberate prose
        boundary (a cost figure, a bullet dash, a bang). Narrowed to the same curated set;
        the bracketed IPv6 form has a dedicated pattern (pinned by test_ipv6_forms and
        test_bracketed_global_address_with_port)."""
        self.assertEqual(r.extract_referents("cost-:11435 estimate", {}), [])
        self.assertEqual(r.extract_referents("really!:11435 no", {}), [])
        self.assertEqual(r.extract_referents("v2/:11435 path-shaped", {}), [])

    def test_narrowed_delimiter_still_covers_documented_prose_shapes(self):
        # every shape the module's own docstring/tests already rely on keeps working: start of
        # text, whitespace, and open-paren (all curated-set members, not the wide fallback).
        self.assertEqual(r.extract_referents("CA substrate :11435 up", {}),
                          [("port", "11435")])
        self.assertEqual(r.extract_referents(":8085 and (:8000)", {}),
                          [("port", "8085"), ("port", "8000")])


class TestPortDriver(unittest.TestCase):
    def _mock_table(self, table):
        return mock.patch("quintessence.refs._ss_listen_table", return_value=table)

    def test_listening_with_comm(self):
        with self._mock_table({11435: ["llama-server"]}):
            self.assertEqual(r._fp_port("11435", {}), ("listen:llama-server", "ok"))

    def test_listening_without_process_visibility(self):
        with self._mock_table({3389: []}):
            self.assertEqual(r._fp_port("3389", {}), ("listen", "ok"))

    def test_not_listening_is_missing(self):
        with self._mock_table({}):
            self.assertEqual(r._fp_port("8085", {}), (None, "missing-at-write"))

    def test_ss_missing_is_driver_error(self):
        with mock.patch("quintessence.refs.subprocess.run", side_effect=OSError("no ss")):
            with self.assertRaises(r.RefDriverError):
                r._fp_port("8085", {})

    def test_one_ss_call_per_write_even_for_many_ports(self):
        ctx: dict = {}
        with self._mock_table({8000: ["a"], 9000: ["b"]}) as m:
            r._fp_port("8000", ctx)
            r._fp_port("9000", ctx)
            r._fp_port("7777", ctx)
        self.assertEqual(m.call_count, 1)

    def test_ss_failure_cached_for_the_write(self):
        ctx: dict = {}
        with mock.patch("quintessence.refs._ss_listen_table",
                         side_effect=r.RefDriverError("down")) as m:
            for _ in range(3):
                with self.assertRaises(r.RefDriverError):
                    r._fp_port("8000", ctx)
        self.assertEqual(m.call_count, 1)

    def test_real_socket_end_to_end(self):
        import socket
        s = socket.socket()
        self.addCleanup(s.close)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            fp, status = r._fp_port(str(port), {})
        except r.RefDriverError:
            self.skipTest("ss not usable on this host")
        self.assertEqual(status, "ok")
        self.assertTrue(fp.startswith("listen"))

    def test_bind_write_port_records_and_warns(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        store = make_store(tmp)
        with self._mock_table({8080: ["nginx"]}):
            with captured_stderr() as buf:
                r.bind_write(store, "t", "serving :8080; :9090 retired")
        self.assertIn("referent does not exist: 9090", buf.getvalue())
        recs = {x["id"]: x for x in read_records(store)}
        self.assertEqual(recs["8080"]["kind"], "port")
        self.assertEqual(recs["8080"]["fp"], "listen:nginx")
        self.assertEqual(recs["8080"]["status"], "ok")
        self.assertEqual(recs["9090"]["status"], "missing-at-write")

    def test_explicit_ref_port(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        store = make_store(tmp)
        with self._mock_table({11435: []}):
            with captured_stderr():
                r.bind_write(store, "t", "no ports in prose", explicit=["port:11435"])
        recs = read_records(store)
        self.assertEqual([(x["kind"], x["id"], x["fp"]) for x in recs],
                          [("port", "11435", "listen")])


def fake_ssh(stdout: str = "", returncode: int = 0, stderr: str = ""):
    """Patch subprocess.run for the rfile driver: capture the argv, return a canned remote
    probe result."""
    calls: list = []

    def runner(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    return mock.patch("quintessence.refs.subprocess.run", side_effect=runner), calls


class TestRemoteHostExtraction(unittest.TestCase):
    """host:path binds ONLY for a QQ_BIND_HOSTS host — the colon-preceded path shape
    is this extractor's alone (it deliberately does not match _PATH_RE), so an unconfigured
    host's path stays entirely unbound."""

    HM = {"nas": "user@nas"}

    def test_configured_host_binds(self):
        got = r.extract_referents("synced nas:/tank/ca-state tonight", {}, self.HM)
        self.assertEqual(got, [("rfile", "nas:/tank/ca-state")])

    def test_home_relative_path_and_case_insensitive_host(self):
        got = r.extract_referents("Nas:~/backups/latest.tar rotated", {}, self.HM)
        self.assertEqual(got, [("rfile", "nas:~/backups/latest.tar")])

    def test_unconfigured_host_is_silent_and_no_file_fallback(self):
        self.assertEqual(r.extract_referents("gateway:/etc/tor/torrc tuned", {}, self.HM), [])
        self.assertEqual(r.extract_referents("nas:/tank/x", {}, None), [])

    def test_trailing_punctuation_and_globs(self):
        got = r.extract_referents("moved to nas:/tank/ca-state. Next", {}, self.HM)
        self.assertEqual(got, [("rfile", "nas:/tank/ca-state")])
        self.assertEqual(r.extract_referents("prune nas:/tank/*.img weekly", {}, self.HM),
                          [])

    def test_url_shapes_never_match(self):
        self.assertEqual(r.extract_referents(
            "see https://nas/x and ssh://nas/tank/y", {}, self.HM), [])


class TestRemoteFileDriver(unittest.TestCase):
    HM = {"nas": "user@nas"}

    def test_small_file_sha(self):
        digest = "ab" * 32
        patcher, calls = fake_ssh(stdout=f"SHA {digest}\n")
        with patcher:
            fp, status = r._fp_rfile("nas:/tank/x.conf", self.HM)
        self.assertEqual((fp, status), (f"sha256:{digest}", "ok"))
        argv, kw = calls[0]
        self.assertEqual(argv[:5], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"])
        self.assertIn("user@nas", argv)
        self.assertEqual(argv[-1], "/tank/x.conf")
        self.assertEqual(kw.get("input"), r._RFILE_SCRIPT)
        self.assertEqual(kw.get("timeout"), r._RFILE_TIMEOUT)

    def test_large_file_stat(self):
        patcher, _ = fake_ssh(stdout="STAT 123456789 1751600000\n")
        with patcher:
            self.assertEqual(r._fp_rfile("nas:/tank/big.img", self.HM),
                              ("stat:123456789:1751600000", "ok"))

    def test_dir_listing_hash(self):
        listing = "a\t10\t1751600000\nb\t20\t1751600001\n"
        patcher, _ = fake_ssh(stdout="DIR\n" + listing)
        with patcher:
            fp, status = r._fp_rfile("nas:/tank/ca-state", self.HM)
        import hashlib
        self.assertEqual(status, "ok")
        self.assertEqual(fp, "rdirsha256:" + hashlib.sha256(listing.encode()).hexdigest())

    def test_missing_remote_path_is_loud_class(self):
        patcher, _ = fake_ssh(stdout="MISSING\n")
        with patcher:
            self.assertEqual(r._fp_rfile("nas:/tank/gone", self.HM),
                              (None, "missing-at-write"))

    def test_unreachable_host_is_driver_error(self):
        patcher, _ = fake_ssh(stdout="", returncode=255, stderr="Connection timed out")
        with patcher:
            with self.assertRaises(r.RefDriverError):
                r._fp_rfile("nas:/tank/x", self.HM)

    def test_timeout_is_driver_error(self):
        with mock.patch("quintessence.refs.subprocess.run",
                         side_effect=subprocess.TimeoutExpired("ssh", 30)):
            with self.assertRaises(r.RefDriverError):
                r._fp_rfile("nas:/tank/x", self.HM)

    def test_unknown_host_and_garbage_output_are_driver_errors(self):
        with self.assertRaises(r.RefDriverError):
            r._fp_rfile("nowhere:/x", self.HM)
        patcher, _ = fake_ssh(stdout="SHA not-a-hash\n")
        with patcher:
            with self.assertRaises(r.RefDriverError):
                r._fp_rfile("nas:/tank/x", self.HM)

    def test_path_is_quoted_so_the_remote_shell_cannot_reinterpret_it(self):
        """ssh joins its command words into one string that the far end's shell re-parses, so the
        driver must render the path inert itself. `--ref rfile:host:path` accepts an unrestricted
        value, and the prose extractor's character class is not this driver's invariant to lean
        on — quoting here is (2026-07-29 review)."""
        patcher, calls = fake_ssh(stdout="MISSING\n")
        with patcher:
            r._fp_rfile("nas:/tank/$(hostname) x;ls", self.HM)
        sent = calls[0][0]
        self.assertEqual(sent[-1], shlex.quote("/tank/$(hostname) x;ls"))
        # and the joined form the remote shell would actually see keeps it as ONE inert word
        self.assertEqual(shlex.split(" ".join(sent[-4:])), ["sh", "-s", "--", "/tank/$(hostname) x;ls"])

    def test_bind_write_unreachable_is_quiet_fp_error(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        store = make_store(tmp, QQ_BIND_HOSTS="nas=user@nas")
        patcher, _ = fake_ssh(stdout="", returncode=255, stderr="unreachable")
        with patcher:
            with captured_stderr() as buf:
                r.bind_write(store, "t", "synced nas:/tank/ca-state")
        err = buf.getvalue()
        self.assertIn("ref fingerprint error", err)
        self.assertNotIn("born stale", err)
        rec = read_records(store)[0]
        self.assertEqual((rec["kind"], rec["id"], rec["status"]),
                          ("rfile", "nas:/tank/ca-state", "fp-error"))

    def test_bind_write_missing_remote_warns_loud(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        store = make_store(tmp, QQ_BIND_HOSTS="nas=user@nas")
        patcher, _ = fake_ssh(stdout="MISSING\n")
        with patcher:
            with captured_stderr() as buf:
                r.bind_write(store, "t", "synced nas:/tank/gone")
        self.assertIn("referent does not exist: nas:/tank/gone", buf.getvalue())
        self.assertEqual(read_records(store)[0]["status"], "missing-at-write")


class TestBindHosts(unittest.TestCase):
    def test_parse_and_normalize(self):
        cfg = Config(env={}, config_file="/nonexistent",
                      overrides={"QQ_BIND_HOSTS": "Nas=user@nas, bad, =y, z="})
        self.assertEqual(r.parse_bind_hosts(cfg), {"nas": "user@nas"})


class TestParseRefArg(unittest.TestCase):
    def test_valid_kinds(self):
        self.assertEqual(r.parse_ref_arg("file:~/x"), ("file", "~/x"))
        self.assertEqual(r.parse_ref_arg("git:dist@abc1234"), ("git", "dist@abc1234"))
        self.assertEqual(r.parse_ref_arg("probe:systemctl is-active x"),
                          ("probe", "systemctl is-active x"))

    def test_malformed_and_unknown_kind(self):
        for bad in ("nokind", "file:", "proc:foo.service", "bogus:x", ":x"):
            self.assertIsNone(r.parse_ref_arg(bad), bad)


class TestRepoAliases(unittest.TestCase):
    def test_parse_and_normalize(self):
        cfg = Config(env={}, config_file="/nonexistent",
                      overrides={"QQ_BIND_REPOS": "Dist=~/d, infra=/home/x/infra, bad, =y, z="})
        self.assertEqual(r.parse_repo_aliases(cfg),
                          {"dist": os.path.expanduser("~/d"), "infra": "/home/x/infra"})


class TestFileDriver(unittest.TestCase):
    def test_file_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "f.txt")
            with open(p, "w") as fh:
                fh.write("hello\n")
            fp, status = r._fp_file(p)
            self.assertEqual(status, "ok")
            import hashlib
            self.assertEqual(fp, "sha256:" + hashlib.sha256(b"hello\n").hexdigest())

    def test_missing_file(self):
        self.assertEqual(r._fp_file("/etc/definitely-not-a-real-file-b1"),
                          (None, "missing-at-write"))

    def test_dir_fingerprint_moves_with_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp1, s1 = r._fp_file(tmp)
            self.assertEqual(s1, "ok")
            self.assertTrue(fp1.startswith("dirsha256:"))
            with open(os.path.join(tmp, "new"), "w") as fh:
                fh.write("x")
            fp2, _ = r._fp_file(tmp)
            self.assertNotEqual(fp1, fp2)

    def test_dangling_symlink_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = os.path.join(tmp, "dangling")
            os.symlink(os.path.join(tmp, "gone"), link)
            self.assertEqual(r._fp_file(link), (None, "missing-at-write"))


class TestUnreadableParentIsDriverError(unittest.TestCase):
    """Amendment (iv): a probe that CANNOT SEE must not report absence. EACCES under an
    unreadable parent = RefDriverError (quiet fp-error downstream), never missing-at-write
    (loud born-stale) — locally and in the D4 remote probe script alike. Provable absence
    (ENOENT/ENOTDIR) still reports missing."""

    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("permission checks are void as root")
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.locked = os.path.join(self.tmp, "locked")
        os.makedirs(os.path.join(self.locked, "inner"))
        with open(os.path.join(self.locked, "inner", "f.txt"), "w") as fh:
            fh.write("x\n")
        os.chmod(self.locked, 0)
        self.addCleanup(os.chmod, self.locked, 0o755)

    def test_unreadable_parent_raises_not_missing(self):
        with self.assertRaises(r.RefDriverError):
            r._fp_file(os.path.join(self.locked, "inner", "f.txt"))

    def test_symlink_into_unreadable_dir_raises_not_missing(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(os.path.join(self.locked, "inner", "f.txt"), link)
        with self.assertRaises(r.RefDriverError):
            r._fp_file(link)

    def test_enotdir_is_still_provable_absence(self):
        plain = os.path.join(self.tmp, "plain.txt")
        with open(plain, "w") as fh:
            fh.write("x\n")
        self.assertEqual(r._fp_file(os.path.join(plain, "child")),
                          (None, "missing-at-write"))

    def _run_rfile_script(self, path: str):
        return subprocess.run(["sh", "-s", "--", path], input=r._RFILE_SCRIPT,
                               capture_output=True, text=True, timeout=10)

    def test_remote_script_unreadable_parent_exits_9_not_missing(self):
        p = self._run_rfile_script(os.path.join(self.locked, "inner", "f.txt"))
        self.assertEqual(p.returncode, 9)
        self.assertNotIn("MISSING", p.stdout)

    def test_remote_script_true_absence_still_missing(self):
        p = self._run_rfile_script(os.path.join(self.tmp, "no", "such", "file.bin"))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "MISSING")


class TestGitDriver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.name", "t"], check=True)
        with open(os.path.join(self.repo, "a"), "w") as fh:
            fh.write("a")
        subprocess.run(["git", "-C", self.repo, "add", "a"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "one"], check=True)
        self.sha = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"],
                                    capture_output=True, text=True).stdout.strip()

    def test_existing_commit_on_default_branch(self):
        fp, status = r._fp_git(f"repo@{self.sha[:7]}", {"repo": self.repo})
        self.assertEqual(status, "ok")
        self.assertEqual(fp, f"git:{self.sha}:default")

    def test_missing_commit(self):
        fp, status = r._fp_git("repo@deadbeef1234", {"repo": self.repo})
        self.assertEqual((fp, status), (None, "missing-at-write"))

    def test_unknown_alias_is_driver_error_not_absence(self):
        with self.assertRaises(r.RefDriverError):
            r._fp_git("nope@abc1234", {"repo": self.repo})

    def test_subprocess_error_after_existence_proven_is_driver_error(self):
        real_run = subprocess.run

        def selective_run(argv, **kw):
            if "cat-file" in argv and "--batch-check" in argv:
                return real_run(argv, **kw)
            if "rev-parse" in argv:
                raise subprocess.TimeoutExpired(argv, r._PROBE_TIMEOUT)
            return real_run(argv, **kw)

        with mock.patch("quintessence.refs.subprocess.run", side_effect=selective_run):
            with self.assertRaises(r.RefDriverError):
                r._fp_git(f"repo@{self.sha[:7]}", {"repo": self.repo})

    def test_catfile_existence_check_timeout_is_driver_error(self):
        def selective_run(argv, **kw):
            if "cat-file" in argv and "--batch-check" in argv:
                raise subprocess.TimeoutExpired(argv, r._PROBE_TIMEOUT)
            return subprocess.run(argv, **kw)

        with mock.patch("quintessence.refs.subprocess.run", side_effect=selective_run):
            with self.assertRaises(r.RefDriverError):
                r._fp_git(f"repo@{self.sha[:7]}", {"repo": self.repo})

    def test_catfile_fatal_rc_is_driver_error_not_absence(self):
        def selective_run(argv, **kw):
            if "cat-file" in argv and "--batch-check" in argv:
                return subprocess.CompletedProcess(
                    argv, 128, stdout="",
                    stderr="fatal: not a git repository")
            return subprocess.run(argv, **kw)

        with mock.patch("quintessence.refs.subprocess.run", side_effect=selective_run):
            with self.assertRaises(r.RefDriverError):
                r._fp_git(f"repo@{self.sha[:7]}", {"repo": self.repo})

    def test_revparse_nonzero_after_existence_is_driver_error(self):
        real_run = subprocess.run

        def selective_run(argv, **kw):
            if "rev-parse" in argv:
                return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal")
            return real_run(argv, **kw)

        with mock.patch("quintessence.refs.subprocess.run", side_effect=selective_run):
            with self.assertRaises(r.RefDriverError):
                r._fp_git(f"repo@{self.sha[:7]}", {"repo": self.repo})


@unittest.skipUnless(shutil.which("systemctl"), "systemctl not on this host")
class TestUnitDriver(unittest.TestCase):
    def test_nonexistent_unit_is_missing(self):
        fp, status = r._fp_unit("qq-b1-definitely-not-a-unit.service")
        self.assertEqual((fp, status), (None, "missing-at-write"))


def fake_systemctl(cat_scopes=("system",), enabled_state="enabled", unit_text="[Unit]\nX=1\n",
                    enabled_raises=False, calls=None):
    """Patch subprocess.run with a systemctl double: `cat` succeeds in `cat_scopes`,
    `is-enabled` reports `enabled_state` (via stdout, nonzero rc for anything but enabled —
    the real tool's contract) or raises."""
    def runner(argv, **kw):
        if calls is not None:
            calls.append(argv)
        scope = "user" if "--user" in argv else "system"
        if "cat" in argv:
            if scope in cat_scopes:
                return subprocess.CompletedProcess(argv, 0, stdout=unit_text, stderr="")
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="No files found")
        if enabled_raises:
            raise OSError("is-enabled exploded")
        rc = 0 if enabled_state == "enabled" else 1
        return subprocess.CompletedProcess(argv, rc, stdout=enabled_state + "\n", stderr="")
    return mock.patch("quintessence.refs.subprocess.run", side_effect=runner)


class TestUnitEnablementFold(unittest.TestCase):
    """is-enabled folded into the unit fingerprint (versioned `sha256e:` format so
    older `sha256:` records are a format mismatch = no comparison basis, never a mass flip);
    last-run state stays excluded (the sweep's axis)."""

    UNIT = "x.service"

    def _fp(self, **kw):
        with fake_systemctl(**kw):
            return r._fp_unit(self.UNIT)

    def test_fp_is_versioned_and_deterministic(self):
        import hashlib
        fp, status = self._fp()
        self.assertEqual(status, "ok")
        want = hashlib.sha256("[Unit]\nX=1\n\n[enabled-state] enabled\n".encode()).hexdigest()
        self.assertEqual(fp, "sha256e:" + want)

    def test_enablement_flip_changes_fp_same_unit_text(self):
        fp_en, _ = self._fp(enabled_state="enabled")
        fp_dis, _ = self._fp(enabled_state="disabled")
        self.assertNotEqual(fp_en, fp_dis)
        self.assertTrue(fp_dis.startswith("sha256e:"))

    def test_disabled_state_reads_stdout_despite_nonzero_rc(self):
        import hashlib
        fp, status = self._fp(enabled_state="disabled")
        self.assertEqual(status, "ok")
        want = hashlib.sha256("[Unit]\nX=1\n\n[enabled-state] disabled\n".encode()).hexdigest()
        self.assertEqual(fp, "sha256e:" + want)

    def test_enabled_probe_failure_degrades_to_unknown_not_error(self):
        import hashlib
        fp, status = self._fp(enabled_raises=True)
        self.assertEqual(status, "ok")   # the unit file WAS found; secondary probe is soft
        want = hashlib.sha256("[Unit]\nX=1\n\n[enabled-state] unknown\n".encode()).hexdigest()
        self.assertEqual(fp, "sha256e:" + want)

    def test_is_enabled_runs_in_the_scope_cat_resolved(self):
        calls: list = []
        with fake_systemctl(cat_scopes=("user",), calls=calls):
            fp, status = r._fp_unit(self.UNIT)
        self.assertEqual(status, "ok")
        enabled_calls = [c for c in calls if "is-enabled" in c]
        self.assertEqual(len(enabled_calls), 1)
        self.assertIn("--user", enabled_calls[0])

    def test_missing_unit_never_probes_enablement(self):
        calls: list = []
        with fake_systemctl(cat_scopes=(), calls=calls):
            fp, status = r._fp_unit(self.UNIT)
        self.assertEqual((fp, status), (None, "missing-at-write"))
        self.assertEqual([c for c in calls if "is-enabled" in c], [])

    def test_bases_returns_both_encodings_of_one_probe(self):
        import hashlib
        with fake_systemctl():
            fp_new, fp_legacy, status = r._fp_unit_bases(self.UNIT)
        self.assertEqual(status, "ok")
        self.assertEqual(fp_legacy,
                          "sha256:" + hashlib.sha256("[Unit]\nX=1\n".encode()).hexdigest())
        self.assertTrue(fp_new.startswith("sha256e:"))

    def test_one_scope_errored_and_other_not_found_is_driver_error(self):
        def runner(argv, **kw):
            scope = "user" if "--user" in argv else "system"
            if "cat" in argv:
                if scope == "system":
                    raise subprocess.TimeoutExpired(argv, r._PROBE_TIMEOUT)
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="No files found")
            return subprocess.CompletedProcess(argv, 0, stdout="enabled\n", stderr="")

        with mock.patch("quintessence.refs.subprocess.run", side_effect=runner):
            with self.assertRaises(r.RefDriverError):
                r._fp_unit("x.service")

    def test_systemctl_cat_nonzero_without_absence_diagnostic_is_driver_error(self):
        def runner(argv, **kw):
            if "cat" in argv:
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="Failed to connect to bus")
            return subprocess.CompletedProcess(argv, 0, stdout="enabled\n", stderr="")

        with mock.patch("quintessence.refs.subprocess.run", side_effect=runner):
            with self.assertRaises(r.RefDriverError):
                r._fp_unit("x.service")

    def test_old_format_records_are_a_prefix_mismatch_not_a_change(self):
        # the older record shape: sha256:<hash of cat stdout alone>. The new driver can never
        # emit that prefix — a comparer sees the format mismatch and must re-fingerprint, not
        # flip (contract pinned here so the sweep can rely on it).
        import hashlib
        old = "sha256:" + hashlib.sha256("[Unit]\nX=1\n".encode()).hexdigest()
        new, _ = self._fp()
        self.assertTrue(old.startswith("sha256:") and not old.startswith("sha256e:"))
        self.assertTrue(new.startswith("sha256e:"))


class TestProbeDriver(unittest.TestCase):
    def test_stdout_hash_normalized(self):
        fp1, s1 = r._fp_probe("printf 'a  \\nb\\n'")
        fp2, s2 = r._fp_probe("printf 'a\\nb'")
        self.assertEqual((s1, s2), ("ok", "ok"))
        self.assertEqual(fp1, fp2)   # trailing whitespace/newline normalized away

    def test_failing_probe_is_missing(self):
        self.assertEqual(r._fp_probe("false"), (None, "missing-at-write"))

    def test_subprocess_error_is_driver_error(self):
        with mock.patch("quintessence.refs.subprocess.run",
                         side_effect=OSError("no bash")):
            with self.assertRaises(r.RefDriverError):
                r._fp_probe("echo hello")

    def test_timeout_is_driver_error(self):
        with mock.patch("quintessence.refs.subprocess.run",
                         side_effect=subprocess.TimeoutExpired("bash", r._PROBE_TIMEOUT)):
            with self.assertRaises(r.RefDriverError):
                r._fp_probe("sleep 999")


class TestBindWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_ok_record_shape(self):
        store = make_store(self.tmp)
        target = os.path.join(self.tmp, "artifact.txt")
        with open(target, "w") as fh:
            fh.write("real\n")
        with captured_stderr() as buf:
            r.bind_write(store, "topic-x", f"claim about {target} verified")
        self.assertEqual(buf.getvalue(), "")
        recs = read_records(store)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["head"], "topic-x")
        self.assertEqual(rec["kind"], "file")
        self.assertEqual(rec["id"], target)
        self.assertEqual(rec["status"], "ok")
        self.assertTrue(rec["fp"].startswith("sha256:"))
        self.assertEqual(rec["line_ts"], rec["asof"])
        self.assertEqual(set(rec), {"head", "line_ts", "kind", "id", "fp", "asof", "status"})

    def test_missing_referent_warns_loud_and_records(self):
        store = make_store(self.tmp)
        gone = os.path.join(self.tmp, "not-there.md")
        with captured_stderr() as buf:
            r.bind_write(store, "t", f"born stale: {gone} deployed")
        self.assertIn(f"qq: referent does not exist: {gone} – claim may be born stale",
                       buf.getvalue())
        recs = read_records(store)
        self.assertEqual(recs[0]["status"], "missing-at-write")
        self.assertIsNone(recs[0]["fp"])

    def test_qq_bind_zero_is_inert(self):
        store = make_store(self.tmp, QQ_BIND="0")
        gone = os.path.join(self.tmp, "not-there.md")
        with captured_stderr() as buf:
            r.bind_write(store, "t", f"{gone} deployed", explicit=["bogus:x"])
        self.assertEqual(buf.getvalue(), "")
        self.assertFalse((store.state_dir / "refs").exists())

    def test_no_referents_touches_nothing(self):
        store = make_store(self.tmp)
        with captured_stderr() as buf:
            r.bind_write(store, "t", "a plain prose line with no referents at all")
        self.assertEqual(buf.getvalue(), "")
        self.assertFalse((store.state_dir / "refs").exists())

    def test_explicit_ref_binds_and_bad_ref_warns_soft(self):
        store = make_store(self.tmp)
        target = os.path.join(self.tmp, "f")
        with open(target, "w") as fh:
            fh.write("x")
        with captured_stderr() as buf:
            r.bind_write(store, "t", "no referents in prose",
                          explicit=[f"file:{target}", "proc:foo.service"])
        self.assertIn("--ref 'proc:foo.service' ignored", buf.getvalue())
        recs = read_records(store)
        self.assertEqual([(x["kind"], x["id"]) for x in recs], [("file", target)])

    def test_driver_error_records_fp_error_quietly(self):
        store = make_store(self.tmp)   # empty QQ_BIND_REPOS -> unknown alias = driver error
        with captured_stderr() as buf:
            r.bind_write(store, "t", "x", explicit=["git:ghost@abc1234"])
        err = buf.getvalue()
        self.assertIn("ref fingerprint error", err)
        self.assertNotIn("born stale", err)
        recs = read_records(store)
        self.assertEqual(recs[0]["status"], "fp-error")

    def test_line_ts_threaded_asof_stays_bind_time(self):
        # B1 amendment (i): a caller-supplied line_ts lands verbatim; asof is still bind time.
        store = make_store(self.tmp)
        target = os.path.join(self.tmp, "f2")
        with open(target, "w") as fh:
            fh.write("x")
        with captured_stderr():
            r.bind_write(store, "t", f"see {target}", line_ts="2026-01-02T03:04:05Z")
        rec = read_records(store)[0]
        self.assertEqual(rec["line_ts"], "2026-01-02T03:04:05Z")
        self.assertNotEqual(rec["asof"], rec["line_ts"])
        self.assertRegex(rec["asof"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_max_refs_overflow_notes_quietly(self):
        # B1 amendment (ii): the cap still bites (MAX_REFS records, first-seen order) but is no
        # longer silent — one quiet stderr line names the bound/total and dropped counts.
        store = make_store(self.tmp)
        many = " ".join(f"/etc/cap-test-{i}.conf" for i in range(r.MAX_REFS + 5))
        with captured_stderr() as buf:
            r.bind_write(store, "t", many)
        err = buf.getvalue()
        self.assertIn(f"ref cap – bound {r.MAX_REFS} of {r.MAX_REFS + 5}", err)
        self.assertIn("dropped 5", err)
        self.assertEqual(len(read_records(store)), r.MAX_REFS)

    def test_explicit_refs_survive_cap(self):
        store = make_store(self.tmp)
        cap = 4
        exp_a = os.path.join(self.tmp, "explicit-a.txt")
        exp_b = os.path.join(self.tmp, "explicit-b.txt")
        for p in [exp_a, exp_b]:
            with open(p, "w") as fh:
                fh.write("x\n")
        auto_files = []
        for i in range(cap + 1):
            p = os.path.join(self.tmp, f"auto-{i}.txt")
            with open(p, "w") as fh:
                fh.write("x\n")
            auto_files.append(p)
        text = " ".join(auto_files)
        with mock.patch.object(r, "MAX_REFS", cap):
            with captured_stderr() as buf:
                r.bind_write(store, "t", text,
                             explicit=[f"file:{exp_a}", f"file:{exp_b}"])
        recs = read_records(store)
        self.assertEqual(len(recs), cap)
        bound_ids = [rec["id"] for rec in recs]
        self.assertIn(exp_a, bound_ids)
        self.assertIn(exp_b, bound_ids)
        err = buf.getvalue()
        self.assertIn("dropped", err)
        self.assertIn("(0 explicit)", err)

    def test_explicit_count_exceeds_cap(self):
        store = make_store(self.tmp)
        cap = 2
        exp_files = []
        for i in range(4):
            p = os.path.join(self.tmp, f"exp-over-{i}.txt")
            with open(p, "w") as fh:
                fh.write("x\n")
            exp_files.append(p)
        with mock.patch.object(r, "MAX_REFS", cap):
            with captured_stderr() as buf:
                r.bind_write(store, "t", "no refs here",
                             explicit=[f"file:{p}" for p in exp_files])
        recs = read_records(store)
        self.assertEqual(len(recs), cap)
        bound_ids = [rec["id"] for rec in recs]
        self.assertEqual(bound_ids, exp_files[:cap])
        err = buf.getvalue()
        self.assertIn("dropped 2 (2 explicit)", err)

    def test_no_overflow_no_note(self):
        store = make_store(self.tmp)
        with captured_stderr() as buf:
            r.bind_write(store, "t", "/etc/hostname only")
        self.assertNotIn("ref cap", buf.getvalue())

    def test_never_raises_even_when_persist_explodes(self):
        store = make_store(self.tmp)
        with mock.patch("quintessence.refs.state_lock", side_effect=RuntimeError("boom")):
            with captured_stderr():
                r.bind_write(store, "t", "/etc/hostname is fine")   # must not raise
        self.assertEqual(read_records(store), [])


class TestWriteVerbIntegration(unittest.TestCase):
    """The seam in quintessence.write: binding rides the verbs, never alters their output or
    outcome, and rewrite binds only its NOVEL lines."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.store = make_store(self.tmp)
        with captured_stderr():
            w.new(self.store, "T", "seed")

    def test_update_with_missing_referent_still_succeeds(self):
        gone = os.path.join(self.tmp, "vanished.md")
        with captured_stderr() as buf:
            out = w.update(self.store, "T", f"shipped {gone} tonight\n")
        self.assertIn("T.md committed + mirrored", out)
        self.assertIn("born stale", buf.getvalue())
        recs = read_records(self.store)
        self.assertEqual([(x["head"], x["status"]) for x in recs],
                          [("T", "missing-at-write")])

    def test_update_output_identical_with_and_without_binding(self):
        store0 = make_store(os.path.join(self.tmp, "b0"), QQ_BIND="0")
        with captured_stderr():
            w.new(store0, "T", "seed")
        line = "no referents named here\n"
        with captured_stderr() as b1:
            out1 = w.update(self.store, "T", line)
        with captured_stderr() as b0:
            out0 = w.update(store0, "T", line)
        # both stderr empty; stdout differs only in the content-hash-free parts (same shape)
        self.assertEqual(b1.getvalue(), "")
        self.assertEqual(b0.getvalue(), "")
        self.assertEqual(out1.splitlines()[-1], out0.splitlines()[-1])
        self.assertEqual(read_records(self.store), [])
        self.assertFalse((store0.state_dir / "refs").exists())

    def test_rewrite_binds_only_novel_lines(self):
        old_ref = os.path.join(self.tmp, "old-artifact")
        with open(old_ref, "w") as fh:
            fh.write("x")
        with captured_stderr():
            w.update(self.store, "T", f"historical claim about {old_ref}\n")
        os.unlink(old_ref)   # the historical referent dies — must NOT re-warn on rewrite
        full = self.store.read_head("T")
        new_ref = os.path.join(self.tmp, "new-artifact")
        with open(new_ref, "w") as fh:
            fh.write("y")
        with captured_stderr() as buf:
            w.rewrite(self.store, "T", full + f"\n> updated: 2026-01-01T00:00:00Z see {new_ref}\n")
        self.assertNotIn("born stale", buf.getvalue())
        novel = [x for x in read_records(self.store) if x["id"] == new_ref]
        dead_rebinds = [x for x in read_records(self.store)
                         if x["id"] == old_ref and x["status"] == "missing-at-write"]
        self.assertEqual(len(novel), 1)
        self.assertEqual(dead_rebinds, [])

    def test_essence_binds_its_text(self):
        target = os.path.join(self.tmp, "essence-artifact")
        with open(target, "w") as fh:
            fh.write("z")
        with captured_stderr():
            w.essence(self.store, "T", f"tracks {target} now")
        recs = [x for x in read_records(self.store) if x["id"] == target]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["status"], "ok")

    def test_update_threads_true_line_ts(self):
        # amendment (i) end-to-end: the REF's line_ts equals the stamp on the HEAD's newest
        # rendered update-line (B2's join key). Holds for bare prose AND when the caller tries to
        # supply a timestamp — `qq update` now IGNORES any caller-supplied stamp and re-stamps with
        # now() (fabrication guard, 2026-07-09), so line_ts tracks qq's stamp, never the caller's.
        from quintessence.heads import parse
        target = os.path.join(self.tmp, "lt-artifact")
        with open(target, "w") as fh:
            fh.write("x")
        with captured_stderr():
            w.update(self.store, "T", f"verified {target} now\n")
        rec = [x for x in read_records(self.store) if x["id"] == target][-1]
        newest = parse(self.store.read_head("T")).updates[0]
        self.assertEqual(rec["line_ts"], newest.timestamp)
        # caller-supplied timestamp is stripped + replaced with qq's now(): the join-key invariant
        # still holds (line_ts == rendered stamp), but the caller's value never lands.
        with captured_stderr():
            w.update(self.store, "T", f"2026-01-02T03:04:05Z re-verified {target}\n")
        rec2 = [x for x in read_records(self.store) if x["id"] == target][-1]
        newest2 = parse(self.store.read_head("T")).updates[0]
        self.assertEqual(rec2["line_ts"], newest2.timestamp)
        self.assertNotEqual(rec2["line_ts"], "2026-01-02T03:04:05Z")

    def test_rewrite_novel_update_line_keeps_its_stamp(self):
        # The novel update-line goes into the HEADER region (right after the title), where a
        # real rewrite puts one. Until the 2026-08-09 reader unification this fixture appended
        # it at EOF — BODY region — and the whole-file bucketing still read it as an
        # update-line there; the one reader treats a body '> updated:' as prose, so an
        # EOF-appended line now (correctly) binds under the None bucket at bind time instead.
        new_ref = os.path.join(self.tmp, "rw-artifact")
        with open(new_ref, "w") as fh:
            fh.write("y")
        lines = self.store.read_head("T").split("\n")
        lines.insert(1, f"> updated: 2026-02-03T04:05:06Z see {new_ref}")
        with captured_stderr():
            w.rewrite(self.store, "T", "\n".join(lines))
        rec = [x for x in read_records(self.store) if x["id"] == new_ref][-1]
        self.assertEqual(rec["line_ts"], "2026-02-03T04:05:06Z")

    def test_refused_write_never_binds(self):
        gone = os.path.join(self.tmp, "gone.md")
        with captured_stderr() as buf:
            with self.assertRaises(w.WriteError):
                w.update(self.store, "NO-SUCH", f"claim {gone}\n", refs=None)
        # update on a missing HEAD refuses inside _execute_write -> bind never runs
        self.assertNotIn("born stale", buf.getvalue())


class TestExcludeRoots(unittest.TestCase):
    """Rooted paths under QQ_BIND_EXCLUDE_ROOTS never auto-extract — process-runtime
    state is near-always a prose EXAMPLE, and hot dirs there defeat suspect-once (perpetual
    annotation). Explicit --ref stays exempt (deliberate act)."""

    def test_default_excludes_runtime_roots(self):
        # /run/systemd + /tmp exist as roots, so before the exclusion these bound (live noise)
        self.assertEqual(r.extract_referents(
            "compared against /run/systemd and /tmp/scratch.txt for size", {}), [])

    def test_empty_tuple_disables_exclusion(self):
        got = r.extract_referents("compared against /run/systemd today", {},
                                   exclude_roots=())
        self.assertEqual(got, [("file", "/run/systemd")])

    def test_custom_roots_replace_not_extend_the_default(self):
        got = r.extract_referents("both /etc/fstab and /run/systemd named", {},
                                   exclude_roots=("/etc",))
        self.assertEqual(got, [("file", "/run/systemd")])

    def test_root_itself_and_children_excluded_but_not_prefix_lookalikes(self):
        self.assertTrue(r.path_excluded("/tmp", ("/tmp",)))
        self.assertTrue(r.path_excluded("/tmp/x/y", ("/tmp",)))
        self.assertFalse(r.path_excluded("/tmpfs-notes", ("/tmp",)))

    def test_tilde_ids_compare_by_expanded_location(self):
        home = os.path.expanduser("~")
        self.assertTrue(r.path_excluded("~/x", (home,)))
        # tools-6: the negative half of this assertion is only meaningful when $HOME does NOT
        # itself fall under /tmp — a scratch $HOME created the usual mktemp/TemporaryDirectory
        # way (as every hermetic test run's $HOME does) almost always lands somewhere UNDER
        # /tmp (e.g. "/tmp/xyzzy123"), not literally "/tmp" itself; the old `home != "/tmp"`
        # guard let exactly that case slip through and assert something FALSE (a ~-expanded
        # path under a /tmp-nested $HOME genuinely IS excluded by ("/tmp",)), failing the test
        # on every hermetic run. Guard on the whole-path-component prefix instead.
        if home != "/tmp" and not home.startswith("/tmp/"):
            self.assertFalse(r.path_excluded("~/x", ("/tmp",)))

    def test_container_dir_itself_never_binds_children_do(self):
        # HOME / repo worktrees / corpus roots are guaranteed-churn containers
        home = os.path.expanduser("~")
        self.assertEqual(r.extract_referents(f"everything under {home} tonight", {},
                                              exclude_roots=()), [])
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        repo = os.path.join(tmp, "work")
        os.makedirs(repo)
        child = os.path.join(repo, "deploy.sh")
        with open(child, "w") as fh:
            fh.write("x\n")
        rm = {"work": repo}
        self.assertEqual(r.extract_referents(f"repo lives at {repo}", rm,
                                              exclude_roots=()), [])
        got = r.extract_referents(f"script at {child}", rm, exclude_roots=())
        self.assertEqual(got, [("file", child)])

    def test_container_ids_derives_home_repos_and_corpora(self):
        ids = r.container_ids({"a": "/tmp/ra"}, {"mem": "/tmp/ca"})
        self.assertIn(os.path.realpath(os.path.expanduser("~")), ids)
        self.assertIn(os.path.realpath("/tmp/ra"), ids)
        self.assertIn(os.path.realpath("/tmp/ca"), ids)

    def test_parse_corpus_excludes(self):
        cfg = Config(env={}, config_file="/nonexistent",
                      overrides={"QQ_BIND_CORPUS_EXCLUDE": "docs:backlog.md, docs:draft-*, bad"})
        self.assertEqual(r.parse_corpus_excludes(cfg),
                          [("docs", "backlog.md"), ("docs", "draft-*")])

    def test_fragment_paths_never_bind(self):
        # B5 second wave, from the live corpus seed: ellipsis, template specifiers and
        # truncated stems are prose artifacts — each manufactured a false born-stale.
        for text in ("lib at /usr/lib/.../libykcs11.so today",
                      "unit writes /home/%i/torrents per instance",
                      "build lives in /data/llama.cpp- variants",
                      "files named ~/docs/session_state_ prefix"):
            self.assertEqual(r.extract_referents(text, {}, exclude_roots=()), [],
                              msg=text)

    def test_parse_drops_relative_and_strips_trailing_slash(self):
        cfg = Config(env={}, config_file="/nonexistent",
                      overrides={"QQ_BIND_EXCLUDE_ROOTS": "/run/, relative, /dev"})
        self.assertEqual(r.parse_exclude_roots(cfg), ("/run", "/dev"))

    def test_bind_write_honors_registry_default(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        # None = unset override -> the registry default (make_store's "" would neutralize it)
        store = make_store(tmp, QQ_BIND_EXCLUDE_ROOTS=None)
        with captured_stderr():
            r.bind_write(store, "t", "noise example /run/systemd churned again")
        self.assertEqual([x for x in read_records(store) if x["id"] == "/run/systemd"], [])

    def test_bind_write_explicit_ref_is_exempt(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        store = make_store(tmp, QQ_BIND_EXCLUDE_ROOTS=None)
        with captured_stderr():
            r.bind_write(store, "t", "prose that names nothing",
                          explicit=["file:/run/systemd"])
        recs = [x for x in read_records(store) if x["id"] == "/run/systemd"]
        self.assertEqual(len(recs), 1)


class TestUnreadableReferentIsDriverError(unittest.TestCase):
    """A referent that EXISTS (lstat succeeds) but whose later read is DENIED must raise
    RefDriverError, not a bare PermissionError — matching the discipline the initial
    existence check already applies."""

    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("permission checks are void as root")
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_unreadable_directory_raises_driver_error(self):
        d = os.path.join(self.tmp, "sealed")
        os.makedirs(d)
        with open(os.path.join(d, "child"), "w") as fh:
            fh.write("x")
        os.chmod(d, 0o111)
        self.addCleanup(os.chmod, d, 0o755)
        with self.assertRaises(r.RefDriverError):
            r._fp_file(d)

    def test_unreadable_file_raises_driver_error(self):
        f = os.path.join(self.tmp, "sealed.txt")
        with open(f, "w") as fh:
            fh.write("content")
        os.chmod(f, 0o000)
        self.addCleanup(os.chmod, f, 0o644)
        with self.assertRaises(r.RefDriverError):
            r._fp_file(f)


class TestRelPathExcludeAndContainerChecks(unittest.TestCase):
    """Repo-relative paths that resolve to absolute ids must go through the same
    excluded-path and container-realpath checks that rooted paths do."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo, _ = make_repo(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "sub"))
        with open(os.path.join(self.repo, "sub", "mod.py"), "w") as fh:
            fh.write("x\n")
        self.rm = {"work": self.repo}

    def test_relpath_under_excluded_root_not_extracted(self):
        got = r.extract_referents("patched sub/mod.py today", self.rm,
                                   exclude_roots=(self.tmp,))
        self.assertEqual(got, [])

    def test_relpath_resolving_to_container_dir_not_extracted(self):
        abspath = os.path.join(self.repo, "sub", "mod.py")
        containers = {os.path.realpath(abspath)}
        got = r.extract_referents("patched sub/mod.py today", self.rm,
                                   exclude_roots=(), containers=containers)
        self.assertEqual(got, [])


class TestBindCorpusFiles(unittest.TestCase):
    """bind_corpus_files: locked read-modify-write, atomic replace, replace-on-rebind
    dropping superseded records, and preservation of unrelated lines."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.store = make_store(self.tmp)
        self.corpus_root = os.path.join(self.tmp, "corpus")
        os.makedirs(self.corpus_root)

    def _write_md(self, name, content):
        path = os.path.join(self.corpus_root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)

    def test_locked_read_modify_write(self):
        target = os.path.join(self.tmp, "artifact.txt")
        with open(target, "w") as fh:
            fh.write("real\n")
        self._write_md("doc.md", f"see {target} for details")
        lock_acquired = []
        original_lock = r.state_lock
        @contextlib.contextmanager
        def tracking_lock(store):
            lock_acquired.append(True)
            with original_lock(store):
                yield
        with mock.patch("quintessence.refs.state_lock", tracking_lock):
            count = r.bind_corpus_files(self.store, "test", self.corpus_root,
                                         ["doc.md"], "hook")
        self.assertTrue(lock_acquired)
        self.assertGreater(count, 0)

    def test_atomic_replace_via_temp_file(self):
        target = os.path.join(self.tmp, "artifact.txt")
        with open(target, "w") as fh:
            fh.write("real\n")
        self._write_md("doc.md", f"see {target} for details")
        replace_calls = []
        original_replace = os.replace
        def tracking_replace(src, dst):
            replace_calls.append((src, dst))
            return original_replace(src, dst)
        with mock.patch("quintessence.refs.os.replace", tracking_replace):
            r.bind_corpus_files(self.store, "test", self.corpus_root,
                                 ["doc.md"], "hook")
        self.assertEqual(len(replace_calls), 1)
        src, dst = replace_calls[0]
        # The temp carries a unique suffix (atomicio names temps <target>.tmp.<random>, so two concurrent writers to
        # one destination cannot truncate each other's in-flight file), so assert the PROPERTY --
        # a sibling temp for this target -- rather than the exact old "<target>.tmp" spelling.
        self.assertIn(".tmp", os.path.basename(str(src)))
        self.assertTrue(os.path.basename(str(src)).startswith("refs.jsonl"))
        self.assertEqual(os.path.dirname(str(src)), os.path.dirname(str(dst)))
        self.assertTrue(str(dst).endswith("refs.jsonl"))

    def test_replace_on_rebind_drops_old_records(self):
        target = os.path.join(self.tmp, "artifact.txt")
        with open(target, "w") as fh:
            fh.write("v1\n")
        self._write_md("doc.md", f"see {target} for details")
        r.bind_corpus_files(self.store, "test", self.corpus_root,
                             ["doc.md"], "src1")
        recs_v1 = [x for x in read_records(self.store) if x.get("corpus") == "test"]
        self.assertGreater(len(recs_v1), 0)
        with open(target, "w") as fh:
            fh.write("v2\n")
        r.bind_corpus_files(self.store, "test", self.corpus_root,
                             ["doc.md"], "src2")
        recs_v2 = [x for x in read_records(self.store) if x.get("corpus") == "test"]
        self.assertTrue(all(x["src"] == "src2" for x in recs_v2))

    def test_unrelated_lines_preserved(self):
        refs_dir = self.store.state_dir / "refs"
        refs_dir.mkdir(parents=True, exist_ok=True)
        head_record = json.dumps({"head": "myhead", "line_ts": "x", "kind": "file",
                                   "id": "/etc/hostname", "fp": "sha256:abc",
                                   "asof": "x", "status": "ok"})
        other_corpus = json.dumps({"corpus": "other", "file": "doc.md", "kind": "file",
                                    "id": "/etc/fstab", "fp": "sha256:def",
                                    "asof": "x", "status": "ok", "src": "hook"})
        foreign_line = "not-valid-json"
        with open(refs_dir / "refs.jsonl", "w") as fh:
            fh.write(head_record + "\n")
            fh.write(other_corpus + "\n")
            fh.write(foreign_line + "\n")
        target = os.path.join(self.tmp, "artifact.txt")
        with open(target, "w") as fh:
            fh.write("content\n")
        self._write_md("doc.md", f"see {target}")
        r.bind_corpus_files(self.store, "test", self.corpus_root,
                             ["doc.md"], "hook")
        raw = (refs_dir / "refs.jsonl").read_text()
        lines = [l for l in raw.splitlines() if l]
        head_lines = [l for l in lines if "myhead" in l]
        other_lines = [l for l in lines if '"other"' in l]
        foreign_lines = [l for l in lines if "not-valid-json" in l]
        self.assertEqual(len(head_lines), 1)
        self.assertEqual(len(other_lines), 1)
        self.assertEqual(len(foreign_lines), 1)


if __name__ == "__main__":
    unittest.main()
