#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# qq-redact.sh — OPTIONAL involuntary-injection redaction. When the active model has a limited
# reader profile, or is UNKNOWN, exclude a configured set of topic slugs from the
# context that hooks inject WITHOUT the user asking: semantic auto-recall (resume-match.sh), the
# SessionStart open-loops digest (inject-contract.sh), and per-command doc-hints (prederive-
# recall.sh). The slugs stay fully reachable via explicit `qq show` — this only removes the
# INVOLUNTARY surfacing. Rationale: an auto-injected snippet of certain topics can cause a silent
# downgrade for some readers; removing that involuntary injection preserves the limited reader
# while the full-access reader still gets full recall.
#
# SOURCED by the hook scripts. Safe no-op unless a non-empty redact list is configured, so it is
# inert on a stock install. Detection reuses the transcript's message.model — the only reliable
# model signal (the banner is client-side; cf. fable-downgrade-scan). NOTE: at a given hook firing
# the transcript shows the model that answered the PREVIOUS turn (the model for THIS turn is chosen
# model after the hook runs), so detection is "last-known model" and we FAIL SAFE: unknown -> redact.
#
# Config (dotenv ~/.config/quintessence/config or environment; env wins):
#   QQ_REDACT_FILE        redact-slug list (default ${XDG_CONFIG_HOME:-~/.config}/quintessence/redact-slugs)
#                         one entry/line; '#' comments; trailing '*' = prefix match; empty file = feature off.
#   QQ_WRITE_TRUSTED_MODEL  model trusted to author on the write path (default: empty — see
#                         quintessence/config.py KEYS; read side consults this to
#                         distinguish a known limited reader from an unknown session,
#                         but both are withheld from identically — only the
#                         full-access prefix receives unfiltered content)
#   QQ_SAFE_MODEL_PREFIX  the ungated model id prefix (default claude-opus-)

# Make the documented "dotenv or environment; env wins" contract TRUE even when a caller sources
# this file WITHOUT qq-config.sh first (e.g. a standalone harness hook like meta-harness's
# local-time-and-closure.sh). Idempotent and env-wins (_qq_load_config never overwrites a set
# var), so the engine's own hooks — which source qq-config.sh before this file — are unaffected.
# Regression this closes: P6 genericized the in-script QQ_WRITE_TRUSTED_MODEL default to EMPTY and
# moved the deployment value into the config file; an env-only read here silently disabled the
# model gate for exactly the bare-sourcing callers (2026-07-03, found via Opus-push leakage
# into a limited reader session).
. "$(dirname "${BASH_SOURCE[0]:-$0}")/qq-config.sh" 2>/dev/null || true

_qq_redact_file() {
  printf '%s' "${QQ_REDACT_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/quintessence/redact-slugs}"
}

_qq_redact_entries() {   # cleaned entries (strip inline #comments, trim, drop blanks/comment lines)
  local f; f="$(_qq_redact_file)"
  [ -r "$f" ] || return 0
  sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//' "$f" 2>/dev/null \
    | grep -vE '^[[:space:]]*(#.*)?$' 2>/dev/null
}

qq_redact_active() {     # 0 if a non-empty redact list is configured (feature on)
  [ -n "$(_qq_redact_entries)" ]
}

