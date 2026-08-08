#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-setup-wire.sh — setup.sh --wire-claude writes settings.json THROUGH a symlink.
#
# os.replace does not follow symlinks: it replaces the LINK with a regular file. A user who has
# symlinked ~/.claude/settings.json into a dotfiles repo — the ordinary way to version a dotfile —
# would have had the link silently detached and the repo copy stranded with the old hooks, with
# nothing reporting it. Fixed 2026-08-03; this pins it, because the fix shipped untested and an
# unpinned fix is one refactor away from being silently reverted.
#
# Runs the WHOLE installer and reads one property out of it. An earlier version of this comment
# claimed it exercised the embedded python block directly; it never did, and that wrong belief is
# why the isolation below was missing.
set -u
# Each setup.sh below is launched with `setsid` so it leads its own process group and can be
# killed as one — see the kill note below. `set -m` would also give a process group, but its job
# monitor prints a bare "Terminated" into the suite's output, which reads like a failure.
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# ---- isolation: this suite runs the installer, so it must not install anything real -----------
# setup.sh links the CLIs into BIN_DIR, rewrites the user config, creates the store and installs
# skills. Without isolation the documented gate `bash tests/run.sh` silently reconfigures the live
# install of whoever runs it: ~/.local/bin/qq is repointed at the checkout under test and
# QUINTESSENCE_DIR/QQ_MEMDIR are rewritten to the defaults, so a user whose store is anywhere else
# finds their notes gone. Reproduced against a decoy HOME, and observed on the author's own
# machine. (Seventh pass, F1.)
#
# THE ISOLATION IS AN ALLOWLIST, NOT A LIST OF THINGS TO UNSET. The first version of it exported a
# throwaway HOME and unset two variables, on the belief that everything the installer touches is
# derived from $HOME. It is not: setup.sh reads five environment overrides and qq-config.sh reads
# two more, and each one that survives goes straight past the throwaway HOME to the operator's real
# install. Reproduced verbatim at the documented gate — `QQ_CONFIG=/path/to/decoy bash
# tests/run.sh` reported ALL 23 suites passed and rewrote the decoy to a $TMP path this suite then
# deleted (eighteenth pass, F1). XDG_CONFIG_HOME and SKILLS_DIR do the same thing and are on
# NOBODY's hand list — that is the point: an unset-list covers the overrides someone remembered,
# and the eighth is added next Tuesday without anyone coming back here.
#
# So the installer runs under `env -i` with an explicit allowlist and inherits NOTHING else. A new
# override added to setup.sh tomorrow is excluded on the day it is written, because exclusion is
# the default rather than the thing that must be remembered. What the allowlist carries and why:
#   PATH  — the installer shells out to python3, git and qq.
#   HOME  — the throwaway one; every $HOME-derived path in setup.sh must land inside it.
#   GIT_* — a scratch identity, because `qq init` commits and the throwaway HOME has no
#           ~/.gitconfig, so git would refuse with "unable to auto-detect email address".
#           tests/run.sh seeds the same four for the same reason; under `env -i` they cannot be
#           inherited from it, so they are named here.
# Anything a single case needs beyond that is passed to run_installer as an explicit VAR=VALUE
# argument, which is how the CLAUDE_SETTINGS and PYTHONPATH cases below get theirs.
export HOME="$TMP/home"
# ONE definition of the allowlist, held as an array and used by both the launcher and the pin that
# checks it. Written out twice it would be possible for the pin to certify a construction the
# installer no longer runs under, which is the shape of vacuity this round is about.
_ISOLATED_ENV=(env -i
  PATH="$PATH" HOME="$HOME"
  GIT_AUTHOR_NAME=qq-tests GIT_AUTHOR_EMAIL=qq-tests@localhost
  GIT_COMMITTER_NAME=qq-tests GIT_COMMITTER_EMAIL=qq-tests@localhost)
run_installer() {   # VAR=VALUE... — launch setup.sh --wire-claude in its own process group; $! is it
  # --no-self-check breaks the loop this suite would otherwise close: setup.sh's step 5 runs
  # tests/run.sh, run.sh runs THIS suite, and this suite runs setup.sh. Every assertion still
  # passed while it happened, so nothing failed and nothing reported it — see the case at the
  # foot of this file, which pins the flag's effect rather than trusting this line.
  setsid "${_ISOLATED_ENV[@]}" "$@" bash "$ENGINE/setup.sh" --wire-claude --no-self-check >/dev/null 2>&1 &
}

# Same launcher, output CAPTURED instead of discarded — used only by the recursion case, which
# has to read what the installer said about step 5.
run_installer_logged() {   # logfile VAR=VALUE...
  local _log=$1; shift
  setsid "${_ISOLATED_ENV[@]}" "$@" bash "$ENGINE/setup.sh" --wire-claude --no-self-check >"$_log" 2>&1 &
}

# Every environment variable the installer honours, READ OUT OF THE ESTATE rather than written
# here: the `${NAME:-default}` / `${NAME+x}` reads in setup.sh and qq-config.sh are exactly the
# points at which an ambient value takes over, and setup.sh's own `# Env overrides:` header line is
# folded in so a name it advertises cannot be missing from the derivation. Used to prove the
# allowlist above excludes all of them — never to unset them one by one, which is the construction
# this replaced.
_documented_overrides() {
  {
    sed -nE 's/^#[[:space:]]*Env overrides:[[:space:]]*//p' "$ENGINE/setup.sh" | tr -s ' ' '\n'
    grep -hoE '\$\{[A-Z][A-Z0-9_]*(:-|:\+|\+|-)' "$ENGINE/setup.sh" "$ENGINE/qq-config.sh" \
      | sed -E 's/^\$\{//; s/(:-|:\+|\+|-)$//'
  } | grep -E '^[A-Z][A-Z0-9_]*$' | sort -u
}

fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

# The dotfiles-repo shape: the real file lives in a repo, ~/.claude/settings.json links to it.
mkdir -p "$TMP/dotfiles" "$TMP/home/.claude"
cat > "$TMP/dotfiles/settings.json" <<'JSON'
{"myCustomSetting": "keep me", "hooks": {}}
JSON
# 0640, NOT 0600: 0600 is what a temp gets by default, so asserting it would pass
# whether or not the mode is carried across (sixth pass, F3).
chmod 0640 "$TMP/dotfiles/settings.json"
ln -s "$TMP/dotfiles/settings.json" "$TMP/home/.claude/settings.json"

