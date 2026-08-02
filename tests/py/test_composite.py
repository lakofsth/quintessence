# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Multi-store composition tests: the Store `qdir` override, CompositeStore fetch/list
semantics, single-store delegation identity, and the cli render verbs over a composite.

Degenerate identity is covered by the WHOLE existing suite staying green (those tests never build
a project store, so they exercise the len==1 path the `qq` entry point takes). Here we build a
genuine 2-store search path (project shadows user) and pin the composed behaviour.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence import cli
from quintessence.checks import Checks
from quintessence.config import Config
from quintessence.slugs import SlugResolver
from quintessence.store import Store, CompositeStore


def _cfg():
    return Config(env={}, config_file="/nonexistent", overrides={})


def _mk_store(tmp, name, heads):
    """A store dir under tmp/<name> with the given {slug: text} HEADs; returns a Store over it."""
    qdir = os.path.join(tmp, name)
    os.makedirs(qdir)
    for slug, text in heads.items():
        with open(os.path.join(qdir, f"{slug}.md"), "w") as f:
            f.write(text)
    return Store(_cfg(), qdir=qdir), qdir


class TestStoreQdirOverride(unittest.TestCase):
    def test_override_repoints_qdir_journal_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg()
            proj = os.path.join(tmp, "proj", ".quintessence")
            os.makedirs(proj)
            s = Store(cfg, qdir=proj)
            self.assertEqual(str(s.qdir), proj)
            self.assertEqual(str(s.journal_dir), os.path.join(proj, "journal"))
            self.assertEqual(str(s.index_path), os.path.join(proj, "INDEX.md"))

    def test_override_leaves_state_and_memdir_config_resolved(self):
        # state_dir + memdir are NOT layered in P2 — they stay Config-resolved (user-global).
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg()
            proj = os.path.join(tmp, "p", ".quintessence")
            os.makedirs(proj)
            s = Store(cfg, qdir=proj)
            self.assertEqual(str(s.state_dir), cfg.get_path("QQ_STATE_DIR"))
            self.assertEqual(str(s.memdir), cfg.get_path("QQ_MEMDIR"))

    def test_no_override_is_config_default(self):
        cfg = _cfg()
        s = Store(cfg)
        self.assertEqual(str(s.qdir), cfg.get_path("QUINTESSENCE_DIR"))


class TestCompositeFetch(unittest.TestCase):
    def test_project_shadows_user_on_slug_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, _ = _mk_store(tmp, "proj", {"shared": "PROJECT version\n", "ponly": "p\n"})
            user, _ = _mk_store(tmp, "user", {"shared": "USER version\n", "uonly": "u\n"})
            c = CompositeStore([proj, user])
            self.assertEqual(c.read_head("shared"), "PROJECT version\n")   # project wins
            self.assertEqual(c.read_head("ponly"), "p\n")
            self.assertEqual(c.read_head("uonly"), "u\n")                  # falls through to user
            self.assertTrue(c.has_head("uonly"))
            self.assertFalse(c.has_head("nowhere"))

    def test_head_path_points_at_owning_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, pdir = _mk_store(tmp, "proj", {"ponly": "p\n"})
            user, udir = _mk_store(tmp, "user", {"uonly": "u\n"})
            c = CompositeStore([proj, user])
            self.assertEqual(str(c.head_path("ponly")), os.path.join(pdir, "ponly.md"))
            self.assertEqual(str(c.head_path("uonly")), os.path.join(udir, "uonly.md"))

    def test_missing_slug_read_raises_like_bare_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, _ = _mk_store(tmp, "proj", {})
            user, _ = _mk_store(tmp, "user", {})
            c = CompositeStore([proj, user])
            with self.assertRaises(FileNotFoundError):
                c.read_head("nope")


