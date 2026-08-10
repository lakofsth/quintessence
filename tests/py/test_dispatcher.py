# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for the `qq` entry-point dispatcher itself (the strangler front door, now
fully strangled): native-verb routing (quintessence/cli.py,
quintessence.write, quintessence.admin), search/ask direct-exec mechanics (argv / stdin /
exit code preserved byte-for-byte via os.execv), and — since P9 — the rule that NOTHING
delegates to qq-legacy anymore, unknown commands included (rendered natively; see the `qq`
file's own NATIVE_VERBS comment).

These tests run the REAL `qq` dispatcher file as a subprocess against FAKE `qq-legacy` /
`qq-search` / `qq-ask` stubs (tiny scripts that report what they were called with) placed
next to it in an isolated temp directory — the search/ask stubs prove the direct-exec
mechanics, and the qq-legacy stub survives P9 as a TRIPWIRE: any test that sees
"LEGACY-CALLED" has caught a delegation that should no longer exist. `quintessence` itself is
found via PYTHONPATH pointing at the real repo, since the temp copy of `qq` has no package
alongside it.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
QQ_SCRIPT = os.path.join(REPO_ROOT, "qq")

FAKE_LEGACY = """#!/usr/bin/env python3
import sys
sys.stdout.write("LEGACY-CALLED argv=" + repr(sys.argv[1:]) + "\\n")
data = sys.stdin.read()
if data:
    sys.stdout.write("LEGACY-STDIN=" + repr(data) + "\\n")
if sys.argv[1:2] == ["exit-code"]:
    sys.exit(int(sys.argv[2]))
sys.exit(7)
"""

# search/ask: direct-exec'd straight to qq-search/qq-ask, NEVER via qq-legacy (see
# the `qq` file's `_DIRECT_EXEC` comment for the grounded env-leak bug this avoids) — a
# DIFFERENT fake stub, so a test can tell "went to qq-legacy" and "went to qq-search/qq-ask"
# apart, and so `qq`'s own argv[0] stripping (cmd shifted off before the exec) is pinned too.
FAKE_SEARCH_OR_ASK = """#!/usr/bin/env python3
import sys
sys.stdout.write("DIRECT-CALLED[{name}] argv=" + repr(sys.argv[1:]) + "\\n")
data = sys.stdin.read()
if data:
    sys.stdout.write("DIRECT-STDIN=" + repr(data) + "\\n")
if sys.argv[1:2] == ["exit-code"]:
    sys.exit(int(sys.argv[2]))
sys.exit(9)
"""


class DispatcherHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        shutil.copy2(QQ_SCRIPT, os.path.join(self.tmp, "qq"))
        os.chmod(os.path.join(self.tmp, "qq"), 0o755)
        legacy = os.path.join(self.tmp, "qq-legacy")
        with open(legacy, "w") as f:
            f.write(FAKE_LEGACY)
        os.chmod(legacy, stat.S_IRWXU)
        for name in ("qq-search", "qq-ask"):
            stub = os.path.join(self.tmp, name)
            with open(stub, "w") as f:
                f.write(FAKE_SEARCH_OR_ASK.format(name=name))
            os.chmod(stub, stat.S_IRWXU)

        # An isolated, empty-but-valid store for the native verbs that need Store/Config.
        self.store_dir = os.path.join(self.tmp, "store")
        os.makedirs(self.store_dir)
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = REPO_ROOT + os.pathsep + self.env.get("PYTHONPATH", "")
        self.env.update({
            "QUINTESSENCE_DIR": self.store_dir,
            "QQ_CONFIG": os.path.join(self.tmp, "config"),
            "QQ_STATE_DIR": os.path.join(self.tmp, "state"),
            "QQ_MEMDIR": os.path.join(self.tmp, "mem"),
            "QQ_KB_ROOT": os.path.join(self.tmp, "kb"),
            "QQ_RECONCILE_REGISTRY": "",   # generic install: reconcile is a no-op
        })

    def run_qq(self, args, stdin=None):
        # `input=None` does NOT redirect stdin -- the child inherits the runner's. The exec stubs
        # here call sys.stdin.read(), so whenever the suite runs with a stdin that never sends
        # EOF (an agent harness, `ssh` without -n, some CI runners) the child blocks forever and
        # the whole suite hangs with no further output. Observed 2026-08-03: a 6-minute stall in
        # test_ask_direct_execs_to_qq_ask_not_legacy with the stub's fd 0 an open socket. Pass an
        # empty string so stdin is a pipe that EOFs at once; an explicit `stdin=` still works.
        return subprocess.run([sys.executable, os.path.join(self.tmp, "qq"), *args],
                               input="" if stdin is None else stdin,
                               capture_output=True, text=True, env=self.env)


class TestNativeVerbsDoNotDelegate(DispatcherHarness):
    def test_menu_on_empty_store_is_native(self):
        p = self.run_qq(["menu"])
        self.assertEqual(p.returncode, 0)
        self.assertIn("# Quintessence — exit-note menu", p.stdout)
        self.assertNotIn("LEGACY-CALLED", p.stdout)

    def test_no_args_defaults_to_menu_natively(self):
        p = self.run_qq([])
        self.assertEqual(p.returncode, 0)
        self.assertIn("# Quintessence — exit-note menu", p.stdout)

    def test_digest_on_empty_store_is_native_and_exits_0(self):
        p = self.run_qq(["digest"])
        self.assertEqual(p.returncode, 0)   # the P2 ratified fix, not the legacy set-e bug
        self.assertIn("QUINTESSENCE OPEN LOOPS", p.stdout)

    def test_list_is_native(self):
        p = self.run_qq(["list"])
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")   # empty store -> no topics

    def test_findings_is_native(self):
        p = self.run_qq(["findings"])
        self.assertEqual(p.returncode, 0)

    def test_findings_next_is_native(self):
        p = self.run_qq(["findings", "next"])
        self.assertEqual(p.returncode, 0)
        self.assertIn("no pending findings", p.stdout)

    def test_findings_bad_subverb_errors_natively_without_delegating(self):
        p = self.run_qq(["findings", "bogus"])
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage: qq findings [next|list]", p.stderr)
        self.assertNotIn("LEGACY-CALLED", p.stdout)

    def test_show_missing_topic_is_native(self):
        p = self.run_qq(["show", "nope"])
        self.assertEqual(p.returncode, 0)
        self.assertIn("no HEAD for 'nope'", p.stdout)

    def test_show_no_args_usage_error_is_native(self):
        p = self.run_qq(["show"])
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage: qq show", p.stderr)

    def test_brief_is_native(self):
        p = self.run_qq(["brief", "nope"])
        self.assertEqual(p.returncode, 0)
        self.assertIn("no HEAD for 'nope'", p.stdout)

    def test_path_is_native(self):
        p = self.run_qq(["path", "sometopic"])
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), os.path.join(self.store_dir, "sometopic.md"))

    def test_menu_piped_into_a_closing_reader_dies_quietly_like_bash(self):
        """Caught live (piping `qq menu | head` through the ~/bin symlink): Python turns a
        SIGPIPE from an early-closing reader into a BrokenPipeError, which by default prints a
        traceback and exits 1 — qq-legacy (bash) just dies with exit 141 (128+SIGPIPE), no
        message. `qq` must match bash's quiet behavior, not leak a Python implementation
        detail to a piped caller (a hook capturing partial output, `qq digest | head -1`,
        etc.)."""
        # A big enough store that render_menu's output exceeds the pipe buffer, so `head -1`
        # closing its read end after the first line forces a SECOND write() from qq to hit a
        # truly-closed pipe (a single small write can complete before the reader even closes,
        # which is why this needs real bulk, not just a `.close()` race against one write()).
        for i in range(200):
            store_head = os.path.join(self.store_dir, f"h{i}.md")
            with open(store_head, "w") as f:
                f.write(f"# Quintessence — h{i}\n> updated: 2026-07-01T00:00:00Z (created)\n"
                        f"> essence: {'x' * 500}\n\n## RE-ENTER HERE\n")
        qq_path = os.path.join(self.tmp, "qq")
        cmd = f'"{sys.executable}" "{qq_path}" menu | head -1'
        proc = subprocess.run(["bash", "-c", f"{cmd}; echo EXIT=${{PIPESTATUS[0]}} >&2"],
                               capture_output=True, text=True, env=self.env, timeout=10)
        exit_code = int(proc.stderr.strip().rsplit("EXIT=", 1)[-1])
        self.assertEqual(exit_code, 141)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("BrokenPipeError", proc.stderr)

    def test_help_is_native_for_all_three_spellings(self):
        for flag in ("help", "-h", "--help"):
            p = self.run_qq([flag])
            self.assertEqual(p.returncode, 0, flag)
            self.assertIn("usage: qq [command] [args]", p.stdout)
            self.assertNotIn("LEGACY-CALLED", p.stdout)


