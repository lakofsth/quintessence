#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-tsk.sh — the tsk task broker's contract: run/wait/rc/status/stop/clean lifecycle, rc
# propagation, duplicate-name refusal, and input validation. State isolates via TSK_STATE; the
# units themselves are real `systemd-run --user` transients (that ownership IS the product), so
# names are suffixed with this test's PID and every started unit is stopped on exit. On a host
# with no systemd user manager the suite SKIPS the manager-bound half loudly and passes the
# hermetic ones — tsk cannot function there and says so itself.
#
# THIS PROJECT'S CI IS NOT SUCH A HOST, and an earlier version of this comment said it was
# ("e.g. a CI container"). GitHub's ubuntu-latest runs a systemd user manager, so the gate below
# does not fire there and the manager-bound assertions DO execute: measured `tsk: 0 failed,
# 25 passed` in CI run 31167177882, `44 passed` in 31186975559 and `45 passed` in 31276284827 —
# the full counts, not the hermetic-only subset. Three consecutive review rounds reasoned from
# their own manager-less sandbox to a conclusion about CI without reading a CI log (ledger D115).
# The skip path is for containers and sandboxes that lack a manager, not for our pipeline.
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
  systemctl --user stop "tsk-$J-ok.service" "tsk-$J-rc.service" "tsk-$J-argv.service" "tsk-$J-emptyrc.service" "tsk-$J-norun.service" "tsk-$J-donekeep.service" "tsk-$J-race.service" >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

# The hermetic assertions run BEFORE the manager gate: they exercise pure file/argument
# handling and were measured passing with systemctl absent entirely, so hiding them behind
# the SKIP left them unrun on any manager-less host (follow-up round F3). Deliberately no
# count here — the SKIP message below prints the live one, and a hand-written total in a
# comment is the drift this project keeps ledgering (D43, D90). What the round got wrong was
# the reach, not the fix: it cited CI as a manager-less host and CI is not one (see the header
# and ledger D115). The relocation is still right — it covers developer sandboxes, containers,
# and any future runner without a manager — it just was not protecting the pipeline.
mkdir -p "$TSK_STATE"   # these run before any tsk call, so the state dir does not exist yet;
                        # without it H1's setup writes fail silently and the assertion would
                        # pass via wait's unrelated "not running" die path
# H1 (hermetic — runs BEFORE the manager gate; was 4i). wait on a done job whose rc file holds a non-numeric value degrades to rc 1 with the
#     die path instead of crashing on exit (round-2/3 review F-C; needs external tampering
#     to arise, but the crash shape is a new-guard boundary worth pinning)
#     Enumerated across the ENVELOPE, not one example, because the interesting failures are all
#     at the edges and each earlier narrowing of this guard stopped one step short (ledger D96,
#     D97: `-f`->`-s`, then `-n`->`^[0-9]+$`, and an all-digit out-of-range value still reached
#     `exit`). Two distinct wrong behaviours are pinned here, both MEASURED on this bash rather
#     than reasoned about: `exit 99999999999999999999` dies "numeric argument required" rc 2 —
#     the very crash this guard's comment claims to have removed — and `exit 300` silently
#     WRAPS to 44, `exit 999` to 231, `exit -1` to 255, reporting a wrong rc as if it were real.
#     The silent wrap is the worse half. Note `exit 007` is 7, not octal 7-as-8: leading zeros
#     are decimal to `exit`, so 007 is a legal status and stays one.
_rcenv_bad=0
for _c in "abc:1" ":1" " :1" "0:0" "7:7" "007:7" "255:255" "256:1" "300:1" "1000:1" "99999999999999999999:1" "-1:1"; do
  _v="${_c%:*}"; _want="${_c##*:}"
  printf '%s' "$_v" > "$TSK_STATE/$J-rcenv.rc"; touch "$TSK_STATE/$J-rcenv.done"
  _err=$("$TSK" wait "$J-rcenv" 5 2>&1 >/dev/null); _got=$?
  [ "$_got" = "$_want" ] || { _rcenv_bad=$((_rcenv_bad+1)); no "rc file '$_v' -> wait exited $_got (want $_want)"; }
  # the degrade must be SILENT: a range check written with `[` alone leaks
  # "integer expression expected" to the user's stderr on the 20-digit case
  case "$_err" in *"integer expression expected"*|*"numeric argument required"*)
    _rcenv_bad=$((_rcenv_bad+1)); no "rc file '$_v' leaked a shell diagnostic: $_err" ;;
  esac
