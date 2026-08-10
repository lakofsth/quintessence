#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# test-redact.sh — coverage for the qq-redact.sh trust machinery (the READ-side model gate the
# AUTHORING GATE's write-side trust also reuses). Closes the 2026-07-03 test-suite gap noted
# when the P6 bare-source regression was found+fixed (dist ade82f8 + c55af14): until this
# suite, qq_model_mode / qq_should_redact / qq_slug_redacted / qq_redact_filter_lines had NO
# tests, and the bare-source (no qq-config.sh) hook path — the exact path the regression hid
# on — was never exercised. Every check here runs qq-redact.sh in a FRESH bash subprocess with
# a fully explicit environment (env -i keeps the invoking session's own QQ_*/CLAUDE_* out),
# so the suite pins:
#   - the dotenv-or-env contract THROUGH THE BARE-SOURCE PATH (ade82f8: sourcing qq-redact.sh
#     alone must load the install dotenv; a set env var must still win),
#   - the model-mode matrix incl. both empty-input fail-safes (empty model and empty gated id
#     both resolve 'unknown', never a spurious 'fable' — ade82f8's second half),
#   - redaction activation/matching/filtering semantics (fail safe: only confirmed-opus skips).
# No store involved — pure fixture files under mktemp.
set -u
ENGINE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0; pass=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL %s\n' "$1"; }

GATED=acme-gated-5

# fixture transcripts (the LAST message.model line is the signal)
# Transcript fixtures carry `"type":"assistant"` because a REAL entry does: model
# detection parses entries now rather than scanning the line for `"model":"..."`,
# so a fixture without the type field is a transcript Claude Code never writes.
OPUS_TP="$TMP/opus.jsonl";     printf '{"type":"assistant","message":{"model":"claude-opus-4-6"}}\n' > "$OPUS_TP"
FABLE_TP="$TMP/fable.jsonl";   printf '{"type":"assistant","message":{"model":"%s"}}\n' "$GATED" > "$FABLE_TP"
SONNET_TP="$TMP/sonnet.jsonl"; printf '{"type":"assistant","message":{"model":"claude-sonnet-4-5"}}\n' > "$SONNET_TP"
MULTI_TP="$TMP/multi.jsonl"    # older opus turn, newest fable turn: last-known wins
{ printf '{"type":"assistant","message":{"model":"claude-opus-4-6"}}\n'
  printf '{"type":"assistant","message":{"model":"%s"}}\n' "$GATED"
  printf '{"type":"user","no_model":true}\n'; } > "$MULTI_TP"
EMPTY_TP="$TMP/empty.jsonl"; : > "$EMPTY_TP"

# redact-slug fixtures
REDACT="$TMP/redact-slugs"
printf '# a comment\n\nssh-exposure\nsec-*   # inline comment\n' > "$REDACT"
EMPTY_REDACT="$TMP/redact-empty"; printf '# only a comment\n\n' > "$EMPTY_REDACT"

# run <fn+args...> with an explicit env, qq-redact.sh sourced BARE (no qq-config.sh first) —
# exactly how a standalone harness hook consumes it. env -i isolates from the invoking session.
run(){  # run VAR=val... -- fn args...
  local envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done; shift
  env -i HOME="$TMP" PATH="$PATH" "${envs[@]}" \
    bash -c '. "$1"; shift; "$@"' _ "$ENGINE/qq-redact.sh" "$@"
}

# ---- qq_model_mode matrix (bare-source path) ------------------------------------------------------
t(){ # t <desc> <want> <env...> -- qq_model_mode <tp>
  local desc="$1" want="$2"; shift 2
  local got; got="$(run "$@")"
  [ "$got" = "$want" ] && ok "model-mode: $desc -> $want" || no "model-mode: $desc -> got '$got', want '$want'"
}
t "gated id matches"            fable   QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_model_mode "$FABLE_TP"
t "safe prefix matches"         opus    QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_model_mode "$OPUS_TP"
t "other model"                 unknown QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_model_mode "$SONNET_TP"
t "last-known model wins"       fable   QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_model_mode "$MULTI_TP"
t "missing transcript"          unknown QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_model_mode "$TMP/nope.jsonl"
t "no transcript arg"           unknown QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_model_mode ""
t "fresh/empty transcript"      unknown QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_model_mode "$EMPTY_TP"
# ade82f8's second half: empty gated id must NEVER report 'fable', even for an empty model
t "empty gated id, fable tp"    unknown QQ_WRITE_TRUSTED_MODEL= QQ_CONFIG=/nonexistent -- qq_model_mode "$FABLE_TP"
t "empty gated id, empty tp"    unknown QQ_WRITE_TRUSTED_MODEL= QQ_CONFIG=/nonexistent -- qq_model_mode "$EMPTY_TP"
t "empty gated id, opus tp"     opus    QQ_WRITE_TRUSTED_MODEL= QQ_CONFIG=/nonexistent -- qq_model_mode "$OPUS_TP"
# custom safe prefix is honoured; empty-env prefix collapses to the default (bash ${VAR:-})
t "custom safe prefix"          opus    QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_SAFE_MODEL_PREFIX=claude-sonnet- QQ_CONFIG=/nonexistent -- qq_model_mode "$SONNET_TP"
t "empty safe prefix -> default" opus   QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_SAFE_MODEL_PREFIX= QQ_CONFIG=/nonexistent -- qq_model_mode "$OPUS_TP"

