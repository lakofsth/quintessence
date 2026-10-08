#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# consistency-audit.sh — the CHANGE-GATED Tier-2/3 audit runner (fired daily by a systemd timer).
# It spends the (LLM) audit pass ONLY when the corpus has actually moved enough since the last
# audit — else it exits cheap. So a daily timer + this gate = "according to number of changes":
# floor ≤1 run/day, escalate on churn (≥THRESH changes), ceiling forces a run after MAX_DAYS quiet.
# The LLM agent (audit-runbook.md) is flag-only; this script merges its findings into the AUDIT
# section of the pending-findings queue and advances the baseline. Heavy work lives in the model;
# this stays dumb. See HEAD continuity-consistency.
#
# Usage: consistency-audit.sh [--force]      (--force runs regardless of the gate)
set -uo pipefail

THRESH="${AUDIT_THRESH:-5}"          # run if (qq commits + changed memory files) >= THRESH
MAX_DAYS="${AUDIT_MAX_DAYS:-10}"     # ...or if it's been this many days regardless
MODEL="${AUDIT_MODEL:-claude-opus-4-8}"   # judgment-heavy + low-frequency → strongest model; override via env
CLAUDE="${CLAUDE_BIN:-$(command -v claude || echo claude)}"

ENGINE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"   # this script's dir (ships the runbook)
. "$ENGINE/qq-config.sh"
QDIR="${QUINTESSENCE_DIR:-$HOME/quintessence}"
MEMDIR="${QQ_MEMDIR:-$HOME/.quintessence-memory}"
# Runtime state goes to QQ_STATE_DIR (NOT beside the engine) so a read-only / packaged engine
# dir stays pristine and the audit works wherever the dist is installed. RUNBOOK is shipped.
STATE="${QQ_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/quintessence}"; mkdir -p "$STATE"
BASELINE="$STATE/.audit-baseline"
MANIFEST="$STATE/.audit-memory-manifest"
RUNBOOK="$ENGINE/audit-runbook.md"
TMPF="$STATE/.audit-findings.tmp"
LOG="$STATE/audit.log"
. "$ENGINE/findings.sh"

log(){ printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }
force=0; [ "${1:-}" = "--force" ] && force=1

# B4 proc-vs-disk probe: mechanical + cheap (systemctl show + stat),
# so it runs on EVERY fire, BEFORE the change gate — the gate protects the LLM spend, not this.
# Findings land in the auto-resolve PROC section of the same pending queue; a service restart
# clears them on the next fire. Fail-soft: a probe failure never blocks the audit.
python3 "$ENGINE/proc-probe.py" --write >> "$LOG" 2>&1 || log "proc-probe: rc=$? (fail-soft, audit continues)"

# The baseline is DATA, read as key=value — never sourced. It is same-user state, but a file
# executed as shell is the pattern the rest of this tree refuses (qq-config.sh parses its
# dotenv for the same reason). Only the two keys the audit writes are read; anything else in
# the file is ignored.
audit_read_baseline() {
  local line key val
  ts=0; qqhead=""
  [ -f "$BASELINE" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    case "$key" in
      ts)     case "$val" in ''|*[!0-9]*) ;; *) ts="$val" ;; esac ;;
      qqhead) case "$val" in ''|*[!0-9a-fA-F]*) ;; *) qqhead="$val" ;; esac ;;
    esac
  done < "$BASELINE"
  return 0
}

now=$(date +%s)
audit_read_baseline
last_ts="${ts:-0}"; last_head="${qqhead:-}"
days_since=$(( (now - last_ts) / 86400 ))

# change signal: quintessence commits since baseline + changed memory files since baseline
qq_commits=0
if [ -n "$last_head" ] && git -C "$QDIR" cat-file -e "$last_head" 2>/dev/null; then
  qq_commits=$(git -C "$QDIR" rev-list --count "$last_head"..HEAD 2>/dev/null || echo 0)
fi
curman=$(mktemp)
( cd "$MEMDIR" 2>/dev/null && sha256sum ./*.md 2>/dev/null ) | sort -k2 > "$curman"
if [ -f "$MANIFEST" ]; then
  mem_changes=$(diff "$MANIFEST" "$curman" 2>/dev/null | grep -E '^[<>]' | awk '{print $3}' | sort -u | grep -c . || true)
else
  mem_changes=$(grep -c . "$curman" || true)
fi
changes=$(( qq_commits + mem_changes ))

# gate
if   [ "$force" = 1 ];                then reason="forced"
elif [ "$changes" -ge "$THRESH" ];    then reason="changes=$changes>=$THRESH"
elif [ "$days_since" -ge "$MAX_DAYS" ];then reason="quiet ${days_since}d>=${MAX_DAYS}d"
else reason=""; fi
log "gate: qq_commits=$qq_commits mem_changes=$mem_changes changes=$changes days_since=$days_since -> ${reason:-SKIP}"
if [ -z "$reason" ]; then
  echo "audit: skip (changes=$changes/<$THRESH, ${days_since}d/<${MAX_DAYS}d)"
  rm -f "$curman"; exit 0
fi

# run the LLM audit (it writes $TMPF as its completion signal)
rm -f "$TMPF"
echo "audit: running — $reason (model=$MODEL)"; log "running audit: $reason"
# Claude-tool layer: an allowlist SCOPED to what the runbook asks of the agent — read anything,
# run the four qq read verbs, and edit the one findings file. The agent reads HEADs and memory
# facts any session can write, so a bare `Bash`/`Write` grant was the blast radius of a planted
# instruction (posture review 2026-10-08, H2). `Edit(path)` is the rule shape Claude Code matches
# for file writes (a `Write(path)` rule is inert). No systemd unit ships with this tree; if you
# run it from one, add ReadOnlyPaths=/NoNewPrivileges= there — this allowlist is the only floor
# the tree itself provides. If the audit cannot complete, widen the allowlist here deliberately
# and say why. INVARIANTS.md: the-unattended-audit-holds-only-the-tools-its-runbook-names.
AUDIT_TOOLS="Read,Grep,Glob,Bash(qq check:*),Bash(qq menu),Bash(qq show:*),Bash(qq brief:*),Bash(qq fact:*),Bash(qq findings:*),Edit($TMPF)"
timeout 600 "$CLAUDE" --allowedTools "$AUDIT_TOOLS" --model "$MODEL" -p "$(cat "$RUNBOOK")" >> "$LOG" 2>&1 \
  || log "claude exited non-zero (rc=$?)"

if [ -f "$TMPF" ]; then
  sed '/^[[:space:]]*$/d' "$TMPF" | findings_set_section AUDIT
  cnt=$(grep -c . "$TMPF" 2>/dev/null || echo 0)
  echo "audit: done — $cnt finding(s) written to AUDIT section"
  log "audit complete: $cnt finding(s)"
  # advance baseline ONLY on a completed run
  printf 'ts=%s\nqqhead=%s\n' "$now" "$(git -C "$QDIR" rev-parse HEAD 2>/dev/null || echo '')" > "$BASELINE"
  mv "$curman" "$MANIFEST"
  rm -f "$TMPF"
else
  echo "audit: FAILED — agent wrote no findings file; baseline unchanged (will retry next fire)"
  log "audit FAILED: no findings file; baseline NOT advanced"
  rm -f "$curman"
  exit 1
fi