run_installer HERE="$ENGINE" CLAUDE_SETTINGS="$TMP/home/.claude/settings.json"
_pid=$!
# setup.sh does more than the wire step; the property is settled once the write lands.
landed=0
for _ in $(seq 1 100); do
  if grep -q inject-contract "$TMP/dotfiles/settings.json" 2>/dev/null; then landed=1; break; fi
  sleep 0.1
done
# Kill the GROUP, not the shell. setup.sh's last step runs tests/run.sh — which re-enters this
# suite — so a bare `kill` can leave a nested suite orphaned, still writing after the EXIT trap
# has removed $TMP. The negative pid is the process group, which `setsid` above makes the child
# lead — bash starts a background job in the SHELL's group, so setsid execs without forking and
# $! is therefore the new group's id.
kill -- -"$_pid" 2>/dev/null; wait "$_pid" 2>/dev/null

# EVERY assertion below is gated on the write having LANDED, and the gate is the poll's own
# result rather than a re-test of it. Five of the nine assertions in this file used to report `ok`
# against a fixture the installer had never touched: the symlink is still a symlink because the
# fixture made it one, the unrelated setting is still there because nobody rewrote the file, the
# mode is still 0640 because the fixture chmod'd it, and no temp is left behind because no temp
# was ever created. Put `sys.exit(0)` at the top of setup.sh's embedded python and the suite
# printed 5 passed, 4 failed -- the flagship "the symlink survives the write" among the passes.
# The poll above expires after 10 s, so a slow host or a hung installer is enough to reach that
# state for real (sixteenth pass, F4).
if [ "$landed" -eq 0 ]; then
  no "the installer never wrote the hooks (poll expired after 10s) — the four assertions below cannot be told from an untouched fixture, so they are skipped rather than reported as passes"
else
  ok "the hooks land in the dotfiles copy, not beside it"

  [ -L "$TMP/home/.claude/settings.json" ] \
    && ok "the symlink survives the write" \
    || no "the symlink was replaced by a regular file (the defect this test exists for)"

  grep -q "keep me" "$TMP/dotfiles/settings.json" 2>/dev/null \
    && ok "unrelated settings are preserved" \
    || no "an unrelated setting was lost"

  [ "$(stat -c %a "$TMP/dotfiles/settings.json" 2>/dev/null)" = "640" ] \
    && ok "the destination's mode is carried across" \
    || no "mode changed (got $(stat -c %a "$TMP/dotfiles/settings.json" 2>/dev/null), expected 640)"

  # shellcheck disable=SC2012
  [ "$(ls "$TMP/dotfiles" | grep -c '\.tmp')" = "0" ] \
    && ok "no temp file is left behind" \
    || no "a temp file survived the write"
fi

# --- a FRESH settings.json must honour the operator's umask, not a hardcoded mode -------------
# An operator who set a umask has opted into a default, and settings.json can carry an env block.
#
# TWO umasks, because one cannot do the job. 027 -> 0640 catches a hardcoded 0600 (what a temp
# gets by default), which is what the sixth pass diagnosed. But 027 masks BOTH 0o666 and 0o644 to
# 0640, so it cannot see the create mode being NARROWED -- and narrowing silently drops group
# write for anyone on umask 002. 002 separates them: 0o666 -> 0664, 0o644 -> 0644. Ninth pass, F3,
# the third appearance of this shape. An assertion must distinguish the correct value from the
# plausible wrong ones, not merely from the defaults.
for _case in "027 640" "002 664"; do
  set -- $_case; _mask="$1"; _want="$2"
  mkdir -p "$TMP/fresh$_mask/.claude"
  # umask is inherited across exec and is NOT an environment variable, so it survives `env -i`
  # inside run_installer — which is what makes this case testable under the allowlist at all.
  ( umask "$_mask"
    run_installer HERE="$ENGINE" CLAUDE_SETTINGS="$TMP/fresh$_mask/.claude/settings.json"
    _p=$!
    for _ in $(seq 1 100); do
      [ -s "$TMP/fresh$_mask/.claude/settings.json" ] && break; sleep 0.1
    done
    kill -- -"$_p" 2>/dev/null; wait "$_p" 2>/dev/null )

  _got="$(stat -c %a "$TMP/fresh$_mask/.claude/settings.json" 2>/dev/null)"
  [ "$_got" = "$_want" ] \
    && ok "a fresh settings.json honours umask $_mask (=$_want)" \
    || no "fresh settings.json is $_got under umask $_mask, expected $_want"
done

# --- a filesystem that refuses chmod must not abort the write or strand a temp -----------------
# vfat/exfat and some CIFS/NFS mounts have no unix modes. The baseline never called chmod, so the
# write succeeded there; a refusal escaping would break an install that used to work.
mkdir -p "$TMP/nochmod/.claude" "$TMP/pypatch"
# Scoped to the settings file only: setup.sh runs `qq init` first, which chmods the store's git
# hooks, so refusing chmod process-wide kills the script before the block under test is reached.
cat > "$TMP/pypatch/sitecustomize.py" <<'PYPATCH'
import os
_real = os.chmod
def _refuse(path, *a, **k):
    # ONLY the temp the block under test creates. Wider scopes hit `qq init` (it chmods the
    # store's git hooks) and shutil.copy2 in the backup step (it copies mode, and its target
    # name also contains "settings.json") — neither is what this test is about.
    if ".tmp." in str(path):
        raise PermissionError(1, "Operation not permitted")
    return _real(path, *a, **k)
os.chmod = _refuse
PYPATCH
echo '{"hooks": {}}' > "$TMP/nochmod/.claude/settings.json"
run_installer PYTHONPATH="$TMP/pypatch" HERE="$ENGINE" \
              CLAUDE_SETTINGS="$TMP/nochmod/.claude/settings.json"
_p=$!
nochmod_landed=0
for _ in $(seq 1 100); do
  if grep -q inject-contract "$TMP/nochmod/.claude/settings.json" 2>/dev/null; then
    nochmod_landed=1; break
  fi
  sleep 0.1
done
kill -- -"$_p" 2>/dev/null; wait "$_p" 2>/dev/null

# Same gate, same reason: "no temp was stranded" is trivially true of a directory the installer
# never reached, so it only says something once the write is known to have landed.
if [ "$nochmod_landed" -eq 0 ]; then
  no "the write died when chmod was refused (it succeeded at baseline) — the strand check is skipped rather than passed against a directory nothing wrote to"
