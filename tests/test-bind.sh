#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-bind.sh — B1 reality-binding acceptance gates (write-time binding),
# driven end-to-end through the REAL `qq` dispatcher on throwaway fixture stores:
#   PARITY: a write whose line has no extractable referents is byte-identical (stdout, stderr,
#           committed store content) between QQ_BIND=1 and QQ_BIND=0 — and QQ_BIND=0 never
#           creates the refs dir even when the line DOES name referents.
#   BORN-STALE PROBE: an update naming a nonexistent path warns loudly on stderr, records
#           status missing-at-write, and the write itself succeeds unchanged (exit 0, line in
#           the HEAD, committed).
#   --ref: the explicit flag binds a referent the prose never names, on every write verb shape.
# NEVER touches the live store — mktemp fixtures, own QQ_CONFIG/QQ_STATE_DIR, same isolation
# convention as test-write-parity.sh.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QQ="$ENGINE/qq"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# Fixtures live under mktemp (= /tmp), which the default QQ_BIND_EXCLUDE_ROOTS covers —
# neutralize suite-wide so every case keeps exercising extraction; the exclude-roots case
# below re-enables the default explicitly.
export QQ_BIND_EXCLUDE_ROOTS=
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

# normalize(): timestamps AND git-show `index <blob>..<blob>` lines — the blob hashes derive
# from file content whose embedded timestamp can straddle a second boundary between the two
# runs (caught as a run.sh-order-dependent flake; the content itself IS compared normalized).
normalize(){ sed -E -e 's/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z/<TS>/g' \
                     -e 's/index [0-9a-f]{4,}\.\.[0-9a-f]{4,}/index <IDX>/g'; }

mkfixture(){   # mkfixture <dir>  — init an isolated store, set the env for it
  local QDIR="$1"
  export QUINTESSENCE_DIR="$QDIR" QQ_CONFIG="$QDIR.config" QQ_STATE_DIR="$QDIR.state" QQ_MEMDIR="$QDIR.mem"
  mkdir -p "$QQ_MEMDIR"; : > "$QQ_CONFIG"
  "$ENGINE/qq" init "$QDIR" >/dev/null 2>&1
}

# ---- PARITY GATE: no-referent line, QQ_BIND=1 vs QQ_BIND=0 --------------------------------------
S1="$TMP/on/store";  mkdir -p "$TMP/on";  mkfixture "$S1"
QQ_BIND=1 "$QQ" new T "seed" >/dev/null 2>&1
QQ_BIND=1 "$QQ" update T "a plain line naming no referents at all" >"$TMP/on.out" 2>"$TMP/on.err"
rc_on=$?

S0="$TMP/off/store"; mkdir -p "$TMP/off"; mkfixture "$S0"
QQ_BIND=0 "$QQ" new T "seed" >/dev/null 2>&1
QQ_BIND=0 "$QQ" update T "a plain line naming no referents at all" >"$TMP/off.out" 2>"$TMP/off.err"
rc_off=$?

[ "$rc_on" -eq 0 ] && [ "$rc_off" -eq 0 ] && ok "parity: both writes exit 0" || no "parity: exit codes ($rc_on/$rc_off)"
diff -q <(normalize <"$TMP/on.out") <(normalize <"$TMP/off.out") >/dev/null \
  && ok "parity: stdout byte-identical (post ts-normalization)" || { no "parity: stdout differs"; diff <(normalize <"$TMP/on.out") <(normalize <"$TMP/off.out") | sed 's/^/       /'; }
[ ! -s "$TMP/on.err" ] && [ ! -s "$TMP/off.err" ] && ok "parity: stderr empty on both sides" \
  || { no "parity: stderr not empty"; cat "$TMP/on.err" "$TMP/off.err" | sed 's/^/       /'; }
diff -q <(normalize <"$S1/T.md") <(normalize <"$S0/T.md") >/dev/null \
  && ok "parity: committed HEAD content identical" || no "parity: HEAD content differs"
[ ! -e "$S1.state/refs" ] && ok "parity: no refs dir for a referent-free write (QQ_BIND=1)" \
  || no "parity: refs dir created despite no referents"

# ---- BORN-STALE PROBE ----------------------------------------------------------------------------
export QUINTESSENCE_DIR="$S1" QQ_CONFIG="$S1.config" QQ_STATE_DIR="$S1.state" QQ_MEMDIR="$S1.mem"
GONE="$TMP/definitely-not-here.md"
QQ_BIND=1 "$QQ" update T "shipped $GONE tonight" >"$TMP/bs.out" 2>"$TMP/bs.err"
rc=$?
[ "$rc" -eq 0 ] && ok "born-stale: write still exits 0" || no "born-stale: exit $rc"
grep -q "referent does not exist: $GONE – claim may be born stale" "$TMP/bs.err" \
  && ok "born-stale: loud stderr warning" || { no "born-stale: warning missing"; sed 's/^/       /' "$TMP/bs.err"; }
