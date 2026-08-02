# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.remotepolicy — the deny policy fronting the hosted remote face.

The security claims of the remote face reduce to this module, so the tests are written as the
claims themselves: crown jewels never leave on ANY profile; a withheld topic is denied on the
standard profile and served on the wide one; a missing deny list denies everything rather than
serving it; and the matcher does not drift from the ask path's."""
import os
import sys
import tempfile
import unittest

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ENGINE)

from quintessence.config import Config  # noqa: E402
from quintessence.ask import Ask  # noqa: E402
from quintessence.remotepolicy import (  # noqa: E402
    PROFILE_STANDARD,
    PROFILE_WIDE,
    DenyListUnavailable,
    RemotePolicy,
    is_safe_topic,
    read_entries,
    slug_matches,
)

CROWN = "covert-channel-lab"
WITHHELD = "finnair-inflight-tunnel"
GREEN = "flowtun-stack"


class RemotePolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.crown_file = os.path.join(self.tmp.name, "remote-deny-slugs")
        self.withheld_file = os.path.join(self.tmp.name, "redact-slugs")
        self._write(self.crown_file, f"# crown jewels\n{CROWN}\ncvp-*\n")
        self._write(self.withheld_file, f"# withheld topics\n{WITHHELD}\n")

    def _write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def _policy(self, profile=PROFILE_STANDARD, crown=None, withheld=None):
        cfg = Config(overrides={
            "QQ_REMOTE_DENY_FILE": crown if crown is not None else self.crown_file,
            "QQ_REDACT_FILE": withheld if withheld is not None else self.withheld_file,
        })
        return RemotePolicy(cfg, profile=profile)

    # ---- axis B: crown jewels are never liftable -----------------------------------------
    def test_crown_denied_on_standard(self):
        self.assertTrue(self._policy(PROFILE_STANDARD).denies(f"kb/quintessence/{CROWN}.md"))

    def test_crown_denied_on_wide_too(self):
        """The whole point of axis B: the wide credential lifts axis A and NOTHING else."""
        self.assertTrue(self._policy(PROFILE_WIDE).denies(f"kb/quintessence/{CROWN}.md"))

    def test_crown_prefix_glob_denied_on_both_profiles(self):
        for profile in (PROFILE_STANDARD, PROFILE_WIDE):
            with self.subTest(profile=profile):
                self.assertTrue(self._policy(profile).denies("kb/quintessence/cvp-anything.md"))

    # ---- axis A: the withheld-topics list is liftable BY CREDENTIAL, and only that -------
    def test_withheld_topic_denied_on_standard(self):
        self.assertTrue(self._policy(PROFILE_STANDARD).denies(f"kb/quintessence/{WITHHELD}.md"))

    def test_withheld_topic_served_on_wide(self):
        self.assertFalse(self._policy(PROFILE_WIDE).denies(f"kb/quintessence/{WITHHELD}.md"))

    def test_green_served_on_both(self):
        for profile in (PROFILE_STANDARD, PROFILE_WIDE):
            with self.subTest(profile=profile):
                self.assertFalse(self._policy(profile).denies(f"kb/quintessence/{GREEN}.md"))

    # ---- fail-closed ---------------------------------------------------------------------
    def test_missing_crown_file_denies_everything(self):
        p = self._policy(crown=os.path.join(self.tmp.name, "nope"))
        self.assertTrue(p.denies(f"kb/quintessence/{GREEN}.md"))
        self.assertEqual(p.filter_hits([{"path": f"kb/quintessence/{GREEN}.md"}]), [])
        with self.assertRaises(DenyListUnavailable):
            p.preflight()

    def test_missing_withheld_topics_file_denies_everything_even_on_wide(self):
        """A wide profile does not CONSULT axis A — but a standard profile that silently became
        wide because a file went missing is the failure mode this refuses to allow, so absence is
        an error on every profile, not an empty list."""
        p = self._policy(PROFILE_WIDE, withheld=os.path.join(self.tmp.name, "nope"))
        self.assertTrue(p.denies(f"kb/quintessence/{GREEN}.md"))
        with self.assertRaises(DenyListUnavailable):
            p.preflight()

    def test_empty_but_present_file_is_a_statement_not_a_misconfiguration(self):
        empty = os.path.join(self.tmp.name, "empty")
        self._write(empty, "# nothing is crown jewels here\n")
        p = self._policy(crown=empty)
        p.preflight()  # must NOT raise
        self.assertFalse(p.denies(f"kb/quintessence/{GREEN}.md"))
        self.assertTrue(p.denies(f"kb/quintessence/{WITHHELD}.md"))  # axis A still applies

    def test_control_char_in_entry_is_fatal(self):
        """A deny-list entry containing a control character (null byte, etc.) makes the entire
        list unreadable — the policy fails closed rather than silently skipping the entry."""
        for bad_char, label in [("\x00", "null"), ("\x01", "SOH"), ("\x1f", "US"), ("\x7f", "DEL")]:
            with self.subTest(char=label):
                bad_file = os.path.join(self.tmp.name, f"bad-{label}")
                self._write(bad_file, f"good-entry\nbad{bad_char}entry\n")
                p = self._policy(crown=bad_file)
                self.assertTrue(p.denies(f"kb/quintessence/{GREEN}.md"))
                with self.assertRaises(DenyListUnavailable):
                    p.preflight()

    def test_read_entries_distinguishes_unreadable_from_empty(self):
        self.assertIsNone(read_entries(os.path.join(self.tmp.name, "absent")))
        empty = os.path.join(self.tmp.name, "e2")
        self._write(empty, "\n# just a comment\n")
        self.assertEqual(read_entries(empty), [])

    # ---- topic gate (the `brief` verb shells out) -----------------------------------------
    def test_unsafe_topic_names_denied(self):
        p = self._policy()
        for bad in ("--help", "-x", "../../etc/passwd", "a/b", "", "a b", "x" * 200, "$(id)"):
            with self.subTest(topic=bad):
                self.assertFalse(is_safe_topic(bad))
                self.assertTrue(p.topic_denied(bad))

    def test_trailing_newline_topic_not_safe(self):
        self.assertFalse(is_safe_topic("valid-name\n"))
        p = self._policy()
        self.assertTrue(p.topic_denied("valid-name\n"))

    def test_topic_denied_matches_the_deny_lists(self):
        p = self._policy()
        self.assertTrue(p.topic_denied(CROWN))
        self.assertTrue(p.topic_denied(WITHHELD))
        self.assertFalse(p.topic_denied(GREEN))
        self.assertFalse(self._policy(PROFILE_WIDE).topic_denied(WITHHELD))

    # ---- filter_hits ----------------------------------------------------------------------
    def test_filter_hits_drops_denied_and_keeps_the_rest(self):
        hits = [
            {"path": f"kb/quintessence/{GREEN}.md"},
            {"path": f"kb/quintessence/{CROWN}.md"},
            {"path": f"kb/quintessence/{WITHHELD}.md"},
        ]
        kept = [h["path"] for h in self._policy(PROFILE_STANDARD).filter_hits(hits)]
        self.assertEqual(kept, [f"kb/quintessence/{GREEN}.md"])
        kept_wide = [h["path"] for h in self._policy(PROFILE_WIDE).filter_hits(hits)]
        self.assertEqual(kept_wide, [f"kb/quintessence/{GREEN}.md", f"kb/quintessence/{WITHHELD}.md"])

    def test_filter_hits_drops_hit_with_empty_path(self):
        hits = [
            {"path": f"kb/quintessence/{GREEN}.md"},
            {"path": ""},
            {"path": f"kb/quintessence/{CROWN}.md"},
        ]
        kept = self._policy(PROFILE_STANDARD).filter_hits(hits)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["path"], f"kb/quintessence/{GREEN}.md")

    def test_filter_hits_drops_hit_with_none_path(self):
        hits = [
            {"path": f"kb/quintessence/{GREEN}.md"},
            {"path": None},
        ]
        kept = self._policy(PROFILE_STANDARD).filter_hits(hits)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["path"], f"kb/quintessence/{GREEN}.md")

    def test_filter_hits_drops_hit_without_path_key(self):
        hits = [
            {"path": f"kb/quintessence/{GREEN}.md"},
            {"score": 0.9},
            {"path": f"kb/quintessence/{CROWN}.md"},
        ]
        kept = self._policy(PROFILE_STANDARD).filter_hits(hits)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["path"], f"kb/quintessence/{GREEN}.md")

    # ---- no drift from the ask path -------------------------------------------------------
    def test_matcher_parity_with_ask(self):
        """remotepolicy.slug_matches re-implements Ask._slug_redacted (which is private to the ask
        path and has its own parity suite). Pin them together so neither can drift alone."""
        entries = [CROWN, "cvp-*", WITHHELD]
        paths = [
            f"kb/quintessence/{CROWN}.md",
            f"kb/quintessence/{GREEN}.md",
            "kb/quintessence/cvp-anything.md",
            "kb/memory/cvp-nested/deep.md",
            f"kb/docs/{WITHHELD}.md",
            "kb/docs/unrelated.md",
            f"some/path/with/{CROWN}/inside.md",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(slug_matches(path, entries), Ask._slug_redacted(path, entries))

    def test_unknown_profile_rejected(self):
        cfg = Config(overrides={"QQ_REMOTE_DENY_FILE": self.crown_file,
                                "QQ_REDACT_FILE": self.withheld_file})
        with self.assertRaises(ValueError):
            RemotePolicy(cfg, profile="god-mode")


if __name__ == "__main__":
    unittest.main()
