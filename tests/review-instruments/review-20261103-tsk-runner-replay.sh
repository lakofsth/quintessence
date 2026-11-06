#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Retained by fix-review 20261102T222427Z (report: a private review record).
# Not a gate: tests/run.sh does not run it. Usage: bash tests/review-instruments/review-20261103-tsk-runner-replay.sh ./tsk [ITERS]
# item3-tsk-runner-replay.sh TSK [ITERS] — fix-review 20261102T222427Z, item 3 (b1130d8).
# Replays the deep duplicate-start race against the `tsk` at path TSK with a
# FAKE manager: stub `systemctl` and `systemd-run` on PATH that keep units as files under a fake
# dir and START NOTHING (no real systemd unit is created, stopped or queried). systemd-run records
# the runner path a unit would execute; the unit "opens" it only when this harness reads it, which
# is the fork-to-first-open gap held open on purpose. Invariant under test:
# a-tsk-unit-executes-the-runner-its-own-start-wrote.
#   R1 lied-once:  winner `run J -- sleep 30`; manager lies "inactive" ONCE; loser `run J -- true`.
#                  PASS iff loser refused, the file the unit's ExecStart names holds the winner's
#                  command and not the loser's, and .cmd names the winner.
#                  (Commands are compared whitespace-squeezed: tsk's %q argv leaves "( sleep 30  )".)
#   R2 lied-twice: as R1, but the manager also lies to install_failed's is-active guard. Reports
#                  whether the live job's .run/.cmd records survive (the row's headline says they
#                  describe that start "whatever any other start of the same name does").
#   R3 concurrent: ITERS iterations of two simultaneous starts of one name (`echo A` / `echo B`)
#                  with a manager that always answers "inactive" (systemd-run still refuses an
#                  existing unit). The per-name flock refuses the second start before it touches
#                  state, so this mostly measures the lock. PASS iff exactly one start exits 0, and the unit's runner, .cmd
#                  and the start that exited 0 all name the same command.
set -u
TSK="$1"; ITERS="${2:-40}"
W=$(mktemp -d); F="$W/fake"; B="$W/bin"; mkdir -p "$F/units" "$B"
cat > "$B/systemctl" <<'S'
#!/usr/bin/env bash
F="$FAKE"; q=0; args=()
for a in "$@"; do case "$a" in --user) ;; --quiet) q=1 ;; *) args+=("$a");; esac; done
case "${args[0]}" in
  is-active) u="${args[1]}"
    if [ -f "$F/lie" ] && n=$(cat "$F/lie") && [ "$n" != 0 ]; then
      [ "$n" = inf ] || echo $((n-1)) > "$F/lie"; [ $q = 1 ] || echo inactive; exit 3; fi
    if [ -f "$F/units/$u" ]; then [ $q = 1 ] || echo active; exit 0; fi
    [ $q = 1 ] || echo inactive; exit 3 ;;
  stop) rm -f "$F/units/${args[1]}"; exit 0 ;;
  show) u="${args[-1]}"; [ -f "$F/units/$u" ] && printf '{ path=%s ; }\n' "$(cat "$F/units/$u")"; exit 0 ;;
  list-units) exit 0 ;;
esac; exit 0
S
cat > "$B/systemd-run" <<'S'
#!/usr/bin/env bash
F="$FAKE"; unit=""; prev=""; for a in "$@"; do [ "$prev" = --unit ] && unit="$a.service"; prev="$a"; last="$a"; done
( set -o noclobber; echo "$last" > "$F/units/$unit" ) 2>/dev/null || { echo "Failed to start transient service unit: Unit $unit already exists." >&2; exit 1; }
exit 0
S
chmod +x "$B/systemctl" "$B/systemd-run"
export FAKE="$F" PATH="$B:$PATH"
cmd_of(){ grep -o '( .* )' "$1" 2>/dev/null | head -1 | tr -s ' '; }
reset(){ rm -rf "$F/units"/* "$F/lie"; export TSK_STATE="$W/state.$1"; rm -rf "$TSK_STATE"; }
pass=0; fail=0
ok(){ pass=$((pass+1)); echo "ok   $*"; }; no(){ fail=$((fail+1)); echo "FAIL $*"; }
note(){ echo "NOTE $*"; }   # R2's record loss: reported, not counted (reachable only by a manager that lies twice)

# R1
reset r1; bash "$TSK" run J -- sleep 30 >/dev/null 2>&1 || no "R1 winner did not start"
echo 1 > "$F/lie"; bash "$TSK" run J -- true >/dev/null 2>&1 && no "R1 loser exited 0" || ok "R1 loser refused"
ex=$(cat "$F/units/tsk-J.service"); c=$(cmd_of "$ex")
[ "$c" = "( sleep 30 )" ] && ok "R1 the unit's runner ($(basename "$ex")) holds the winner's command" \
  || no "R1 the unit's runner ($(basename "$ex")) holds '$c', not the winner's ( sleep 30 )"
[ "$(cat "$TSK_STATE/J.cmd" 2>/dev/null)" = "sleep 30" ] && ok "R1 .cmd names the winner" || no "R1 .cmd holds '$(cat "$TSK_STATE/J.cmd" 2>/dev/null)'"
# R2
reset r2; bash "$TSK" run J -- sleep 30 >/dev/null 2>&1
echo 2 > "$F/lie"; bash "$TSK" run J -- true >/dev/null 2>&1
ex=$(cat "$F/units/tsk-J.service"); c=$(cmd_of "$ex")
[ "$c" = "( sleep 30 )" ] && ok "R2 the unit's runner holds the winner's command" || no "R2 the unit's runner holds '$c'"
[ -f "$TSK_STATE/J.run" ] && ok "R2 the live job's .run record survives a loser whose manager lied twice" \
  || note "R2 the live job's .run record was REMOVED by the loser (install_failed's is-active guard was lied to): tsk stop will not mark it done"
[ -f "$TSK_STATE/J.cmd" ] && ok "R2 the live job's .cmd survives" || note "R2 the live job's .cmd was removed by the loser"
# R3
bad=0
for i in $(seq 1 "$ITERS"); do
  reset r3; echo inf > "$F/lie"
  bash "$TSK" run J -- echo A >/dev/null 2>&1 & pa=$!
  bash "$TSK" run J -- echo B >/dev/null 2>&1 & pb=$!
  wait $pa; ra=$?; wait $pb; rb=$?
  ex=$(cat "$F/units/tsk-J.service" 2>/dev/null); c=$(cmd_of "$ex"); cm=$(cat "$TSK_STATE/J.cmd" 2>/dev/null)
  if [ $ra = 0 ] && [ $rb != 0 ]; then want="( echo A )"; wcmd="echo A"
  elif [ $rb = 0 ] && [ $ra != 0 ]; then want="( echo B )"; wcmd="echo B"
  else bad=$((bad+1)); echo "     iter $i: exit codes A=$ra B=$rb (want exactly one 0)"; continue; fi
  if [ "$c" != "$want" ] || [ "$cm" != "$wcmd" ]; then bad=$((bad+1)); echo "     iter $i: winner $want, unit runs '$c', .cmd '$cm'"; fi
done
[ $bad = 0 ] && ok "R3 $ITERS/$ITERS concurrent pairs: the unit runs the start that exited 0, and .cmd names it" \
  || no "R3 $bad/$ITERS concurrent pairs ran or recorded a command that was not the winner's"
echo "RESULT $(basename "$(dirname "$TSK")") passed=$pass failed=$fail"
rm -rf "$W"
[ "$fail" -eq 0 ]