done
rm -f "$TSK_STATE/$J-rcenv".{rc,done}
[ "$_rcenv_bad" -eq 0 ] && ok "wait degrades every out-of-envelope rc to 1, silently, and passes 0-255 through"

# H2 (hermetic — runs BEFORE the manager gate; was 4j). every name-taking verb applies run's charset check (round-1/3 review note: stop now
#     WRITES under the unvalidated name; traversal was probed unreachable only because
#     systemctl happened to reject the unit name — validate at our own boundary instead)
for v in status wait log rc stop clean; do
  out=$("$TSK" "$v" '../evil' 2>&1); rc=$?
  if [ $rc -eq 1 ] && printf '%s' "$out" | grep -q "name must be"; then
    ok "verb '$v' refuses a non-charset name"
  else
    no "verb '$v' accepted '../evil' (rc=$rc)"
  fi
done
# H3 (hermetic). an orphaned runner temp (tsk killed between write and rename) is
# reclaimable: clean <name> sweeps <name>:run.tmp.* (follow-up rounds F2/F2b — the ':'
# separator is FORBIDDEN in job names, so the sweep glob can never reach another job)
: > "$TSK_STATE/$J-litter:run.tmp.999"
"$TSK" clean "$J-litter"
[ -e "$TSK_STATE/$J-litter:run.tmp.999" ] && no "clean left an orphaned runner temp" || ok "clean sweeps orphaned runner temps"

# H3b (hermetic). the sweep must not swallow a LEGAL job whose name merely extends the
# cleaned one (reset-round F2: an unanchored .run.tmp* glob deleted job "X.run.tmp1"'s
# whole state on "tsk clean X")
: > "$TSK_STATE/$J-x.run.tmp1.run"; : > "$TSK_STATE/$J-x.run.tmp1.rc"
"$TSK" clean "$J-x"
if [ -e "$TSK_STATE/$J-x.run.tmp1.run" ] && [ -e "$TSK_STATE/$J-x.run.tmp1.rc" ]; then
  ok "clean leaves a longer-named job's state alone"
else
  no "clean deleted another job's state via the temp glob"
fi
rm -f "$TSK_STATE/$J-x.run.tmp1".*

# H3c (hermetic). clean --all-finished reclaims STALE orphan temps by age (reset-round
# F4: an orphan's own .done was deleted by run's pre-flight rm, so the *.done iteration
# can never reach it) while leaving a fresh temp — possibly a concurrent run mid-write —
# untouched
: > "$TSK_STATE/$J-old:run.tmp.1"; touch -d '2 hours ago' "$TSK_STATE/$J-old:run.tmp.1"
: > "$TSK_STATE/$J-new:run.tmp.2"
"$TSK" clean --all-finished
[ -e "$TSK_STATE/$J-old:run.tmp.1" ] && no "all-finished left a stale orphan temp" || ok "all-finished reclaims stale orphan temps"
[ -e "$TSK_STATE/$J-new:run.tmp.2" ] && ok "all-finished spares a fresh temp" || no "all-finished ate a fresh (in-flight) temp"
rm -f "$TSK_STATE/$J-new:run.tmp.2"

# H3d (hermetic). the SAME age sweep, with TSK_STATE reached through a SYMLINK — the one
# condition H3c cannot see, because its own TSK_STATE is a real directory (follow-up round
# F1, ledger D114). `find` defaults to -P and will not descend a symlink named as a starting
# point, so the sweep matched nothing, deleted nothing and exited 0: the reclamation this
# code exists to provide, silently absent. H3c IS this pin's positive control — same command,
# same name shape, same age, real directory — so a red here is about the symlink and nothing
# else. A symlinked state dir is ordinary: TSK_STATE is an operator env var.
_sl_real="$TMP/slreal"; _sl_link="$TMP/sllink"
mkdir -p "$_sl_real"; ln -s "$_sl_real" "$_sl_link"
: > "$_sl_real/$J-sl:run.tmp.7"; touch -d '3 hours ago' "$_sl_real/$J-sl:run.tmp.7"
# assert the sweep has work before asking whether it did it: an empty state dir would let a
# missing-file check report success for the wrong reason (ledger D59 — zero matches is
# instrument failure, not a pass)
if [ -e "$_sl_real/$J-sl:run.tmp.7" ]; then
  TSK_STATE="$_sl_link" "$TSK" clean --all-finished
  [ -e "$_sl_real/$J-sl:run.tmp.7" ] \
    && no "the age sweep reclaims nothing when TSK_STATE is a symlink (find -P will not descend a symlinked starting point)" \
    || ok "the age sweep reclaims through a symlinked TSK_STATE"
