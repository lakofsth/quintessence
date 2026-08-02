# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.findings: the Finding record, its class-specific renderers
(verified byte-for-byte against the P0 surface-freeze goldens — those ARE the oracle for
"today's exact line format"), parse_line's best-effort field recovery, and FindingsFile's
on-disk marker-format round-trip + section semantics (auto-resolve vs append)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence.findings import Finding, FindingsFile, parse_line

GOLD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures", "surface-freeze")


def _read(name: str) -> str:
    with open(os.path.join(GOLD, name), encoding="utf-8") as f:
        return f.read()


class TestRenderersMatchP0Goldens(unittest.TestCase):
    def test_t1_link_matches_golden(self):
        golden = _read("findings-list.golden").split("\n")[0]
        f = Finding(cls="T1 link", fields={"tokens": ["foo", "bar"]})
        self.assertEqual(f.render(), golden)

    def test_t2_stale_matches_golden(self):
        golden = _read("findings-list.golden").split("\n")[1]
        f = Finding(cls="T2 stale?", fields={
            "memory": "betamem", "mem_date": "2026-01-01", "head": "beta",
            "head_date": "2026-06-01T00:00:00Z", "sim": 0.71,
        })
        self.assertEqual(f.render(), golden)

    def test_t1_size_update_lines_matches_golden(self):
        golden = _read("findings-next-t1size.golden").split("\n")[0]
        f = Finding(cls="T1 size", fields={
            "head": "alpha", "kind": "update-lines", "kb": 40, "lines": 20,
        })
        self.assertEqual(f.render(), golden)

    def test_t1_size_body_variant(self):
        f = Finding(cls="T1 size", fields={"head": "bigone", "kind": "body", "kb": 55})
        self.assertEqual(f.render(),
                          "- [T1 size] HEAD bigone: body 55kB (update-lines lean) → "
                          "`qq compact` can't shrink this; condense the ## sections in a "
                          "deliberate rewrite, or read via `qq brief bigone`")

    def test_t1_ambiguous_renders_token_and_candidates(self):
        """P4.5: the namespace design ruling's new Finding class — no P0 golden exists (this is
        new behavior, not a frozen-surface port), so this pins the wording directly."""
        f = Finding(cls="T1 ambiguous", fields={
            "token": "backlog", "candidates": ["kb-doc:docs/backlog", "kb-doc:infra/backlog"]})
        rendered = f.render()
        self.assertEqual(rendered,
                          "- [T1 ambiguous] [[backlog]] matches more than one candidate "
                          "(kb-doc:docs/backlog, kb-doc:infra/backlog) – a genuine cross-repo/"
                          "store naming collision, not a broken link: qualify the reference "
                          "(e.g. `[[kb-source/backlog]]`) or adjudicate which is meant")


class TestParseLine(unittest.TestCase):
    def test_round_trips_t1_link_with_fields(self):
        line = _read("findings-list.golden").split("\n")[0]
        f = parse_line(line)
        self.assertEqual(f.cls, "T1 link")
        self.assertEqual(f.fields["tokens"], ["foo", "bar"])
        self.assertEqual(f.render(), line)

    def test_round_trips_t2_stale_with_fields(self):
        line = _read("findings-list.golden").split("\n")[1]
        f = parse_line(line)
        self.assertEqual(f.cls, "T2 stale?")
        self.assertEqual(f.fields["memory"], "betamem")
        self.assertEqual(f.fields["head"], "beta")
        self.assertAlmostEqual(f.fields["sim"], 0.71)
        self.assertEqual(f.render(), line)

    def test_unknown_class_still_round_trips_verbatim(self):
        line = "- [T1 index] INDEX.md stale vs HEADs → run `qq reindex`"
        f = parse_line(line)
        self.assertEqual(f.cls, "T1 index")
        self.assertEqual(f.fields, {})
        self.assertEqual(f.render(), line)   # verbatim replay, no structured renderer needed

    def test_garbage_line_has_empty_class_and_still_round_trips(self):
        line = "not a finding line at all"
        f = parse_line(line)
        self.assertEqual(f.cls, "")
        self.assertEqual(f.render(), line)

    def test_finding_without_renderer_or_text_raises(self):
        f = Finding(cls="T1 link", fields={})
        with self.assertRaises(KeyError):
            f.render()   # missing required field -> KeyError from the renderer, not silent

    def test_round_trips_t1_ambiguous_with_fields(self):
        line = ("- [T1 ambiguous] [[backlog]] matches more than one candidate "
                "(kb-doc:docs/backlog, kb-doc:infra/backlog) — a genuine cross-repo/store "
                "naming collision, not a broken link: qualify the reference "
                "(e.g. `[[kb-source/backlog]]`) or adjudicate which is meant")
        f = parse_line(line)
        self.assertEqual(f.cls, "T1 ambiguous")
        self.assertEqual(f.fields["token"], "backlog")
        self.assertEqual(f.fields["candidates"], ["kb-doc:docs/backlog", "kb-doc:infra/backlog"])
        self.assertEqual(f.render(), line)


class TestFindingJson(unittest.TestCase):
    def test_json_round_trip(self):
        f = Finding(cls="T2 stale?", fields={"memory": "m", "head": "h", "sim": 0.6,
                                              "mem_date": "d1", "head_date": "d2"})
        f2 = Finding.from_json(f.to_json())
        self.assertEqual(f2.cls, f.cls)
        self.assertEqual(f2.fields, f.fields)
        self.assertEqual(f2.render(), f.render())