else
  ok "a chmod refusal does not abort the write"

  # shellcheck disable=SC2012
  [ "$(ls -a "$TMP/nochmod/.claude" | grep -c '\.tmp')" = "0" ] \
    && ok "a chmod refusal strands no temp file" \
    || no "a temp file was stranded when chmod was refused"
fi

# --- the chmod must run BEFORE the payload, not after it --------------------------------------
# Twenty-third pass, F6, swept from the engine into the mirror. The mode carry above is pinned at
# the finished file, which cannot see WHEN the chmod runs: the create already carries the mode, so
# moving `os.chmod(tmp, carry)` below the `json.dump` leaves the destination at exactly the same
# mode and every assertion above green. The two orderings differ only while the payload is being
# written, and only when the umask actually took a bit off the create -- umask 077 against a 0644
# destination gives a create at 0600 and a chmod back to 0644.
#
# So the mode is read AT THE MOMENT OF THE WRITE, by wrapping `json.dump`: the block under test
# hands it the open temp, whose `.name` is the path to stat. Same interposition the chmod-refusal
# case above uses, for the same reason -- setup.sh's write cannot be imported and called.
mkdir -p "$TMP/modewindow/.claude" "$TMP/modeprobe"
echo '{"hooks": {}}' > "$TMP/modewindow/.claude/settings.json"
chmod 0644 "$TMP/modewindow/.claude/settings.json"
cat > "$TMP/modeprobe/sitecustomize.py" <<'PYPATCH'
import json, os, stat
# The temp's PATH comes from os.open, not from the file object: the block hands `json.dump` an
# `os.fdopen` handle, whose `.name` is the descriptor number and not a path to stat.
_seen = {}
_real_open = os.open
def _open(path, *a, **k):
    fd = _real_open(path, *a, **k)
    if ".tmp." in str(path) and "settings.json" in str(path):
        _seen["temp"] = path
    return fd
os.open = _open

_real_dump = json.dump
def _dump(obj, fp, *a, **k):
    probe, temp = os.environ.get("MODE_PROBE"), _seen.get("temp")
    if probe and temp and os.path.exists(temp):
        with open(probe, "a") as out:
            out.write("%o\n" % stat.S_IMODE(os.stat(temp).st_mode))
    return _real_dump(obj, fp, *a, **k)
json.dump = _dump
PYPATCH
( umask 077
  run_installer PYTHONPATH="$TMP/modeprobe" MODE_PROBE="$TMP/modeprobe/mode" HERE="$ENGINE" \
                CLAUDE_SETTINGS="$TMP/modewindow/.claude/settings.json"
  _p=$!
  for _ in $(seq 1 100); do
    grep -q inject-contract "$TMP/modewindow/.claude/settings.json" 2>/dev/null && break
    sleep 0.1
  done
  kill -- -"$_p" 2>/dev/null; wait "$_p" 2>/dev/null )

# The instrument first: an empty probe would make the assertion below vacuous, and a spy that
# never fired looks exactly like a write that never happened.
_window="$(head -n1 "$TMP/modeprobe/mode" 2>/dev/null)"
if [ -z "$_window" ]; then
  no "the json.dump spy never fired — the in-flight mode was never measured, so the ordering is unchecked rather than checked"
else
  [ "$_window" = "644" ] \
    && ok "the temp carries the destination's mode while the payload is written" \
    || no "the temp was $_window while the payload was written, not 644 — the chmod is running after the write, leaving the file at the create's \`carry & ~umask\`"
fi

# --- the two things setup.sh took from atomicio without taking their guards --------------------
# Eighteenth pass, F3 and F4. setup.sh open-codes the atomic write because it runs before the
# package is importable, and it adopted atomicio's UNIQUE temp names and its 17-byte name overhead
# without the reclaim sweep or the name-budget refusal that make those safe. The parity note in
# setup.sh says which properties are mirrored; these two assertions are what makes the note
# checkable rather than aspirational.
#
# CLAUDE_SETTINGS is the injection point: it names the target outright, so litter can be planted
# beside it and a hostile basename can be handed to it, both without touching anything else.
#
# WHAT THIS DOES NOT REACH, and it is a fair amount: the interrupted-write path that produces the
# litter in the first place is not exercised here, only the litter's shape and the two runs that
# must sweep it (one with work to do, one without); and nothing executes the parity CLAIM — that the two
# implementations stay in step is held up by a comment and by whoever reads it next, and an
# AST-level comparison of a heredoc against a module is not a test this suite can carry. The
# python side's own guards are pinned in tests/py/test_atomicio.py; this is the shell copy only.
#
# THE PARITY LIST, PROPERTY BY PROPERTY, so the gaps are named rather than left to be inferred
# from what happens to be asserted (nineteenth pass, F3 — the reach note said nothing about the
# cycle handling, and that silence read as coverage):
#   resolve-first symlink handling  — pinned (the dotfiles case at the top of this file)
#   the mode carry                  — pinned (two umasks, the chmod-refusal case, and the chmod's
#                                     POSITION: read from inside the payload write, since the
#                                     finished file is identical whichever side of it the chmod
#                                     runs on)
#   BaseException cleanup           — pinned in the chmod window (below): an injected interrupt
#                                     between `fdopen` and the write must close the file object
#                                     AND strand no temp. That window is the one where `fd` is
#                                     already cleared, so it is the one where a cleanup that
#                                     closes only `fd` leaks (twenty-second pass, F3). The other
#                                     interrupt points -- inside the write, inside the cleanup
#                                     itself -- are pinned in tests/py/test_atomicio.py against
#                                     the engine, not here
#   the name-length refusal         — pinned in its POSITIVE direction at both consumers (17 and
#                                     23). The engine's discrimination test also holds the
#                                     NEGATIVE one — a path over PATH_MAX must NOT be claimed as
#                                     a basename budget — and the mirror's copy of that `None`
#                                     return has no case here: it needs a >PATH_MAX directory
#                                     tree built around an installer run.
#   the stale-temp reclaim          — pinned (both spellings, plus the foreign tail left alone),
#                                     and its REACHABILITY pinned separately by a no-change run,
#                                     which is where it used to be uncallable (twenty-second
#                                     pass, F2). The engine sweeps after each write; this block
#                                     sweeps after each successful wire, because its writes are
#                                     the ones that may legitimately not happen
#   the symlink-cycle refusal       — pinned below, by the diagnosis and by the outcome
#   the O_EXCL unique temp          — NOT pinned here. Pinning it means making the temp name
#                                     predictable, which means pinning `os.urandom` for a whole
#                                     installer run that also runs `qq init` — a wider blast
#                                     radius than the property is worth. The engine's O_EXCL is
#                                     pinned by the real attack in tests/py/test_atomicio.py.
mkdir -p "$TMP/litter/.claude"
echo '{"hooks": {}}' > "$TMP/litter/.claude/settings.json"
_old="$(( $(date +%s) - 3 * 86400 ))"
# THE GENERATED NAME COMES FROM THE WRITER, not from this file and not from the constant the
# RECOGNISER reads. Both of the shorter routes assert against a world that may not exist: a
# hand-typed `settings.json.tmp.deadbeef1234` pins whatever the writer emitted the day it was
# typed, and a width re-derived from TEMP_TOKEN_BYTES pins the recogniser against itself — under
# a writer left on a literal 6 while the constant moved to 5, that version stayed green while the
# real litter became unreclaimable. So the installer is run once with a spy on `os.replace`, and
# the name it was actually handed is what gets planted, aged, and swept below.
#
# The capture run's RANDOMNESS is pinned along with the spy, for a reason the name-capture alone
# cannot cover: the reclaim claims only tails carrying one of `abcdef`, deliberately narrower
# than the writer, so about one captured tail in 281 is all-decimal and is NOT reclaimed. That is
# ruled behaviour, and the "is reclaimed" assertion below was contradicting it about one run in
# 281 (D77). Pinned narrowly — only the CAPTURE run, whose whole job is to produce a name; the
# measuring run below keeps the real thing. The note further up about not pinning `os.urandom` is
# about the O_EXCL property, which needs it held for a whole run and is pinned in the engine
# instead. The forced bytes carry a counter, so uniqueness — the property O_EXCL rests on — is
# preserved for every other caller in the process tree that asks for two bytes or more; a caller
# asking for ONE gets a letter-bearing byte that is the same one every time, because a single byte
# has no room for both the letter and a counter. The width asked for is the width returned, so a
# change to the writer's token width still moves this fixture with it.
mkdir -p "$TMP/spypatch"
cat > "$TMP/spypatch/sitecustomize.py" <<'SPYPATCH'
import os
_real_urandom = os.urandom
_seq = [0]