else
  no "H3d could not plant its fixture — the assertion below would have been vacuous"
fi
rm -rf "$_sl_real" "$_sl_link"

# H4 (hermetic). the help surface is the verb list and nothing more (reset-round F1: a
# column-0 rationale comment leaked into the \`^#\` harvest and printed implementation
# commentary on every bad-verb invocation; nothing pinned that surface)
"$TSK" nonsense-verb 2>&1 | tail -1 | grep -q "tsk clean" \
  && ok "help output ends at the verb list" || no "help output leaks past the verb list"

# H5 (hermetic). the runner script's WRITE is checked, not only its rename (ledger D116).
#     Installing a runner is two steps and only the second was hardened. A SHORT write —
#     ENOSPC, quota — leaves a TRUNCATED script that `chmod +x` and the checked `mv` then
#     install without complaint; systemd-run executes it, so the job never runs its command,
#     never writes rc/done, and a watcher gating on the marker waits forever while `run`
#     reports success. tsk sets no `-e`, so cat's nonzero status was simply discarded.
# The stub reproduces the dangerous STATE rather than a bare failure: it emits a truncated
# script on stdout — which the shell has already redirected into the temp — and then reports
# the write error, which is what cat does on ENOSPC. `cat` here takes no arguments; the
# redirection is the caller's, so a stdout-writing stub is the faithful shape.
# This assertion is hermetic BECAUSE it pins the MESSAGE: without the fix a manager-less host
# reaches systemd-run and dies "systemd-run failed", and a host WITH a manager launches the
# truncated script and exits 0 — both are red here, for the right reason either way.
_h5stub="$TMP/h5stub"; mkdir -p "$_h5stub"
printf '#!/bin/bash\nprintf "#!/usr/bin/env bash\\nexec > /dev/null 2>&1\\n"\nexit 1\n' > "$_h5stub/cat"
chmod +x "$_h5stub/cat"
_h5out=$(PATH="$_h5stub:$PATH" "$TSK" run "$J-catfail" -- true 2>&1); _h5rc=$?
if [ "$_h5rc" -ne 0 ] && printf '%s' "$_h5out" | grep -q "could not write runner script"; then
  ok "a short write to the runner script is refused, not installed"
else
  no "a truncated runner script was installed instead of refused (rc=$_h5rc, said: $(printf '%s' "$_h5out" | tail -1))"
fi
if [ -e "$TSK_STATE/$J-catfail.run" ] || [ -e "$TSK_STATE/$J-catfail.cmd" ]; then
  no "a refused write left job state behind"
else
  ok "a refused write leaves no job state"
fi
ls "$TSK_STATE/$J-catfail":run.tmp.* >/dev/null 2>&1 \
  && no "a refused write left its runner temp" || ok "a refused write leaves no runner temp"

# H5b (hermetic). the third step of the same install — chmod — is checked too. Added because
#      mutation caught it going UNPINNED: dropping its check left the whole suite green while
#      H5 stayed red for cat, so the fix would have shipped with two of three steps proven and
#      the middle one resting on nobody's word but the author's (ledger D15 family).
printf '#!/bin/bash\nexit 1\n' > "$_h5stub/chmod"; chmod +x "$_h5stub/chmod"
rm -f "$_h5stub/cat"   # cat must succeed here: this pins the STEP AFTER it
_h5bout=$(PATH="$_h5stub:$PATH" "$TSK" run "$J-chmodfail" -- true 2>&1); _h5brc=$?
if [ "$_h5brc" -ne 0 ] && printf '%s' "$_h5bout" | grep -q "could not make runner script executable"; then
  ok "a runner script that cannot be made executable is refused"
else
  no "an unexecutable runner script was installed instead of refused (rc=$_h5brc, said: $(printf '%s' "$_h5bout" | tail -1))"
fi

