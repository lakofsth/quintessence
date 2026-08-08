#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# One-shot post-install setup – IDEMPOTENT: re-running changes only what has drifted, and names
# what it changed. A user (or their agent) just runs:   bash setup.sh
#
#   bash setup.sh                 link CLIs + create store + write config (never touches settings.json)
#   bash setup.sh --wire-claude   ALSO idempotently wire the 4 qq hooks into ~/.claude/settings.json
#                                 (Option B). Repoints a drifted/old qq hook, adds a missing one,
#                                 and leaves every non-qq hook untouched. Backs up before any change.
#
# Env overrides:  BIN_DIR  QUINTESSENCE_DIR  QQ_MEMDIR  CLAUDE_SETTINGS  SKILLS_DIR
# (and QQ_CONFIG / XDG_CONFIG_HOME, which qq-config.sh honours and which therefore decide WHICH
#  config file the store-location keys below are written into. Five here plus those two is what
#  tests/test-setup-wire.sh derives and proves it excludes; the line said four until an operator
#  with QQ_CONFIG exported ran the test gate and had their config rewritten — eighteenth pass, F1.)
# Does NOT edit your shell profile (prints the PATH line). Nothing here is destructive.
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
_qd_explicit="${QUINTESSENCE_DIR+x}"; _mem_explicit="${QQ_MEMDIR+x}"   # was it explicitly provided?
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
export QUINTESSENCE_DIR="${QUINTESSENCE_DIR:-$HOME/quintessence}"
export QQ_MEMDIR="${QQ_MEMDIR:-$HOME/.quintessence-memory}"
CLAUDE_SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
WIRE=0; [ "${1:-}" = "--wire-claude" ] && WIRE=1
# --no-self-check: skip step 5. The ONE caller is tests/test-setup-wire.sh, which runs this
# installer for real to pin its env isolation. Without the flag that is a loop -- step 5 runs
# tests/run.sh, run.sh runs test-setup-wire.sh, test-setup-wire.sh runs this installer -- and
# each turn also spawns a python suite, so the box fills with work while every assertion still
# passes. Deliberately a FLAG and not an env var: run_installer launches under `env -i` with an
# allowlist, so an env marker would be stripped exactly where it is needed, and adding one to
# that allowlist would collide with the pin that proves the allowlist excludes every documented
# override. Scanned separately from $1 so --wire-claude's position-1 semantics are unchanged.
SELFCHECK=1
for _arg in "$@"; do [ "$_arg" = "--no-self-check" ] && SELFCHECK=0; done

# ---- 1. CLI on PATH (relink only if the target differs) ----------------------------------
# `qq` is the ONE user entry point; qq-search/qq-ask stay engine-internal (exec targets for
# `qq search`/`qq ask` and the hooks, always reached engine-relative — never via PATH).
mkdir -p "$BIN_DIR"
for c in qq tsk; do
  if [ "$(readlink "$BIN_DIR/$c" 2>/dev/null)" = "$HERE/$c" ]; then
    echo "  = $c link already points here"
  else
    ln -sf "$HERE/$c" "$BIN_DIR/$c"; echo "  ↻ linked $c → $BIN_DIR"
  fi
done

# ---- 2. store + config (qq init is idempotent; config set only-if-different) --------------
"$HERE/qq" init >/dev/null
_setcfg() { # KEY VALUE — set only when the current resolved value differs
  local cur; cur="$("$HERE/qq" config get "$1" 2>/dev/null || true)"
  if [ "$cur" = "$2" ]; then echo "  = config $1 already = $2";
  else QQ_ALLOW_RELOCATE=1 "$HERE/qq" config set "$1" "$2" >/dev/null; echo "  ↻ config $1 = $2"; fi   # setup legitimately seeds store-location keys
}
_cfg_pref() { # KEY EXPLICIT_FLAG VALUE — honor explicit env; else keep existing; else seed default
  local cur; cur="$("$HERE/qq" config get "$1" 2>/dev/null || true)"
  if [ -n "$2" ]; then _setcfg "$1" "$3"
  elif [ -n "$cur" ]; then echo "  = config $1 kept (existing = $cur)"
  else _setcfg "$1" "$3"; fi
}
_cfg_pref QUINTESSENCE_DIR "${_qd_explicit:-}" "$QUINTESSENCE_DIR"
_cfg_pref QQ_MEMDIR "${_mem_explicit:-}" "$QQ_MEMDIR"

