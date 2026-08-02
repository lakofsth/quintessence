# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Cross-language lock interop: bash's findings.sh (findings_set_section, sourced by
consistency-audit.sh/fidelity-audit.sh) and python's persist_findings (quintessence.checks,
`qq check --write`) both mutate $QQ_STATE_DIR/pending-findings.md. The lock only holds if
EVERY writer takes the SAME one — this proves the two engines actually contend on the SAME
lockfile ($QQ_STATE_DIR/.state.lock), in BOTH directions:
  1. python holds state_lock()  -> a concurrent bash findings_set_section blocks, then times out
     LOUDLY (bounded wait + stderr, never a silent/unbounded hang) and leaves the file untouched;
     it succeeds immediately once python releases.
  2. bash holds the lock (via `flock`, the same mechanism qq-lib.sh's qq_lock_acquire uses on
     `.qq.lock`) -> a concurrent python persist_findings() blocks, then raises LockTimeout; it
     succeeds once bash releases.
Not a test of findings_set_section's own read-modify-write logic (test_findings.py covers that)
or of state_lock's own acquire/block/timeout mechanics (test_store.py covers that) — this is
ONLY the cross-language mutual-exclusion property.
"""
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ENGINE)

from quintessence.checks import persist_findings
from quintessence.config import Config
from quintessence.store import LockTimeout, Store, state_lock

FINDINGS_SH = os.path.join(ENGINE, "findings.sh")


def make_store(base: str, **overrides) -> Store:
    cfg = Config(env={}, config_file="/nonexistent", overrides={
        "QUINTESSENCE_DIR": os.path.join(base, "store"),
        "QQ_MEMDIR": os.path.join(base, "mem"),
        "QQ_STATE_DIR": os.path.join(base, "state"),
        **overrides,
    })
    return Store(cfg)


def run_bash_write(state_dir: str, tag: str, line: str, wait: float) -> subprocess.CompletedProcess:
    """Shell out to a bash process that sources findings.sh fresh and calls
    findings_set_section TAG once, with QQ_LOCK_WAIT set BEFORE the source (findings.sh reads
    its default at source time, matching how a real audit script would set it via env)."""
    script = (f'source "{FINDINGS_SH}"; echo {shlex.quote(line)} | '
              f'findings_set_section {tag}; echo "rc=$?"')
    env = dict(os.environ)
    env["QQ_STATE_DIR"] = state_dir
    env["QQ_LOCK_WAIT"] = str(wait)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)


class TestPythonHoldsBashContends(unittest.TestCase):
    def test_bash_blocks_then_times_out_loudly_while_python_holds(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            got_lock = threading.Event()
            release = threading.Event()

            def holder():
                with state_lock(store, wait=5):
                    got_lock.set()
                    release.wait(5)

            t = threading.Thread(target=holder)
            t.start()
            self.assertTrue(got_lock.wait(2), "python holder never acquired state_lock")
            try:
                start = time.monotonic()
                result = run_bash_write(str(store.state_dir), "AUDIT", "probe-1", wait=1)
                elapsed = time.monotonic() - start
            finally:
                release.set()
                t.join(5)

            # bounded: didn't hang past ~QQ_LOCK_WAIT, and didn't return instantly either (it
            # genuinely contended on OUR lock, not raced past it)
            self.assertGreaterEqual(elapsed, 0.9)
            self.assertLess(elapsed, 10)
            self.assertIn("timed out", result.stderr)
            self.assertIn("state lock", result.stderr)
            self.assertIn("rc=1", result.stdout)
            findings_path = os.path.join(str(store.state_dir), "pending-findings.md")
            self.assertFalse(os.path.exists(findings_path),
                              "a timed-out bash write must leave the file untouched, not partial")

    def test_bash_succeeds_immediately_after_python_releases(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            with state_lock(store, wait=5):
                pass   # acquire + release before bash ever tries
            result = run_bash_write(str(store.state_dir), "AUDIT", "probe-2", wait=5)
            self.assertIn("rc=0", result.stdout)
            findings_path = os.path.join(str(store.state_dir), "pending-findings.md")
            with open(findings_path) as f:
                content = f.read()
            self.assertIn("probe-2", content)


class TestBashHoldsPythonContends(unittest.TestCase):
    def _spawn_bash_holder(self, state_dir: str, hold_seconds: float):
        """A bash subprocess that opens fd 8 on $QQ_STATE_DIR/.state.lock and flocks it for
        `hold_seconds` — the SAME lockfile+fd findings.sh's findings_with_state_lock uses,
        constructed directly here (not via findings_set_section) so we control the hold
        duration precisely instead of racing a real write's short critical section."""
        lock_path = os.path.join(state_dir, ".state.lock")
        os.makedirs(state_dir, exist_ok=True)
        script = (f'exec 8>"{lock_path}"; flock -x 8; echo HELD; sleep {hold_seconds}')
        proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.stdout.close)
        line = proc.stdout.readline()
        self.assertEqual(line.strip(), "HELD", "bash holder never reported acquiring the lock")
        return proc

    def test_persist_findings_blocks_then_times_out_while_bash_holds(self):
        # persist_findings takes no `wait` kwarg (its real call site in `qq` never passes one —
        # it always uses the configured QQ_LOCK_WAIT); to keep this test fast we configure the
        # STORE's QQ_LOCK_WAIT low instead, which state_lock(store) — the exact call
        # persist_findings makes internally — reads via `store.config.get_int("QQ_LOCK_WAIT")`.
        with tempfile.TemporaryDirectory() as base:
            # QQ_LOCK_WAIT is a registered "int" key (whole seconds) -- 1s is the shortest
            # realistic bound; the bash holder holds for 3s so the timeout is unambiguous.
            store = make_store(base, QQ_LOCK_WAIT=1)
            proc = self._spawn_bash_holder(str(store.state_dir), hold_seconds=3)
            try:
                start = time.monotonic()
                with self.assertRaises(LockTimeout):
                    persist_findings(store, ["T1 something"])
                elapsed = time.monotonic() - start
            finally:
                proc.wait(5)
            self.assertGreaterEqual(elapsed, 0.8)
            findings_path = os.path.join(str(store.state_dir), "pending-findings.md")
            self.assertFalse(os.path.exists(findings_path),
                              "a timed-out persist_findings must leave the file untouched")

    def test_persist_findings_succeeds_after_bash_releases(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            proc = self._spawn_bash_holder(str(store.state_dir), hold_seconds=1)
            start = time.monotonic()
            persist_findings(store, ["T1 something"])
            elapsed = time.monotonic() - start
            self.assertGreaterEqual(elapsed, 0.5, "persist_findings must have waited for bash")
            findings_path = os.path.join(str(store.state_dir), "pending-findings.md")
            with open(findings_path) as f:
                content = f.read()
            self.assertIn("T1 something", content)
            proc.wait(5)


if __name__ == "__main__":
    unittest.main()