# ---- the ade82f8 bare-source dotenv contract ------------------------------------------------------
CONF="$TMP/config"
printf 'QQ_WRITE_TRUSTED_MODEL=%s\n' "$GATED" > "$CONF"
got="$(run QQ_CONFIG="$CONF" -- qq_model_mode "$FABLE_TP")"
[ "$got" = "fable" ] && ok "bare-source: install dotenv loads without qq-config.sh (P6 regression pinned)" \
  || no "bare-source: dotenv not loaded (got '$got')"
got="$(run QQ_CONFIG="$CONF" QQ_WRITE_TRUSTED_MODEL=something-else -- qq_model_mode "$FABLE_TP")"
[ "$got" = "unknown" ] && ok "bare-source: environment wins over the dotenv file" \
  || no "bare-source: env did not win (got '$got')"

# ---- qq_redact_active -----------------------------------------------------------------------------
run QQ_REDACT_FILE="$REDACT" QQ_CONFIG=/nonexistent -- qq_redact_active \
  && ok "active: populated redact file -> feature on" || no "active: populated file reported off"
run QQ_REDACT_FILE="$EMPTY_REDACT" QQ_CONFIG=/nonexistent -- qq_redact_active \
  && no "active: comment-only file reported ON" || ok "active: comment/blank-only file -> feature off"
run QQ_REDACT_FILE="$TMP/nope" QQ_CONFIG=/nonexistent -- qq_redact_active \
  && no "active: missing file reported ON" || ok "active: missing file -> feature off"

# ---- qq_slug_redacted -----------------------------------------------------------------------------
s(){ # s <desc> <want 0|1> <arg>
  local desc="$1" want="$2" arg="$3"
  run QQ_REDACT_FILE="$REDACT" QQ_CONFIG=/nonexistent -- qq_slug_redacted "$arg"; local got=$?
  [ "$got" -eq "$want" ] && ok "slug: $desc" || no "slug: $desc (rc=$got, want $want)"
}
s "exact slug matches"                    0 "ssh-exposure"
s "basename of a path matches"            0 "/kb/quintessence/ssh-exposure.md"
s ".md is stripped before matching"       0 "ssh-exposure.md"
s "trailing-* prefix glob matches"        0 "sec-incident-42"
s "raw substring (path) matches an entry" 0 "/kb/memory/project-ssh-exposure-notes.md"
s "unrelated slug does not match"         1 "roadmap"

# ---- qq_should_redact (fail-safe direction) -------------------------------------------------------
r(){ # r <desc> <want 0|1> <tp>
  local desc="$1" want="$2" tp="$3"
  run QQ_REDACT_FILE="$REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_should_redact "$tp"; local got=$?
  [ "$got" -eq "$want" ] && ok "should-redact: $desc" || no "should-redact: $desc (rc=$got, want $want)"
}
r "confirmed opus -> full recall"      1 "$OPUS_TP"
r "gated model -> redact"              0 "$FABLE_TP"
r "unrecognized model -> redact"       0 "$SONNET_TP"
r "unknown (no transcript) -> redact"  0 "$TMP/nope.jsonl"
run QQ_REDACT_FILE="$EMPTY_REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_should_redact "$TMP/nope.jsonl" \
  && no "should-redact: feature-off still redacts" || ok "should-redact: empty redact list -> feature off even for unknown"