# ---- 3. (opt-in) idempotently wire the 4 qq hooks into settings.json ----------------------
if [ "$WIRE" -eq 1 ]; then
  command -v python3 >/dev/null || { echo "  ✗ --wire-claude needs python3"; exit 1; }
  HERE="$HERE" CLAUDE_SETTINGS="$CLAUDE_SETTINGS" python3 - <<'PY'
import json, os, shutil, stat, sys, time
here = os.environ["HERE"]; path = os.environ["CLAUDE_SETTINGS"]
# Desired qq hooks: (event, matcher-or-None, command, [substrings that ID an existing qq hook to
# reconcile]). A hook is "ours" if its command contains any recognizer — so we repoint a drifted
# path (e.g. an old engine dir) or a hand-written equivalent, and NO-OP once it already matches.
desired = [
  ("SessionStart",   None,   f'bash "{here}/hooks/inject-contract.sh"',
       ["inject-contract.sh", "CONTINUITY DISCIPLINE", "CONTRACT.md"]),
  ("UserPromptSubmit", None, f'bash "{here}/resume-match.sh"',     ["resume-match.sh"]),
  ("PreToolUse",     "Bash", f'bash "{here}/prederive-recall.sh"', ["prederive-recall.sh"]),
  ("Stop",           None,   f'bash "{here}/finalize-check.sh"',   ["finalize-check.sh"]),
]
# THE SYMLINK-CYCLE REFUSAL SITS BEFORE THE READ, and the position is the whole of it. This check
# used to live beside the write, where the engine has it — and there it could never fire, because
# `open(path)` on a cycle raises ELOOP first and the handler below exits on it. Replacing the
# condition with `if False:` therefore changed nothing anywhere, which is how the mirror's cycle
# handling went unpinned while the parity note listed it among the properties carried across
# (nineteenth pass, F3). Before the read it does its own job: the operator is told they have a
# cycle, rather than that their settings.json is "unreadable" with an errno.
# `path` is deliberately NOT rebound here. The backup further down is taken beside the LINK, not
# beside the link's target, and resolving this early would move it.
if os.path.islink(os.path.realpath(path)):     # realpath gives up on a cycle, returning a link
    print("  ✗ settings.json is a symlink cycle — not touching it"); sys.exit(1)
try:
    with open(path) as f: s = json.load(f)
except FileNotFoundError:
    s = {}
except Exception as e:
    print(f"  ✗ settings.json unreadable ({e}) — not touching it"); sys.exit(1)
s.setdefault("hooks", {})
changed = []
for event, matcher, cmd, recog in desired:
    groups = s["hooks"].setdefault(event, [])
    found = False
    for g in groups:
        for h in g.get("hooks", []):
            if h.get("type") == "command" and any(r in h.get("command", "") for r in recog):
                found = True
                if h["command"] != cmd:
                    h["command"] = cmd            # repoint; preserve sibling keys (asyncRewake etc.)
                    changed.append(f"{event}: repointed → qq dist")
    if not found:
        grp = {"hooks": [{"type": "command", "command": cmd}]}
        if matcher: grp["matcher"] = matcher
        groups.append(grp)
        changed.append(f"{event}: added qq hook")
