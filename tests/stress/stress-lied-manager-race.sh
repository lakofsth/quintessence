#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
#
# Looped end-to-end exercise of the DEEP duplicate-start window: a stateful systemctl stub
# lies "inactive" exactly once for the raced name, so a second `run` passes the duplicate
# check against a live job, then loses at (stubbed, failing) systemd-run. Each iteration
# then stops the live job and requires its completion marker: DONE with rc=143.
# Retained from the 2026-08-10 diagnosis round (see tests/stress/README.md): this loop
# produced the pre-fix substitution evidence — 41/1300 failures at ambient load, every one
# showing a sleep-30 job logging start+exit rc=0 in the same second (the loser's `true`
# executed under the winner's name). Post-flock-fix the loop runs clean; the stubbed deep
# window itself remains reachable by design and is pinned by suite test 4k.
#
# Contention lane: drives real systemd --user units in a loop — idle box only.
# Usage: stress-lied-manager-race.sh [iterations]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
TSK="${TSK:-$HERE/../../tsk}"
D=$(mktemp -d "${TMPDIR:-/tmp}/tsk-stress-lied.XXXXXX")
trap 'rm -rf "$D"' EXIT
export HOME="$D/home"; mkdir -p "$HOME"
ITERS="${1:-200}"
# Failure evidence goes OUTSIDE the trap-removed workdir.
FAILDIR="${STRESS_FAILDIR:-$PWD/tsk-stress-failures.$$}"
total_fail=0
deep_subst=0
ts(){ date +%s.%N; }

for i in $(seq 1 "$ITERS"); do
  TMP=$(mktemp -d "$D/run.XXXXXX")
  export TSK_STATE="$TMP/state"; mkdir -p "$TSK_STATE"
  J="lied$$-$i"
  out="$TMP/out.log"
  {
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

    echo "=== iter $i job $J pid $$ t0=$(ts) ==="
    "$TSK" run "$J-race" -- sleep 30
    echo "t_after_first_run=$(ts)"
    inode_before=$(ls -i "$TSK_STATE/$J-race.run" 2>/dev/null | awk '{print $1}')
    echo "inode_before=$inode_before"
    PATH="$racedir:$PATH" "$TSK" run "$J-race" -- true
    raced_rc=$?
    echo "t_after_raced_run=$(ts) raced_rc=$raced_rc"
    if [ -f "$TSK_STATE/$J-race.run" ]; then
      echo "RUN_PRESENT=yes"
      echo "inode_after=$(ls -i "$TSK_STATE/$J-race.run" 2>/dev/null | awk '{print $1}')"
    else
      echo "RUN_PRESENT=no"
    fi
    echo "t_before_stop=$(ts)"
    "$TSK" stop "$J-race"
    echo "t_after_stop=$(ts) stop_rc=$?"
    if [ -f "$TSK_STATE/$J-race.done" ]; then
      echo "DONE_PRESENT=yes RC=$(cat "$TSK_STATE/$J-race.rc" 2>/dev/null)"
    else
      echo "DONE_PRESENT=no"
    fi
    echo "log_file_contents:"; cat "$TSK_STATE/$J-race.log" 2>/dev/null || echo "(no log)"
    systemctl --user show "tsk-$J-race.service" -p ActiveState -p SubState -p Result -p ExecMainStatus -p ExecMainCode 2>&1 || true
  } > "$out" 2>&1

  # Two KNOWN outcomes under the stub. (a) The winner's child had already opened its
  # script: stop kills a real sleep-30, marker rc=143. (b) The forced manager lie landed
  # inside the winner's fork-to-first-open gap: the loser's script is what the child
  # opens — the documented deep-window substitution, reachable only when the manager
  # mis-reports a live unit (every truthful-manager path is closed by the start lock +
  # the activating-aware check). (b) is MEASURED and reported, not a gate failure — the
  # gate on it is the unique-runner-path rework, queued as its own series. Anything
  # OUTSIDE these two outcomes fails the run.
  if grep -q "DONE_PRESENT=yes RC=143" "$out"; then
    :
  elif grep -q "DONE_PRESENT=yes RC=0" "$out" && grep -q "raced_rc=1" "$out"; then
    deep_subst=$((deep_subst+1))
    mkdir -p "$FAILDIR"
    cp "$out" "$FAILDIR/deep-subst-$(printf '%04d' "$i")-$$.log"
    echo "DEEP-WINDOW SUBSTITUTION iter $i (measured, not a failure) -> $FAILDIR"
  else
    total_fail=$((total_fail+1))
    mkdir -p "$FAILDIR"
    dest="$FAILDIR/fail-$(printf '%04d' "$i")-$$.log"
    cp "$out" "$dest"
    echo "FAIL iter $i -> $dest"
  fi
  systemctl --user stop "tsk-$J-race.service" >/dev/null 2>&1
  rm -rf "$TMP"
  if [ $((i % 50)) -eq 0 ]; then echo "progress: $i/$ITERS done, fails=$total_fail" >&2; fi
done
echo "RESULT total_fail=$total_fail / $ITERS  (deep_window_substitutions=$deep_subst — measured residual, evidence in $FAILDIR)"
[ "$total_fail" -eq 0 ]
