# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.xref: Config/SearchIndex plumbing (no ambient
environ reads), generic-memory-type skip + xref: track/skip overrides, wave-off round-trip and
its date/timestamp-precision dedupe, and the MAX-findings cap. The embedding-index-dependent
parts (pair_sims/candidates/findings) are exercised end-to-end via the LIVE parity check in the
build report (byte-identical against staleness-xref.py on the real store); here we unit-test the
pieces that don't require a real embedder."""
import io
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quintessence.config import Config
from quintessence.search import SearchIndex
from quintessence.xref import Xref


def make_xref(base: str, **overrides) -> Xref:
    over = {"QUINTESSENCE_DIR": os.path.join(base, "store"),
            "QQ_KB_ROOT": os.path.join(base, "kb"),
            "QQ_STATE_DIR": os.path.join(base, "state"),
            "QQ_CACHE": os.path.join(base, "cache", "embeddings.json")}
    over.update(overrides)
    cfg = Config(env={}, config_file="/nonexistent", overrides=over)
    idx = SearchIndex(cfg)
    return Xref(cfg, idx)


class TestConfigPlumbing(unittest.TestCase):
    def test_paths_and_thresholds_derive_from_config(self):
        with tempfile.TemporaryDirectory() as base:
            xr = make_xref(base, QQ_XREF_SIM="0.7", QQ_XREF_DAYS="5", QQ_XREF_MAX="2")
            self.assertEqual(xr.sim, 0.7)
            self.assertEqual(xr.days, 5)
            self.assertEqual(xr.max_findings, 2)
            self.assertEqual(xr.qdir, os.path.join(base, "store"))

    def test_memdir_derives_from_kb_root_not_qq_memdir(self):
        """PRESERVED judgment call (see quintessence/xref.py docstring): memdir tracks the
        SAME SearchIndex's kb_root/memory, not QQ_MEMDIR, even when the two differ."""
        with tempfile.TemporaryDirectory() as base:
            xr = make_xref(base, QQ_MEMDIR=os.path.join(base, "totally-different-memdir"))
            self.assertEqual(xr.memdir, os.path.join(base, "kb", "memory"))
            self.assertNotEqual(xr.memdir, os.path.join(base, "totally-different-memdir"))

    def test_generic_types_from_csv(self):
        with tempfile.TemporaryDirectory() as base:
            xr = make_xref(base, QQ_XREF_GENERIC_TYPES="feedback,style")
            self.assertEqual(xr.generic_types, {"feedback", "style"})


class TestIsGeneric(unittest.TestCase):
    def _write_mem(self, base, name, type_=None, xref=None):
        d = os.path.join(base, "kb", "memory")
        os.makedirs(d, exist_ok=True)
        lines = ["---", f"name: {name}"]
        if type_:
            lines.append(f"metadata:")
            lines.append(f"  type: {type_}")
        if xref:
            lines.append(f"xref: {xref}")
        lines += ["---", "body"]
        path = os.path.join(d, name + ".md")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def test_generic_type_is_skipped(self):
        with tempfile.TemporaryDirectory() as base:
            xr = make_xref(base)
            p = self._write_mem(base, "m1", type_="feedback")
            self.assertTrue(xr.is_generic(p))

    def test_non_generic_type_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as base:
            xr = make_xref(base)
            p = self._write_mem(base, "m2", type_="project")
            self.assertFalse(xr.is_generic(p))

    def test_xref_track_override_forces_inclusion(self):
        with tempfile.TemporaryDirectory() as base:
            xr = make_xref(base)
            p = self._write_mem(base, "m3", type_="feedback", xref="track")
            self.assertFalse(xr.is_generic(p))

    def test_xref_skip_override_forces_exclusion(self):
        with tempfile.TemporaryDirectory() as base:
            xr = make_xref(base)
            p = self._write_mem(base, "m4", type_="project", xref="skip")
            self.assertTrue(xr.is_generic(p))


