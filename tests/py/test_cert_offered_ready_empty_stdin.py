# SPDX-License-Identifier: AGPL-3.0-or-later
"""Running each command `qq findings next` offers for a queued PROPOSED write of a gated verb,
exactly as printed with empty stdin, exits non-zero (or no bare command is offered).

Every gated verb (authgate.GATED_VERBS) is queued with queue_proposal, in a scratch store, with the
topic present and absent; the offered `qq <verb> ...` lines are run as printed with stdin at
/dev/null and again with stdin closed-empty (a pipe), and none may exit 0."""
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

from quintessence import authgate
from quintessence.config import Config
from quintessence.store import Store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
QQ = [sys.executable, os.path.join(REPO_ROOT, "qq")]


def _env(base):
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
    return env


def offered(env):
    p = subprocess.run(QQ + ["findings", "next"], env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    out = p.stdout.split("to resolve:", 1)[-1]
    return [l.strip() for l in out.splitlines() if l.strip().startswith("qq ")]


class OfferedReadyCommandNeverPlaceholders(unittest.TestCase):
    def _run(self, verb, head_exists, stdin_kind):
        with tempfile.TemporaryDirectory() as base:
            env = _env(base)
            if head_exists:
                subprocess.run(QQ + ["new", "sec-topic", "old essence"], env=env, check=True,
                               stdin=subprocess.DEVNULL, capture_output=True)
            c = Config(env={}, config_file="/nonexistent",
                       overrides={"QUINTESSENCE_DIR": env["QUINTESSENCE_DIR"],
                                  "QQ_MEMDIR": env["QQ_MEMDIR"],
                                  "QQ_STATE_DIR": env["QQ_STATE_DIR"]})
            authgate.queue_proposal(Store(c), verb, "sec-topic", "proposed text", "m")
            cmds = offered(env)
            for cmd in cmds:
                argv = shlex.split(cmd)
                self.assertEqual(argv[0], "qq")
                if stdin_kind == "devnull":
                    kw = {"stdin": subprocess.DEVNULL}
                else:
                    kw = {"input": ""}
                p = subprocess.run(QQ + argv[1:], env=env, capture_output=True, text=True, **kw)
                self.assertNotEqual(
                    p.returncode, 0,
                    f"offered `{cmd}` (proposal verb {verb}) run as printed with empty stdin "
                    f"exited 0; stdout={p.stdout!r} stderr={p.stderr!r}")

    def test_offered_commands_refuse_empty_stdin(self):
        for verb in authgate.GATED_VERBS:
            for exists in (False, True):
                for kind in ("devnull", "pipe"):
                    with self.subTest(verb=verb, head_exists=exists, stdin=kind):
                        self._run(verb, exists, kind)


if __name__ == "__main__":
    unittest.main()
