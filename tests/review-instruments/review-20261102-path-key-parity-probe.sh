#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# review-20261102-path-key-parity-probe.sh — a REVIEW INSTRUMENT, not a gate (it is not named
# test-*.sh, so tests/run.sh does not run it). Written by the fix-review of 2026-11-02 over
# 0f35e8c..736270e (report: a private review record).
#
# Usage: bash review-20261102-path-key-parity-probe.sh [REPO_DIR]     (default: this checkout)
#
# For every path key some tracked shell site reads, it asks two engines the same question under
# `env -i` with a throwaway HOME:
#   bash   — source REPO/qq-config.sh (and qq-redact.sh), then expand the key the way the sites
#            write it (the RHS of the site lines, copied below, NOT derived; the per-site run is
#            tests/test-certify-path-key-shell-sites.sh's job — this probe varies the VALUE).
#   python — quintessence.config.Config().get(KEY) from REPO.
# Values: ~  ~/x  /abs/x  $HOME/x (literal)  rel/x  ""(empty)  ~nosuchuser/x
# Layers: environment, config file.  HOME: plain, trailing slash.
# Config location: default, QQ_CONFIG=~/qc/config, XDG_CONFIG_HOME=~/xc.
# Each row is AGREE or DISAGREE; empty and ~user rows are tagged as the disclosed residuals.
# Run it at the fix's parent (d61c812) to see it disagree: that is its positive control.
# Exit status: 0 when every non-residual row agrees, 1 otherwise.
set -u
REPO="$(cd "${1:-$(dirname "$(readlink -f "$0")")/../..}" && pwd)"
SCR="$(mktemp -d)"; trap 'rm -rf "$SCR"' EXIT
PY="$(command -v python3)"

declare -A EXPR=(
  [QUINTESSENCE_DIR]='${QUINTESSENCE_DIR:-$HOME/quintessence}'
  [QQ_MEMDIR]='${QQ_MEMDIR:-$HOME/.quintessence-memory}'
  [QQ_STATE_DIR]='${QQ_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/quintessence}'
  [QQ_IDLE_AUTOSAVE_STATE]='${QQ_IDLE_AUTOSAVE_STATE:-${QQ_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/quintessence}/idle-autosave}'
  [QQ_CONTRACT_EXTRA]='${QQ_CONTRACT_EXTRA:-${QQ_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/quintessence}/contract-extra.md}'
  [QQ_RECALL_PRIME]='${QQ_RECALL_PRIME:-${QQ_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/quintessence}/recall-prime.txt}'
  [QQ_REDACT_FILE]='$(_qq_redact_file)'
)
# XDG_STATE_HOME is a key bash normalises too; its observable is QQ_STATE_DIR.
SETKEYS=(QUINTESSENCE_DIR QQ_MEMDIR QQ_STATE_DIR QQ_IDLE_AUTOSAVE_STATE QQ_CONTRACT_EXTRA QQ_RECALL_PRIME QQ_REDACT_FILE XDG_STATE_HOME)
VALUES=('~' '~/x' '/abs/x' '$HOME/x' 'rel/x' '' '~nosuchuser/x')

agree=0; disagree=0; residual_dis=0; n=0
for homeform in plain slash; do
for loc in default qqconfig xdg; do
for layer in env file; do
for key in "${SETKEYS[@]}"; do
for val in "${VALUES[@]}"; do
  n=$((n+1))
  H="$SCR/c$n/home"; mkdir -p "$H"; HV="$H"; [ "$homeform" = slash ] && HV="$H/"
  envs=(HOME="$HV" PATH=/usr/bin:/bin)
  case "$loc" in
    default)  cf="$H/.config/quintessence/config" ;;
    qqconfig) cf="$H/qc/config"; envs+=(QQ_CONFIG='~/qc/config') ;;
    xdg)      cf="$H/xc/quintessence/config"; envs+=(XDG_CONFIG_HOME='~/xc') ;;
  esac
  mkdir -p "$(dirname "$cf")"; : > "$cf"
  if [ "$layer" = env ]; then envs+=("$key=$val"); else printf '%s=%s\n' "$key" "$val" > "$cf"; fi
  observe="$key"; [ "$key" = XDG_STATE_HOME ] && observe=QQ_STATE_DIR
  b="$(cd "$SCR" && env -i "${envs[@]}" bash -c '. "$1/qq-config.sh"; . "$1/qq-redact.sh" 2>/dev/null; eval "printf %s \"$2\""' _ "$REPO" "${EXPR[$observe]}" 2>/dev/null)"
  p="$(cd "$SCR" && env -i "${envs[@]}" "$PY" -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from quintessence.config import Config; sys.stdout.write(str(Config().get(sys.argv[2])))' "$REPO" "$observe" 2>&1)"
  tag=""; case "$val" in '') tag=" [residual: empty]";; '~nosuchuser/x') tag=" [residual: ~user]";; esac
  if [ "$b" = "$p" ]; then agree=$((agree+1))
  else
    if [ -n "$tag" ]; then residual_dis=$((residual_dis+1)); else disagree=$((disagree+1)); fi
    printf 'DISAGREE%s home=%s loc=%s layer=%s %s=%q -> bash=%q python=%q\n' "$tag" "$homeform" "$loc" "$layer" "$key" "$val" "${b//$H/\$H}" "${p//$H/\$H}"
  fi
done; done; done; done; done
echo "----- $REPO @ $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null): $n cases, $agree agree, $disagree disagree, $residual_dis disagree on a disclosed residual -----"
[ "$disagree" -eq 0 ]
