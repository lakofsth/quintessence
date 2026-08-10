# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.heads — L1: the Head parser/serializer.

A HEAD (RUBRIC.md) is: title / `> updated:` lines (newest prepended right after the title,
each possibly followed by bare continuation-paragraph lines) / `> essence:` / optional other
`> key: value` meta lines / a blank line / `## `-headed body sections. This module is THE
reader of that structure: since the 2026-08-09 reader unification, every surface that asks
"is this line an update-line and what are its parts" asks here (parse/update_lines/
update_marker_flags for whole documents, the is_*_marker predicates for fragments), and
nothing else in the tree spells the grammar. Two deliberate exceptions live in write.py, both
DELIBERATELY LOOSER courtesy strippers whose looseness carries no safety weight (whatever they
fail to strip lands as prose behind a stamp qq composes): `_UPDATED_MARKER_RE` and
`_LEADING_ISO_RE` — see their shared comment. Every ACCEPTANCE decision — including the
composer's keep-the-caller's-timestamp branch (`_first_line_keepable_stamp`), which the
d78810c review caught still carrying its own looser spelling — derives from this module's
grammar; a stamp-shaped acceptance may be NARROWER than the reader (the rejected shape becomes
inert prose), never looser (that is a forgery hole).

FENCE-AWARE: a body line that happens to start with "> updated:" / "> essence:" /
"## " (e.g. a HEAD excerpt pasted into a RE-ENTER section as an example) must not be mistaken
for a real structural marker. This parser tracks fenced-code state (``` or ~~~,
CommonMark-style: a closing fence must reuse the opening fence character and be at least as
long) and ignores all three markers while inside a fence.
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
from datetime import datetime, timezone
from typing import Union

# The marker spellings. Every regex below is BUILT from these constants so there is exactly one
# place each prefix is spelled; every other module asks this one (via the predicates further
# down or via parse()/update_lines()) instead of respelling it. That is the 2026-08-09 ruling:
# one reader, one grammar — a guard's pattern comes from the reader it protects, and with one
# reader the guard and the reader cannot drift apart.
UPDATE_PREFIX = "> updated:"
ESSENCE_PREFIX = "> essence:"
BODY_HEADER_PREFIX = "## "

_TITLE_RE = re.compile(r"^# (?!#)")            # "# " but not "## " (H1 only)
_BODY_HEADER_RE = re.compile(rf"^{re.escape(BODY_HEADER_PREFIX)}")
_UPDATED_RE = re.compile(rf"^{re.escape(UPDATE_PREFIX)}\s*(.*)$")
_ESSENCE_RE = re.compile(rf"^{re.escape(ESSENCE_PREFIX)}\s*(.*)$")
_META_RE = re.compile(r"^> ")
_FENCE_RE = re.compile(r"^(\s*)([`~]{3,})")
# An update marker's leading ISO-8601-ish timestamp, if present (qq-write's own convention —
# see its "safety net" comment: bare prose gets a fresh timestamp, an already-stamped line
# keeps its stamp). Not required for round-trip; a convenience accessor only.
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z?)?)\s*(.*)$")


# ---- line predicates (fragment-context entry points) -----------------------------------------
# These answer "does this LINE spell the marker" with no document context. They are correct
# where the surrounding state is known — the first line of caller content, a rendered fragment
# whose lines were already classified once (refsview), a search chunk whose fence state is
# unknowable. Whole documents go through parse()/update_lines()/update_marker_flags(), which
# add the fence and header/body-region rules on top of these same predicates.

def is_update_marker(line: str) -> bool:
    return line.startswith(UPDATE_PREFIX)


def is_essence_marker(line: str) -> bool:
    return line.startswith(ESSENCE_PREFIX)


def is_body_header(line: str) -> bool:
    return line.startswith(BODY_HEADER_PREFIX)