class TestCheckFamilyIsNative(DispatcherHarness):
    """check/reconcile/waveoff are now served natively (quintessence.checks/.reconcile/
    .xref), not delegated. --fast is used throughout to avoid a real embedder dependency in this
    subprocess harness (matches tests/test-check-fast.sh's own bash-level convention)."""
    def test_check_fast_on_empty_store_is_native(self):
        p = self.run_qq(["check", "--fast"])
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        # an empty store has no INDEX.md yet -> the one T1 finding an empty store always has
        self.assertIn("INDEX.md stale vs HEADs", p.stdout)

    def test_check_write_fast_persists_to_state_dir_natively(self):
        """`check --write` now performs the ONE sanctioned auto-fix (reindex + commit
        INDEX.md under the transaction lock, quintessence.write.index_autofix_finding) instead
        of only ever flagging staleness — the store here isn't git-initialized, so the commit
        itself silently no-ops (matching qq-legacy's own `qq_commit_push ... || true` on a
        non-repo dir — a pre-existing wart, not introduced), but the write DOES happen and the
        finding recorded is "[T1 fix]", not the old flag-only "[T1 index]"."""
        p = self.run_qq(["check", "--write", "--fast"])
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        self.assertIn("Tier-1 + skipped T2-xref finding(s)", p.stdout)
        findings_path = os.path.join(self.tmp, "state", "pending-findings.md")
        with open(findings_path) as f:
            content = f.read()
        self.assertIn("<!-- TIER1:START -->", content)
        self.assertIn("INDEX.md was stale → reindexed + committed (auto)", content)
        self.assertTrue(os.path.isfile(os.path.join(self.store_dir, "INDEX.md")))

    def test_check_write_autofix_actually_commits_on_a_real_repo(self):
        """Same auto-fix, but on a REAL git repo (unlike the sibling test above) — proves the
        commit genuinely lands, not just that the finding text changed. Path-scoped commit
        message matches quintessence.write.index_autofix_finding's port of run_check's own."""
        subprocess.run(["git", "init", "-q", self.store_dir], check=True)
        subprocess.run(["git", "-C", self.store_dir, "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", self.store_dir, "config", "user.name", "t"], check=True)
        p = self.run_qq(["check", "--write", "--fast"])
        self.assertEqual(p.returncode, 0)
        log = subprocess.run(["git", "-C", self.store_dir, "log", "--format=%s"],
                              capture_output=True, text=True)
        self.assertIn("qq check: reindex stale INDEX", log.stdout)
        show = subprocess.run(["git", "-C", self.store_dir, "show", "--stat", "--format=",
                                "HEAD"], capture_output=True, text=True)
        self.assertIn("INDEX.md", show.stdout)

    def test_check_consistent_message_on_a_clean_store(self):
        # a HEAD + a fresh INDEX.md leaves nothing to flag
        head = os.path.join(self.store_dir, "alpha.md")
        with open(head, "w") as f:
            f.write("# Quintessence — alpha\n> updated: x\n> essence: e\n\n## RE-ENTER HERE\n")
        index_text = ("# Quintessence — exit-note menu\n"
                       "_Pick one to continue (`qq show <topic>`). Saving is automatic; "
                       "resuming is your choice._\n\n"
                       f"{'TOPIC':<26} | {'UPDATED':<20} | ESSENCE\n"
                       f"{'-' * 26}-+-{'-' * 20}-+-------\n"
                       f"{'alpha':<26} | {'x':<20} | e\n")
        with open(os.path.join(self.store_dir, "INDEX.md"), "w") as f:
            f.write(index_text)
        p = self.run_qq(["check", "--fast"])
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "qq check: no Tier-1 findings (xref skipped) – consistent.\n")

    def test_reconcile_no_registry_is_native(self):
        p = self.run_qq(["reconcile"])
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        self.assertIn("qq reconcile: no registry", p.stdout)

    def test_waveoff_missing_memory_is_native(self):
        p = self.run_qq(["waveoff", "nomem", "nohead"])
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        self.assertIn("no memory file 'nomem'", p.stderr)

    def test_waveoff_usage_error_is_native(self):
        p = self.run_qq(["waveoff", "onlyone"])
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage: qq waveoff", p.stderr)
        self.assertNotIn("LEGACY-CALLED", p.stdout)

    def test_waveoff_head_bulk_on_an_unindexable_store_degrades_cleanly(self):
        """A bare `--head` bulk waveoff needs the embedding index (unlike the 2-arg single-pair
        path); on an empty store that index can never build. staleness-xref.py's own standalone
        CLI has the identical gap (a raw Python traceback) — discovered, not introduced, by this
        P4 dispatcher wiring; degraded to a clean stderr message here instead (see `qq`'s
        _cmd_waveoff)."""
        p = self.run_qq(["waveoff", "--head", "sometopic", "--all"])
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        self.assertIn("qq waveoff --head:", p.stderr)