# ---- the three constants this block shares with quintessence/atomicio.py --------------------
# PARITY NOTE, kept current because it is the only thing holding these two in step. setup.sh runs
# before (and without) the package on the path, so it cannot import the primitive and open-codes
# it. What is mirrored: the resolve-first symlink handling, the symlink-cycle refusal, the O_EXCL
# unique temp, the mode carry, the BaseException cleanup, the name-length refusal and the
# stale-temp reclaim. What is NOT mirrored, deliberately: streaming (settings.json is small), the
# reclaim's caller-side grace tuning, the POSITION of the cycle refusal — the engine meets a cycle
# at the write and raises ELOOP there, while this block reads the file first, so the refusal has
# to come before the read or the read answers "unreadable" for it — and WHEN the reclaim runs. The
# engine sweeps after each write, which for it is every call: `atomic_write` always writes. This
# block's call may legitimately write nothing, because `--wire-claude` is idempotent, so "after
# each write" would mean "never again" for an install that has settled; it sweeps on every
# successful wire instead. Same rule, same directory, a reachability the engine gets for free.
# Same decisions, different lines, and the reason is written where each one sits. Anything changed
# on either side belongs on both, in the same commit; tests/test-setup-wire.sh lists which of
# these it reaches.
TEMP_INFIX = ".tmp."
TEMP_TOKEN_BYTES = 6                                    # -> 12 hex characters
TEMP_OVERHEAD = len(TEMP_INFIX) + 2 * TEMP_TOKEN_BYTES  # 17, same arithmetic as the engine's
TEMP_GRACE_SECONDS = 3600
# setup.sh has ONE consumer of name room the engine does not: the backup taken before any change.
# `.qqbak-` plus a 16-byte UTC stamp is 23 bytes, wider than the temp's 17, so the budget that
# actually binds here is the backup's whenever the target already exists. Checking only the temp's
# would have refused later, in the copy2, with the traceback this refusal exists to replace.
BACKUP_OVERHEAD = len(".qqbak-") + len("YYYYmmddTHHMMSSZ")   # 23


def _name_budget_refusal(parent, basename, overhead, what):
    """The refusal a too-long TARGET name earns, said in full -- or None if the name fits.

    Mirrors atomicio._name_budget_error, including the reason it returns None rather than always
    speaking: ENAMETOOLONG also comes from the whole PATH exceeding PATH_MAX, a different problem
    with a different fix, and answering that with a basename budget sends the operator to the
    wrong place. So the budget is checked before the message is claimed.
    """
    limit = 255                       # every filesystem qq is documented against
    try:
        limit = os.pathconf(parent, "PC_NAME_MAX")
    except (OSError, ValueError, AttributeError):
        pass
    used = len(os.fsencode(basename))
    if used + overhead <= limit:
        return None
    return (f"  x CLAUDE_SETTINGS names a file this cannot write: {what} needs {overhead} bytes "
            f"of room beside the target, this name is {used} bytes and the directory allows "
            f"{limit}, so a settings.json written here may be at most {limit - overhead} bytes "
            f"of name. Shorten CLAUDE_SETTINGS, or point it at a shorter basename.")


def _reclaim_stale_temps(parent, basename):
    """Remove leftover temps for THIS target older than the grace period. Silent, deliberately.

    Carried across from atomicio because setup.sh took atomicio's UNIQUE temp names without it.
    The old idiom reused one `settings.json.tmp`, so litter self-limited to a single file the next
    run overwrote; unique names do not self-limit, and every interrupted `--wire-claude` left
    another permanent file in ~/.claude, which nothing in the package sweeps (eighteenth pass, F3).

    Two conditions, exactly as the engine states them, and one spelling: the generated
    `<basename>.tmp.<12 lowercase hex>`, by prefix AND by tail. The tail is the writer's own width
    and nothing wider -- an 8-or-more window here would delete an operator's
    `settings.json.tmp.20260801` an hour after they wrote it (eighteenth pass, F5).

    The tail must also carry at least one of `abcdef`, which is NARROWER than what the writer
    emits, for the reason the engine gives beside its own constants: twelve digits are twelve
    valid hex characters, so `settings.json.tmp.202608041200` -- `date +%Y%m%d%H%M`, which is
    exactly how a person names a backup of a settings file -- sat inside the width and died an
    hour later (twenty-first pass, F2). The cost is that roughly one generated temp in 281 has an
    all-decimal tail and is no longer reclaimed here. Under-deletion, on purpose.

    A bare `settings.json.tmp` SURVIVES. The engine matched that spelling exactly until
    2026-08-04 and this mirror followed; the clause is gone from both (owner's ruling, twentieth
    pass F2). This writer cannot produce that name, so the rule could only delete a file the
    installer did not write -- an operator's own backup of their settings, as likely as anything.
    A pre-atomicio `~/.claude/settings.json.tmp` is cleared by `tools/reclaim_legacy_temps.py`
    instead: once, on purpose, listing before it deletes.
    """
    prefix = basename + TEMP_INFIX
    cutoff = time.time() - TEMP_GRACE_SECONDS
    try:
        entries = list(os.scandir(parent))
    except OSError:
        return
    for entry in entries:
        _, sep, token = entry.name.rpartition(TEMP_INFIX)
        generated = (bool(sep) and len(token) == 2 * TEMP_TOKEN_BYTES
                     and all(c in "0123456789abcdef" for c in token)
                     and any(c in "abcdef" for c in token))
        if not (entry.name.startswith(prefix) and generated):
            continue
        try:
            if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            pass                      # housekeeping must never turn a successful write into a failure


