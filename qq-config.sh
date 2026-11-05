#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# qq-config.sh — shared per-install config loader. SOURCED by every bash entry point (qq-lib.sh
# and the standalone hook/audit scripts) so an install is DEFINED in one durable file, not
# reconstructed from the ambient shell environment each run (which plugin hooks can't rely on).
#
# Format: a dotenv KEY=value file. Search order:  $QQ_CONFIG  →  ${XDG_CONFIG_HOME:-~/.config}/quintessence/config
# (a leading ~ or ~/ in either is expanded against $HOME, as the python Config does — _qq_untilde).
# Precedence: environment variable > config file > each script's built-in default. (The file never
# replaces a value already in the environment; it only fills in what the environment didn't set.
# One rewrite IS made in place, to env and file values alike, after loading: _qq_untilde_path_keys
# below expands a leading `~` / `~/` of every path key against $HOME, and re-joins a csvpath key
# holding a `~` member by member, trimmed, empties dropped. The meaning is Config.get's own; the
# bytes a child process inherits are the resolved ones, not the ones the user wrote.)
# The file is PARSED line-by-line, never `source`d — so a config file can't execute arbitrary code.

# A leading `~` or `~/` expanded against $HOME, exactly as quintessence/config.py's
# _bootstrap_config_path (`_tilde`) and _expanduser do; anything else is printed as given. A path
# that reaches a hook UNEXPANDED (a settings.json "env" value, a systemd Environment= line: neither
# expands `~`) was otherwise read as ./~/... under the hook's cwd, so `qq config set` (python)
# wrote $HOME/... and reported success while every bash entry point read no file at all.
# INVARIANTS.md: the-shell-and-python-loaders-name-the-same-config-file.
_qq_untilde() {   # PATH
  case "$1" in
    "~")   printf '%s' "$HOME" ;;
    "~/"*) printf '%s' "${HOME%/}/${1#\~/}" ;;
    *)     printf '%s' "$1" ;;
  esac
}

_qq_config_file() {
  if [ -n "${QQ_CONFIG:-}" ]; then _qq_untilde "$QQ_CONFIG"; return 0; fi
  _qq_untilde "${XDG_CONFIG_HOME:-$HOME/.config}/quintessence/config"
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
    # and one of OURS: only QQ_*, QUINTESSENCE_DIR and XDG_* are exported (the python parser,
    # Config._parse_dotenv, keeps the same allowlist — parity). Every hook execs python and
    # bash with this environment, so exporting any identifier the file held reached PYTHONPATH,
    # BASH_ENV, LD_PRELOAD, PATH (posture review 2026-10-08, M2) — the comment above about a
    # config file not executing code was true of this loader and false of what it exported.
    # INVARIANTS.md: config-set-writes-one-line-of-one-registered-shape.
    case "$key" in QQ_*|QUINTESSENCE_DIR|XDG_*) ;; *) continue ;; esac
    case "$val" in                                   # strip one layer of surrounding quotes
      \"*\") val="${val#\"}"; val="${val%\"}" ;;
      \'*\') val="${val#\'}"; val="${val%\'}" ;;
    esac
    if [ -z "${!key+x}" ]; then export "$key=$val"; fi   # env wins: set only if not already set
  done < "$f"
  return 0                                            # never let a sourcing script's set -e trip on us
}

_qq_load_config

# Every PATH-typed key, normalised once, here, for every script that sources this file: a leading
# `~` or `~/` expanded against $HOME (_qq_untilde), as quintessence/config.py's Config.get_path does
# — and, for a `csvpath` key, member by member, as Config.get does. Before this, bash read these raw:
# `QUINTESSENCE_DIR=~/notes` (which `qq config set` writes and reports success for, and which a
# settings.json "env" or systemd Environment= value carries unexpanded) was $HOME/notes to python
# and ./~/notes under the hook's cwd to qq-lib.sh's QDIR, memory-commit.sh and every QQ_STATE_DIR
# reader, so writes, locks and state landed apart from the store the user reads. Done in place, in
# both layers (env and file), so a child process sees the value python would have resolved anyway.
# The lists are the KEYS registry's `path` / `csvpath` keys plus the XDG bases config.py expands into
# a path default; tests/py/test_path_key_tilde_parity.py fails when they drift from the registry.
# `~user` is left literal (python defers it to the stdlib, which expands only a user that exists).
# INVARIANTS.md: every-shell-site-of-a-path-key-names-the-path-config-resolves.
_QQ_PATH_KEYS=(QUINTESSENCE_DIR QQ_MEMDIR QQ_STATE_DIR QQ_CONFIG QQ_KB_ROOT QQ_CORPUS_CONFIG
  QQ_CACHE QQ_REDACT_FILE QQ_REMOTE_DENY_FILE QQ_IDLE_AUTOSAVE_STATE QQ_MODEL_TRANSCRIPT
  QQ_XREF_CONTENT QQ_XREF_WAVEOFFS QQ_RECONCILE_REGISTRY QQ_RECONCILE_SNAPSHOT QQ_DOCDIR
  QQ_CONTRACT_EXTRA QQ_RECALL_PRIME QQ_QUINTESSENCE_EXTRA QQ_WRAP_DOCS QQ_WRAP_EXTRA
  XDG_STATE_HOME XDG_CONFIG_HOME)
_QQ_CSVPATH_KEYS=(QQ_PROJECT_STORES QQ_BIND_EXCLUDE_ROOTS)

_qq_untilde_path_keys() {
  local key m out
  local -a members
  for key in "${_QQ_PATH_KEYS[@]}"; do
    [ -n "${!key+x}" ] || continue
    case "${!key}" in "~"|"~/"*) printf -v "$key" '%s' "$(_qq_untilde "${!key}")" ;; esac
  done
  for key in "${_QQ_CSVPATH_KEYS[@]}"; do
    [ -n "${!key+x}" ] || continue
    case "${!key}" in *"~"*) ;; *) continue ;; esac
    IFS=, read -r -a members <<< "${!key}"
    out=""
    for m in "${members[@]}"; do      # trimmed and empties dropped, as Config.get splits a csvpath
      m="${m#"${m%%[![:space:]]*}"}"; m="${m%"${m##*[![:space:]]}"}"
      [ -n "$m" ] || continue
      out="${out:+$out,}$(_qq_untilde "$m")"
    done
    printf -v "$key" '%s' "$out"
  done
  return 0
}

_qq_untilde_path_keys