class TestDelegation(DispatcherHarness):
    """P9 (the release-gate legacy port): the delegation this class once asserted no longer
    exists — compact/delete/rm/reindex are native (quintessence.write), doctor/config/init are
    native (quintessence.admin), and unknown commands error natively (same message shape +
    exit 2 as legacy's `*)` case). The qq-legacy stub is now a TRIPWIRE: "LEGACY-CALLED" in
    any output is a dispatch regression."""

    def test_unknown_verb_errors_natively_with_usage(self):
        p = self.run_qq(["totally-bogus-verb"])
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        self.assertIn("qq: unknown command 'totally-bogus-verb'", p.stderr)
        self.assertIn("usage: qq", p.stderr)

    def test_formerly_delegated_verbs_never_hit_legacy(self):
        # Each verb runs against the fenced store; outcomes vary (init scaffolds it, reindex
        # writes INDEX.md, the topic verbs refuse politely) — what this test pins is ONLY the
        # dispatch boundary: none of them may reach the legacy stub. doctor is excluded here
        # (its embedder/ask probes can hit the network) — covered by tests/test-*.sh parity.
        for argv in (["config"], ["init"], ["compact", "t"], ["delete", "t"],
                     ["rm", "t"], ["reindex"]):
            p = self.run_qq(argv)
            self.assertNotIn("LEGACY-CALLED", p.stdout, f"{argv[0]} must not delegate")

    def test_missing_topic_verbs_refuse_natively(self):
        for verb, rc in (("compact", 1), ("delete", 1), ("rm", 1)):
            p = self.run_qq([verb, "no-such-topic"])
            self.assertEqual(p.returncode, rc, f"{verb}: {p.stderr}")
            self.assertIn(f"no HEAD 'no-such-topic'", p.stderr)
            self.assertNotIn("LEGACY-CALLED", p.stdout)

    def test_reindex_is_native_and_writes_index(self):
        p = self.run_qq(["reindex"])
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        self.assertIn("reindexed", p.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.store_dir, "INDEX.md")))

    def test_compact_rejects_non_integer_keep_n(self):
        """The dispatcher refuses what legacy's awk would have silently mangled (a
        non-numeric N coerces to 0 and drops EVERY update-line)."""
        p = self.run_qq(["compact", "topic", "lots"])
        self.assertEqual(p.returncode, 2)
        self.assertIn("keep-N must be an integer", p.stderr)


