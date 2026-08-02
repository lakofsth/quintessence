# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.admin — the setup/diagnostic verbs (`qq doctor`, `qq init`,
`qq config`). Presently just the `doctor` git-author-identity check (human-3, 2026-07-29
review): every other admin.py behavior is exercised end-to-end by tests/test-*.sh.

Identity isolation: `doctor()` shells out to plain `git config user.name`/`user.email` (no
`-C`/`env=`, so it reads whatever the REAL process env + cwd resolve). These tests patch
os.environ's HOME/GIT_CONFIG_GLOBAL/GIT_CONFIG_SYSTEM and os.chdir into a scratch directory
for the duration of the call — never `git config --global` (this machine's real global
identity is live and must not be touched), and never assume the ambient identity is any
particular value (it may be genuinely unset on some other host running this suite)."""
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence import admin
from quintessence.config import Config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_config(base: str, **overrides) -> Config:
    over = {
        "QUINTESSENCE_DIR": os.path.join(base, "store"),
        "QQ_MEMDIR": os.path.join(base, "mem"),
        "QQ_STATE_DIR": os.path.join(base, "state"),
        "QQ_KB_ROOT": os.path.join(base, "kb"),
        # Never let doctor's optional embedder probe reach a real ollama endpoint (11434/11435
        # are live services on this machine and must not be touched by a test) — point it at a
        # closed local port so the probe fails fast and locally instead.
        "QQ_OLLAMA_URL": "http://127.0.0.1:1",
        "QQ_ASK_ENDPOINTS": [],
    }
    over.update(overrides)
    return Config(env={}, config_file="/nonexistent", overrides=over)


@contextlib.contextmanager
def scratch_git_identity_env(scratch_home: str, *, configured: bool):
    """Isolate `git config user.name`/`user.email` lookups to `scratch_home`, for the duration
    of the `with` block: patches os.environ (HOME + GIT_CONFIG_GLOBAL/GIT_CONFIG_SYSTEM, so
    global/system scope can't fall through to this machine's real identity) and os.chdir()s into
    a fresh, non-repo scratch directory (so there is no local repo-scope config to leak in
    either — quintessence-dist's OWN .git has a real user.name/user.email configured locally).
    `configured=True` writes a throwaway global config with a fake identity first."""
    global_cfg = os.path.join(scratch_home, ".gitconfig")
    if configured:
        with open(global_cfg, "w", encoding="utf-8") as fh:
            fh.write("[user]\n\tname = Scratch Test User\n\temail = scratch-test@example.invalid\n")
    cwd_dir = os.path.join(scratch_home, "cwd")
    os.makedirs(cwd_dir, exist_ok=True)
    env_patch = {
        "HOME": scratch_home,
        "GIT_CONFIG_GLOBAL": global_cfg,   # exists only when configured=True; git treats a
                                            # missing file at this path as an empty config
        "GIT_CONFIG_SYSTEM": os.path.join(scratch_home, "no-such-system-gitconfig"),
    }
    prev_cwd = os.getcwd()
    try:
        os.chdir(cwd_dir)
        with mock.patch.dict(os.environ, env_patch):
            yield
    finally:
        os.chdir(prev_cwd)


class TestDoctorGitIdentity(unittest.TestCase):
    """human-3 (2026-07-29 review): `qq doctor` used to report all-green with no git author
    identity configured, and the very next command (the first write) then failed outright. This
    pins the fix: doctor now warns explicitly, with the exact fix command, but still exits 0 —
    a missing identity is diagnosable-but-not-fatal, same as every other store/corpus check."""

    def test_missing_identity_warns_with_fix_command_and_does_not_fail(self):
        with tempfile.TemporaryDirectory() as base:
            scratch_home = os.path.join(base, "home")
            os.makedirs(scratch_home)
            config = make_config(base)
            with scratch_git_identity_env(scratch_home, configured=False):
                rc, text = admin.doctor(config, REPO_ROOT)
        self.assertEqual(rc, 0, text)   # required deps are present on this host; see module note
        self.assertIn("git author identity not configured", text)
        self.assertIn('git config --global user.name  "Your Name"', text)
        self.assertIn("git config --global user.email you@example.com", text)

    def test_configured_identity_no_warning(self):
        with tempfile.TemporaryDirectory() as base:
            scratch_home = os.path.join(base, "home")
            os.makedirs(scratch_home)
            config = make_config(base)
            with scratch_git_identity_env(scratch_home, configured=True):
                rc, text = admin.doctor(config, REPO_ROOT)
        self.assertEqual(rc, 0, text)
        self.assertNotIn("git author identity not configured", text)

    def test_identityless_store_warns_even_when_cwd_repo_has_an_identity(self):
        """This verification pass (2026-07-29): doctor used to run a BARE `git config` with no
        `-C <store>`, so it reported on whatever repo the process cwd happened to be in — an
        identity-less store with the process cwd sitting inside an UNRELATED repo that HAS an
        identity printed no warning at all, and the very next `qq new` then failed with the
        exact identity error doctor exists to preflight. Pins the fix: the check is scoped to
        the STORE (git -C qdir), so the unrelated cwd repo's identity must not mask a genuinely
        identity-less store."""
        with tempfile.TemporaryDirectory() as base:
            scratch_home = os.path.join(base, "home")
            os.makedirs(scratch_home)
            store_dir = os.path.join(base, "store")
            os.makedirs(store_dir)
            subprocess.run(["git", "init", "-q", store_dir], check=True)   # no identity set here
            config = make_config(base)
            with scratch_git_identity_env(scratch_home, configured=False):
                # cwd is a DIFFERENT repo that DOES have an identity — must not leak in.
                other_repo = os.path.join(scratch_home, "cwd")
                subprocess.run(["git", "init", "-q", other_repo], check=True)
                subprocess.run(["git", "-C", other_repo, "config", "user.name", "Other Repo User"],
                                check=True)
                subprocess.run(["git", "-C", other_repo, "config", "user.email",
                                "other-repo@example.invalid"], check=True)
                rc, text = admin.doctor(config, REPO_ROOT)
        self.assertEqual(rc, 0, text)
        self.assertIn("git author identity not configured", text)

    def test_missing_git_binary_does_not_crash_doctor(self):
        # git itself missing is already flagged as a ✗ required dep elsewhere in doctor's
        # output; the identity check must fail soft (OSError) rather than raise, same as it
        # would for any other host missing git.
        with tempfile.TemporaryDirectory() as base:
            scratch_home = os.path.join(base, "home")
            os.makedirs(scratch_home)
            config = make_config(base)
            fake_bin = os.path.join(base, "fake-bin")
            os.makedirs(fake_bin)
            for name in ("bash", "jq", "flock", "python3"):
                real = shutil.which(name)
                if real:
                    os.symlink(real, os.path.join(fake_bin, name))
            with scratch_git_identity_env(scratch_home, configured=False):
                with mock.patch.dict(os.environ, {"PATH": fake_bin}):
                    rc, text = admin.doctor(config, REPO_ROOT)
        self.assertEqual(rc, 1)   # git itself is the missing required dep here
        self.assertIn("git MISSING", text)


if __name__ == "__main__":
    unittest.main()


class ConfigGetEffectiveValue20260729(unittest.TestCase):
    """`qq config get` answers "what does the FILE say", and its empty stdout used to read as
    "not configured anywhere" — a false negative that, for a key whose default switches a
    protection ON, reports that protection as absent. Stdout and rc are unchanged (scripts
    depend on both); the explanation goes to stderr."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = os.path.join(self.tmp.name, "config")
        with open(self.cfg_path, "w") as fh:
            fh.write("QQ_DIGEST_PIN=pinned-topic\n")
        self.config = Config(env={"HOME": self.tmp.name}, config_file=self.cfg_path)

    def test_file_set_key_prints_on_stdout_rc0(self):
        rc, out, err = admin.config_cmd(self.config, ["get", "QQ_DIGEST_PIN"])
        self.assertEqual((rc, out.strip(), err), (0, "pinned-topic", ""))

    def test_default_supplied_key_explains_on_stderr_keeping_stdout_and_rc(self):
        rc, out, err = admin.config_cmd(self.config, ["get", "QQ_REDACT_FILE"])
        self.assertEqual(rc, 1)          # unchanged: the FILE does not set it
        self.assertEqual(out, "")        # unchanged: scripts read stdout
        self.assertIn("IS in effect", err)
        self.assertIn("built-in default", err)
        self.assertIn(self.tmp.name, err)   # and names the value it resolves to

    def test_unregistered_key_says_so(self):
        rc, out, err = admin.config_cmd(self.config, ["get", "QQ_NOT_A_REAL_KEY"])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("not a registered key", err)

    def test_csv_key_is_rendered_in_config_file_syntax_not_python_repr(self):
        """2026-07-29 verification round, F4: csv keys resolve to a list, and the note printed
        its repr — ['https://claude.ai'] — which is not something a reader can paste back into
        the config file. Comma-joined, no brackets/quotes."""
        rc, out, err = admin.config_cmd(self.config, ["get", "QQ_REMOTE_ALLOWED_ORIGINS"])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("default: https://claude.ai\n", err)
        for ch in ("['", "']", "["):
            self.assertNotIn(ch, err, f"python list repr leaked into `qq config get`: {err!r}")

    def test_multi_value_csv_key_joins_with_commas(self):
        rc, out, err = admin.config_cmd(self.config, ["get", "QQ_BIND_EXCLUDE_ROOTS"])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("default: /run, /proc, /sys, /tmp, /dev\n", err)
