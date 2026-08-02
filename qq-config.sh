#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# qq-config.sh — shared per-install config loader. SOURCED by every bash entry point (qq-lib.sh
# and the standalone hook/audit scripts) so an install is DEFINED in one durable file, not
# reconstructed from the ambient shell environment each run (which plugin hooks can't rely on).
#
# Format: a dotenv KEY=value file. Search order:  $QQ_CONFIG  →  ${XDG_CONFIG_HOME:-~/.config}/quintessence/config
# Precedence: environment variable > config file > each script's built-in default. (A value already
# in the environment is never overwritten; the file only fills in what the environment didn't set.)
# The file is PARSED line-by-line, never `source`d — so a config file can't execute arbitrary code.

_qq_config_file() {
  if [ -n "${QQ_CONFIG:-}" ]; then printf '%s' "$QQ_CONFIG"; return 0; fi
  printf '%s' "${XDG_CONFIG_HOME:-$HOME/.config}/quintessence/config"
}

_qq_load_config() {
  local f line key val
  f="$(_qq_config_file)"
  [ -r "$f" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac      # skip blank + comment lines
    case "$line" in *=*) ;; *) continue ;; esac     # must be KEY=VALUE
    key="${line%%=*}"; val="${line#*=}"
    # trim surrounding whitespace (incl. a trailing CR from CRLF files) from BOTH — must match
    # qqsearch_core._load_config_file byte-for-byte, else the bash CLIs and the python recall
    # could resolve a key (e.g. QUINTESSENCE_DIR) to different values.
    key="${key#"${key%%[![:space:]]*}"}"; key="${key%"${key##*[![:space:]]}"}"
    val="${val#"${val%%[![:space:]]*}"}"; val="${val%"${val##*[![:space:]]}"}"
    # key must be a valid identifier — skip a malformed key, never silently mangle it
    case "$key" in ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;; esac
    case "$val" in                                   # strip one layer of surrounding quotes
      \"*\") val="${val#\"}"; val="${val%\"}" ;;
      \'*\') val="${val#\'}"; val="${val%\'}" ;;
    esac
    if [ -z "${!key+x}" ]; then export "$key=$val"; fi   # env wins: set only if not already set
  done < "$f"
  return 0                                            # never let a sourcing script's set -e trip on us
}

_qq_load_config
