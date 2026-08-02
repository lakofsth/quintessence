# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.findings — L1: the Finding record type + renderer.

Today (findings.sh) the pending-findings queue is marker-delimited PROSE: a producer (run_check,
staleness-xref.py, config-reconcile.py) prints already-formatted "- [T1 ...] ..." lines, and a
consumer (qq's findings_next) recovers structured fields by REGEX-PARSING that rendered prose
back out — the "line shape:" comments in `qq` are the tell. Any wording change silently breaks
adjudication context. Spec D5: findings become a typed record with a renderer; producers emit
records, `findings_render`/`findings_next` read fields. This module is that record + renderer,
matching TODAY's exact line formats (the P0 surface-freeze goldens are the oracle — see
tests/py/test_findings.py, which asserts byte-identical output against
tests/fixtures/surface-freeze/*.golden) — NOT a redesign of the wording.

Scope (explicitly, per the P1 brief): this is the LIBRARY + its tests. `qq`/`findings.sh` are
NOT rewired to use it here — that is P4 ("producers emit records"). This module can already
read/write today's ON-DISK marker format losslessly (FindingsFile), so P4 has something to
migrate a real pending-findings.md through rather than a green-field rewrite.

Section semantics (unchanged from findings.sh, made explicit):
  TIER1 / XREF / AUDIT  — AUTO-RESOLVE. Each run's producer emits its FULL current set; a
                          fixed drift means the section is empty next time, so the finding
                          simply disappears. Primitive: `FindingsFile.set_section` (full
                          replace) — see findings_set_section.
  SALIENCE              — APPEND, not auto-resolve (each session's transcript is fixed
                          history, audited once). Primitive: `FindingsFile.append_salience`
                          (read current + union in new lines) — a captured item is cleared by
                          editing it out, never by re-running the producer. See findings.sh's
                          own docstring for why (owned by a per-session LLM auditor not
                          present in this dist).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

SECTION_ORDER = ("TIER1", "XREF", "AUDIT", "SALIENCE")
AUTO_RESOLVE_SECTIONS = ("TIER1", "XREF", "AUDIT")
# Append-only is a producer convention, not a set_section guard: SALIENCE grows via
# append_salience, PROPOSED-WRITES only via authgate.queue_proposal. set_section stays
# permissive because the documented auditor pattern is get_section + set_section(old + new).
APPEND_ONLY_SECTIONS = ("SALIENCE", "PROPOSED-WRITES")

_CLASS_RE = re.compile(r"^- \[([^\]]+)\]\s?(.*)$")


@dataclass
class Finding:
    """A single finding line. `fields` is the structured payload a class-specific renderer
    needs to reproduce the line; `text`, if set, is the exact rendered line (used when a line
    was parsed from an existing file for a class this module has no structured renderer for
    yet — it still round-trips byte-for-byte via verbatim replay, per D5's "renderer" being
    additive, not a precondition for representing a finding)."""
    cls: str
    fields: dict = field(default_factory=dict)
    text: Optional[str] = None

    def render(self) -> str:
        if self.text is not None:
            return self.text
        renderer = _RENDERERS.get(self.cls)
        if renderer is None:
            raise ValueError(f"no structured renderer registered for finding class {self.cls!r} "
                              f"(and no verbatim .text) – pass text= for a class not yet ported")
        return renderer(self.fields)

    def to_json(self) -> str:
        return json.dumps({"class": self.cls, "fields": self.fields, "text": self.text}, sort_keys=True)

    @staticmethod
    def from_json(s: str) -> "Finding":
        d = json.loads(s)
        return Finding(cls=d["class"], fields=d.get("fields") or {}, text=d.get("text"))


# ---- structured renderers: EXACT current-format lines (verified against P0 goldens) --------
def _render_t1_link(f: dict) -> str:
    tokens = f["tokens"]
    return (f"- [T1 link] {len(tokens)} unresolved [[link]](s) (may be intentional to-write "
            f"markers): {', '.join(tokens)}")


def _render_t1_size(f: dict) -> str:
    head = f["head"]
    if f.get("kind") == "body":
        return (f"- [T1 size] HEAD {head}: body {f['kb']}kB (update-lines lean) → "
                f"`qq compact` can't shrink this; condense the ## sections in a deliberate "
                f"rewrite, or read via `qq brief {head}`")
    return (f"- [T1 size] HEAD {head}: update-lines {f['kb']}kB / {f['lines']} lines → "
            f"`qq compact {head}` (folds old update-lines to the journal, keeps newest ~5 "
            f"+ the body; `qq brief` reads it meanwhile)")


def _render_t2_stale(f: dict) -> str:
    return (f"- [T2 stale?] memory {f['memory']} ({f['mem_date']}) vs HEAD {f['head']} "
            f"(updated {f['head_date']}, sim {f['sim']:.2f}) – verify the memory's claims "
            f"against the HEAD; edit or wave off (`qq waveoff {f['memory']} {f['head']}`).")


# ---- run_check's remaining T1 classes (producers emit records, not rendered prose) ---------
# Each renderer below is an EXACT port of the corresponding `echo "- [T1 ...] ..."` line in
# qq-legacy's run_check (see quintessence/checks.py, which builds these Finding.fields dicts
# from the same L1 objects run_check used to grep/awk over directly). Verified against
# qq-legacy's live-store output by tests/py/test_checks.py's parity fixtures. Following the
# precedent set by "T1 size" (one bracket tag, a `kind` field selects the variant): a class with
# several distinct wordings under ONE legacy bracket tag stays ONE Finding.cls (matching what
# parse_line() would recover from an on-disk line) with `fields["kind"]` as the discriminator,
# rather than inventing a separate cls per wording.
def _render_t1_index(f: dict) -> str:
    if f["kind"] == "orphan":
        return f"- [T1 index] memory/{f['basename']} has no MEMORY.md entry (orphan)"
    if f["kind"] == "dangling":
        return f"- [T1 index] MEMORY.md references {f['ref']} but the file is missing"
    return "- [T1 index] INDEX.md stale vs HEADs → run `qq reindex`"   # kind == "stale"


def _render_t1_fix(f: dict) -> str:
    return "- [T1 fix] INDEX.md was stale → reindexed + committed (auto)"


def _render_t1_mem_health(f: dict) -> str:
    if f["kind"] == "size":
        return (f"- [T1 mem-health] MEMORY.md is {f['bytes']}B (>= {f['max_bytes']}B) – "
                f"approaching the ~24KB silent-truncation cap; compact hooks or move detail "
                f"into topic files")
    if f["kind"] == "line":
        # `over` is a list of (label, length) pairs; the ORIGINAL bash (`printf '%s(%d) '`, no
        # separator collapsing) leaves a trailing space before the line ends — byte-parity kept
        # deliberately, not tidied up here (a real, if cosmetic, wart of the line this ports).
        over = "".join(f"{label}({length}) " for label, length in f["over"])
        return (f"- [T1 mem-health] MEMORY.md index line(s) over {f['max_chars']}B (budget "
                f"~200; trim the hook, detail lives in the topic file): {over}")
    return f"- [T1 mem-health] MEMORY.md duplicate index entr(y/ies): {', '.join(f['dups'])}"


def _render_t1_head(f: dict) -> str:
    marker = "> updated:" if f["kind"] == "missing-updated" else "> essence:"
    return f"- [T1 head] HEAD {f['head']} missing '{marker}' line"


def _render_t1_fm(f: dict) -> str:
    field_name = {"missing-name": "name:", "missing-description": "description:",
                  "missing-type": "type:"}[f["kind"]]
    return f"- [T1 fm] memory/{f['basename']}.md missing '{field_name}'"


def _render_t1_essence_lag(f: dict) -> str:
    return (f"- [T1 essence-lag] HEAD {f['head']}: an update-line supersedes/corrects a phrase "
            f"STILL verbatim in the essence – \"{f['phrase']}\" → the essence likely states a "
            f"now-false claim; refresh with `qq essence {f['head']} …`")


# ---- P4.5: the namespace design ruling's new Finding class (quintessence/slugs.py's per-tier
# SlugResolver) — a bare [[token]] whose normalized form matches more than one DISTINCT real
# file (a store-internal HEAD-vs-memory tie, 2+ kb docs from different source repos, or a
# derived/weak store guess contested by an independent kb doc). Deliberately NOT worded like
# "T1 link"'s unresolved-link finding: SlugResolver already has a real candidate here (more than
# one, in fact) — this is "which one did you mean", not "this doesn't exist". Same-basename-
# different-tier is no longer collision/hygiene noise; this is its own, lower-severity class.
def _render_t1_ambiguous(f: dict) -> str:
    cands = ", ".join(f["candidates"])
    return (f"- [T1 ambiguous] [[{f['token']}]] matches more than one candidate ({cands}) – "
            f"a genuine cross-repo/store naming collision, not a broken link: qualify the "
            f"reference (e.g. `[[kb-source/{f['token']}]]`) or adjudicate which is meant")


# ---- B4 (reality binding): the proc-vs-disk probe's finding class. NEW class, not a
# legacy port — quintessence/procprobe.py emits these records into its own auto-resolve PROC
# section; a restart clears the drift, the next probe run empties the section.
def _render_proc_drift(f: dict) -> str:
    return (f"- [PROC drift] {f['unit']} ({f['scope']}): running process predates on-disk "
            f"change – {f['path']} modified {f['mtime']}, unit started {f['started']}; "
            f"restart (after daemon-reload if the unit file moved) to reconcile, or exclude "
            f"via QQ_PROC_PROBE_EXCLUDE")


# ---- AUTHORING GATE (write-side model trust, quintessence/authgate.py): a non-trusted
# model's write to a security-tagged HEAD lands here as a PROPOSED entry instead of the HEAD,
# pending ratification by a trusted session. NEW class, not a legacy port. Unlike every class
# above, the payload (the verbatim proposed text — possibly a whole multi-line rewrite) is
# JSON-encoded inline: the findings file is line-oriented and marker-delimited, so raw
# multi-line content could collide with the section markers and would not round-trip; a JSON
# string keeps one-proposal-one-line, byte-exact recovery (parse_line decodes it back).
# APPEND-only semantics (SALIENCE-like): no producer re-emits this section; an entry is
# cleared only by adjudication (replay-then-delete, or delete = reject).
def _render_proposed_write(f: dict) -> str:
    return (f"- [PROPOSED write] {f['ts']} HEAD {f['head']} via `qq {f['verb']} {f['head']}` "
            f"by model {f['model']} – pending ratification: a trusted session replays the "
            f"JSON-decoded text through that verb, then deletes this line; text: "
            f"{json.dumps(f['text'], ensure_ascii=False)}")


_RENDERERS: dict[str, Callable[[dict], str]] = {
    "T1 link": _render_t1_link,
    "T1 size": _render_t1_size,
    "T2 stale?": _render_t2_stale,
    "T1 index": _render_t1_index,
    "T1 fix": _render_t1_fix,
    "T1 mem-health": _render_t1_mem_health,
    "T1 head": _render_t1_head,
    "T1 fm": _render_t1_fm,
    "T1 essence-lag": _render_t1_essence_lag,
    "T1 ambiguous": _render_t1_ambiguous,
    "PROC drift": _render_proc_drift,
    "PROPOSED write": _render_proposed_write,
}


# ---- best-effort parse of an EXISTING rendered line back into a Finding --------------------
_T1_LINK_RE = re.compile(
    r"^\d+ unresolved \[\[link\]\]\(s\) \(may be intentional to-write markers\): (.*)$")
_T1_SIZE_UL_RE = re.compile(
    r"^HEAD (\S+): update-lines (\d+)kB / (\d+) lines")
_T1_SIZE_BODY_RE = re.compile(r"^HEAD (\S+): body (\d+)kB \(update-lines lean\)")
_T2_STALE_RE = re.compile(
    r"^memory (\S+) \(([^)]+)\) vs HEAD (\S+) \(updated ([^,]+), sim ([\d.]+)\)")
_T1_AMBIGUOUS_RE = re.compile(r"^\[\[([^\]]+)\]\] matches more than one candidate \(([^)]*)\)")
_PROPOSED_WRITE_RE = re.compile(
    r"^(\S+) HEAD (\S+) via `qq (\S+) \S+` by model (\S+) [—–] pending ratification: .*; text: (.*)$")


def parse_line(line: str) -> Finding:
    """Recover a Finding from an already-rendered line. `text` is ALWAYS set to the verbatim
    input (so render() reproduces it byte-for-byte even if field-extraction below fails or the
    class isn't one of the structured ones); `fields` is filled in on a best-effort basis for
    the classes this module knows how to parse, for callers that want structured access
    (findings_next-style branching) without re-deriving it from the string themselves."""
    m = _CLASS_RE.match(line)
    if not m:
        return Finding(cls="", fields={}, text=line)
    cls, rest = m.group(1), m.group(2)
    fields: dict = {}
    if cls == "T1 link":
        lm = _T1_LINK_RE.match(rest)
        if lm:
            fields = {"tokens": [t.strip() for t in lm.group(1).split(",") if t.strip()]}
    elif cls == "T1 size":
        um = _T1_SIZE_UL_RE.match(rest)
        bm = _T1_SIZE_BODY_RE.match(rest)
        if um:
            fields = {"head": um.group(1), "kind": "update-lines",
                       "kb": int(um.group(2)), "lines": int(um.group(3))}
        elif bm:
            fields = {"head": bm.group(1), "kind": "body", "kb": int(bm.group(2))}
    elif cls == "T2 stale?":
        sm = _T2_STALE_RE.match(rest)
        if sm:
            fields = {"memory": sm.group(1), "mem_date": sm.group(2), "head": sm.group(3),
                       "head_date": sm.group(4), "sim": float(sm.group(5))}
    elif cls == "T1 ambiguous":
        am = _T1_AMBIGUOUS_RE.match(rest)
        if am:
            fields = {"token": am.group(1),
                       "candidates": [c.strip() for c in am.group(2).split(",") if c.strip()]}
    elif cls == "PROPOSED write":
        pm = _PROPOSED_WRITE_RE.match(rest)
        if pm:
            try:
                text_val = json.loads(pm.group(5))
            except ValueError:
                text_val = None   # best-effort contract: fields stay partial, .text is exact
            if isinstance(text_val, str):
                fields = {"ts": pm.group(1), "head": pm.group(2), "verb": pm.group(3),
                           "model": pm.group(4), "text": text_val}
    return Finding(cls=cls, fields=fields, text=line)


# ---- FindingsFile: the on-disk marker format (findings.sh), lossless read/write -------------
def _markers(tag: str) -> tuple[str, str]:
    return f"<!-- {tag}:START -->", f"<!-- {tag}:END -->"


# findings.sh's OWN marker regex is generic — `findings_render`'s awk matches literally
# `<!-- .*:START -->` / `<!-- .*:END -->`, not a fixed set of four tag names. A deployment can
# (and, on the author's live store, does: fidelity-audit.sh's `findings_set_section FIDELITY`)
# grow an ad-hoc extra section that `findings_set_section` appends fresh markers for at the
# END of the file (its "missing markers -> append" fallback) if they don't already exist. A
# FindingsFile that only recognized SECTION_ORDER would silently DROP such a section's content
# from render_lines()/count() — a real divergence caught by parity-testing against the live
# store, not a hypothetical. So parsing/rendering below is fully generic over whatever tags are
# actually present, in FILE ORDER; SECTION_ORDER only fixes the seed/default order for a FRESH
# (unparsed) FindingsFile(), matching findings_init's scaffold.
_MARKER_RE = re.compile(r"^<!-- (.*):(START|END) -->$")


class FindingsFile:
    """Models findings.sh's pending-findings.md: marker-delimited sections, each holding an
    ordered list of raw lines (blank lines are not meaningful content — findings_render strips
    them, matching `awk ... inside && NF {print}`). A freshly-constructed instance seeds the
    four sections findings_init scaffolds (TIER1/XREF/AUDIT/SALIENCE) in that order; parse()
    additionally preserves any OTHER tag actually found in a real file, in the order it
    appears — see the generic-marker note above."""

    def __init__(self, sections: Optional[dict[str, list[str]]] = None):
        self.sections: dict[str, list[str]] = {tag: [] for tag in SECTION_ORDER}
        for tag, lines in (sections or {}).items():
            self.sections[tag] = list(lines)

    # ---- parse / serialize the exact on-disk format ------------------------------------
    @classmethod
    def parse(cls, text: str) -> "FindingsFile":
        ff = cls()
        current: Optional[str] = None
        for line in text.split("\n"):
            m = _MARKER_RE.match(line)
            if m:
                tag, kind = m.group(1), m.group(2)
                if kind == "START":
                    current = tag
                    ff.sections.setdefault(tag, [])
                else:
                    current = None
                continue
            if current is not None:
                ff.sections[current].append(line)
        return ff

    def serialize(self) -> str:
        """Reconstructs the on-disk layout: each section's markers, its lines (if any), in the
        order the sections exist on this instance (SECTION_ORDER for a fresh instance; a
        parsed instance's own file order, generic-tag sections included, thereafter) —
        matching the bash literal `printf` in findings_init and the in-place rewrite
        `findings_set_section` performs (content between markers, markers preserved, one
        section at a time; a genuinely NEW tag's markers land at the end, matching
        `findings_set_section`'s own "append if missing" fallback since a new key is always
        inserted at the end of `self.sections`)."""
        out: list[str] = []
        for tag, lines in self.sections.items():
            start, end = _markers(tag)
            out.append(start)
            out.extend(lines)
            out.append(end)
        return "\n".join(out) + "\n"

    # ---- section primitives (mirror findings_set_section / findings_get_section) --------
    def get_section(self, tag: str) -> list[str]:
        return list(self.sections.get(tag, []))

    def set_section(self, tag: str, lines: list[str]) -> None:
        """Full replace — the AUTO-RESOLVE primitive for TIER1/XREF/AUDIT (and, on a
        deployment that has grown one, any other ad-hoc auto-resolve section — e.g. FIDELITY):
        call with a producer's complete current output; anything not re-emitted this run
        silently drops. A previously-unseen tag is appended at the end, matching
        findings_set_section's "append fresh markers if missing" fallback."""
        self.sections[tag] = list(lines)

    def append_salience(self, new_lines: list[str]) -> None:
        """The APPEND (not auto-resolve) primitive for SALIENCE: union new_lines onto the
        existing section rather than replacing it, matching a per-session auditor's
        get_section + set_section(get_section + new) pattern (findings.sh's own docstring)."""
        self.sections["SALIENCE"] = self.get_section("SALIENCE") + list(new_lines)

    # ---- rendering (mirrors findings_render / findings_count) -----------------------------
    def render_lines(self) -> list[str]:
        """Every non-blank line across all sections THIS INSTANCE KNOWS ABOUT, in the order
        they were added/parsed — matches `findings_render`'s awk (`inside && NF {print}`:
        strips markers AND blank lines, over ANY `<!-- tag:START/END -->` pair, not just the
        four SECTION_ORDER names)."""
        out: list[str] = []
        for lines in self.sections.values():
            out.extend(ln for ln in lines if ln.strip())
        return out

    def render(self) -> str:
        return "\n".join(self.render_lines())

    def count(self) -> int:
        return len(self.render_lines())

    def findings(self) -> list[Finding]:
        """render_lines(), each parsed into a Finding (best-effort fields; always exact text)."""
        return [parse_line(ln) for ln in self.render_lines()]