def canonical_newlines(text: str) -> str:
    """The reader's definition of "a line" starts here: \\r\\n and lone \\r become \\n. Files are
    read in text mode, where Python's universal-newline translation applies exactly this mapping
    — so caller-supplied content must get the same mapping BEFORE any line-oriented decision is
    made about it, or the decision is made about lines the readers will never see (a forged
    update-line hidden behind a lone \\r was invisible to a split("\\n") guard and real to every
    reader; round-five finding (a))."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def stamp_of(rest: str) -> "str | None":
    """The leading timestamp of an update-line's rest (the text after '> updated: '), or None.
    THE stamp grammar — date required, time and Z optional (_TS_RE)."""
    m = _TS_RE.match(rest)
    return m.group(1) if m else None


def stamp_datetime(ts: str):
    """A reader-parsed stamp as an aware UTC datetime, or None if it does not denote a real
    moment (e.g. month 13). Date-only stamps mean midnight UTC."""
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def split_stamped(line: str) -> "tuple[str, str] | None":
    """Split a stamped update-line into (lead, text): lead is everything through the stamp and
    the whitespace after it, text is the rest. None unless the READER parses a leading stamp
    with whitespace separating it from the text — the insertion point for the derived agent
    marker, computed from the reader's own spans rather than a re-spelled grammar (the previous
    private regex accepted stamp forms the reader never parsed, e.g. fractional seconds, so a
    marker could be inserted after a "stamp" no surface would ever rank by)."""
    um = _UPDATED_RE.match(line)
    if not um:
        return None
    rest = um.group(1)
    tm = _TS_RE.match(rest)
    if not tm:
        return None
    if tm.start(2) == tm.end(1):   # no whitespace between stamp and text (or bare stamp with
        return None                # nothing after) — parity with the replaced regex's \s+
    off = um.start(1)
    return line[: off + tm.start(2)], rest[tm.start(2):]


@dataclass
class UpdateItem:
    marker: str                                   # raw "> updated: ..." line, verbatim
    continuation: list[str] = field(default_factory=list)  # raw lines following, verbatim

    @property
    def rest(self) -> str:
        """Everything after '> updated: ' (leading whitespace consumed) — the raw string the
        menu/digest UPDATED column renders, stamp and all."""
        m = _UPDATED_RE.match(self.marker)
        return m.group(1) if m else ""

    @property
    def text(self) -> str:
        """The marker's text after '> updated: ', with any leading ISO-8601 stamp stripped."""
        ts = _TS_RE.match(self.rest)
        return ts.group(2) if ts else self.rest

    @property
    def timestamp(self) -> str | None:
        return stamp_of(self.rest)


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
        """Bytes of the update-line region (marker + continuation, every UpdateItem; each
        line's length + 1 for its newline) — what `qq compact` folds, so the size nudge
        measures THIS, not the whole file."""
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


FENCE_CLOSED = (False, "", 0)   # the initial fence_toggle state


def fence_toggle(line: str, state: tuple[bool, str, int]) -> tuple[bool, str, int]:
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


def _classify(lines: "list[str]") -> "list[str]":
    """Per-line kinds under THE document grammar — one state machine for every reader:
    'preamble' (before the header region begins), 'title', 'update', 'essence', 'meta',
    'text' (header-region bare/blank/fenced line), 'body' (first non-fenced '## ' line to EOF).

    Region rules (2026-08-09 ruling): the header region begins at the H1 title — or, in a
    file with no H1, at the first non-fenced update/essence marker. Title-less HEADs exist in
    the live store, and the whole-file readers this machine replaces always saw their
    update-lines; requiring a title made the parser the one disagreeing reader. The first
    non-fenced '## ' line starts the body from ANY phase, and everything from it on is body:
    raw, unparsed, inert — a pasted example there is not structure (A5). Markers inside a
    code fence are never structure either."""
    kinds: "list[str]" = []
    fence_state = FENCE_CLOSED
    phase = "seek"   # "seek" -> "header" -> "body"
    for line in lines:
        if phase == "body":
            kinds.append("body")
            continue
        in_fence_before = fence_state[0]
        fence_state = fence_toggle(line, fence_state)
        if not in_fence_before and _BODY_HEADER_RE.match(line):
            phase = "body"
            kinds.append("body")
            continue
        if phase == "seek":
            if not in_fence_before and _TITLE_RE.match(line):
                kinds.append("title")
                phase = "header"
            elif not in_fence_before and _UPDATED_RE.match(line):
                kinds.append("update")
                phase = "header"
            elif not in_fence_before and _ESSENCE_RE.match(line):
                kinds.append("essence")
                phase = "header"
            else:
                kinds.append("preamble")
            continue
        # phase == "header"
        if not in_fence_before and _UPDATED_RE.match(line):
            kinds.append("update")
        elif not in_fence_before and _ESSENCE_RE.match(line):
            kinds.append("essence")
        elif not in_fence_before and _META_RE.match(line):
            kinds.append("meta")
        else:
            kinds.append("text")
    return kinds


