# SPDX-License-Identifier: AGPL-3.0-or-later
"""INVARIANTS.md: a-ratification-never-drops-the-proposed-text.

Every command qq offers as ready for ratifying a proposal carries the proposal's text by the
channel that command reads; a command given its text only on a channel it does not read refuses
with a non-zero exit (stdin into `qq new` once it has arrived, see Limit), and argv text a
command does not read is refused whatever stdin carries, so a ratification never succeeds with
the proposed text dropped.

The verbs are authgate.GATED_VERBS. The channel each one reads is whatever the PROPOSED line
itself names, read back off a line queue_proposal wrote — not a list kept here. Every replay
runs the real `qq` script as a subprocess, so stdin is a real fd 0, in a scratch store."""
import os
import re
import subprocess
import sys
import tempfile
import unittest

from quintessence import authgate
from quintessence.config import Config
from quintessence.findings import FindingsFile
from quintessence.store import Store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
QQ = [sys.executable, os.path.join(REPO_ROOT, "qq")]
CHANNELS = ("argv", "stdin")
MARK = "ratify-marker-7f3a"
_CHANNEL_RE = re.compile(r"\(text channel: ([a-z ]+?)\)")


def _env(base: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env.update({"QUINTESSENCE_DIR": os.path.join(base, "store"),
                "QQ_CONFIG": os.path.join(base, "config"),
                "QQ_STATE_DIR": os.path.join(base, "state"),
                "QQ_MEMDIR": os.path.join(base, "mem"),
                "QQ_KB_ROOT": os.path.join(base, "kb"),
                "QQ_RECONCILE_REGISTRY": ""})
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    return env


def _scratch_store(base: str) -> dict:
    env = _env(base)
    d = env["QUINTESSENCE_DIR"]
    os.makedirs(d)
    subprocess.run(["git", "init", "-q", d], check=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
    return env


def named_channels(verb: str) -> list:
    """The channels the PROPOSED line queue_proposal writes for `verb` tells its ratifier to use."""
    with tempfile.TemporaryDirectory() as base:
        c = Config(env={}, config_file="/nonexistent",
                   overrides={"QUINTESSENCE_DIR": os.path.join(base, "store"),
                              "QQ_MEMDIR": os.path.join(base, "mem"),
                              "QQ_STATE_DIR": os.path.join(base, "state")})
        store = Store(c)
        authgate.queue_proposal(store, verb, "sec-topic", "x", "m")
        with open(os.path.join(c.get_path("QQ_STATE_DIR"), "pending-findings.md"),
                  encoding="utf-8") as fh:
            [line] = FindingsFile.parse(fh.read()).get_section(authgate.SECTION)
    m = _CHANNEL_RE.search(line)
    return m.group(1).split(" or ") if m else []


def replay(cmd: list, verb: str, channels) -> "tuple[int, set]":
    """Ratify a `verb` proposal for sec-topic by `cmd`, with a text on each of `channels` (one
    channel, or argv and stdin together, each carrying its own marker), in a scratch store.
    Returns (exit code, the channels whose text is in the HEAD afterwards)."""
    if isinstance(channels, str):
        channels = (channels,)
    with tempfile.TemporaryDirectory() as base:
        env = _scratch_store(base)
        head = os.path.join(env["QUINTESSENCE_DIR"], "sec-topic.md")
        if verb != "new":
            subprocess.run(cmd + ["new", "sec-topic", "old essence"], env=env, check=True,
                           stdin=subprocess.DEVNULL, capture_output=True)
        old = ""
        if verb == "rewrite":
            with open(head, encoding="utf-8") as fh:
                old = fh.read()

        def text(ch):
            return (old + f"\nproposed {MARK}-{ch}\n") if verb == "rewrite" else f"proposed {MARK}-{ch}"
        argv = cmd + [verb, "sec-topic"] + ([text("argv")] if "argv" in channels else [])
        kw = {"input": text("stdin")} if "stdin" in channels else {"stdin": subprocess.DEVNULL}
        p = subprocess.run(argv, env=env, capture_output=True, text=True, **kw)
        landed = set()
        if os.path.isfile(head):
            with open(head, encoding="utf-8") as fh:
                body = fh.read()
            landed = {ch for ch in channels if f"{MARK}-{ch}" in body}
        return p.returncode, landed


def drops_argv(rc: int, landed: set) -> bool:
    """Exit 0 with the argv text not in the HEAD. Argv is always addressed to the command, so a
    verb that does not read it must refuse it, whatever stdin carries. (Stdin beside argv text is
    not judged here: it may be inherited, as in a `while read` loop, and is not read.)"""
    return rc == 0 and "argv" not in landed


class RatificationKeepsTheText(unittest.TestCase):
    def test_every_proposed_line_names_a_channel(self):
        for verb in authgate.GATED_VERBS:
            with self.subTest(verb=verb):
                ch = named_channels(verb)
                self.assertTrue(ch, f"the PROPOSED line for {verb} names no text channel")
                self.assertTrue(set(ch) <= set(CHANNELS), ch)

    def test_every_named_channel_lands_the_text(self):
        for verb in authgate.GATED_VERBS:
            for channel in named_channels(verb):
                with self.subTest(verb=verb, channel=channel):
                    rc, landed = replay(QQ, verb, channel)
                    self.assertEqual(rc, 0)
                    self.assertIn(channel, landed, f"qq {verb} exited 0 with the text dropped")

    def test_no_channel_drops_the_text_silently(self):
        for verb in authgate.GATED_VERBS:
            for channel in CHANNELS:
                with self.subTest(verb=verb, channel=channel):
                    rc, landed = replay(QQ, verb, channel)
                    self.assertTrue(rc != 0 or channel in landed,
                                    f"qq {verb} with the text on {channel}: exit 0, text dropped")

    def test_planted_command_that_drops_stdin_is_caught(self):
        # Rule 3 control: a command that throws its stdin away and still succeeds. The checker
        # above must see it as exit 0 with the text dropped, or its silence proves nothing.
        dropper = ["sh", "-c", 'exec "$0" "$@" </dev/null'] + QQ
        rc, landed = replay(dropper, "new", "stdin")
        self.assertEqual(rc, 0)
        self.assertFalse(landed)


    def test_argv_text_is_never_dropped_whatever_stdin_carries(self):
        # Argv text alone, and argv text with data on stdin together: a verb either lands the
        # argv text or refuses. Exit 0 with the argv text gone is the drop (`qq rewrite <slug>
        # <text>` with stdin data took stdin and threw the argv away).
        for verb in authgate.GATED_VERBS:
            for channels in (("argv",), ("argv", "stdin")):
                with self.subTest(verb=verb, channels=channels):
                    rc, landed = replay(QQ, verb, channels)
                    self.assertFalse(drops_argv(rc, landed),
                                     f"qq {verb} with text on {'+'.join(channels)}: exit 0, "
                                     f"argv text dropped (landed: {sorted(landed)})")

    def test_planted_command_that_drops_argv_is_caught(self):
        # Rule 3 control for the test above: a command that throws away the argv text (keeps
        # only verb and slug) while stdin carries data, so `qq update` takes stdin and exits 0.
        # The same predicate must call that a drop, or its silence on the real verbs proves
        # nothing.
        dropper = ["sh", "-c", 'exec "$0" "$1" "$2" "$3"'] + QQ
        rc, landed = replay(dropper, "update", ("argv", "stdin"))
        self.assertEqual((rc, landed), (0, {"stdin"}))
        self.assertTrue(drops_argv(rc, landed))


PLACEHOLDER = "<one-line essence: what this thread is about>"


def new_with_slow_writer(delay: float) -> "tuple[int, str, str]":
    """`qq new slow-topic` with no argv essence, its stdin a pipe whose writer writes the text
    `delay` seconds after qq was started, in a scratch store. Returns (exit code, stderr, the
    HEAD's text or "" if none was made)."""
    import time
    with tempfile.TemporaryDirectory() as base:
        env = _scratch_store(base)
        p = subprocess.Popen(QQ + ["new", "slow-topic"], env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(delay)
        try:
            p.stdin.write(f"proposed {MARK}-slow\n".encode())
            p.stdin.close()
        except BrokenPipeError:
            pass
        p.stdout.read()
        err = p.stderr.read()
        p.wait(timeout=60)
        head = os.path.join(env["QUINTESSENCE_DIR"], "slow-topic.md")
        body = ""
        if os.path.isfile(head):
            with open(head, encoding="utf-8") as fh:
                body = fh.read()
        return p.returncode, err.decode(), body


class NewStdinGrace(unittest.TestCase):
    """The Limit line of a-ratification-never-drops-the-proposed-text: `qq new` refuses stdin
    data that has arrived, after a short grace (0.2 s, `_stdin_carries_data`) for a pipe whose
    writer has not written yet; a writer slower than that is not seen. `subprocess.run(input=)`
    writes before qq has started, so the tests above never reach the window; these do.

    The inside delay (0.15 s, timed from spawn) ends before the grace can, since the grace only
    starts after qq's own startup, so it is refused however loaded the host is. With the grace
    at 0.0 it is red whenever qq starts in under 0.15 s (~50 ms measured)."""

    def test_writer_inside_the_grace_is_refused(self):
        rc, err, body = new_with_slow_writer(0.15)
        self.assertEqual(rc, 2, f"a writer 0.15 s late was not refused; HEAD: {body!r}")
        self.assertEqual(body, "", "a refused qq new made a HEAD")
        self.assertIn("text on stdin is not read", err)

    def test_writer_outside_the_grace_is_not_seen(self):
        # The stated limit, pinned so a change to it is a decision rather than a drift: a writer
        # far slower than the grace is not seen, and the HEAD is made with the placeholder.
        rc, err, body = new_with_slow_writer(1.5)
        self.assertEqual(rc, 0, err)
        self.assertIn(PLACEHOLDER, body)
        self.assertNotIn(MARK, body)

    def test_refusal_names_dev_null_for_inherited_stdin(self):
        # `while read t; do qq new "$t"; done < topics` hands every qq new the loop's own input
        # on fd 0, so all but the last are refused. The remedy is one redirect, and the refusal
        # says so.
        with tempfile.TemporaryDirectory() as base:
            env = _scratch_store(base)
            p = subprocess.run(QQ + ["new", "loop-topic"], env=env, input="next-topic\n",
                               capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("</dev/null", p.stderr)


def offered_commands(verb: str) -> list:
    """The ready commands `qq findings next` prints for a queued PROPOSED `verb` write."""
    from quintessence import cli
    with tempfile.TemporaryDirectory() as base:
        c = Config(env={}, config_file="/nonexistent",
                   overrides={"QUINTESSENCE_DIR": os.path.join(base, "store"),
                              "QQ_MEMDIR": os.path.join(base, "mem"),
                              "QQ_STATE_DIR": os.path.join(base, "state")})
        store = Store(c)
        authgate.queue_proposal(store, verb, "sec-topic", "x", "m")
        with open(os.path.join(c.get_path("QQ_STATE_DIR"), "pending-findings.md"),
                  encoding="utf-8") as fh:
            ff = FindingsFile.parse(fh.read())
        out = cli.render_findings_next(store, c, ff)
    return [l.strip() for l in out.split("to resolve:\n", 1)[1].splitlines()
            if l.strip().startswith("qq ")]


class OfferedCommandIsRunAsPrinted(unittest.TestCase):
    def test_offered_ready_command_never_succeeds_with_the_text_dropped(self):
        # Run exactly what `qq findings next` offers, stdin empty: it must refuse (the text
        # was not given) or not be offered as ready at all.
        for verb in authgate.GATED_VERBS:
            for cmd in offered_commands(verb):
                with self.subTest(verb=verb, cmd=cmd):
                    with tempfile.TemporaryDirectory() as base:
                        env = _scratch_store(base)
                        if verb != "new":
                            subprocess.run(QQ + ["new", "sec-topic", "old essence"], env=env,
                                           check=True, stdin=subprocess.DEVNULL,
                                           capture_output=True)
                        argv = QQ + cmd.split()[1:]
                        p = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
                                           capture_output=True, text=True)
                        self.assertNotEqual(p.returncode, 0,
                                            f"offered `{cmd}` exits 0 with no proposed text")


if __name__ == "__main__":
    unittest.main()
