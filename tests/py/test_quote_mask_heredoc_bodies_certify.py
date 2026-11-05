# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Certify: quote-mask.py masks a command the way bash lexes it, heredoc bodies included.

The sentence, in full: quote-mask.py masks a command the way bash lexes it, heredoc bodies
included — a qq write-verb inside a body never reaches command position in the mask and a quote
inside a body never masks code after the terminator — and a scan whose output is not its input's
length raises MaskMismatch, the CLI printing nothing for such a command so the caller reads no
match and errs toward waking once more rather than toward suppressing a wake.

Written by a certifier from the sentence alone. "Reaches command position" is the caller's own
test, transcribed from `idle-autosave.sh`: over the mask with its newline runs folded to `;`,
`(^|[|;&])[[:space:]]*(sudo[[:space:]]+)?qq[[:space:]]+(update|finalize|checkpoint|rewrite|new)`
anchored at a line start or a metacharacter.

The directions of the sentence, one family of cases each:

  A — a qq write-verb inside a heredoc body never reaches command position in the mask. Body is
    data for the command, never command text: quoted, unquoted and double-quoted delimiters, the
    tab-stripping `<<-` form, a second body after a first terminator, a body after a quoted `<<`
    that is not a redirection, a body carrying a metacharacter-prefixed verb, and the text after
    a line that merely starts with the delimiter (a terminator is a bare line, so that text is
    still body).
  B — a quote inside a body never masks code after the terminator. An apostrophe or a double
    quote in the body is a letter of the body; the real unquoted `qq <write-verb>` after the
    terminator must survive in the mask.
  C — a scan whose output is not its input's length raises MaskMismatch, and the CLI prints
    nothing for such a command.
  S — supporting cases the sentence also demands and the pre-fix mask already gives: the fix must
    not over-mask (a real write before the heredoc, a `#` comment body, a herestring, a quoted
    `<<`, a comment naming a heredoc — the real write after each stays visible).