if not changed:
    print("  = settings.json: 4 qq hooks already wired to this dist (no change)")
else:
    # The budget is checked BEFORE anything is written, so a name that can never work is refused
    # with arithmetic instead of failing halfway through with a kernel message naming a path the
    # operator never chose. `ASSURANCE.md` promises "the refusal is loud and states the
    # arithmetic" -- true of the engine and false here, where a 245-byte basename gave a raw
    # traceback out of shutil.copy2 while the idiom this replaced (a 4-byte `.tmp`, no backup)
    # succeeded (eighteenth pass, F4).
    #
    # The BACKUP's budget is checked here, against the UNRESOLVED path, because that is where
    # copy2 writes it -- beside the link, not beside the link's target. The temp's budget is
    # checked further down against the RESOLVED parent, because that is where the temp is created.
    # One check for both would have been wrong for exactly the symlinked-into-a-dotfiles-repo case
    # this block exists to support.
    if os.path.exists(path):
        _refusal = _name_budget_refusal(os.path.dirname(path) or ".", os.path.basename(path),
                                        BACKUP_OVERHEAD, "the pre-change backup `.qqbak-<stamp>`")
        if _refusal:
            print(_refusal); sys.exit(1)
        bak = f"{path}.qqbak-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        shutil.copy2(path, bak); print(f"  • backed up settings.json → {os.path.basename(bak)}")
    # Resolve symlinks first: os.replace does NOT follow them, so writing a settings.json the
    # operator has symlinked into a dotfiles repo would replace the LINK with a regular file and
    # silently strand the repo copy. O_EXCL also means a planted <path>.tmp.* symlink cannot
    # redirect the write. Mirrors quintessence/atomicio.py; setup.sh cannot import it (it runs before/without the
    # package on the path), so it is open-coded. Keep the two in step.
    # A cycle here would still be a link after the resolve, and os.replace would clobber it — but
    # it cannot reach this line: the refusal for that is above, before the read that would have
    # turned it into an "unreadable" exit first. One live check, not two, and it is the one an
    # input can actually arrive at.
    path = os.path.realpath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # The temp's budget, against the RESOLVED parent -- that is the directory the temp is created
    # in and the one whose PC_NAME_MAX binds it. Refused before the O_EXCL loop so the failure is
    # the arithmetic rather than a hundred ENAMETOOLONGs and an "could not create a unique temp".
    _refusal = _name_budget_refusal(os.path.dirname(path), os.path.basename(path),
                                    TEMP_OVERHEAD, "an atomic write's temp `.tmp.<12 hex>`")
    if _refusal:
        print(_refusal); sys.exit(1)
    # O_CREAT|O_EXCL|O_WRONLY with 0o666 so the KERNEL applies the umask, atomically, exactly as
    # plain open() would. Reading the umask means SETTING it (there is no read-only form) and it
    # is process-global -- the engine hit exactly that and had to drop it.
    # The destination's mode is read BEFORE the temp exists and passed to os.open, so the temp is
    # never created wider than the file it becomes -- the kernel still applies the umask on top.
    # Chmod'ing only afterwards left a window at 0o666 & ~umask (sixteenth pass, F7).
    carry = None
    try: carry = stat.S_IMODE(os.stat(path).st_mode)   # a REWRITE keeps the existing mode
    except OSError: pass                               # fresh file, or a filesystem with no modes
    f = fd = tmp = None
    try:
        for _try in range(100):
            # Spelled from the SAME two constants the reclaim above reads. It was a literal
            # ".tmp." and a literal 6 while the recogniser used the constants, so changing
            # TEMP_TOKEN_BYTES would have moved the recogniser and left the writer emitting the
            # old width -- and the reclaim would then have stopped claiming its own litter,
            # silently. The engine derives both from one constant for exactly this reason.
            cand = "%s%s%s" % (path, TEMP_INFIX, os.urandom(TEMP_TOKEN_BYTES).hex())
            try:
                fd = os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                             0o666 if carry is None else carry); tmp = cand; break
            except FileExistsError:
                continue
        if fd is None:
            raise OSError("could not create a unique temp beside %s" % path)
        # The handoff completes whether fdopen returns or raises: io.open closes the descriptor
        # itself when it fails part-way, so clearing fd only on success left the cleanup closing
        # the same number twice (sixteenth pass, F3) -- keep this in step with the engine.
        try: f = os.fdopen(fd, "w")
        finally: fd = None                      # f owns the descriptor now
        if carry is not None:
            try:
                os.chmod(tmp, carry)            # put back the bits the umask took off the create
            except OSError:
                pass                            # a filesystem with no unix modes refuses chmod
        with f: json.dump(s, f, indent=2, ensure_ascii=False); f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        # Every step runs even when one of them raises something that is not an Exception: a
        # Ctrl-C landing in the close would otherwise skip the unlink and strand the temp. The
        # interrupt is re-raised after the unlink rather than dropped. Same shape, and the same
        # reason, as the engine's cleanup loop (sixteenth pass, F1) -- keep the two in step.
        # `f` FIRST, then `fd`, then the unlink -- the engine's order, and the two are
        # complementary rather than alternatives: `fd` is cleared the moment `os.fdopen` returns,
        # because the file object owns the descriptor from then on, so between there and the
        # `with f:` below (a window containing the real `os.chmod` syscall) `f` is the ONLY thing
        # holding it. Closing only `fd` left that window leaking a descriptor on an interrupt,
        # under a parity note claiming this cleanup was mirrored (twenty-second pass, F3). At most
        # one of the two is ever open, so no step here closes a number twice.
        interrupt = None
        for _step in (lambda: f.close() if f is not None else None,
                      lambda: os.close(fd) if fd is not None else None,
                      lambda: os.unlink(tmp) if tmp is not None else None):
            try: _step()
            except Exception: pass
            except BaseException as exc:
                if interrupt is None: interrupt = exc
        if interrupt is not None: raise interrupt
        raise
    for c in changed: print(f"  ↻ {c}")
    print("  NOTE: only the 4 qq hooks were managed; all other hooks left untouched.")
    print("  NOTE: SessionStart now uses inject-contract.sh (CONTRACT + optional contract-extra +")
    print("        digest); if you relied on a custom SessionStart command (e.g. breadcrumbs), keep")
    print("        it as a SEPARATE additional SessionStart hook — they concatenate.")

