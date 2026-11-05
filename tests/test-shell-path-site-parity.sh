#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-shell-path-site-parity.sh — certify check (independent of the fixing round) for the sentence:
#
#   For every `path` or `csvpath` key in the `KEYS` registry, and for the XDG bases `config.py`
#   expands into a path default, every shell site outside `tests/` that reads the key names the
#   path `Config.get` resolves for the same environment and config file, a leading `~` or `~/`
#   expanded against `HOME` on both sides.
#
# HOW THE SITES ARE FOUND — derived, never a hand-list. The key set is read from the registry
# itself (every KeyDef whose type is `path` or `csvpath`), the XDG bases are every `XDG_*` name
# config.py mentions, and the shell files are every tracked file outside tests/ that is `*.sh` or
# carries a bash/sh shebang. A SITE is any non-comment line that reads one of those names through a
# defaulting or plain expansion (`${K:-…}`, `${K-…}`, `${K:=…}`, `${K=…}`, `${K}`); a presence test
# (`${K+x}`) reads no value and is not a site. A site added tomorrow is checked without this file
# changing; a site this file cannot classify FAILS rather than being skipped.
#
# HOW A SITE IS ASKED — in the context its own script gives it, never compared to a hand-written
# expectation:
#   - the loaders the script sources before the site (`. …/qq-config.sh`, qq-lib.sh, …) are
#     sourced, and a library that sources none is given the loaders of the script that sources it;
#   - every earlier site assignment in the same file is replayed first (a site may build on one:
#     `$state/contract-extra.md`), and every function the file defines is defined;
#   - a site inside a function that prints a path is asked by calling the function;
#   - a site that assigns a variable is evaluated as the statement, then any later one-line
#     re-assignment of that variable (or a one-line `for` over a list naming it) is applied up to
#     the variable's first use — so the value compared is the one the script goes on to USE;
#   - any other site is asked as its own `${…}` expansion.
# The other side is `Config().get(<key>)` for the same environment and the same config file. The
# key a site answers for is the first registry key on its line, else the first one in its
# enclosing function. Every case runs under `env -i` with a fresh throwaway HOME and a throwaway
# cwd, so no case reads the invoking user's config.
#
# Directions walked, one per way the sentence can be false:
#   - layer: the value arrives from the ENVIRONMENT, or from the CONFIG FILE (a shell site that
#     never loads the file names the default while Config names the file's value);
#   - shape: absolute, bare `~`, `~/…` (a site that does not expand it names ./~/… under its cwd);
#   - the XDG bases themselves, `~/…`-valued, absolute, and present-but-empty (documented as unset
#     on both sides), from the environment and (XDG_STATE_HOME) from the file;
#   - the config file's own location reached through a `~/…` QQ_CONFIG or a `~/…` XDG_CONFIG_HOME,
#     with a path key set in it (both engines must read the same file to agree on anything in it);
#   - derived defaults: a `~/…` QQ_STATE_DIR seen through the keys whose default hangs off it;
#   - HOME with a trailing separator; and the baseline with nothing set.
# Not pinned, and said so: a `~user` form (the sentence names `~` and `~/` only), an EMPTY HOME,
# and a present-but-EMPTY value of a QQ_* key (shell `:-` reads it as unset, Config as the value
# ""; which is right is a question about the key, not about the two engines' paths).
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
command -v python3 >/dev/null 2>&1 || { echo "test-shell-path-site-parity.sh: python3 not found — skipping (treated as pass)"; exit 0; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
PATH0="${PATH:-/usr/local/bin:/usr/bin:/bin}"
ABS="$TMP/elsewhere"

# ---- 1. discover keys and sites, write one probe script per site --------------------------------
PROBES="$TMP/probes"; mkdir -p "$PROBES"
if ! PYTHONPATH="$ENGINE" python3 - "$ENGINE" "$PROBES" > "$TMP/sites.tsv" 2> "$TMP/disc.err" <<'PY'
import os, re, subprocess, sys
engine, probes = sys.argv[1], sys.argv[2]
from quintessence.config import KEYS
pathkeys = {k.name: k.type for k in KEYS if k.type in ("path", "csvpath")}
cfgsrc = open(os.path.join(engine, "quintessence", "config.py")).read()
xdg = sorted(set(re.findall(r"\bXDG_[A-Z_]+\b", cfgsrc)))
names = sorted(set(pathkeys) | set(xdg), key=len, reverse=True)

files = subprocess.run(["git", "-C", engine, "ls-files"], capture_output=True, text=True).stdout.split()
if not files:   # not a git checkout (a plain copy): walk the tree
    for d, _, fs in os.walk(engine):
        if "/.git" in d: continue
        files += [os.path.relpath(os.path.join(d, f), engine) for f in fs]
shfiles = []
for f in sorted(set(files)):
    if f.startswith("tests/") or "/tests/" in f: continue
    p = os.path.join(engine, f)
    if not os.path.isfile(p): continue
    try: first = open(p, "rb").readline().decode("utf-8", "replace")
    except OSError: continue
    if f.endswith(".sh") or (first.startswith("#!") and re.search(r"\b(bash|sh)\b", first)):
        shfiles.append(f)

site_re = re.compile(r"\$\{(" + "|".join(map(re.escape, names)) + r")(:-|:=|-|=|\})")
load_re = re.compile(r"^\s*(?:\.|source)\s+.*/([A-Za-z0-9_.-]+\.sh)\b")
func_re = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{?")
assign_re = lambda v: re.compile(r"^\s*(?:export\s+|local\s+|readonly\s+|declare\s+)?" + re.escape(v) + r"=")

def code(line):
    s = line.lstrip()
    return "" if s.startswith("#") else line

def funcs(lines):
    """[(name, start, end)] of top-level function definitions (0-based, inclusive)."""
    out, i = [], 0
    while i < len(lines):
        m = func_re.match(lines[i])
        if m and code(lines[i]):
            if lines[i].rstrip().endswith("}") and lines[i].count("{") <= lines[i].count("}"):
                out.append((m.group(1), i, i)); i += 1; continue
            j = i + 1
            while j < len(lines) and not lines[j].startswith("}"): j += 1
            out.append((m.group(1), i, j)); i = j + 1; continue
        i += 1
    return out

def loads_before(f, n, lines):
    out = []
    for l in lines[:n]:
        m = load_re.match(code(l))
        if m and m.group(1) in {os.path.basename(s) for s in shfiles}:
            out.append(next(s for s in shfiles if os.path.basename(s) == m.group(1)))
    return out

src = {f: open(os.path.join(engine, f), encoding="utf-8", errors="replace").read().split("\n") for f in shfiles}

def context_loads(f, n):
    own = loads_before(f, n, src[f])
    if own: return own
    base = os.path.basename(f)
    for g in shfiles:                      # a library: the loaders of the script that sources it
        if g == f: continue
        for i, l in enumerate(src[g]):
            m = load_re.match(code(l))
            if m and m.group(1) == base:
                return [x for x in loads_before(g, i, src[g]) if x != f]
    return []

def outer_expansion(line, start):
    depth, i = 0, start
    while i < len(line):
        if line.startswith("${", i): depth += 1; i += 2; continue
        if line[i] == "}":
            depth -= 1
            if depth == 0: return line[start:i + 1]
        i += 1
    return None

allfuncs = {nm for g in shfiles for nm, _, _ in funcs(src[g])}

def wrapped_by_external(line, start):
    """True when the expansion sits inside a `$( … )` whose command is not a shell function of
    this estate (e.g. `$(cat "${K:-…}")`): the assignment then holds that command's OUTPUT, not
    the path, and the site is the expansion itself."""
    depth, i, opens = 0, 0, []
    while i < start:
        if line.startswith("$(", i): opens.append(i); i += 2; continue
        if line[i] == ")" and opens: opens.pop()
        i += 1
    for o in opens:
        w = re.match(r"\s*([A-Za-z_][A-Za-z0-9_-]*)", line[o + 2:])
        if w and w.group(1) not in allfuncs: return True
    return False

def first_key(text):
    hits = [(m.start(), m.group(1)) for m in re.finditer(r"\b(" + "|".join(map(re.escape, pathkeys)) + r")\b", text)]
    return min(hits)[1] if hits else None

n_probe = 0
for f in shfiles:
    lines = src[f]
    fdefs = funcs(lines)
    sites_assign = []   # (lineno, var) of earlier top-level site assignments, for replay
    for idx, raw in enumerate(lines):
        line = code(raw)
        ms = list(site_re.finditer(line))
        if not ms: continue
        enc = next(((nm, a, b) for nm, a, b in fdefs if a <= idx <= b), None)
        key = first_key(line)
        if key is None and enc:
            key = first_key("\n".join(lines[enc[1]:enc[2] + 1]))
        n_probe += 1
        pid = "p%03d" % n_probe
        loc = "%s:%d" % (f, idx + 1)
        if key is None:
            print("\t".join([pid, loc, "-", "UNCLASSIFIED", "no registry key on the line or in its function"])); continue
        # how the site is asked
        am = re.match(r"^\s*(?:export\s+|local\s+|readonly\s+|declare\s+)?([A-Za-z_][A-Za-z0-9_]*)=", line)
        cm = re.match(r"^\s*:\s+\"?\$\{([A-Za-z_][A-Za-z0-9_]*):?=", line)
        body = []
        body.append('cd "$PROBE_CWD" 2>/dev/null; ENGINE=%s; HERE="$ENGINE"; root="$ENGINE"; set +eu' % repr(engine))
        for L in context_loads(f, idx):
            body.append('. "$ENGINE/%s" </dev/null >/dev/null 2>&1; set +eu' % L)
        for nm, a, b in fdefs:
            if enc and nm == enc[0]: continue
            body.append("\n".join(lines[a:b + 1]))
        for ln, var in sites_assign:
            body.append(re.sub(r"^(\s*)local\s+", r"\1", lines[ln]) + " </dev/null >/dev/null 2>&1")
        if enc:
            mode = "function " + enc[0]
            body.append("\n".join(lines[enc[1]:enc[2] + 1]))
            body.append('__v="$(%s </dev/null 2>/dev/null)"' % enc[0])
        elif (am or cm) and not wrapped_by_external(line, ms[0].start()):
            var = (am or cm).group(1)
            mode = "assignment " + var
            body.append(raw + " </dev/null >/dev/null 2>&1")
            # later one-line re-assignments of VAR (or a one-line for over a list naming it), up to its first use
            use = re.compile(r"\$\{?" + re.escape(var) + r"\b")
            for j in range(idx + 1, min(idx + 41, len(lines))):
                lj = code(lines[j])
                if any(a <= j <= b for _, a, b in fdefs): continue
                if assign_re(var).match(lj):
                    body.append(lj + " </dev/null >/dev/null 2>&1"); continue
                if re.match(r"^\s*for\s+\w+\s+in\b.*\b" + re.escape(var) + r"\b.*;\s*done\s*$", lj):
                    body.append(lj + " </dev/null >/dev/null 2>&1"); continue
                if use.search(lj): break
            body.append('__v="${%s-}"' % var)
            if not enc and am and not re.match(r"^\s*local\s", line):
                sites_assign.append((idx, var))
        else:
            exp = outer_expansion(line, ms[0].start())
            if exp is None:
                print("\t".join([pid, loc, key, "UNCLASSIFIED", "unbalanced expansion"])); continue
            if re.search(r":?-\}$", exp) and re.search(r"\[\[?\s+-[nz]\s", line):
                print("\t".join([pid, loc, key, "PRESENCE", exp])); continue
            mode = "expansion " + exp
            body.append('__v="%s"' % exp)
        body.append("printf 'SITEVALUE=%s\\n' \"$__v\"")
        open(os.path.join(probes, pid + ".sh"), "w").write("\n".join(body) + "\n")
        print("\t".join([pid, loc, key, pathkeys[key], mode]))
sys.stderr.write("keys: %s\nxdg: %s\nshell files: %d\n" % (" ".join(sorted(pathkeys)), " ".join(xdg), len(shfiles)))
PY
then echo "FAIL site discovery crashed:"; cat "$TMP/disc.err"; exit 1; fi
cat "$TMP/disc.err"

fail=0; pass=0
ok(){ pass=$((pass+1)); }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

nsites=0
while IFS=$'\t' read -r pid loc key typ mode; do
  case "$typ" in
    UNCLASSIFIED) no "site $loc cannot be classified ($mode) — every site must answer for a key";;
    PRESENCE)     echo "  (not a naming site: $loc tests $mode for presence)";;
    *)            nsites=$((nsites+1)); echo "  site $loc -> $key [$mode]";;
  esac