class TestSearchAskDirectExec(DispatcherHarness):
    """search/ask are direct-exec'd straight to qq-search/qq-ask, NEVER relayed
    through qq-legacy — the grounded bug this avoids (qq-legacy's config bootstrap exports the
    dotenv config-file's QUINTESSENCE_DIR into the environment, which then defeats
    quintessence.storepath's file-vs-env pin distinction for the exec'd child) is documented on
    the `qq` file's `_DIRECT_EXEC`. Same argv/stdin/exit-code passthrough guarantees as
    TestDelegation above, just against the qq-search/qq-ask stubs instead of qq-legacy's."""

    def test_search_direct_execs_to_qq_search_not_legacy(self):
        p = self.run_qq(["search", "a query"])
        self.assertIn("DIRECT-CALLED[qq-search] argv=['a query']", p.stdout)
        self.assertNotIn("LEGACY-CALLED", p.stdout)

    def test_ask_direct_execs_to_qq_ask_not_legacy(self):
        p = self.run_qq(["ask", "a question"])
        self.assertIn("DIRECT-CALLED[qq-ask] argv=['a question']", p.stdout)
        self.assertNotIn("LEGACY-CALLED", p.stdout)

    def test_extra_args_all_forwarded(self):
        p = self.run_qq(["search", "-k", "8", "-g", "some query", "--json"])
        self.assertIn("DIRECT-CALLED[qq-search] argv=['-k', '8', '-g', 'some query', "
                      "'--json']", p.stdout)

    def test_exit_code_propagates_exactly(self):
        p = self.run_qq(["ask", "exit-code", "42"])
        self.assertEqual(p.returncode, 42)

    def test_stdin_passes_through(self):
        p = self.run_qq(["search", "a query"], stdin="piped stdin\n")
        self.assertIn("DIRECT-STDIN=" + repr("piped stdin\n"), p.stdout)


