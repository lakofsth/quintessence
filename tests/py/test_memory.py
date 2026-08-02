# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit + property tests for quintessence.memory.

Property test (same discipline as heads): parse/serialize round-trips losslessly
against EVERY real memory file under the configured QQ_MEMDIR (env override, else the
registry's generic default `~/.quintessence-memory`) — READ-ONLY, never written. Skipped (not
failed) if that dir isn't present, or has no content yet, on the machine running the suite.
"""
import glob
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence import memory

REAL_MEMDIR = os.path.expanduser(os.environ.get("QQ_MEMDIR", "~/.quintessence-memory"))


class TestBasicShape(unittest.TestCase):
    def test_flat_frontmatter(self):
        text = "---\nname: foo\ndescription: a fact\ntype: reference\n---\nbody text.\n"
        m = memory.parse(text)
        self.assertTrue(m.has_frontmatter)
        self.assertEqual(m.name, "foo")
        self.assertEqual(m.description, "a fact")
        self.assertEqual(m.type, "reference")
        self.assertEqual(m.body, "body text.")
        self.assertEqual(m.serialize(), text)

    def test_nested_metadata_type(self):
        text = ("---\nname: foo\ndescription: \"a fact\"\nmetadata: \n  model: x\n"
                "  node_type: memory\n  type: project\n---\nbody\n")
        m = memory.parse(text)
        self.assertEqual(m.name, "foo")
        self.assertEqual(m.description, '"a fact"')
        self.assertEqual(m.type, "project")
        self.assertEqual(m.serialize(), text)

    def test_xref_override(self):
        text = "---\nname: foo\ndescription: d\nmetadata:\n  type: feedback\n  xref: track\n---\nbody\n"
        m = memory.parse(text)
        self.assertEqual(m.xref, "track")

    def test_no_frontmatter_round_trips(self):
        text = "just plain prose, no frontmatter at all\n"
        m = memory.parse(text)
        self.assertFalse(m.has_frontmatter)
        self.assertIsNone(m.name)
        self.assertEqual(m.serialize(), text)

    def test_unclosed_frontmatter_treated_as_no_frontmatter(self):
        text = "---\nname: foo\nno closing delimiter\n"
        m = memory.parse(text)
        self.assertFalse(m.has_frontmatter)
        self.assertEqual(m.serialize(), text)

    def test_no_trailing_newline_preserved(self):
        text = "---\nname: foo\n---\nbody"
        m = memory.parse(text)
        self.assertFalse(m.trailing_newline)
        self.assertEqual(m.serialize(), text)


def _real_memory_files():
    return sorted(glob.glob(os.path.join(REAL_MEMDIR, "*.md")))


class TestRealMemoryRoundTrip(unittest.TestCase):
    # P6 fix (same class as test_heads.TestRealStoreRoundTrip): skip on NO CONTENT, not merely
    # on the dir not existing — a freshly-created empty QQ_MEMDIR must not fail this.
    @unittest.skipUnless(_real_memory_files(), "no real memory content on this machine "
                         "(a fresh/empty memory dir is a clean skip, not a failure)")
    def test_every_real_memory_file_round_trips_losslessly(self):
        files = _real_memory_files()
        failures = []
        for f in files:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            m1 = memory.parse(text)
            s1 = m1.serialize()
            if s1 != text:
                failures.append((f, "serialize(parse(x)) != x"))
                continue
            m2 = memory.parse(s1)
            if m2 != m1:
                failures.append((f, "parse(serialize(parse(x))) != parse(x)"))
        if failures:
            detail = "\n".join(f"  {why}: {f}" for f, why in failures)
            self.fail(f"{len(failures)}/{len(files)} real memory files failed round-trip:\n{detail}")


if __name__ == "__main__":
    unittest.main()
