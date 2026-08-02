# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.memory — L1: the MemoryFact parser.

A memory fact is a YAML-frontmatter markdown file (Claude Code's own memory-tool format):
`---` / `name:` / `description:` / `metadata:` (nested `type:`, `node_type:`, etc.) / `---` /
body prose. Existing tooling (qq's run_check, staleness-xref.py's `_mem_meta`) never parses
this as real YAML — it grep/regexes tolerant, indentation-agnostic patterns (`^name:`,
`^[[:space:]]*type:`) over the frontmatter block. This module keeps that spirit deliberately:
it does NOT bring in a YAML dependency (stdlib-only) and does NOT interpret nested structure —
it locates the frontmatter block by its `---` delimiters, keeps every line inside it verbatim,
and exposes the same tolerant field lookups (name / description / type / xref-override) the
bash and python originals use. This keeps parsing LOSSLESS regardless of how deep a future
memory-tool version nests its metadata, at the cost of not being a "real" YAML parser — a
deliberate scope match to what the rest of the system already assumes about this format.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_NAME_RE = re.compile(r"^name:\s*(.*)$")
_DESCRIPTION_RE = re.compile(r"^description:\s*(.*)$")
_TYPE_RE = re.compile(r"^[ \t]*type:\s*(.*)$")
_XREF_RE = re.compile(r"^[ \t]*xref:\s*(.*)$")


def _first_match(pattern: re.Pattern, lines: list[str]) -> str | None:
    for ln in lines:
        m = pattern.match(ln)
        if m:
            return m.group(1).strip()
    return None


@dataclass
class MemoryFact:
    has_frontmatter: bool
    frontmatter_lines: list[str] = field(default_factory=list)  # raw lines BETWEEN the '---' delimiters
    body_lines: list[str] = field(default_factory=list)         # raw lines after the closing '---'
    preamble_lines: list[str] = field(default_factory=list)     # only set when has_frontmatter is False
    trailing_newline: bool = True

    # ---- convenience accessors (tolerant of arbitrary nesting depth, matching the existing
    # grep-based readers rather than a structural YAML parse) -----------------------------
    @property
    def name(self) -> str | None:
        return _first_match(_NAME_RE, self.frontmatter_lines)

    @property
    def description(self) -> str | None:
        return _first_match(_DESCRIPTION_RE, self.frontmatter_lines)

    @property
    def type(self) -> str | None:
        return _first_match(_TYPE_RE, self.frontmatter_lines)

    @property
    def xref(self) -> str | None:
        """Per-memory T2-xref override (`xref: track` force-includes, `xref: skip` force-
        excludes) — see staleness-xref.py's `_mem_meta`/`is_generic`."""
        return _first_match(_XREF_RE, self.frontmatter_lines)

    @property
    def body(self) -> str:
        return "\n".join(self.body_lines)

    # ---- serialize --------------------------------------------------------------------------
    def serialize(self) -> str:
        if self.has_frontmatter:
            lines = ["---"] + self.frontmatter_lines + ["---"] + self.body_lines
        else:
            lines = list(self.preamble_lines)
        text = "\n".join(lines)
        if self.trailing_newline:
            text += "\n"
        return text


def parse(text: str) -> MemoryFact:
    trailing_newline = text.endswith("\n")
    lines = text.split("\n")
    if trailing_newline:
        lines = lines[:-1]

    if lines and lines[0] == "---":
        close_idx = None
        for i in range(1, len(lines)):
            if lines[i] == "---":
                close_idx = i
                break
        if close_idx is not None:
            return MemoryFact(
                has_frontmatter=True,
                frontmatter_lines=lines[1:close_idx],
                body_lines=lines[close_idx + 1:],
                trailing_newline=trailing_newline,
            )
    return MemoryFact(has_frontmatter=False, preamble_lines=lines, trailing_newline=trailing_newline)


def serialize(fact: MemoryFact) -> str:
    return fact.serialize()