grep -q "shipped $GONE tonight" "$S1/T.md" \
  && ok "born-stale: line landed in the HEAD unchanged" || no "born-stale: line missing from HEAD"
grep -q "committed + mirrored" "$TMP/bs.out" \
  && ok "born-stale: stdout reports the normal commit" || no "born-stale: commit line missing from stdout"
REFS="$S1.state/refs/refs.jsonl"
[ -f "$REFS" ] && grep -q '"status": "missing-at-write"' "$REFS" && grep -qF "\"$GONE\"" "$REFS" \
  && ok "born-stale: missing-at-write record persisted" || no "born-stale: record missing from refs.jsonl"

# ---- existing referent binds ok, silently ---------------------------------------------------------
REAL="$TMP/real-artifact.txt"; printf 'content\n' > "$REAL"
QQ_BIND=1 "$QQ" update T "verified $REAL just now" >/dev/null 2>"$TMP/ok.err"
[ ! -s "$TMP/ok.err" ] && ok "existing referent: no stderr" || { no "existing referent: unexpected stderr"; sed 's/^/       /' "$TMP/ok.err"; }
grep -qF "\"$REAL\"" "$REFS" && grep -q '"fp": "sha256:' "$REFS" \
  && ok "existing referent: ok record with sha256 fp" || no "existing referent: record/fp missing"

# ---- QQ_BIND=0 stays inert even WITH referents -----------------------------------------------------
QQ_BIND=0 "$QQ" update T "another gone one $TMP/also-not-here.md" >/dev/null 2>"$TMP/off2.err"
[ ! -s "$TMP/off2.err" ] && ok "QQ_BIND=0: no warning even on a missing referent" || no "QQ_BIND=0: warned"
grep -q "also-not-here" "$REFS" 2>/dev/null && no "QQ_BIND=0: still recorded a ref" || ok "QQ_BIND=0: nothing recorded"

# ---- --ref explicit flag ---------------------------------------------------------------------------
QQ_BIND=1 "$QQ" update T --ref "file:$REAL" "prose that never names it" >/dev/null 2>&1
n_real="$(grep -cF "\"$REAL\"" "$REFS")"
[ "$n_real" -ge 2 ] && ok "--ref on update: explicit ref recorded" || no "--ref on update: not recorded"
QQ_BIND=1 "$QQ" essence T --ref "file:$REAL" "essence with explicit ref" >/dev/null 2>&1
[ "$(grep -cF "\"$REAL\"" "$REFS")" -gt "$n_real" ] && ok "--ref on essence: recorded" || no "--ref on essence: not recorded"
QQ_BIND=1 "$QQ" update T --ref >/dev/null 2>"$TMP/refusage.err"; rc=$?
[ "$rc" -eq 2 ] && grep -q -- "--ref needs a value" "$TMP/refusage.err" \
  && ok "--ref without value: clean usage error (exit 2)" || no "--ref without value: wrong handling (exit $rc)"
QQ_BIND=1 "$QQ" update T --ref "bogus:x" "line" >/dev/null 2>"$TMP/badref.err"; rc=$?
[ "$rc" -eq 0 ] && grep -q "ignored" "$TMP/badref.err" \
  && ok "--ref bad kind: soft-ignored, write succeeds" || no "--ref bad kind: wrong handling (exit $rc)"

# ---- D1: resolution-gated bare shas ----------------------------------------------------------------
REPO="$TMP/work-repo"; mkdir -p "$REPO"
git init -q "$REPO"; git -C "$REPO" config user.email t@t; git -C "$REPO" config user.name t
printf 'x\n' > "$REPO/x"; git -C "$REPO" add -A; git -C "$REPO" commit -qm one
WSHA="$(git -C "$REPO" rev-parse --short=9 HEAD)"
QQ_BIND=1 QQ_BIND_REPOS="work=$REPO" "$QQ" update T "landed $WSHA tonight" >/dev/null 2>"$TMP/d1.err"
[ ! -s "$TMP/d1.err" ] && grep -qF "\"work@$WSHA\"" "$REFS" && grep -q '"kind": "git"' "$REFS" \
  && ok "D1: bare sha resolving in exactly one repo binds as git kind, silently" \
  || { no "D1: bare sha did not bind"; sed 's/^/       /' "$TMP/d1.err"; }
QQ_BIND=1 QQ_BIND_REPOS="work=$REPO" "$QQ" update T "checked deadbeef012 and 20260704 today" >/dev/null 2>"$TMP/d1b.err"
[ ! -s "$TMP/d1b.err" ] && ! grep -q "deadbeef012\|20260704" "$REFS" \
  && ok "D1: unresolvable hex/date tokens skipped silently" || no "D1: unresolvable token bound or warned"