class TestWriteVerbsAreNative(DispatcherHarness):
    """update/essence/new/rewrite/finalize/checkpoint/save are now rendered natively
    (quintessence.write) against a REAL git store this harness initializes itself (unlike the
    read/check-family harnesses above, the write path needs a real `.git` to commit into — the
    fake qq-legacy stub is only there to prove NON-delegation, never actually exercised for
    these verbs). Byte-level parity against the bash engine was once covered by
    tests/test-write-parity.sh; that suite left the tree with the bash engine, and no
    cross-engine parity coverage exists anymore. These tests pin the
    DISPATCH/native-vs-delegate boundary and the basic verb contracts."""

    def setUp(self):
        super().setUp()
        subprocess.run(["git", "init", "-q", self.store_dir], check=True)
        subprocess.run(["git", "-C", self.store_dir, "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", self.store_dir, "config", "user.name", "t"], check=True)

    def test_new_is_native_and_scaffolds(self):
        p = self.run_qq(["new", "alpha", "an essence"])
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        with open(os.path.join(self.store_dir, "alpha.md")) as f:
            text = f.read()
        self.assertIn("# Quintessence — alpha", text)
        self.assertIn("> essence: an essence", text)
        self.assertIn("## RE-ENTER HERE", text)

    def test_new_on_existing_head_refuses_natively(self):
        self.run_qq(["new", "alpha", "e"])
        p = self.run_qq(["new", "alpha", "dup"])
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        self.assertIn("already exists", p.stderr)

    def test_update_is_native_arg_and_stdin_forms(self):
        self.run_qq(["new", "alpha", "e"])
        p = self.run_qq(["update", "alpha", "arg-text"])
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        p2 = self.run_qq(["update", "alpha"], stdin="stdin-text\n")
        self.assertEqual(p2.returncode, 0)
        with open(os.path.join(self.store_dir, "alpha.md")) as f:
            text = f.read()
        self.assertIn("arg-text", text)
        self.assertIn("stdin-text", text)

    def test_essence_is_native(self):
        self.run_qq(["new", "alpha", "seed"])
        p = self.run_qq(["essence", "alpha", "refreshed"])
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        with open(os.path.join(self.store_dir, "alpha.md")) as f:
            text = f.read()
        self.assertIn("> essence: refreshed", text)

    def test_essence_missing_head_refuses_natively(self):
        p = self.run_qq(["essence", "ghost", "text"])
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        self.assertIn("no HEAD 'ghost'", p.stderr)

    def test_rewrite_is_native(self):
        self.run_qq(["new", "alpha", "seed"])
        p = self.run_qq(["rewrite", "alpha"],
                         stdin="# Quintessence — alpha\n> essence: seed\n\n## Notes\nnew\n")
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        with open(os.path.join(self.store_dir, "alpha.md")) as f:
            text = f.read()
        self.assertIn("## Notes\nnew", text)

    def test_rewrite_on_missing_head_refuses_and_points_at_new(self):
        """Carried ruling (sanctioned behavior CHANGE from legacy): `rewrite` on a missing
        topic no longer silently creates an unscaffolded HEAD — it refuses and points at
        `qq new`, matching `update`'s existing missing-HEAD refusal."""
        p = self.run_qq(["rewrite", "ghost"], stdin="whatever\n")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        self.assertIn("qq new", p.stderr)
        self.assertFalse(os.path.isfile(os.path.join(self.store_dir, "ghost.md")))

    def test_finalize_checkpoint_save_all_native_and_journal(self):
        self.run_qq(["new", "alpha", "seed"])
        for verb in ("finalize", "checkpoint", "save"):
            p = self.run_qq([verb, "alpha"])
            self.assertEqual(p.returncode, 0, verb)
            self.assertNotIn("LEGACY-CALLED", p.stdout, verb)
        journal_dir = os.path.join(self.store_dir, "journal", "alpha")
        self.assertTrue(os.path.isdir(journal_dir))
        self.assertGreaterEqual(len(os.listdir(journal_dir)), 1)

    def test_update_missing_flag_leaks_still_errors_natively(self):
        """test-write-surface.sh's own regression matrix, re-asserted here at the dispatcher-
        unit level: a stray unrecognized flag must error, not silently become HEAD content."""
        self.run_qq(["new", "alpha", "e"])
        p = self.run_qq(["update", "alpha", "--bogus-flag"])
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        with open(os.path.join(self.store_dir, "alpha.md")) as f:
            text = f.read()
        self.assertNotIn("--bogus-flag", text)

    def test_write_txn_marker_never_leaks_into_dispatcher_process_env(self):
        """A2 (carried ruling): QQ_WRITE_TXN must never be exported process-wide. This can't be
        observed from OUTSIDE the subprocess (its env dies with it), so this test drives the
        native path IN-PROCESS instead of through run_qq's subprocess."""
        import quintessence.write as writemod
        from quintessence.config import Config
        from quintessence.store import Store
        cfg = Config(env={}, config_file="/nonexistent",
                     overrides={"QUINTESSENCE_DIR": self.store_dir,
                                "QQ_STATE_DIR": os.path.join(self.tmp, "state2")})
        store = Store(cfg)
        self.assertNotIn("QQ_WRITE_TXN", os.environ)
        writemod.new(store, "inproc", "e")
        self.assertNotIn("QQ_WRITE_TXN", os.environ)