def parse(text: str) -> Head:
    trailing_newline = text.endswith("\n")
    lines = text.split("\n")
    if trailing_newline:
        lines = lines[:-1]

    kinds = _classify(lines)

    preamble: list[str] = []
    title: str | None = None
    header_items: list[HeaderItem] = []
    body_lines: list[str] = []
    current_update: UpdateItem | None = None

    for i, (line, kind) in enumerate(zip(lines, kinds)):
        if kind == "body":
            # everything remaining, verbatim, no further structural parsing — this is what
            # makes a pasted "> updated:"/"## " example in the body inert by construction.
            body_lines = lines[i:]
            break
        if kind == "preamble":
            preamble.append(line)
        elif kind == "title":
            title = line
        elif kind == "update":
            current_update = UpdateItem(marker=line)
            header_items.append(current_update)
        elif kind == "essence":
            header_items.append(EssenceItem(raw=line))
            current_update = None
        elif kind == "meta":
            header_items.append(MetaItem(raw=line))
            current_update = None
        else:   # "text"
            if current_update is not None:
                current_update.continuation.append(line)
            else:
                header_items.append(TextItem(raw=line))

    return Head(preamble=preamble, title=title, header_items=header_items,
                body_lines=body_lines, trailing_newline=trailing_newline)


def update_lines(text: str) -> "list[UpdateItem]":
    """THE document-level reader: every real update-line of `text`, in order — defined as
    parse(text).updates so there is nothing separate to drift."""
    return parse(text).updates


def update_marker_flags(lines: "list[str]") -> "list[bool]":
    """Per-line "is this line a real update-line marker" flags, for callers that walk lines
    with their own state machines (brief, compact, novel-line bucketing): the SAME
    classification parse() uses, exposed positionally so those machines cannot drift from
    the reader."""
    return [k == "update" for k in _classify(lines)]


def serialize(head: Head) -> str:
    return head.serialize()


# ---- rendering helpers on the one reader -----------------------------------------------------
# Until 2026-08-09 the three helpers below were DELIBERATE, LITERAL ports of the legacy bash
# engine's awk/grep one-liners, kept whole-file and fence-unaware for byte-parity with that
# engine. The parity constraint is gone — the bash engine is not on this machine and the
# cross-engine parity suite no longer exists — and the reader-unification ruling (see
# _classify) converges them on parse(). What that changes at the margins, measured against the
# live store before ruling: a fenced or body-region '> updated:'/'> essence:' example stops
# being counted or displayed (zero such lines existed), and a title-less HEAD's update-lines
# START being seen by the parse() side (four such files existed, and the whole-file readers —
# these three — always saw them; the count/display surfaces themselves change nothing there).
def count_update_markers(text: str) -> int:
    """Count of REAL update-lines — the one reader's count, no longer `grep -c '^> updated:'`
    (a pasted example in a fence or in the body no longer inflates the [T1 size] nudge or the
    `findings next` HEAD summary). Used by legacy_brief, checks, and the write path's
    compaction nudge."""
    return len(update_lines(text))


def head_meta(text: str) -> tuple[str, str]:
    """The FIRST real update-line's text after the prefix (timestamp/parenthetical NOT
    stripped — the menu/digest UPDATED column convention) and the FIRST real essence line's
    text after its prefix. Either half is "" if that marker is absent. Since the 2026-08-09
    ruling this is the one reader's first-wins view (header region only) rather than a
    whole-file scan."""
    head = parse(text)
    first = head.updates[0].rest if head.updates else ""
    essence = head.essence
    return first, essence if essence is not None else ""


def legacy_brief(text: str) -> tuple[int, list[str]]:
    """Port of qq's `brief_one` awk body: returns (total update-line count, the lines the awk
    would print, in order). The caller (quintessence.cli) prepends the
    "===== quintessence: TOPIC (BRIEF ...) =====" header (which needs the total count) and
    appends the trailing blank line brief_one's shell wrapper prints after the awk call.
    Update-line recognition comes from the one reader (update_marker_flags); the awk's own
    display state machine — which sections print, the newest-3 cap, the A5
    continuation-dropping wart — is preserved verbatim."""
    lines = text.split("\n")
    if lines and text.endswith("\n"):
        lines = lines[:-1]
    flags = update_marker_flags(lines)
    tot = sum(flags)

    out: list[str] = []
    h = False        # title already printed
    insec = False     # inside SOME "## " body section
    re_flag = False   # inside the "## RE-ENTER HERE" section specifically
    u = 0             # update-line counter (only the newest 3 print)
    for i, ln in enumerate(lines):
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
        if flags[i]:
            u += 1
            if u <= 3:
                out.append(ln)
            continue
        if ln.startswith("> "):
            out.append(ln); continue
        # no awk rule matches this line (e.g. a blank line before the first "## " section, or a
        # bare continuation paragraph of an update-line) -> dropped, matching today's output.
    return tot, out
