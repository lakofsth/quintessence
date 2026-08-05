#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-store-pollution.sh — regression for human-1-store-pollution: `bash setup.sh` (and any shell
# that has `export QUINTESSENCE_DIR=...` per README's manual install path) leaves QUINTESSENCE_DIR
# exported into every child process, including tests/run.sh's own suites. A suite that isolates via
# HOME=$TMP but never UNSETS an inherited QUINTESSENCE_DIR gets its walk-up discovery silently
# beaten — env wins over config by design — and writes its fixture HEADs into whatever store
# QUINTESSENCE_DIR points at instead of its own throwaway sandbox.
#
# This test reproduces exactly that invocation shape (QUINTESSENCE_DIR pre-exported before the
# suite runs, matching setup.sh's real environment) against a DECOY scratch store that is never
# Thomas's real store, and asserts the decoy stays completely empty. It targets the two suites the
# finding named (test-multi-store.sh, test-recall-composition.sh) directly, independent of
# setup.sh's own `env -u QUINTESSENCE_DIR` guard — so a regression in either suite's own defensive
# `unset` is caught even if setup.sh's invocation-side fix stays intact.
set -u
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
DECOY="$(mktemp -d)"; trap 'rm -rf "$DECOY"' EXIT

fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

for suite in test-multi-store.sh test-recall-composition.sh; do
  [ -e "$DECOY" ] || mkdir -p "$DECOY"
  before="$(find "$DECOY" -mindepth 1 2>/dev/null | wc -l)"
  # QUINTESSENCE_DIR pre-exported into the child's environment BEFORE it runs — exactly what
  # `bash setup.sh` (steps 1-4 export it) or README's manual `export QUINTESSENCE_DIR=...` path
  # leaves in the invoking shell when tests/run.sh is later run in it.
  QUINTESSENCE_DIR="$DECOY" bash "$HERE/$suite" >/dev/null 2>&1
  child_rc=$?
  after="$(find "$DECOY" -mindepth 1 2>/dev/null | wc -l)"
  # The child's EXIT STATUS is read, not discarded. "It wrote nothing into the decoy" is trivially
  # true of a suite that died on its first line, so without this the assertion reports `ok` for a
  # suite that never ran — a negative control that cannot fail. Its own failures are not this
  # file's business, but a suite that cannot run at all cannot testify about isolation either.
  if [ "$child_rc" -ne 0 ]; then
    no "$suite exited $child_rc under a pre-exported QUINTESSENCE_DIR -- it wrote nothing into the decoy, but a suite that did not run proves nothing about isolation. Run it directly to see why."
  elif [ "$after" -eq "$before" ]; then
    ok "$suite left the pre-exported decoy QUINTESSENCE_DIR untouched"
  else
    no "$suite wrote into the pre-exported decoy QUINTESSENCE_DIR ($before -> $after entries) -- walk-up isolation defeated by an inherited env var"
  fi
done

# --- the same family, one level up: any suite that RUNS the installer must isolate -------------
# setup.sh derives BIN_DIR, the store, the memory dir and the skills dir from $HOME, so a suite
# that executes it without a throwaway HOME reconfigures the live install of whoever runs the
# documented gate `bash tests/run.sh`: ~/.local/bin/qq is repointed at the checkout under test and
# QUINTESSENCE_DIR/QQ_MEMDIR are rewritten to the defaults. test-setup-wire.sh shipped exactly
# that way and it reached a real machine before a review caught it (seventh pass, F1). It then
# shipped a SECOND way: HOME isolated, and the seven environment overrides that route past HOME
# left to flow straight through (eighteenth pass, F1). Both defences are required below.
#
# Checked statically, and here rather than by observation, because CI cannot see this: the runner's
# HOME is itself throwaway, so the damage leaves no trace there and the pipeline stays green. The
# check is on the whole glob, not a named list, so a new suite is examined the day it is written —
# the instance was fixed once already and came back in another file. Whether it is CAUGHT depends
# on the detector below, which reads text and says plainly where it stops; the earlier version of
# this sentence promised coverage the detector did not have, which is how the direct-execution
# spelling passed for two commits.