# ---- D2: resolution-gated repo-relative paths ------------------------------------------------------
mkdir -p "$REPO/kernel"; printf 'k\n' > "$REPO/kernel/backup.py"
git -C "$REPO" add -A; git -C "$REPO" commit -qm kernel
QQ_BIND=1 QQ_BIND_REPOS="work=$REPO" "$QQ" update T "patched kernel/backup.py:12 today" >/dev/null 2>"$TMP/d2.err"
[ ! -s "$TMP/d2.err" ] && grep -qF "\"$REPO/kernel/backup.py\"" "$REFS" \
  && ok "D2: repo-relative path binds as file ref under the ABSOLUTE worktree path, silently" \
  || { no "D2: relpath did not bind"; sed 's/^/       /' "$TMP/d2.err"; }
QQ_BIND=1 QQ_BIND_REPOS="work=$REPO" "$QQ" update T "checked and/or.x plus eval/results.md" >/dev/null 2>"$TMP/d2b.err"
[ ! -s "$TMP/d2b.err" ] && ! grep -q 'and/or.x\|eval/results.md' "$REFS" \
  && ok "D2: unresolvable relpath lookalikes skipped silently" || no "D2: lookalike bound or warned"

# ---- D3: listener ports ----------------------------------------------------------------------------
# a real loopback listener owned by this test; killed by PID (never pkill by pattern)
PORTFILE="$TMP/port"; rm -f "$PORTFILE"
python3 -c '
import socket, sys, time
s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
open(sys.argv[1], "w").write(str(s.getsockname()[1]))
time.sleep(120)' "$PORTFILE" &
LISTENER_PID=$!
for _ in $(seq 50); do [ -s "$PORTFILE" ] && break; sleep 0.1; done
LPORT="$(cat "$PORTFILE")"
QQ_BIND=1 "$QQ" update T "service up on :$LPORT now" >/dev/null 2>"$TMP/d3.err"
if [ ! -s "$TMP/d3.err" ] && grep -q "\"kind\": \"port\", \"id\": \"$LPORT\", \"fp\": \"listen" "$REFS"; then
  ok "D3: live listener binds as port kind with listen fp, silently"
else
  no "D3: live listener did not bind"; sed 's/^/       /' "$TMP/d3.err"
fi
kill "$LISTENER_PID" 2>/dev/null; wait "$LISTENER_PID" 2>/dev/null
# a port nothing listens on: loud born-stale warning, write unaffected
DEADPORT=$((LPORT == 65535 ? 64000 : LPORT + 1))
while ss -ltnH 2>/dev/null | grep -q ":$DEADPORT "; do DEADPORT=$((DEADPORT+1)); done
QQ_BIND=1 "$QQ" update T "listener claimed on :$DEADPORT" >"$TMP/d3b.out" 2>"$TMP/d3b.err"; rc=$?
[ "$rc" -eq 0 ] && grep -q "referent does not exist: $DEADPORT" "$TMP/d3b.err" \
  && grep -q "\"kind\": \"port\", \"id\": \"$DEADPORT\", \"fp\": null" "$REFS" \
  && ok "D3: dead listener warns loudly, records missing-at-write, write succeeds" \
  || no "D3: dead-listener handling wrong (exit $rc)"
QQ_BIND=1 "$QQ" update T "met at 19:22 and 19:2233 sharp" >/dev/null 2>"$TMP/d3c.err"
[ ! -s "$TMP/d3c.err" ] && ! grep -q '"kind": "port", "id": "2233"' "$REFS" \
  && ok "D3: times never bind as ports (no record, no warning)" || no "D3: time bound as port"

# ---- D4: remote-host paths (rfile) — ssh stubbed on PATH, driver + probe script run for real ------
STUB="$TMP/stub"; mkdir -p "$STUB"
cat > "$STUB/ssh" <<'EOSSH'
#!/usr/bin/env bash
# fake ssh: run the piped remote probe script LOCALLY against the last arg (the path)
exec sh -s -- "${@: -1}"
EOSSH
chmod +x "$STUB/ssh"
RFILE="$TMP/remote-artifact.conf"; printf 'remote content\n' > "$RFILE"
WANT_SHA="$(sha256sum "$RFILE" | cut -d' ' -f1)"
QQ_BIND=1 QQ_BIND_HOSTS="nas=user@nas" PATH="$STUB:$PATH" \
  "$QQ" update T "synced nas:$RFILE tonight" >/dev/null 2>"$TMP/d4.err"
