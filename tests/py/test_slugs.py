# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.slugs.SlugResolver.

Synthetic tests pin normalize()/strip_type_prefix() and the candidate-dedup-by-realpath
behaviour (a corpus symlink farm must not manufacture a false "collision" out of the same
underlying file reachable two ways). The real-store test is the load-bearing one (per the P1
brief): every [[link]] token currently in HEADs + memory must resolve, or be on a locally-
supplied documented-dangler list (see QQ_TEST_DOCUMENTED_DANGLERS below) — a real, hand-
verified adjudication, not a guess. Skipped (not failed) if the real store/kb isn't present or
has no content yet.
"""
import glob
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence.config import Config
from quintessence.slugs import SlugResolver, normalize, strip_type_prefix
from quintessence.store import Store, _locale_sort_key

REAL_QDIR = os.path.expanduser(os.environ.get("QUINTESSENCE_DIR", "~/quintessence"))
REAL_MEMDIR = os.path.expanduser(os.environ.get("QQ_MEMDIR", "~/.quintessence-memory"))
REAL_KB_ROOT = os.path.expanduser(os.environ.get("QQ_KB_ROOT", "~/kb"))

# Intentional to-write link markers (targets that don't exist YET, by design, not a broken
# link) are DEPLOYMENT-SPECIFIC adjudications about one person's real store content — never
# committed here (P6: a prior revision hardcoded two real private topic slugs + a private HEAD
# name directly into this public test file). Supply your own locally via a comma-separated env
# var if your real store carries any: QQ_TEST_DOCUMENTED_DANGLERS=slug-one,slug-two. Verify
# freshness before trusting a locally-kept list (feedback: knowledge-freshness) — a "to-write"
# marker may since have been written and should be retired from the list once it has.
DOCUMENTED_DANGLERS = {s.strip() for s in
                       os.environ.get("QQ_TEST_DOCUMENTED_DANGLERS", "").split(",") if s.strip()}
# link-syntax / doc-example tokens that appear literally in prose about the [[link]] syntax
# itself, not as real references (mirrors run_check's STOP list).
STOP_TOKENS = {"links", "link", "slug", "name", "their-name", "topic", "foo-bar", "bar"}

LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


class TestNormalize(unittest.TestCase):
    def test_lowercases_and_underscores_to_hyphens(self):
        self.assertEqual(normalize("Feedback_Device_Appeal"), "feedback-device-appeal")

    def test_trims_whitespace(self):
        self.assertEqual(normalize("  foo-bar  "), "foo-bar")

    def test_strip_type_prefix(self):
        # P6: synthetic examples — a prior revision's examples happened to be real private
        # topic slugs lifted verbatim from the author's live store (not obviously sensitive on
        # their face, but real content nonetheless; same class of leak as the DOCUMENTED_
        # DANGLERS/REAL_MEMDIR fixes elsewhere in this file).
        self.assertEqual(strip_type_prefix("feedback-widget-color-scheme"), "widget-color-scheme")
        self.assertEqual(strip_type_prefix("project-example-app"), "example-app")
        self.assertEqual(strip_type_prefix("no-prefix-here"), "no-prefix-here")
        self.assertEqual(strip_type_prefix("session-state-build-pipeline"), "build-pipeline")


class TestResolverSynthetic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.qdir = os.path.join(base, "store")
        self.memdir = os.path.join(base, "mem")
        self.kbroot = os.path.join(base, "kb")
        os.makedirs(self.qdir)
        os.makedirs(self.memdir)
        os.makedirs(os.path.join(self.kbroot, "docs"))
        with open(os.path.join(self.qdir, "alpha.md"), "w") as f:
            f.write("# Quintessence — alpha\n> updated: x\n> essence: e\n")
        with open(os.path.join(self.memdir, "feedback_device_thing.md"), "w") as f:
            f.write("---\nname: device-thing-fact\ndescription: d\ntype: feedback\n---\nbody\n")
        with open(os.path.join(self.kbroot, "docs", "some-design-doc.md"), "w") as f:
            f.write("# a design doc\n")
        cfg = Config(env={}, config_file="/nonexistent",
                     overrides={"QUINTESSENCE_DIR": self.qdir, "QQ_MEMDIR": self.memdir,
                                "QQ_KB_ROOT": self.kbroot})
        self.store = Store(cfg)
        self.sr = SlugResolver(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolves_head_by_filename(self):
        m = self.sr.resolve("alpha")
        self.assertEqual((m.source, m.slug), ("head", "alpha"))

    def test_resolves_memory_by_filename(self):
        m = self.sr.resolve("feedback_device_thing")
        self.assertEqual((m.source, m.slug), ("memory", "feedback_device_thing"))

    def test_resolves_memory_by_name_field(self):
        m = self.sr.resolve("device-thing-fact")
        self.assertEqual((m.source, m.slug), ("memory", "feedback_device_thing"))

    def test_resolves_memory_by_prefix_stripped_filename(self):
        m = self.sr.resolve("device_thing")
        self.assertEqual((m.source, m.slug), ("memory", "feedback_device_thing"))

    def test_resolves_kb_doc(self):
        m = self.sr.resolve("some-design-doc")
        self.assertEqual((m.source, m.slug), ("kb-doc", "some-design-doc"))

    def test_unknown_token_is_unresolved(self):
        self.assertIsNone(self.sr.resolve("nothing-like-this-exists"))
        self.assertFalse(self.sr.is_known("nothing-like-this-exists"))

    def test_case_and_underscore_insensitive(self):
        self.assertTrue(self.sr.is_known("Feedback_Device_Thing"))
        self.assertTrue(self.sr.is_known("ALPHA"))


class TestKbDocGlobLocaleCollation(unittest.TestCase):
    def test_kb_doc_tie_break_matches_bash_glob_order_not_codepoint_order(self):
        """P4 fix, analogous to the P2 Store.list_head_slugs/list_memory_slugs bug (see
        test_store.py's identical-shaped test): SlugResolver's kb-doc scan
        (`sorted(self.kb_root.glob("*/*.md"))`) used plain codepoint sorted(), but run_check's
        own bash glob (`for f in "${QQ_KB_ROOT}"/*/*.md`) is LOCALE-collated. This only becomes
        externally observable via `resolve()`'s first-seen-wins tie-break: two kb-doc files
        that normalize to the SAME slug (a real, if rare, corpus situation) resolve to whichever
        one bash's glob would have reached first."""
        import locale
        try:
            locale.setlocale(locale.LC_COLLATE, "")
        except locale.Error:
            self.skipTest("no locale support on this host")
        names = ["self-hosted-claude-code", "selfhood-two-bootstraps"]
        if sorted(names, key=locale.strxfrm) == sorted(names):
            self.skipTest("this host's locale collates punctuation the same as codepoint "
                           "order — can't distinguish the fix from a plain sorted() here")
        with tempfile.TemporaryDirectory() as base:
            qdir = os.path.join(base, "store")
            memdir = os.path.join(base, "mem")
            kbroot = os.path.join(base, "kb")
            os.makedirs(qdir)
            os.makedirs(memdir)
            subdir = os.path.join(kbroot, "docs")
            os.makedirs(subdir)
            # Two DIFFERENT files whose slugs (post-normalize) collide, so first-seen-wins is
            # externally visible via resolve_all()'s candidate order.
            for n in names:
                with open(os.path.join(subdir, f"{n}.md"), "w") as f:
                    f.write(f"# {n}\n")
            cfg = Config(env={}, config_file="/nonexistent",
                         overrides={"QUINTESSENCE_DIR": qdir, "QQ_MEMDIR": memdir, "QQ_KB_ROOT": kbroot})
            sr = SlugResolver(Store(cfg))
            slugs_in_order = [m.slug for m in sr.resolve_all("selfhood-two-bootstraps", best_tier_only=False)] \
                + [m.slug for m in sr.resolve_all("self-hosted-claude-code", best_tier_only=False)]
            # Each name normalizes to itself (no underscores/uppercase), so each resolves cleanly
            # to its own file regardless of scan order — the REAL assertion is on scan order
            # itself, exercised directly below (matches test_store.py's own approach).
            self.assertEqual(slugs_in_order, ["selfhood-two-bootstraps", "self-hosted-claude-code"])
            seen_order = [p.stem for p in sorted(
                sr.kb_root.glob("*/*.md"),
                key=lambda p: _locale_sort_key(str(p.relative_to(sr.kb_root))))]
            self.assertEqual(seen_order, ["selfhood-two-bootstraps", "self-hosted-claude-code"])


class TestResolverDedupBySymlink(unittest.TestCase):
    def test_kb_symlink_mirror_of_a_head_is_not_a_false_collision(self):
        """The kb corpus root is commonly a symlink farm (kb/quintessence -> the real store);
        the SAME file reachable as both a HEAD and a kb-doc must be ONE candidate, not two."""
        tmp = tempfile.TemporaryDirectory()
        try:
            base = tmp.name
            qdir = os.path.join(base, "store")
            memdir = os.path.join(base, "mem")
            kbroot = os.path.join(base, "kb")
            os.makedirs(qdir)
            os.makedirs(memdir)
            os.makedirs(kbroot)
            with open(os.path.join(qdir, "alpha.md"), "w") as f:
                f.write("# Quintessence — alpha\n> updated: x\n> essence: e\n")
            os.symlink(qdir, os.path.join(kbroot, "quintessence"))
            cfg = Config(env={}, config_file="/nonexistent",
                         overrides={"QUINTESSENCE_DIR": qdir, "QQ_MEMDIR": memdir, "QQ_KB_ROOT": kbroot})
            sr = SlugResolver(Store(cfg))
            cands = sr.resolve_all("alpha")
            self.assertEqual(len(cands), 1, f"expected the symlinked mirror to dedup, got {cands}")
            self.assertEqual(sr.resolve("alpha").source, "head")
        finally:
            tmp.cleanup()


class TestPerTierNamespaceResolution(unittest.TestCase):
    """P4.5: the namespace design ruling (Thomas 2026-07-03, "global namespace is a pain").
    Live examples the ruling names verbatim: a HEAD `roadmap` colliding on basename with
    ~/docs/roadmap.md (store wins outright — "dissolved", not an ambiguity); ~/docs/backlog.md
    vs ~/infra/backlog.md (pure kb, two distinct sources — ambiguous); the qualified-path escape
    hatch for picking one of several same-named kb docs on purpose."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.qdir = os.path.join(base, "store")
        self.memdir = os.path.join(base, "mem")
        self.kbroot = os.path.join(base, "kb")
        os.makedirs(self.qdir)
        os.makedirs(self.memdir)
        os.makedirs(os.path.join(self.kbroot, "docs"))
        os.makedirs(os.path.join(self.kbroot, "infra"))
        cfg = Config(env={}, config_file="/nonexistent",
                     overrides={"QUINTESSENCE_DIR": self.qdir, "QQ_MEMDIR": self.memdir,
                                "QQ_KB_ROOT": self.kbroot})
        self.store = Store(cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _sr(self):
        return SlugResolver(self.store)

    def test_store_exact_hit_wins_outright_over_a_same_named_kb_doc(self):
        """The roadmap case: a HEAD's own exact filename is authoritative — the kb doc is not
        even a contest, let alone an ambiguity. This is what "dissolves" the case."""
        with open(os.path.join(self.qdir, "roadmap.md"), "w") as f:
            f.write("# Quintessence — roadmap\n> updated: x\n> essence: e\n")
        with open(os.path.join(self.kbroot, "docs", "roadmap.md"), "w") as f:
            f.write("# roadmap (canonical brief)\n")
        sr = self._sr()
        cands = sr.resolve_all("roadmap")
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].source, "head")
        m = sr.resolve("roadmap")
        self.assertIsNotNone(m)
        self.assertEqual(m.source, "head")

    def test_bare_basename_matching_multiple_kb_docs_is_ambiguous_not_a_silent_winner(self):
        """The backlog/readme case: no store item at all, two DISTINCT kb docs from different
        source repos share a basename. Not resolved to a first-seen winner; resolve() is None
        and resolve_all() surfaces both candidates for the caller to report."""
        with open(os.path.join(self.kbroot, "docs", "backlog.md"), "w") as f:
            f.write("# docs backlog\n")
        with open(os.path.join(self.kbroot, "infra", "backlog.md"), "w") as f:
            f.write("# infra backlog\n")
        sr = self._sr()
        self.assertIsNone(sr.resolve("backlog"))
        cands = sr.resolve_all("backlog")
        self.assertEqual(len(cands), 2)
        self.assertEqual({c.kb_source for c in cands}, {"docs", "infra"})
        # still "known" (a real candidate exists) — just not uniquely resolvable
        self.assertTrue(sr.is_known("backlog"))

    def test_bare_basename_matching_a_single_kb_doc_resolves(self):
        with open(os.path.join(self.kbroot, "docs", "some-design-doc.md"), "w") as f:
            f.write("# doc\n")
        sr = self._sr()
        m = sr.resolve("some-design-doc")
        self.assertEqual((m.source, m.slug), ("kb-doc", "some-design-doc"))

    def test_store_internal_tie_head_vs_memory_is_a_real_ambiguity(self):
        """Both a HEAD and a memory hit the SAME (exact-identity) best tier — a genuine store-
        internal collision, unaffected by whatever kb also happens to contain."""
        with open(os.path.join(self.qdir, "dupe.md"), "w") as f:
            f.write("# Quintessence — dupe\n> updated: x\n> essence: e\n")
        with open(os.path.join(self.memdir, "dupe.md"), "w") as f:
            f.write("---\nname: dupe\ndescription: d\ntype: reference\n---\nbody\n")
        sr = self._sr()
        self.assertIsNone(sr.resolve("dupe"))
        cands = sr.resolve_all("dupe")
        self.assertEqual(len(cands), 2)
        self.assertEqual({c.source for c in cands}, {"head", "memory"})

    def test_derived_store_guess_contested_by_an_independent_kb_doc_is_ambiguous(self):
        """Store's OWN best hit here is only the prefix-stripped DERIVED tier (no exact filename
        or name: field matches "widget"), so it isn't authoritative enough to silently beat an
        independent kb doc of the same bare basename."""
        with open(os.path.join(self.memdir, "feedback_widget.md"), "w") as f:
            f.write("---\nname: feedback-widget\ndescription: d\ntype: feedback\n---\nbody\n")
        with open(os.path.join(self.kbroot, "docs", "widget.md"), "w") as f:
            f.write("# widget doc\n")
        sr = self._sr()
        self.assertIsNone(sr.resolve("widget"))
        cands = sr.resolve_all("widget")
        self.assertEqual(len(cands), 2)
        self.assertEqual({c.source for c in cands}, {"memory", "kb-doc"})

    def test_derived_store_guess_alone_still_resolves_when_kb_has_nothing(self):
        """Unaffected by P4.5: a prefix-stripped memory reference with NO competing kb doc
        resolves exactly as before (the common case for feedback-/project-prefixed memories)."""
        with open(os.path.join(self.memdir, "feedback_widget.md"), "w") as f:
            f.write("---\nname: feedback-widget\ndescription: d\ntype: feedback\n---\nbody\n")
        sr = self._sr()
        m = sr.resolve("widget")
        self.assertEqual((m.source, m.slug), ("memory", "feedback_widget"))

    def test_qualified_path_resolves_a_specific_kb_doc_bypassing_ambiguity(self):
        with open(os.path.join(self.kbroot, "docs", "backlog.md"), "w") as f:
            f.write("# docs backlog\n")
        with open(os.path.join(self.kbroot, "infra", "backlog.md"), "w") as f:
            f.write("# infra backlog\n")
        sr = self._sr()
        m = sr.resolve("infra/backlog")
        self.assertIsNotNone(m)
        self.assertEqual((m.source, m.slug, m.kb_source), ("kb-doc", "backlog", "infra"))
        m2 = sr.resolve("docs/backlog.md")   # trailing .md tolerated
        self.assertEqual(m2.kb_source, "docs")

    def test_qualified_path_to_nonexistent_file_is_unresolved_not_a_fallback(self):
        with open(os.path.join(self.kbroot, "docs", "backlog.md"), "w") as f:
            f.write("# docs backlog\n")
        sr = self._sr()
        self.assertIsNone(sr.resolve("nosuchsource/backlog"))
        self.assertFalse(sr.is_known("nosuchsource/backlog"))


class TestRealStoreLinkResolution(unittest.TestCase):
    @unittest.skipUnless(os.path.isdir(REAL_QDIR) and os.path.isdir(REAL_MEMDIR),
                          "no real store/memory dir on this machine")
    def test_every_link_token_resolves_or_is_a_documented_dangler(self):
        cfg = Config(env={}, config_file="/nonexistent",
                     overrides={"QUINTESSENCE_DIR": REAL_QDIR, "QQ_MEMDIR": REAL_MEMDIR,
                                "QQ_KB_ROOT": REAL_KB_ROOT})
        sr = SlugResolver(Store(cfg))
        files = [f for f in glob.glob(os.path.join(REAL_QDIR, "*.md")) +
                 glob.glob(os.path.join(REAL_MEMDIR, "*.md"))
                 if os.path.basename(f) not in ("INDEX.md", "RUBRIC.md", "MEMORY.md")]
        unresolved = {}
        for f in files:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for m in LINK_RE.finditer(text):
                token = m.group(1)
                nt = normalize(token)
                if not nt or nt in STOP_TOKENS:
                    continue
                if not sr.is_known(token):
                    unresolved.setdefault(nt, f)
        genuinely_unresolved = {tok: f for tok, f in unresolved.items() if tok not in DOCUMENTED_DANGLERS}
        if genuinely_unresolved:
            detail = "\n".join(f"  {tok}  (first seen in {f})" for tok, f in genuinely_unresolved.items())
            self.fail(f"{len(genuinely_unresolved)} [[link]] token(s) resolve to nothing and "
                      f"are NOT on the documented dangler list:\n{detail}")


if __name__ == "__main__":
    unittest.main()