class TestWaveoffRoundTrip(unittest.TestCase):
    def _write_head(self, base, topic, updated_iso):
        os.makedirs(os.path.join(base, "store"), exist_ok=True)
        path = os.path.join(base, "store", topic + ".md")
        with open(path, "w") as f:
            f.write(f"# {topic}\n> updated: {updated_iso}\n> essence: x\n")
        return path

    def _write_mem(self, base, name):
        d = os.path.join(base, "kb", "memory")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name + ".md"), "w") as f:
            f.write(f"---\nname: {name}\n---\nbody\n")

    def test_waveoff_persists_and_load_roundtrips(self):
        with tempfile.TemporaryDirectory() as base:
            xr = make_xref(base)
            self._write_head(base, "h1", "2026-01-01T00:00:00Z")
            self._write_mem(base, "m1")
            msg = xr.waveoff("m1", "h1")
            self.assertIn("waved off", msg)
            loaded = xr.load_waveoffs()
            self.assertIn(("m1", "h1"), loaded)

    def test_a_content_store_too_long_to_write_says_so_and_the_waveoff_still_lands(self):
        """Seventeenth pass, F1, swept one frame up. `save_content_store` carries no handler of
        its own — all three of its callers wrapped it in `except OSError: pass`, so the atomic
        write's name-length refusal was swallowed there instead, and `QQ_XREF_CONTENT` is a name
        an operator sets. A scan that looked only at the write's own lines called this clean.

        Both halves are asserted: the wave-off completes (the content store is a cache of hashes
        that rebuilds itself, so a failed save must not fail the command) and the refusal reaches
        stderr with the arithmetic."""
        import quintessence.atomicio as atomicio
        with tempfile.TemporaryDirectory() as base:
            state = os.path.join(base, "state")
            os.makedirs(state, exist_ok=True)
            limit = os.pathconf(state, "PC_NAME_MAX")
            edge = limit - atomicio._TEMP_NAME_OVERHEAD
            content = os.path.join(state, "x" * (edge + 1))

            xr = make_xref(base, QQ_XREF_CONTENT=content)
            self._write_head(base, "h1", "2026-01-01T00:00:00Z")
            self._write_mem(base, "m1")

            err = io.StringIO()
            with unittest.mock.patch.object(sys, "stderr", err):
                msg = xr.waveoff("m1", "h1")

            self.assertIn("waved off", msg, "the command must still complete")
            self.assertIn(("m1", "h1"), xr.load_waveoffs(), "and its own write must have landed")
            self.assertFalse(os.path.exists(content))
            line = err.getvalue()
            self.assertIn("xref content store", line,
                          f"the refusal must reach the operator: {line!r}")
            for number in ("17", str(edge + 1), str(limit), str(edge)):
                self.assertIn(number, line,
                              f"and carry the arithmetic — {number} missing from {line!r}")

    def test_waveoff_unknown_memory_raises(self):
        with tempfile.TemporaryDirectory() as base:
            xr = make_xref(base)
            self._write_head(base, "h1", "2026-01-01T00:00:00Z")
            with self.assertRaises(ValueError):
                xr.waveoff("no-such-memory", "h1")

    def test_waveoff_unknown_head_raises(self):
        with tempfile.TemporaryDirectory() as base:
            xr = make_xref(base)
            self._write_mem(base, "m1")
            with self.assertRaises(ValueError):
                xr.waveoff("m1", "no-such-head")


class TestStalenessXrefCliCleanFailure(unittest.TestCase):
    """recall-06: staleness-xref.py's default (no-args) branch used to let Xref.pair_sims()'s
    RuntimeError (index incomplete/empty) propagate as a raw Python traceback — even though
    `qq`'s own use of the identical Xref class (both `check` and `waveoff --head`) already
    degrades this exact failure to a clean one-line stderr message. A completely empty store
    (no mem/qq content at all) reaches "index empty for mem/qq" without any network call, so
    this stays within the hard safety rule against touching a live model endpoint."""

    def _run(self, base, *args):
        store = os.path.join(base, "store")
        os.makedirs(store, exist_ok=True)
        env = dict(os.environ)
        env.update({
            "QUINTESSENCE_DIR": store,
            "QQ_KB_ROOT": os.path.join(base, "kb"),
            "QQ_STATE_DIR": os.path.join(base, "state"),
            "QQ_MEMDIR": os.path.join(base, "mem"),
            "QQ_CACHE": os.path.join(base, "cache.json"),
            "QQ_CONFIG": os.path.join(base, "nonexistent-config"),
        })
        return subprocess.run([sys.executable, os.path.join(ENGINE, "staleness-xref.py"), *args],
                               capture_output=True, text=True, env=env, timeout=30)

    def test_empty_store_fails_clean_not_with_a_traceback(self):
        with tempfile.TemporaryDirectory() as base:
            p = self._run(base)
            self.assertNotEqual(p.returncode, 0)
            self.assertNotIn("Traceback", p.stderr)
            self.assertIn("staleness-xref.py:", p.stderr)

    def test_explain_on_empty_store_fails_clean_not_with_a_traceback(self):
        """This verification pass: recall-06 only wrapped the bare (no-args) branch — --explain
        transitively calls the SAME Xref.pair_sims() (via candidates()) and used to still crash
        with a raw traceback on the identical empty-store RuntimeError."""
        with tempfile.TemporaryDirectory() as base:
            p = self._run(base, "--explain")
            self.assertNotEqual(p.returncode, 0)
            self.assertNotIn("Traceback", p.stderr)
            self.assertIn("staleness-xref.py:", p.stderr)

    def test_waveoff_head_on_empty_store_fails_clean_not_with_a_traceback(self):
        """Same gap as --explain: --waveoff-head H calls waveoff_head_all() -> candidates() ->
        pair_sims() and used to crash raw instead of degrading like `qq waveoff --head` does."""
        with tempfile.TemporaryDirectory() as base:
            p = self._run(base, "--waveoff-head", "some-topic")
            self.assertNotEqual(p.returncode, 0)
            self.assertNotIn("Traceback", p.stderr)
            self.assertIn("staleness-xref.py:", p.stderr)


class TestFindingsCap(unittest.TestCase):
    def test_findings_stops_at_max(self):
        with tempfile.TemporaryDirectory() as base:
            xr = make_xref(base, QQ_XREF_MAX="2")
            # candidates() drives findings(); stub it directly to isolate the cap logic from
            # the embedder-dependent pipeline.
            rows = [(100 - i, 0.9, f"m{i}", "2026-01-01", f"h{i}", "2026-01-05", "FIRE")
                    for i in range(5)]
            xr.candidates = lambda: rows
            out = xr.findings()
            self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
