# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.checks: the run_check port. Each TestCase below targets
one section of run_check (see checks.py's own section comments for the qq-legacy mapping),
synthetic-fixture-driven so every branch is exercised regardless of what the real store happens
to contain. Byte-for-byte parity against a real `qq-legacy check --fast` run (both on the live
store AND on a synthetic fixture built to exercise every check class — the live store alone
doesn't trigger mem-health/fm/essence-lag/size) is documented in the P4 build report, not
re-verified here (this suite pins BEHAVIOR; the report is the one-time live-parity evidence)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence.checks import Checks, compute_index_text
from quintessence.config import Config
from quintessence.store import Store


def make_checks(base: str, **overrides) -> Checks:
    over = {"QUINTESSENCE_DIR": os.path.join(base, "store"),
            "QQ_MEMDIR": os.path.join(base, "mem"),
            "QQ_KB_ROOT": os.path.join(base, "kb"),
            "QQ_STATE_DIR": os.path.join(base, "state")}
    over.update(overrides)
    cfg = Config(env={}, config_file="/nonexistent", overrides=over)
    for d in ("QUINTESSENCE_DIR", "QQ_MEMDIR"):
        os.makedirs(cfg.get_path(d), exist_ok=True)
    return Checks(cfg, Store(cfg))


def render(findings) -> list[str]:
    return [f.render() for f in findings]


class TestLinkFindings(unittest.TestCase):
    def test_unresolved_link_is_flagged_sorted_and_deduped(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "store", "alpha.md"), "w") as f:
                f.write("# Quintessence — alpha\n> updated: x\n> essence: e\n\n"
                        "## Notes\nsee [[zebra-topic]] and [[zebra-topic]] and [[apple-topic]]\n")
            findings = c.link_findings()
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].render(),
                              "- [T1 link] 2 unresolved [[link]](s) (may be intentional to-write "
                              "markers): apple-topic, zebra-topic")

    def test_stop_tokens_and_known_slugs_are_not_flagged(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "store", "alpha.md"), "w") as f:
                f.write("# Quintessence — alpha\n> updated: x\n> essence: e\n\n"
                        "## Notes\n[[link]] [[alpha]]\n")
            self.assertEqual(c.link_findings(), [])

    def test_clean_store_has_no_finding(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            self.assertEqual(c.link_findings(), [])

    def test_ambiguous_bare_token_is_its_own_finding_not_unresolved(self):
        """P4.5 namespace ruling: a bare [[backlog]] whose basename matches two DISTINCT kb docs
        (different source repos) is neither silently resolved nor lumped into the unresolved-
        link finding — it gets its own T1 ambiguous finding listing the candidates."""
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            os.makedirs(os.path.join(base, "kb", "docs"))
            os.makedirs(os.path.join(base, "kb", "infra"))
            with open(os.path.join(base, "kb", "docs", "backlog.md"), "w") as f:
                f.write("# docs backlog\n")
            with open(os.path.join(base, "kb", "infra", "backlog.md"), "w") as f:
                f.write("# infra backlog\n")
            with open(os.path.join(base, "store", "alpha.md"), "w") as f:
                f.write("# Quintessence — alpha\n> updated: x\n> essence: e\n\n"
                        "## Notes\nsee [[backlog]]\n")
            findings = c.link_findings()
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].cls, "T1 ambiguous")
            self.assertEqual(findings[0].fields["token"], "backlog")
            self.assertEqual(set(findings[0].fields["candidates"]),
                              {"kb-doc:docs/backlog", "kb-doc:infra/backlog"})
            rendered = findings[0].render()
            self.assertIn("[[backlog]]", rendered)
            self.assertIn("matches more than one candidate", rendered)

    def test_store_exact_hit_dissolves_a_same_named_kb_doc_no_finding_at_all(self):
        """The roadmap case: a HEAD's own exact filename wins outright over a same-basename kb
        doc — not an unresolved link, not an ambiguity, no finding of any kind."""
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            os.makedirs(os.path.join(base, "kb", "docs"))
            with open(os.path.join(base, "kb", "docs", "roadmap.md"), "w") as f:
                f.write("# canonical roadmap brief\n")
            with open(os.path.join(base, "store", "roadmap.md"), "w") as f:
                f.write("# Quintessence — roadmap\n> updated: x\n> essence: e\n")
            with open(os.path.join(base, "store", "alpha.md"), "w") as f:
                f.write("# Quintessence — alpha\n> updated: x\n> essence: e\n\n"
                        "## Notes\nsee [[roadmap]]\n")
            self.assertEqual(c.link_findings(), [])

    def test_unresolved_and_ambiguous_coexist_as_separate_findings(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            os.makedirs(os.path.join(base, "kb", "docs"))
            os.makedirs(os.path.join(base, "kb", "infra"))
            with open(os.path.join(base, "kb", "docs", "backlog.md"), "w") as f:
                f.write("# docs backlog\n")
            with open(os.path.join(base, "kb", "infra", "backlog.md"), "w") as f:
                f.write("# infra backlog\n")
            with open(os.path.join(base, "store", "alpha.md"), "w") as f:
                f.write("# Quintessence — alpha\n> updated: x\n> essence: e\n\n"
                        "## Notes\nsee [[backlog]] and [[nothing-such]]\n")
            findings = c.link_findings()
            classes = sorted(f.cls for f in findings)
            self.assertEqual(classes, ["T1 ambiguous", "T1 link"])


class TestMemoryIndexFindings(unittest.TestCase):
    def test_orphan_memory_not_in_index(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "mem", "MEMORY.md"), "w") as f:
                f.write("# Memory Index\n")
            with open(os.path.join(base, "mem", "orphan.md"), "w") as f:
                f.write("---\nname: orphan\ndescription: d\ntype: reference\n---\nbody\n")
            findings = render(c.memory_index_findings())
            self.assertIn("- [T1 index] memory/orphan.md has no MEMORY.md entry (orphan)", findings)

    def test_dangling_reference_in_index(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "mem", "MEMORY.md"), "w") as f:
                f.write("# Memory Index\n- [gone](gone.md)\n")
            findings = render(c.memory_index_findings())
            self.assertIn("- [T1 index] MEMORY.md references gone.md but the file is missing", findings)

    def test_bijected_memory_is_clean(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "mem", "MEMORY.md"), "w") as f:
                f.write("# Memory Index\n- [ok](ok.md)\n")
            with open(os.path.join(base, "mem", "ok.md"), "w") as f:
                f.write("---\nname: ok\ndescription: d\ntype: reference\n---\nbody\n")
            self.assertEqual(c.memory_index_findings(), [])

    def test_no_memory_index_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            self.assertEqual(c.memory_index_findings(), [])


