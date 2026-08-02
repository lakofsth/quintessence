# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.storepath: the multi-store search-path resolver.

Pins the two frozen invariants and the discovery precedence:
  * degenerate case ($HOME, no project store, no explicit dir) == today's single user store;
  * explicit QUINTESSENCE_DIR (override/env/file) wins and skips discovery;
  * project store found via $CLAUDE_PROJECT_DIR or a git-style walk-up, most-specific first;
  * the user store is never itself mistaken for a project store.
Hermetic: every case synthesizes its own env/cwd/home over a tmp tree; no reliance on the live
process env or the real ~/quintessence.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence.config import Config
from quintessence.storepath import (
    PROJECT_STORE_DIRNAME,
    StorePath,
    StoreLocation,
    resolve_store_path,
)


class TestDegenerateCase(unittest.TestCase):
    """Invariant 2: no project store + no explicit dir => single user store, unchanged."""

    def test_home_run_is_single_user_store(self):
        # No project store on the walk-up, no explicit QUINTESSENCE_DIR => one 'user' store.
        with tempfile.TemporaryDirectory() as home:
            cfg = Config(env={}, config_file="/nonexistent", overrides={})
            sp = resolve_store_path(cwd=home, env={}, config=cfg, home=home)
            self.assertFalse(sp.explicit)
            self.assertEqual(len(sp.path), 1)
            self.assertEqual(sp.path[0].kind, "user")
            self.assertEqual(sp.write_target.kind, "user")

    def test_degenerate_user_qdir_is_the_config_default(self):
        cfg = Config(env={}, config_file="/nonexistent", overrides={})
        with tempfile.TemporaryDirectory() as home:
            sp = resolve_store_path(cwd=home, env={}, config=cfg, home=home)
            self.assertEqual(sp.qdirs, [cfg.get_path("QUINTESSENCE_DIR")])


class TestExplicitDirWins(unittest.TestCase):
    """Invariant 3: an explicit QUINTESSENCE_DIR (override/env/file) skips discovery."""

    def test_env_quintessence_dir_skips_project_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, "proj")
            os.makedirs(os.path.join(proj, PROJECT_STORE_DIRNAME))   # a project store DOES exist
            explicit = os.path.join(tmp, "explicit-store")
            os.makedirs(explicit)
            cfg = Config(env={"QUINTESSENCE_DIR": explicit}, config_file="/nonexistent", overrides={})
            sp = resolve_store_path(cwd=proj, env={"QUINTESSENCE_DIR": explicit}, config=cfg, home=tmp)
            self.assertTrue(sp.explicit)
            self.assertEqual(sp.qdirs, [explicit])
            self.assertEqual(sp.path[0].kind, "explicit")

    def test_override_quintessence_dir_skips_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, "proj")
            os.makedirs(os.path.join(proj, PROJECT_STORE_DIRNAME))
            explicit = os.path.join(tmp, "ov-store")
            cfg = Config(env={}, config_file="/nonexistent", overrides={"QUINTESSENCE_DIR": explicit})
            sp = resolve_store_path(cwd=proj, env={}, config=cfg, home=tmp)
            self.assertTrue(sp.explicit)
            self.assertEqual(sp.qdirs, [explicit])

    def test_file_recorded_quintessence_dir_does_NOT_pin(self):
        """The value `qq init` writes to the config FILE only locates the user store — it must
        not disable project discovery (the mainstream-user footgun). Discovery still runs and a
        project store prepends the file-located user store."""
        with tempfile.TemporaryDirectory() as tmp:
            user_qdir = os.path.join(tmp, "userstore")
            os.makedirs(user_qdir)
            cfgfile = os.path.join(tmp, "config")
            with open(cfgfile, "w") as f:
                f.write(f"QUINTESSENCE_DIR={user_qdir}\n")
            proj = os.path.join(tmp, "proj")
            pstore = os.path.join(proj, PROJECT_STORE_DIRNAME)
            os.makedirs(pstore)
            cfg = Config(env={}, config_file=cfgfile, overrides={})
            sp = resolve_store_path(cwd=proj, env={}, config=cfg, home=tmp)
            self.assertFalse(sp.explicit)               # NOT pinned
            self.assertEqual(len(sp.path), 2)
            self.assertEqual(sp.path[0].kind, "project")
            self.assertEqual(os.path.realpath(sp.path[0].qdir), os.path.realpath(pstore))
            self.assertEqual(sp.path[1].kind, "user")
            self.assertEqual(sp.path[1].qdir, user_qdir)   # user store at the file-recorded path


