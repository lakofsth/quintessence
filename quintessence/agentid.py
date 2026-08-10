# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.agentid — the identity of the agent session making a write, derived not typed.

An update-line has always carried a marker naming the model that wrote it (`[Opus 5, ...]`), and
it has always been prose: the writing session types it, nothing derives it, nothing checks it.
That is the shape this estate has ledgered twice already (D43, D90) — a hand-written value drifts,
and the field a model is least reliable about is its own identifier. So the marker a reader sees
is derived here instead, from the harness environment and the session's own transcript, which
makes it a record rather than a claim.

Two properties, because this sits on the write path of every `qq update`:

  - **Fail-soft.** Missing environment, unreadable transcript, unrecognised value — all yield
    None, and the line is composed exactly as it was before. Attribution must never be able to
    fail a write.
  - **No free text reaches a HEAD.** Both halves are validated against a strict charset first.
    The session id comes from the environment, which is not qq's to trust: a value containing a
    newline would not corrupt one marker, it would forge an update-line — the same fabrication
    class `write._strip_caller_stamp` exists to close.

The harness coupling — `CLAUDE_CODE_SESSION_ID` and Claude Code's transcript layout — is
deliberate and narrow, and it is the only harness this knows. Off it (a human at a terminal,
cron, another agent runner) nothing is emitted. That is the correct answer, not a degraded one:
a write with no agent behind it should carry no agent's name.

The estate's `agent-stamp(1)` derives the same identity in bash for git's `prepare-commit-msg`
hook. The two are independent implementations of one convention — this one marks the update-line
a reader sees, that one marks the commit that carries it — so a change to the convention needs
both. Neither can be derived from the other: the hook must run with no python import path, and
this must run with no binary on PATH.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

# Session id: the harness writes a uuid, but this is an environment value, so it is bounded and
# charset-checked rather than trusted. Anything else is treated as "no agent session".
# `\Z`, not `$`: `$` also matches before a trailing newline, so `$`-anchored validation admits
# `claude-opus-5\n` — which does not corrupt a marker, it SPLITS THE UPDATE-LINE and leaves the
# remainder as a continuation line. Found in review; the bash `case` guard rejected it all along,
# so this was also a divergence between the two. On `_SID_RE` the same swap is defence in depth
# rather than a fix: `marker()` strips the environment value before validating, so the trailing
# newline never reaches here. An earlier commit message claimed both were live fixes; only
# `_MODEL_RE` was.
_SID_RE = re.compile(r"\A[0-9A-Za-z][0-9A-Za-z._-]{0,63}\Z")
# Model id as it appears in a transcript. Brackets are legitimate (`claude-opus-5[1m]`).
_MODEL_RE = re.compile(r"\A[0-9A-Za-z][0-9A-Za-z._:\[\]-]{0,63}\Z")
# How long two transcripts' mtimes may differ and still count as "written at the same moment",
# i.e. too close to tell apart. See _writer_transcripts.
_AMBIGUITY_WINDOW_S = 5.0

# Transcripts reach several MB and this runs on every write, so the model is read from a bounded
# tail rather than the whole file — same bound the bash helper uses, for the same reason. Measured
# at 1.1 ms per call against a real 680 KB transcript with 37 project directories to scan, which
# is noise beside the git commit the write is already paying for.
_TAIL_BYTES = 1_000_000
# ...and only this many entries back from the end are examined. An assistant entry is normally the
# last or second-to-last; a bound keeps a pathological tail from turning a write into a parse job.
_MAX_ENTRIES = 200

UNKNOWN_MODEL = "model-unknown"


def _transcript_paths(sid: str, home: str) -> list[str]:
    """Every transcript that `sid`'s work could have been written to: the session's own, plus one
    per subagent it has spawned.

    A subagent does not get its own session id — it inherits `CLAUDE_CODE_SESSION_ID` from the
    session that spawned it — but its turns go to `<sid>/subagents/agent-*.jsonl`, never to the
    parent transcript. So reading only the parent names the PARENT's model for a write a subagent
    made, and those differ constantly: measured on this estate, 238 of 379 subagent transcripts
    ran a different model than their parent. That is the plausible-wrong-name failure this module
    exists to avoid, arriving through the back door.

    There is no environment variable that tells a process which of the two it is. Both
    `CLAUDE_CODE_CHILD_SESSION=1` and `CLAUDE_PID` are byte-identical in a parent and its
    subagent (checked, 2026-08-09), so the caller cannot be identified by asking; it has to be
    inferred from which transcript was written last. See _writer_transcripts.

    The glob over project directories is because a project dir is derived from a working
    directory this process does not necessarily share with the session."""
    root = os.path.join(home, ".claude", "projects")
    try:
        projects = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for proj in projects:
        p = os.path.join(root, proj, f"{sid}.jsonl")
        if os.path.isfile(p):
            out.append(p)
        subs = os.path.join(root, proj, sid, "subagents")
        try:
            for name in sorted(os.listdir(subs)):
                if name.endswith(".jsonl"):
                    q = os.path.join(subs, name)
                    if os.path.isfile(q):
                        out.append(q)
        except OSError:
            pass
    return out


