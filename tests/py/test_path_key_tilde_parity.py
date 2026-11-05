# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Path-KEY parity: every bash site that reads a path-typed config key names the path the python
Config resolves for it, a leading `~` or `~/` expanded against HOME on both sides.

INVARIANTS.md: `every-shell-site-of-a-path-key-names-the-path-config-resolves`.

The sibling of `the-shell-and-python-loaders-name-the-same-config-file` (which pinned the three
config-FAMILY variables). Before this, bash read every other path key raw: with
`QUINTESSENCE_DIR=~/notes` in the config file (which `qq config set` writes and reports success
for) or in a settings.json `env` / systemd `Environment=` value (neither expands `~`), python used
$HOME/notes while qq-lib.sh's QDIR, memory-commit.sh's QQ_MEMDIR and the hooks' QQ_STATE_DIR named
./~/notes under the hook's cwd (fix-review qq-idle-autosave-2026-10, path-key-tilde-sibling-probe.sh).

The repair is ONE normalisation in qq-config.sh, which every such site sources before reading the
key; this file pins that from four sides, each derived rather than hand-listed:
  1. the key lists qq-config.sh normalises equal the KEYS registry's path/csvpath keys, plus the
     XDG bases config.py expands into a path default (`resolve_raw("XDG_…")`);
  2. every one of those keys, set to each value shape in the env layer AND the config-file layer,
     reaches bash as `Config.get` resolves it;
  3. every `${KEY:-…}` / `${KEY:=…}` expression at a derived shell site, evaluated by bash after
     qq-config.sh, equals `Config.get(KEY)`;
  4. every derived site file sources qq-config.sh (directly or via qq-lib.sh) before its first
     site, or is sourced only by files that do. setup.sh is held to the same rule: it once read
     the keys from the environment alone (its own `_untilde` loop was the exemption here), so a
     store moved with `qq config set` was named $HOME/quintessence by a re-run of the installer.
