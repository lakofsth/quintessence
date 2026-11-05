#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Certify INVARIANTS.md `the-shell-and-python-loaders-name-the-same-config-file`:

For every value of HOME, QQ_CONFIG, XDG_CONFIG_HOME and QQ_REDACT_FILE, every shell
function that builds a path of the config-file family names the same file the python
Config resolves for it, a leading ~ or ~/ expanded against HOME on both sides.

Directions pinned here, each derived from the sentence:
  1. `_qq_config_file` (qq-config.sh) vs `Config().config_path` over the full matrix of
     QQ_CONFIG x XDG_CONFIG_HOME values (unset, empty, absolute, "~", "~/sub").
  2. `_qq_redact_file` (qq-redact.sh) vs `Config().get_path("QQ_REDACT_FILE")` over the
     full matrix of QQ_REDACT_FILE x XDG_CONFIG_HOME values.
  3. The tilde clause named by the sentence: with HOME set, a "~" or "~/x" value names
     exactly $HOME and $HOME/x on BOTH sides (not merely "the same wrong file").
  4. The same disagreement behaviourally: with a real default redact-slugs file and an
     EMPTY QQ_REDACT_FILE, bash and python must agree on whether the withheld list
     exists at all.

The shell builders are the two the sentence's "every shell function" covers at this
revision: every non-comment line of a tracked shell file outside tests/ that expands
QQ_CONFIG, XDG_CONFIG_HOME or QQ_REDACT_FILE sits in `_qq_config_file` (qq-config.sh)
or `_qq_redact_file` (qq-redact.sh).

Directions NOT pinned, and why: `~user` forms (the invariant's register row declares
them out of scope — config.py is itself split on them); HOME unset (python falls back
to the passwd entry by documented design, which is not a value of HOME to expand
against); HOME empty (python's stdlib-root fallback under an empty HOME is the same
documented design — see the F5 note in quintessence/config.py `_home`).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from quintessence.config import Config  # noqa: E402

UNSET = object()

CFG_VALUES = [UNSET, "", "/opt/qq/cfg", "~", "~/sub/cfg"]
XDG_VALUES = [UNSET, "", "/opt/qq/xdg", "~", "~/xdg"]
RED_VALUES = [UNSET, "", "/opt/qq/slugs", "~", "~/sub/slugs"]


def _env_of(home, pairs):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home}
    for key, val in pairs:
        if val is not UNSET:
            env[key] = val
    return env


def shell_path(src, func, home, pairs):
    r = subprocess.run(
        ["bash", "-c", f'. "{ROOT}/{src}" >/dev/null 2>&1; {func}'],
        env=_env_of(home, pairs), capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(f"sourcing {src} and calling {func} exited {r.returncode}: {r.stderr}")
    return r.stdout


def label(pairs):
    return ", ".join(f"{k}={'<unset>' if v is UNSET else repr(v)}" for k, v in pairs)


class ConfigFamilyParity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="qq-cert-parity-")
        cls.home = os.path.join(cls.tmp, "home")
        os.makedirs(cls.home)
        cls.home_spaced = os.path.join(cls.tmp, "the home")
        os.makedirs(cls.home_spaced)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _check_pair(self, src, func, py_get, home, pairs):
        env = _env_of(home, pairs)
        env.pop("PATH")
        want = py_get(Config(env=env))
        got = shell_path(src, func, home, pairs)
        self.assertEqual(
            want, got,
            f"{func} and Config disagree [{label(pairs)}] (HOME={home!r})")

    def test_config_file_matrix(self):
        for home in (self.home, self.home_spaced):
            for cval in CFG_VALUES:
                for xval in XDG_VALUES:
                    with self.subTest(QQ_CONFIG=cval, XDG_CONFIG_HOME=xval, home=home):
                        self._check_pair(
                            "qq-config.sh", "_qq_config_file",
                            lambda cfg: cfg.config_path, home,
                            [("QQ_CONFIG", cval), ("XDG_CONFIG_HOME", xval)])

    def test_redact_file_matrix(self):
        for home in (self.home, self.home_spaced):
            for rval in RED_VALUES:
                for xval in XDG_VALUES:
                    with self.subTest(QQ_REDACT_FILE=rval, XDG_CONFIG_HOME=xval, home=home):
                        self._check_pair(
                            "qq-redact.sh", "_qq_redact_file",
                            lambda cfg: cfg.get_path("QQ_REDACT_FILE"), home,
                            [("QQ_REDACT_FILE", rval), ("XDG_CONFIG_HOME", xval)])

    def test_leading_tilde_names_home_on_both_sides(self):
        tilde_cases = [
            ("qq-config.sh", "_qq_config_file", [("QQ_CONFIG", "~")], self.home),
            ("qq-config.sh", "_qq_config_file", [("QQ_CONFIG", "~/qq.conf")], self.home + "/qq.conf"),
            ("qq-config.sh", "_qq_config_file",
             [("QQ_CONFIG", UNSET), ("XDG_CONFIG_HOME", "~/xdg")], self.home + "/xdg/quintessence/config"),
            ("qq-redact.sh", "_qq_redact_file", [("QQ_REDACT_FILE", "~/slugs")], self.home + "/slugs"),
        ]
        for src, func, pairs, want in tilde_cases:
            with self.subTest(func=func, pairs=label(pairs)):
                env = _env_of(self.home, pairs)
                env.pop("PATH")
                if func == "_qq_config_file":
                    py = Config(env=env).config_path
                else:
                    py = Config(env=env).get_path("QQ_REDACT_FILE")
                self.assertEqual(want, py, f"python side [{label(pairs)}]")
                self.assertEqual(want, shell_path(src, func, self.home, pairs),
                                 f"shell side [{label(pairs)}]")

    def test_empty_redact_file_agrees_on_whether_the_list_exists(self):
        home = os.path.join(self.tmp, "redhome")
        qdir = os.path.join(home, ".config", "quintessence")
        os.makedirs(qdir)
        with open(os.path.join(qdir, "redact-slugs"), "w") as fh:
            fh.write("secret-topic\n")
        pairs = [("QQ_REDACT_FILE", ""), ("XDG_CONFIG_HOME", UNSET)]
        r = subprocess.run(
            ["bash", "-c", f'. "{ROOT}/qq-redact.sh" >/dev/null 2>&1; '
                           'if qq_redact_active; then echo ACTIVE; else echo OFF; fi'],
            env=_env_of(home, pairs), capture_output=True, text=True, timeout=30)
        shell_state = r.stdout.strip()
        py_path = Config(env=_env_of(home, pairs)).get_path("QQ_REDACT_FILE")
        py_state = "ACTIVE" if os.path.isfile(py_path) else "OFF"
        self.assertEqual(
            py_state, shell_state,
            "with QQ_REDACT_FILE set but EMPTY and a real default list on disk, bash and "
            f"python disagree on whether withholding is on (bash={shell_state}, "
            f"python names {py_path!r})")


if __name__ == "__main__":
    unittest.main()
