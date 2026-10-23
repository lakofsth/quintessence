# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quote-mask.py -- bash-aware, length-preserving quote mask for `idle-autosave.sh`.

CLI: reads one JSON string per stdin line (a whole, possibly multi-line, shell command), masks the
quoted spans and comments of the command AS BASH LEXES IT, then folds every newline run to `;` and
prints one line per command. The caller greps that output for a command-position verb, so a verb
that lives inside a quoted argument, a heredoc-ish printf, a grep pattern or a comment is data and
does not match, while a real unquoted `echo x | qq update ...` survives.

Why not two regexes (strip '...' then "..."): an apostrophe inside a double-quoted span ("it's")
pairs with the next literal quote and deletes everything between -- including a REAL write in
command position. That is the fail-open direction for a gate that decides whether a checkpoint
already happened. Here inside "..." an apostrophe is a letter, inside '...' a double quote is.

LENGTH-PRESERVING: every quote mark and masked character becomes one fill character (an escape pair
becomes two), so an index into the mask is an index into the raw text. An UNBALANCED quote masks the
rest of the text as data.

Also modelled because bash lexes them: a `#` at the start of a word outside any quote begins a
COMMENT to end of line (an apostrophe in `# don't ...` must not open a span; the comment masks to
spaces and keeps its newline, so the next line is code again), and `$'...'` ANSI-C quoting, where a
backslash escapes any character. Masking happens on the WHOLE command BEFORE the newline fold: a
fold is not neutral to the mask (a comment ends at a newline, a `;` does not).

Escapes per bash: outside any span a backslash escapes the next character; inside "..." only before
a backslash, double quote, backtick or $; inside '...' nothing escapes.
"""
import json
import re
import sys

# A `#` begins a comment only at the start of a word: at the start of the text, or after
# whitespace or one of bash's metacharacters. `a#b`, `$#` and `${#x}` are not comments.
_WORD_START = " \t\n;&|()<>"


def mask_quoted(t, quotes="'\"", keep="", fill=" ", live=False):
    """Length-preserving bash-aware mask of t.

    BOTH quote types are always SCANNED — that is what bash-awareness means: a span
    opened by " swallows any apostrophe inside it, and only the scanner that knows the
    double-quoted span is open can know the apostrophe is a letter. (A parser that only
    tracks the quote types it masks is the apostrophe defect in new clothes: the apostrophe in
    "don't" opens a fake single-quoted span again.) Comments and `$'...'` are scanned too
    (module docstring); `$'...'` masks when "'" is in `quotes`.

    `quotes` decides which span types are MASKED; content of a scanned-but-not-masked
    span passes through unchanged — that lets a caller keep the LIVE
    backticks of interpolating "..." visible while single-quoted backticks (data) mask
    away. Quote marks themselves always mask. `keep` names escaped characters (backslash
    + c, inside "...") that survive masking in place of the pair.

    `fill` is the character a masked quote mark, span character or escape pair becomes
    (comments always become spaces). A non-space fill keeps a quoted argument a WORD in
    the mask, so words found in the mask can be read back out of the raw text.

    `live=True` leaves the command substitutions inside a masked "..." span — `$(...)` and
    `` `...` `` — UNMASKED and in place, scanned by these same rules as the code they are:
    `until [ -z "$(pgrep -x X)" ]` masks to a gate whose pgrep still sits after its
    `until`. Default off, which is the behaviour every other site was written against.

    Escapes per bash: outside any span a backslash escapes the next character (the pair
    masks); inside "...", only before another backslash, a double quote, a backtick, or
    $; inside '...' nothing escapes; inside $'...' a backslash escapes anything.
    """
    out = []
    _scan(t, 0, len(t), out, None, quotes, keep, fill, live)
    return "".join(out)


def _scan(t, i, n, out, close, quotes, keep, fill, live):
    """Scan t[i:] in UNQUOTED context, appending to out, until `close` ends the context
    (")" for a $(...) body, "`" for a backtick body, None for the top level). Returns the
    index just past the closing character, which is appended as itself."""
    depth = 0
    while i < n:
        c = t[i]
        if close == ")" and c == ")" and depth == 0:
            out.append(c); return i + 1
        if close == "`" and c == "`":
            out.append(c); return i + 1
        if c == "#" and (i == 0 or t[i - 1] in _WORD_START):
            j = t.find("\n", i)
            j = n if j < 0 else j
            if close == "`":                      # a backtick ends the body even in a comment
                k = t.find("`", i, j)
                j = k if k >= 0 else j
            out.append(" " * (j - i)); i = j
            continue
        if c == "\\" and i + 1 < n:
            out.append(fill * 2); i += 2
            continue
        if c == "'" and i > 0 and t[i - 1] == "$" and not _escaped(t, i - 1):
            i = _span_ansi(t, i, n, out, quotes, fill)
            continue
        if c == "'":
            i = _span_single(t, i, n, out, quotes, fill)
            continue
        if c == '"':
            i = _span_double(t, i, n, out, quotes, keep, fill, live)
            continue
        if close == ")":
            depth += {"(": 1, ")": -1}.get(c, 0)
        out.append(c); i += 1
    return i


def _escaped(t, i):
    """True when t[i] is preceded by an odd run of backslashes."""
    k = i - 1
    while k >= 0 and t[k] == "\\":
        k -= 1
    return (i - 1 - k) % 2 == 1


def _span_single(t, i, n, out, quotes, fill):
    out.append(fill); i += 1                       # the opening mark
    while i < n:
        c = t[i]
        if c == "'":
            out.append(fill); return i + 1
        out.append(fill if "'" in quotes else c); i += 1
    return i


def _span_ansi(t, i, n, out, quotes, fill):
    out.append(fill); i += 1
    while i < n:
        c = t[i]
        if c == "\\" and i + 1 < n:
            out.append(fill * 2 if "'" in quotes else t[i:i + 2]); i += 2
            continue
        if c == "'":
            out.append(fill); return i + 1
        out.append(fill if "'" in quotes else c); i += 1
    return i


def _span_double(t, i, n, out, quotes, keep, fill, live):
    out.append(fill); i += 1
    masked = '"' in quotes
    while i < n:
        c = t[i]
        if c == "\\" and i + 1 < n and t[i + 1] in '\\"$`':
            nxt = t[i + 1]
            if nxt in keep:
                out.append(fill); out.append(nxt)
            else:
                out.append(fill * 2)
            i += 2
            continue
        if c == '"':
            out.append(fill); return i + 1
        if masked and live and c == "$" and t[i + 1:i + 2] == "(":
            out.append("$("); i = _scan(t, i + 2, n, out, ")", quotes, keep, fill, live)
            continue
        if masked and live and c == "`":
            out.append("`"); i = _scan(t, i + 1, n, out, "`", quotes, keep, fill, live)
            continue
        out.append(fill if masked else c); i += 1
    return i


def main():
    for line in sys.stdin:
        try:
            cmd = json.loads(line.rstrip("\n"))
        except ValueError:
            continue
        if isinstance(cmd, str):
            sys.stdout.write(re.sub(r"[\r\n]+", ";", mask_quoted(cmd)) + "\n")


if __name__ == "__main__":
    main()