Not covered, on purpose: `~user` forms (python hands them to the stdlib, which expands them only
for a user that exists; bash leaves them literal), and the EMPTY value, on which the engines split
for a different reason (Config returns '' for an empty QUINTESSENCE_DIR; bash's `:-` takes the
default) — a defaulting question, not a tilde one.
"""
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

from quintessence.config import KEYS, Config  # noqa: E402

PATH_KEYS = sorted(k.name for k in KEYS if k.type == "path")
CSVPATH_KEYS = sorted(k.name for k in KEYS if k.type == "csvpath")
with open(os.path.join(ENGINE, "quintessence", "config.py"), encoding="utf-8") as _f:
    XDG_BASES = sorted(set(re.findall(r'resolve_raw\("(XDG_[A-Z_]+)"\)', _f.read())))
ALL_KEYS = PATH_KEYS + CSVPATH_KEYS

# Value shapes a path key can arrive in; `~user` and "" are excluded (module docstring).
PATH_VALUES = ["~", "~/", "~/x", "~/a/b", "/abs/x", "rel/x", "a~/b"]
CSV_VALUES = ["~/a", " ~/a , /b", "~,~/c", "/b", "x,~/y,,"]

# A presence test (`${KEY+x}`, `${KEY:+…}`) reads no value, so it is not a site: setup.sh asks
# `${QUINTESSENCE_DIR+x}` BEFORE it sources qq-config.sh, deliberately, to tell an explicit
# environment value from one the config file supplies.
_KEYALT = "|".join(ALL_KEYS + XDG_BASES)
_EXPANSION = re.compile(r"\$(?:\{(%s)\b(?!:?\+)|(%s)\b)" % (_KEYALT, _KEYALT))
_SOURCES_CONFIG = re.compile(r'^\s*(?:\.|source)\s+.*/(qq-config|qq-lib)\.sh"?(\s|$)')
_SOURCES = re.compile(r'^\s*(?:\.|source)\s+.*/([A-Za-z0-9_.-]+\.sh)"?(\s|$)')


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
            if re.match(rb"#!.*\b(ba)?sh\b", f.readline(128)):
                yield rel


def _lines(rel):
    with open(os.path.join(ENGINE, rel), encoding="utf-8", errors="replace") as f:
        return f.read().split("\n")


def _code(line):
    return "" if line.lstrip().startswith("#") else line


def derived_sites():
    """{file: [(lineno, line)]} for every non-comment line of a shell file outside tests/ that
    expands a path/csvpath KEY or an XDG base of a path default."""
    sites = {}
    for rel in _shell_files():
        for n, line in enumerate(_lines(rel), 1):
            if _EXPANSION.search(_code(line)):
                sites.setdefault(rel, []).append((n, line))
    return sites


def _param_expansions(line):
    """Every outermost `${KEY:-…}` / `${KEY:=…}` in LINE whose KEY is a path key, brace-balanced."""
    out = []
    for m in re.finditer(r"\$\{(%s):[-=]" % "|".join(ALL_KEYS), line):
        depth, i = 0, m.start() + 1
        while i < len(line):
            if line[i] == "{":
                depth += 1
            elif line[i] == "}":
                depth -= 1
                if depth == 0:
                    out.append((m.group(1), line[m.start():i + 1]))
                    break
            i += 1
    # keep only outermost: drop one that sits inside another found on the same line
    return [(k, e) for k, e in out if not any(e != o and e in o for _k, o in out)]


def _bash(script, env, cwd):
    run_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    run_env.update(env)
    r = subprocess.run(["bash", "-c", f'. "{ENGINE}/qq-config.sh"; ' + script],
                       capture_output=True, text=True, timeout=10, env=run_env, cwd=cwd)
    return r.stdout


def _norm_py(v):
    if isinstance(v, list):
        return [os.path.normpath(x) for x in v]
    return os.path.normpath(v)


def _norm_sh(key, v):
    if key in CSVPATH_KEYS:
        return [os.path.normpath(x.strip()) for x in v.split(",") if x.strip()]
    return os.path.normpath(v)


class PathKeyTildeParity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.cwd = os.path.join(self.tmp, "cwd")
        os.makedirs(self.home)
        os.makedirs(self.cwd)

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp])

    def test_the_normalised_key_lists_equal_the_registry(self):
        out = _bash('printf "P %s\\n" "${_QQ_PATH_KEYS[@]}"; printf "C %s\\n" "${_QQ_CSVPATH_KEYS[@]}"',
                    {"HOME": self.home}, self.cwd)
        got_p = sorted(l[2:] for l in out.split("\n") if l.startswith("P ") and l[2:])
        got_c = sorted(l[2:] for l in out.split("\n") if l.startswith("C ") and l[2:])
        self.assertTrue(PATH_KEYS and CSVPATH_KEYS and XDG_BASES)
        self.assertEqual(got_p, sorted(PATH_KEYS + XDG_BASES))
        self.assertEqual(got_c, CSVPATH_KEYS)

    def _one(self, key, value, layer, home):
        """(bash's answer, Config's answer) for KEY=VALUE arriving in LAYER, both normalised."""
        env = {"HOME": home}
        cfg_file = os.path.join(self.tmp, "qq.conf")
        if os.path.exists(cfg_file):
            os.unlink(cfg_file)
        if layer == "file":
            with open(cfg_file, "w") as f:
                f.write(f"{key}={value}\n")
        if key not in ("QQ_CONFIG", "XDG_CONFIG_HOME"):
            env["QQ_CONFIG"] = cfg_file
        if layer == "env":
            env[key] = value
        sh = _bash(f'printf "%s" "${{{key}-<unset>}}"', env, self.cwd)
        cfg = Config(env=dict(env))
        # an XDG base is asked through the default it feeds, with the key under it left unset
        if key == "XDG_STATE_HOME":
            return os.path.normpath(sh + "/quintessence"), _norm_py(cfg.get_path("QQ_STATE_DIR"))
        if key == "XDG_CONFIG_HOME":
            return os.path.normpath(sh + "/quintessence/config"), _norm_py(cfg.config_path)
        return _norm_sh(key, sh), _norm_py(cfg.get(key))

    def test_every_path_key_reaches_bash_as_config_resolves_it(self):
        n = 0
        for key in ALL_KEYS + XDG_BASES:
            values = CSV_VALUES if key in CSVPATH_KEYS else PATH_VALUES
            # QQ_CONFIG and the XDG bases locate the file, so they have no file layer
            layers = ["env"] if key == "QQ_CONFIG" or key in XDG_BASES else ["env", "file"]
            for layer in layers:
                for value in values:
                    for home in (self.home, self.home + "/"):
                        sh, py = self._one(key, value, layer, home)
                        with self.subTest(key=key, value=value, layer=layer, home=home):
                            self.assertEqual(sh, py)
                            n += 1
        self.assertGreater(n, 0)

    def test_every_site_expression_resolves_as_config(self):
        exprs = set()
        for rel, lines in derived_sites().items():
            for _n, line in lines:
                for key, e in _param_expansions(_code(line)):
                    exprs.add((rel, key, e))
        self.assertTrue(exprs)
        for rel, key, e in sorted(exprs):
            cases = [{key: v} for v in (CSV_VALUES if key in CSVPATH_KEYS else PATH_VALUES)]
            if "XDG_STATE_HOME" in e:
                cases += [{"XDG_STATE_HOME": v} for v in PATH_VALUES if v.startswith("~")]
            for case in cases:
                env = {"HOME": self.home, "QQ_CONFIG": os.path.join(self.tmp, "none.conf")}
                env.update(case)
                # a default naming a script-local variable ($state, $_sd) is not reached once
                # KEY is set; with KEY unset only the XDG cases run, whose defaults are
                # self-contained, so the local is left empty and the line must not depend on it
                if key not in case and re.search(r"\$\{?(?!HOME|XDG_|QQ_)[a-z_]", e):
                    continue
                sh = _bash(f'printf "%s" "{e}"', env, self.cwd)
                py = Config(env=env).get(key)
                with self.subTest(site=rel, expr=e, env=case):
                    self.assertEqual(_norm_sh(key, sh), _norm_py(py))

    def test_every_site_file_normalises_before_its_first_site(self):
        sites = derived_sites()
        files = list(_shell_files())
        self.assertTrue(sites)

        def first_config_source(rel):
            for n, line in enumerate(_lines(rel), 1):
                if _SOURCES_CONFIG.match(_code(line)):
                    return n
            return None

        for rel, lines in sites.items():
            if rel in ("qq-config.sh",):
                continue        # the normaliser itself
            first_site = lines[0][0]
            with self.subTest(file=rel):
                src = first_config_source(rel)
                if src is not None and src < first_site:
                    continue
                # a library: every file that sources it has sourced qq-config.sh first
                sourcers = []
                for other in files:
                    for n, line in enumerate(_lines(other), 1):
                        m = _SOURCES.match(_code(line))
                        if m and m.group(1) == os.path.basename(rel):
                            sourcers.append((other, n))
                self.assertTrue(sourcers, f"{rel}: reads a path key before sourcing qq-config.sh "
                                          f"(line {first_site}) and is sourced by nobody who does")
                for other, n in sourcers:
                    s = first_config_source(other)
                    self.assertTrue(s is not None and s < n,
                                    f"{other}:{n} sources {rel} before qq-config.sh")


if __name__ == "__main__":
    unittest.main()
