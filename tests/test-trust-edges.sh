#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-trust-edges.sh — the shell halves of the 2026-10-08 posture-review pins (INVARIANTS.md:
# a-hook-never-decides-permission, the-unattended-audit-holds-only-the-tools-its-runbook-names,
# config-set-writes-one-line-of-one-registered-shape (export filter), and
# generated-shell-quotes-every-interpolated-path). Hermetic: own HOME/QQ_CONFIG/QQ_STATE_DIR.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

# ---- no-permission-decision: every hook that prints hookSpecificOutput ----------------------
emitters=$(grep -lE 'hookSpecificOutput' "$ENGINE"/*.sh "$ENGINE"/hooks/*.sh 2>/dev/null)
[ -n "$emitters" ] || no "no-permission-decision: the emitter enumeration found nothing (instrument broken)"
bad=""
for f in $emitters; do grep -vE '^[[:space:]]*#' "$f" | grep -q 'permissionDecision' && bad="$bad $f"; done
[ -z "$bad" ] && ok "no-permission-decision: no hook emits a permissionDecision" \
  || no "no-permission-decision: $(echo "$bad" | tr '\n' ' ')"

# live run of prederive-recall.sh: a matching command with a forced hit must emit context only.
export HOME="$TMP/home"; mkdir -p "$HOME"
export QQ_CONFIG="$TMP/config" QQ_STATE_DIR="$TMP/state" QUINTESSENCE_DIR="$TMP/store" QQ_MEMDIR="$TMP/mem"
: > "$QQ_CONFIG"; mkdir -p "$QQ_STATE_DIR" "$QUINTESSENCE_DIR" "$QQ_MEMDIR"
fakebin="$TMP/fakeengine"; mkdir -p "$fakebin"
cp "$ENGINE/prederive-recall.sh" "$ENGINE/qq-config.sh" "$ENGINE/qq-redact.sh" "$fakebin/" 2>/dev/null
cat > "$fakebin/qq-search" <<'EOF'
#!/usr/bin/env bash
printf '  [0.912] (qq) qq show runbook-x › a title\n'
EOF
chmod +x "$fakebin/qq-search"
payload='{"tool_input":{"command":"sudo iptables -A INPUT -p tcp --dport 22 -j ACCEPT && echo done"},"session_id":"s1","transcript_path":""}'
out=$(printf '%s' "$payload" | bash "$fakebin/prederive-recall.sh" 2>/dev/null)
if [ -n "$out" ]; then
  printf '%s' "$out" | jq -e '.hookSpecificOutput.additionalContext' >/dev/null 2>&1 \
    && ok "prederive-recall: emits additionalContext on a hit" || no "prederive-recall: no additionalContext on a hit"
  printf '%s' "$out" | jq -e '.hookSpecificOutput | has("permissionDecision")' 2>/dev/null | grep -q true \
    && no "prederive-recall: still emits permissionDecision" || ok "prederive-recall: no permissionDecision in the live output"
else
  no "prederive-recall: produced no output on a forced hit (harness broken, nothing pinned)"
fi

# ---- audit-tools-scoped / audit-baseline-parsed ------------------------------------------------
tools=$(grep -oE '^AUDIT_TOOLS="[^"]*"' "$ENGINE/consistency-audit.sh" | head -1)
grep -qE -- '--allowedTools +"\$AUDIT_TOOLS"' "$ENGINE/consistency-audit.sh" \
  && ok "audit-tools-scoped: the claude call takes its tools from AUDIT_TOOLS" \
  || no "audit-tools-scoped: the claude call does not use AUDIT_TOOLS (instrument reads the wrong list)"
if [ -z "$tools" ]; then no "audit-tools-scoped: no AUDIT_TOOLS assignment found"; else
  printf '%s' "$tools" | tr ',' '\n' | grep -qE '(^|")Bash("|$)' \
    && no "audit-tools-scoped: a bare Bash grant" || ok "audit-tools-scoped: no bare Bash grant"
  printf '%s' "$tools" | tr ',' '\n' | grep -qE '(^|")Write("|$)' \
    && no "audit-tools-scoped: a bare Write grant" || ok "audit-tools-scoped: no bare Write grant"
fi
grep -qE '(^|[;&|[:space:]])(\.|source) +"?\$BASELINE"?' "$ENGINE/consistency-audit.sh" \
  && no "audit-baseline-parsed: the baseline is still sourced" || ok "audit-baseline-parsed: the baseline is not sourced"
# the parser must read ts/qqhead and ignore anything else
mkdir -p "$TMP/state2"; printf 'ts=1700000000\nqqhead=abc123\nPATH=/evil\n' > "$TMP/state2/.audit-baseline"
parsed=$(cd "$ENGINE" && QQ_STATE_DIR="$TMP/state2" bash -c '
  BASELINE="$QQ_STATE_DIR/.audit-baseline"
  . <(sed -n "/^audit_read_baseline()/,/^}/p" consistency-audit.sh)
  audit_read_baseline; printf "%s %s %s" "${ts:-}" "${qqhead:-}" "$PATH"' 2>/dev/null)
case "$parsed" in "1700000000 abc123 /evil"*) no "audit-baseline-parsed: PATH leaked from the baseline" ;;
  "1700000000 abc123 "*) ok "audit-baseline-parsed: ts/qqhead read, other keys ignored" ;;
  *) no "audit-baseline-parsed: parser gave '$parsed'" ;; esac

# ---- config-export-filter: only QQ_* and QUINTESSENCE_DIR leave the file -----------------------
printf 'QQ_DIGEST_PIN=keepme\nQUINTESSENCE_DIR=%s\nPYTHONPATH=/evil\nBASH_ENV=/evil\nLD_PRELOAD=/evil\n' "$TMP/store" > "$QQ_CONFIG"
got=$(env -i PATH="$PATH" HOME="$HOME" QQ_CONFIG="$QQ_CONFIG" bash -c ". '$ENGINE/qq-config.sh'; printf '%s|%s|%s|%s|%s' \"\${QQ_DIGEST_PIN:-}\" \"\${QUINTESSENCE_DIR:-}\" \"\${PYTHONPATH:-unset}\" \"\${BASH_ENV:-unset}\" \"\${LD_PRELOAD:-unset}\"")
[ "$got" = "keepme|$TMP/store|unset|unset|unset" ] && ok "config-export-filter: QQ_* and QUINTESSENCE_DIR exported, other identifiers not" \
  || no "config-export-filter: got '$got'"

# ---- tsk-state-quoted: a state dir with a quote and a dollar survives into the runner ----------
weird="$TMP/st\"ate \$x"; mkdir -p "$weird"
if command -v systemd-run >/dev/null 2>&1; then
  # we only need the generated runner, not a started job: run `tsk` with a stub systemd-run on PATH
  stub="$TMP/stubbin"; mkdir -p "$stub"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$stub/systemd-run"; chmod +x "$stub/systemd-run"
  printf '#!/usr/bin/env bash\nif [ "$1" = is-active ]; then echo inactive; exit 3; fi; exit 0\n' > "$stub/systemctl"; chmod +x "$stub/systemctl"
  ( cd "$TMP" && PATH="$stub:$PATH" TSK_STATE="$weird" bash "$ENGINE/tsk" run tq1 true >/dev/null 2>&1 )
  if [ -f "$weird/tq1.run" ]; then
    bash -n "$weird/tq1.run" 2>/dev/null && ok "tsk-state-quoted: generated runner parses with a quote+dollar state dir" \
      || no "tsk-state-quoted: generated runner does not parse"
    # run the generated runner itself (its command is `true`): the rc must land in the weird dir
    rm -f "$weird/tq1.rc"; ( cd "$TMP" && bash "$weird/tq1.run" >/dev/null 2>&1 )
    [ "$(cat "$weird/tq1.rc" 2>/dev/null)" = "0" ] && ok "tsk-state-quoted: the runner writes its rc into the quote+dollar state dir" \
      || no "tsk-state-quoted: the runner did not write rc=0 into the state dir"
  else
    no "tsk-state-quoted: no runner written (harness broken)"
  fi
else
  ok "tsk-state-quoted: skipped (no systemd-run on this host)"
fi

# ---- setup-hook-quoted: the wired command survives a dist path with a quote and a dollar -------
qdist="$TMP/di\"st \$x"; mkdir -p "$qdist/hooks"
( cd "$ENGINE" && tar --exclude=.git --exclude=tests -cf - . ) | ( cd "$qdist" && tar -xf - )
mkdir -p "$TMP/wirehome/.claude"; printf '{}' > "$TMP/wirehome/.claude/settings.json"
( cd "$qdist" && env -i PATH="$PATH" HOME="$TMP/wirehome" QQ_CONFIG="$TMP/wirecfg" QUINTESSENCE_DIR="$TMP/wirestore" QQ_MEMDIR="$TMP/wiremem" BIN_DIR="$TMP/wirebin" CLAUDE_SETTINGS="$TMP/wirehome/.claude/settings.json" \
    bash setup.sh --wire-claude --no-self-check >/dev/null 2>&1 )
cmd=$(jq -r '.hooks.PreToolUse[0].hooks[0].command // empty' "$TMP/wirehome/.claude/settings.json" 2>/dev/null)
if [ -n "$cmd" ]; then
  # the command must be a shell line that, when run, invokes exactly that path
  probe=$(cmd="$cmd" bash -c 'eval "set -- ${cmd#bash }"; printf "%s" "$1"' 2>/dev/null)
  [ "$probe" = "$qdist/prederive-recall.sh" ] && ok "setup-hook-quoted: wired command resolves to the dist path verbatim" \
    || no "setup-hook-quoted: wired command resolves to '$probe'"
else
  no "setup-hook-quoted: no PreToolUse hook wired (harness broken)"
fi

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
