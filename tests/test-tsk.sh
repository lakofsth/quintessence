#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-tsk.sh — the tsk task broker's contract: run/wait/rc/status/stop/clean lifecycle, rc
# propagation, duplicate-name refusal, and input validation. State isolates via TSK_STATE; the
# units themselves are real `systemd-run --user` transients (that ownership IS the product), so
# names are suffixed with this test's PID and every started unit is stopped on exit. On a host
# with no systemd user manager (e.g. a CI container) the suite SKIPS loudly and passes — tsk
# cannot function there and says so itself.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
TSK="$ENGINE/tsk"
TMP="$(mktemp -d)"
export TSK_STATE="$TMP/state"
J="qqtest-$$"   # unique name prefix so parallel/aborted runs never collide on the user manager
cleanup() {
  systemctl --user stop "tsk-$J-dup.service" >/dev/null 2>&1
  systemctl --user stop "tsk-$J-ok.service" "tsk-$J-rc.service" "tsk-$J-argv.service" >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

if ! systemctl --user is-system-running >/dev/null 2>&1 && ! systemctl --user list-units >/dev/null 2>&1; then
  echo "SKIP: no systemd user manager on this host — tsk cannot run here (all 0 assertions vacuous)."
  echo "tsk: 0 failed"
  exit 0
fi

# 1. happy path: run, wait, rc 0, DONE status
out="$("$TSK" run "$J-ok" -- true 2>&1)"; rc=$?
[ $rc -eq 0 ] && printf '%s' "$out" | grep -q "started tsk-$J-ok" \
  && ok "run starts a job and says so" || no "run failed (rc=$rc): $out"
"$TSK" wait "$J-ok" 15 >/dev/null 2>&1; rc=$?
[ $rc -eq 0 ] && ok "wait returns the job's rc (0)" || no "wait rc=$rc for a true job"
[ "$("$TSK" rc "$J-ok")" = "0" ] && ok "rc prints 0" || no "rc did not print 0"
"$TSK" status "$J-ok" | grep -q "DONE rc=0" && ok "status shows DONE rc=0" || no "status wrong for done job"

# 2. rc propagation: a job exiting 7 surfaces 7 through wait and rc
"$TSK" run "$J-rc" -- bash -c 'exit 7' >/dev/null 2>&1
"$TSK" wait "$J-rc" 15 >/dev/null 2>&1; rc=$?
[ $rc -eq 7 ] && ok "wait propagates nonzero job rc (7)" || no "wait rc=$rc, wanted 7"
[ "$("$TSK" rc "$J-rc")" = "7" ] && ok "rc prints 7" || no "rc did not print 7"

# 3. log: the job's output is captured and printed
"$TSK" log "$J-ok" 2>/dev/null | grep -q "\[tsk\] exit rc=0" && ok "log shows the exit marker" || no "log missing exit marker"

# 4. duplicate name while running is refused; stop ends it
"$TSK" run "$J-dup" -- sleep 30 >/dev/null 2>&1 || no "could not start the long job"
out="$("$TSK" run "$J-dup" -- true 2>&1)"; rc=$?
[ $rc -ne 0 ] && printf '%s' "$out" | grep -q "already running" \
  && ok "duplicate name refused while running" || no "duplicate name not refused (rc=$rc): $out"
"$TSK" stop "$J-dup" >/dev/null 2>&1
sleep 1
systemctl --user is-active --quiet "tsk-$J-dup.service" && no "stop left the unit running" || ok "stop ends the unit"

# 5. input validation: bad name / no command
"$TSK" run "bad name!" -- true >/dev/null 2>&1 && no "accepted an invalid name" || ok "invalid name refused"
"$TSK" run "$J-nocmd" >/dev/null 2>&1 && no "accepted a run with no command" || ok "missing command refused"

# 6. wait on an unknown job dies rather than hanging
"$TSK" wait "$J-ghost" 5 >/dev/null 2>&1 && no "wait on unknown job succeeded" || ok "wait on unknown job errors out"

# 7. spaced argv survives intact: a command arg containing a space must reach the job
#    as one argument, not word-split (regression for glue-tsk-argv-split)
argvdir="$TMP/argvtest"
mkdir -p "$argvdir"
"$TSK" run "$J-argv" -- touch "$argvdir/a b.txt" >/dev/null 2>&1
"$TSK" wait "$J-argv" 15 >/dev/null 2>&1
if [ "$(ls -1 "$argvdir" 2>/dev/null | wc -l)" -eq 1 ] && [ -f "$argvdir/a b.txt" ]; then
  ok "spaced arg reaches the job as one file (space preserved in name)"
else
  no "spaced arg was split (dir contents: $(ls -1 "$argvdir" 2>/dev/null | tr '\n' ' '))"
fi

# 8. clean removes the job's state files
"$TSK" clean "$J-ok"
ls "$TSK_STATE/$J-ok".* >/dev/null 2>&1 && no "clean left state files" || ok "clean removes state files"
"$TSK" clean --all-finished
ls "$TSK_STATE"/*.done >/dev/null 2>&1 && no "clean --all-finished left done markers" || ok "clean --all-finished sweeps"

echo "tsk: $fail failed, $pass passed"
exit $((fail > 0))
