#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-config-family-path-parity.sh — the config-file family (the dotenv config file and the
# redact-slugs list) resolves to ONE file across the two engines. For every value of HOME,
# QQ_CONFIG, XDG_CONFIG_HOME and QQ_REDACT_FILE, every shell function that builds a path of that
# family — `_qq_config_file` (qq-config.sh) and `_qq_redact_file` (qq-redact.sh), the two sites
# that join `quintessence/config` and `quintessence/redact-slugs` — must name the same file the
# python Config resolves for the same environment, a leading `~` or `~/` expanded against $HOME
# on both sides.
#
# Both sides are ASKED, never compared to a hand-written expectation: the shell side is the
# sourced function's own output, the python side is `Config().config_path` and
# `Config().get_path("QQ_REDACT_FILE")`. Every case runs under `env -i` with a throwaway HOME that
# exists and is empty, so no case reads the invoking user's config and none writes anything. Only
# stdout is compared — a loader grumbling on stderr (e.g. a config path that names a directory)
# is a diagnostic, printed under the failure, not a path.
#
# Directions this walks, one per way the sentence can be false:
#   - which variable carries the value: QQ_CONFIG (the config file itself), XDG_CONFIG_HOME (the
#     base of BOTH family members), QQ_REDACT_FILE (the list), HOME (what a `~` expands against).
#   - which shape the value has: unset, absolute, bare `~`, `~/…`, and a `~` NOT in the leading
#     position (which must stay literal on both sides — the sentence expands a LEADING one).
#   - the empty string, for the variables whose empty value the estate documents: XDG_CONFIG_HOME
#     ("present but EMPTY means unset", config.py's _config_base_default) and QQ_CONFIG; and
#     QQ_REDACT_FILE, whose empty value CONFIG.md documents as "Empty or absent means the
#     filtering is off". An empty value is a value, and the two engines must at least agree on
#     which file it names.
#   - which family member is asked: the config file and the redact list share the base, so an
#     unexpanded base shows up twice.
#   - precedence: a value on one variable must not change what another one names (an absolute
#     QQ_REDACT_FILE over a `~/…` XDG_CONFIG_HOME, a `~/…` QQ_CONFIG over a `~/…` base).
# Not pinned here, and said so rather than left unmentioned: a `~user` form (the sentence names
# `~` and `~/` expanded against HOME; `~user` is neither), an EMPTY HOME (python falls back to the
# passwd entry, which bash has no equivalent of — nothing in the sentence asks for one), and a
# value arriving from the dotenv layer rather than the environment (the sentence quantifies over
# values, not layers — and for the config file's own location the file layer is unreachable by
# construction on the python side, which reads the file only after locating it).
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
command -v python3 >/dev/null 2>&1 || { echo "test-config-family-path-parity.sh: python3 not found — skipping (treated as pass)"; exit 0; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
HOME_SYN="$TMP/home"; mkdir -p "$HOME_SYN"     # exists, empty: the anchor every `~` expands to
ABS="$TMP/elsewhere"                            # absolute; never created — only the NAME is compared
PATH0="${PATH:-/usr/local/bin:/usr/bin:/bin}"

fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

# A repeated separator and a trailing separator name the SAME file, so they are not a divergence.
canon(){ printf '%s' "$1" | sed -e 's|/\+|/|g' -e '/^\/$/!s|/$||'; }

# one_case <label> [NAME=VALUE ...] — a NAME not listed is UNSET for the case. HOME is the
# throwaway one unless a SPEC names it, and an explicit HOME= wins because `env` takes the LAST
# assignment of a name.
one_case(){
  local label="$1"; shift
  local -a envs=("$@")
  local sh_cfg sh_red py_out py_cfg py_red c_sh c_py
  sh_cfg="$(env -i PATH="$PATH0" HOME="$HOME_SYN" "${envs[@]}" \
            bash -c ". '$ENGINE/qq-config.sh'; _qq_config_file" 2>"$TMP/e1")"
  sh_red="$(env -i PATH="$PATH0" HOME="$HOME_SYN" "${envs[@]}" \
            bash -c ". '$ENGINE/qq-redact.sh'; _qq_redact_file" 2>"$TMP/e2")"
  py_out="$(env -i PATH="$PATH0" HOME="$HOME_SYN" PYTHONPATH="$ENGINE" "${envs[@]}" \
            python3 -c '
from quintessence.config import Config
c = Config()
print("QQCFG=" + c.config_path)
print("QQRED=" + c.get_path("QQ_REDACT_FILE"))' 2>"$TMP/e3")"
  py_cfg="$(printf '%s\n' "$py_out" | sed -n 's/^QQCFG=//p')"
  py_red="$(printf '%s\n' "$py_out" | sed -n 's/^QQRED=//p')"
  if [ -z "$py_cfg" ] && [ -z "$py_red" ]; then
    no "$label / Config did not answer: $py_out $(printf '%s' "$(cat "$TMP/e3")")"
    return
  fi

  # stderr of the two shell sides, shown only under a failure of that side
  local se; se="$(cat "$TMP/e1" "$TMP/e2" 2>/dev/null)"

  c_sh="$(canon "$sh_cfg")"; c_py="$(canon "$py_cfg")"
  if [ "$c_sh" = "$c_py" ]; then ok "$label / config file: $c_sh"
  else no "$label / config file names two files: shell '$c_sh' vs Config '$c_py'${se:+ | stderr: $se}"; fi

  c_sh="$(canon "$sh_red")"; c_py="$(canon "$py_red")"
  if [ "$c_sh" = "$c_py" ]; then ok "$label / redact list: $c_sh"
  else no "$label / redact list names two files: shell '$c_sh' vs Config '$c_py'${se:+ | stderr: $se}"; fi
}

# ---- baseline: nothing set but a throwaway HOME -> both family members under $HOME/.config ----
one_case "HOME only"

# ---- HOME's own shape: a trailing separator still names the same file -------------------------
one_case "HOME with trailing separator" "HOME=$HOME_SYN/"

# ---- QQ_CONFIG: the config file itself -------------------------------------------------------
one_case "QQ_CONFIG absolute"      "QQ_CONFIG=$ABS/cfg"
one_case "QQ_CONFIG empty"         "QQ_CONFIG="
one_case "QQ_CONFIG bare tilde"    "QQ_CONFIG=~"
one_case "QQ_CONFIG tilde-slash"   "QQ_CONFIG=~/cfg-file"
one_case "QQ_CONFIG mid tilde"     "QQ_CONFIG=$ABS/a~/b"
one_case "QQ_CONFIG tilde-slash over tilde-slash XDG" "QQ_CONFIG=~/cfg-file" "XDG_CONFIG_HOME=~/xdg"

# ---- XDG_CONFIG_HOME: the base of BOTH family members ----------------------------------------
one_case "XDG absolute"            "XDG_CONFIG_HOME=$ABS/xdg"
one_case "XDG empty"               "XDG_CONFIG_HOME="
one_case "XDG bare tilde"          "XDG_CONFIG_HOME=~"
one_case "XDG tilde-slash"         "XDG_CONFIG_HOME=~/xdg"
one_case "XDG mid tilde"           "XDG_CONFIG_HOME=$ABS/a~/b"

# ---- QQ_REDACT_FILE: the list, overriding the base -------------------------------------------
one_case "QQ_REDACT_FILE absolute"  "QQ_REDACT_FILE=$ABS/redact"
one_case "QQ_REDACT_FILE empty"     "QQ_REDACT_FILE="
one_case "QQ_REDACT_FILE bare tilde"   "QQ_REDACT_FILE=~"
one_case "QQ_REDACT_FILE tilde-slash"  "QQ_REDACT_FILE=~/redact-list"
one_case "QQ_REDACT_FILE mid tilde"    "QQ_REDACT_FILE=$ABS/a~/b"
one_case "QQ_REDACT_FILE absolute over tilde-slash XDG" "QQ_REDACT_FILE=$ABS/redact" "XDG_CONFIG_HOME=~/xdg"

# ---- all three at once ----------------------------------------------------------------------
one_case "all tilde-valued at once" "QQ_CONFIG=~/cfg-file" "XDG_CONFIG_HOME=~/xdg" "QQ_REDACT_FILE=~/redact-list"
one_case "mixed absolute and tilde" "QQ_CONFIG=$ABS/cfg" "XDG_CONFIG_HOME=~/xdg" "QQ_REDACT_FILE=~/redact-list"

echo "----- $pass passed, $fail failed -----"
[ "$fail" -eq 0 ]
