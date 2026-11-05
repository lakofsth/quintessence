# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Config-file PATH parity: the bash loaders and the python Config name the same file.

INVARIANTS.md: `the-shell-and-python-loaders-name-the-same-config-file`.

test_config_parity.py pins how the two engines PARSE a config file; this pins WHICH file each
one opens. They used to disagree whenever QQ_CONFIG or XDG_CONFIG_HOME arrived with a literal
leading `~` (a settings.json "env" value, a systemd Environment= line — neither expands it):
config.py expanded it, qq-config.sh did not, so `qq config set` wrote $HOME/qq.conf and said so,
while every bash hook looked for ./~/qq.conf under its cwd and read no file at all (fix-review
qq-idle-autosave-2026-10 second leg, probe row R5: the "disabled" waiter still exited 2).

Sites are DERIVED, not hand-listed: every shell function outside tests/ that expands one of the
path variables of the config-file family. A new such function fails
test_every_shell_site_is_covered until it is added to SHELL_SITES and so to the parity matrix.
Not covered, on purpose: `~user` forms. config.py is itself split on them (_bootstrap_config_path
keeps them literal, _expanduser hands them to the stdlib), so there is no one python answer to
match; the bash side leaves them literal.
"""
import itertools
import os
import re
import subprocess
import sys
import tempfile
import unittest

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ENGINE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _treefiles  # noqa: E402

from quintessence.config import Config  # noqa: E402

FAMILY_VARS = ("QQ_CONFIG", "XDG_CONFIG_HOME", "QQ_REDACT_FILE")
# (file, function) -> the python accessor whose answer it must equal.
SHELL_SITES = {
    ("qq-config.sh", "_qq_config_file"): lambda cfg: cfg.config_path,
    ("qq-redact.sh", "_qq_redact_file"): lambda cfg: cfg.get_path("QQ_REDACT_FILE"),
}

_EXPANSION = re.compile(r"\$\{?(%s)\b" % "|".join(FAMILY_VARS))
_FUNC = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{")


def _shell_files():
    # git ls-files in a checkout, a .gitignore-filtered walk in a plain copy (_treefiles.py): the
    # suite runs from an export or a tarball too.
    for rel in _treefiles.engine_files(ENGINE):
        if not rel or rel.startswith("tests/"):
            continue
        path = os.path.join(ENGINE, rel)
        if not os.path.isfile(path):
            continue
        if rel.endswith(".sh"):
            yield rel
            continue
        with open(path, "rb") as f:
            first = f.readline(128)
        if re.match(rb"#!.*\b(ba)?sh\b", first):
            yield rel


def derived_shell_sites():
    """{(file, enclosing function or '<top level>')} for every non-comment line of a shell file
    outside tests/ that expands a config-file-family path variable."""
    sites = set()
    for rel in _shell_files():
        with open(os.path.join(ENGINE, rel), encoding="utf-8", errors="replace") as f:
            func = "<top level>"
            for line in f:
                m = _FUNC.match(line)
                if m:
                    func = m.group(1)
                elif line.startswith("}"):
                    func = "<top level>"
                code = line.split("#", 1)[0] if line.lstrip().startswith("#") else line
                if _EXPANSION.search(code):
                    sites.add((rel, func))
    return sites


UNSET = None


def _bash_paths(env):
    script = (f'. "{ENGINE}/qq-config.sh"; . "{ENGINE}/qq-redact.sh"; '
              + "; ".join(f'printf "%s\\x01" "$({fn})"' for (_f, fn) in SHELL_SITES))
    run_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    run_env.update({k: v for k, v in env.items() if v is not UNSET})
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10,
                         env=run_env, cwd=env["HOME"])
    return dict(zip(SHELL_SITES, out.stdout.split("\x01")))


def _python_paths(env):
    cfg = Config(env={k: v for k, v in env.items() if v is not UNSET})
    return {site: os.path.normpath(acc(cfg)) for site, acc in SHELL_SITES.items()}


class ConfigPathParity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp])

    def test_every_shell_site_is_covered(self):
        self.assertEqual(derived_shell_sites(), set(SHELL_SITES))

    def test_every_site_names_the_python_file(self):
        homes = [self.tmp, self.tmp + "/"]
        qq_configs = [UNSET, "", "/abs/qq.conf", "rel/qq.conf", "~/qq.conf", "~", "~/", "~/a/b.conf"]
        xdgs = [UNSET, "", "/xdg", "~/xdg", "~", "~/"]
        # Every family variable is walked with "" too: an empty value is a value, and which file
        # (if any) it names is the python Config's answer, not bash's `:-` habit. QQ_CONFIG and
        # XDG_CONFIG_HOME read empty as unset on both sides; QQ_REDACT_FILE='' names NO file
        # ("Empty or absent means the filtering is off", CONFIG.md), so bash must say so too.
        redacts = [UNSET, "", "/abs/slugs", "~/slugs", "~"]
        n = 0
        for home, qc, xdg, rf in itertools.product(homes, qq_configs, xdgs, redacts):
            env = {"HOME": home, "QQ_CONFIG": qc, "XDG_CONFIG_HOME": xdg, "QQ_REDACT_FILE": rf}
            py = _python_paths(env)
            sh = _bash_paths(env)
            for site in SHELL_SITES:
                with self.subTest(site=site, env=env):
                    self.assertEqual(os.path.normpath(sh[site]), py[site])
                    n += 1
        self.assertGreater(n, 0)

    def test_every_family_variable_is_walked_empty(self):
        # The matrix above is the class check only if it asks every family variable its empty
        # value; derived from FAMILY_VARS so a variable added there is held to the same.
        src = open(os.path.abspath(__file__), encoding="utf-8").read()
        for var, name in (("QQ_CONFIG", "qq_configs"), ("XDG_CONFIG_HOME", "xdgs"),
                          ("QQ_REDACT_FILE", "redacts")):
            with self.subTest(var=var):
                m = re.search(r"^\s*%s = \[(.*)\]$" % name, src, re.M)
                self.assertIsNotNone(m)
                self.assertIn('""', m.group(1))
        self.assertEqual(set(FAMILY_VARS), {"QQ_CONFIG", "XDG_CONFIG_HOME", "QQ_REDACT_FILE"})

    def test_an_empty_qq_redact_file_switches_shell_withholding_off(self):
        # The user-level property: QQ_REDACT_FILE= switches the withheld-topics filtering off
        # for that run (CONFIG.md), even with a populated default list on disk. Python names no
        # file; the shell's auto-recall gates (qq_redact_active / qq_slug_redacted, read by
        # resume-match.sh and prederive-recall.sh) must withhold nothing either. Positive
        # control: with the variable UNSET the same default list IS active and the slug IS
        # withheld, so the empty case passing is not an instrument that can only say "off".
        d = os.path.join(self.tmp, ".config", "quintessence")
        os.makedirs(d)
        with open(os.path.join(d, "redact-slugs"), "w") as f:
            f.write("secret-topic\n")
        script = (f'. "{ENGINE}/qq-redact.sh"; '
                  'qq_redact_active && echo active || echo inactive; '
                  'qq_slug_redacted /store/secret-topic.md && echo withheld || echo shown')
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.tmp}
        for rf, want in ((UNSET, "active\nwithheld\n"), ("", "inactive\nshown\n")):
            with self.subTest(QQ_REDACT_FILE=rf):
                env = dict(base)
                if rf is not UNSET:
                    env["QQ_REDACT_FILE"] = rf
                out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                     timeout=10, env=env, cwd=self.tmp)
                self.assertEqual(out.stdout, want)
                py = Config(env={k: v for k, v in env.items() if k != "PATH"}).get_path("QQ_REDACT_FILE")
                self.assertEqual(bool(py), rf is UNSET)

    def test_a_tilde_qq_config_reaches_bash_as_it_reaches_python(self):
        # The user-level property: a key set in a file named by an UNEXPANDED ~/ path is the
        # value both engines resolve (positive control: the same file IS read via an absolute path).
        with open(os.path.join(self.tmp, "qq.conf"), "w") as f:
            f.write("QQ_IDLE_AUTOSAVE_DISABLE=1\n")
        for qc in ("~/qq.conf", os.path.join(self.tmp, "qq.conf")):
            with self.subTest(QQ_CONFIG=qc):
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.tmp, "QQ_CONFIG": qc}
                out = subprocess.run(["bash", "-c", f'. "{ENGINE}/qq-config.sh"; '
                                      'printf "%s" "${QQ_IDLE_AUTOSAVE_DISABLE-unset}"'],
                                     capture_output=True, text=True, timeout=10, env=env,
                                     cwd=self.tmp)
                self.assertEqual(out.stdout, "1")
                cfg = Config(env={"HOME": self.tmp, "QQ_CONFIG": qc})
                self.assertEqual(os.path.normpath(cfg.config_path),
                                 os.path.join(self.tmp, "qq.conf"))


if __name__ == "__main__":
    unittest.main()