# H6 (hermetic). a run that fails to START keeps the previous log AND the previous result.
#     The log half pins a RULING [Thomas, 2026-08-09, ledger D117]: a failed start is when the
#     previous run's output is most worth having, so `.log` is deliberately absent from every
#     cleanup list, and `tsk`'s help says so. The result half pins the D117 ROOT NOTE, closed
#     2026-08-10: rc/done are STASHED pre-flight and restored on a failed start, so log, rc
#     and done all describe the same previous run (the old pre-flight clear left a kept log
#     with no result — `status` said unknown against a readable log). If someone later
#     "tidies" `.log` into a cleanup list or the stash out of the failure path, this goes red
#     and the help line, the code comment and this assertion all get revisited together.
#     Reuses H5's cat stub: the write fails after the stash, so the run dies with no
#     manager involved, which is what keeps this hermetic.
printf 'previous run output\n' > "$TSK_STATE/$J-keeplog.log"
printf '0'   > "$TSK_STATE/$J-keeplog.rc"; : > "$TSK_STATE/$J-keeplog.done"
printf '#!/bin/bash\nprintf "#!/usr/bin/env bash\\n"\nexit 1\n' > "$_h5stub/cat"; chmod +x "$_h5stub/cat"
rm -f "$_h5stub/chmod"
PATH="$_h5stub:$PATH" "$TSK" run "$J-keeplog" -- true >/dev/null 2>&1
if [ -s "$TSK_STATE/$J-keeplog.log" ]; then
  ok "a failed start keeps the previous run's log (ruled: D117)"
else
  no "a failed start deleted the previous run's log — the help line and D117 say it is kept"
fi
if [ "$(cat "$TSK_STATE/$J-keeplog.rc" 2>/dev/null)" = "0" ] && [ -e "$TSK_STATE/$J-keeplog.done" ]; then
  ok "a failed start restores the previous result, consistent with the kept log"
else
  no "a failed start lost the previous result (rc: '$(cat "$TSK_STATE/$J-keeplog.rc" 2>/dev/null)') — the stash should have restored it"
fi
rm -f "$TSK_STATE/$J-keeplog".{log,rc,done,run,cmd} "$TSK_STATE/$J-keeplog":prev.{rc,done}
rm -rf "$_h5stub"

if ! systemctl --user is-system-running >/dev/null 2>&1 && ! systemctl --user list-units >/dev/null 2>&1; then
  echo "SKIP: no systemd user manager on this host — the remaining assertions are manager-bound;"
  echo "      the $((pass+fail)) hermetic ones above (name charset, rc-file degradation) DID run."
  echo "tsk: $fail failed, $pass passed"
  exit $((fail > 0))
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

# 4f2. a failed runner install refuses loudly instead of launching the WRONG script
#      (follow-up round-2 F1): if the temp->runner rename fails, the previous run's
#      script is what systemd-run would execute — run must die before reaching it,
#      and its own temp must not survive
printf '#!/bin/bash\nexit 1\n' > "$stubdir/mv"; chmod +x "$stubdir/mv"
out=$(PATH="$stubdir:$PATH" "$TSK" run "$J-mvfail" -- true 2>&1); rc=$?
rm -f "$stubdir/mv"
if [ $rc -ne 0 ] && printf '%s' "$out" | grep -q "could not install runner"; then
  ok "failed runner install refused loudly"
else
  no "failed runner install not refused (rc=$rc)"
fi
ls "$TSK_STATE/$J-mvfail":run.tmp.* >/dev/null 2>&1 && no "failed install left its runner temp" || ok "failed install leaves no runner temp"
# explicit per-file checks, NOT `ls a.{run,cmd}`: brace expansion always hands ls a
# missing path here (.run is never created when mv fails), and ls exits nonzero on ANY
# missing argument — so the ls form could not fail while .cmd survived (fca2d01-round F-1)
if [ -e "$TSK_STATE/$J-mvfail.run" ] || [ -e "$TSK_STATE/$J-mvfail.cmd" ]; then
  no "failed install left job state behind"
else
  ok "failed install leaves no job state (reset-round F3)"
fi

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

