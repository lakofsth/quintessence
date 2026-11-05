# SPDX-License-Identifier: AGPL-3.0-or-later
"""Certification: `qq findings next` offers the bare `qq <verb> <head>` as a ready command for a
PROPOSED write only when the text channel the line names includes stdin; for every other gated
verb it offers no ready command and prints that verb's argv form instead.

The verbs are authgate.GATED_VERBS; the channel is read back off the PROPOSED line
queue_proposal wrote, not from a table kept here. `qq findings next` runs as the real `qq`
script in a scratch store, and a ready command is a `  qq ...` line under "to resolve:"."""
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
HEAD = "sec-topic"
_CHANNEL_RE = re.compile(r"\(text channel: ([a-z ]+?)\)")


def findings_next(verb: str) -> "tuple[list, str]":
    """Queue a `verb` proposal for HEAD, run `qq findings next`. Returns (channels the
    PROPOSED line names, stdout)."""
    with tempfile.TemporaryDirectory() as base:
        paths = {"QUINTESSENCE_DIR": os.path.join(base, "store"),
                 "QQ_MEMDIR": os.path.join(base, "mem"),
                 "QQ_STATE_DIR": os.path.join(base, "state"),
                 "QQ_KB_ROOT": os.path.join(base, "kb")}
        os.makedirs(paths["QUINTESSENCE_DIR"])
        subprocess.run(["git", "init", "-q", paths["QUINTESSENCE_DIR"]], check=True)
        store = Store(Config(env={}, config_file="/nonexistent", overrides=paths))
        authgate.queue_proposal(store, verb, HEAD, "proposed text", "m")
        with open(os.path.join(paths["QQ_STATE_DIR"], "pending-findings.md"),
                  encoding="utf-8") as fh:
            [line] = FindingsFile.parse(fh.read()).get_section(authgate.SECTION)
        m = _CHANNEL_RE.search(line)
        channels = m.group(1).split(" or ") if m else []
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env.update(paths, QQ_CONFIG=os.path.join(base, "config"), QQ_RECONCILE_REGISTRY="")
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        r = subprocess.run(QQ + ["findings", "next"], env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return channels, r.stdout


def ready_commands(out: str) -> list:
    tail = out.split("\nto resolve:\n", 1)
    assert len(tail) == 2, out
    return [ln.strip() for ln in tail[1].splitlines() if ln.startswith("  qq ")]


class BareRatifyReadyOnlyOnStdin(unittest.TestCase):
    def test_every_gated_verb(self):
        self.assertTrue({"update", "new", "rewrite", "essence"} <= set(authgate.GATED_VERBS))
        for verb in authgate.GATED_VERBS:
            with self.subTest(verb=verb):
                channels, out = findings_next(verb)
                self.assertTrue(channels, f"the {verb} line names no text channel:\n{out}")
                ready = ready_commands(out)
                if "stdin" in channels:
                    self.assertIn(f"qq {verb} {HEAD}", ready,
                                  f"{verb} reads stdin ({channels}) but its bare form is not "
                                  f"offered:\n{out}")
                else:
                    self.assertEqual(ready, [],
                                     f"{verb} names channel {channels}, no stdin, yet a ready "
                                     f"command is offered:\n{out}")
                    # the argv form: the verb and head followed by a text argument
                    resolve = out.split("\nto resolve:\n", 1)[1]
                    self.assertRegex(resolve, rf"qq {verb} {HEAD} \S",
                                     f"{verb}'s argv form is not printed:\n{out}")


if __name__ == "__main__":
    unittest.main()