def _writer_transcripts(paths: list[str]) -> list[str]:
    """`paths` narrowed to the one that most likely belongs to whoever is writing, or [] when
    that cannot be told.

    Newest mtime wins: the process calling `qq` has just produced the turn that called it, so its
    own transcript is the one that was appended to a moment ago.

    The exception is concurrent subagents, which append at the same time and are genuinely
    indistinguishable from here. When two candidates were written within `_AMBIGUITY_WINDOW_S` of
    each other, this returns [] and the caller degrades to `model-unknown` — the session id is
    still recorded, only the model is withheld. Guessing between siblings would produce exactly
    the confident wrong name the whole module is built to refuse.

    What that costs, measured on this estate rather than estimated, because the withholding is not
    paid only by subagents: 42.4% of subagent writes withhold, and so do 8.9% of ordinary
    MAIN-SESSION writes in sessions that use subagents — roughly one `qq update` in eleven — since
    a parent turn within 5s of a subagent append is indistinguishable too. Weighed against a 63%
    wrong-name rate for subagent writes, that is the right trade, but the parent pays part of it.

    The rule is a heuristic and here is its edge: it is correct only while `qq` runs close in time
    to its caller's own last turn. A subagent that shells `pytest && qq update ...`, with the suite
    taking longer than the window and the parent still working, is stamped with the PARENT's model
    — confidently and wrongly. Replayed against every subagent turn on this estate (38,275
    candidate writes across 74 sessions) the rule named a wrong transcript 0 times, so this is a
    real but unobserved edge; it is written down because nothing in the suite can mark it."""
    stamped = []
    for p in paths:
        try:
            stamped.append((os.stat(p).st_mtime, p))
        except OSError:
            continue
    if not stamped:
        return []
    stamped.sort(reverse=True)
    if len(stamped) > 1 and (stamped[0][0] - stamped[1][0]) < _AMBIGUITY_WINDOW_S:
        return []
    return [stamped[0][1]]


def _model_of_entry(line: str, validate: bool = True) -> Optional[str]:
    """`message.model` of one transcript line, if that line is an assistant entry. Any other
    line — a user turn, a tool result, a truncated fragment from the head of the tail read — is
    None.

    `validate` splits two callers whose right answer differs, and the split is deliberate:

      - LABELLING (this module) validates. A model id that fails `_MODEL_RE` — `<synthetic>`,
        which Claude Code writes for API-error, interrupt and compaction entries — is skipped so
        the walk continues to the real model behind it. Best effort at a true name.
      - TRUST (quintessence.authgate) does NOT. There the newest entry's answer is final,
        whatever it says: skipping backwards past an unrecognised entry to find a recognised one
        can only ever move a session TOWARDS trusted, and unknown must mean untrusted. Fail-safe
        beats informative.

    Measured 2026-08-09: 22 of 1112 live transcripts end on a `<synthetic>` entry, so this is a
    2% path, not a theoretical one."""
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    model = msg.get("model")
    if not isinstance(model, str):
        return None
    if validate and not _MODEL_RE.match(model):
        return None
    return model


def assistant_entry_model(line: str) -> "tuple[bool, Optional[str]]":
    """`(is_an_assistant_entry, its message.model or None)` — the reader a TRUST decision needs.

    Trust cannot use `_model_of_entry`, and an earlier attempt to make it do so was a fail-OPEN.
    That function returns None both for "this is not an assistant entry" and for "this entry's
    model is unusable", and `authgate` treats None as "keep looking" — so rejecting a malformed
    newest entry made the scan walk BACKWARDS to an older, well-formed, possibly more-trusted one.
    A tab or CR in the newest model let a session resumed onto a local model read as `opus` and
    author a gated HEAD directly. Separating the two answers is the whole point of this function:
    the caller can stop at the newest assistant entry whatever it says, which is the only
    fail-safe reading."""
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):
        return False, None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return False, None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return True, None
    model = msg.get("model")
    return True, model if isinstance(model, str) else None


def model_of(sid: str, home: Optional[str] = None) -> Optional[str]:
    """The model named by the NEWEST assistant entry in `sid`'s transcript, or None.

    Newest rather than oldest: a session can be resumed onto a different model, and the write
    being stamped is happening now.

    Each candidate line is parsed and the value taken from `message.model` of an entry whose
    `type` is `assistant` — deliberately not a regex for `"model":"..."` over the raw text, which
    is what the bash helper does and what this was first written to do.

    That scan is not currently WRONG, and the difference is worth stating precisely rather than as
    a defect it is not. Measured over 400 live transcripts: an unescaped `"model":"` occurs on
    assistant entries and nowhere else (3582 hits, zero elsewhere), because a tool result quoting
    that text arrives as an escaped JSON string, where `\\"model\\":` does not match the pattern.
    So today the last raw match and the newest assistant entry are the same thing.

    What the scan rests on is that no STRUCTURED tool result ever carries a `model` key. Structured
    tool results are already stored unescaped on `user` entries (1306 of them in the same sample),
    so that is a property of what tools happen to return, guaranteed by nothing. One tool returning
    an object with that key and the scan attributes the write to it — silently, with a name too
    plausible to catch. Parsing the entry reads the field it means and does not rest on it."""
    home = home if home is not None else os.path.expanduser("~")
    for path in _writer_transcripts(_transcript_paths(sid, home)):
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                start = max(0, fh.tell() - _TAIL_BYTES)
                fh.seek(start)
                blob = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        lines = blob.splitlines()
        if start > 0 and lines:
            lines = lines[1:]        # the tail read almost certainly split this one mid-entry
        for line in reversed(lines[-_MAX_ENTRIES:]):
            model = _model_of_entry(line)
            if model:
                return model
    return None


def marker(home: Optional[str] = None) -> Optional[str]:
    """`[<model>, session <first 8 of the id>]` for the agent session making this write, or None
    when no agent session is making it.

    Eight characters of the session id, because the marker is read in a line of prose and eight
    is what the estate's other surfaces already quote — it identifies the transcript. Where a
    `prepare-commit-msg` hook is installed to stamp the commit as well, that trailer carries the
    id in full; no such hook ships with this package, so do not promise a reader one."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not sid or not _SID_RE.match(sid):
        return None
    return f"[{model_of(sid, home) or UNKNOWN_MODEL}, session {sid[:8]}]"
