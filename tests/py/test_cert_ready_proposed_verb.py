# SPDX-License-Identifier: AGPL-3.0-or-later
"""Certification: every verb write._gate_divert can queue as a PROPOSED write is offered by
`qq findings next` as a ready command for a slug-safe head; the allowed set is
authgate.GATED_VERBS, not a hand-written list.

The verb set is derived here from the SOURCE of quintessence/write.py (every
`_gate_divert(store, "<verb>", ...)` call site), not from any list in the code under test."""
import ast
import inspect
import os
import subprocess
import tempfile
import unittest

from quintessence import authgate, cli
from quintessence import write as w
from quintessence.config import Config
from quintessence.findings import RATIFY_CHANNEL, FindingsFile
from quintessence.store import Store


def diverted_verbs() -> set:
    tree = ast.parse(inspect.getsource(w))
    out = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_gate_divert"
                and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)):
            out.add(n.args[1].value)
    return out


def _store(base):
    c = Config(env={}, config_file="/nonexistent",
               overrides={"QUINTESSENCE_DIR": os.path.join(base, "store"),
                          "QQ_MEMDIR": os.path.join(base, "mem"),
                          "QQ_STATE_DIR": os.path.join(base, "state")})
    os.makedirs(c.get_path("QUINTESSENCE_DIR"), exist_ok=True)
    subprocess.run(["git", "init", "-q", c.get_path("QUINTESSENCE_DIR")], check=True)
    return Store(c)


class ReadyProposedVerb(unittest.TestCase):
    def test_the_derivation_found_the_verbs(self):
        self.assertTrue({"update", "new", "rewrite", "essence"} <= diverted_verbs())

    def test_every_diverted_verb_is_offered_as_ready_command(self):
        for verb in sorted(diverted_verbs()):
            with self.subTest(verb=verb), tempfile.TemporaryDirectory() as base:
                store = _store(base)
                authgate.queue_proposal(store, verb, "sec-topic", "some text", "m")
                path = os.path.join(store.config.get_path("QQ_STATE_DIR"), "pending-findings.md")
                with open(path, encoding="utf-8") as fh:
                    ff = FindingsFile.parse(fh.read())
                out = cli.render_findings_next(store, store.config, ff)
                if "stdin" not in RATIFY_CHANNEL[verb]:
                    # the bare form cannot carry an argv-only verb's text: named with its argv
                    # text, not as ready
                    self.assertNotIn(f"\n  qq {verb} sec-topic\n", out)
                    self.assertIn(f"\n  run: qq {verb} sec-topic -- $'some text'\n", out)
                else:
                    self.assertIn(f"\n  qq {verb} sec-topic\n", out)

    def test_allowed_set_is_authgate_GATED_VERBS(self):
        self.assertTrue(hasattr(authgate, "GATED_VERBS"), "authgate.GATED_VERBS does not exist")
        self.assertEqual(set(authgate.GATED_VERBS), diverted_verbs())

    def test_unsafe_head_is_not_offered(self):
        with tempfile.TemporaryDirectory() as base:
            store = _store(base)
            authgate.queue_proposal(store, "update", "x; rm -rf y", "t", "m")
            path = os.path.join(store.config.get_path("QQ_STATE_DIR"), "pending-findings.md")
            with open(path, encoding="utf-8") as fh:
                ff = FindingsFile.parse(fh.read())
            out = cli.render_findings_next(store, store.config, ff)
            self.assertNotIn("\n  qq update x", out)


if __name__ == "__main__":
    unittest.main()
