# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.reconcile: Config plumbing (no ambient environ
reads), the no-registry no-op, a stale-token flag in a HEAD essence, the live-revert guard, and
the deploy-hook change-detection snapshot half. See tests/test-config-reconcile.sh for the
fuller bash-level behavioural suite this mirrors at unit granularity; both are green and were
diffed byte-for-byte against the live store's config-reconcile.py run (see the build report)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence.config import Config
from quintessence.reconcile import Reconcile


def make_reconcile(base: str, **overrides) -> Reconcile:
    over = {"QUINTESSENCE_DIR": os.path.join(base, "store"),
            "QQ_MEMDIR": os.path.join(base, "mem"),
            "QQ_STATE_DIR": os.path.join(base, "state"),
            "QQ_DOCDIR": os.path.join(base, "docs"),
            "QQ_RECONCILE_SNAPSHOT": os.path.join(base, "state", "snap.json")}
    over.update(overrides)
    cfg = Config(env={}, config_file="/nonexistent", overrides=over)
    return Reconcile(cfg)


class TestConfigPlumbing(unittest.TestCase):
    def test_paths_derive_from_config(self):
        with tempfile.TemporaryDirectory() as base:
            rec = make_reconcile(base)
            self.assertEqual(rec.qdir, os.path.join(base, "store"))
            self.assertEqual(rec.docdir, os.path.join(base, "docs"))

    def test_no_registry_is_a_noop(self):
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "store"))
            rec = make_reconcile(base, QQ_RECONCILE_REGISTRY="")
            self.assertEqual(rec.run(), [])


class TestStaleTokenDetection(unittest.TestCase):
    def _setup(self, base):
        os.makedirs(os.path.join(base, "store"), exist_ok=True)
        os.makedirs(os.path.join(base, "mem"), exist_ok=True)
        os.makedirs(os.path.join(base, "docs"), exist_ok=True)
        with open(os.path.join(base, "live.env"), "w") as f:
            f.write("GATE_MODEL=new-model-y\n")
        with open(os.path.join(base, "reg.ini"), "w") as f:
            f.write(f"[gate-model]\nsource = envfile {base}/live.env GATE_MODEL\n"
                    f"current = new-model-y\nstale = old-model-x\n")

    def test_flags_stale_essence(self):
        with tempfile.TemporaryDirectory() as base:
            self._setup(base)
            with open(os.path.join(base, "store", "h1.md"), "w") as f:
                f.write("# h1\n> essence: the gate runs on old-model-x today\n")
            rec = make_reconcile(base, QQ_RECONCILE_REGISTRY=os.path.join(base, "reg.ini"))
            out = rec.run()
            self.assertTrue(any("HEAD h1 (essence) asserts a stale gate-model" in ln for ln in out))

    def test_does_not_scan_head_body(self):
        with tempfile.TemporaryDirectory() as base:
            self._setup(base)
            with open(os.path.join(base, "store", "h2.md"), "w") as f:
                f.write("# h2\n> essence: the gate runs on new-model-y\n\n"
                        "## body\nhistorically it was old-model-x\n")
            rec = make_reconcile(base, QQ_RECONCILE_REGISTRY=os.path.join(base, "reg.ini"))
            out = rec.run()
            self.assertFalse(any("h2" in ln for ln in out))

    def test_live_revert_guard(self):
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "store"), exist_ok=True)
            os.makedirs(os.path.join(base, "mem"), exist_ok=True)
            os.makedirs(os.path.join(base, "docs"), exist_ok=True)
            with open(os.path.join(base, "live2.env"), "w") as f:
                f.write("V=tok-old\n")
            with open(os.path.join(base, "reg2.ini"), "w") as f:
                f.write(f"[k2]\nsource = envfile {base}/live2.env V\nstale = tok-old\n")
            with open(os.path.join(base, "store", "h3.md"), "w") as f:
                f.write("# h3\n> essence: still on tok-old\n")
            rec = make_reconcile(base, QQ_RECONCILE_REGISTRY=os.path.join(base, "reg2.ini"))
            out = rec.run()
            self.assertEqual(out, [])


class TestChangeDetectionSnapshot(unittest.TestCase):
    def test_change_detected_and_committed(self):
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "store"), exist_ok=True)
            os.makedirs(os.path.join(base, "mem"), exist_ok=True)
            os.makedirs(os.path.join(base, "docs"), exist_ok=True)
            os.makedirs(os.path.join(base, "state"), exist_ok=True)
            with open(os.path.join(base, "cd.env"), "w") as f:
                f.write("M=alpha\n")
            with open(os.path.join(base, "cdreg.ini"), "w") as f:
                f.write(f"[k]\nsource = envfile {base}/cd.env M\n")
            rec = make_reconcile(base, QQ_RECONCILE_REGISTRY=os.path.join(base, "cdreg.ini"))

            first = rec.run(commit=True)
            self.assertFalse(any("CHANGED" in ln for ln in first), "first run should baseline, not fire")

            with open(os.path.join(base, "cd.env"), "w") as f:
                f.write("M=beta\n")
            uncommitted = rec.run()
            self.assertTrue(any("k CHANGED since last check: 'alpha' -> 'beta'" in ln for ln in uncommitted))

            still_uncommitted = rec.run()
            self.assertTrue(any("CHANGED" in ln for ln in still_uncommitted),
                             "a read-only run must not consume the detected change")

            rec.run(commit=True)
            after_commit = rec.run()
            self.assertFalse(any("CHANGED" in ln for ln in after_commit),
                              "--commit-snapshot must acknowledge the change")


if __name__ == "__main__":
    unittest.main()