# ---- qq_redact_filter_lines -----------------------------------------------------------------------
INPUT=$'resume ssh-exposure now\nplain line stays\nsee sec-incident-42 notes\n'
got="$(printf '%s' "$INPUT" | run QQ_REDACT_FILE="$REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_redact_filter_lines "$FABLE_TP")"
[ "$got" = "plain line stays" ] && ok "filter: gated model drops lines mentioning redact slugs" \
  || no "filter: gated-model filtering wrong: '$got'"
got="$(printf '%s' "$INPUT" | run QQ_REDACT_FILE="$REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_redact_filter_lines "$OPUS_TP")"
[ "$got" = "$(printf '%s' "$INPUT")" ] && ok "filter: confirmed opus passes everything through" \
  || no "filter: opus pass-through wrong: '$got'"
got="$(printf '%s' "$INPUT" | run QQ_REDACT_FILE="$EMPTY_REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_redact_filter_lines "$FABLE_TP")"
[ "$got" = "$(printf '%s' "$INPUT")" ] && ok "filter: feature off passes everything through" \
  || no "filter: feature-off pass-through wrong: '$got'"
# bare-wildcard list: every entry collapses to empty after stripping — must withhold, not pass through
WILDCARD_REDACT="$TMP/redact-wildcard"; printf '*\n' > "$WILDCARD_REDACT"
got="$(printf '%s' "$INPUT" | run QQ_REDACT_FILE="$WILDCARD_REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_redact_filter_lines "$FABLE_TP")"
[ -z "$got" ] && ok "filter: bare-wildcard list on gated model emits nothing (fail-safe)" \
  || no "filter: bare-wildcard list on gated model leaked input: '$got'"
got="$(printf '%s' "$INPUT" | run QQ_REDACT_FILE="$WILDCARD_REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_redact_filter_lines "$OPUS_TP")"
[ "$got" = "$(printf '%s' "$INPUT")" ] && ok "filter: bare-wildcard list on ungated model passes everything" \
  || no "filter: bare-wildcard list on ungated model wrong: '$got'"
# wildcard among named entries: must withhold everything regardless of wildcard position
WILD_LAST_REDACT="$TMP/redact-wild-last"; printf 'ssh-exposure\nsec-*\n*\n' > "$WILD_LAST_REDACT"
got="$(printf '%s' "$INPUT" | run QQ_REDACT_FILE="$WILD_LAST_REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_redact_filter_lines "$FABLE_TP")"
[ -z "$got" ] && ok "filter: wildcard LAST among named entries withholds everything" \
  || no "filter: wildcard LAST among named entries leaked input: '$got'"
WILD_FIRST_REDACT="$TMP/redact-wild-first"; printf '*\nssh-exposure\nsec-*\n' > "$WILD_FIRST_REDACT"
got="$(printf '%s' "$INPUT" | run QQ_REDACT_FILE="$WILD_FIRST_REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_redact_filter_lines "$FABLE_TP")"
[ -z "$got" ] && ok "filter: wildcard FIRST among named entries withholds everything" \
  || no "filter: wildcard FIRST among named entries leaked input: '$got'"
# named entries alone (no wildcard) still filter selectively
got="$(printf '%s' "$INPUT" | run QQ_REDACT_FILE="$REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_CONFIG=/nonexistent -- qq_redact_filter_lines "$FABLE_TP")"
[ "$got" = "plain line stays" ] && ok "filter: named entries alone filter only their matches" \
  || no "filter: named-only filtering wrong: '$got'"

# ---- integration: a REAL call site, end-to-end -----------------------------------------------------
# Everything above unit-tests qq-redact.sh's own functions directly. But every real caller (resume-
# match.sh, prederive-recall.sh, hooks/inject-contract.sh) gates its use of them behind
# `command -v qq_should_redact` after a `. qq-redact.sh 2>/dev/null || true` -- a SILENT-FAILURE-
# SHAPED guard: if that source ever stops working (file moved/renamed, a syntax error, a future
# refactor drops the function), nothing errors anywhere -- the guard just reads false and redaction
# becomes a silent no-op, leaking redact-listed content straight into an involuntary-injection
# surface. Drive resume-match.sh (the UserPromptSubmit hook) against a REAL throwaway qq store so a
# broken source is actually caught, not just the standalone functions. (Verified while writing this:
# copying resume-match.sh + qq-config.sh + qq-search + quintessence/ into a scratch dir WITHOUT
# qq-redact.sh reproduces exactly this leak -- the redact-listed slug's content comes straight
# through -- confirming this section fails loudly if that regression class recurs.)
IT="$TMP/it"; mkdir -p "$IT/home" "$IT/kb" "$IT/mem"
ISTORE="$IT/store"
qqenv() {  # qqenv -- cmd...   (the store/config env every call below shares)
  env HOME="$IT/home" QUINTESSENCE_DIR="$ISTORE" QQ_CONFIG="$IT/config" QQ_STATE_DIR="$IT/state" \
      QQ_MEMDIR="$IT/mem" QQ_KB_ROOT="$IT/kb" QQ_CACHE="$IT/cache.json" \
      QQ_OLLAMA_URL="http://127.0.0.1:1" \
      GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t \
      "$@"
}
qqenv "$ENGINE/qq" init "$ISTORE" >/dev/null 2>&1
ln -s "$ISTORE" "$IT/kb/qq"    # a real corpus source subdir, same shape resume-match.sh expects
# "ssh-exposure" is already a configured redact entry (the $REDACT fixture above); "roadmap-safe"
# matches nothing in it, so it must stay fully visible -- the live positive/negative pair.
qqenv "$ENGINE/qq" new ssh-exposure "xyzzysentinelredact notes that must never auto-surface" >/dev/null 2>&1
qqenv "$ENGINE/qq" new roadmap-safe "xyzzysentinelkeep notes that should surface normally" >/dev/null 2>&1