class TestMemHealthFindings(unittest.TestCase):
    def test_size_over_budget(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base, QQ_MEMINDEX_MAX_BYTES="10")
            with open(os.path.join(base, "mem", "MEMORY.md"), "w") as f:
                f.write("# Memory Index\nmore than ten bytes here\n")
            findings = render(c.mem_health_findings())
            self.assertTrue(any("MEMORY.md is" in f and ">= 10B" in f for f in findings))

    def test_line_over_char_budget(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base, QQ_MEMLINE_MAX_CHARS="20")
            long_line = "- [a-very-long-label-indeed](a-very-long-label-indeed.md) padding padding"
            with open(os.path.join(base, "mem", "MEMORY.md"), "w") as f:
                f.write(f"# Memory Index\n{long_line}\n")
            findings = render(c.mem_health_findings())
            over_line = next(f for f in findings if "index line(s) over" in f)
            self.assertIn(f"a-very-long-label-indeed({len(long_line)})", over_line)

    def test_duplicate_index_entries(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "mem", "MEMORY.md"), "w") as f:
                f.write("# Memory Index\n- [a](dup.md)\n- [b](dup.md)\n- [c](once.md)\n")
            findings = render(c.mem_health_findings())
            dup_line = next(f for f in findings if "duplicate index entr" in f)
            self.assertEqual(dup_line, "- [T1 mem-health] MEMORY.md duplicate index entr(y/ies): dup.md")

    def test_disabled_via_config(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base, QQ_MEM_HEALTH="0", QQ_MEMINDEX_MAX_BYTES="1")
            with open(os.path.join(base, "mem", "MEMORY.md"), "w") as f:
                f.write("# Memory Index\nlots of content over budget\n")
            self.assertEqual(c.mem_health_findings(), [])


