# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.config: KEYS registry sanity, resolution precedence
(overrides > env > file > default), the three bool-parsing flavors, and the D3
validate_set guard."""
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence.config import Config, KEYS, KEYS_BY_NAME, ValidationResult, _is_temp_path


class TestKeysRegistry(unittest.TestCase):
    def test_no_duplicate_names(self):
        names = [k.name for k in KEYS]
        self.assertEqual(len(names), len(set(names)), "duplicate KeyDef name(s) in KEYS")

    def test_every_key_has_scope_and_description(self):
        for k in KEYS:
            self.assertTrue(k.scope, f"{k.name} missing scope")
            self.assertTrue(k.description, f"{k.name} missing description")

    def test_alias_points_at_registered_canonical(self):
        for k in KEYS:
            if k.type == "alias":
                self.assertIn(k.alias_of, KEYS_BY_NAME, f"{k.name} aliases unregistered {k.alias_of}")

    def test_generic_defaults_no_deployment_specific_ports_no_ca_prefixed_canonical_names(self):
        # No deployment-specific ports baked into any default (the ports this
        # guards against are the ones a prior deployment's hardcoded ask-endpoint fallback
        # used); CA_ names may only appear as a documented DEPRECATED alias, never as a
        # first-class KeyDef default.
        retired_hardcoded_ports = ("8090", "11435")
        for k in KEYS:
            if k.type == "alias":
                continue
            default = k.default if not callable(k.default) else ""
            if isinstance(default, str):
                for port in retired_hardcoded_ports:
                    self.assertNotIn(port, default,
                                      f"{k.name} default names a deployment-specific port")
            self.assertFalse(k.name.startswith("CA_"), f"{k.name} is a first-class CA_-namespaced key")


class TestResolution(unittest.TestCase):
    def cfg(self, env=None, file_text=None, overrides=None):
        path = "/nonexistent/no-such-config"
        self._tmp = None
        if file_text is not None:
            fd, path = tempfile.mkstemp()
            with os.fdopen(fd, "w") as f:
                f.write(file_text)
            self._tmp = path
        return Config(env=env or {}, config_file=path, overrides=overrides)

    def tearDown(self):
        if getattr(self, "_tmp", None):
            os.unlink(self._tmp)

    def test_default_only(self):
        c = self.cfg()
        self.assertEqual(c.get("QUINTESSENCE_DIR"), os.path.expanduser("~/quintessence"))
        self.assertEqual(c.get("QQ_LOCK_WAIT"), 30)
        self.assertEqual(c.get("QQ_XREF_SIM"), 0.55)
        self.assertEqual(c.get("QQ_ASK_ENDPOINTS"), [])

    def test_file_overrides_default(self):
        c = self.cfg(file_text="QQ_LOCK_WAIT=99\n")
        self.assertEqual(c.get("QQ_LOCK_WAIT"), 99)

    def test_env_overrides_file(self):
        c = self.cfg(env={"QQ_LOCK_WAIT": "5"}, file_text="QQ_LOCK_WAIT=99\n")
        self.assertEqual(c.get("QQ_LOCK_WAIT"), 5)

    def test_ctor_overrides_win_over_env_and_file(self):
        c = self.cfg(env={"QQ_LOCK_WAIT": "5"}, file_text="QQ_LOCK_WAIT=99\n",
                      overrides={"QQ_LOCK_WAIT": "1"})
        self.assertEqual(c.get("QQ_LOCK_WAIT"), 1)

    def test_derived_default_follows_state_dir_override(self):
        c = self.cfg(overrides={"QQ_STATE_DIR": "/x/state"})
        self.assertEqual(c.get("QQ_XREF_CONTENT"), "/x/state/xref-content")
        self.assertEqual(c.get("QQ_XREF_WAVEOFFS"), "/x/state/xref-waveoffs")
        self.assertEqual(c.get("QQ_CONTRACT_EXTRA"), "/x/state/contract-extra.md")

    def test_csv_type(self):
        c = self.cfg(file_text="QQ_ASK_ENDPOINTS=http://a:1, http://b:2 ,\n")
        self.assertEqual(c.get("QQ_ASK_ENDPOINTS"), ["http://a:1", "http://b:2"])

    def test_unregistered_key_raises(self):
        c = self.cfg()
        with self.assertRaises(KeyError):
            c.get("QQ_NOT_A_REAL_KEY")

    def test_resolve_raw_works_for_unregistered_names(self):
        c = self.cfg(env={"XDG_STATE_HOME": "/custom/state"})
        self.assertEqual(c.get("QQ_STATE_DIR"), "/custom/state/quintessence")


class TestRegisteredDefaultsAreTyped(unittest.TestCase):
    """A registered default must arrive in the SAME shape as a configured value.

    2026-07-29 verification round, F1: Config.get's default branch handled only `path` and the
    bool flavors, so a default written as a STRING for any other coercing type came back raw —
    a csv default resolved to the bare string, and a caller iterating it got one item per
    CHARACTER. These pin the shape per type, from both directions (string default -> coerced;
    already-typed default -> untouched)."""

    def cfg(self, env=None):
        return Config(env=env or {}, config_file="/nonexistent/no-such-config")

    def test_csv_default_given_as_a_string_is_split(self):
        c = self.cfg()
        self.assertEqual(c.get("QQ_REMOTE_ALLOWED_ORIGINS"), ["https://claude.ai"])
        # the failure this guards: list() over the raw string would be a per-character array
        self.assertNotEqual(list(c.get("QQ_REMOTE_ALLOWED_ORIGINS")), list("https://claude.ai"))

    def test_empty_csv_default_given_as_a_string_is_an_empty_list(self):
        self.assertEqual(self.cfg().get("QQ_REMOTE_ALLOWED_HOSTS"), [])

    def test_csv_default_given_as_a_list_is_untouched(self):
        c = self.cfg()
        self.assertEqual(c.get("QQ_ASK_ENDPOINTS"), [])
        self.assertEqual(c.get("QQ_XREF_GENERIC_TYPES"), ["feedback"])
        self.assertEqual(c.get("QQ_BIND_EXCLUDE_ROOTS"), ["/run", "/proc", "/sys", "/tmp", "/dev"])

    def test_numeric_defaults_keep_their_type(self):
        c = self.cfg()
        self.assertIsInstance(c.get("QQ_LOCK_WAIT"), int)
        self.assertEqual(c.get("QQ_LOCK_WAIT"), 30)
        self.assertIsInstance(c.get("QQ_XREF_SIM"), float)
        self.assertEqual(c.get("QQ_XREF_SIM"), 0.55)
        # an int key whose default is None means "unset" and must stay None, not become 0
        self.assertIsNone(c.get("QQ_EMBED_NUM_GPU"))

    def test_bool_defaults_keep_their_flavor(self):
        c = self.cfg()
        self.assertIs(c.get("QQ_ESSENCE_LAG"), True)         # default-true-falsy-zero
        self.assertIs(c.get("QQ_SESSIONSTART_DIGEST"), False)  # truthy-exact-one
        self.assertIs(c.get("QQ_XREF_DISABLE"), False)        # truthy-nonempty

    def test_path_default_still_expands_tilde(self):
        c = self.cfg(env={"HOME": "/home/example-user"})
        self.assertEqual(c.get("QUINTESSENCE_DIR"), "/home/example-user/quintessence")
        c2 = Config(env={"HOME": "/home/example-user", "XDG_CONFIG_HOME": "~/cfg"},
                    config_file="/nonexistent/no-such-config")
        self.assertEqual(c2.get("QQ_CONFIG"),
                          "/home/example-user/cfg/quintessence/config")

    def test_every_registered_default_matches_its_declared_type(self):
        # Sweep: no key may resolve to a shape its own type contradicts.
        c = self.cfg()
        for kd in KEYS:
            value = c.get(kd.name)
            if value is None:
                continue
            if kd.type in ("csv", "csvpath"):
                self.assertIsInstance(value, list, f"{kd.name} ({kd.type}) resolved to {value!r}")
            elif kd.type in ("path", "str"):
                self.assertIsInstance(value, str, f"{kd.name} ({kd.type}) resolved to {value!r}")
            elif kd.type == "int":
                self.assertIsInstance(value, int, f"{kd.name} (int) resolved to {value!r}")
            elif kd.type == "float":
                self.assertIsInstance(value, (int, float), f"{kd.name} (float) resolved to {value!r}")
            else:
                self.assertIsInstance(value, bool, f"{kd.name} (bool) resolved to {value!r}")


class TestXdgBaseEmptyMeansUnset(unittest.TestCase):
    """2026-07-29 verification round, F3: an XDG base that is present but EMPTY means unset —
    bash reads `${XDG_CONFIG_HOME:-$HOME/.config}`, so a python side that treated "" as a real
    value resolved the config-file family CWD-RELATIVE and the two engines looked for the same
    file in different places."""

    def cfg(self, env):
        return Config(env=env, config_file="/nonexistent/no-such-config")

    def test_empty_xdg_config_home_falls_back_to_home(self):
        c = self.cfg({"HOME": "/home/example-user", "XDG_CONFIG_HOME": ""})
        self.assertEqual(c.get("QQ_CONFIG"),
                          "/home/example-user/.config/quintessence/config")

    def test_empty_xdg_state_home_falls_back_to_home(self):
        c = self.cfg({"HOME": "/home/example-user", "XDG_STATE_HOME": ""})
        self.assertEqual(c.get("QQ_STATE_DIR"), "/home/example-user/.local/state/quintessence")

    def test_set_xdg_bases_still_win(self):
        c = self.cfg({"HOME": "/home/example-user", "XDG_CONFIG_HOME": "/cfg",
                      "XDG_STATE_HOME": "/st"})
        self.assertEqual(c.get("QQ_CONFIG"), "/cfg/quintessence/config")
        self.assertEqual(c.get("QQ_STATE_DIR"), "/st/quintessence")


class TestBoolFlavors(unittest.TestCase):
    def cfg(self, env):
        return Config(env=env, config_file="/nonexistent")

    def test_default_true_falsy_zero(self):
        self.assertTrue(self.cfg({}).get("QQ_ESSENCE_LAG"))
        self.assertTrue(self.cfg({"QQ_ESSENCE_LAG": "anything"}).get("QQ_ESSENCE_LAG"))
        self.assertFalse(self.cfg({"QQ_ESSENCE_LAG": "0"}).get("QQ_ESSENCE_LAG"))

    def test_truthy_exact_one(self):
        self.assertFalse(self.cfg({}).get("QQ_SESSIONSTART_DIGEST"))
        self.assertTrue(self.cfg({"QQ_SESSIONSTART_DIGEST": "1"}).get("QQ_SESSIONSTART_DIGEST"))
        self.assertFalse(self.cfg({"QQ_SESSIONSTART_DIGEST": "yes"}).get("QQ_SESSIONSTART_DIGEST"))

    def test_truthy_nonempty(self):
        self.assertFalse(self.cfg({}).get("QQ_XREF_DISABLE"))
        self.assertTrue(self.cfg({"QQ_XREF_DISABLE": "0"}).get("QQ_XREF_DISABLE"))  # ANY non-empty
        self.assertTrue(self.cfg({"QQ_XREF_DISABLE": "anything"}).get("QQ_XREF_DISABLE"))


class TestValidateSet(unittest.TestCase):
    def setUp(self):
        self.c = Config(env={}, config_file="/nonexistent")

    def test_unknown_key_warns_but_allows(self):
        r = self.c.validate_set("QQ_TOTALLY_MADE_UP", "x")
        self.assertTrue(r.ok)
        self.assertIn("unknown", r.message)

    def test_refuses_quintessence_dir_under_tmp(self):
        r = self.c.validate_set("QUINTESSENCE_DIR", "/tmp/scratch-store")
        self.assertFalse(r.ok)

    def test_refuses_qq_memdir_under_tmpdir_env(self):
        with unittest.mock.patch.dict(os.environ, {"TMPDIR": "/weird/tmproot"}):
            r = self.c.validate_set("QQ_MEMDIR", "/weird/tmproot/sub/mem")
            self.assertFalse(r.ok)

    def test_force_overrides_refusal(self):
        r = self.c.validate_set("QUINTESSENCE_DIR", "/tmp/scratch-store", force=True)
        self.assertTrue(r.ok)

    def test_normal_path_allowed(self):
        # P6 fix: a hardcoded non-temp path, not `os.path.expanduser("~/quintessence")` — the
        # old form was flaky whenever the RUNNING PROCESS's own $HOME happens to resolve under
        # a temp-directory-class root (e.g. a `mktemp -d`-based scratch $HOME used to verify a
        # fresh install, as the fresh-install check does): _is_temp_path()
        # then correctly (by its own definition) refuses it, which isn't what this test means
        # to exercise ("a normal path is allowed") — it was accidentally testing environment
        # plumbing instead of the guard's actual normal-path behaviour.
        r = self.c.validate_set("QUINTESSENCE_DIR", "/home/example-user/quintessence")
        self.assertTrue(r.ok)
        self.assertIsNone(r.message)


class TestWithOverrides(unittest.TestCase):
    """P8 recall composition: Config.with_overrides() derives a sibling Config sharing this
    instance's env/config-file layers, with new overrides layered on top at the SAME
    (highest) precedence any other constructor override has."""

    def test_shares_env_and_file_layers(self):
        with tempfile.TemporaryDirectory() as base:
            cfg_file = os.path.join(base, "config")
            with open(cfg_file, "w") as f:
                f.write("QQ_EMBED_MODEL=file-model\n")
            base_cfg = Config(env={"QQ_KEEP_ALIVE": "9h"}, config_file=cfg_file)
            derived = base_cfg.with_overrides(QQ_KB_ROOT="/tmp/derived-kb")
            self.assertEqual(derived.get_str("QQ_KEEP_ALIVE"), "9h")           # env layer carried
            self.assertEqual(derived.get_str("QQ_EMBED_MODEL"), "file-model")  # file layer carried
            self.assertEqual(derived.get_path("QQ_KB_ROOT"), "/tmp/derived-kb")

    def test_new_override_wins_over_an_existing_one_of_the_same_name(self):
        base_cfg = Config(env={}, config_file="/nonexistent", overrides={"QQ_KB_ROOT": "/a"})
        derived = base_cfg.with_overrides(QQ_KB_ROOT="/b")
        self.assertEqual(derived.get_path("QQ_KB_ROOT"), "/b")

    def test_preserves_other_existing_overrides_untouched(self):
        base_cfg = Config(env={}, config_file="/nonexistent",
                           overrides={"QQ_EMBED_MODEL": "kept-model"})
        derived = base_cfg.with_overrides(QQ_KB_ROOT="/new-kb")
        self.assertEqual(derived.get_str("QQ_EMBED_MODEL"), "kept-model")
        self.assertEqual(derived.get_path("QQ_KB_ROOT"), "/new-kb")

    def test_does_not_mutate_the_original_config(self):
        base_cfg = Config(env={}, config_file="/nonexistent", overrides={"QQ_KB_ROOT": "/a"})
        base_cfg.with_overrides(QQ_KB_ROOT="/b")
        self.assertEqual(base_cfg.get_path("QQ_KB_ROOT"), "/a")


if __name__ == "__main__":
    unittest.main()


class SyntheticHomeResolution20260729(unittest.TestCase):
    """Pin for the 2026-07-29 review's engine-002: every HOME-anchored default resolves through
    the Config INSTANCE's own env (overrides > env > file > process home) — a synthetic
    env={"HOME": ...} yields store paths under that home, never the real process home."""

    def test_home_anchored_defaults_follow_instance_env(self):
        c = Config(env={"HOME": "/tmp/qq-synthetic-home"}, config_file="/nonexistent")
        for key in ("QUINTESSENCE_DIR", "QQ_MEMDIR", "QQ_KB_ROOT", "QQ_CACHE",
                    "QQ_STATE_DIR", "QQ_DOCDIR"):
            self.assertTrue(c.get_path(key).startswith("/tmp/qq-synthetic-home/"),
                            f"{key} resolved outside the synthetic home: {c.get_path(key)}")

    def test_empty_env_still_falls_back_to_process_home(self):
        import os as _os
        c = Config(env={}, config_file="/nonexistent")
        self.assertEqual(c.get_path("QUINTESSENCE_DIR"),
                         _os.path.join(_os.path.expanduser("~"), "quintessence"))

    def test_configured_tilde_path_expands_under_the_instance_home(self):
        """2026-07-29 verification round, F3: a `~` path arriving from env/file went through
        os.path.expanduser (the PROCESS home) while the same instance's HOME-derived defaults
        resolved under its own home — one Config, two homes. Both must land in /synthetic."""
        c = Config(env={"HOME": "/synthetic", "QQ_MEMDIR": "~/mem"}, config_file="/nonexistent")
        self.assertEqual(c.get_path("QQ_MEMDIR"), "/synthetic/mem")
        self.assertEqual(c.get_path("QUINTESSENCE_DIR"), "/synthetic/quintessence")

    def test_tilde_path_from_an_override_also_uses_the_instance_home(self):
        c = Config(env={"HOME": "/synthetic"}, config_file="/nonexistent",
                   overrides={"QQ_KB_ROOT": "~/kb"})
        self.assertEqual(c.get_path("QQ_KB_ROOT"), "/synthetic/kb")

    def test_tilde_user_form_still_falls_through_to_the_stdlib(self):
        """`~someuser` is NOT the instance-home form; it keeps stdlib behaviour (unchanged, and
        left alone rather than silently reinterpreted)."""
        import os as _os
        c = Config(env={"HOME": "/synthetic", "QQ_MEMDIR": "~nosuchuser42/mem"},
                   config_file="/nonexistent")
        self.assertEqual(c.get_path("QQ_MEMDIR"),
                         _os.path.expanduser("~nosuchuser42/mem"))


class CsvPathTildeAndHomeShapes20260729(unittest.TestCase):
    """2026-07-29 verification round, F5. The round-2 tilde fix reached `path` but not the csv
    keys whose MEMBERS are paths, so QQ_BIND_EXCLUDE_ROOTS=~/a,~/b stayed literal — and
    quintessence.refs drops a non-absolute exclude root, so the exclusion read as protection and
    did nothing. Also pins the two degenerate homes a `~` can be joined to."""

    def test_csvpath_members_expand_under_the_instance_home(self):
        c = Config(env={"HOME": "/synthetic", "QQ_BIND_EXCLUDE_ROOTS": "~/a,~/b"},
                   config_file="/nonexistent")
        self.assertEqual(c.get("QQ_BIND_EXCLUDE_ROOTS"), ["/synthetic/a", "/synthetic/b"])

    def test_csvpath_mixes_absolute_and_tilde_members(self):
        c = Config(env={"HOME": "/synthetic", "QQ_BIND_EXCLUDE_ROOTS": "/run, ~/scratch ,/tmp"},
                   config_file="/nonexistent")
        self.assertEqual(c.get("QQ_BIND_EXCLUDE_ROOTS"),
                         ["/run", "/synthetic/scratch", "/tmp"])

    def test_csvpath_bare_tilde_member_is_the_home_itself(self):
        c = Config(env={"HOME": "/synthetic", "QQ_BIND_EXCLUDE_ROOTS": "~"},
                   config_file="/nonexistent")
        self.assertEqual(c.get("QQ_BIND_EXCLUDE_ROOTS"), ["/synthetic"])

    def test_csvpath_tilde_user_member_falls_through_to_the_stdlib(self):
        import os as _os
        c = Config(env={"HOME": "/synthetic",
                        "QQ_BIND_EXCLUDE_ROOTS": "~nosuchuser42/scratch"},
                   config_file="/nonexistent")
        self.assertEqual(c.get("QQ_BIND_EXCLUDE_ROOTS"),
                         [_os.path.expanduser("~nosuchuser42/scratch")])

    def test_plain_csv_keys_are_deliberately_left_literal(self):
        """The exclusion is a judgement, so it gets a test: a csv key whose members are NOT
        paths (slugs here; likewise URLs, hosts, unit names, `name=path` maps) is untouched."""
        c = Config(env={"HOME": "/synthetic", "QQ_AUTHOR_GATE_SLUGS": "~/a,b"},
                   config_file="/nonexistent")
        self.assertEqual(c.get("QQ_AUTHOR_GATE_SLUGS"), ["~/a", "b"])

    def test_trailing_slash_home_does_not_double_the_separator(self):
        c = Config(env={"HOME": "/synthetic/", "QQ_MEMDIR": "~/mem",
                        "QQ_BIND_EXCLUDE_ROOTS": "~/a"}, config_file="/nonexistent")
        self.assertEqual(c.get_path("QQ_MEMDIR"), "/synthetic/mem")
        self.assertEqual(c.get("QQ_BIND_EXCLUDE_ROOTS"), ["/synthetic/a"])

    def test_root_home_expands_to_root_not_a_doubled_slash(self):
        c = Config(env={"HOME": "/", "QQ_MEMDIR": "~/mem"}, config_file="/nonexistent")
        self.assertEqual(c.get_path("QQ_MEMDIR"), "/mem")
        c2 = Config(env={"HOME": "/", "QQ_MEMDIR": "~"}, config_file="/nonexistent")
        self.assertEqual(c2.get_path("QQ_MEMDIR"), "/")

    def test_empty_home_means_unset_not_the_filesystem_root(self):
        """HOME='' used to make `~/mem` resolve to `/mem` and a HOME-anchored default land
        CWD-relative — the same 'present but empty means unset' reading the XDG bases take."""
        import os as _os
        real = _os.path.expanduser("~")
        c = Config(env={"HOME": "", "QQ_MEMDIR": "~/mem"}, config_file="/nonexistent")
        self.assertEqual(c.get_path("QQ_MEMDIR"), _os.path.join(real, "mem"))
        self.assertEqual(c.get_path("QUINTESSENCE_DIR"), _os.path.join(real, "quintessence"))
        c2 = Config(env={"HOME": "", "QQ_BIND_EXCLUDE_ROOTS": "~/a"},
                    config_file="/nonexistent")
        self.assertEqual(c2.get("QQ_BIND_EXCLUDE_ROOTS"), [_os.path.join(real, "a")])
