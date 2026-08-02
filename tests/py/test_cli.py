# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.cli — the P2 read-verb renderers. Mirrors/complements
tests/test-surface-freeze.sh (which drives the real `qq` subprocess end-to-end and IS the
byte-parity oracle against the legacy bash engine); these tests exercise quintessence.cli's
functions directly, at unit granularity, including edge cases the shell fixture doesn't bother
constructing (an unpinned QQ_DIGEST_PIN, a >160-char essence truncation, findings-next branches
built straight from Finding records rather than parsed text, usage rendering).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence import cli
from quintessence.config import Config
from quintessence.findings import FindingsFile
from quintessence.store import Store

GOLD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures", "surface-freeze")


def _read(name: str) -> str:
    with open(os.path.join(GOLD, name), encoding="utf-8") as f:
        return f.read()


def make_store(base: str, **overrides) -> Store:
    over = {
        "QUINTESSENCE_DIR": os.path.join(base, "store"),
        "QQ_MEMDIR": os.path.join(base, "mem"),
        "QQ_STATE_DIR": os.path.join(base, "state"),
        "QQ_KB_ROOT": os.path.join(base, "kb"),
    }
    over.update(overrides)
    cfg = Config(env={}, config_file="/nonexistent", overrides=over)
    store = Store(cfg)
    os.makedirs(store.qdir, exist_ok=True)
    os.makedirs(store.memdir, exist_ok=True)
    os.makedirs(store.state_dir, exist_ok=True)
    return store


HEAD_ALPHA = ("# Quintessence — alpha\n> updated: 2026-07-01T00:00:00Z (created)\n"
              "> essence: alpha essence\n\n## RE-ENTER HERE\nalpha body.\n")
HEAD_BETA = ("# Quintessence — beta\n> updated: 2026-07-02T00:00:00Z (created)\n"
             "> essence: beta essence\n\n## RE-ENTER HERE\nbeta body.\n")


