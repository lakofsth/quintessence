# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quote-mask.py -- bash-aware, length-preserving quote mask for `idle-autosave.sh`.

CLI: reads one JSON string per stdin line (a whole, possibly multi-line, shell command), masks the
quoted spans, heredoc bodies and comments of the command AS BASH LEXES IT, then folds every newline
run to `;` and prints one line per command. The caller greps that output for a command-position
verb, so a verb that lives inside a quoted argument, a heredoc body, a grep pattern or a comment is
data and does not match, while a real unquoted `echo x | qq update ...` survives. A command whose
scan comes out any length other than its input's is REFUSED: the CLI prints nothing for it, and the
caller's no-match errs toward waking once more rather than toward suppressing a wake.

Why not two regexes (strip '...' then "..."): an apostrophe inside a double-quoted span ("it's")
pairs with the next literal quote and deletes everything between -- including a REAL write in
command position. That is the fail-open direction for a gate that decides whether a checkpoint
already happened. Here inside "..." an apostrophe is a letter, inside '...' a double quote is.

LENGTH-PRESERVING: every quote mark and masked character becomes one fill character (an escape pair
becomes two), so an index into the mask is an index into the raw text. An UNBALANCED quote masks the
rest of the text as data. THE LENGTH HOLDS OR THE CALL RAISES (MaskMismatch): a heredoc body bounds
what is read inside it -- a substitution in an unquoted body that never closes ends at the body's
end, and a heredoc that substitution opens has no lines to read -- and a search that looks past that
bound yields a mask that is not the input's length, into which no offset can be trusted. Every
public function checks the length of what it scanned and raises when it differs; a caller that
cannot read a command refuses it rather than allow.

Modelled because bash lexes them: a `#` at the start of a word outside any quote begins a COMMENT to
end of line (an apostrophe in `# don't ...` must not open a span; the comment masks to spaces and
keeps its newline, so the next line is code again); `$'...'` ANSI-C quoting, where a backslash
escapes any character; and the HEREDOC BODY -- `<<` or `<<-` (never `<<<`) outside any quote,
followed by a delimiter word in any form bash accepts (`EOF`, `'EOF'`, `"EOF"`, `\\EOF`, `E"O"F`),
reads its body from the next unquoted newline to the first line that is exactly the terminator
(leading tabs stripped first for `<<-`), or to the end of text. A QUOTED-delimiter body reads as a
single-quoted span, an UNQUOTED one as a double-quoted span with no closing mark; nothing in a body
opens a quote or a comment. A masked body and its terminator line become SPACES and keep their
newlines, whatever `fill` is -- a body is not an argument word -- so the line after the terminator
is code again. `heredocs(t)` returns the spans.

