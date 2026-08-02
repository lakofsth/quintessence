# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Startup-shape tests for quintessence.remotepolicy — does the remote interface start on a fresh
install, and does it still refuse to start when it should?

The defect: read_entries returns None for a missing file, and entries() treated that as fatal for
BOTH axes. The withheld-topics list (axis A) is an optional convenience whose file most installs never
create, so a fresh install could not start the remote interface at all. The fix distinguishes
"not configured" (built-in default, file absent -> empty list) from "configured but unusable"
(explicitly set, file absent -> fatal), for axis A only. Axis B (crown jewels) stays fail-closed
unconditionally.
"""
import os
import sys
import tempfile
import unittest

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ENGINE)

from quintessence.config import Config  # noqa: E402
from quintessence.remotepolicy import (  # noqa: E402
    PROFILE_STANDARD,
    PROFILE_WIDE,
    DenyListUnavailable,
    RemotePolicy,
)

GREEN = "flowtun-stack"
CROWN = "covert-channel-lab"


class FreshInstallStartupTest(unittest.TestCase):
    """Neither file present, nothing configured — the fresh-install shape."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.config_dir = os.path.join(self.home, ".config", "quintessence")
        os.makedirs(self.config_dir, exist_ok=True)

    def _default_config(self):
        return Config(env={"HOME": self.home})

    def _crown_path(self):
        return os.path.join(self.config_dir, "remote-deny-slugs")

    def _withheld_path(self):
        return os.path.join(self.config_dir, "redact-slugs")

    def _write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_fresh_install_starts_when_crown_file_present(self):
        """Crown jewels file exists (empty); withheld-topics file absent at default. The server must
        start: nothing withheld on axis A, axis B allows because the file is empty."""
        self._write(self._crown_path(), "# nothing is crown jewels\n")
        cfg = self._default_config()
        for profile in (PROFILE_STANDARD, PROFILE_WIDE):
            with self.subTest(profile=profile):
                p = RemotePolicy(cfg, profile=profile)
                p.preflight()  # must not raise
                self.assertFalse(p.denies(f"kb/quintessence/{GREEN}.md"))

    def test_fresh_install_withheld_axis_withholds_nothing(self):
        """With no withheld-topics file and no configuration, axis A contributes no entries — a
        topic that would be denied only by withheld-topics entries is served."""
        self._write(self._crown_path(), "# empty\n")
        cfg = self._default_config()
        p = RemotePolicy(cfg, PROFILE_STANDARD)
        self.assertEqual(p.entries(), [])

    def test_fresh_install_crown_absent_still_fatal(self):
        """Neither file present: the never-shared list refuses to start — its fail-closed
        behaviour is not softened, even when nothing is configured."""
        cfg = self._default_config()
        for profile in (PROFILE_STANDARD, PROFILE_WIDE):
            with self.subTest(profile=profile):
                p = RemotePolicy(cfg, profile=profile)
                with self.assertRaises(DenyListUnavailable):
                    p.preflight()
                self.assertTrue(p.denies(f"kb/quintessence/{GREEN}.md"))

    @unittest.skipIf(os.getuid() == 0, "cannot restrict file permissions as root")
    def test_default_withheld_exists_but_unreadable_is_fatal(self):
        """Withheld-topics file at the default path exists but cannot be read — fatal, not
        silently treated as empty.  Only true absence at the default path means nothing withheld."""
        self._write(self._crown_path(), "# empty\n")
        withheld = self._withheld_path()
        self._write(withheld, "some-topic\n")
        os.chmod(withheld, 0o000)
        self.addCleanup(lambda: os.chmod(withheld, 0o200))
        cfg = self._default_config()
        p = RemotePolicy(cfg, PROFILE_STANDARD)
        with self.assertRaises(DenyListUnavailable) as cm:
            p.preflight()
        msg = str(cm.exception)
        self.assertIn(withheld, msg)
        self.assertIn("cannot be read", msg)

    def test_crown_absent_message_says_not_found(self):
        """When the never-shared list does not exist, the message says the file was not found,
        names the path, and tells the operator that the file is required and that an empty file
        is a valid answer."""
        cfg = self._default_config()
        p = RemotePolicy(cfg)
        with self.assertRaises(DenyListUnavailable) as cm:
            p.preflight()
        msg = str(cm.exception)
        self.assertIn("not found", msg)
        self.assertIn(self._crown_path(), msg)
        self.assertIn("empty file", msg)