done < "$TMP/sites.tsv"
echo "sites to ask: $nsites"
[ "$nsites" -gt 0 ] || { no "no shell site found at all — discovery is broken"; }

canon(){ printf '%s' "$1" | sed -e 's|/\+|/|g' -e '/^\/$/!s|/$||'; }
canon_list(){  # csvpath: members, each `~`-expanded against $2 as the sentence says both sides do
  local IFS=','; local out="" m
  for m in $1; do m="${m#"${m%%[![:space:]]*}"}"; m="${m%"${m##*[![:space:]]}"}"; [ -n "$m" ] || continue
    out="$out,$(canon "$m")"; done
  printf '%s' "${out#,}"
}

ncase=0
# one_case <label> <file-body-or-'-'> [NAME=VALUE ...]  — HOME is a fresh throwaway dir; a config
# file body (lines separated by '|') is written where the case's environment locates it.
one_case(){
  local label="$1" cfgbody="$2"; shift 2
  ncase=$((ncase+1))
  local H="$TMP/h$ncase" CWD="$TMP/cwd$ncase"; mkdir -p "$H" "$CWD"
  local -a envs=("HOME=$H" "$@")
  # where does the config file go? ask the engine's own Config (for the same env) and refuse a
  # case whose two engines would disagree on that alone — that is a site failure caught below.
  if [ "$cfgbody" != "-" ]; then
    local cpath; cpath="$(cd "$CWD" && env -i PATH="$PATH0" PYTHONPATH="$ENGINE" "${envs[@]}" python3 -c 'from quintessence.config import Config; print(Config().config_path)')"
    mkdir -p "$(dirname "$cpath")"; printf '%s\n' "$cfgbody" | tr '|' '\n' > "$cpath"
  fi
  local py; py="$(cd "$CWD" && env -i PATH="$PATH0" PYTHONPATH="$ENGINE" "${envs[@]}" python3 -c '
from quintessence.config import Config, KEYS
c = Config()
for k in KEYS:
    if k.type == "path": print(k.name + "\t" + c.get(k.name))
    elif k.type == "csvpath": print(k.name + "\t" + ",".join(c.get(k.name)))' 2>"$TMP/pyerr")" || { no "$label / Config did not answer: $(cat "$TMP/pyerr")"; return; }
  local Hh e; Hh="$H"; for e in "${envs[@]}"; do case "$e" in HOME=*) Hh="${e#HOME=}";; esac; done
  Hh="$(printf '%s' "$Hh" | sed 's|/*$||')"
  while IFS=$'\t' read -r pid loc key typ mode; do
    case "$typ" in UNCLASSIFIED|PRESENCE) continue;; esac
    local want got out
    want="$(printf '%s\n' "$py" | awk -F'\t' -v k="$key" '$1==k{print $2; exit}')"
    out="$(cd "$CWD" && env -i PATH="$PATH0" "${envs[@]}" PROBE_CWD="$CWD" bash "$PROBES/$pid.sh" 2>/dev/null </dev/null)"
    got="$(printf '%s\n' "$out" | sed -n 's/^SITEVALUE=//p' | tail -n1)"
    if [ "$typ" = csvpath ]; then want="$(canon_list "$want")"; got="$(canon_list "$got")"
    else want="$(canon "$want")"; got="$(canon "$got")"; fi
    if [ "$got" = "$want" ]; then ok
    else no "$label / $loc ($key, $mode): shell names '${got//$Hh/\$HOME}' but Config.get resolves '${want//$Hh/\$HOME}'"; fi
  done < "$TMP/sites.tsv"
}