def _letter_bearing_urandom(n):
    """Bytes whose hex always carries one of `abcdef`, of the width asked, distinct per call
    from n == 2 up.

    THE BOUNDARY IS n == 1. The letter comes from the leading 0xaa byte, so it holds at every
    width; the counter goes in the REMAINING n-1 bytes, so distinctness needs a tail to sit in
    and starts at two. At n == 1 there is no tail and this returns 0xaa on every call --
    letter-bearing and the width asked for, but NOT distinct, which is all one byte can carry.
    The counter is reduced to the tail's width rather than handed to `to_bytes` whole, so a
    tail too narrow for the count repeats after 256**(n-1) calls instead of raising
    OverflowError; at n == 1 that reduction is 0 into a zero-width tail, the empty bytes.
    Both cases -- the too-narrow tail and n == 1 -- raised OverflowError before (twenty-fourth
    pass, F3). Latent here, since the writer asks for six and nothing in this run asks for one,
    but it would have surfaced as an installer crash inside an unrelated caller.
    """
    if n < 1:
        return _real_urandom(n)
    _seq[0] += 1
    tail = n - 1
    return b"\xaa" + (_seq[0] % (256 ** tail)).to_bytes(tail, "big")


os.urandom = _letter_bearing_urandom
_real = os.replace
_record = os.environ.get("QQ_TEST_TEMP_RECORD")
def _spy(src, dst, *a, **k):
    # Only the atomic-write temps, and only their basenames. `qq init` also calls os.replace in
    # this process tree, and its writes are none of this test's business.
    if _record and ".tmp." in os.path.basename(str(src)):
        with open(_record, "a") as fh:
            fh.write(os.path.basename(str(src)) + "\n")
    return _real(src, dst, *a, **k)
os.replace = _spy
SPYPATCH
run_installer PYTHONPATH="$TMP/spypatch" QQ_TEST_TEMP_RECORD="$TMP/temp-names" \
              HERE="$ENGINE" CLAUDE_SETTINGS="$TMP/litter/.claude/settings.json"
_p=$!
for _ in $(seq 1 100); do
  grep -q inject-contract "$TMP/litter/.claude/settings.json" 2>/dev/null && break; sleep 0.1
done
kill -- -"$_p" 2>/dev/null; wait "$_p" 2>/dev/null
_gen_name="$(grep '^settings\.json\.tmp\.' "$TMP/temp-names" 2>/dev/null | head -1)"
_gen_tail="${_gen_name##*.}"

echo '{"hooks": {}}' > "$TMP/litter/.claude/settings.json"   # give the second run work to do
litter_planted=0
if [ -z "$_gen_name" ]; then
  no "the spy captured no temp name from the installer — the reclaim fixture below would be spelled by hand against a shape nothing produces, so it is not planted"
elif [ "$_gen_tail" = 20260801 ] || [ "$_gen_tail" = 202608041200 ]; then
  no "the writer now emits the same tail as one of this suite's FOREIGN fixtures (settings.json.tmp.20260801, settings.json.tmp.202608041200) — pick a foreign tail the writer cannot produce before trusting the left-alone assertions"
elif ! printf '%s' "$_gen_tail" | grep -q '[a-f]'; then
  # The capture run's urandom is pinned to a letter-bearing token precisely so this cannot happen
  # by chance. If it happens anyway the pinning has stopped reaching the writer, and the honest
  # answer is to say so once, loudly — not to plant a fixture the reclaim is RULED not to claim
  # and read the resulting delete-failure as a mirror defect (D77).
  no "the captured tail '$_gen_tail' carries no hex letter, so the reclaim is ruled not to claim it — the capture run's forced os.urandom is no longer reaching the writer, and the assertions below would be testing the documented cost instead of the mirror"
else
  for _n in settings.json.tmp "$_gen_name" settings.json.tmp.20260801 settings.json.tmp.202608041200; do
    echo litter > "$TMP/litter/.claude/$_n"; touch -d "@$_old" "$TMP/litter/.claude/$_n"
  done
  litter_planted=1
