# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit + property tests for quintessence.heads.

Synthetic cases pin the fence-awareness and multi-line update-block handling.
The property test is the load-bearing one: parse(serialize(parse(x))) == parse(x)
and serialize(parse(x)) == x, byte-for-byte, against EVERY real HEAD in ~/quintessence/*.md
and every journal snapshot — READ-ONLY, the live store is a test fixture here, never written.
Skipped (not failed) if the store isn't present, or is present but has no content yet (e.g.
right after a fresh `qq init`) — an empty store is nothing to test, not a failure.
"""
import glob
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence import heads

REAL_STORE = os.path.expanduser(os.environ.get("QUINTESSENCE_DIR", "~/quintessence"))


class TestBasicShape(unittest.TestCase):
    def test_title_essence_updates(self):
        text = (
            "# Quintessence — alpha\n"
            "> updated: 2026-07-02T00:00:00Z (created)\n"
            "> essence: the essence text\n"
            "\n"
            "## RE-ENTER HERE\n"
            "body here.\n"
        )
        h = heads.parse(text)
        self.assertEqual(h.title, "# Quintessence — alpha")
        self.assertEqual(h.title_text, "Quintessence — alpha")
        self.assertEqual(h.essence, "the essence text")
        self.assertEqual(len(h.updates), 1)
        self.assertEqual(h.updates[0].timestamp, "2026-07-02T00:00:00Z")
        self.assertEqual(h.updates[0].text, "(created)")
        self.assertTrue(h.body.startswith("## RE-ENTER HERE"))
        self.assertEqual(h.serialize(), text)

    def test_multiline_update_block_with_continuation(self):
        text = (
            "# Quintessence — M\n"
            "> updated: 2026-06-22T03:00:00Z newest line.\n"
            "KEEP-CONT continuation of the newest block.\n"
            "> updated: 2026-06-22T02:00:00Z middle line.\n"
            "> updated: 2026-06-22T01:00:00Z oldest line.\n"
            "ORPHAN-CONT continuation of the oldest block.\n"
            "> essence: essence-m\n"
            "## RE-ENTER HERE\n"
            "BODY-MARKER stays.\n"
        )
        h = heads.parse(text)
        self.assertEqual(len(h.updates), 3)
        self.assertEqual(h.updates[0].continuation, ["KEEP-CONT continuation of the newest block."])
        self.assertEqual(h.updates[1].continuation, [])
        self.assertEqual(h.updates[2].continuation, ["ORPHAN-CONT continuation of the oldest block."])
        self.assertEqual(h.serialize(), text)

    def test_no_trailing_newline_preserved(self):
        text = "# Quintessence — x\n> updated: 2026-01-01T00:00:00Z hi\n> essence: e\n\n## S\nbody"
        h = heads.parse(text)
        self.assertFalse(h.trailing_newline)
        self.assertEqual(h.serialize(), text)

    def test_headless_file_round_trips(self):
        text = "just some text\nwith no H1 at all\n"
        h = heads.parse(text)
        self.assertIsNone(h.title)
        self.assertEqual(h.serialize(), text)


class TestFenceAwareness(unittest.TestCase):
    def test_updated_marker_inside_body_fence_is_inert(self):
        """A HEAD excerpt pasted as an example INSIDE the body (a code fence) must not be
        mistaken for a real update-line or corrupt the update-line-region byte count (A5)."""
        text = (
            "# Quintessence — self-quote\n"
            "> updated: 2026-07-02T00:00:00Z real update.\n"
            "> essence: real essence\n"
            "\n"
            "## RE-ENTER HERE\n"
            "example of the format:\n"
            "```\n"
            "> updated: 1999-01-01T00:00:00Z FAKE, inside a fence\n"
            "> essence: FAKE essence, inside a fence\n"
            "## FAKE body header, inside a fence\n"
            "```\n"
            "more real body text.\n"
        )
        h = heads.parse(text)
        self.assertEqual(len(h.updates), 1)
        self.assertEqual(h.updates[0].timestamp, "2026-07-02T00:00:00Z")
        self.assertEqual(h.essence, "real essence")
        # the fake update-line inside the fence must NOT inflate the update-line-region size
        self.assertEqual(h.update_line_region_bytes,
                          len("> updated: 2026-07-02T00:00:00Z real update.") + 1)
        self.assertEqual(h.serialize(), text)

    def test_body_header_marker_inside_pre_body_fence_does_not_split_early(self):
        """A '## '-looking line inside a fence BEFORE the real body starts must not be
        mistaken for the header/body boundary."""
        text = (
            "# Quintessence — x\n"
            "> updated: 2026-01-01T00:00:00Z u\n"
            "```\n"
            "## looks like a body header but is inside a fence\n"
            "```\n"
            "> essence: e\n"
            "\n"
            "## REAL BODY START\n"
            "content\n"
        )
        h = heads.parse(text)
        self.assertEqual(h.essence, "e")
        self.assertTrue(h.body.startswith("## REAL BODY START"))
        self.assertEqual(h.serialize(), text)

    def test_tilde_fence_and_longer_closing_fence(self):
        text = (
            "# Quintessence — x\n"
            "> updated: 2026-01-01T00:00:00Z u\n"
            "> essence: e\n"
            "\n"
            "## S\n"
            "~~~~\n"
            "> updated: fake\n"
            "~~~~\n"
        )
        h = heads.parse(text)
        self.assertEqual(h.essence, "e")
        self.assertEqual(h.serialize(), text)


def _real_store_files():
    return sorted(glob.glob(os.path.join(REAL_STORE, "*.md"))) + \
        sorted(glob.glob(os.path.join(REAL_STORE, "journal", "*", "*.md")))


class TestRealStoreRoundTrip(unittest.TestCase):
    # NOTE (P6 fix): the skip condition checks for actual HEAD/journal content, not just
    # directory EXISTENCE — a freshly `qq init`-ed store (e.g. a fresh install's own
    # post-install self-check, tests/run.sh via setup.sh) has a real, existing store dir with
    # zero files in it. The old `skipUnless(os.path.isdir(REAL_STORE), ...)` treated "exists"
    # as "populated" and instead FAILED (assertGreater(0, 0)) on every fresh install — this is
    # a live-store PARITY test (property-tested against real content), meaningless
    # and mis-signaling on a store that legitimately has no content yet.
    @unittest.skipUnless(_real_store_files(), "no real HEAD/journal content on this machine "
                         "(a fresh/empty store is a clean skip, not a failure)")
    def test_every_real_head_and_journal_snapshot_round_trips_losslessly(self):
        files = _real_store_files()
        failures = []
        for f in files:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            h1 = heads.parse(text)
            s1 = h1.serialize()
            if s1 != text:
                failures.append((f, "serialize(parse(x)) != x"))
                continue
            h2 = heads.parse(s1)
            if h2 != h1:
                failures.append((f, "parse(serialize(parse(x))) != parse(x)"))
        if failures:
            detail = "\n".join(f"  {why}: {f}" for f, why in failures)
            self.fail(f"{len(failures)}/{len(files)} real files failed round-trip:\n{detail}")


class TestHeadMeta(unittest.TestCase):
    """head_meta() — the menu/digest UPDATED/ESSENCE column source. Literal port of qq's
    head_meta() awk: FIRST '> updated:'/'> essence:' line, timestamp NOT stripped (unlike
    UpdateItem.text)."""

    def test_both_present(self):
        text = ("# T\n> updated: 2026-07-01T00:00:00Z (created)\n> essence: hi\n\n## S\n")
        self.assertEqual(heads.head_meta(text), ("2026-07-01T00:00:00Z (created)", "hi"))

    def test_only_first_updated_line_wins(self):
        text = ("# T\n> updated: newer\n> updated: older\n> essence: e\n\n## S\n")
        u, e = heads.head_meta(text)
        self.assertEqual(u, "newer")
        self.assertEqual(e, "e")

    def test_missing_essence_is_empty_string(self):
        text = "# T\n> updated: x\n\n## S\n"
        self.assertEqual(heads.head_meta(text), ("x", ""))

    def test_missing_both_is_empty_pair(self):
        self.assertEqual(heads.head_meta("# T\n\n## S\n"), ("", ""))


class TestCountUpdateMarkers(unittest.TestCase):
    def test_counts_raw_lines_not_fence_aware(self):
        text = ("# T\n> updated: a\n> updated: b\n"
                "```\n> updated: inside a fence, still counted (not fence-aware, matches grep -c)\n```\n"
                "> essence: e\n\n## S\n")
        self.assertEqual(heads.count_update_markers(text), 3)

    def test_zero_when_absent(self):
        self.assertEqual(heads.count_update_markers("# T\n> essence: e\n\n## S\n"), 0)


class TestLegacyBrief(unittest.TestCase):
    """legacy_brief() — literal port of qq's brief_one awk. The audit-A5 wart (a bare
    continuation paragraph under '> updated:' is dropped unless it happens to start with
    '> ') is preserved on purpose (byte-parity with the P0 goldens is the P2 acceptance gate,
    not a place to slip in the A5 fix silently)."""

    def test_title_updates_essence_and_reenter_kept_other_sections_dropped(self):
        text = ("# Quintessence — x\n> updated: 2026-07-01T00:00:00Z (created)\n"
                "> essence: e\n\n## RE-ENTER HERE\nre body.\n\n## Notes\nhidden.\n")
        tot, lines = heads.legacy_brief(text)
        self.assertEqual(tot, 1)
        # the blank line between "re body." and "## Notes" is still INSIDE the RE-ENTER section
        # (re_flag only clears on the NEXT "## " header), so it is captured too — matches
        # brief-alpha.golden's two trailing blank lines (this one + brief_one's own echo).
        self.assertEqual(lines, ["# Quintessence — x", "> updated: 2026-07-01T00:00:00Z (created)",
                                  "> essence: e", "## RE-ENTER HERE", "re body.", ""])

    def test_only_newest_3_update_lines_kept_but_all_counted(self):
        text = ("# T\n> updated: u5\n> updated: u4\n> updated: u3\n> updated: u2\n"
                "> updated: u1\n> essence: e\n\n## RE-ENTER HERE\n")
        tot, lines = heads.legacy_brief(text)
        self.assertEqual(tot, 5)
        self.assertEqual(lines, ["# T", "> updated: u5", "> updated: u4", "> updated: u3",
                                  "> essence: e", "## RE-ENTER HERE"])

    def test_bare_continuation_paragraph_is_dropped_a5_wart_preserved(self):
        text = ("# T\n> updated: 2026-07-01T00:00:00Z marker\n"
                "a bare continuation paragraph, not '>'-prefixed\n"
                "> essence: e\n\n## RE-ENTER HERE\n")
        tot, lines = heads.legacy_brief(text)
        self.assertEqual(tot, 1)
        self.assertNotIn("a bare continuation paragraph, not '>'-prefixed", lines)

    def test_blank_line_before_first_section_header_is_dropped(self):
        text = "# T\n> essence: e\n\n## RE-ENTER HERE\n"
        tot, lines = heads.legacy_brief(text)
        self.assertEqual(lines, ["# T", "> essence: e", "## RE-ENTER HERE"])

    def test_no_reenter_section_at_all(self):
        text = "# T\n> essence: e\n\n## Notes\nhidden.\n"
        tot, lines = heads.legacy_brief(text)
        self.assertEqual(lines, ["# T", "> essence: e"])


if __name__ == "__main__":
    unittest.main()
