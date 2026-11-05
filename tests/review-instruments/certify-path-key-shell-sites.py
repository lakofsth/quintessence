#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""certify-path-key-shell-sites.py ENGINE — every shell site that reads a path key names Config's path.

The sentence under test (tests/INVARIANTS.md wording, certified independently of the fix):
  For every `path` or `csvpath` key in the `KEYS` registry, and for the XDG bases `config.py`
  expands into a path default, every shell site outside `tests/` that reads the key names the
  path `Config.get` resolves for the same environment and config file, a leading `~` or `~/`
  expanded against `HOME` on both sides.

NOTHING HERE IS A HAND LIST. The keys come from `quintessence.config.KEYS` (type path/csvpath);
the shell files are every `*.sh` or bash/sh-shebang file under ENGINE outside tests/; a SITE is a
line of one of them that resolves one of those names (or XDG_STATE_HOME / XDG_CONFIG_HOME) with a
default — `${NAME:-…}`, `${NAME-…}`, `${NAME:=…}`, `${NAME=…}` — or assigns from a helper call
that names the key as a word. Which key a site stands for is DERIVED, not written down: at the
baseline (nothing set but HOME) the site's value must equal exactly the default of some path key,
and that key is what it is compared against in every other case. A site that equals no key's
default already fails.

BOTH SIDES ARE ASKED. The shell side is the site's own script, run for real under an exact
environment (no inherited variable), with a DEBUG trap that stops it just after its last top-level
site — after any top-level assignment or shell-function call that follows (a fix-up such as an
untilde loop is part of what the site names), before the first other command — and prints each
site's value: the assigned variable for a top-level site, the function's output for a site
inside a function, and the site's own expression, evaluated in that script's context, for a site
nested in a top-level block. A file other files SOURCE is run as they run it: sourced, after
qq-config.sh when every file sourcing it has loaded the config first. The python side is
`Config(env=<the same env>).get(key)`, reading the same config file.

DIRECTIONS (one per way the sentence can be false):
  - layer: the value arrives from the ENVIRONMENT; from the CONFIG FILE (at its default
    location); from both, which must agree on which one wins.
  - shape: absolute; bare `~`; `~/`; `~/sub`; a `~` not in leading position (stays literal).
  - variable: each path key some site reads, and each XDG base (whose empty value means unset on
    both sides, so the empty string is a shape for those two).
  - default: nothing set at all — the site's own default must be the registry default.
Not pinned, said rather than left unmentioned: an EMPTY value of a key (Config answers '' for it,
which names no path, so there is nothing for a site to agree with); `~user`; an empty HOME;
QQ_CONFIG from the config file (the registry: it "cannot be set from the file"); csvpath keys are
asked for but no shell site reads one at the time of writing, so they are walked vacuously.
setup.sh IS held in the config-file layer, like every other site. It was once skipped there, on
the ground that an installer defers to the file through `qq config get`. A scratch-HOME install
at 7aed6ca refuted that ground (it named $HOME/quintessence over the configured store), and the
skip left this certify green with e4a3aae's `. "$HERE/qq-config.sh"` replaced by `:`. Without
the skip that mutation is red: 10 disagreements, all setup.sh:29/:30, all in the file layer.
The run stops at setup.sh's last site, before its first command, so nothing is installed.

