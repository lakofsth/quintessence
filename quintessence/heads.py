# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.heads — L1: the Head parser/serializer.

A HEAD (RUBRIC.md) is: title / `> updated:` lines (newest prepended right after the title,
each possibly followed by bare continuation-paragraph lines) / `> essence:` / optional other
`> key: value` meta lines / a blank line / `## `-headed body sections. Today this structure is
implicit — read by ad-hoc grep/awk/sed scattered across `qq`, `qq-write`, `staleness-xref.py`
(see ul_region_bytes in qq-lib.sh, the compact/essence awk blocks in qq-write, brief_one/
run_check in qq). This module makes it explicit as one parser everyone can import.

FENCE-AWARE: a body line that happens to start with "> updated:" / "> essence:" /
"## " (e.g. a HEAD excerpt pasted into a RE-ENTER section as an example) must not be mistaken
for a real structural marker. The existing bash tooling scans the WHOLE FILE for these markers
with no fence awareness at all (ul_region_bytes is a plain awk /pattern/ over every line), so a
self-quoting HEAD silently miscounts its own size / drops continuation paragraphs. This parser
tracks fenced-code state (``` or ~~~, CommonMark-style: a closing fence must reuse the opening
fence character and be at least as long) and ignores all three markers while inside a fence.
More importantly, once the header/body boundary (the first non-fenced "## " line) is found,
everything after it is `body` — a raw, unparsed blob. A pasted "> updated:" example inside the
body is therefore inert by construction, not merely correctly classified.

LOSSLESS ROUND-TRIP is the load-bearing property: `parse(serialize(parse(x))) ==
parse(x)` and `serialize(parse(x)) == x` byte-for-byte for every real HEAD. Every original line
is bucketed into exactly one of: preamble (before a title, if any), the title line itself, one
header-region item (an update-line marker + its continuation lines, an essence line, another
`> `-prefixed meta line, or a bare text/blank line), or the body (everything from the first
real "## " line to EOF, verbatim). Serialization is the exact inverse concatenation, so any
line the parser doesn't have a special-case for still round-trips via TextItem/body.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Union

_TITLE_RE = re.compile(r"^# (?!#)")            # "# " but not "## " (H1 only)
_BODY_HEADER_RE = re.compile(r"^## ")
_UPDATED_RE = re.compile(r"^> updated:\s*(.*)$")
_ESSENCE_RE = re.compile(r"^> essence:\s*(.*)$")
_META_RE = re.compile(r"^> ")
_FENCE_RE = re.compile(r"^(\s*)([`~]{3,})")
# An update marker's leading ISO-8601-ish timestamp, if present (qq-write's own convention —
# see its "safety net" comment: bare prose gets a fresh timestamp, an already-stamped line
# keeps its stamp). Not required for round-trip; a convenience accessor only.
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z?)?)\s*(.*)$")


@dataclass
class UpdateItem:
    marker: str                                   # raw "> updated: ..." line, verbatim
    continuation: list[str] = field(default_factory=list)  # raw lines following, verbatim

    @property
    def text(self) -> str:
        """The marker's text after '> updated: ', with any leading ISO-8601 stamp stripped."""
        m = _UPDATED_RE.match(self.marker)
        rest = m.group(1) if m else ""
        ts = _TS_RE.match(rest)
        return ts.group(2) if ts else rest

    @property
    def timestamp(self) -> str | None:
        m = _UPDATED_RE.match(self.marker)
        rest = m.group(1) if m else ""
        ts = _TS_RE.match(rest)
        return ts.group(1) if ts else None


@dataclass
class EssenceItem:
    raw: str                                       # raw "> essence: ..." line, verbatim

    @property
    def text(self) -> str:
        m = _ESSENCE_RE.match(self.raw)
        return m.group(1) if m else ""


@dataclass
class MetaItem:
    raw: str                                       # any other "> key: ..." line, verbatim


@dataclass
class TextItem:
    raw: str                                       # a bare/blank line in the header region, verbatim


HeaderItem = Union[UpdateItem, EssenceItem, MetaItem, TextItem]