run_resume() {  # run_resume <transcript_path> <session_id>  -- pipes a UserPromptSubmit payload in
  local tp="$1" sid="$2"
  python3 -c 'import json,sys; print(json.dumps({"prompt": "xyzzysentinelredact xyzzysentinelkeep", "session_id": sys.argv[1], "transcript_path": sys.argv[2]}))' "$sid" "$tp" \
    | qqenv QQ_REDACT_FILE="$REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" bash "$ENGINE/resume-match.sh"
}

out_gated="$(run_resume "$FABLE_TP" "it-fable")"
printf '%s' "$out_gated" | grep -q 'ssh-exposure' \
  && no "resume-match.sh integration: gated model leaked the redact-listed slug end-to-end: $out_gated" \
  || ok "resume-match.sh integration: gated model suppresses the redact-listed slug end-to-end"
printf '%s' "$out_gated" | grep -q 'roadmap-safe' \
  && ok "resume-match.sh integration: gated model still surfaces the non-redacted slug" \
  || no "resume-match.sh integration: gated model dropped the non-redacted slug too (over-redaction): $out_gated"

out_opus="$(run_resume "$OPUS_TP" "it-opus")"
# recall-02 closed WITHOUT changing what this pins: `qq search` now applies the redact list like
# `ask` always did, but the filtering is LIFTABLE per-call (QQ_SEARCH_UNREDACTED), and this hook
# sets that lift because it judges redaction itself, per hit, from the session's model (line ~64).
# So the model-aware behaviour below is unchanged: a gated reader is protected, an ungated one
# keeps full recall. The scope of the redact list is an OPEN design question — see the
# qq-packaging HEAD's 2026-07-29 'redaction scope' entry; if it is ruled the other way, THIS
# assertion is the one that flips, deliberately and visibly.
printf '%s' "$out_opus" | grep -q 'ssh-exposure' \
  && ok "resume-match.sh integration: confirmed-opus model surfaces the redact-listed slug too (full recall)" \
  || no "resume-match.sh integration: opus path unexpectedly suppressed a slug: $out_opus"
printf '%s' "$out_opus" | grep -q 'roadmap-safe' \
  && ok "resume-match.sh integration: confirmed-opus model surfaces the non-redacted slug" \
  || no "resume-match.sh integration: opus path unexpectedly dropped the non-redacted slug: $out_opus"

# ---- integration: the SECOND call site, hooks/inject-contract.sh's SessionStart digest ------------
# Same silent-failure class as above, one file over: the digest is filtered ONLY if
# qq_redact_filter_lines is defined, and `qq digest` does no redaction of its own — so a broken
# source used to mean the whole open-loops digest went out RAW (2026-07-29 verification round, F4).
# Driven here with the digest enabled (QQ_SESSIONSTART_DIGEST=1) against the same throwaway store.
run_contract() {  # run_contract <transcript_path> [plugin_root]
  local tp="$1" rt="${2:-$ENGINE}"
  python3 -c 'import json,sys; print(json.dumps({"session_id": "it-contract", "transcript_path": sys.argv[1]}))' "$tp" \
    | qqenv QQ_REDACT_FILE="$REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" QQ_SESSIONSTART_DIGEST=1 \
            CLAUDE_PLUGIN_ROOT="$rt" bash "$rt/hooks/inject-contract.sh"
}
out_dg_gated="$(run_contract "$FABLE_TP")"
printf '%s' "$out_dg_gated" | grep -q 'xyzzysentinelredact' \
  && no "inject-contract.sh integration: gated model leaked the redact-listed HEAD into the digest" \
  || ok "inject-contract.sh integration: gated model suppresses the redact-listed HEAD in the digest"