Exit 0 when every site agrees in every case, 1 otherwise; one line per disagreement.
"""
import os
import re
import shlex
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

ENGINE = os.path.abspath(sys.argv[1])
sys.path.insert(0, ENGINE)
from quintessence.config import KEYS, Config  # noqa: E402

PATH_KEYS = [k.name for k in KEYS if k.type in ("path", "csvpath")]
XDG = ["XDG_STATE_HOME", "XDG_CONFIG_HOME"]
NAMES = PATH_KEYS + XDG
NAME_ALT = "|".join(re.escape(n) for n in sorted(NAMES, key=len, reverse=True))
DEFAULT_READ = re.compile(r"\$\{(" + NAME_ALT + r")(:-|-|:=|=)")
ASSIGN = re.compile(r"^\s*(?:export\s+|readonly\s+|local\s+|declare\s+(?:-\w+\s+)*)?([A-Za-z_]\w*)=(.*)$")
COLON_ASSIGN = re.compile(r"^\s*:\s+\"?\$\{(" + NAME_ALT + r"):?=")
FUNC_START = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{?")
READERS = ("cat", "head", "tail", "sed", "grep", "jq", "wc", "awk")


def shell_files():
    out = []
    for root, dirs, files in os.walk(ENGINE):
        rel = os.path.relpath(root, ENGINE)
        if rel == "tests" or rel.startswith("tests" + os.sep) or "/.git" in "/" + rel \
                or rel.startswith(".git") or "node_modules" in rel.split(os.sep):
            dirs[:] = []
            continue
        for f in files:
            p = os.path.join(root, f)
            if not os.path.isfile(p):
                continue
            if f.endswith(".sh"):
                out.append(p)
                continue
            try:
                with open(p, "rb") as fh:
                    first = fh.readline(200)
            except OSError:
                continue
            if re.match(rb"#!.*\b(bash|sh)\b", first):
                out.append(p)
    return sorted(out)


def complete_prefixes(lines, upto):
    """complete[k] is True when lines[0:k] parse as a complete script (bash -n)."""
    res = {0: True}
    for k in range(1, upto + 1):
        r = subprocess.run(["bash", "-n"], input="\n".join(lines[:k]) + "\n", text=True,
                           capture_output=True)
        res[k] = r.returncode == 0
    return res


def words(s):
    """Split one shell line into top-level words, keeping quotes, $( ), ${ } and backticks whole."""
    out, cur, i, n = [], "", 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t;&|" and True:
            if cur:
                out.append(cur)
                cur = ""
            i += 1
            continue
        j = skip(s, i)
        cur += s[i:j]
        i = j
    if cur:
        out.append(cur)
    return out


def skip(s, i):
    """Index just past the syntactic unit starting at s[i]."""
    c = s[i]
    if c == "\\":
        return min(i + 2, len(s))
    if c == "'":
        j = s.find("'", i + 1)
        return len(s) if j < 0 else j + 1
    if c == '"':
        j = i + 1
        while j < len(s) and s[j] != '"':
            j = skip_in_dq(s, j)
        return min(j + 1, len(s))
    if s.startswith("$(", i) or s.startswith("${", i):
        close = ")" if s[i + 1] == "(" else "}"
        openc = "(" if close == ")" else "{"
        depth, j = 1, i + 2
        while j < len(s) and depth:
            if s[j] in "'\"\\" or s.startswith("$(", j) or s.startswith("${", j):
                j = skip(s, j)
                continue
            if s[j] == openc:
                depth += 1
            elif s[j] == close:
                depth -= 1
            j += 1
        return j
    return i + 1


def skip_in_dq(s, j):
    if s[j] == "\\":
        return j + 2
    if s.startswith("$(", j) or s.startswith("${", j):
        return skip(s, j)
    return j + 1


def site_word(text, name):
    """The expression of a nested, non-assignment-shaped site: the word holding ${NAME…},
    descending through a reader's command substitution (`$(cat "${NAME:-…}")` names a path by
    its argument, not by the bytes it reads)."""
    for w in words(text):
        if "${" + name not in w and name not in w:
            continue
        m = ASSIGN.match(w)
        rhs = m.group(2) if m else w
        inner = rhs.strip('"')
        if inner.startswith("$(") and inner.endswith(")"):
            iw = words(inner[2:-1])
            if iw and iw[0] in READERS:
                for x in iw[1:]:
                    if "${" + name in x:
                        return x
        return rhs
    return None


def find_sites(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")
    hits = []
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        m = DEFAULT_READ.search(line)
        name = m.group(1) if m else None
        if name is None:
            am = ASSIGN.match(line)
            if am and "$(" in am.group(2):
                wm = re.search(r"\b(" + NAME_ALT + r")\b", am.group(2))
                if wm and "$" + wm.group(1) not in am.group(2) and "${" + wm.group(1) not in am.group(2):
                    name = wm.group(1)
        if name:
            hits.append((idx + 1, name, line))
    if not hits:
        return lines, []
    comp = complete_prefixes(lines, max(h[0] for h in hits))
    sites, seen_funcs = [], set()
    for ln, name, line in hits:
        if comp[ln - 1]:
            kind = "top"
        else:
            s = ln - 1
            while s > 1 and not comp[s - 1]:
                s -= 1
            fm = FUNC_START.match(lines[s - 1])
            if fm:
                if fm.group(1) in seen_funcs:
                    continue
                seen_funcs.add(fm.group(1))
                sites.append({"line": ln, "name": name, "kind": "func", "func": fm.group(1), "text": line})
                continue
            kind = "block"
        sites.append({"line": ln, "name": name, "kind": kind, "text": line})
    return lines, sites


def site_snippet(site, out):
    tag = "%s:%d" % (site["rel"], site["line"])
    pr = "printf 'S\\t%%s\\t%%s\\n' %s \"$__pv\" >> %s" % (shlex.quote(tag), shlex.quote(out))
    if site["kind"] == "func":
        return "__pv=\"$(%s 2>/dev/null)\"; %s" % (site["func"], pr)
    text = site["text"]
    cm = COLON_ASSIGN.match(text)
    am = ASSIGN.match(text)
    if site["kind"] == "top":
        var = cm.group(1) if cm else (am.group(1) if am else None)
        if var:
            return "__pv=\"${%s-<unset>}\"; %s" % (var, pr)
    if am and not cm:
        expr = site_word(text, site["name"])
        rhs = am.group(2).strip()
        if expr is not None and expr != rhs:      # a reader's argument
            return "__pv=$(eval %s 2>/dev/null); %s" % (shlex.quote("printf '%s' " + expr), pr)
        return "eval %s 2>/dev/null; __pv=\"${%s-<unset>}\"; %s" % (
            shlex.quote("%s=%s" % (am.group(1), rhs)), am.group(1), pr)
    expr = site_word(text, site["name"]) or ""
    return "__pv=$(eval %s 2>/dev/null); %s" % (shlex.quote("printf '%s' " + expr), pr)


def build_plan():
    files = shell_files()
    plan = []
    sourced_by = {}
    loads_config = {}
    src_re = re.compile(r"^\s*(?:\.|source)\s+\S*?/([\w.-]+\.sh)\b")
    for p in files:
        with open(p, encoding="utf-8", errors="replace") as f:
            txt = f.read().split("\n")
        targets = set()
        for line in txt:
            if line.lstrip().startswith("#"):
                continue
            m = src_re.match(line)
            if m:
                targets.add(m.group(1))
        for t in targets:
            sourced_by.setdefault(t, set()).add(p)
        base = os.path.basename(p)
        loads_config[p] = base == "qq-config.sh" or bool(targets & {"qq-config.sh", "qq-lib.sh", "qq-redact.sh"})
    for p in files:
        lines, sites = find_sites(p)
        if not sites:
            continue
        rel = os.path.relpath(p, ENGINE)
        for s in sites:
            s["rel"] = rel
        base = os.path.basename(p)
        sourcers = sourced_by.get(base, set()) - {p}
        library = bool(sourcers)
        pre = library and not loads_config[p] and all(loads_config[q] for q in sourcers)
        effective_loads = loads_config[p] or pre
        tops = [s["line"] for s in sites if s["kind"] == "top"]
        if tops:
            cut = max(tops)
        elif library:
            cut = 10 ** 9
        else:
            cut = 10 ** 9
        plan.append({"path": p, "rel": rel, "sites": sites, "library": library, "pre": pre,
                     "cut": cut, "loads": effective_loads})
    return plan


TRAP = r'''
__P_F=%(file)s; __P_CUT=%(cut)d; __P_DONE=0
__p_dump(){
  [ "$__P_DONE" = 1 ] && return 0
  __P_DONE=1
  set +eu
  local __pv
  printf 'D\t1\n' >> %(out)s
%(snippets)s
  return 0
}
__p_dbg(){
  [ "$__P_DONE" = 1 ] && return 0
  [ "$1" = "$__P_F" ] || return 0
  [ "$2" -le "$__P_CUT" ] && return 0
  case "$3" in
    [A-Za-z_]*=*|export\ *|local\ *|declare\ *|readonly\ *|:|:\ *|for\ *|set\ *|shopt\ *) return 0 ;;
  esac
  case "${3%%%% *}" in *=*) return 0 ;; esac
  declare -F -- "${3%%%% *}" >/dev/null 2>&1 && return 0
  __p_dump; exit 0
}
trap '__p_dbg "${BASH_SOURCE[0]:-}" "$LINENO" "$BASH_COMMAND"' DEBUG
trap '__p_dump' EXIT
'''


def run_file(item, env, workdir, n):
    out = os.path.join(workdir, "dump-%d" % n)
    open(out, "w").close()
    snippets = "\n".join("  " + site_snippet(s, out) for s in item["sites"])
    rc_path = os.path.join(workdir, "rc-%d.bash" % n)
    with open(rc_path, "w") as f:
        f.write(TRAP % {"file": shlex.quote(item["path"]), "cut": item["cut"],
                        "out": shlex.quote(out), "snippets": snippets})
    cwd = os.path.join(workdir, "cwd-%d" % n)
    os.makedirs(cwd, exist_ok=True)
    e = dict(env)
    e["BASH_ENV"] = rc_path
    if item["library"]:
        pre = ". %s;" % shlex.quote(os.path.join(ENGINE, "qq-config.sh")) if item["pre"] else ""
        cmd = ["bash", "-c", pre + " . %s; __p_dump" % shlex.quote(item["path"])]
    else:
        cmd = ["bash", item["path"]]
    try:
        subprocess.run(cmd, env=e, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
    except subprocess.TimeoutExpired:
        pass
    vals, dumped = {}, False
    with open(out) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if parts[0] == "D":
                dumped = True
            elif parts[0] == "S" and len(parts) == 3:
                vals[parts[1]] = parts[2]
    return dumped, vals


def canon(p):
    p = re.sub(r"/+", "/", p)
    if p != "/" and p.endswith("/"):
        p = p[:-1]
    return p


def py_values(env):
    c = Config(env=env)
    out = {}
    for k in PATH_KEYS:
        try:
            v = c.get(k)
        except Exception as ex:  # noqa: BLE001
            v = "<error %s>" % ex
        out[k] = ",".join(v) if isinstance(v, list) else str(v)
    return out


def main():
    plan = build_plan()
    if not plan:
        print("FAIL no shell site reading a path key was found — the scan is certifying nothing")
        return 1
    tmp = tempfile.mkdtemp(prefix="certify-path-key-shell-sites.")
    home = os.path.join(tmp, "home")
    os.makedirs(home)
    path0 = os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    base_env = {"PATH": path0, "HOME": home, "LC_ALL": "C",
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t", "TMPDIR": tmp}
    cfg_file = os.path.join(home, ".config", "quintessence", "config")
    counter = [0]
    fails = []
    npass = [0]

    def run_case(label, env_add, file_add, skip_rel=()):
        counter[0] += 1
        cdir = os.path.join(tmp, "case-%d" % counter[0])
        os.makedirs(cdir)
        if os.path.exists(cfg_file):
            os.remove(cfg_file)
        if file_add:
            os.makedirs(os.path.dirname(cfg_file), exist_ok=True)
            with open(cfg_file, "w") as f:
                for k, v in file_add.items():
                    f.write("%s=%s\n" % (k, v))
        env = dict(base_env)
        env.update(env_add)
        py = py_values(env)
        with ThreadPoolExecutor(8) as ex:
            results = list(ex.map(lambda a: (a[1], run_file(a[1], env, cdir, a[0])), enumerate(plan)))
        return py, results

    # baseline: derive which key each site stands for
    py, results = run_case("baseline", {}, {})
    keymap = {}
    for item, (dumped, vals) in results:
        for s in item["sites"]:
            tag = "%s:%d" % (s["rel"], s["line"])
            if not dumped or tag not in vals or vals[tag] == "<unset>":
                fails.append("baseline / %s: site not reached (the script stopped before it)" % tag)
                continue
            sv = canon(vals[tag])
            cands = [k for k in PATH_KEYS if py[k] and canon(py[k]) == sv]
            if len(cands) > 1 and s["name"] in cands:
                cands = [s["name"]]
            if not cands:
                fails.append("baseline / %s: names %r, which is the default of no path key" % (tag, vals[tag]))
                continue
            keymap[tag] = cands[0]
            npass[0] += 1
    print("sites: %d across %d files; mapped: %s" % (
        sum(len(i["sites"]) for i in plan), len(plan),
        ", ".join("%s->%s" % (t, k) for t, k in sorted(keymap.items()))))

    read_names = sorted({s["name"] for i in plan for s in i["sites"]} |
                        {k for k in keymap.values()} | set(XDG))
    cases = []
    for nm in read_names:
        if nm in XDG:
            shapes = [("abs", os.path.join(tmp, "abs-" + nm)), ("~", "~"), ("~/sub", "~/x-" + nm), ("empty", "")]
        else:
            shapes = [("abs", os.path.join(tmp, "abs-" + nm)), ("~", "~"), ("~/", "~/"),
                      ("~/sub", "~/sub-" + nm), ("non-leading ~", "lit/~/" + nm)]
        if nm == "QQ_CONFIG":
            # `~` and `~/` name the home DIRECTORY as the config FILE: a loader failing to read a
            # directory is not a question about which path is named, so only file-shaped values.
            shapes = [x for x in shapes if x[0] not in ("~", "~/")]
        for sl, v in shapes:
            cases.append(("env %s=%s [%s]" % (nm, v, sl), {nm: v}, {}))
        if nm != "QQ_CONFIG":
            for sl, v in shapes:
                cases.append(("file %s=%s [%s]" % (nm, v, sl), {}, {nm: v}))
            cases.append(("env %s=~/e + file %s=~/f [precedence]" % (nm, nm),
                          {nm: "~/e-" + nm}, {nm: "~/f-" + nm}))
    for label, env_add, file_add in cases:
        py, results = run_case(label, env_add, file_add)
        for item, (dumped, vals) in results:
            for s in item["sites"]:
                tag = "%s:%d" % (s["rel"], s["line"])
                if tag not in keymap:
                    continue
                k = keymap[tag]
                if not dumped or tag not in vals or vals[tag] == "<unset>":
                    fails.append("%s / %s: site not reached" % (label, tag))
                    continue
                if canon(vals[tag]) == canon(py[k]):
                    npass[0] += 1
                else:
                    fails.append("%s / %s (%s): shell names %r, Config.get(%s) resolves %r" % (
                        label, tag, k, vals[tag], k, py[k]))
    for f in fails:
        print("FAIL " + f)
    print("certify-path-key-shell-sites: %d agreements, %d disagreements" % (npass[0], len(fails)))
    subprocess.run(["rm", "-rf", tmp])
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