# 4k. a LOST same-name run race must not disarm the live job (round-2 F-A / round-3 F1):
#     stateful stubs recreate the one-roundtrip window — systemctl lies "inactive" exactly
#     once so the raced run passes the duplicate check, then systemd-run fails "unit exists".
#     The loser must keep the live job's .run (stop-marker stays armed) and must have
#     replaced the runner by RENAME (new inode), never truncating the inode the running
#     bash may still be reading.
racedir="$TMP/racestub"; mkdir -p "$racedir"
cat > "$racedir/systemctl" <<STUB
#!/bin/bash
if [ "\$1" = "--user" ] && [ "\$2" = "is-active" ] && [[ "\$*" == *"tsk-$J-race.service"* ]] && [ ! -f "$TMP/race-lied" ]; then
  touch "$TMP/race-lied"; exit 1
fi
exec $(command -v systemctl) "\$@"
STUB
printf '#!/bin/bash\nexit 1\n' > "$racedir/systemd-run"
chmod +x "$racedir/systemctl" "$racedir/systemd-run"
"$TSK" run "$J-race" -- sleep 30 >/dev/null 2>&1
inode_before=$(ls -i "$TSK_STATE/$J-race.run" | awk '{print $1}')
PATH="$racedir:$PATH" "$TSK" run "$J-race" -- true >/dev/null 2>&1 && no "raced run exited 0" || ok "raced run still refused"
[ -f "$TSK_STATE/$J-race.run" ] && ok "lost race keeps the live job's .run" || no "lost race deleted the live job's .run"
if [ -f "$TSK_STATE/$J-race.run" ]; then
  inode_after=$(ls -i "$TSK_STATE/$J-race.run" | awk '{print $1}')
  [ "$inode_before" != "$inode_after" ] && ok "runner rewrite is rename, not in-place truncation" || no "runner truncated in place (inode $inode_before unchanged)"
else
  no "runner missing — inode pin unverifiable"
fi
"$TSK" stop "$J-race" >/dev/null 2>&1
[ -f "$TSK_STATE/$J-race.done" ] && [ "$(cat "$TSK_STATE/$J-race.rc" 2>/dev/null)" = "143" ] \
  && ok "stop after a lost race still marks (end-to-end)" || no "stop after lost race left no marker"

# 4l. same-name starts are SERIALIZED: while one start holds the per-name lock, a second
#     refuses BEFORE touching any state file. Pins the 2026-08-10 command-substitution race:
#     a losing racer used to install its runner over the live job's .run inside the winner's
#     fork-to-first-open window, so the refused caller's command silently ran under the
#     winner's name (reproduced 41/1300 at ambient load).
exec 8>"$TSK_STATE/$J-lockheld:run.lock" && flock -n 8
"$TSK" run "$J-lockheld" -- true >/dev/null 2>&1 && no "start proceeded under a held lock" || ok "held lock refuses a second start"
[ ! -f "$TSK_STATE/$J-lockheld.cmd" ] && [ ! -f "$TSK_STATE/$J-lockheld.run" ] \
  && ok "refused start wrote no state files" || no "refused start wrote .cmd/.run under a held lock"
exec 8>&-

# 4m. a FAILED start RESTORES the previous result instead of destroying it (the D117 root
#     note, closed): rc/done are stashed pre-flight and install_failed puts them back.
printf '7\n' > "$TSK_STATE/$J-keepres.rc"; touch "$TSK_STATE/$J-keepres.done"
PATH="$racedir:$PATH" "$TSK" run "$J-keepres" -- true >/dev/null 2>&1 && no "failed start exited 0" || ok "failed start still refused"
[ "$(cat "$TSK_STATE/$J-keepres.rc" 2>/dev/null)" = "7" ] && [ -f "$TSK_STATE/$J-keepres.done" ] \
  && ok "failed start restored the previous rc/done" || no "failed start destroyed the previous result (rc: '$(cat "$TSK_STATE/$J-keepres.rc" 2>/dev/null)')"
[ -f "$TSK_STATE/$J-keepres:prev.rc" ] && no "stash left behind after restore" || ok "restore consumed the stash"