printf '%s' "$out_dg_gated" | grep -q 'xyzzysentinelkeep' \
  && ok "inject-contract.sh integration: gated model still carries the non-redacted HEAD's digest line" \
  || no "inject-contract.sh integration: the digest lost the non-redacted HEAD too (over-redaction)"
out_dg_opus="$(run_contract "$OPUS_TP")"
printf '%s' "$out_dg_opus" | grep -q 'xyzzysentinelredact' \
  && ok "inject-contract.sh integration: confirmed-opus model gets the full digest" \
  || no "inject-contract.sh integration: opus path unexpectedly dropped a digest line: $out_dg_opus"

# ...and the failure mode itself: a plugin root where the redaction library is MISSING must
# SUPPRESS the digest, not emit it unfiltered. The contract still goes out — the digest is the
# optional extra, and an unfiltered digest is the one outcome that cannot be allowed.
NOREDACT="$TMP/noredact"; mkdir -p "$NOREDACT/hooks"
for f in "$ENGINE"/*; do
  case "$(basename "$f")" in qq-redact.sh) continue ;; esac
  ln -s "$f" "$NOREDACT/$(basename "$f")"
done
cp "$ENGINE/hooks/inject-contract.sh" "$NOREDACT/hooks/inject-contract.sh"
[ -e "$NOREDACT/qq-redact.sh" ] && no "missing-filter fixture: qq-redact.sh is still reachable (fixture is wrong)"
out_dg_broken="$(run_contract "$FABLE_TP" "$NOREDACT")"
printf '%s' "$out_dg_broken" | grep -q 'xyzzysentinelredact' \
  && no "inject-contract.sh: a MISSING redaction library emitted the digest unfiltered (fail-open)" \
  || ok "inject-contract.sh: a missing redaction library suppresses the digest (fail-closed)"
printf '%s' "$out_dg_broken" | grep -q 'xyzzysentinelkeep' \
  && no "inject-contract.sh: the digest went out at all with no filter available" \
  || ok "inject-contract.sh: the whole digest is dropped, not just the redact-listed lines"
printf '%s' "$out_dg_broken" | grep -q 'operational contract' \
  && ok "inject-contract.sh: the contract itself is still injected when the digest is suppressed" \
  || no "inject-contract.sh: suppressing the digest also lost the contract: $out_dg_broken"

# ---- integration: the THIRD call site, prederive-recall.sh ----------------------------------------
# Also pins the hit-line parse (2026-07-29 verification round, F3): hit_locator returns
# `qq show <slug>` for a HEAD — a locator WITH SPACES — and the old sed wanted a space-free token
# before ` › `, so it never matched and the whole rendered line (score, tag, title and all) became
# the "path": the dedup key hashed the score+title, making "once per artifact per session" once per
# LINE, and the hint printed the line back at itself. The assertions below are on the emitted
# additionalContext, so they fail if either the parse or the redaction gate regresses.
run_prederive() {  # run_prederive <transcript_path> <session_id> <command>
  local tp="$1" sid="$2" cmd="$3"
  python3 -c 'import json,sys; print(json.dumps({"session_id": sys.argv[1], "transcript_path": sys.argv[2], "tool_input": {"command": sys.argv[3]}}))' "$sid" "$tp" "$cmd" \
    | qqenv QQ_REDACT_FILE="$REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" bash "$ENGINE/prederive-recall.sh"
}
# A command inside the hook's ALLOWLIST vocabulary whose words all appear in the HEAD below, so the
# keyword fallback (no embedder in this suite) scores it above the hook's 0.55 threshold.
PD_CMD="systemctl enable xyzzysentinelkeep.service before the machine reboots"
qqenv "$ENGINE/qq" update roadmap-safe "systemctl enable xyzzysentinelkeep.service before the machine reboots again" >/dev/null 2>&1
out_pd="$(run_prederive "$OPUS_TP" "it-pd-1" "$PD_CMD")"
ctx_pd="$(printf '%s' "$out_pd" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null)"
case "$ctx_pd" in
  *'`qq show roadmap-safe`'*) ok "prederive-recall.sh: the hint carries the parsed locator (\`qq show <slug>\`)" ;;
  '')                         no "prederive-recall.sh: no hint emitted for a strongly matching command" ;;
  *)                          no "prederive-recall.sh: hint carries a mangled locator: $ctx_pd" ;;
esac
printf '%s' "$ctx_pd" | grep -qE '\[[0-9.]+\][[:space:]]*\(' \
  && no "prederive-recall.sh: the rendered hit line leaked into the hint (the parse did not fire)" \
  || ok "prederive-recall.sh: the score/tag prefix and title are not in the hint"
# dedup keyed on the locator alone: the SAME artifact must not surface twice in one session, even
# though a different command changes the score and the rendered line.
out_pd2="$(run_prederive "$OPUS_TP" "it-pd-1" "$PD_CMD and also systemctl enable it")"
[ -z "$out_pd2" ] \
  && ok "prederive-recall.sh: a second command matching the same artifact stays silent (dedup)" \
  || no "prederive-recall.sh: dedup missed the same artifact on a differently-scored line: $out_pd2"
[ -n "$(run_prederive "$OPUS_TP" "it-pd-2" "$PD_CMD")" ] \
  && ok "prederive-recall.sh: a NEW session surfaces the artifact again (dedup is per session)" \
  || no "prederive-recall.sh: dedup suppressed the artifact in a fresh session"

# ---- the same failure mode at the other two call sites --------------------------------------------
# resume-match.sh and prederive-recall.sh resolve their engine dir through readlink -f, so a
# symlink in the fixture would escape back to the real directory and FIND the library. Copy the
# scripts themselves (the rest of the fixture stays symlinked), same trick as inject-contract.sh
# above. With the filter unavailable, both must emit NOTHING — for any model — rather than
# surface hits unfiltered.
for f in resume-match.sh prederive-recall.sh; do
  rm -f "$NOREDACT/$f"; cp "$ENGINE/$f" "$NOREDACT/$f"
done
run_resume_broken() {  # run_resume_broken <transcript_path> <session_id>
  local tp="$1" sid="$2"
  python3 -c 'import json,sys; print(json.dumps({"prompt": "xyzzysentinelredact xyzzysentinelkeep", "session_id": sys.argv[1], "transcript_path": sys.argv[2]}))' "$sid" "$tp" \
    | qqenv QQ_REDACT_FILE="$REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" bash "$NOREDACT/resume-match.sh"
}
[ -z "$(run_resume_broken "$FABLE_TP" "it-rm-broken-1")" ] \
  && ok "resume-match.sh: a missing redaction library emits no hints at all (fail-closed, gated model)" \
  || no "resume-match.sh: a MISSING redaction library surfaced hits unfiltered (fail-open)"
[ -z "$(run_resume_broken "$OPUS_TP" "it-rm-broken-2")" ] \
  && ok "resume-match.sh: no filter means no hints even for a full-access model (cannot classify without it)" \
  || no "resume-match.sh: missing library surfaced hits on the full-access path"
run_prederive_broken() {  # run_prederive_broken <transcript_path> <session_id>
  local tp="$1" sid="$2"
  python3 -c 'import json,sys; print(json.dumps({"session_id": sys.argv[1], "transcript_path": sys.argv[2], "tool_input": {"command": sys.argv[3]}}))' "$sid" "$tp" "$PD_CMD" \
    | qqenv QQ_REDACT_FILE="$REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" bash "$NOREDACT/prederive-recall.sh"
}
[ -z "$(run_prederive_broken "$FABLE_TP" "it-pd-broken-1")" ] \
  && ok "prederive-recall.sh: a missing redaction library emits no hint (fail-closed, gated model)" \
  || no "prederive-recall.sh: a MISSING redaction library emitted a hint unfiltered (fail-open)"
[ -z "$(run_prederive_broken "$OPUS_TP" "it-pd-broken-2")" ] \
  && ok "prederive-recall.sh: no filter means no hint even for a full-access model" \
  || no "prederive-recall.sh: missing library emitted a hint on the full-access path"

# ---- show/brief withheld-topics refusal (the read-side gate on explicit verbs) --------------------
# Uses the same throwaway store ($ISTORE) with ssh-exposure (redacted) and roadmap-safe (not redacted).
# The gate lives in the python `qq` dispatcher, so these tests invoke `qq show`/`qq brief` directly.
qqrun() {  # qqrun <transcript_path> [extra_env...] -- qq verb args...
  local tp="$1"; shift
  local envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done; shift
  env HOME="$IT/home" QUINTESSENCE_DIR="$ISTORE" QQ_CONFIG="$IT/config" QQ_STATE_DIR="$IT/state" \
      QQ_MEMDIR="$IT/mem" QQ_KB_ROOT="$IT/kb" QQ_CACHE="$IT/cache.json" \
      QQ_OLLAMA_URL="http://127.0.0.1:1" \
      QQ_REDACT_FILE="$REDACT" QQ_WRITE_TRUSTED_MODEL="$GATED" \
      QQ_MODEL_TRANSCRIPT="$tp" \
      "${envs[@]}" \
      python3 "$ENGINE/qq" "$@"
}

# --- limited reader (fable transcript) is REFUSED with zero content ---
out_show_gated="$(qqrun "$FABLE_TP" -- show ssh-exposure 2>/dev/null)"
rc_show_gated=$?
[ -z "$out_show_gated" ] && ok "show gate: limited reader gets ZERO content on withheld topic (stdout empty)" \
  || no "show gate: limited reader got content on withheld topic: '$out_show_gated'"
[ "$rc_show_gated" -ne 0 ] && ok "show gate: limited reader gets nonzero exit on withheld topic" \
  || no "show gate: limited reader got exit 0 on withheld topic"
# verify the refusal MESSAGE (on stderr) names the topic and the override
err_show_gated="$(qqrun "$FABLE_TP" -- show ssh-exposure 2>&1 >/dev/null)"
printf '%s' "$err_show_gated" | grep -q 'ssh-exposure' \
  && ok "show gate: refusal names the withheld topic" \
  || no "show gate: refusal does not name the topic: '$err_show_gated'"
printf '%s' "$err_show_gated" | grep -q '\-\-unredacted' \
  && ok "show gate: refusal names the --unredacted flag" \
  || no "show gate: refusal does not name the flag: '$err_show_gated'"
printf '%s' "$err_show_gated" | grep -q 'QQ_SHOW_UNREDACTED' \
  && ok "show gate: refusal names the QQ_SHOW_UNREDACTED setting" \
  || no "show gate: refusal does not name the setting: '$err_show_gated'"

# same for brief
out_brief_gated="$(qqrun "$FABLE_TP" -- brief ssh-exposure 2>/dev/null)"
[ -z "$out_brief_gated" ] && ok "brief gate: limited reader gets ZERO content on withheld topic" \
  || no "brief gate: limited reader got content: '$out_brief_gated'"

# non-withheld topic on the same limited reader renders normally
out_show_safe="$(qqrun "$FABLE_TP" -- show roadmap-safe 2>/dev/null)"
rc_show_safe=$?
printf '%s' "$out_show_safe" | grep -q 'roadmap-safe' \
  && ok "show gate: limited reader gets a non-withheld topic normally" \
  || no "show gate: limited reader's non-withheld topic is missing: '$out_show_safe'"
[ "$rc_show_safe" -eq 0 ] && ok "show gate: non-withheld topic exits 0" \
  || no "show gate: non-withheld topic got exit $rc_show_safe"

# PROVE zero content means EXACTLY zero bytes (wc -c), not just "short output"
byte_count="$(qqrun "$FABLE_TP" -- show ssh-exposure 2>/dev/null | wc -c)"
[ "$byte_count" -eq 0 ] && ok "show gate: refused output is exactly 0 bytes" \
  || no "show gate: refused output was $byte_count bytes, not 0"
byte_count_brief="$(qqrun "$FABLE_TP" -- brief ssh-exposure 2>/dev/null | wc -c)"
[ "$byte_count_brief" -eq 0 ] && ok "brief gate: refused output is exactly 0 bytes" \
  || no "brief gate: refused output was $byte_count_brief bytes, not 0"

# --- limited reader WITH --unredacted flag gets the content ---
out_show_lifted="$(qqrun "$FABLE_TP" -- show ssh-exposure --unredacted 2>/dev/null)"
printf '%s' "$out_show_lifted" | grep -q 'ssh-exposure' \
  && ok "show gate: --unredacted flag lifts the refusal" \
  || no "show gate: --unredacted flag did not lift: '$out_show_lifted'"
out_brief_lifted="$(qqrun "$FABLE_TP" -- brief ssh-exposure --unredacted 2>/dev/null)"
printf '%s' "$out_brief_lifted" | grep -q 'ssh-exposure' \
  && ok "brief gate: --unredacted flag lifts the refusal" \
  || no "brief gate: --unredacted flag did not lift: '$out_brief_lifted'"

# --- limited reader WITH QQ_SHOW_UNREDACTED=1 gets the content ---
out_show_env="$(qqrun "$FABLE_TP" QQ_SHOW_UNREDACTED=1 -- show ssh-exposure 2>/dev/null)"
printf '%s' "$out_show_env" | grep -q 'ssh-exposure' \
  && ok "show gate: QQ_SHOW_UNREDACTED=1 lifts the refusal" \
  || no "show gate: QQ_SHOW_UNREDACTED=1 did not lift: '$out_show_env'"

# --- full-access reader (opus transcript) is UNAFFECTED ---
out_show_opus="$(qqrun "$OPUS_TP" -- show ssh-exposure 2>/dev/null)"
rc_show_opus=$?
printf '%s' "$out_show_opus" | grep -q 'ssh-exposure' \
  && ok "show gate: full-access reader gets the withheld topic" \
  || no "show gate: full-access reader was refused: '$out_show_opus'"
[ "$rc_show_opus" -eq 0 ] && ok "show gate: full-access reader exits 0" \
  || no "show gate: full-access reader got exit $rc_show_opus"
out_brief_opus="$(qqrun "$OPUS_TP" -- brief ssh-exposure 2>/dev/null)"
printf '%s' "$out_brief_opus" | grep -q 'ssh-exposure' \
  && ok "brief gate: full-access reader gets the withheld topic" \
  || no "brief gate: full-access reader was refused: '$out_brief_opus'"

# --- unconfigured install (no redact file) is UNAFFECTED ---
out_show_noredact="$(qqrun "$FABLE_TP" QQ_REDACT_FILE="$TMP/nope" -- show ssh-exposure 2>/dev/null)"
rc_show_noredact=$?
printf '%s' "$out_show_noredact" | grep -q 'ssh-exposure' \
  && ok "show gate: unconfigured install (no redact file) renders normally for limited reader" \
  || no "show gate: unconfigured install refused: '$out_show_noredact'"
[ "$rc_show_noredact" -eq 0 ] && ok "show gate: unconfigured install exits 0" \
  || no "show gate: unconfigured install got exit $rc_show_noredact"

# --- prefix glob matching: sec-* entry matches sec-incident-42 ---
qqenv "$ENGINE/qq" new sec-incident-42 "a topic matching the sec-* glob" >/dev/null 2>&1
out_show_glob="$(qqrun "$FABLE_TP" -- show sec-incident-42 2>/dev/null)"
[ -z "$out_show_glob" ] && ok "show gate: prefix glob entry (sec-*) refuses matching topic" \
  || no "show gate: prefix glob entry did not refuse: '$out_show_glob'"

# --- nonexistent topic ON the withheld list must be indistinguishable from an existing one ---
# The gate consults the list only, never the store — so a withheld topic that does not exist
# must produce the same refusal as one that does, preventing the refusal from being used to
# discover whether a topic exists.  "ssh-exposure" exists; "ssh-exposure-ghost" does not, but
# matches the list (it would need an exact entry; add one).
printf 'ssh-exposure-ghost\n' >> "$REDACT"
out_ghost="$(qqrun "$FABLE_TP" -- show ssh-exposure-ghost 2>/dev/null)"
rc_ghost=$?
[ -z "$out_ghost" ] && ok "show gate: nonexistent withheld topic produces zero bytes on stdout" \
  || no "show gate: nonexistent withheld topic leaked content: '$out_ghost'"
[ "$rc_ghost" -ne 0 ] && ok "show gate: nonexistent withheld topic gets nonzero exit" \
  || no "show gate: nonexistent withheld topic got exit 0"
# Compare the two refusals (existing vs nonexistent) after normalising out the topic name —
# the structure, wording, and every other detail must be identical so the caller cannot tell
# whether the topic is real.
err_existing="$(qqrun "$FABLE_TP" -- show ssh-exposure 2>&1 >/dev/null | sed 's/ssh-exposure/TOPIC/g')"
err_ghost="$(qqrun "$FABLE_TP" -- show ssh-exposure-ghost 2>&1 >/dev/null | sed 's/ssh-exposure-ghost/TOPIC/g')"
[ "$err_existing" = "$err_ghost" ] \
  && ok "show gate: refusal for nonexistent withheld topic is indistinguishable from the existing-topic refusal" \
  || no "show gate: refusals differ — existing: '$err_existing' vs nonexistent: '$err_ghost'"

# ---- summary --------------------------------------------------------------------------------------
echo "----- $pass passed, $fail failed -----"
[ "$fail" -eq 0 ]