class TestMenu(unittest.TestCase):
    def test_empty_store(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            out = cli.render_menu(store)
            self.assertIn("TOPIC", out)
            self.assertIn("ESSENCE", out)
            self.assertTrue(out.endswith("\n"))

    def test_two_heads_rendered_in_slug_order(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            (store.qdir / "beta.md").write_text(HEAD_BETA)
            (store.qdir / "alpha.md").write_text(HEAD_ALPHA)
            out = cli.render_menu(store)
            self.assertLess(out.index("alpha"), out.index("beta"))
            self.assertIn("alpha essence", out)

    def test_missing_index_is_generated_and_written(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            (store.qdir / "alpha.md").write_text(HEAD_ALPHA)
            self.assertFalse(store.index_path.exists())
            out = cli.render_menu(store)
            self.assertIn("alpha essence", out)
            self.assertTrue(store.index_path.exists())
            self.assertEqual(store.index_path.read_text(), out)

    def test_missing_store_dir_refuses_cleanly_instead_of_crashing(self):
        """tools-2 (2026-07-29 review): bare `qq`/`qq menu` on a fresh machine (no `qq init`
        yet) used to crash with a raw FileNotFoundError from index_path.write_text() — the
        single most likely first command, and the worst-behaved one (list/digest/check/show/
        doctor all already degrade cleanly on a missing store). Must now refuse the same way
        `doctor` reports it ("store dir missing ... run: qq init"), via StorePathError so the
        `qq` dispatcher's existing top-level handler gives a clean nonzero exit, not a
        traceback."""
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            import shutil as _shutil
            _shutil.rmtree(store.qdir)   # simulate: never `qq init`'d
            self.assertFalse(store.qdir.is_dir())
            with self.assertRaises(cli.StorePathError) as ctx:
                cli.render_menu(store)
            msg = str(ctx.exception)
            self.assertIn("store dir missing", msg)
            self.assertIn(str(store.qdir), msg)
            self.assertIn("qq init", msg)

    def test_existing_index_is_served_verbatim_even_if_stale(self):
        """Port of qq's `[ -f "$INDEX" ] || reindex; cat "$INDEX"` — caught by live-store
        parity testing: `qq update` never reindexes, so on a real store INDEX.md routinely
        lags the live HEAD content, and `qq menu` faithfully shows that lag rather than
        silently re-deriving fresh content."""
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            (store.qdir / "alpha.md").write_text(HEAD_ALPHA)   # live content says "alpha essence"
            stale = "# Quintessence — exit-note menu\n\nSTALE CACHED CONTENT, not re-derived\n"
            store.index_path.write_text(stale)
            out = cli.render_menu(store)
            self.assertEqual(out, stale)
            self.assertNotIn("alpha essence", out)


class TestDigest(unittest.TestCase):
    def test_empty_store_no_findings_no_actlog(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            out = cli.render_digest(store, store.config)
            self.assertIn("QUINTESSENCE OPEN LOOPS", out)
            self.assertNotIn("last touched", out)
            self.assertNotIn("PENDING CONSISTENCY", out)

    def test_pin_row_only_when_pinned_head_exists(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base, QQ_DIGEST_PIN="alpha")
            (store.qdir / "alpha.md").write_text(HEAD_ALPHA)
            out = cli.render_digest(store, store.config)
            self.assertIn("MAP", out)
            self.assertIn("alpha", out)

    def test_no_pin_row_when_pin_set_but_head_absent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base, QQ_DIGEST_PIN="nonexistent")
            out = cli.render_digest(store, store.config)
            self.assertNotIn("MAP", out)

    def test_essence_truncated_past_160_chars(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            long_ess = "x" * 200
            head = f"# Quintessence — long\n> updated: 2026-07-01T00:00:00Z (created)\n> essence: {long_ess}\n\n## RE-ENTER HERE\n"
            (store.qdir / "long.md").write_text(head)
            out = cli.render_digest(store, store.config)
            self.assertIn("…", out)
            self.assertNotIn("x" * 161, out)

    def test_findings_preamble_printed_when_nonempty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            ff = FindingsFile()
            ff.set_section("TIER1", ["- [T1 link] 1 unresolved [[link]](s) (may be intentional to-write markers): foo"])
            out = cli.render_digest(store, store.config, ff)
            self.assertIn("PENDING CONSISTENCY FINDINGS (1", out)
            self.assertIn("[T1 link]", out)

    def test_findings_render_is_capped_with_remainder_count(self):
        """Flagged judgment call (cli.FINDINGS_DIGEST_MAX): a growing SALIENCE queue
        (APPEND-only, never auto-resolved) must not inflate every future SessionStart digest
        without bound. The header count (fcount) stays the TRUE total; only the rendered lines
        are capped, with a remainder note."""
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            ff = FindingsFile()
            n = cli.FINDINGS_DIGEST_MAX + 7
            ff.set_section("SALIENCE", [f"- [SALIENCE] item {i}" for i in range(n)])
            out = cli.render_digest(store, store.config, ff)
            self.assertIn(f"PENDING CONSISTENCY FINDINGS ({n}", out)   # true total, uncapped
            self.assertIn("item 0", out)
            self.assertIn(f"item {cli.FINDINGS_DIGEST_MAX - 1}", out)   # last SHOWN item
            self.assertNotIn(f"item {cli.FINDINGS_DIGEST_MAX}", out)    # first item past the cap
            self.assertIn("(+7 more – `qq findings` for the full list)", out)

    def test_findings_render_uncapped_when_at_or_under_the_limit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            ff = FindingsFile()
            ff.set_section("SALIENCE", [f"- [SALIENCE] item {i}" for i in range(cli.FINDINGS_DIGEST_MAX)])
            out = cli.render_digest(store, store.config, ff)
            self.assertNotIn("more –", out)
            self.assertIn(f"item {cli.FINDINGS_DIGEST_MAX - 1}", out)

    def _write_corpus_refs(self, store, records):
        d = store.state_dir / "refs"
        d.mkdir(parents=True, exist_ok=True)
        import json as _json
        with open(d / "refs.jsonl", "a", encoding="utf-8") as fh:
            for r in records:
                fh.write(_json.dumps(r) + "\n")

    def _corpus_rec(self, ident="/x", status="suspect", file="fact.md", **extra):
        d = {"corpus": "mem", "file": file, "kind": "file", "id": ident,
             "fp": "sha256:aa", "asof": "2026-07-01T10:00:00Z", "status": status}
        d.update(extra)
        return d

    def test_corpus_refs_section_renders_suspect_and_born_stale_only(self):
        """The SessionStart digest is the corpus records' render
        surface — suspect + missing-at-write listed; ok/fp-error/retired and HEAD-bound
        records (brief/show annotate those inline) never appear."""
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            self._write_corpus_refs(store, [
                self._corpus_rec("/moved", "suspect",
                                  suspect_asof="2026-07-02T00:00:00Z"),
                self._corpus_rec("/never-was", "missing-at-write", fp=None),
                self._corpus_rec("/fine", "ok"),
                self._corpus_rec("/quiet", "fp-error", fp=None),
                self._corpus_rec("/silenced", "retired"),
                {"head": "T", "line_ts": "2026-07-01T10:00:00Z", "kind": "file",
                 "id": "/head-bound", "fp": "sha256:bb", "status": "suspect",
                 "asof": "2026-07-01T10:00:00Z"},
            ])
            out = cli.render_digest(store, store.config)
            self.assertIn("CORPUS CLAIMS vs REALITY (2;", out)
            self.assertIn("[suspect] mem:fact.md -> file /moved (moved since 2026-07-02)", out)
            self.assertIn("[born-stale] mem:fact.md -> file /never-was", out)
            for absent in ("/fine", "/quiet", "/silenced", "/head-bound"):
                self.assertNotIn(absent, out)

    def test_seed_sourced_born_stale_stays_out_of_the_digest(self):
        # the seed's missing-at-write findings are a ONE-TIME audit report (other-host
        # artifacts, negative claims, historical facts — see the live 2026-07-04 seed), not a
        # standing banner; commit-sourced born-stale is fresh drift and still shows.
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            self._write_corpus_refs(store, [
                self._corpus_rec("/seeded-gone", "missing-at-write", fp=None,
                                  src="seed:2026-07-04T20:39:00Z"),
                self._corpus_rec("/committed-gone", "missing-at-write", fp=None,
                                  src="/home/x/memory@abc1234"),
                self._corpus_rec("/seeded-moved", "suspect",
                                  src="seed:2026-07-04T20:39:00Z"),
            ])
            out = cli.render_digest(store, store.config)
            self.assertNotIn("/seeded-gone", out)
            self.assertIn("/committed-gone", out)
            self.assertIn("/seeded-moved", out)   # suspect shows regardless of source

    def test_corpus_refs_section_absent_when_clean(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            self._write_corpus_refs(store, [self._corpus_rec("/fine", "ok")])
            out = cli.render_digest(store, store.config)
            self.assertNotIn("CORPUS CLAIMS", out)

    def test_corpus_refs_capped_with_remainder(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            n = cli.CORPUS_REFS_DIGEST_MAX + 3
            self._write_corpus_refs(store, [
                self._corpus_rec(f"/moved-{i:02d}", "suspect", file=f"f{i:02d}.md")
                for i in range(n)])
            out = cli.render_digest(store, store.config)
            self.assertIn(f"CORPUS CLAIMS vs REALITY ({n};", out)
            self.assertIn("(+3 more", out)

    def test_corpus_refs_last_record_wins_like_the_view(self):
        # a rebind appends fresh records; an ok that supersedes a suspect must silence it
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            self._write_corpus_refs(store, [
                self._corpus_rec("/x", "suspect"),
                self._corpus_rec("/x", "ok"),
            ])
            out = cli.render_digest(store, store.config)
            self.assertNotIn("CORPUS CLAIMS", out)

    def test_footer_prints_whenever_actlog_nonempty_regardless_of_head_count(self):
        """The P2 ratified deviation: unlike legacy (which only reaches the footer when total
        HEADs > max due to a set-e/pipeline-exit accident), render_digest always checks the
        activity log independently of HEAD count."""
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            (store.qdir / "alpha.md").write_text(HEAD_ALPHA)
            (store.state_dir / "activity.log").write_text(
                "2026-07-01T00:00:00Z\talpha\n2026-07-01T01:00:00Z\tbeta\n")
            out = cli.render_digest(store, store.config)
            self.assertIn("last touched: beta, alpha", out)

    def test_matches_p0_digest_plain_golden(self):
        # NOW must be *run-day* (matching tests/test-surface-freeze.sh's own `date -u` capture):
        # the golden's age column says "today", which only holds when the fixture stamp and the
        # render share a date. A hard-coded 2026-07-03 here made this test fail on any other
        # day — a date-flake found (pre-existing) during the B2 build, fixed without weakening
        # the byte-comparison.
        import tempfile
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            (store.qdir / "alpha.md").write_text(
                f"# Quintessence — alpha\n> updated: {now} (created)\n"
                "> essence: alpha essence for surface freeze\n\n## RE-ENTER HERE\nalpha re-enter body.\n\n"
                "## Notes\nalpha notes.\n")
            (store.qdir / "beta.md").write_text(
                f"# Quintessence — beta\n> updated: {now} (created)\n"
                "> essence: beta essence for surface freeze pairing\n\n## RE-ENTER HERE\nbeta re-enter body.\n")
            out = cli.render_digest(store, store.config)
            golden = _read("digest-plain.golden").replace("<<NOW>>", now)
            self.assertEqual(out, golden)


class TestListPathShowBrief(unittest.TestCase):
    def test_list_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            self.assertEqual(cli.render_list(store), "")

    def test_list_two_heads(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            (store.qdir / "alpha.md").write_text(HEAD_ALPHA)
            (store.qdir / "beta.md").write_text(HEAD_BETA)
            self.assertEqual(cli.render_list(store), "alpha\nbeta\n")

    def test_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            self.assertEqual(cli.render_path(store, "foo"), f"{store.qdir}/foo.md\n")

    def test_show_missing_and_present(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            (store.qdir / "alpha.md").write_text(HEAD_ALPHA)
            out = cli.render_show(store, ["alpha", "nope"])
            self.assertIn("===== quintessence: alpha =====", out)
            self.assertIn(HEAD_ALPHA, out)
            self.assertIn("----- no HEAD for 'nope' (skipping) -----", out)

    def test_brief_drops_non_reenter_sections(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            (store.qdir / "alpha.md").write_text(
                "# Quintessence — alpha\n> updated: 2026-07-01T00:00:00Z (created)\n"
                "> essence: e\n\n## RE-ENTER HERE\nre body.\n\n## Notes\nshould not appear.\n")
            out = cli.render_brief(store, ["alpha"])
            self.assertIn("re body.", out)
            self.assertNotIn("should not appear", out)
            self.assertNotIn("## Notes", out)

    def test_brief_only_first_3_update_lines_counted_and_shown(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            head = ("# Quintessence — alpha\n"
                    "> updated: 2026-07-05T00:00:00Z u5\n"
                    "> updated: 2026-07-04T00:00:00Z u4\n"
                    "> updated: 2026-07-03T00:00:00Z u3\n"
                    "> updated: 2026-07-02T00:00:00Z u2\n"
                    "> updated: 2026-07-01T00:00:00Z u1 (created)\n"
                    "> essence: e\n\n## RE-ENTER HERE\n")
            (store.qdir / "alpha.md").write_text(head)
            out = cli.render_brief(store, ["alpha"])
            self.assertIn("of 5 update-lines", out)
            self.assertIn("u5", out); self.assertIn("u4", out); self.assertIn("u3", out)
            self.assertNotIn("u2", out); self.assertNotIn("u1", out)

    def test_brief_near_miss_topic_shows_hint(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            (store.qdir / "packaging.md").write_text(HEAD_ALPHA)
            out = cli.render_brief(store, ["packging"])
            self.assertIn("no HEAD for 'packging'", out)
            self.assertIn("Did you mean: packaging?", out)

    def test_brief_wildly_wrong_topic_no_hint(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            (store.qdir / "packaging.md").write_text(HEAD_ALPHA)
            out = cli.render_brief(store, ["zzzzzzz"])
            self.assertIn("----- no HEAD for 'zzzzzzz' (skipping) -----", out)
            self.assertNotIn("Did you mean", out)

    def test_brief_existing_topic_golden_untouched(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            (store.qdir / "alpha.md").write_text(HEAD_ALPHA)
            out = cli.render_brief(store, ["alpha"])
            self.assertIn("===== quintessence: alpha (BRIEF", out)
            self.assertIn("alpha body.", out)
            self.assertNotIn("Did you mean", out)

    def test_show_near_miss_topic_shows_hint(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            (store.qdir / "packaging.md").write_text(HEAD_ALPHA)
            out = cli.render_show(store, ["packging"])
            self.assertIn("no HEAD for 'packging'", out)
            self.assertIn("Did you mean: packaging?", out)

    def test_show_wildly_wrong_topic_no_hint(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            (store.qdir / "packaging.md").write_text(HEAD_ALPHA)
            out = cli.render_show(store, ["zzzzzzz"])
            self.assertIn("----- no HEAD for 'zzzzzzz' (skipping) -----", out)
            self.assertNotIn("Did you mean", out)


class TestFindingsRendering(unittest.TestCase):
    def test_render_findings_list_empty_is_empty_string(self):
        self.assertEqual(cli.render_findings_list(FindingsFile()), "")

    def test_render_findings_next_clean(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            out = cli.render_findings_next(store, store.config, FindingsFile())
            self.assertEqual(out, "qq findings next: no pending findings – clean.\n")

    def test_render_findings_next_t2_stale_uses_structured_fields(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            (store.qdir / "beta.md").write_text(HEAD_BETA)
            os.makedirs(os.path.join(store.config.get_path("QQ_KB_ROOT"), "memory"), exist_ok=True)
            mpath = os.path.join(store.config.get_path("QQ_KB_ROOT"), "memory", "betamem.md")
            with open(mpath, "w") as f:
                f.write("---\nname: betamem\ndescription: d\ntype: reference\n---\nbody\n")
            ff = FindingsFile()
            ff.set_section("XREF", [
                "- [T2 stale?] memory betamem (2026-01-01) vs HEAD beta (updated "
                "2026-06-01T00:00:00Z, sim 0.71) — verify the memory's claims against the "
                "HEAD; edit or wave off (`qq waveoff betamem beta`)."])
            out = cli.render_findings_next(store, store.config, ff)
            self.assertIn("----- memory: betamem (full body) -----", out)
            self.assertIn("body", out)
            self.assertIn("compatible, not stale:  qq waveoff betamem beta", out)
            self.assertIn("===== quintessence: beta (BRIEF", out)

    def test_render_findings_next_t1_size_missing_head_skips_bytes_line(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            ff = FindingsFile()
            ff.set_section("TIER1", [
                "- [T1 size] HEAD ghost: update-lines 40kB / 20 lines → `qq compact ghost` "
                "(folds old update-lines to the journal, keeps newest ~5 + the body; "
                "`qq brief` reads it meanwhile)"])
            out = cli.render_findings_next(store, store.config, ff)
            self.assertNotIn("bytes total", out)   # HEAD ghost doesn't exist -> block skipped
            self.assertIn("qq compact ghost", out)

    def test_render_findings_next_t1_size_body_variant_command(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            ff = FindingsFile()
            ff.set_section("TIER1", [
                "- [T1 size] HEAD bigone: body 55kB (update-lines lean) → `qq compact` can't "
                "shrink this; condense the ## sections in a deliberate rewrite, or read via "
                "`qq brief bigone`"])
            out = cli.render_findings_next(store, store.config, ff)
            self.assertIn("condense the ## sections in bigone by hand", out)

    def test_render_findings_next_generic_extracts_backticked_commands(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            ff = FindingsFile()
            ff.set_section("TIER1", ["- [T1 index] INDEX.md stale vs HEADs → run `qq reindex`"])
            out = cli.render_findings_next(store, store.config, ff)
            self.assertIn("  qq reindex\n", out)
            self.assertIn("or: edit the source file directly, then re-run: qq check --write", out)


class TestUsage(unittest.TestCase):
    def test_usage_mentions_all_verb_groups(self):
        out = cli.render_usage()
        for token in ("READ / RESUME", "WRITE ", "LIFECYCLE / MAINTENANCE"):
            self.assertIn(token, out)

    def test_usage_mentions_ref_flag(self):
        # B1's --ref flag exists on all four write verbs; the help text must say so (tracked
        # from the B1 review as a deliberate USAGE_TEXT change, its own commit).
        out = cli.render_usage()
        self.assertIn("--ref kind:id", out)
        self.assertIn("file/git/unit/probe/port/rfile", out)

    def test_help_does_not_claim_qq_write_still_works(self):
        # tools-1 (2026-07-29 review): --help used to say "DEPRECATED aliases (still work):
        # `qq-write` -> ...", but the qq-write binary was purged from the tree with the bash
        # engine (P9) — no file of that name exists any more, so the claim was falsifiable on
        # the very first command a new user runs. ONBOARDING.md already gets this right
        # ("were retired with the bash engine"); --help must not contradict it.
        out = cli.render_usage()
        self.assertNotIn("qq-write` -> ", out)
        self.assertNotRegex(out, r"qq-write.{0,40}still work")
        self.assertIn("Retired with the bash engine", out)
        self.assertIn("qq-write", out)   # still mentioned, but as "retired", not "still works"


class TestFact(unittest.TestCase):
    """`qq fact <name>` — read-only atomic-fact viewer (search hits point at it for `mem` hits)."""

    def test_prints_fact_verbatim(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            body = "---\nname: user-name\n---\nThomas Lakofski.\n"
            with open(os.path.join(store.memdir, "user_name.md"), "w") as f:
                f.write(body)
            self.assertEqual(cli.render_fact(store, "user_name"), body)

    def test_tolerates_md_suffix(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            body = "the fact\n"
            with open(os.path.join(store.memdir, "x.md"), "w") as f:
                f.write(body)
            self.assertEqual(cli.render_fact(store, "x.md"), body)

    def test_miss_suggests_near_names_and_does_not_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            for n in ("user_nationality", "user_neurotype"):
                with open(os.path.join(store.memdir, f"{n}.md"), "w") as f:
                    f.write("x\n")
            out = cli.render_fact(store, "user_natonality")  # typo
            self.assertIn("no memory fact 'user_natonality'", out)
            self.assertIn("user_nationality", out)          # difflib suggestion


if __name__ == "__main__":
    unittest.main()