[ ! -s "$TMP/d4.err" ] && grep -q "\"kind\": \"rfile\", \"id\": \"nas:$RFILE\", \"fp\": \"sha256:$WANT_SHA\"" "$REFS" \
  && ok "D4: configured host:path binds as rfile with the remote sha256, silently" \
  || { no "D4: rfile did not bind"; sed 's/^/       /' "$TMP/d4.err"; }
QQ_BIND=1 QQ_BIND_HOSTS="nas=user@nas" PATH="$STUB:$PATH" \
  "$QQ" update T "claimed nas:$TMP/never-there.bin" >/dev/null 2>"$TMP/d4b.err"; rc=$?
[ "$rc" -eq 0 ] && grep -q "referent does not exist: nas:$TMP/never-there.bin" "$TMP/d4b.err" \
  && ok "D4: missing remote path warns loudly (born-stale class), write succeeds" \
  || no "D4: missing-remote handling wrong (exit $rc)"
cat > "$STUB/ssh" <<'EOSSH'
#!/usr/bin/env bash
echo "ssh: connect to host nas port 22: Connection timed out" >&2; exit 255
EOSSH
QQ_BIND=1 QQ_BIND_HOSTS="nas=user@nas" PATH="$STUB:$PATH" \
  "$QQ" update T "resynced nas:$RFILE again" >"$TMP/d4c.out" 2>"$TMP/d4c.err"; rc=$?
[ "$rc" -eq 0 ] && grep -q "ref fingerprint error" "$TMP/d4c.err" && ! grep -q "born stale" "$TMP/d4c.err" \
  && grep -q "\"id\": \"nas:$RFILE\", \"fp\": null, .*\"status\": \"fp-error\"" "$REFS" \
  && ok "D4: unreachable host degrades to a QUIET fp-error record, write unaffected" \
  || no "D4: unreachable-host handling wrong (exit $rc)"
QQ_BIND=1 "$QQ" update T "unconfigured gateway:/etc/tor/torrc mention" >/dev/null 2>"$TMP/d4d.err"
[ ! -s "$TMP/d4d.err" ] && ! grep -q "torrc" "$REFS" \
  && ok "D4: unconfigured host:path stays entirely unbound and silent" || no "D4: unconfigured host bound/warned"

# ---- D6: exclude-roots hygiene (registry default back in force for these) -------------------------
D6FILE="$TMP/d6-artifact.txt"; printf 'x\n' > "$D6FILE"
env -u QQ_BIND_EXCLUDE_ROOTS QQ_BIND=1 "$QQ" update T "example /run/systemd churned; scratch at $D6FILE" \
  >/dev/null 2>"$TMP/d6.err"
! grep -q '/run/systemd' "$REFS" && ! grep -q "d6-artifact" "$REFS" \
  && ok "D6: default exclude-roots drop /run + /tmp prose paths (no record, no warn)" \
  || no "D6: default exclusion leaked a record"
env -u QQ_BIND_EXCLUDE_ROOTS QQ_BIND=1 "$QQ" update T --ref "file:/run/systemd" "explicit is deliberate" \
  >/dev/null 2>&1
grep -q '"id": "/run/systemd"' "$REFS" \
  && ok "D6: explicit --ref under an excluded root still binds (exempt)" \
  || no "D6: explicit --ref was wrongly excluded"

# ---- recall-03: spaced real paths reconstructed, not truncate-and-falsely-warned -------------------
SPACED_DIR="$TMP/qtest space dir"; mkdir -p "$SPACED_DIR"
SPACED="$SPACED_DIR/notes.txt"; printf 'content\n' > "$SPACED"
QQ_BIND=1 "$QQ" update T "see $SPACED for the real file" >/dev/null 2>"$TMP/sp.err"
[ ! -s "$TMP/sp.err" ] && grep -qF "\"$SPACED\"" "$REFS" && grep -q '"fp": "sha256:' "$REFS" \
  && ok "recall-03: real spaced path reconstructed whole and binds silently (no false born-stale)" \
  || { no "recall-03: spaced path not reconstructed"; sed 's/^/       /' "$TMP/sp.err"; }

# ---- legacy flag behavior unchanged (regression guard) ---------------------------------------------
QQ_BIND=1 "$QQ" update T --bogus "text" >/dev/null 2>&1; rc=$?
[ "$rc" -eq 2 ] && ok "unknown flag still hard-errors (exit 2)" || no "unknown flag handling changed (exit $rc)"
QQ_BIND=1 "$QQ" update T -- "--ref file:$REAL literal text" >/dev/null 2>&1
grep -q -- "--ref file:.*literal text" "$S1/T.md" \
  && ok "'--' still makes a trailing --ref literal text" || no "'--' literal-text contract broken"

echo
[ "$fail" -eq 0 ] && { echo "test-bind.sh: all $pass checks passed"; exit 0; }
echo "test-bind.sh: $fail FAILED ($pass passed)"; exit 1
