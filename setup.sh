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
# Env overrides:  BIN_DIR  QUINTESSENCE_DIR  QQ_MEMDIR  CLAUDE_SETTINGS
# Does NOT edit your shell profile (prints the PATH line). Nothing here is destructive.
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
_qd_explicit="${QUINTESSENCE_DIR+x}"; _mem_explicit="${QQ_MEMDIR+x}"   # was it explicitly provided?
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
export QUINTESSENCE_DIR="${QUINTESSENCE_DIR:-$HOME/quintessence}"
export QQ_MEMDIR="${QQ_MEMDIR:-$HOME/.quintessence-memory}"
CLAUDE_SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
WIRE=0; [ "${1:-}" = "--wire-claude" ] && WIRE=1

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
import json, os, shutil, sys, time
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
if not changed:
    print("  = settings.json: 4 qq hooks already wired to this dist (no change)")
else:
    if os.path.exists(path):
        bak = f"{path}.qqbak-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        shutil.copy2(path, bak); print(f"  • backed up settings.json → {os.path.basename(bak)}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(s, f, indent=2, ensure_ascii=False); f.write("\n")
    os.replace(tmp, path)
    for c in changed: print(f"  ↻ {c}")
    print("  NOTE: only the 4 qq hooks were managed; all other hooks left untouched.")
    print("  NOTE: SessionStart now uses inject-contract.sh (CONTRACT + optional contract-extra +")
    print("        digest); if you relied on a custom SessionStart command (e.g. breadcrumbs), keep")
    print("        it as a SEPARATE additional SessionStart hook — they concatenate.")
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
if [ -r "$HERE/tests/run.sh" ]; then
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
