# SPDX-License-Identifier: AGPL-3.0-or-later
"""Certification: a-printed-argv-ratification-passes-the-text-whole.

The argv form `qq findings next` prints for a PROPOSED write (its `run:` line), run by bash
exactly as printed, passes the proposed text to the verb as one argv element equal to the text,
so the verb lands it or refuses with a non-zero exit.

Directions, each from the sentence:
  - the printed line must carry THE text, not a placeholder standing for it;
  - bash must not split, expand, substitute or unquote any of it: spaces, quotes of both kinds,
    `$`, `$(...)`, backticks, backslashes, globs, `;`, `#`, `~`, a leading `-`, tabs, newlines,
    non-ASCII — each case is one text, and argv must hold it as ONE element, byte-equal;
  - run through the real `qq`, the verb either lands the text in the HEAD or exits non-zero.

Every `run:` line printed for any gated verb is held to this; a `run:` line is required for
the plain text of `qq new`, whose text has no other channel (argv only). The command is the
rest of the line after `run: `, less a closing `)` that only closes a `(` opened before `run:`.
"""
import json
import re
import os
import stat
import subprocess
import sys
import tempfile
import unittest

from quintessence import authgate, cli
from quintessence.config import Config
from quintessence.findings import FindingsFile
from quintessence.store import Store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HEAD = "sec-topic"
PLAIN = "plain proposed text"
TEXTS = [
    PLAIN,
    "it's a single quote",
    "two '' quotes and a trailing '",
    'double "quoted" text',
    "dollar $HOME and ${PATH} and $1",
    "command $(touch PWNED-subst) substitution",
    "backtick `touch PWNED-tick` substitution",
    "back\\slash and trailing \\",
    "glob * ? [ab] here",
    "semi; echo injected && false || true",
    "# looks like a comment",
    "~ tilde and ~root",
    "-leading dash",
    "--long-option-shaped",
    "tab\there",
    "line one\nline two",
    "bang ! and !! history",
    "non-ascii – ü ✓",
    "  surrounding spaces  ",
    "'",
]

# `run: ` as a word of its own: "re-run: qq check --write" is a different instruction.
_RUN_RE = re.compile(r"(?<![\w-])run: ")
_ARGV_DUMP = (f"#!{sys.executable}\n"
              "import json, os, sys\n"
              "with open(os.environ['ARGV_DUMP'], 'w', encoding='utf-8') as fh:\n"
              "    json.dump(sys.argv[1:], fh)\n")


def _config(base):
    return Config(env={}, config_file="/nonexistent",
                  overrides={"QUINTESSENCE_DIR": os.path.join(base, "store"),
                             "QQ_MEMDIR": os.path.join(base, "mem"),
                             "QQ_STATE_DIR": os.path.join(base, "state"),
                             "QQ_KB_ROOT": os.path.join(base, "kb")})


def findings_next(verb, text):
    """`qq findings next` for one queued `verb` proposal of `text` on HEAD."""
    with tempfile.TemporaryDirectory() as base:
        c = _config(base)
        os.makedirs(c.get_path("QUINTESSENCE_DIR"))
        subprocess.run(["git", "init", "-q", c.get_path("QUINTESSENCE_DIR")], check=True)
        store = Store(c)
        authgate.queue_proposal(store, verb, HEAD, text, "m")
        with open(os.path.join(c.get_path("QQ_STATE_DIR"), "pending-findings.md"),
                  encoding="utf-8") as fh:
            ff = FindingsFile.parse(fh.read())
        return cli.render_findings_next(store, c, ff)


def run_lines(out):
    """The commands of every `run:` line in a findings-next output, as printed."""
    cmds = []
    for ln in out.split("\n"):
        m = _RUN_RE.search(ln)
        if not m:
            continue
        before, cmd = ln[:m.start()], ln[m.end():]
        if before.count("(") > before.count(")") and cmd.endswith(")"):
            cmd = cmd[:-1]
        cmds.append(cmd)
    return cmds


def bash_argv(cmd):
    """Run `cmd` by bash, `qq` a stub that records its argv. Returns (argv list or None, rc)."""
    with tempfile.TemporaryDirectory() as d:
        stub = os.path.join(d, "bin")
        os.makedirs(stub)
        qq = os.path.join(stub, "qq")
        with open(qq, "w", encoding="utf-8") as fh:
            fh.write(_ARGV_DUMP)
        os.chmod(qq, os.stat(qq).st_mode | stat.S_IXUSR)
        dump = os.path.join(d, "argv.json")
        env = dict(os.environ, PATH=stub + os.pathsep + os.environ.get("PATH", ""),
                   ARGV_DUMP=dump, HOME=d)
        p = subprocess.run(["bash", "-c", cmd], cwd=d, env=env, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL)
        side_effects = [n for n in os.listdir(d) if n.startswith("PWNED")]
        argv = None
        if os.path.isfile(dump):
            with open(dump, encoding="utf-8") as fh:
                argv = json.load(fh)
        return argv, p.returncode, side_effects, p.stderr