class TestDashArgFootguns(DispatcherHarness):
    """A `-`/`--` token must never be swallowed as an object (topic/dir/subcommand): -h/--help
    renders that verb's help; any other dash-token in an object slot is refused, not acted on.
    Recognized flags in their proper positions stay untouched. Regression net for the
    `qq init --help` footgun (init treated --help as its target dir + rewrote the global store
    pointer) and the whole dash-in-object-slot class."""

    # --- -h/--help in the ARGUMENT position renders that verb's help (never swallowed) ---
    def test_init_help_renders_usage_and_does_not_scaffold(self):
        p = self.run_qq(["init", "--help"])
        self.assertEqual(p.returncode, 0)
        self.assertIn("usage: qq init", p.stdout)
        self.assertNotIn("LEGACY-CALLED", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.store_dir, ".git")),
                         "`qq init --help` must render help, not scaffold a store")

    def test_verb_help_shows_that_verbs_own_usage(self):
        for verb, needle in (("brief", "qq brief <topic>"), ("new", "qq new <topic>"),
                             ("update", "qq update <topic>"), ("delete", "qq delete <topic>"),
                             ("check", "qq check")):
            p = self.run_qq([verb, "--help"])
            self.assertEqual(p.returncode, 0, f"{verb} --help exit")
            self.assertIn(needle, p.stdout, f"{verb} --help usage line")

    def test_short_help_flag_also_renders_help(self):
        p = self.run_qq(["show", "-h"])
        self.assertEqual(p.returncode, 0)
        self.assertIn("qq show <topic>", p.stdout)

    # --- a dash-token in the OBJECT slot is refused (exit 2), never acted on ---
    def test_dash_token_refused_in_object_slot(self):
        for args in (["init", "--foo"], ["config", "--foo"], ["new", "--foo"],
                     ["update", "--bar", "text"], ["update", "-x"], ["delete", "--wipe"],
                     ["essence", "--e", "x"], ["compact", "--n"], ["path", "--p"],
                     ["rewrite", "--r"], ["fact", "--f"]):
            p = self.run_qq(args)
            self.assertEqual(p.returncode, 2, f"{args} must be refused (exit 2)")
            # Same phrase the older flag-shaped-topic guard uses — the advice loop-breaker in
            # tests/test-write-surface.sh greps for it, so both guards must speak with one voice.
            self.assertIn("looks like a command-line flag", p.stderr, f"{args} refusal message")
            self.assertNotIn("LEGACY-CALLED", p.stdout)

    def test_init_dash_arg_does_not_scaffold(self):
        p = self.run_qq(["init", "--foo"])
        self.assertEqual(p.returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(self.store_dir, ".git")),
                         "a refused `qq init --foo` must not scaffold a store")

    # --- recognized flags in their proper positions are NOT refused ---
    def test_recognized_flags_not_refused(self):
        for args in (["path", "--global", "x"], ["show", "--global", "x"],
                     ["check", "--fast"], ["check", "--write"], ["reconcile", "--explain"],
                     ["waveoff", "--head", "x"], ["memdir", "--global"]):
            p = self.run_qq(args)
            self.assertNotIn("looks like a command-line flag", p.stderr,
                             f"{args} is a legitimate flag, must not be refused")

    def test_init_project_flag_not_refused(self):
        """`--project` is init's ONE real first-position flag. The
        object-slot guard once refused it, which made project stores uncreatable and took the
        multi-store + recall-composition suites down with it."""
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(proj)
        p = self.run_qq(["init", "--project", proj])
        self.assertNotIn("looks like a command-line flag", p.stderr,
                         "`qq init --project` must not be refused as a dash-object")
        self.assertTrue(os.path.exists(os.path.join(proj, ".quintessence", ".git")),
                        "`qq init --project <dir>` must scaffold the project store")

    # --- `--` still introduces literal text: a --help AFTER `--` is written, not a help request ---
    def test_double_dash_keeps_help_literal(self):
        self.run_qq(["new", "ltopic", "e"])
        p = self.run_qq(["update", "ltopic", "--", "--help"])
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("usage: qq update", p.stdout)     # not intercepted as help
        self.assertNotIn("looks like a command-line flag", p.stderr)  # not refused as a dash-object
        with open(os.path.join(self.store_dir, "ltopic.md")) as f:
            self.assertIn("--help", f.read())               # landed as literal text

    # --- a --token in the COMMAND position stays an unknown command (unchanged) ---
    def test_dash_in_command_position_is_unknown_command(self):
        p = self.run_qq(["--xyz"])
        self.assertEqual(p.returncode, 2)
        self.assertIn("unknown command", p.stderr)


if __name__ == "__main__":
    unittest.main()