qq_model_mode() {        # $1 = transcript path; echoes fable|opus|unknown from last message.model
  local tp="$1" m
  local gated="${QQ_WRITE_TRUSTED_MODEL:-}"
  local safe="${QQ_SAFE_MODEL_PREFIX:-claude-opus-}"
  [ -n "$tp" ] && [ -r "$tp" ] || { echo unknown; return 0; }
  # The NEWEST `type:"assistant"` entry's message.model, and STOP there — never walk back to an
  # older entry. Kept byte-parallel with quintessence/authgate.model_identity, which learned this
  # the hard way: rejecting a malformed newest entry by "keep looking" is a fail-OPEN, because
  # every step backwards can only surface a different model and the only direction that matters is
  # that it must never be a more trusted one than the session is actually running.
  #
  # Parsed, not scanned for the text `"model": "..."`. That scan is right on every transcript today
  # (measured over 1112: unescaped matches occur on assistant entries and nowhere else) but rests
  # on no STRUCTURED tool result carrying a `model` key, and those are stored unescaped on `user`
  # entries. This is a TRUST decision, so it must not rest on what tools happen to return.
  # No parser -> empty -> unknown -> untrusted, the fail-safe direction.
  m=""
  if command -v jq >/dev/null 2>&1; then
    _last="$(jq -cR 'fromjson? | select(.type == "assistant")' <"$tp" 2>/dev/null | tail -1)"
    [ -n "$_last" ] && m="$(printf '%s' "$_last" \
      | jq -r 'if (.message | type) == "object" and (.message.model | type) == "string"
               then .message.model else "" end' 2>/dev/null)"
  elif command -v python3 >/dev/null 2>&1; then
    m="$(python3 -c '
import json, sys
out = ""
for line in sys.stdin:
    try:
        o = json.loads(line)
    except ValueError:
        continue
    if isinstance(o, dict) and o.get("type") == "assistant":
        msg = o.get("message")
        v = msg.get("model") if isinstance(msg, dict) else None
        out = v if isinstance(v, str) else ""          # newest wins, and it is FINAL
print(out)
' <"$tp" 2>/dev/null)"
  fi
  # A control or format character never classifies: it splits the line for one reader and not
  # another, and both bash branches used to disagree with python about it.
  case "$m" in *[[:cntrl:]]*) m="" ;; esac
  # empty model (fresh transcript) and empty gated id must BOTH resolve unknown — a bare
  # `case "" in "")` would otherwise spuriously report "fable" on a gate-off install
  [ -n "$m" ] || { echo unknown; return 0; }
  if [ -n "$gated" ] && [ "$m" = "$gated" ]; then echo fable; return 0; fi
  # belt-and-braces: an empty safe prefix must never reach the case (""* matches everything →
  # every session would read as ungated and redaction would silently turn OFF estate-wide).
  # Unreachable today — the ${QQ_SAFE_MODEL_PREFIX:-} read above collapses empty→default — this
  # guards the failure staying impossible if that ever becomes ${QQ_SAFE_MODEL_PREFIX-}.
  [ -n "$safe" ] || { echo unknown; return 0; }
  case "$m" in
    "$safe"*)  echo opus ;;
    *)         echo unknown ;;
  esac
}

qq_should_redact() {     # $1 = transcript path; 0 if we should redact now (feature on AND not on the ungated model)
  qq_redact_active || return 1
  case "$(qq_model_mode "$1")" in
    opus) return 1 ;;    # ungated model -> full recall
    *)    return 0 ;;    # gated or unknown -> redact (fail safe)
  esac
}

qq_slug_redacted() {     # $1 = a path or slug; 0 if it matches a configured redact entry
  local raw="$1" base e pre
  base="${raw##*/}"; base="${base%.md}"
  while IFS= read -r e; do
    [ -n "$e" ] || continue
    case "$e" in
      *\*) pre="${e%\*}"; case "$base" in "$pre"*) return 0 ;; esac
                          case "$raw"  in *"$pre"*) return 0 ;; esac ;;
      *)   [ "$base" = "$e" ] && return 0
           case "$raw" in *"$e"*) return 0 ;; esac ;;
    esac
  done < <(_qq_redact_entries)
  return 1
}

qq_redact_filter_lines() {   # $1 = transcript path; stdin->stdout, drop lines mentioning a redact slug when should_redact
  local tp="$1" pats
  if qq_should_redact "$tp"; then
    # a bare wildcard entry (a line that is exactly '*') means withhold everything —
    # detect it before building patterns, since command substitution would strip the
    # trailing empty line the wildcard collapses to and silently lose the match-all intent
    if _qq_redact_entries | grep -qxF '*'; then
      cat > /dev/null
    else
      pats="$(_qq_redact_entries | sed -E 's/\*$//')"
      if [ -n "$pats" ]; then grep -vFf <(printf '%s\n' "$pats"); else cat > /dev/null; fi
    fi
  else
    cat
  fi
}