class TestHeadMetaFindings(unittest.TestCase):
    def test_missing_updated_and_essence(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "store", "bare.md"), "w") as f:
                f.write("# Quintessence — bare\n\n## Notes\n")
            findings = render(c.head_meta_findings())
            self.assertIn("- [T1 head] HEAD bare missing '> updated:' line", findings)
            self.assertIn("- [T1 head] HEAD bare missing '> essence:' line", findings)

    def test_well_formed_head_is_clean(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "store", "ok.md"), "w") as f:
                f.write("# Quintessence — ok\n> updated: x\n> essence: e\n\n## Notes\n")
            self.assertEqual(c.head_meta_findings(), [])


class TestMemoryFrontmatterFindings(unittest.TestCase):
    def test_missing_all_three_fields(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "mem", "bad.md"), "w") as f:
                f.write("---\nfoo: bar\n---\nbody\n")
            findings = render(c.memory_frontmatter_findings())
            self.assertIn("- [T1 fm] memory/bad.md missing 'name:'", findings)
            self.assertIn("- [T1 fm] memory/bad.md missing 'description:'", findings)
            self.assertIn("- [T1 fm] memory/bad.md missing 'type:'", findings)

    def test_well_formed_memory_is_clean(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "mem", "good.md"), "w") as f:
                f.write("---\nname: good\ndescription: d\ntype: reference\n---\nbody\n")
            self.assertEqual(c.memory_frontmatter_findings(), [])


class TestHeadSizeFindings(unittest.TestCase):
    def test_update_lines_over_hard_bytes_flags_update_lines_kind(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base, QQ_COMPACT_HARD_BYTES="10", QQ_COMPACT_HARD_LINES="1000")
            with open(os.path.join(base, "store", "big.md"), "w") as f:
                f.write("# Quintessence — big\n> updated: " + ("x" * 50) + "\n> essence: e\n\n## Notes\n")
            findings = render(c.head_size_findings())
            self.assertEqual(len(findings), 1)
            self.assertIn("update-lines", findings[0])
            self.assertIn("`qq compact big`", findings[0])

    def test_body_over_hard_bytes_flags_body_kind_when_update_lines_are_lean(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base, QQ_COMPACT_HARD_BYTES="999999", QQ_COMPACT_HARD_LINES="999999",
                             QQ_BODY_HARD_BYTES="10")
            with open(os.path.join(base, "store", "bloated.md"), "w") as f:
                f.write("# Quintessence — bloated\n> updated: x\n> essence: e\n\n## Notes\n" + ("y" * 50) + "\n")
            findings = render(c.head_size_findings())
            self.assertEqual(len(findings), 1)
            self.assertIn("body", findings[0])
            self.assertIn("update-lines lean", findings[0])

    def test_small_head_is_clean(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "store", "small.md"), "w") as f:
                f.write("# Quintessence — small\n> updated: x\n> essence: e\n\n## Notes\n")
            self.assertEqual(c.head_size_findings(), [])