def real_run(verb, cmd):
    """Run `cmd` by bash with `qq` the real script, in a scratch store. Returns (rc, HEAD body)."""
    with tempfile.TemporaryDirectory() as base:
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env.update({"QUINTESSENCE_DIR": os.path.join(base, "store"),
                    "QQ_CONFIG": os.path.join(base, "config"),
                    "QQ_STATE_DIR": os.path.join(base, "state"),
                    "QQ_MEMDIR": os.path.join(base, "mem"),
                    "QQ_KB_ROOT": os.path.join(base, "kb"),
                    "QQ_RECONCILE_REGISTRY": ""})
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        d = env["QUINTESSENCE_DIR"]
        os.makedirs(d)
        subprocess.run(["git", "init", "-q", d], check=True)
        subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
        stub = os.path.join(base, "bin")
        os.makedirs(stub)
        with open(os.path.join(stub, "qq"), "w", encoding="utf-8") as fh:
            fh.write(f'#!/bin/sh\nexec "{sys.executable}" "{os.path.join(REPO_ROOT, "qq")}" "$@"\n')
        os.chmod(os.path.join(stub, "qq"), 0o755)
        env["PATH"] = stub + os.pathsep + env.get("PATH", "")
        if verb != "new":
            subprocess.run(["qq", "new", HEAD, "old essence"], env=env, check=True,
                           stdin=subprocess.DEVNULL, capture_output=True)
        p = subprocess.run(["bash", "-c", cmd], cwd=base, env=env, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL)
        body = ""
        path = os.path.join(d, f"{HEAD}.md")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
        return p.returncode, body


def text_whole(argv, verb, text):
    """argv is `<verb> <head> ... <text>` with the text as exactly one element, equal to it."""
    return (isinstance(argv, list) and argv[:2] == [verb, HEAD]
            and [a for a in argv[2:] if a != "--"] == [text])


class PrintedArgvRatification(unittest.TestCase):
    def test_new_prints_a_run_line(self):
        self.assertTrue(run_lines(findings_next("new", PLAIN)),
                        "qq findings next printed no run: line for a PROPOSED `qq new`")

    def test_every_run_line_passes_the_text_as_one_argv_element(self):
        for verb in sorted(authgate.GATED_VERBS):
            for text in TEXTS:
                for cmd in run_lines(findings_next(verb, text)):
                    with self.subTest(verb=verb, text=text, cmd=cmd):
                        argv, rc, side, err = bash_argv(cmd)
                        self.assertEqual(side, [], f"bash ran a substitution: {cmd!r}")
                        self.assertTrue(text_whole(argv, verb, text),
                                        f"printed: {cmd!r}\nbash passed argv: {argv!r} "
                                        f"(rc {rc}, stderr {err.strip()!r})\n"
                                        f"proposed text: {text!r}")

    def test_every_run_line_lands_the_text_or_refuses(self):
        for verb in sorted(authgate.GATED_VERBS):
            for text in TEXTS:
                for cmd in run_lines(findings_next(verb, text)):
                    with self.subTest(verb=verb, text=text, cmd=cmd):
                        rc, body = real_run(verb, cmd)
                        self.assertTrue(rc != 0 or text in body,
                                        f"printed: {cmd!r}\nexit 0 and the HEAD does not hold "
                                        f"{text!r}:\n{body}")

    def test_planted_naive_forms_are_caught(self):
        # Rule 3 control: the predicate must call a placeholder and a double-quoted form wrong,
        # or its silence on the real output proves nothing.
        text = "it's $(touch PWNED-x)"
        for cmd in (f"qq new {HEAD} '<the proposed text, decoded>'",
                    f'qq new {HEAD} "{text}"', f"qq new {HEAD} {text}"):
            with self.subTest(cmd=cmd):
                argv, _, side, _ = bash_argv(cmd)
                self.assertFalse(text_whole(argv, "new", text) and not side)
        import shlex
        argv, _, side, _ = bash_argv(f"qq new {HEAD} {shlex.quote(text)}")
        self.assertTrue(text_whole(argv, "new", text) and not side)
        self.assertEqual(run_lines("  (no ready command: x; run: qq new h 'a')\n"
                                   "  or: edit it, then re-run: qq check --write"),
                         ["qq new h 'a'"])


if __name__ == "__main__":
    unittest.main()