Every mask is additionally checked length-preserving, which the sentence's third clause rests on.
"""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import unittest

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MASKER = os.path.join(ENGINE, "quote-mask.py")


def _load():
    spec = importlib.util.spec_from_file_location("quote_mask_under_test", MASKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The caller's command-position grep, transcribed from idle-autosave.sh (WV there).
_CMDPOS = re.compile(
    r"(^|[|;&])[ \t]*(?:sudo[ \t]+)?qq[ \t]+"
    r"(?:update|finalize|checkpoint|rewrite|new)(?:[ \t]|$)")


def _folded_mask(module, cmd):
    return re.sub(r"[\r\n]+", ";", module.mask_quoted(cmd))


def _reaches_command_position(module, cmd):
    return bool(_CMDPOS.search(_folded_mask(module, cmd)))


# --- A: a write-verb inside a body never reaches command position in the mask ---

BODY_CASES = [
    ("quoted delimiter", "cat <<'EOF'\nqq update inside\nEOF\n"),
    ("tab-stripping <<-", "cat <<-HEREDOC\n\tqq update tabbed\n\tHEREDOC\n"),
    ("unquoted delimiter", "cat <<EOF\nqq update unquoted-delim\nEOF\n"),
    ("double-quoted delimiter", 'cat <<"EOF"\nqq update dq-delim\nEOF\n'),
    ("second body after a first terminator",
     "cat <<'A'\nline\nA <<'B'\nqq update second body\nB\n"),
    ("body after a quoted << that is not a redirection",
     "grep '<<' f <<'EOF'\nqq update after quoted ltlt\nEOF\n"),
    ("body carrying a metacharacter-prefixed verb",
     "printf '%s\\n' x <<'EOF'\ntrue; qq update piped body\nEOF\n"),
    # A terminator is a line holding nothing but the delimiter (measured against bash: with no
    # bare delimiter line the body runs to end of input). Text on a line that merely STARTS with
    # the delimiter is body, never command text — this pins the terminator rule itself rather
    # than a line-prefix match.
    ("text after a line merely starting with the delimiter is still body",
     "cat <<'EOF'\ndon't\nEOF; qq update same line\n"),
]

# --- B: a quote inside a body never masks code after the terminator ---

TERMINATOR_CASES = [
    ("apostrophe in body, real write after", "cat <<'EOF'\ndon't\nEOF\nqq update real\n"),
    ("double quote in body, real write after", 'cat <<EOF\nsay "hi\nEOF\nqq update real\n'),
    ("apostrophe in body, real finalize after", "cat <<'EOF'\ndon't\nEOF\nqq finalize real\n"),
    ("apostrophe in body, real checkpoint after a semicolon",
     "cat <<'EOF'\ndon't\nEOF\ntrue; qq checkpoint real"),
    ("apostrophe in a tab-stripped body, real write after",
     "cat <<-'EOF'\n\tdon't\n\tEOF\nqq update real\n"),
    ("quote in the first of two bodies, real write after both",
     "cat <<'A' <<'B'\ndon't\nA\nplain\nB\nqq update real\n"),
]

# --- S: supporting cases — the fix must not over-mask these real writes ---

OVERMASK_CASES = [
    ("real write before the heredoc", "qq update real <<'EOF'\ndon't\nEOF\n"),
    ("hash-prefixed apostrophe inside a body", "cat <<'EOF'\n# don't\nEOF\nqq update real\n"),
    ("a herestring is not a heredoc", "true <<< 'x' ; qq update real\n"),
    ("a quoted << is not a redirection", 'echo "<<EOF"\nqq update real\n'),
    ("a comment naming a heredoc start", "# cat <<EOF\nqq update real\n"),
]


class TestWriteVerbInsideABodyNeverReachesCommandPosition(unittest.TestCase):
    def setUp(self):
        self.qm = _load()

    def test_mask_quoted(self):
        for name, cmd in BODY_CASES:
            with self.subTest(case=name):
                self.assertFalse(_reaches_command_position(self.qm, cmd),
                                 "a write-verb inside a heredoc body reached command position "
                                 "in the mask: " + repr(_folded_mask(self.qm, cmd)))

    def test_cli(self):
        lines = "".join(json.dumps(c) + "\n" for _, c in BODY_CASES)
        p = subprocess.run([sys.executable, MASKER], input=lines,
                           capture_output=True, text=True)
        out = p.stdout.splitlines()
        self.assertEqual(len(out), len(BODY_CASES),
                         f"one output line per command expected:\n{p.stdout!r}\n{p.stderr}")
        for (name, _), line in zip(BODY_CASES, out):
            with self.subTest(case=name):
                self.assertFalse(_CMDPOS.search(line),
                                 "a write-verb inside a heredoc body reached command position "
                                 "in the CLI output: " + repr(line))


class TestQuoteInsideABodyNeverMasksCodeAfterTheTerminator(unittest.TestCase):
    def setUp(self):
        self.qm = _load()

    def test_mask_quoted(self):
        for name, cmd in TERMINATOR_CASES:
            with self.subTest(case=name):
                self.assertTrue(_reaches_command_position(self.qm, cmd),
                                "a quote inside the body masked the real write after the "
                                "terminator: " + repr(_folded_mask(self.qm, cmd)))

    def test_cli(self):
        lines = "".join(json.dumps(c) + "\n" for _, c in TERMINATOR_CASES)
        p = subprocess.run([sys.executable, MASKER], input=lines,
                           capture_output=True, text=True)
        out = p.stdout.splitlines()
        self.assertEqual(len(out), len(TERMINATOR_CASES),
                         f"one output line per command expected:\n{p.stdout!r}\n{p.stderr}")
        for (name, _), line in zip(TERMINATOR_CASES, out):
            with self.subTest(case=name):
                self.assertTrue(_CMDPOS.search(line),
                                "a quote inside the body masked the real write after the "
                                "terminator: " + repr(line))


class TestRealWritesSurviveHeredocAwareness(unittest.TestCase):
    def setUp(self):
        self.qm = _load()

    def test_mask_quoted(self):
        for name, cmd in OVERMASK_CASES:
            with self.subTest(case=name):
                self.assertTrue(_reaches_command_position(self.qm, cmd),
                                "the mask hid a real command-position write: "
                                + repr(_folded_mask(self.qm, cmd)))


class TestScanIsLengthPreserving(unittest.TestCase):
    """Every mask is its input's length — the property the MaskMismatch guard rests on."""

    NASTY = [
        "cat <<'EOF'\ndon't\nEOF\n",
        "cat <<EOF\n\"unbalanced\nEOF\nqq update x",
        "cat <<-A\n\ttabbed \\\n\tA",
        "echo \"it's\" ; qq update x",
        "echo 'a\\\\b' `date` \"$(x)\"",
        "$'don\\'t'",
        "x='unterminated",
        "qq update \"multi\nline\"",
        "cat <<'EOF'\nhéllo wörld 'quote' éè\nEOF\nqq update real",
        "cat <<'A' <<'B'\ndon't\nA\nplain\nB\nqq update real\n",
        "a#b $# ${#x} # don't\nqq update real",
    ]

    def setUp(self):
        self.qm = _load()

    def test_length_preserved(self):
        for t in self.NASTY:
            with self.subTest(text=repr(t)):
                self.assertEqual(len(self.qm.mask_quoted(t)), len(t))