fi
run_installer HERE="$ENGINE" CLAUDE_SETTINGS="$TMP/litter/.claude/settings.json"
_p=$!
litter_landed=0
for _ in $(seq 1 100); do
  if grep -q inject-contract "$TMP/litter/.claude/settings.json" 2>/dev/null; then
    litter_landed=1; break
  fi
  sleep 0.1
done
kill -- -"$_p" 2>/dev/null; wait "$_p" 2>/dev/null

# Gated, like every other assertion here: "the litter is gone" cannot be told from "the installer
# never ran" unless the write is known to have landed — and, one step earlier, unless the fixture
# was planted at all. Without the second gate an unplanted fixture read as a PASS: `[ ! -e ... ]`
# is true of a file nobody made, so the guards above would say `no` and the assertion below would
# say `ok` about the same absent file, in the same run.
if [ "$litter_planted" -eq 0 ]; then
  : # the guard above already said `no` once; there is no fixture here to assert against
elif [ "$litter_landed" -eq 0 ]; then
  no "the installer never rewrote the litter fixture (poll expired after 10s) — the reclaim assertions are skipped rather than reported as passes"
else
  [ -n "$_gen_name" ] && [ ! -e "$TMP/litter/.claude/$_gen_name" ] \
    && ok "a stale generated temp ($_gen_name, captured from the writer) beside the target is reclaimed" \
    || no "$_gen_name, aged 3 days, survived --wire-claude: setup.sh took atomicio's unique temp names without atomicio's sweep, so every interrupted run leaves a permanent file"

  # The engine matched a bare <target>.tmp by exact match until 2026-08-04 and this mirror
  # followed; both clauses were removed on the owner's ruling (twentieth pass, F2). This writer
  # cannot produce that name, so the rule could only delete a file the installer did not write —
  # an operator's own backup of their settings as readily as the pre-atomicio idiom's litter.
  # Planted at the same age as the generated temp above, so only the NAME separates them.
  [ -e "$TMP/litter/.claude/settings.json.tmp" ] \
    && ok "a bare settings.json.tmp is left alone" \
    || no "settings.json.tmp was deleted — the shell mirror kept the legacy exact match the engine dropped, so an operator's own backup of their settings dies an hour after they take it"

  # The narrowing, carried across: an 8-character tail is not a name this writer can produce, so
  # it is an operator's file. Deleting it is the failure mode, not the success one.
  [ -e "$TMP/litter/.claude/settings.json.tmp.20260801" ] \
    && ok "a foreign 8-character tail beside the target is left alone" \
    || no "settings.json.tmp.20260801 was deleted — the shell mirror kept the 8+ window the engine dropped, so an operator's own file died an hour after they wrote it"

  # Twenty-first pass, F2, carried across to the mirror: `date +%Y%m%d%H%M` is twelve characters
  # and all twelve are valid lowercase hex, so the WIDTH test alone claimed it. This is the
  # likeliest hand-made backup name there is for a settings file, and the mirror deleted it
  # identically to the engine. Only the "at least one of abcdef" condition separates it from the
  # generated temp above, which is planted at the same age and IS reclaimed.
  [ -e "$TMP/litter/.claude/settings.json.tmp.202608041200" ] \
    && ok "a foreign 12-DIGIT tail (date +%Y%m%d%H%M) beside the target is left alone" \
    || no "settings.json.tmp.202608041200 was deleted — twelve digits are twelve valid hex characters, so the mirror claimed an operator's timestamped backup as its own litter"
fi

# --- the sweep has to be REACHABLE, and the steady state is where it has to run ---------------
# Twenty-second pass, F2. The reclaim call sat inside the `else:` of `if not changed:`, so it ran
# only on a run that rewrote settings.json. `--wire-claude` is idempotent, so every install
# reaches a steady state where nothing changes and the sweep is never called again -- and the
# sequence that produces the litter makes that permanent: an interrupted run strands a temp, the
# operator's retry DOES have changes so it sweeps, but the temp is seconds old and inside the
# one-hour grace, and every run after that is a no-op. `ASSURANCE.md` told the operator "the next
# successful wiring now removes any older than an hour", which was false for exactly the
# situation it described.
#
# This is the second unreachable guard in this one mirror: the symlink-cycle refusal sat below
# the read it was meant to precede, where no input could reach it (nineteenth pass, F3, pinned
# above by a cycle that must be refused AS a cycle). A guard's REACHABILITY is a property in its
# own right, and it needs a case that fails when the guard stops being called -- not just one
# that shows the predicate is right. The predicate here was never in doubt: the run above
# already proves it sweeps when there is work to do.
#
# So: plant aged litter, then run a wire that CHANGES NOTHING, and require the sweep anyway. The
# run's own "(no change)" line is the control -- without it, a run that quietly did have work to
# do would report this as a pass.
if [ "$litter_planted" -eq 1 ] && [ "$litter_landed" -eq 1 ]; then
  echo litter > "$TMP/litter/.claude/$_gen_name"
  touch -d "@$_old" "$TMP/litter/.claude/$_gen_name"
  # Foreground: the assertion is on what this run SAYS as well as what it removes, and the file
  # is already wired so there is nothing to poll for. `timeout` bounds a version that hangs.
  _out="$(timeout 120 "${_ISOLATED_ENV[@]}" HERE="$ENGINE" \
            CLAUDE_SETTINGS="$TMP/litter/.claude/settings.json" \
            bash "$ENGINE/setup.sh" --wire-claude --no-self-check 2>&1)"
  case "$_out" in
    *"already wired to this dist (no change)"*)
      [ ! -e "$TMP/litter/.claude/$_gen_name" ] \
        && ok "a steady-state --wire-claude (no change) still sweeps a stale generated temp" \
        || no "$_gen_name, aged 3 days, survived a no-change --wire-claude: the sweep runs only when the wiring changes something, so an idempotent install never sweeps again and interrupted-run litter is permanent" ;;
    *)
      no "the steady-state run was not a no-change run, so it proves nothing about reachability; installer said: $(printf '%s' "$_out" | tail -3)" ;;
  esac
fi