STRUCTURE DOES NOT DEPEND ON WHAT IS SHOWN: every substitution (`$(`, a backtick, `<(`, `>(`) is
scanned as the code it is wherever it opens -- in unquoted code, in "..." and in an unquoted heredoc
body -- because a `"` inside the body of `git commit -m "$(cat <<'EOF' ... EOF)"` must not close the
outer span; `live`, `quotes` and `fill` decide only what the mask SHOWS of it. `substitutions(t)`
returns those spans. A `)`-closed body ends where bash ends it, a `case ... esac` in it included (a
pattern's `)` is a pattern's, not the body's close), and `$((` is arithmetic only where bash reads
it as arithmetic.

Escapes per bash: outside any span a backslash escapes the next character; inside "..." only before
a backslash, double quote, backtick or $; inside '...' nothing escapes.
"""
import json
import os
import re
import sys

# A `#` begins a comment only at the start of a word: at the start of the text, or after
# whitespace or one of bash's metacharacters. `a#b`, `$#` and `${#x}` are not comments.
_WORD_START = " \t\n;&|()<>"

# A word bash may read as a reserved word: no quote, escape or expansion in it. The reserved
# words after which the next word is still in command position.
_PLAIN_WORD = re.compile(r"[^\s;&|()<>'\"`$\\]+")
_KEEPS_CMDPOS = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "time"}


class MaskMismatch(ValueError):
    """The scan's output is not the input's length: no offset into it can be trusted. A caller
    that judges shell code refuses the call ("could not read this command") rather than allow it."""


def _run(t, quotes="'\"", keep="", fill=" ", live=True, code_bodies=(), cont=False):
    """Scan t whole; (mask, state). Raises MaskMismatch when the mask is not t's length."""
    out = []
    st = {"pending": [], "spans": [], "code": set(code_bodies)}
    if cont:
        st["cont"] = []
    _scan(t, 0, len(t), out, None, quotes, keep, fill, live, st)
    m = "".join(out)
    if len(m) != len(t):
        raise MaskMismatch("mask is %d characters, input %d" % (len(m), len(t)))
    return m, st


def mask_quoted(t, quotes="'\"", keep="", fill=" ", live=False, code_bodies=()):
    """Length-preserving bash-aware mask of t.

    BOTH quote types are always SCANNED — that is what bash-awareness means: a span
    opened by " swallows any apostrophe inside it, and only the scanner that knows the
    double-quoted span is open can know the apostrophe is a letter. (A parser that only
    tracks the quote types it masks is the apostrophe defect in new clothes: the apostrophe in
    "don't" opens a fake single-quoted span again.) Comments and `$'...'` are scanned too
    (module docstring); `$'...'` masks when "'" is in `quotes`.

    `quotes` decides which span types are MASKED; content of a scanned-but-not-masked
    span passes through unchanged (a substitution inside it is code, and is shown as scanned,
    its own quotes masked by this same rule). Quote marks themselves always mask. `keep` names
    escaped characters (backslash + c, inside "...") that survive masking in place of the pair;
    an escaped character is LITERAL to bash, so a caller asking "does the shell run this?" never
    wants it kept.

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

    Heredoc bodies are read as bash reads them (module docstring). `code_bodies` names the
    start offsets (heredocs(t)'s "body"[0]) of bodies to scan as CODE instead.

    Raises MaskMismatch when the mask is not t's length (module docstring).
    """
    return _run(t, quotes, keep, fill, live, code_bodies)[0]


def continuations(t, code_bodies=()):
    """The offsets of the backslash of every backslash-newline pair bash removes as a LINE
    CONTINUATION, in textual order: one in unquoted code (a `$(...)`, a backtick body and a
    heredoc body fed to a shell as code included) or inside "...". Bash deletes such a pair
    before it splits words or reads an operator, so `&\\<NL>&` is `&&`. A pair inside '...',
    `$'...'`, a comment or a data heredoc body is NOT one (the backslash is literal there, and a
    comment ends at the newline), and neither is a newline after an escaped backslash `\\\\`."""
    return _run(t, code_bodies=code_bodies, cont=True)[1]["cont"]


def heredocs(t):
    """The heredocs of t in textual order, as bash reads them: one dict each, with "op" (the
    offset of its `<<`), "delim" (the terminator, quotes removed), "quoted" (any quoting in
    the delimiter word: the body is literal), "strip" (`<<-`), "body" ((start, end) of the
    body) and "end" (the offset of the newline ending its terminator line, or len(t))."""
    return _run(t)[1]["spans"]


def substitutions(t, code_bodies=()):
    """The command and process substitutions of t, innermost last among those sharing a start:
    one dict each, with "kind" ("cmd" for `$(...)` and backticks, "proc" for `<(...)` and
    `>(...)`, "arith" for an unquoted arithmetic `((...))`), "open" (the offset of its `$`, `<`,
    `>` or opening backtick, or of the first `(` of an arithmetic pair) and "body" ((start, end)
    of the text between its delimiters; end is the offset of the closing character, or len(t)
    when it never closes). Read by the same scan as mask_quoted(live=True) with the same
    `code_bodies`, so an offset into that mask is an offset here."""
    return _run(t, code_bodies=code_bodies)[1].get("subs", [])


def _heredoc_word(t, k, n):
    """(end, terminator, quoted) of the heredoc delimiter word at t[k:], or None."""
    out, quoted, j = [], False, k
    while j < n and t[j] not in " \t\n;&|()<>":
        c = t[j]
        if c == "\\" and j + 1 < n:
            out.append(t[j + 1]); quoted = True; j += 2
            continue
        if c in "'\"":
            e = t.find(c, j + 1, n)
            e = n if e < 0 else e
            out.append(t[j + 1:e]); quoted = True; j = e + 1
            continue
        out.append(c); j += 1
    if j == k:
        return None
    return j, "".join(out), quoted


def _bodies(t, i, n, out, quotes, keep, fill, live, st):
    """Read the pending heredoc bodies, the first starting at t[i] (just past the unquoted
    newline that ended their command line). Returns the index after the last one."""
    pending, st["pending"] = st["pending"], []
    for hd in pending:
        if i in st["code"]:
            return i                       # a body fed to a shell: scanned on, as code
        a = i
        while i < n:
            e = t.find("\n", i, n)
            e = n if e < 0 else e
            line = t[i:e]
            if (line.lstrip("\t") if hd["strip"] else line) == hd["delim"]:
                break
            i = e + 1 if e < n else n
        b = i
        end = t.find("\n", b, n)          # bounded: inside a body, the body is all there is
        end = n if end < 0 else end
        hd.update(body=(a, b), end=end)
        st["spans"].append(hd)
        masked = ("'" in quotes) if hd["quoted"] else ('"' in quotes)
        if hd["quoted"]:
            out.append(_blank(t[a:end]) if masked else t[a:end])
        else:                              # its substitutions are read whatever is shown
            buf = []
            _body_live(t, a, b, buf, quotes, keep, fill, live, st)
            if not masked:
                out.append(t[a:end])
            else:
                out.extend(buf if live else [_blank(t[a:b])])
                out.append(_blank(t[b:end]))
        i = end
        if i < n:                          # the newline that ends the terminator line
            out.append("\n"); i += 1
    return i


def _blank(s):
    return "".join("\n" if ch == "\n" else " " for ch in s)


def _body_live(t, i, n, out, quotes, keep, fill, live, st):
    """An unquoted heredoc body: spaces, but its substitutions stay as code (the caller blanks
    them too unless live=True; they are SCANNED either way, so what follows is read the same)."""
    while i < n:
        c = t[i]
        if c == "\\" and i + 1 < n:
            out.append(_blank(t[i:i + 2])); i += 2
            continue
        if _opens_sub(t, i):
            i = _sub(t, i, n, out, quotes, keep, fill, live, st)
            continue
        out.append("\n" if c == "\n" else " "); i += 1


def _opens_sub(t, i):
    """True when t[i] opens a command or process substitution: `$(` (and a `$((` bash does not
    read as arithmetic, see _arith), `<(`, `>(` (a `<((` too: bash has no arithmetic there), or
    a backtick."""
    c = t[i]
    if c == "`":
        return True
    if c in "<>":
        return t[i + 1:i + 2] == "("
    return c == "$" and t[i + 1:i + 2] == "(" and not _arith(t, i + 1)


def _arith(t, i):
    """True when the `((` at t[i] is arithmetic as bash reads it: bash reads on from the second
    `(` to the `)` that balances it, and only when the very next character is `)` is the whole
    `((...))` arithmetic. Otherwise `$((` is `$(` with a body that opens with a subshell, and a
    bare `((` two nested subshells: `$(( cmd ) )`, `$((cmd) )` and `$((a); (b))` all run
    commands. A `((` whose second paren never closes stays arithmetic (bash refuses the text; the
    mask is the same either way)."""
    if t[i + 1:i + 2] != "(":
        return False
    e = _arith_walk(t, i + 2, len(t), [], "'\"", "", " ", False,
                    {"pending": [], "spans": [], "code": set()})
    return e >= len(t) or t[e + 1:e + 2] == ")"


def _arith_walk(t, j, n, out, quotes, keep, fill, live, st):
    """Walk t[j:] (just past a `((`) as bash reads it, appending to out, to the `)` that balances
    the second `(`; returns its offset (not appended), or n when there is none. Quotes are read as
    quotes (`'it)s'` holds no paren) and every substitution is scanned and recorded: the one walk
    both _arith's decision and the arithmetic span's end come from, so they cannot differ."""
    depth = 0
    while j < n:
        c = t[j]
        if c == "\\" and j + 1 < n:
            out.append(t[j:j + 2]); j += 2
            continue
        if _opens_sub(t, j):
            j = _sub(t, j, n, out, quotes, keep, fill, live, st)
            continue
        if c == "'":
            j = _span_single(t, j, n, out, quotes, fill)
            continue
        if c == '"':
            j = _span_double(t, j, n, out, quotes, keep, fill, live, st)
            continue
        if c == ")" and depth == 0:
            return j
        depth += {"(": 1, ")": -1}.get(c, 0)
        out.append(c); j += 1
    return n


def _sub(t, i, n, out, quotes, keep, fill, live, st):
    """Scan the substitution opening at t[i] as the code it is, record it, append it as scanned.
    Returns the index just past its closing character."""
    k = i + (1 if t[i] == "`" else 2)
    out.append(t[i:k])
    rec = {"kind": "proc" if t[i] in "<>" else "cmd", "open": i}
    st.setdefault("subs", []).append(rec)
    j = _scan(t, k, n, out, "`" if t[i] == "`" else ")", quotes, keep, fill, live, st)
    closed = j <= n and j > k and t[j - 1] == ("`" if t[i] == "`" else ")")
    rec["body"] = (k, j - 1 if closed else j)
    return j


def _case_step(t, i, k, c, depth, cs, cmdpos, out):
    """One step of the `case ... esac` reader of a `)`-closed body (see _scan). cs is the stack of
    open cases, each [state, depth it opened at, at a pattern's start]; state is "subj" (before
    the subject word), "in" (before `in`), "pat" (reading patterns) or "body" (an arm's commands).
    Returns (next index, cmdpos) when it consumed t[i], else (None, cmdpos)."""
    top = cs[-1] if cs and cs[-1][1] == depth else None
    if top and top[0] == "pat":
        if c == ")":                              # a pattern's `)`: the arm's commands follow
            top[0] = "body"; out.append(c); return i + 1, True
        if c == "(" and top[2]:                   # the optional `(` of `(x)`: no depth
            top[2] = False; out.append(c); return i + 1, cmdpos
    if top and top[0] == "body" and c == ";" and t[i + 1:i + 2] in (";", "&"):
        e = i + (3 if t.startswith(";;&", i) else 2)
        top[0], top[2] = "pat", True             # `;;`, `;&`, `;;&`: the next arm's patterns
        out.append(t[i:e]); return e, True
    if c in " \t\n;&|()<>" or not (i == k or t[i - 1] in _WORD_START):
        return None, cmdpos
    if c == "#":                                  # a comment: _scan skips it
        return None, cmdpos
    m = _PLAIN_WORD.match(t, i)
    w = m.group(0) if m and (m.end() == len(t) or t[m.end()] in " \t\n;&|()<>") else ""
    if top and top[0] == "subj":                  # the subject, in any form
        top[0] = "in"
        return None, False
    if top and top[0] == "in":
        if w != "in":
            return None, False
        top[0], top[2] = "pat", True
    elif top and top[0] == "pat":                 # a pattern word, or the `esac` of no more arms
        if not (w == "esac" and top[2]):
            top[2] = False
            return None, False
        cs.pop()
    elif cmdpos and w == "case":
        cs.append(["subj", depth, False])
    elif cmdpos and w == "esac" and top:
        cs.pop()
    elif not (cmdpos and w in _KEEPS_CMDPOS):
        return None, False
    out.append(w)
    return i + len(w), w in _KEEPS_CMDPOS


def _scan(t, i, n, out, close, quotes, keep, fill, live, st):
    """Scan t[i:] in UNQUOTED context, appending to out, until `close` ends the context
    (")" for a $(...) body, "`" for a backtick body, None for the top level). Returns the
    index just past the closing character, which is appended as itself.

    A `)`-closed body ends where bash ends it, a `case ... esac` in it included: `case` read as
    a word in command position opens one, and until its `esac` a pattern's `)` (`x)`, `(x)`,
    `a|x)`) is a pattern's, not the body's close."""
    depth, k, cs, cmdpos = 0, i, [], True
    while i < n:
        c = t[i]
        if close == ")":
            j, cmdpos = _case_step(t, i, k, c, depth, cs, cmdpos, out)
            if j is not None:
                i = j
                continue
        if close == ")" and c == ")" and depth == 0:
            out.append(c); return i + 1
        if close == "`" and c == "`":
            out.append(c); return i + 1
        if c == "#" and (i == 0 or t[i - 1] in _WORD_START):
            j = t.find("\n", i, n)
            j = n if j < 0 else j
            if close == "`":                      # a backtick ends the body even in a comment
                k = t.find("`", i, j)
                j = k if k >= 0 else j
            out.append(" " * (j - i)); i = j
            continue
        if c == "\\" and i + 1 < n:
            if t[i + 1] == "\n":
                st.setdefault("cont", []).append(i)
            out.append(fill * 2); i += 2
            continue
        if c == "\n" and st["pending"]:
            out.append(c)
            i = _bodies(t, i + 1, n, out, quotes, keep, fill, live, st)
            continue
        if c == "(" and _arith(t, i):           # arithmetic: `$((1<<2))` shifts, opens no
            rec = {"kind": "arith", "open": i}  # heredoc; a `$(...)` or backtick in it is still
            st.setdefault("subs", []).append(rec)  # a substitution, scanned and recorded as one
            out.append("((")
            e = _arith_walk(t, i + 2, n, out, quotes, keep, fill, live, st)
            rec["body"] = (i + 2, e)
            if e < n:                           # its `))`
                out.append(t[e:min(e + 2, n)])
            i = min(e + 2, n)
            continue
        if c == "<" and t.startswith("<<", i) and not t.startswith("<<<", i) \
                and not (i > 0 and t[i - 1] == "<"):
            k = i + 2 + (t[i + 2:i + 3] == "-")
            while k < n and t[k] in " \t":
                k += 1
            w = _heredoc_word(t, k, n)
            if w:
                st["pending"].append({"op": i, "delim": w[1], "quoted": w[2],
                                      "strip": t[i + 2:i + 3] == "-"})
                out.append(t[i:k]); i = k       # the word itself is lexed as the shell lexes it
                continue
        if _opens_sub(t, i):
            i = _sub(t, i, n, out, quotes, keep, fill, live, st)
            continue
        if c == "'" and i > 0 and t[i - 1] == "$" and not _escaped(t, i - 1):
            i = _span_ansi(t, i, n, out, quotes, fill)
            continue
        if c == "'":
            i = _span_single(t, i, n, out, quotes, fill)
            continue
        if c == '"':
            i = _span_double(t, i, n, out, quotes, keep, fill, live, st)
            continue
        if close == ")":
            depth += {"(": 1, ")": -1}.get(c, 0)
            if c in ";&|(\n":                     # a separator: a command word may follow
                cmdpos = True
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


def _span_double(t, i, n, out, quotes, keep, fill, live, st):
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
        if c == "\\" and t[i + 1:i + 2] == "\n":       # a continuation: bash removes the pair
            st.setdefault("cont", []).append(i)
            out.append(fill * 2 if masked else t[i:i + 2]); i += 2
            continue
        if c == '"':
            out.append(fill); return i + 1
        if c in "$`" and _opens_sub(t, i):          # code inside "...": read whatever is shown
            buf = []
            i2 = _sub(t, i, n, buf, quotes, keep, fill, live, st)
            out.extend(buf if (live or not masked) else [fill * (i2 - i)])
            i = i2
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
            try:
                masked = mask_quoted(cmd)
            except MaskMismatch:
                # The scan is not the input's length: no offset into it can be trusted, so this
                # command is refused — nothing is printed for it, and the caller's no-match errs
                # toward waking once more rather than toward suppressing a wake.
                continue
            sys.stdout.write(re.sub(r"[\r\n]+", ";", masked) + "\n")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    from quintessence import pipeexit
    pipeexit.run(main)
