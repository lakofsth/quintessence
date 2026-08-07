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
# `tsk` itself reads no config file (verified at source, round-1 incognito review — an earlier
# version of this comment claimed it sources qq-config.sh; it does not). The pin below stays as
# the uniform one-line belt every suite carries: it costs nothing and keeps an inherited
# QQ_CONFIG from ever mattering here, even if tsk grows a config read later.
export QQ_CONFIG="$TMP/config"; : > "$QQ_CONFIG"
J="qqtest-$$"   # unique name prefix so parallel/aborted runs never collide on the user manager
cleanup() {
  systemctl --user stop "tsk-$J-dup.service" >/dev/null 2>&1
  systemctl --user stop "tsk-$J-ok.service" "tsk-$J-rc.service" "tsk-$J-argv.service" "tsk-$J-emptyrc.service" "tsk-$J-norun.service" "tsk-$J-donekeep.service" >/dev/null 2>&1
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

# 4b. a stopped job still gets its bookkeeping: systemctl stop kills the runner's whole
#     cgroup before the runner can write rc/done, so tsk stop must write them itself —
#     anything gating on the done-marker would otherwise hang on a stopped job
if [ -f "$TSK_STATE/$J-dup.done" ] && [ "$(cat "$TSK_STATE/$J-dup.rc" 2>/dev/null)" = "143" ]; then
  ok "stopped job carries done-marker + rc 143"
else
  no "stopped job unmarked (done: $([ -f "$TSK_STATE/$J-dup.done" ] && echo present || echo absent), rc: $(cat "$TSK_STATE/$J-dup.rc" 2>/dev/null || echo none))"
fi
"$TSK" wait "$J-dup" 5 >/dev/null 2>&1; rc=$?
[ $rc -eq 143 ] && ok "wait on a stopped job returns its rc (143)" || no "wait on stopped job rc=$rc, wanted 143"

# 4c. a FAILED stop must not fabricate completion (round-1 review F1): stop of a name with
#     state on disk but no unit exits nonzero and writes NO marker — a stop that did not
#     end the unit has nothing truthful to record
: > "$TSK_STATE/$J-ghoststop.run"
"$TSK" stop "$J-ghoststop" >/dev/null 2>&1 && no "stop of a unit-less job exited 0" || ok "failed stop exits nonzero"
[ -f "$TSK_STATE/$J-ghoststop.done" ] && no "failed stop fabricated a done-marker" || ok "failed stop writes no marker"
rm -f "$TSK_STATE/$J-ghoststop".*

# 4d. an empty runner-written rc is not treated as authoritative on stop (round-1 review
#     F3): a zero-length .rc is replaced by 143, so wait never exits on an empty string
"$TSK" run "$J-emptyrc" -- sleep 30 >/dev/null 2>&1
: > "$TSK_STATE/$J-emptyrc.rc"
"$TSK" stop "$J-emptyrc" >/dev/null 2>&1
[ "$(cat "$TSK_STATE/$J-emptyrc.rc" 2>/dev/null)" = "143" ] && ok "empty rc replaced by 143 on stop" || no "empty rc survived stop (rc file: '$(cat "$TSK_STATE/$J-emptyrc.rc" 2>/dev/null)')"

# 4e. wait on a done job whose rc file is empty degrades to rc 1 with the die path,
#     instead of crashing on `exit ""` (empty-rc robustness, same class as 4d)
: > "$TSK_STATE/$J-emptywait.rc"; touch "$TSK_STATE/$J-emptywait.done"
"$TSK" wait "$J-emptywait" 5 >/dev/null 2>&1; rc=$?
[ $rc -eq 1 ] && ok "wait on empty rc degrades to 1" || no "wait on empty rc returned $rc (want 1)"

# 4f. a run whose systemd-run fails leaves NO state behind (round-1 review F2), so .run
#     present really does mean the job started and a later stop cannot mark a ghost
stubdir="$TMP/stub"; mkdir -p "$stubdir"
printf '#!/bin/bash\nexit 1\n' > "$stubdir/systemd-run"; chmod +x "$stubdir/systemd-run"
PATH="$stubdir:$PATH" "$TSK" run "$J-failrun" -- true >/dev/null 2>&1 && no "run with failing systemd-run exited 0" || ok "failed systemd-run refused"
ls "$TSK_STATE/$J-failrun".* >/dev/null 2>&1 && no "failed run left state behind" || ok "failed run leaves no state"

# 4g. the started-evidence conjunct is load-bearing (round-2 review F-B): a successful stop
#     of a live unit with NO .run record writes no marker — .run present must MEAN started,
#     and the stop side is the consumer of that guarantee
"$TSK" run "$J-norun" -- sleep 30 >/dev/null 2>&1
rm -f "$TSK_STATE/$J-norun.run"
"$TSK" stop "$J-norun" >/dev/null 2>&1
[ -f "$TSK_STATE/$J-norun.done" ] && no "stop marked a job with no .run record" || ok "no .run record, no marker (started-evidence conjunct)"

# 4h. the done-marker conjunct is load-bearing (round-2 review F-B): a successful stop with
#     .done already present touches nothing — the empty .rc would be rewritten to 143 if the
#     guard's [ ! -f .done ] conjunct were dropped, so emptiness staying empty is the pin.
#     (The remaining conjunct, ! is-active after a successful synchronous stop, is belt that
#     cannot be exercised on a healthy user manager — named here rather than silently unpinned.)
"$TSK" run "$J-donekeep" -- sleep 30 >/dev/null 2>&1
: > "$TSK_STATE/$J-donekeep.rc"; touch "$TSK_STATE/$J-donekeep.done"
"$TSK" stop "$J-donekeep" >/dev/null 2>&1
[ -s "$TSK_STATE/$J-donekeep.rc" ] && no "stop rewrote bookkeeping despite done-marker present" || ok "done-marker present, stop leaves bookkeeping alone"

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