@dataclass
class Head:
    preamble: list[str]         # raw lines before a title (empty in every well-formed HEAD)
    title: str | None           # raw "# ..." line, verbatim (None if the file has no H1 at all)
    header_items: list[HeaderItem]
    body_lines: list[str]       # raw lines from the first non-fenced "## " line to EOF, verbatim
    trailing_newline: bool

    # ---- convenience accessors (derived; do not affect serialization) -------------------
    @property
    def title_text(self) -> str | None:
        """Title with the leading '# ' stripped, or None."""
        return self.title[2:] if self.title is not None else None

    @property
    def updates(self) -> list[UpdateItem]:
        return [i for i in self.header_items if isinstance(i, UpdateItem)]

    @property
    def essence(self) -> str | None:
        """Text of the FIRST '> essence:' line — matches the existing tools' first-wins read
        (qq's `meta()` grep -m1; qq-write's essence-refresh awk `&& !d`)."""
        for i in self.header_items:
            if isinstance(i, EssenceItem):
                return i.text
        return None

    @property
    def body(self) -> str:
        return "\n".join(self.body_lines)

    @property
    def update_line_region_bytes(self) -> int:
        """Bytes of the update-line region (marker + continuation, every UpdateItem) — matches
        qq-lib.sh's `ul_region_bytes` (each line's length + 1 for its newline)."""
        n = 0
        for item in self.updates:
            n += len(item.marker) + 1
            for c in item.continuation:
                n += len(c) + 1
        return n

    # ---- serialize -------------------------------------------------------------------------
    def serialize(self) -> str:
        lines: list[str] = list(self.preamble)
        if self.title is not None:
            lines.append(self.title)
        for item in self.header_items:
            if isinstance(item, UpdateItem):
                lines.append(item.marker)
                lines.extend(item.continuation)
            else:
                lines.append(item.raw)
        lines.extend(self.body_lines)
        text = "\n".join(lines)
        if self.trailing_newline:
            text += "\n"
        return text


def _fence_toggle(line: str, state: tuple[bool, str, int]) -> tuple[bool, str, int]:
    """(in_fence, fence_char, fence_len) -> the next state after `line`. CommonMark-ish: a
    fence opens on a line whose (optionally indented) content starts with >=3 backticks or
    tildes; it closes on a line consisting of only >= that many of the SAME character
    (+ optional surrounding whitespace)."""
    in_fence, fence_char, fence_len = state
    if not in_fence:
        m = _FENCE_RE.match(line)
        if m:
            ch = m.group(2)[0]
            return True, ch, len(m.group(2))
        return state
    # in a fence: does this line close it?
    stripped = line.strip()
    if stripped and set(stripped) == {fence_char} and len(stripped) >= fence_len:
        return False, "", 0
    return state


def parse(text: str) -> Head:
    trailing_newline = text.endswith("\n")
    lines = text.split("\n")
    if trailing_newline:
        lines = lines[:-1]

    preamble: list[str] = []
    title: str | None = None
    header_items: list[HeaderItem] = []
    body_lines: list[str] = []

    fence_state = (False, "", 0)
    current_update: UpdateItem | None = None
    phase = "seek_title"   # "seek_title" -> "header" -> "body"

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        in_fence_before = fence_state[0]
        fence_state = _fence_toggle(line, fence_state)

        if phase == "seek_title":
            if not in_fence_before and _TITLE_RE.match(line):
                title = line
                phase = "header"
            else:
                preamble.append(line)
            i += 1
            continue

        if phase == "header":
            if not in_fence_before and _BODY_HEADER_RE.match(line):
                phase = "body"
                continue  # re-process this line in "body" phase (bulk-copy below)
            if not in_fence_before and _UPDATED_RE.match(line):
                current_update = UpdateItem(marker=line)
                header_items.append(current_update)
            elif not in_fence_before and _ESSENCE_RE.match(line):
                header_items.append(EssenceItem(raw=line))
                current_update = None
            elif not in_fence_before and _META_RE.match(line):
                header_items.append(MetaItem(raw=line))
                current_update = None
            else:
                if current_update is not None:
                    current_update.continuation.append(line)
                else:
                    header_items.append(TextItem(raw=line))
            i += 1
            continue

        # phase == "body": everything remaining, verbatim, no further structural parsing —
        # this is what makes a pasted "> updated:"/"## " example in the body inert (A5).
        body_lines = lines[i:]
        break

    return Head(preamble=preamble, title=title, header_items=header_items,
                body_lines=body_lines, trailing_newline=trailing_newline)