class TestCompositeList(unittest.TestCase):
    def test_union_project_first_deduped(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, _ = _mk_store(tmp, "proj", {"bbb": "x", "shared": "x"})
            user, _ = _mk_store(tmp, "user", {"aaa": "x", "shared": "x"})
            c = CompositeStore([proj, user])
            slugs = c.list_head_slugs()
            # project slugs (collated) first, then user slugs not already seen; 'shared' once.
            self.assertEqual(slugs, ["bbb", "shared", "aaa"])
            self.assertEqual(slugs.count("shared"), 1)


class TestSingleElementDelegationIdentity(unittest.TestCase):
    def test_len1_composite_matches_bare_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = _mk_store(tmp, "only", {"t1": "one\n", "t2": "two\n"})
            c = CompositeStore([store])
            self.assertEqual(c.list_head_slugs(), store.list_head_slugs())
            self.assertEqual(c.read_head("t1"), store.read_head("t1"))
            self.assertEqual(c.has_head("t2"), store.has_head("t2"))
            self.assertEqual(str(c.head_path("t1")), str(store.head_path("t1")))

    def test_getattr_delegates_to_most_specific(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, pdir = _mk_store(tmp, "proj", {})
            user, _ = _mk_store(tmp, "user", {})
            c = CompositeStore([proj, user])
            self.assertEqual(str(c.qdir), pdir)          # delegated to stores[0]
            self.assertIs(c.config, proj.config)
            self.assertEqual(str(c.index_path), os.path.join(pdir, "INDEX.md"))


class TestCliOverComposite(unittest.TestCase):
    def test_render_show_composes(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, _ = _mk_store(tmp, "proj", {"ponly": "PROJ BODY\n"})
            user, _ = _mk_store(tmp, "user", {"uonly": "USER BODY\n"})
            c = CompositeStore([proj, user])
            out = cli.render_show(c, ["ponly", "uonly", "missing"])
            self.assertIn("===== quintessence: ponly =====", out)
            self.assertIn("PROJ BODY", out)
            self.assertIn("===== quintessence: uonly =====", out)   # reached user store by name
            self.assertIn("USER BODY", out)
            self.assertIn("no HEAD for 'missing'", out)

    def test_render_list_unions(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, _ = _mk_store(tmp, "proj", {"bbb": "x"})
            user, _ = _mk_store(tmp, "user", {"aaa": "x"})
            c = CompositeStore([proj, user])
            self.assertEqual(cli.render_list(c), "bbb\naaa\n")

    def test_render_path_resolves_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, pdir = _mk_store(tmp, "proj", {"ponly": "x"})
            user, udir = _mk_store(tmp, "user", {"uonly": "x"})
            c = CompositeStore([proj, user])
            self.assertEqual(cli.render_path(c, "uonly"), f"{os.path.join(udir, 'uonly.md')}\n")


def _mk_mem_store(tmp, name, facts):
    """A store dir with a project-style memory subdir (<qdir>/memory) holding {slug: text}."""
    qdir = os.path.join(tmp, name)
    memdir = os.path.join(qdir, "memory")
    os.makedirs(memdir)
    for slug, text in facts.items():
        with open(os.path.join(memdir, f"{slug}.md"), "w") as f:
            f.write(text)
    return Store(_cfg(), qdir=qdir, memdir=memdir), memdir


class TestStoreMemdirOverride(unittest.TestCase):
    def test_memdir_override_repoints_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = os.path.join(tmp, "p", ".quintessence", "memory")
            os.makedirs(md)
            s = Store(_cfg(), qdir=os.path.join(tmp, "p", ".quintessence"), memdir=md)
            self.assertEqual(str(s.memdir), md)

    def test_no_memdir_override_is_config_default(self):
        cfg = _cfg()
        s = Store(cfg, qdir="/tmp/whatever")
        self.assertEqual(str(s.memdir), cfg.get_path("QQ_MEMDIR"))


class TestCompositeMemory(unittest.TestCase):
    def test_project_shadows_user_and_falls_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, pmd = _mk_mem_store(tmp, "proj", {"shared": "PROJ fact\n", "ponly": "p\n"})
            user, umd = _mk_mem_store(tmp, "user", {"shared": "USER fact\n", "uonly": "u\n"})
            c = CompositeStore([proj, user])
            self.assertEqual(c.read_memory("shared"), "PROJ fact\n")   # project wins
            self.assertEqual(c.read_memory("uonly"), "u\n")            # falls through to user
            self.assertEqual(str(c.memory_path("ponly")), os.path.join(pmd, "ponly.md"))
            self.assertEqual(str(c.memory_path("uonly")), os.path.join(umd, "uonly.md"))

    def test_list_memory_slugs_union_project_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, _ = _mk_mem_store(tmp, "proj", {"bbb": "x", "shared": "x"})
            user, _ = _mk_mem_store(tmp, "user", {"aaa": "x", "shared": "x"})
            c = CompositeStore([proj, user])
            self.assertEqual(c.list_memory_slugs(), ["bbb", "shared", "aaa"])

    def test_single_element_delegates(self):
        with tempfile.TemporaryDirectory() as tmp:
            only, _ = _mk_mem_store(tmp, "only", {"f1": "one\n"})
            c = CompositeStore([only])
            self.assertEqual(c.list_memory_slugs(), only.list_memory_slugs())
            self.assertEqual(c.read_memory("f1"), only.read_memory("f1"))


class TestCheckScopeAcrossComposite(unittest.TestCase):
    """`qq check`'s link resolver spans the composite so a project HEAD's link to a
    user-store topic resolves (not false-flagged), while a genuinely missing link still fires."""

    def _proj_and_user(self, tmp, proj_head_body):
        proj, _ = _mk_store(tmp, "proj",
                            {"projhead": f"# Quintessence — projhead\n{proj_head_body}\n"})
        user, _ = _mk_store(tmp, "user", {"usertopic": "# Quintessence — usertopic\n"})
        return proj, CompositeStore([proj, user])

    def test_project_link_to_user_topic_not_flagged_with_composite_resolver(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, composite = self._proj_and_user(tmp, "see [[usertopic]]")
            # project-only resolver: the cross-store link is a FALSE unresolved (the bug P5 fixes)
            proj_only = Checks(_cfg(), proj,
                               SlugResolver(proj, include_kb_docs=False)).link_findings()
            self.assertTrue(any(f.cls == "T1 link" for f in proj_only),
                            "project-only resolver should (wrongly) flag the cross-store link")
            # composite resolver: resolves against the whole path -> no unresolved finding
            composed = Checks(_cfg(), proj,
                              SlugResolver(composite, include_kb_docs=False)).link_findings()
            self.assertFalse(any(f.cls == "T1 link" for f in composed),
                             "composite resolver should resolve the user-store topic")

    def test_genuinely_missing_link_still_flagged_under_composite(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, composite = self._proj_and_user(tmp, "see [[nowhere-at-all]]")
            composed = Checks(_cfg(), proj,
                              SlugResolver(composite, include_kb_docs=False)).link_findings()
            self.assertTrue(any(f.cls == "T1 link" for f in composed),
                            "a link resolving in NEITHER store must still fire")


class TestRenderMemdir(unittest.TestCase):
    def test_prints_the_stores_memdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = os.path.join(tmp, "p", ".quintessence", "memory")
            os.makedirs(md)
            s = Store(_cfg(), qdir=os.path.join(tmp, "p", ".quintessence"), memdir=md)
            self.assertEqual(cli.render_memdir(s), f"{md}\n")


class TestCompositeShape(unittest.TestCase):
    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            CompositeStore([])


if __name__ == "__main__":
    unittest.main()