# Does this suite RUN the installer? The first version of this detector asked for an interpreter
# word -- `(bash|sh)[[:space:]]+...setup\.sh` -- and setup.sh is mode 0755 with a shebang, so
# DIRECT execution (`"$ENGINE/setup.sh" --wire-claude`) is the natural spelling and went entirely
# unflagged, under a comment claiming the check covered any new suite (fourteenth pass, F4).
#
# So: normalise rather than enumerate spellings. Break each line at every command separator, drop
# leading VAR=value assignments and wrapper words, and ask what is left at the FRONT of the
# command. The setup.sh path in that position is an invocation; the same path as an argument to
# grep or readlink is not, because something else holds the front. The whole file is read,
# heredocs included, since a heredoc that runs the installer runs it.
#
# MATCH ON THE TOKEN, NOT ON AN INTERPRETER GRAMMAR. The second version asked what the FRONT of
# the command looked like and spelled the answer as a path regex, and one quote placed before the
# slash was enough to defeat it. Measured against the five spellings a reviewer tried: `bash
# "$ENGINE/setup.sh"` and `"$ENGINE/setup.sh"` were seen; `"$ENGINE"/setup.sh`, `timeout 60 bash
# "$ENGINE/setup.sh"` and `stdbuf -o0 bash "$ENGINE/setup.sh"` were all MISSED (eighteenth pass,
# F2). Exactly one suite matched at the time, so any of those respellings emptied the loop.
#
# So the setup.sh path is recognised as a WORD first and replaced by a single sentinel, and only
# then is the question "is it at the front" asked. That decouples the two: quoting, path shape and
# `$VAR` placement are all handled by the tokenizer, and the front test no longer has to know how
# a path can be spelled. A word only counts as the token when the name ENDS the word (`setup.sh`
# followed by a quote and then whitespace or end of line) — which is also what keeps this file's
# OWN sed program, whose text contains `setup\.sh` mid-word, from reading as an invocation of it.
#
# Leading words are then stripped generously rather than from a list of known interpreters: an
# assignment, a wrapper with its options or numeric arguments, or a shell EXPANSION — the last of
# which is what `setsid "${_ISOLATED_ENV[@]}" "$@" bash "$ENGINE/setup.sh"` needs, and which no
# amount of extending the wrapper list would have reached. Erring toward flagging is deliberate and
# unchanged: the cost of a false flag is a suite being told to isolate, which is loud and cheap.
#
# WHAT IT DOES NOT REACH, still: an invocation whose PATH is built at runtime and never appears as
# a word — `installer="$ENGINE/setup.sh"; $installer`, or `eval "$cmd"`. The assignment there does
# carry the token, so this particular spelling is in fact flagged by the tokenizer; a path assembled
# from fragments (`"$ENGINE/setup"".sh"`, `$ENGINE/$name`) is not, and no static check can. It also
# reads text, not behaviour: a suite that runs the installer through a helper script in another file
# is invisible here, and a suite that MENTIONS the token at the front of a command it never runs is
# flagged. Both are stated because a guard on one axis makes the whole question look settled.
runs_installer() {
  # Comments stripped first: this very file discusses `bash setup.sh` in prose, and several
  # suites grep setup.sh without running it.
  # `${...}` is collapsed to a bare `$X` FIRST, for the same reason `$(...)` is collapsed below:
  # the separator split further down breaks lines at `{` and `}`, which would tear
  # `"${_ISOLATED_ENV[@]}"` into three fragments and leave a bare quote at the front of the one
  # that carries the invocation. Collapsed to `$X` rather than to `X` so it still reads as an
  # expansion to the leading-word stripper.
  local body; body="$(grep -vE '^[[:space:]]*#' "$1" | sed -E 's/\$\{[^{}]*\}/$X/g')"
  {
    # (a) the OUTER command on each line, with command substitutions collapsed to one token, so
    #     an isolating prefix like HOME="$(mktemp -d)" stays a single assignment word. Splitting
    #     on the parens instead lost exactly that case in testing.
    printf '%s\n' "$body" | sed -E 's/\$\([^()]*\)/X/g'
    # (b) commands INSIDE a substitution or subshell, reached by splitting at the parens.
    printf '%s\n' "$body" | tr '()' '\n\n'
  } | tr ';&|{}`' '\n' \
    | sed -E 's#[^[:space:]]*setup\.sh(["'"'"']?)([[:space:]]|$)#@SETUP@\2#g' \
    | sed -E ':top
s/^[[:space:]]+//
s/^[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+//; t top
s/^(bash|sh|env|exec|command|sudo|time|timeout|stdbuf|setsid|nohup|nice|ionice|chrt|eval|source|\.|if|then|else|do|while|until|!)([[:space:]]+(-[^[:space:]]+|[0-9]+[a-z]*))*[[:space:]]+//; t top
s/^["'"'"']?\$[^[:space:]]*[[:space:]]+//; t top
' \
    | grep -q '^@SETUP@'
}

# TWO defences are required of an installer-running suite, because HOME alone was not enough and
# the way it was not enough cost a live install. setup.sh honours five environment overrides and
# qq-config.sh two more, every one of which routes past a throwaway HOME straight to the operator's
# real files: `QQ_CONFIG=/path/to/decoy bash tests/run.sh` reported ALL 23 suites passed and
# rewrote the decoy to a directory the suite then deleted (eighteenth pass, F1).
#
# So the second requirement is `env -i` — an allowlist, not an unset-list. Requiring the CLOSED
# construction rather than a list of neutralised names is the whole point: a name added to
# setup.sh next Tuesday is excluded without anyone editing anything, whereas a check that counted
# `unset` lines would certify a suite that had merely remembered today's five.
#
# WHAT THESE TWO CHECKS ARE WORTH, said plainly, because "the gate is green" reads as more than
# this and did so for two commits. Both read TEXT. `HOME=` matches an assignment anywhere in the
# file — without `export`, in a comment that survived the strip, inside a heredoc, or in a branch
# that never executes — and `env -i` matches the string anywhere at all, including in this very
# sentence were it in the scanned file. Neither knows whether the construction is on the path that
# actually launches the installer, nor what is IN the allowlist: a suite that wrote
# `env -i QQ_CONFIG="$QQ_CONFIG" ...` passes both. What makes them worth having anyway is that
# they are cheap, they run on the whole glob so a new suite is examined the day it is written, and
# the thing they cannot check — that the isolation actually holds — is checked BEHAVIOURALLY next
# door, in test-setup-wire.sh, against a decoy of every override. This gate's job is to notice a
# suite that never tried; that suite's job is to prove the trying worked.
installer_suites=0
for suite in "$HERE"/test-*.sh; do
  runs_installer "$suite" || continue
  installer_suites=$((installer_suites + 1))
  name="$(basename "$suite")"
  missing=""
  grep -qE '^[[:space:]]*(export[[:space:]]+)?HOME=' "$suite" || missing="$missing HOME"
  grep -q 'env -i' "$suite" || missing="$missing env-i"
  if [ -z "$missing" ]; then
    ok "$name runs the installer with a throwaway HOME and under an env -i allowlist"
  else
    no "$name runs setup.sh without$missing -- the gate would reconfigure the invoking user's install (HOME covers the \$HOME-derived paths; env -i covers the environment overrides that route past HOME entirely)"
  fi
done

# AN EMPTY LOOP CERTIFIES NOTHING. This is the load-bearing assertion of the block above, and it
# is here because the block above spent two commits without it. The detector matched exactly one
# suite; a respelling that the detector could not read therefore did not turn the gate red, it
# emptied the loop — `2 ok, 0 failed`, exit 0, nothing reporting the loss, and the gate went on
# certifying the very suite that was rewriting live installs (eighteenth pass, F2). It happened
# again during this round's own CRITICAL fix, which respelled the invocation and silently emptied
# the loop a third time.
#
# So the count is asserted, not assumed. A repository with genuinely no installer-running suite is
# not a state this file can distinguish from a detector that stopped working, and the safe answer
# to that ambiguity is red: whoever removes the last such suite deletes this assertion in the same
# commit and says so, which is exactly the conversation that did not happen twice.
if [ "$installer_suites" -eq 0 ]; then
  no "the installer-invocation detector matched NO suite -- either every suite that runs setup.sh was removed, or a respelling defeated the detector and this gate is now certifying nothing. It has been the second twice. Check runs_installer against the suite you expect it to match before assuming the first."
else
  ok "the installer-invocation detector found $installer_suites suite(s) to check"
fi

echo "----------------------------------------"
echo "$pass ok, $fail failed"
[ "$fail" -eq 0 ]