def serialize(head: Head) -> str:
    return head.serialize()


# ---- legacy-parity renderers (P2) ------------------------------------------------------------
# The two helpers below are DELIBERATE, LITERAL ports of qq's own awk/grep one-liners (meta(),
# brief_one()) — NOT built on the fence-aware `parse()` above. `qq brief`'s exact output
# (including the audit-A5 wart it does not yet fix: a bare continuation paragraph under a
# `> updated:` line is silently dropped unless it happens to start with "> ") is a byte-parity
# contract with the P0 surface-freeze goldens for THIS phase (P2 mechanically ports rendering;
# A5's rendering fix, if ever made, is a deliberate later decision, not a silent side effect of
# reusing the lossless parser here). Kept in heads.py (not cli.py) because it is still "what a
# HEAD's raw text means", just the pre-clean-room reading of it.
def count_update_markers(text: str) -> int:
    """Count of lines starting '> updated:' — matches `grep -c '^> updated:' file` (whole-file,
    NOT fence-aware; used by both legacy_brief and the [T1 size] `findings next` HEAD summary)."""
    lines = text.split("\n")
    if lines and text.endswith("\n"):
        lines = lines[:-1]
    return sum(1 for ln in lines if ln.startswith("> updated:"))


def head_meta(text: str) -> tuple[str, str]:
    """Exact port of qq's `head_meta()` awk: the FIRST '> updated:' line's text after the
    prefix (timestamp/parenthetical NOT stripped — unlike UpdateItem.text, which is a
    different, later-added convenience accessor) and the FIRST '> essence:' line's text after
    its prefix, in one pass. Either half is "" if that marker is absent. Used by menu/digest for
    the UPDATED/ESSENCE columns — the display convention predates (and differs from) the
    fence-aware parser's own `.essence`/`.updates` accessors, so this stays a literal port
    rather than a call into `parse()`."""
    lines = text.split("\n")
    if lines and text.endswith("\n"):
        lines = lines[:-1]
    u = ""
    e = ""
    for ln in lines:
        if u == "" and ln.startswith("> updated:"):
            u = _UPDATED_RE.match(ln).group(1)
        elif e == "" and ln.startswith("> essence:"):
            e = _ESSENCE_RE.match(ln).group(1)
        if u != "" and e != "":
            break
    return u, e


def legacy_brief(text: str) -> tuple[int, list[str]]:
    """Exact port of qq's `brief_one` awk body: returns (total '> updated:' line count, the
    lines the awk would print, in order). The caller (quintessence.cli) prepends the
    "===== quintessence: TOPIC (BRIEF ...) =====" header (which needs the total count) and
    appends the trailing blank line brief_one's shell wrapper prints after the awk call."""
    lines = text.split("\n")
    if lines and text.endswith("\n"):
        lines = lines[:-1]
    tot = sum(1 for ln in lines if ln.startswith("> updated:"))

    out: list[str] = []
    h = False        # title already printed
    insec = False     # inside SOME "## " body section
    re_flag = False   # inside the "## RE-ENTER HERE" section specifically
    u = 0             # update-line counter (only the newest 3 print)
    for ln in lines:
        if not h and ln.startswith("# "):
            out.append(ln); h = True; continue
        if ln.startswith("## RE-ENTER HERE"):
            re_flag = True; insec = True; out.append(ln); continue
        if ln.startswith("## "):
            re_flag = False; insec = True; continue
        if insec and not re_flag:
            continue
        if re_flag:
            out.append(ln); continue
        if ln.startswith("> updated:"):
            u += 1
            if u <= 3:
                out.append(ln)
            continue
        if ln.startswith("> "):
            out.append(ln); continue
        # no awk rule matches this line (e.g. a blank line before the first "## " section, or a
        # bare continuation paragraph of an update-line) -> dropped, matching today's output.
    return tot, out