class ExplicitWithheldListConfigTest(unittest.TestCase):
    """The withheld-topics list explicitly configured — missing must still be fatal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.crown_file = os.path.join(self.tmp.name, "remote-deny-slugs")
        with open(self.crown_file, "w", encoding="utf-8") as f:
            f.write(f"# crown jewels\n{CROWN}\n")

    def test_explicit_withheld_list_missing_is_fatal(self):
        """Operator set QQ_REDACT_FILE explicitly; the file does not exist. That is an error —
        they asked for it, so silently ignoring it would be worse."""
        missing = os.path.join(self.tmp.name, "nonexistent-redact-slugs")
        cfg = Config(overrides={
            "QQ_REMOTE_DENY_FILE": self.crown_file,
            "QQ_REDACT_FILE": missing,
        })
        p = RemotePolicy(cfg, PROFILE_STANDARD)
        with self.assertRaises(DenyListUnavailable):
            p.preflight()

    def test_explicit_withheld_list_missing_message_says_not_found(self):
        """The message for an explicitly configured but absent withheld-topics list says the file
        was not found, and names the path."""
        missing = os.path.join(self.tmp.name, "nonexistent-redact-slugs")
        cfg = Config(overrides={
            "QQ_REMOTE_DENY_FILE": self.crown_file,
            "QQ_REDACT_FILE": missing,
        })
        p = RemotePolicy(cfg, PROFILE_STANDARD)
        with self.assertRaises(DenyListUnavailable) as cm:
            p.preflight()
        msg = str(cm.exception)
        self.assertIn("not found", msg)
        self.assertIn(missing, msg)

    @unittest.skipIf(os.getuid() == 0, "cannot restrict file permissions as root")
    def test_explicit_withheld_exists_but_unreadable_is_fatal(self):
        """Operator set QQ_REDACT_FILE explicitly; the file exists but cannot be read. That is
        an error — an unreadable list the operator asked for must not be silently skipped."""
        withheld_file = os.path.join(self.tmp.name, "redact-slugs")
        with open(withheld_file, "w", encoding="utf-8") as f:
            f.write("some-topic\n")
        os.chmod(withheld_file, 0o000)
        self.addCleanup(lambda: os.chmod(withheld_file, 0o200))
        cfg = Config(overrides={
            "QQ_REMOTE_DENY_FILE": self.crown_file,
            "QQ_REDACT_FILE": withheld_file,
        })
        p = RemotePolicy(cfg, PROFILE_STANDARD)
        with self.assertRaises(DenyListUnavailable) as cm:
            p.preflight()
        msg = str(cm.exception)
        self.assertIn("cannot be read", msg)
        self.assertIn(withheld_file, msg)

    def test_explicit_withheld_list_present_works_as_before(self):
        """Operator set QQ_REDACT_FILE explicitly; the file exists and has entries. Axis A
        applies those entries on the standard profile and lifts them on wide."""
        withheld_file = os.path.join(self.tmp.name, "redact-slugs")
        withheld = "sensitive-topic"
        with open(withheld_file, "w", encoding="utf-8") as f:
            f.write(f"{withheld}\n")
        cfg = Config(overrides={
            "QQ_REMOTE_DENY_FILE": self.crown_file,
            "QQ_REDACT_FILE": withheld_file,
        })
        p_std = RemotePolicy(cfg, PROFILE_STANDARD)
        p_std.preflight()
        self.assertTrue(p_std.denies(f"kb/quintessence/{withheld}.md"))

        p_wide = RemotePolicy(cfg, PROFILE_WIDE)
        p_wide.preflight()
        self.assertFalse(p_wide.denies(f"kb/quintessence/{withheld}.md"))


class CrownJewelsAlwaysFailClosedTest(unittest.TestCase):
    """The crown-jewels axis stays fatal when absent, regardless of how it was configured."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.withheld_file = os.path.join(self.tmp.name, "redact-slugs")
        with open(self.withheld_file, "w", encoding="utf-8") as f:
            f.write("# empty\n")

    def test_crown_absent_explicit_is_fatal(self):
        missing = os.path.join(self.tmp.name, "nope")
        cfg = Config(overrides={
            "QQ_REMOTE_DENY_FILE": missing,
            "QQ_REDACT_FILE": self.withheld_file,
        })
        for profile in (PROFILE_STANDARD, PROFILE_WIDE):
            with self.subTest(profile=profile):
                p = RemotePolicy(cfg, profile=profile)
                with self.assertRaises(DenyListUnavailable):
                    p.preflight()

    @unittest.skipIf(os.getuid() == 0, "cannot restrict file permissions as root")
    def test_crown_unreadable_message_says_cannot_be_read(self):
        """A never-shared list that exists but cannot be opened gets a distinct message from
        one that does not exist at all."""
        path = os.path.join(self.tmp.name, "unreadable-deny-slugs")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# content\n")
        os.chmod(path, 0o000)
        self.addCleanup(lambda: os.chmod(path, 0o200))
        cfg = Config(overrides={
            "QQ_REMOTE_DENY_FILE": path,
            "QQ_REDACT_FILE": self.withheld_file,
        })
        p = RemotePolicy(cfg)
        with self.assertRaises(DenyListUnavailable) as cm:
            p.preflight()
        msg = str(cm.exception)
        self.assertIn("cannot be read", msg)
        self.assertNotIn("not found", msg)

    def test_withheld_invalid_utf8_is_fatal(self):
        """A withheld-topics file containing non-UTF-8 bytes is unreadable — fatal at startup
        with the exists-but-cannot-be-read message, same as a permission-denied file."""
        crown = os.path.join(self.tmp.name, "remote-deny-slugs")
        with open(crown, "w", encoding="utf-8") as f:
            f.write("# empty\n")
        withheld = os.path.join(self.tmp.name, "redact-slugs")
        with open(withheld, "wb") as f:
            f.write(b"good-topic\n\xff\xfe bad-bytes\n")
        cfg = Config(overrides={
            "QQ_REMOTE_DENY_FILE": crown,
            "QQ_REDACT_FILE": withheld,
        })
        p = RemotePolicy(cfg, PROFILE_STANDARD)
        with self.assertRaises(DenyListUnavailable) as cm:
            p.preflight()
        msg = str(cm.exception)
        self.assertIn("cannot be read", msg)
        self.assertIn(withheld, msg)

    def test_crown_absent_at_default_is_fatal(self):
        home = self.tmp.name
        config_dir = os.path.join(home, ".config", "quintessence")
        os.makedirs(config_dir, exist_ok=True)
        cfg = Config(env={"HOME": home})
        for profile in (PROFILE_STANDARD, PROFILE_WIDE):
            with self.subTest(profile=profile):
                p = RemotePolicy(cfg, profile=profile)
                with self.assertRaises(DenyListUnavailable):
                    p.preflight()


if __name__ == "__main__":
    unittest.main()