# --- the cleanup closes the FILE OBJECT too, not just the descriptor --------------------------
# Twenty-second pass, F3. The `except BaseException` cleanup closed `fd` and unlinked `tmp` but
# never closed `f`. `fd` is cleared the instant `os.fdopen` returns -- correctly, the file object
# owns the descriptor from then on -- so between there and the `with f:` further down nothing in
# the cleanup owned it at all, and an interrupt landing in that window (which contains the real
# `os.chmod` syscall) leaked the descriptor. The engine's tuple leads with `fh.close()`
# (atomicio.py), and the parity note above lists the BaseException cleanup among the properties
# carried across, so this was a break in a claimed property rather than an oversight in an
# unclaimed one.
#
# THE INSTRUMENT: `os.fdopen` is wrapped so the file object records its own close, and `os.chmod`
# is made to raise `KeyboardInterrupt` -- but only for a path carrying `.tmp.`, so the installer's
# other steps are untouched, and only in the heredoc program (`sys.argv == ["-"]`), so no other
# python in the process tree is affected. The interrupt lands in exactly the window the finding
# names: after `fdopen`, before `with f:`. It has to land there -- inside the `with`, the
# with-block closes `f` on its own and the cleanup's close would be invisible.
mkdir -p "$TMP/leak/.claude" "$TMP/leakpatch"
echo '{"hooks": {}}' > "$TMP/leak/.claude/settings.json"
cat > "$TMP/leakpatch/sitecustomize.py" <<'LEAKPATCH'
import os
import sys

_record = os.environ.get("QQ_TEST_CLOSE_RECORD")
if _record and sys.argv[:1] == ["-"]:      # the setup.sh heredoc program, nothing else
    _real_fdopen = os.fdopen
    _real_chmod = os.chmod

    def _note(what):
        with open(_record, "a") as fh:
            fh.write(what + "\n")

    class _SpyFile:
        """Transparent wrapper that says when it is closed."""

        def __init__(self, inner):
            self._inner = inner

        def close(self):
            _note("close")
            return self._inner.close()

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            _note("close")
            return self._inner.__exit__(*exc)

    def _fdopen(*a, **k):
        _note("fdopen")
        return _SpyFile(_real_fdopen(*a, **k))

    def _chmod(path, *a, **k):
        if ".tmp." in os.path.basename(str(path)):
            raise KeyboardInterrupt("injected: an interrupt in the chmod window")
        return _real_chmod(path, *a, **k)

    os.fdopen = _fdopen
    os.chmod = _chmod
LEAKPATCH
_out="$(timeout 120 "${_ISOLATED_ENV[@]}" PYTHONPATH="$TMP/leakpatch" \
          QQ_TEST_CLOSE_RECORD="$TMP/leak/closes" HERE="$ENGINE" \
          CLAUDE_SETTINGS="$TMP/leak/.claude/settings.json" \
          bash "$ENGINE/setup.sh" --wire-claude --no-self-check 2>&1 || true)"
_closes="$(cat "$TMP/leak/closes" 2>/dev/null || true)"
case "$_out" in
  *KeyboardInterrupt*) : ;;   # the injection reached the block, which is the whole experiment
  *) no "the injected interrupt never reached the installer's write (no KeyboardInterrupt in its output), so the cleanup assertions below would be asserting over a run that never entered the window; installer said: $(printf '%s' "$_out" | tail -3)" ;;
esac
case "$_out$_closes" in
  *KeyboardInterrupt*fdopen*)
    case "$_closes" in
      *close*)
        ok "an interrupt in the chmod window closes the file object as well as unlinking the temp" ;;
      *)
        no "the file object was never closed after an interrupt in the chmod window: fd is cleared at fdopen, so between there and the write nothing in the cleanup owns the descriptor and it leaks — the engine's cleanup leads with fh.close()" ;;
    esac ;;
  *)
    no "the fdopen spy never fired, so the close record proves nothing either way (instrument failure, not a result)" ;;
esac
[ -z "$(ls -a "$TMP/leak/.claude" | grep '\.tmp\.' || true)" ] \
  && ok "an interrupt in the chmod window strands no temp file" \
  || no "a temp file survived an interrupt in the chmod window: $(ls -a "$TMP/leak/.claude" | grep '\.tmp\.')"

# The name-budget refusal, both consumers. setup.sh has one the engine does not — the `.qqbak-`
# backup at 23 bytes, wider than the temp's 17 — so which budget binds depends on whether the
# target already exists. Both are exercised, because checking only the temp's would still have
# ended in a traceback out of shutil.copy2, which is exactly how this was found.
_long="$(printf 'n%.0s' $(seq 1 245))"
for _case in "existing 23" "fresh 17"; do
  set -- $_case; _kind="$1"; _want="$2"
  mkdir -p "$TMP/long$_kind/.claude"
  [ "$_kind" = existing ] && echo '{"hooks": {}}' > "$TMP/long$_kind/.claude/$_long"
  # FOREGROUND, unlike every other case in this file, because the assertion is on what the
  # installer SAYS. A refusal exits non-zero inside the heredoc and setup.sh's `set -e` stops the
  # script there, so this returns in about a second and never reaches the self-check step — which
  # is also the property being claimed: it refuses instead of carrying on. `timeout` bounds the
  # other outcome, a version that hangs rather than refusing, so a regression cannot wedge the
  # gate. Same allowlist as run_installer, from the same array.
  _out="$(timeout 60 "${_ISOLATED_ENV[@]}" HERE="$ENGINE" \
            CLAUDE_SETTINGS="$TMP/long$_kind/.claude/$_long" \
            bash "$ENGINE/setup.sh" --wire-claude --no-self-check 2>&1)"
  case "$_out" in
    *Traceback*)
      no "a ${#_long}-byte CLAUDE_SETTINGS basename ($_kind target) gives a raw traceback where the 4-byte .tmp idiom succeeded — ASSURANCE promises the refusal is loud and states the arithmetic, and that was true of the engine only" ;;
    *"needs $_want bytes of room"*)
      ok "an over-long CLAUDE_SETTINGS ($_kind target) is refused with the $_want-byte arithmetic, not a traceback" ;;
    *)
      no "a ${#_long}-byte CLAUDE_SETTINGS basename ($_kind target) produced neither the $_want-byte refusal nor a traceback; installer said: $(printf '%s' "$_out" | tail -3)" ;;
  esac
done