# 4n. clean refuses/skips a name whose start holds the lock (9a78dfb review finding: an
#     unlocked clean racing the stash window silently destroyed the stash AND the log
#     mid-start — the exact loss the stash exists to prevent).
exec 8>"$TSK_STATE/$J-cleanheld:run.lock" && flock -n 8
printf 'previous log\n' > "$TSK_STATE/$J-cleanheld.log"
"$TSK" clean "$J-cleanheld" >/dev/null 2>&1 && no "clean proceeded under a held start lock" || ok "clean refuses while a start is in flight"
[ -f "$TSK_STATE/$J-cleanheld.log" ] && ok "refused clean deleted nothing" || no "refused clean deleted state anyway"
touch "$TSK_STATE/$J-cleanheld.done"   # finished-looking: the sweep must SKIP it while held
"$TSK" clean --all-finished >/dev/null 2>&1
[ -f "$TSK_STATE/$J-cleanheld.log" ] && ok "sweep skips a name whose start is in flight" || no "sweep deleted a held name's state"
exec 8>&-
"$TSK" clean "$J-cleanheld" >/dev/null 2>&1
[ ! -f "$TSK_STATE/$J-cleanheld.log" ] && ok "clean works once the start lock is free" || no "clean still blocked after lock release"

# 4o. a stash is NOT a job: <name>:prev.done satisfies the sweep's *.done glob, and treating
#     it as a job named "<name>:prev" checked a lock nobody holds and then deleted the REAL
#     stash through the :prev glob term (a34f6ac review finding, execution-confirmed). The
#     scanner's grammar must exclude the forms its own series added to the corpus — so
#     fixture the stash FORM itself. status shares the glob and must not list it either.
printf '3\n' > "$TSK_STATE/$J-stashform:prev.rc"; touch "$TSK_STATE/$J-stashform:prev.done"
# status first: after the sweep the fixture may be gone, and a vanished fixture would let
# the status assertion pass vacuously (a negative control that cannot fail is not a control)
"$TSK" status 2>/dev/null | grep -q "$J-stashform:prev" && no "status lists a stash as a DONE job" || ok "status does not list a stash as a job"
"$TSK" clean --all-finished >/dev/null 2>&1
[ -f "$TSK_STATE/$J-stashform:prev.rc" ] && [ -f "$TSK_STATE/$J-stashform:prev.done" ] \
  && ok "sweep leaves a stash alone (not a job)" || no "sweep consumed a stash as a phantom job"
[ ! -f "$TSK_STATE/$J-stashform:prev:run.lock" ] && ok "no stray lock minted for a phantom name" || no "sweep minted a lock for a phantom ':prev' job"
rm -f "$TSK_STATE/$J-stashform:prev".{rc,done}

# 4p. the stash is a CHECKED write (final-round F1): a failed stash mv must refuse the
#     start, not report started while wait/rc/status answer with the PREVIOUS run's result.
stashstub="$TMP/stashstub"; mkdir -p "$stashstub"
cat > "$stashstub/mv" <<STUB
#!/bin/bash
case "\$*" in *:prev*) exit 1;; esac
exec $(command -v mv) "\$@"
STUB
chmod +x "$stashstub/mv"
printf '9\n' > "$TSK_STATE/$J-ckstash.rc"; touch "$TSK_STATE/$J-ckstash.done"
PATH="$stashstub:$PATH" "$TSK" run "$J-ckstash" -- true >/dev/null 2>&1 && no "run reported started despite a failed stash" || ok "failed stash refuses the start"
[ "$(cat "$TSK_STATE/$J-ckstash.rc" 2>/dev/null)" = "9" ] && ok "old result intact after refused start" || no "old rc lost on failed stash"
systemctl --user is-active --quiet "tsk-$J-ckstash.service" && no "failed stash still started a unit" || ok "no unit started on failed stash"
rm -f "$TSK_STATE/$J-ckstash".{rc,done,cmd,run}

# 4q. a signal-interrupted start RESTORES the stash (final-round F2a): the traps are armed
#     before the stash and call stash_back, so the previous result comes back instead of
#     stranding in :prev where clean would delete it.
sigstub="$TMP/sigstub"; mkdir -p "$sigstub"
printf '#!/bin/bash\nkill -TERM "$PPID"\nexit 0\n' > "$sigstub/chmod"; chmod +x "$sigstub/chmod"
printf '9\n' > "$TSK_STATE/$J-sig.rc"; touch "$TSK_STATE/$J-sig.done"
PATH="$sigstub:$PATH" "$TSK" run "$J-sig" -- true >/dev/null 2>&1
[ "$(cat "$TSK_STATE/$J-sig.rc" 2>/dev/null)" = "9" ] && [ -f "$TSK_STATE/$J-sig.done" ] \
  && ok "interrupted start restored the previous result" || no "interrupted start stranded/lost the previous result (rc: '$(cat "$TSK_STATE/$J-sig.rc" 2>/dev/null)')"