# THE SWEEP RUNS ON EVERY SUCCESSFUL WIRE, changed or not, and its position is the whole of it.
# It used to sit beside the write, inside the `else:` above -- so it ran only on a run that
# rewrote settings.json. `--wire-claude` is idempotent, so an install reaches a steady state
# where nothing changes and the sweep is never called again; and the sequence that leaves the
# litter makes that permanent, because the retry that DOES have changes sweeps while the temp is
# still seconds old and inside the one-hour grace (twenty-second pass, F2). Nothing else sweeps
# `~/.claude`: `tools/reclaim_legacy_temps.py` deliberately excludes the generated spelling.
# A guard that cannot be reached is not a guard, and this is the second one in this block --
# the symlink-cycle refusal above sat below the read it was meant to precede (nineteenth pass,
# F3). Both are now pinned by cases in tests/test-setup-wire.sh that go red when the guard stops
# being CALLED, not merely when its condition is wrong: a cycle that must be refused as a cycle,
# and a no-change run that must still sweep.
#
# The resolve is repeated rather than assumed: on the changed path `path` was rebound to its real
# path further up, on the no-change path it is still the configured one, and the temp always
# lives beside the REAL file. `os.path.realpath` is idempotent, so one call is right for both.
# Reached only after a successful wire -- every refusal above exits, and a failed write re-raises.
_swept = os.path.realpath(path)
_reclaim_stale_temps(os.path.dirname(_swept), os.path.basename(_swept))
PY
fi