# --- a settings.json that is a symlink CYCLE is refused, and refused AS a cycle ----------------
# Nineteenth pass, F3. The mirror's cycle handling is on setup.sh's parity list and nothing here
# reached it: replacing `if os.path.islink(path):` with `if False:` left this suite at 17 passed,
# 0 failed. The reason is worth writing down, because it is not "no fixture was built" — the check
# sat AFTER the read, and `open()` on a cycle raises ELOOP, so the read's handler exited first for
# every input that could ever arrive. The line was unreachable, and an unreachable guard cannot be
# pinned by any fixture at all. So setup.sh now refuses the cycle before the read (same commit),
# and this is what holds it there.
#
# TWO assertions, and they fail to different things. The first reads the DIAGNOSIS: at head the
# operator is told they have a cycle; with the refusal gone the read calls their settings.json
# "unreadable" with an errno and exits, which is a worse answer to a question setup.sh can answer
# exactly. The second reads the OUTCOME — both links still links, nothing else in the directory —
# which is the property that must hold however the refusal is spelled, and which goes red only if
# BOTH the cycle check and the read's handler stop refusing.
mkdir -p "$TMP/cycle/.claude"
ln -s "$TMP/cycle/.claude/settings-b.json" "$TMP/cycle/.claude/settings.json"
ln -s "$TMP/cycle/.claude/settings.json" "$TMP/cycle/.claude/settings-b.json"
# FOREGROUND with a timeout, like the name-budget cases and for the same two reasons: the
# assertion is on what the installer SAYS, and a refusal exits non-zero under `set -e` in about a
# second, while a version that carries on rather than refusing is bounded rather than left to
# wedge the gate.
_out="$(timeout 60 "${_ISOLATED_ENV[@]}" HERE="$ENGINE" \
          CLAUDE_SETTINGS="$TMP/cycle/.claude/settings.json" \
          bash "$ENGINE/setup.sh" --wire-claude --no-self-check 2>&1)"
case "$_out" in
  *"symlink cycle"*)
    ok "a settings.json that is a symlink cycle is refused AS a cycle" ;;
  *unreadable*)
    no "the cycle was refused only by the read's generic handler ('unreadable') — the dedicated refusal is gone or has moved back below the read, where it cannot fire; the operator is told an errno instead of the one-word diagnosis setup.sh can give" ;;
  *)
    no "a settings.json that is a symlink cycle produced neither refusal; installer said: $(printf '%s' "$_out" | tail -3)" ;;
esac

# shellcheck disable=SC2012
_cycle_dir="$(ls -a "$TMP/cycle/.claude" | grep -vc '^\.\.\?$')"
[ -L "$TMP/cycle/.claude/settings.json" ] && [ -L "$TMP/cycle/.claude/settings-b.json" ] \
  && [ "$_cycle_dir" = 2 ] \
  && ok "the cycle is left exactly as it was found — two links, nothing written beside them" \
  || no "the cycle was written through: settings.json is $( [ -L "$TMP/cycle/.claude/settings.json" ] && echo 'still a link' || echo 'now a regular file') and the directory holds $_cycle_dir entries (the two links, and nothing else, is what a refusal leaves) — os.replace does not follow links, so this is the silent detachment the mirror exists to prevent"

# --- the isolation itself, pinned: no documented override may reach the installer --------------
# Eighteenth pass, F1. The suite above is the thing that damaged a live install, so its isolation
# gets assertions of its own rather than being taken on the construction's word.
#
# TWO pins, because one cannot do the job. The first reads the environment the allowlist actually
# builds and is exact but textual; the second runs the real installer with a real decoy and reads
# the operator's files afterwards, which is the damage as an operator would meet it. Neither is a
# restatement of the code above: both go red on `env -i` being dropped, and the first also goes
# red on a name being ADDED to the allowlist, which the second cannot see unless that name happens
# to be one the installer writes through.

overrides="$(_documented_overrides)"
# The enumeration must find work. An empty derivation would make both pins below pass while
# asserting nothing — the same vacuity D59 is about, one file over, so it is refused here rather
# than discovered later. QQ_CONFIG is named explicitly because it is the channel the CRITICAL rode
# on: a derivation that stopped finding it would leave that exact hole certified.
if [ -z "$overrides" ]; then
  no "the override enumeration came back EMPTY — setup.sh/qq-config.sh no longer state their env overrides in a form this suite can read, so both isolation pins below assert nothing"
elif ! printf '%s\n' "$overrides" | grep -qx QQ_CONFIG; then
  no "the override enumeration no longer finds QQ_CONFIG — the variable the eighteenth-pass CRITICAL travelled on. Teach _documented_overrides the new spelling; do not leave it underived"
else
  ok "the installer's env overrides are enumerated from setup.sh and qq-config.sh ($(printf '%s ' $overrides))"
fi

# Pin 1: the environment the allowlist builds, read by running `env` through the same construction
# the installer goes through. Decoys are exported here, in the parent, exactly as an operator's
# shell profile would export them.
probe="$(
  for _v in $overrides; do export "$_v=/nonexistent/decoy-$_v"; done
  "${_ISOLATED_ENV[@]}" env          # the launcher's own construction, running `env` in place of
)"                                   # the installer — so this reads what setup.sh would be handed
leaked=""
for _v in $overrides; do
  printf '%s\n' "$probe" | grep -q "^$_v=" && leaked="$leaked $_v"
done
[ -z "$leaked" ] \
  && ok "no documented env override survives into the installer's environment" \
  || no "these documented overrides reach the installer despite the isolation:$leaked — an operator who exports one of them runs the documented gate and their real install is rewritten"

# Pin 2: the reviewer's reproduction, as an assertion. Every override points at a canary path, and
# a real config file with real content sits at the QQ_CONFIG canary — the shape an operator has.
# The whole canary tree is fingerprinted before and after, so a rewritten config, a `qq` symlink
# planted in a decoy BIN_DIR and a skill installed into a decoy SKILLS_DIR are all one assertion,
# derived from the enumeration rather than checked one variable at a time.
# Each override is pointed at `$CANARY/<NAME>/x` — a path INSIDE a per-name directory — so the
# shape of the value does not have to be known per variable: a file-valued override (QQ_CONFIG,
# CLAUDE_SETTINGS) writes `x` as a file, a directory-valued one (BIN_DIR, QUINTESSENCE_DIR,
# QQ_MEMDIR, SKILLS_DIR, XDG_CONFIG_HOME) creates `x` as a directory and fills it, and either way
# the per-name directory gains an entry that was not in the fingerprint. Pointing them AT the
# per-name path directly cannot work both ways at once: a file where a directory is expected makes
# setup.sh's `mkdir -p` fail under `set -e`, which would abort the installer early and leave the
# canary untouched for the wrong reason.
CANARY="$TMP/canary"
for _v in $overrides; do mkdir -p "$CANARY/$_v"; done
printf 'QUINTESSENCE_DIR=%s\n' "$CANARY/MY-REAL-STORE" > "$CANARY/QQ_CONFIG/x"
_canary_print(){ find "$CANARY" | sort | while IFS= read -r p; do
    printf '%s\t%s\n' "$p" "$( [ -L "$p" ] && readlink "$p" || { [ -f "$p" ] && sha256sum < "$p"; } )"
  done; }
