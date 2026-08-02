# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.refsview — B2 read-side revalidation ("revalidate on read").

`brief`/`show` (and `ask`'s evidence headers) re-check the refs of the lines they RENDER and
annotate a line whose referent has moved:

    ⚠ referent changed since written: <id> (<date>)

upgrading the standing snapshot banner from "may be stale" to "IS stale, artifact X moved".
The join is (head, line_ts): a rendered '> updated: <stamp> ...' line joins to the REF records
whose line_ts equals its stamp — the exact key B1 amendment (i) threads through the write path.

INVARIANTS (enforced by construction + tests):
  - READ-ONLY. This module NEVER writes: no status transitions, no refs.jsonl mutation, no
    state-dir touch of any kind — it opens refs.jsonl for reading and stats/hashes referents.
    Status changes come ONLY from the B3 triggers (quintessence.refsresolve) and the audit
    sweep. Enforced by construction: there is no `open(..., "w")`/os.replace/state_lock here.
  - FAIL-SOFT + byte-parity: a missing/corrupt refs.jsonl, an unreadable referent, or any
    exception degrades to exactly today's rendering (annotate_lines returns its input). A HEAD
    with no refs renders byte-identical to a build without this module. QQ_BIND=0 disables the
    read side too (one escape hatch for the whole binding feature).
  - CHEAP: the read-path budget is "milliseconds of hashing". Only `file` refs are
    re-fingerprinted on read (local hashing, memoized per view); `git`/`unit`/`probe`/`port`
    refs annotate only when a trigger/sweep has already marked them `suspect` — re-
    fingerprinting those needs subprocesses (git/systemctl/arbitrary shell/ss) that neither
    fit the budget nor (probe!) belong on a read path at all. Every NEW kind defaults into
    this suspect-only rule by construction (`_changed` re-hashes kind == "file" and nothing
    else).

What counts as "changed" per record status:
  ok / waived          file: current fp != recorded fp (waived compares against the fp the
                       adjudication refreshed). Others: never on read (see above).
  missing-at-write     the referent has APPEARED since the born-stale write (fp None -> real):
                       that is a real change to the claim's ground truth, annotated. Still
                       missing = unchanged (the loud warn already fired at write time).
  suspect              always annotated (a trigger already established the change; read stays
                       pure annotation, no re-adjudication).
  fp-error             skipped (no comparison basis; the sweep owns it).
  retired              never (TERMINAL): adjudication's off-switch for a referent that
                       is real but useless to track (hot dirs defeat suspect-once). Skipped
                       by construction — `_changed` passes only the four statuses above —
                       and pinned by test.
"""
from __future__ import annotations

import json
import os

from .heads import UpdateItem
from .refs import _fp_file

ANNOTATION_PREFIX = "⚠ referent changed since written: "


def _annotation(rec: dict) -> str:
    date = (rec.get("line_ts") or rec.get("asof") or "")[:10]
    return f"{ANNOTATION_PREFIX}{rec.get('id')} ({date})"


class RefsView:
    """One read's view of $QQ_STATE_DIR/refs/refs.jsonl: loaded once, joined to rendered lines
    on (head, line_ts), file fingerprints memoized across lines/topics. Construct per render
    call (brief/show/ask evidence build) — the file is small and a fresh view is what makes a
    just-fired trigger visible on the very next read."""

    def __init__(self, config):
        # {head: {line_ts: {(kind,id): record}}} — the LAST record for a (head,line_ts,kind,id)
        # wins (append order == chronological; a re-bound/re-marked record supersedes).
        self._by_head: dict = {}
        self._fp_cache: dict = {}
        try:
            if not config.get_bool("QQ_BIND"):
                return
            path = os.path.join(config.get_path("QQ_STATE_DIR"), "refs", "refs.jsonl")
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue   # a torn/foreign line never breaks the view
                    if not isinstance(rec, dict):
                        continue
                    head, lts = rec.get("head"), rec.get("line_ts")
                    if not head or not lts:
                        continue
                    self._by_head.setdefault(head, {}).setdefault(lts, {})[
                        (rec.get("kind"), rec.get("id"))] = rec
        except Exception:
            self._by_head = {}

    def has_refs(self, head: str) -> bool:
        return bool(self._by_head.get(head))

    # ---- change detection --------------------------------------------------------------
    def _current_file_fp(self, ident: str):
        if ident not in self._fp_cache:
            try:
                self._fp_cache[ident] = _fp_file(ident)
            except Exception:
                self._fp_cache[ident] = (None, "fp-error")
        return self._fp_cache[ident]

    def _changed(self, rec: dict) -> bool:
        status = rec.get("status")
        if status == "suspect":
            return True
        if rec.get("kind") != "file" or status not in ("ok", "waived", "missing-at-write"):
            return False
        fp, cur_status = self._current_file_fp(rec.get("id") or "")
        if cur_status == "fp-error":
            return False   # unreadable now != known-changed; stay quiet on read
        return fp != rec.get("fp")

    def line_annotations(self, head: str, line_ts: str) -> list:
        recs = self._by_head.get(head, {}).get(line_ts)
        if not recs:
            return []
        return [_annotation(rec) for rec in recs.values() if self._changed(rec)]

    # ---- render-surface entry points (each fail-soft to its input) -----------------------
    def annotate_lines(self, head: str, lines: list) -> list:
        """`lines` with an annotation line inserted directly after every rendered
        '> updated: <stamp> ...' line whose joined refs changed. Input returned unchanged
        (same list content) when the head has no refs or anything goes wrong."""
        try:
            if not self.has_refs(head):
                return list(lines)
            out = []
            for ln in lines:
                out.append(ln)
                if isinstance(ln, str) and ln.startswith("> updated:"):
                    ts = UpdateItem(marker=ln).timestamp
                    if ts:
                        out.extend(self.line_annotations(head, ts))
            return out
        except Exception:
            return list(lines)

    def annotate_text(self, head: str, text: str) -> str:
        """annotate_lines over a whole HEAD text (qq show), trailing newline preserved."""
        try:
            if not self.has_refs(head) or not text:
                return text
            trailing = text.endswith("\n")
            lines = text.split("\n")
            if trailing:
                lines = lines[:-1]
            out = self.annotate_lines(head, lines)
            if out == lines:
                return text   # byte-identical fast path (no join/rebuild drift possible)
            return "\n".join(out) + ("\n" if trailing else "")
        except Exception:
            return text

    def chunk_annotations(self, head: str, text: str) -> list:
        """Deduped annotations for every update-line stamp appearing in `text` — the `ask`
        evidence-header surface: the chunk's own lines are what the model is about to cite,
        so their changed referents ride the block header."""
        try:
            if not self.has_refs(head) or not text:
                return []
            out: list = []
            for ln in text.split("\n"):
                if ln.startswith("> updated:"):
                    ts = UpdateItem(marker=ln).timestamp
                    if ts:
                        for ann in self.line_annotations(head, ts):
                            if ann not in out:
                                out.append(ann)
            return out
        except Exception:
            return []


def search_hit_annotations(config, hits: list) -> dict:
    """Per-hit annotations for a list of search results.

    Returns {index: [annotation_str, ...]} for every hit that carries at least one
    annotation. Hits with no annotations are absent from the dict. One RefsView
    is constructed per call (not per hit) — same amortisation as the ask path."""
    try:
        view = RefsView(config)
    except Exception:
        return {}
    result: dict = {}
    for i, h in enumerate(hits):
        slug = os.path.basename(h.get("path", ""))
        if slug.endswith(".md"):
            slug = slug[:-3]
        anns = view.chunk_annotations(slug, h.get("text", ""))
        if anns:
            result[i] = anns
    return result