class TestMismatchedScanRaisesAndTheCliSaysNothing(unittest.TestCase):
    def setUp(self):
        self.qm = _load()

    def tearDown(self):
        sys.stdin, sys.stdout = sys.__stdin__, sys.__stdout__

    def test_mask_mismatch_is_an_exception_on_the_module(self):
        self.assertTrue(hasattr(self.qm, "MaskMismatch"),
                        "quote-mask.py defines no MaskMismatch — the sentence names it as the "
                        "error a scan whose output is not its input's length raises")
        self.assertTrue(issubclass(self.qm.MaskMismatch, Exception),
                        "MaskMismatch is not an exception class")

    def test_cli_prints_nothing_for_a_command_whose_scan_raises(self):
        exc = getattr(self.qm, "MaskMismatch", None)
        self.assertIsNotNone(exc, "quote-mask.py defines no MaskMismatch to raise")
        self.assertTrue(callable(getattr(self.qm, "mask_quoted", None)),
                        "fixture: the module exposes no mask_quoted for the CLI to call")
        real = self.qm.mask_quoted

        def broken(*args, **kwargs):
            raise exc("certify: forced scan whose output is not its input's length")

        sys.stdin = io.StringIO(json.dumps("qq update x") + "\n")
        sys.stdout = io.StringIO()
        self.qm.mask_quoted = broken
        try:
            self.qm.main()
        finally:
            self.qm.mask_quoted = real
        self.assertEqual(sys.stdout.getvalue(), "",
                         "the CLI printed a line for a command whose scan raised MaskMismatch — "
                         "the caller would read that as a match")

    def test_a_scan_that_changes_length_raises_mask_mismatch(self):
        exc = getattr(self.qm, "MaskMismatch", None)
        self.assertIsNotNone(exc, "quote-mask.py defines no MaskMismatch to raise")
        inner = getattr(self.qm, "_scan", None)
        if inner is None:
            self.skipTest("the module no longer exposes _scan — the seam this case drives")
        real = self.qm._scan

        def short_scan(t, i, n, out, *rest):
            out.append("x")  # a scan whose output is not its input's length
            return n

        self.qm._scan = short_scan
        try:
            with self.assertRaises(exc):
                self.qm.mask_quoted("abc")
        finally:
            self.qm._scan = real


if __name__ == "__main__":
    unittest.main()