before_canary="$(_canary_print)"

rm -rf "$TMP/home/.config"      # so the isolated config below is one this launch wrote
(
  for _v in $overrides; do export "$_v=$CANARY/$_v/x"; done
  run_installer HERE="$ENGINE"
  _p=$!
  for _ in $(seq 1 100); do
    [ -s "$TMP/home/.config/quintessence/config" ] && break; sleep 0.1
  done
  kill -- -"$_p" 2>/dev/null; wait "$_p" 2>/dev/null
)

# The canary is read FIRST and the vacuity gate second, in that order deliberately. A leak sends
# the installer's writes to the canary INSTEAD of the isolated HOME, so both conditions are true at
# once when the isolation is broken, and asking the gate first reports the damage as "the installer
# did nothing" — the true finding, said in a way that hides it. Damage outranks vacuity.
#
# The gate is still here, and for the reason every other assertion in this file has one: "the
# canary is untouched" is trivially true of a run that never got as far as writing a config
# anywhere, and that is a negative control that cannot fail.
if [ "$(_canary_print)" != "$before_canary" ]; then
  no "the installer wrote through a documented override into the operator's own paths — this is the eighteenth-pass CRITICAL back:
$(diff <(printf '%s\n' "$before_canary") <(_canary_print) | head -20)"
elif [ ! -s "$TMP/home/.config/quintessence/config" ]; then
  no "the installer never wrote a config under the isolated HOME (poll expired after 10s) — the canary check cannot be told from an installer that did nothing, so it is reported as unproven rather than as a pass"
else
  ok "the installer with every documented override exported at a decoy leaves all of them byte-identical, and writes its config under the isolated HOME instead"
fi

# --- the installer this suite runs must not re-enter the suite -------------------------------
# Found live 2026-08-08: setup.sh step 5 runs tests/run.sh, run.sh runs this file, this file runs
# setup.sh. The loop is bounded only by the 120s timeout on each launch, so it drains rather than
# running away — which is why it was invisible for weeks: every suite still PASSED, CI went green
# on it, and the only symptom was wall-clock (a 3m17s shell suite) and load on a busy box.
# A diff reviewer sees two reasonable files; a pass/fail gate sees green. So the pin is behavioural
# and reads what the installer actually did with step 5.
_rlog="$TMP/no-self-check.out"
mkdir -p "$TMP/recurse-home/.claude"
run_installer_logged "$_rlog" HERE="$ENGINE" \
  CLAUDE_SETTINGS="$TMP/recurse-home/.claude/settings.json"
_rpid=$!
for _ in $(seq 1 200); do kill -0 "$_rpid" 2>/dev/null || break; sleep 0.1; done
kill -- -"$_rpid" 2>/dev/null   # process group: setsid above made it one
wait "$_rpid" 2>/dev/null

# Two halves, and the second is what stops a vacuous pass: absence of the run marker alone would
# also be "true" if the installer died before reaching step 5, so the skip line must be PRESENT.
if grep -q 'Self-check (tests/run.sh)' "$_rlog" 2>/dev/null; then
  no "setup.sh --no-self-check STILL ran the suite — the installer this file launches re-enters this file (setup.sh step 5 -> tests/run.sh -> test-setup-wire.sh -> setup.sh)"
elif ! grep -q 'Self-check skipped' "$_rlog" 2>/dev/null; then
  no "setup.sh --no-self-check never reached step 5 at all — the absence of a nested run proves nothing here, so this pin is refusing to certify it (see $_rlog)"
else
  ok "the installer launched by this suite skips its own self-check, so running this file cannot re-enter it"
fi

# --- and nothing in this suite may launch the installer without the flag ----------------------
# The pin above proves the FLAG suppresses step 5. It does not prove anything USES it, and that
# gap is the whole defect: --no-self-check was added to the two background launchers on the first
# pass while FOUR foreground call sites in this same file went on invoking the installer without
# it. The cycle kept running, every case kept passing, and the pin above stayed green throughout.
# So this second pin enumerates the launches rather than trusting the ones it knows about — a call
# site written next Tuesday is covered on the day it is written, not on the day someone remembers.
#
# What it does NOT reach, stated so a green here is not read as more than it is: this is a TEXTUAL
# enumeration of `bash "$ENGINE/setup.sh"` / `bash "$HERE/setup.sh"`. A launch spelled any other
# way — unquoted, through a variable, via `sh` or `env` — is invisible to it. That is why the
# behavioural pin above stays: the two fail in different directions. This file also reads its own
# source, so the pattern below is written not to match its own text; the zero-match branch is what
# catches it if that ever stops being true.
_launch_total=0
_launch_bare=""
for _f in "$ENGINE"/tests/*.sh; do
  while IFS= read -r _line; do
    _launch_total=$((_launch_total + 1))
    case "$_line" in
      *--no-self-check*) : ;;
      *) _launch_bare="$_launch_bare    ${_f##*/}:$_line"$'\n' ;;
    esac
  done < <(grep -hnE 'bash "\$(ENGINE|HERE)/setup\.sh"' "$_f" | grep -vE '^[0-9]+:[[:space:]]*#')
done

if [ "$_launch_total" -eq 0 ]; then
  no "the enumeration of installer launches across tests/*.sh matched NOTHING — this suite no longer launches setup.sh in a form this pin can see, so a pass here would be certifying an empty set rather than a property"
elif [ -n "$_launch_bare" ]; then
  no "$_launch_total installer launch(es) enumerated across tests/*.sh, and these bypass --no-self-check, re-opening the setup.sh -> tests/run.sh -> this file -> setup.sh cycle:"$'\n'"$_launch_bare"
else
  ok "all $_launch_total installer launches across tests/*.sh carry --no-self-check"
fi

printf -- '----- %d passed, %d failed -----\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
