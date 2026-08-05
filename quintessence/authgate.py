# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.authgate — the AUTHORING GATE: write-side model trust (two-axis policy,
2026-07-03 design sketch, greenlit 2026-07-04).

The two axes are different mechanisms protecting different things:
  READ-trust  protects the MODEL  — qq-redact.sh filters involuntary injection (auto-recall,
              SessionStart digest) away from limited reader sessions. Out of scope here.
  WRITE-trust protects the ESTATE — this module. A write verb (update/new/rewrite/essence)
              targeting a HEAD whose slug is security-tagged (QQ_AUTHOR_GATE_SLUGS), issued
              from a session whose model is NOT trusted to author such topics alone, does not
              land in the HEAD: the verbatim proposed text is queued as a [PROPOSED write]
              entry in the pending-findings PROPOSED-WRITES section instead, pending
              ratification by a trusted session — "may draft, may not author alone".

TRUST SEMANTICS (reuses qq-redact.sh's qq_model_mode detection + thresholds, ported):
  the session transcript's last `message.model` is the only reliable model signal (the banner
  is client-side); at any given moment it shows the model that answered the PREVIOUS turn, so
  detection is "last-known model" — and we FAIL SAFE: empty/unknown model -> NOT trusted
  (consistent with dist c55af14's never-report-a-known-mode-on-empty direction).
  Trusted-to-author = model_mode 'opus' (QQ_SAFE_MODEL_PREFIX match) OR 'fable' (exact
  QQ_WRITE_TRUSTED_MODEL match). NOTE this deliberately differs from the READ axis, where 'fable'
  is the redacted-from tier: that reader receives filtered recall because auto-injected content
  can cause a silent downgrade — but it is not untrusted as an AUTHOR (the design sketch names
  it a ratifier: pending ratification by Opus or the limited reader model). The untrusted set is everything else:
  smaller/local/unknown models, and any session whose transcript can't be found.

TRANSCRIPT RESOLUTION: QQ_MODEL_TRANSCRIPT if set (test/integration seam); else derive from
$CLAUDE_CODE_SESSION_ID (present in the environment of every command a Claude Code session
runs) as ~/.claude/projects/*/<session-id>.jsonl. No transcript resolvable -> model unknown
-> not trusted. This is NOT an adversarial control: like the read-side redaction it is a
structural guard against a well-meaning non-trusted model authoring security topics, not a
sandbox — a caller who can run `qq` can also edit the queue file. Timing: the update verb's pre-lock existence check (_gate_target_exists) is
re-evaluated under the engine write-lock (quintessence.write._execute_write's gate_check
callback), so a concurrent create of the same protected topic between the pre-lock check
and the lock cannot let an untrusted update land — the under-lock re-check diverts it to
the queue.

QUEUE SEMANTICS: PROPOSED-WRITES is an APPEND-only section (SALIENCE-like, NOT auto-resolve —
no producer re-emits it; an entry is cleared only by adjudication), written under the A1
state_lock with the same atomic tmp+rename discipline as checks.persist_findings /
procprobe.write_findings. One line per proposal (Finding cls "PROPOSED write",
quintessence.findings): head slug, timestamp, verb, model identity, and the verbatim proposed
text JSON-encoded inline, so multi-line rewrites round-trip exactly and no content line can
ever collide with the findings-file markers.

RATIFICATION (minimum viable, deliberately no new CLI surface): a trusted session sees the
entry via `qq findings`, JSON-decodes the text, replays it through the SAME verb
(`qq update <head> "<text>"` / `printf '%s' "<text>" | qq rewrite <head>` ...), then deletes
the line from $QQ_STATE_DIR/pending-findings.md (state dir, not the store — direct edit is
sanctioned there, same as clearing a SALIENCE item). Rejection = delete without replaying.

GOLDEN-PARITY DISCIPLINE (mirrors QQ_BIND's rollout): default is gate ON with an EMPTY slug
list, so a fresh install is byte-identical to pre-gate behavior; QQ_AUTHOR_GATE=0 disables the
gate entirely as the escape hatch. gate_reason() is pure reads and short-circuits BEFORE any
transcript I/O for a non-gated slug, so the non-gated/trusted paths do no new work that could
alter observable behavior (tests/test-authgate.sh pins the byte-parity)."""
from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

from .atomicio import atomic_write_text
from .findings import Finding, FindingsFile
from .store import Store, state_lock

SECTION = "PROPOSED-WRITES"

# The same pattern qq_model_mode greps: the LAST `"model": "<id>"` occurrence in the
# transcript, scanning bottom-up (first match within the last line that has one).
_MODEL_RE = re.compile(r'"model"\s*:\s*"([^"]+)"')

# Where Claude Code keeps session transcripts; <munged-project-dir>/<session-id>.jsonl.
_PROJECTS_GLOB = os.path.join("~", ".claude", "projects", "*", "{sid}.jsonl")


def transcript_path(config) -> Optional[str]:
    """The transcript to probe for model identity: QQ_MODEL_TRANSCRIPT when set, else the
    $CLAUDE_CODE_SESSION_ID-derived path under ~/.claude/projects (newest mtime wins if the
    same id somehow matches under several project dirs). None = no signal (-> unknown)."""
    override = config.get_path("QQ_MODEL_TRANSCRIPT")
    if override:
        return override
    sid = config.resolve_raw("CLAUDE_CODE_SESSION_ID")
    if not sid or "/" in sid or sid in (".", ".."):
        return None
    hits = glob.glob(os.path.expanduser(_PROJECTS_GLOB.format(sid=sid)))
    if not hits:
        return None
    try:
        return max(hits, key=os.path.getmtime)
    except OSError:
        return hits[0]


def model_identity(config) -> str:
    """Last-known model id from the session transcript, '' when there is no readable signal.
    Port of qq_model_mode's read (`tac | grep -m1 -oE '"model": "..."'`): bottom-up, first
    matching line, first match within it. Never raises — any failure is '' (fail safe)."""
    try:
        tp = transcript_path(config)
        if not tp or not os.path.isfile(tp):
            return ""
        with open(tp, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        for ln in reversed(lines):
            m = _MODEL_RE.search(ln)
            if m:
                return m.group(1)
        return ""
    except Exception:
        return ""


def model_mode(config, model: Optional[str] = None) -> str:
    """'fable' | 'opus' | 'unknown' — exact port of qq-redact.sh's qq_model_mode CLASSIFY step
    (the read half lives in model_identity): empty model -> unknown (never a spurious 'fable'
    on a gate-off install, ade82f8); exact QQ_WRITE_TRUSTED_MODEL match -> 'fable';
    QQ_SAFE_MODEL_PREFIX prefix match -> 'opus'; anything else -> unknown. An explicitly-empty
    safe prefix collapses to the registry default first (bash's `${VAR:-claude-opus-}` does the
    same), with the c55af14 belt-and-braces guard kept behind it: empty must never categorize as
    'opus'."""
    if model is None:
        model = model_identity(config)
    gated = config.get_str("QQ_WRITE_TRUSTED_MODEL")
    safe = config.get_str("QQ_SAFE_MODEL_PREFIX") or "claude-opus-"
    if not model:
        return "unknown"
    if gated and model == gated:
        return "fable"
    if not safe:                      # unreachable via the `or` above — pinned impossible
        return "unknown"
    if model.startswith(safe):
        return "opus"
    return "unknown"


def _slug_base(topic: str) -> str:
    """The bare slug a gate entry matches against: basename, '.md' stripped — the same
    normalization qq_slug_redacted applies to its argument."""
    base = topic.rsplit("/", 1)[-1]
    return base[:-3] if base.endswith(".md") else base


def slug_gated(topic: str, entries: list) -> bool:
    """Does `topic` match a QQ_AUTHOR_GATE_SLUGS entry? The redact-slugs pattern, generalized:
    trailing '*' = prefix match, else exact slug match. Deliberately NOT the raw-substring
    fallback qq_slug_redacted also has (that exists for matching whole PATHS on read surfaces;
    a write verb's target is always a slug, and substring matching would over-gate)."""
    base = _slug_base(topic)
    for e in entries:
        if not e:
            continue
        if e.endswith("*"):
            if base.startswith(e[:-1]):
                return True
        elif base == e:
            return True
    return False


def gate_reason(config, topic: str) -> Optional[str]:
    """None = the write proceeds normally. A string = the gate diverts this write; the value
    is the untrusted model's identity ('unknown' when empty) for the queue record/notice.
    Pure reads, ordered so a non-gated call does no transcript I/O: enabled? -> slug list
    non-empty? -> slug match? -> only THEN model detection."""
    if not config.get_bool("QQ_AUTHOR_GATE"):
        return None
    entries = config.get_list("QQ_AUTHOR_GATE_SLUGS")
    if not entries or not slug_gated(topic, entries):
        return None
    model = model_identity(config)
    if model_mode(config, model) in ("opus", "fable"):
        return None
    return model or "unknown"


def queue_proposal(store: Store, verb: str, topic: str, content: str, model: str) -> str:
    """Append one [PROPOSED write] entry to the PROPOSED-WRITES section of pending-findings
    (append-only: read current section + add, never full-replace — no producer auto-resolves
    this section) under the state lock, atomic tmp+rename. Returns the one-line stderr notice
    the diverted verb reports. Raises on a queue-write failure — the caller must NOT let the
    proposed content vanish silently (quintessence.write turns that into a loud WriteError)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    head = _slug_base(topic)
    line = Finding(cls="PROPOSED write",
                   fields={"ts": ts, "head": head, "verb": verb, "model": model,
                            "text": content}).render()
    path = os.path.join(store.config.get_path("QQ_STATE_DIR"), "pending-findings.md")
    with state_lock(store):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                ff = FindingsFile.parse(fh.read())
        except OSError:
            ff = FindingsFile()
        ff.set_section(SECTION, ff.get_section(SECTION) + [line])
        atomic_write_text(path, ff.serialize())
    return (f"qq {verb}: AUTHORING GATE – '{head}' is a security-tagged topic and this "
            f"session's model ({model}) may draft but not author it alone; the proposed text "
            f"is queued as a PROPOSED write in {path} for ratification by a trusted session "
            f"(nothing was written to the HEAD)")