# ---- 3b. (opt-in) deploy bundled skills into ~/.claude/skills (symlink, like the CLIs) --------
# Same "wire into Claude" scope as the hooks above, so it rides on --wire-claude. Symlinks track
# the dist (no drift); backs up a pre-existing real skill file before replacing it.
if [ "$WIRE" -eq 1 ] && [ -d "$HERE/skills" ]; then
  SKILLS_DIR="${SKILLS_DIR:-$HOME/.claude/skills}"
  for s in "$HERE"/skills/*/; do
    src="${s%/}/SKILL.md"; [ -e "$src" ] || continue
    name="$(basename "$s")"; dest="$SKILLS_DIR/$name"
    if [ "$(readlink "$dest/SKILL.md" 2>/dev/null)" = "$src" ]; then
      echo "  = skill $name already linked here"
    else
      mkdir -p "$dest"
      if [ -f "$dest/SKILL.md" ] && [ ! -L "$dest/SKILL.md" ]; then
        mv "$dest/SKILL.md" "$dest/SKILL.md.qqbak-$(date -u +%Y%m%dT%H%M%SZ)"; echo "  • backed up existing $name skill"
      fi
      ln -sf "$src" "$dest/SKILL.md"; echo "  ↻ linked skill $name → $SKILLS_DIR"
    fi
  done
fi

# ---- 4. report ----------------------------------------------------------------------------
"$HERE/qq" doctor || true
echo
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Add to your shell profile so the CLIs are found on PATH:"
     echo "    export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
[ "$WIRE" -eq 0 ] && echo "(hooks NOT wired — re-run with --wire-claude for Option-B settings.json wiring)"

# ---- 5. post-install self-check ------------------------------------------------------------
# Runs tests/run.sh with QUINTESSENCE_DIR/QQ_MEMDIR explicitly unset for the child, regardless of
# what's exported in THIS shell (steps 1-4 above export both) — otherwise the self-check's own
# throwaway-store test suites would inherit the real store path and write fixture HEADs into it
# (human-1-store-pollution). The suites themselves also defensively unset both after their own
# HOME isolation, so this holds even if invoked some other way.
selfcheck_failed=0
if [ "$SELFCHECK" -eq 0 ]; then
  echo; echo "Self-check skipped (--no-self-check)."
elif [ -r "$HERE/tests/run.sh" ]; then
  echo; echo "Self-check (tests/run.sh)…"
  if out="$(env -u QUINTESSENCE_DIR -u QQ_MEMDIR bash "$HERE/tests/run.sh" 2>&1)"; then
    echo "  ✓ $(printf '%s\n' "$out" | tail -1)"
  else
    selfcheck_failed=1
    echo "  ⚠ self-check FAILED:"
    # Check git identity FIRST and name it as the likely root cause when the captured output
    # actually shows its signature — tests/run.sh injects a scratch identity for its own throwaway
    # stores, so a real missing-identity failure now only shows up if something bypassed that (e.g.
    # a suite that shells out to a *different* repo's git). Gate on the output, not just on whether
    # THIS shell's git is configured, so the note isn't shown for an unrelated failure.
    if printf '%s\n' "$out" | grep -qi "no author identity configured\|Please tell me who you are\|ambiguous argument 'HEAD'"; then
      echo "    likely cause: no git author identity configured. Set it once, then re-run:"
      echo "      git config --global user.name \"Your Name\""
      echo "      git config --global user.email you@example.com"
    fi
    echo "    $(printf '%s\n' "$out" | tail -1)"
    echo "    (re-run  bash tests/run.sh  yourself for full per-suite detail)"
  fi
fi
echo "Done. Read ONBOARDING.md (or invoke the /quintessence skill) for how to write/resume HEADs."
[ "$selfcheck_failed" -eq 0 ]