class TestFindingsFile(unittest.TestCase):
    def test_parse_matches_p0_digest_fixture_content(self):
        text = ("<!-- TIER1:START -->\n"
                "- [T1 link] 2 unresolved [[link]](s) (may be intentional to-write markers): foo, bar\n"
                "<!-- TIER1:END -->\n"
                "<!-- XREF:START -->\n"
                "- [T2 stale?] memory betamem (2026-01-01) vs HEAD beta (updated "
                "2026-06-01T00:00:00Z, sim 0.71) – verify the memory's claims against the "
                "HEAD; edit or wave off (`qq waveoff betamem beta`).\n"
                "<!-- XREF:END -->\n"
                "<!-- AUDIT:START -->\n<!-- AUDIT:END -->\n"
                "<!-- SALIENCE:START -->\n<!-- SALIENCE:END -->\n")
        ff = FindingsFile.parse(text)
        self.assertEqual(ff.render() + "\n", _read("findings-list.golden"))
        self.assertEqual(ff.count(), 2)

    def test_serialize_is_lossless_for_empty_file(self):
        empty = ("<!-- TIER1:START -->\n<!-- TIER1:END -->\n<!-- XREF:START -->\n<!-- XREF:END -->\n"
                  "<!-- AUDIT:START -->\n<!-- AUDIT:END -->\n"
                  "<!-- SALIENCE:START -->\n<!-- SALIENCE:END -->\n")
        ff = FindingsFile.parse(empty)
        self.assertEqual(ff.serialize(), empty)
        self.assertEqual(ff.count(), 0)

    def test_set_section_is_full_replace_auto_resolve(self):
        ff = FindingsFile()
        ff.set_section("TIER1", ["- [T1 link] finding one"])
        self.assertEqual(ff.count(), 1)
        ff.set_section("TIER1", [])   # the drift is gone -> the finding disappears
        self.assertEqual(ff.count(), 0)

    def test_append_salience_unions_rather_than_replaces(self):
        ff = FindingsFile()
        ff.append_salience(["- [SALIENCE] first captured item"])
        ff.append_salience(["- [SALIENCE] second captured item"])
        self.assertEqual(ff.get_section("SALIENCE"),
                          ["- [SALIENCE] first captured item", "- [SALIENCE] second captured item"])

    def test_section_order_is_tier1_xref_audit_salience(self):
        ff = FindingsFile()
        ff.set_section("AUDIT", ["- [AUDIT] a"])
        ff.set_section("TIER1", ["- [T1 x] t"])
        ff.append_salience(["- [SALIENCE] s"])
        ff.set_section("XREF", ["- [T2 stale?] x"])
        self.assertEqual([f.text for f in ff.findings()],
                          ["- [T1 x] t", "- [T2 stale?] x", "- [AUDIT] a", "- [SALIENCE] s"])

    def test_blank_lines_within_a_section_are_not_counted(self):
        ff = FindingsFile()
        ff.set_section("TIER1", ["- [T1 x] real", "", "   "])
        self.assertEqual(ff.count(), 1)

    def test_ad_hoc_section_not_in_section_order_still_renders(self):
        """Caught by live-store parity testing: a real
        pending-findings.md has grown a FIDELITY section (fidelity-audit.sh's own
        `findings_set_section FIDELITY` call, findings.sh's marker regex being fully generic
        — `<!-- .*:START -->` matches ANY tag, not just the four SECTION_ORDER names). A
        FindingsFile that only recognized SECTION_ORDER silently dropped this section's
        content from render_lines()/count() entirely — a real `qq findings`/`qq digest`
        divergence from qq-legacy on that store, not a hypothetical."""
        text = ("<!-- TIER1:START -->\n<!-- TIER1:END -->\n"
                "<!-- XREF:START -->\n<!-- XREF:END -->\n"
                "<!-- AUDIT:START -->\n<!-- AUDIT:END -->\n"
                "<!-- FIDELITY:START -->\nno fidelity findings — all sampled\n<!-- FIDELITY:END -->\n"
                "<!-- SALIENCE:START -->\n<!-- SALIENCE:END -->\n")
        ff = FindingsFile.parse(text)
        self.assertEqual(ff.count(), 1)
        self.assertEqual(ff.render_lines(), ["no fidelity findings — all sampled"])
        self.assertEqual(ff.get_section("FIDELITY"), ["no fidelity findings — all sampled"])

    def test_ad_hoc_section_missing_from_salience_still_parses(self):
        """The live store's actual FIDELITY section sits BETWEEN AUDIT and SALIENCE — and on
        at least one real snapshot, SALIENCE itself was entirely absent from the file (never
        written by findings_set_section since no producer had used it yet). Parsing must not
        choke on a section this instance's default scaffold expects but the file omits."""
        text = ("<!-- TIER1:START -->\n- [T1 link] 1 unresolved [[link]](s) (may be intentional "
                "to-write markers): x\n<!-- TIER1:END -->\n"
                "<!-- XREF:START -->\n<!-- XREF:END -->\n"
                "<!-- AUDIT:START -->\n<!-- AUDIT:END -->\n"
                "<!-- FIDELITY:START -->\nfidelity line\n<!-- FIDELITY:END -->\n")
        ff = FindingsFile.parse(text)
        self.assertEqual(ff.count(), 2)
        self.assertEqual(ff.get_section("SALIENCE"), [])   # pre-seeded empty, not an error


if __name__ == "__main__":
    unittest.main()