[ ! -f "$TSK_STATE/$J-sig:prev.rc" ] && ok "no stash stranded after the interrupt" || no "stash stranded in :prev after interrupt"
rm -f "$TSK_STATE/$J-sig".{rc,done,cmd,run} "$TSK_STATE/$J-sig":prev.{rc,done}

# 4r. a SIGKILL-stranded stash is INHERITED by the next run as the previous result
#     (final-round F2b), not discarded: on that run's failed start it is restored.
printf '5\n' > "$TSK_STATE/$J-strand:prev.rc"; touch "$TSK_STATE/$J-strand:prev.done"
printf '#!/bin/bash\nexit 1\n' > "$sigstub/systemd-run"; chmod +x "$sigstub/systemd-run"; rm -f "$sigstub/chmod"
PATH="$sigstub:$PATH" "$TSK" run "$J-strand" -- true >/dev/null 2>&1 && no "failed start exited 0" || ok "failed start refused (strand case)"
[ "$(cat "$TSK_STATE/$J-strand.rc" 2>/dev/null)" = "5" ] && [ -f "$TSK_STATE/$J-strand.done" ] \
  && ok "stranded stash inherited and restored" || no "stranded stash discarded instead of inherited (rc: '$(cat "$TSK_STATE/$J-strand.rc" 2>/dev/null)')"
rm -f "$TSK_STATE/$J-strand".{rc,done,cmd,run} "$TSK_STATE/$J-strand":prev.{rc,done}

# 4s. stop coordinates through the start lock (final-round F3): an unlocked stop racing a
#     clean lost its completion marker. Refusal is pinned by MESSAGE — at the unfixed base
#     stop also exits nonzero here (systemctl fails on a nonexistent unit), so the exit
#     code alone is a control that cannot fail.
exec 8>"$TSK_STATE/$J-stoplock:run.lock" && flock -n 8
stoperr=$("$TSK" stop "$J-stoplock" 2>&1); stoprc=$?
[ $stoprc -ne 0 ] && printf '%s' "$stoperr" | grep -q "contended" && ok "stop refuses loudly while the lock is held" || no "stop did not refuse via the lock (rc=$stoprc: $stoperr)"
exec 8>&-

# 4t. COMPLEMENTARY halves are an interrupted stash/restore, not a live result (91331eb
#     review residual): a SIGKILL between the two stash mvs leaves :prev.rc + live .done;
#     the supersede OR-check must not read the stale lone .done as a completed job and
#     discard the half-stash holding the real rc. Both symmetric directions pinned.
printf '9\n' > "$TSK_STATE/$J-half:prev.rc"; touch "$TSK_STATE/$J-half.done"   # mid-stash shape
printf '#!/bin/bash\nexit 1\n' > "$sigstub/systemd-run"; chmod +x "$sigstub/systemd-run"
PATH="$sigstub:$PATH" "$TSK" run "$J-half" -- true >/dev/null 2>&1
[ "$(cat "$TSK_STATE/$J-half.rc" 2>/dev/null)" = "9" ] && [ -f "$TSK_STATE/$J-half.done" ] \
  && ok "interrupted stash reassembled and restored (rc half in :prev)" || no "half-stash discarded: lone stale .done read as a live result (rc: '$(cat "$TSK_STATE/$J-half.rc" 2>/dev/null)')"
rm -f "$TSK_STATE/$J-half".{rc,done,cmd,run} "$TSK_STATE/$J-half":prev.{rc,done}
printf '9\n' > "$TSK_STATE/$J-half2.rc"; touch "$TSK_STATE/$J-half2:prev.done"  # mid-restore shape
PATH="$sigstub:$PATH" "$TSK" run "$J-half2" -- true >/dev/null 2>&1
[ "$(cat "$TSK_STATE/$J-half2.rc" 2>/dev/null)" = "9" ] && [ -f "$TSK_STATE/$J-half2.done" ] \
  && ok "interrupted restore reassembled and restored (done half in :prev)" || no "mirror half-stash lost (rc: '$(cat "$TSK_STATE/$J-half2.rc" 2>/dev/null)')"
rm -f "$TSK_STATE/$J-half2".{rc,done,cmd,run} "$TSK_STATE/$J-half2":prev.{rc,done}

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
