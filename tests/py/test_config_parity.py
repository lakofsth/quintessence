# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Config-file PARITY test: qq-config.sh's `_qq_load_config` documents
"must match qqsearch_core._load_config_file byte-for-byte" — this test adds a THIRD, python-
package implementation (quintessence.config.Config._parse_dotenv) to that same invariant and
locks it with adversarial fixtures (quotes, CRLF, whitespace, malformed keys, values containing
'=', comments, duplicate keys) fed to a real bash probe (sourcing the actual qq-config.sh) and
compared against the python parser, byte-for-byte, per key.

This is a LIVE comparison against qq-config.sh (a subprocess `bash -c '. qq-config.sh; ...'`),
not a hand-transcribed oracle — if a future edit to qq-config.sh changes parsing behaviour,
this test starts failing against the CURRENT bash, which is the point.
"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence.config import Config

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
QQ_CONFIG_SH = os.path.join(ENGINE, "qq-config.sh")

UNSET_SENTINEL = "@@QQ-PARITY-UNSET@@"


def bash_resolved(config_text: str, keys: list[str]) -> dict[str, str]:
    """Write `config_text` to a temp file, source qq-config.sh against it with a CLEAN
    environment for `keys` (so the resolved value reflects the FILE parse, not env override —
    the layer this test is pinning), and return {key: resolved-or-UNSET_SENTINEL}."""
    fd, path = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "w", newline="") as f:
            f.write(config_text)
        script_lines = [f'unset {" ".join(keys)} 2>/dev/null || true',
                         f'QQ_CONFIG="{path}"',
                         f'. "{QQ_CONFIG_SH}"']
        for k in keys:
            # print a sentinel if unset (mirrors python's None), else the literal value
            script_lines.append(f'if [ -z "${{{k}+x}}" ]; then printf "%s\\x01" "{UNSET_SENTINEL}"; '
                                 f'else printf "%s\\x01" "${k}"; fi')
        out = subprocess.run(["bash", "-c", "\n".join(script_lines)],
                              capture_output=True, text=True, timeout=10, env={"HOME": os.environ.get("HOME", "/tmp")})
        parts = out.stdout.split("\x01")
        return dict(zip(keys, parts))
    finally:
        os.unlink(path)


def python_resolved(config_text: str, keys: list[str]) -> dict[str, str]:
    fd, path = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "w", newline="") as f:
            f.write(config_text)
        cfg = Config(env={}, config_file=path)
        out = {}
        for k in keys:
            v = cfg.resolve_raw(k)
            out[k] = UNSET_SENTINEL if v is None else v
        return out
    finally:
        os.unlink(path)


class TestConfigParity(unittest.TestCase):
    def assert_parity(self, config_text: str, keys: list[str]):
        bash_vals = bash_resolved(config_text, keys)
        py_vals = python_resolved(config_text, keys)
        for k in keys:
            self.assertEqual(py_vals[k], bash_vals[k],
                              f"parity mismatch for {k!r} given config:\n{config_text!r}\n"
                              f"bash={bash_vals[k]!r} python={py_vals[k]!r}")

    def test_plain_values(self):
        self.assert_parity("QQ_KB_ROOT=/a/b/c\nQQ_EMBED_MODEL=some-model\n",
                            ["QQ_KB_ROOT", "QQ_EMBED_MODEL"])

    def test_double_quoted_value(self):
        self.assert_parity('QQ_KB_ROOT="/a/b c"\n', ["QQ_KB_ROOT"])

    def test_single_quoted_value(self):
        self.assert_parity("QQ_KB_ROOT='/a/b c'\n", ["QQ_KB_ROOT"])

    def test_mismatched_quotes_not_stripped(self):
        # opening " but closing ' -> first/last char rule requires them to MATCH; if they
        # don't, no stripping happens on either side (both parsers must agree on this).
        self.assert_parity("QQ_KB_ROOT=\"/a/b'\n", ["QQ_KB_ROOT"])

    def test_crlf_line_endings(self):
        self.assert_parity("QQ_KB_ROOT=/a/b\r\nQQ_EMBED_MODEL=foo\r\n", ["QQ_KB_ROOT", "QQ_EMBED_MODEL"])

    def test_surrounding_whitespace_on_key_and_value(self):
        self.assert_parity("   QQ_KB_ROOT   =   /a/b   \n", ["QQ_KB_ROOT"])

    def test_blank_lines_and_comments_ignored(self):
        self.assert_parity("\n# a comment\n   \nQQ_KB_ROOT=/a/b\n# QQ_EMBED_MODEL=should-not-load\n",
                            ["QQ_KB_ROOT", "QQ_EMBED_MODEL"])

    def test_malformed_key_leading_digit_skipped(self):
        # "9BAD" isn't a valid shell identifier, so we can't even query it as a bash var; the
        # meaningful assertion is that the malformed line doesn't break parsing of what follows.
        self.assert_parity("9BAD=oops\nQQ_KB_ROOT=/a/b\n", ["QQ_KB_ROOT"])

    def test_malformed_key_invalid_chars_skipped(self):
        self.assert_parity("QQ.BAD-KEY=oops\nQQ_KB_ROOT=/a/b\n", ["QQ_KB_ROOT"])

    def test_line_without_equals_skipped(self):
        self.assert_parity("just some prose with no equals sign\nQQ_KB_ROOT=/a/b\n", ["QQ_KB_ROOT"])

    def test_value_containing_equals_sign(self):
        self.assert_parity("QQ_KB_ROOT=/a/b=c=d\n", ["QQ_KB_ROOT"])

    def test_duplicate_key_first_occurrence_wins(self):
        self.assert_parity("QQ_KB_ROOT=first\nQQ_KB_ROOT=second\n", ["QQ_KB_ROOT"])

    def test_unset_key_is_unset_on_both_sides(self):
        self.assert_parity("QQ_KB_ROOT=/a/b\n", ["QQ_EMBED_MODEL"])

    def test_no_newline_at_eof(self):
        self.assert_parity("QQ_KB_ROOT=/a/b", ["QQ_KB_ROOT"])

    def test_empty_value(self):
        self.assert_parity("QQ_KB_ROOT=\n", ["QQ_KB_ROOT"])

    def test_arbitrary_non_qq_identifier_key_still_loads(self):
        # both bash and python originals parse ANY valid-identifier key, not just QQ_*
        self.assert_parity("XDG_STATE_HOME=/custom/state\n", ["XDG_STATE_HOME"])


if __name__ == "__main__":
    unittest.main()