class TestProjectDiscovery(unittest.TestCase):
    def _cfg_user(self, home):
        """Config whose QUINTESSENCE_DIR default is ~/quintessence under the process HOME. We pass
        `home` separately to resolve_store_path; the user qdir comes from the config default, so we
        assert on that value rather than hardcoding a path."""
        return Config(env={}, config_file="/nonexistent", overrides={})

    def test_walk_up_finds_project_store(self):
        with tempfile.TemporaryDirectory() as home:
            proj = os.path.join(home, "work", "repo")
            deep = os.path.join(proj, "src", "pkg")
            os.makedirs(deep)
            pstore = os.path.join(proj, PROJECT_STORE_DIRNAME)
            os.makedirs(pstore)
            cfg = self._cfg_user(home)
            sp = resolve_store_path(cwd=deep, env={}, config=cfg, home=home)
            self.assertFalse(sp.explicit)
            self.assertEqual(len(sp.path), 2)
            self.assertEqual(sp.path[0].kind, "project")
            self.assertEqual(os.path.realpath(sp.path[0].qdir), os.path.realpath(pstore))
            self.assertEqual(sp.path[1].kind, "user")
            self.assertEqual(sp.write_target.kind, "project")

    def test_claude_project_dir_takes_precedence_over_walkup(self):
        with tempfile.TemporaryDirectory() as home:
            # a walk-up store AND a CLAUDE_PROJECT_DIR store both exist; CPD wins.
            walk_proj = os.path.join(home, "a")
            os.makedirs(os.path.join(walk_proj, PROJECT_STORE_DIRNAME))
            cpd = os.path.join(home, "b")
            cpd_store = os.path.join(cpd, PROJECT_STORE_DIRNAME)
            os.makedirs(cpd_store)
            cwd = os.path.join(walk_proj, "deep")
            os.makedirs(cwd)
            cfg = self._cfg_user(home)
            env = {"CLAUDE_PROJECT_DIR": cpd}
            sp = resolve_store_path(cwd=cwd, env=env, config=cfg, home=home)
            self.assertEqual(os.path.realpath(sp.path[0].qdir), os.path.realpath(cpd_store))

    def test_claude_project_dir_without_store_falls_back_to_walkup(self):
        with tempfile.TemporaryDirectory() as home:
            walk_proj = os.path.join(home, "a")
            wstore = os.path.join(walk_proj, PROJECT_STORE_DIRNAME)
            os.makedirs(wstore)
            cpd = os.path.join(home, "b")            # set but has NO .quintessence
            os.makedirs(cpd)
            cwd = os.path.join(walk_proj, "deep")
            os.makedirs(cwd)
            cfg = self._cfg_user(home)
            sp = resolve_store_path(cwd=cwd, env={"CLAUDE_PROJECT_DIR": cpd}, config=cfg, home=home)
            self.assertEqual(os.path.realpath(sp.path[0].qdir), os.path.realpath(wstore))

    def test_walk_up_stops_at_home(self):
        # a .quintessence ABOVE home must not be picked up (walk stops at home).
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, PROJECT_STORE_DIRNAME))   # above home
            home = os.path.join(root, "home", "thomas")
            cwd = os.path.join(home, "proj")
            os.makedirs(cwd)
            cfg = Config(env={}, config_file="/nonexistent", overrides={})
            sp = resolve_store_path(cwd=cwd, env={}, config=cfg, home=home)
            self.assertEqual(len(sp.path), 1)
            self.assertEqual(sp.path[0].kind, "user")

    def test_home_level_store_is_never_a_project_store(self):
        """The home level IS the user layer: a stray $HOME/.quintessence must be inert — never
        discovered by the walk-up from anywhere under home (else it would shadow every
        $HOME-rooted invocation and break the degenerate case)."""
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, PROJECT_STORE_DIRNAME))   # stray store AT home
            cwd = os.path.join(home, "work", "deep")
            os.makedirs(cwd)
            cfg = Config(env={}, config_file="/nonexistent", overrides={})
            for probe_cwd in (cwd, home):
                sp = resolve_store_path(cwd=probe_cwd, env={}, config=cfg, home=home)
                self.assertEqual(len(sp.path), 1)
                self.assertEqual(sp.path[0].kind, "user")

    def test_claude_project_dir_at_home_is_excluded_too(self):
        """Claude Code launched AT $HOME sets CLAUDE_PROJECT_DIR=$HOME in hooks — the home
        exclusion must hold on that branch as well, not just the walk-up."""
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, PROJECT_STORE_DIRNAME))
            cfg = Config(env={}, config_file="/nonexistent", overrides={})
            sp = resolve_store_path(cwd=home, env={"CLAUDE_PROJECT_DIR": home},
                                    config=cfg, home=home)
            self.assertEqual(len(sp.path), 1)
            self.assertEqual(sp.path[0].kind, "user")


class TestUserStoreNeverMistakenForProject(unittest.TestCase):
    def test_project_store_resolving_to_user_store_is_excluded(self):
        """A discovered `.quintessence/` whose realpath IS the user store must not be reported as
        a project store — it collapses to the single user store (the guard + dedup). Forced here
        with a symlink so the walk-up finds a candidate that resolves to the user qdir."""
        with tempfile.TemporaryDirectory() as home:
            # env={"HOME": home}: the user-store default resolves under THIS scratch home
            # (2026-07-29 review, engine-002 — this test used to makedirs the REAL ~/quintessence).
            cfg = Config(env={"HOME": home}, config_file="/nonexistent", overrides={})
            user_default = cfg.get_path("QUINTESSENCE_DIR")
            self.assertTrue(user_default.startswith(home + os.sep))
            os.makedirs(user_default, exist_ok=True)
            proj = os.path.join(home, "proj")
            os.makedirs(proj)
            os.symlink(user_default, os.path.join(proj, PROJECT_STORE_DIRNAME))
            sp = resolve_store_path(cwd=proj, env={}, config=cfg, home=home)
            self.assertEqual(sp.qdirs, [user_default])
            self.assertEqual(sp.path[0].kind, "user")


class TestStorePathShape(unittest.TestCase):
    def test_empty_path_rejected(self):
        with self.assertRaises(ValueError):
            StorePath([])

    def test_project_memdir_convention(self):
        loc = StoreLocation("/x/proj/.quintessence", "project")
        self.assertEqual(loc.memdir, "/x/proj/.quintessence/memory")


if __name__ == "__main__":
    unittest.main()