# keys some site answers for (the cases below vary each of them)
SITE_KEYS="$(awk -F'\t' '$4=="path"||$4=="csvpath"{print $3}' "$TMP/sites.tsv" | sort -u)"

one_case "baseline (nothing set)" -
one_case "HOME with trailing separator" - "HOME=$TMP/h_trail/"
mkdir -p "$TMP/h_trail"

for K in $SITE_KEYS; do
  one_case "env $K absolute"   - "$K=$ABS/$K"
  one_case "env $K bare tilde" - "$K=~"
  one_case "env $K tilde-slash" - "$K=~/v-$K"
  if [ "$K" != "QQ_CONFIG" ]; then   # the config file cannot carry its own location
    one_case "config-file $K absolute"    "$K=$ABS/f-$K"
    one_case "config-file $K tilde-slash" "$K=~/f-$K"
  fi
done

# the XDG bases config.py expands into path defaults
one_case "env XDG_STATE_HOME tilde-slash" - "XDG_STATE_HOME=~/xs"
one_case "env XDG_STATE_HOME absolute"    - "XDG_STATE_HOME=$ABS/xs"
one_case "env XDG_STATE_HOME empty"       - "XDG_STATE_HOME="
one_case "config-file XDG_STATE_HOME tilde-slash" "XDG_STATE_HOME=~/fxs"
one_case "config-file XDG_STATE_HOME absolute"    "XDG_STATE_HOME=$ABS/fxs"
one_case "env XDG_CONFIG_HOME tilde-slash" - "XDG_CONFIG_HOME=~/xc"
one_case "env XDG_CONFIG_HOME absolute"    - "XDG_CONFIG_HOME=$ABS/xc"
one_case "env XDG_CONFIG_HOME empty"       - "XDG_CONFIG_HOME="

# the config file reached through a tilde location, carrying path keys
one_case "tilde QQ_CONFIG, file sets QUINTESSENCE_DIR+QQ_STATE_DIR" \
  "QUINTESSENCE_DIR=~/q-cfg|QQ_STATE_DIR=~/s-cfg" "QQ_CONFIG=~/cfgdir/config"
one_case "tilde XDG_CONFIG_HOME, file sets QQ_MEMDIR+QQ_STATE_DIR" \
  "QQ_MEMDIR=~/m-xc|QQ_STATE_DIR=$ABS/s-xc" "XDG_CONFIG_HOME=~/xc2"
# env over file, both tilde
one_case "env tilde QQ_STATE_DIR over file absolute QQ_STATE_DIR" "QQ_STATE_DIR=$ABS/s-file" "QQ_STATE_DIR=~/s-env"

echo "----- $ncase cases x $nsites sites: $pass passed, $fail failed -----"
[ "$fail" -eq 0 ]