class TestEssenceLagFindings(unittest.TestCase):
    def test_superseded_phrase_still_in_essence_fires(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "store", "alpha.md"), "w") as f:
                f.write(
                    "# Quintessence — alpha\n"
                    "> updated: 2026-07-02T00:00:00Z SUPERSEDES: 'the gizmo runs on 12V only' -> 24V too\n"
                    "> essence: the gizmo runs on 12V only, per spec\n\n## Notes\n")
            findings = render(c.essence_lag_findings())
            self.assertEqual(len(findings), 1)
            self.assertIn('"the gizmo runs on 12V only"', findings[0])
            self.assertIn("HEAD alpha", findings[0])

    def test_stops_at_first_match_per_head(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "store", "alpha.md"), "w") as f:
                f.write(
                    "# Quintessence — alpha\n"
                    "> updated: 2026-07-03T00:00:00Z CORRECTION: 'second phrase over sixteen ch' -> new\n"
                    "> updated: 2026-07-02T00:00:00Z SUPERSEDES: 'first phrase over sixteen ch' -> new\n"
                    "> essence: first phrase over sixteen ch and second phrase over sixteen ch too\n\n## Notes\n")
            findings = render(c.essence_lag_findings())
            self.assertEqual(len(findings), 1)   # not two — only the newest (first-in-file) match

    def test_no_keyword_no_finding(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "store", "alpha.md"), "w") as f:
                f.write("# Quintessence — alpha\n> updated: just a normal note, nothing special\n"
                        "> essence: just a normal note, nothing special\n\n## Notes\n")
            self.assertEqual(c.essence_lag_findings(), [])

    def test_disabled_via_config(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base, QQ_ESSENCE_LAG="0")
            with open(os.path.join(base, "store", "alpha.md"), "w") as f:
                f.write(
                    "# Quintessence — alpha\n"
                    "> updated: 2026-07-02T00:00:00Z SUPERSEDES: 'the gizmo runs on 12V only' -> 24V too\n"
                    "> essence: the gizmo runs on 12V only, per spec\n\n## Notes\n")
            self.assertEqual(c.essence_lag_findings(), [])


class TestIndexStalenessFinding(unittest.TestCase):
    def test_missing_index_is_stale(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "store", "alpha.md"), "w") as f:
                f.write("# Quintessence — alpha\n> updated: x\n> essence: e\n\n## Notes\n")
            findings = render(c.index_staleness_finding())
            self.assertEqual(findings, ["- [T1 index] INDEX.md stale vs HEADs → run `qq reindex`"])

    def test_fresh_index_is_clean(self):
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base)
            with open(os.path.join(base, "store", "alpha.md"), "w") as f:
                f.write("# Quintessence — alpha\n> updated: x\n> essence: e\n\n## Notes\n")
            fresh = compute_index_text(c.store)
            with open(os.path.join(base, "store", "INDEX.md"), "w") as f:
                f.write(fresh)
            self.assertEqual(c.index_staleness_finding(), [])


class TestTier1Order(unittest.TestCase):
    def test_run_check_section_order_is_preserved(self):
        """1 link, 2 index, 2b mem-health, 3 head-meta, 4 frontmatter, 5 size, 6 essence-lag,
        7 index-staleness — run_check's own emission order, since this is what lands in the
        TIER1 findings-queue section verbatim."""
        with tempfile.TemporaryDirectory() as base:
            c = make_checks(base, QQ_COMPACT_HARD_BYTES="1", QQ_COMPACT_HARD_LINES="0")
            with open(os.path.join(base, "mem", "MEMORY.md"), "w") as f:
                f.write("# Memory Index\n")
            with open(os.path.join(base, "mem", "bad.md"), "w") as f:
                f.write("---\nfoo: bar\n---\nbody\n")
            with open(os.path.join(base, "store", "alpha.md"), "w") as f:
                f.write(
                    "# Quintessence — alpha\n"
                    "> updated: 2026-07-02T00:00:00Z SUPERSEDES: 'the gizmo runs on 12V only' -> 24V too\n"
                    "> essence: the gizmo runs on 12V only\n\n## Notes\nsee [[nonexistent-thing]]\n")
            findings = c.tier1_findings()
            classes = [f.cls for f in findings]
            expected = ["T1 link", "T1 index", "T1 fm", "T1 fm", "T1 fm",
                        "T1 size", "T1 essence-lag", "T1 index"]
            self.assertEqual(classes, expected)
            # the two "T1 index" entries are the distinct orphan/stale kinds, in that order
            index_kinds = [f.fields["kind"] for f in findings if f.cls == "T1 index"]
            self.assertEqual(index_kinds, ["orphan", "stale"])


if __name__ == "__main__":
    unittest.main()
