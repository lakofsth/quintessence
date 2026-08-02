# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.procprobe (the B4 proc-vs-disk probe):
systemctl-output parsing (argv path tokens incl. --flag=/path deps, unit file + drop-ins,
repeated ExecStart lines, unix + wall-clock timestamp forms), the mtime>start decision (regular
files only — directory argv tokens are noise), the exclusion globs, the one-finding-per-unit
rule, and the PROC-section write's auto-resolve semantics. systemctl is FAKED throughout
(_run patched) so the suite is deterministic on any host; the live flag/clear loop was
verified against a real throwaway user unit at review time."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence import procprobe as pp
from quintessence.config import Config
from quintessence.findings import FindingsFile, parse_line
from quintessence.store import Store


def make_config(base: str, **overrides) -> Config:
    over = {"QUINTESSENCE_DIR": os.path.join(base, "store"),
            "QQ_MEMDIR": os.path.join(base, "mem"),
            "QQ_STATE_DIR": os.path.join(base, "state")}
    over.update(overrides)
    return Config(env={}, config_file="/nonexistent", overrides=over)


def exec_block(*argv: str) -> str:
    return ("{ path=" + argv[0] + " ; argv[]=" + " ".join(argv) +
            " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; "
            "code=(null) ; status=0/0 }")


class FakeSystemctl:
    """Dict-driven stand-in for procprobe._run."""

    def __init__(self, units: dict, user_units: "dict | None" = None):
        self.by_scope = {"system": units, "user": user_units or {}}

    def __call__(self, argv, timeout=0):
        scope = "user" if "--user" in argv else "system"
        units = self.by_scope[scope]
        if "list-units" in argv:
            return "".join(f"{u} loaded active running desc\n" for u in units)
        if "show" in argv:
            unit = argv[-1]
            props = units.get(unit)
            if props is None:
                return None
            return "".join(f"{k}={v}\n" for k, v in props)
    # (any other argv shape is unused by the module)


class ProbeHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.script = os.path.join(self.tmp, "svc.py")
        with open(self.script, "w") as fh:
            fh.write("print('v1')\n")
        self.unitfile = os.path.join(self.tmp, "svc.service")
        with open(self.unitfile, "w") as fh:
            fh.write("[Service]\n")
        self.mtime = int(os.stat(self.script).st_mtime)

    def props(self, started: int, extra_argv: "list | None" = None):
        argv = ["/usr/bin/python3", self.script] + (extra_argv or [])
        return [("ExecMainStartTimestamp", f"@{started}"),
                ("ExecStart", exec_block(*argv)),
                ("FragmentPath", self.unitfile),
                ("DropInPaths", "")]

    def run_probe(self, units, config=None, user_units=None, scopes=("system",)):
        config = config or make_config(self.tmp)
        with mock.patch.object(pp, "_run", FakeSystemctl(units, user_units)):
            return pp.probe(config, scopes=scopes)


class TestParsing(ProbeHarness):
    def test_dep_paths_argv_unitfile_dropins_and_flag_fed(self):
        props = {"ExecStart": [exec_block("/usr/bin/python3", "/srv/app.py",
                                            "--config=/etc/app.conf", "-v", "relative.txt")],
                 "ExecStartPre": [exec_block("/bin/mkdir", "-p", "/run/app")],
                 "FragmentPath": ["/etc/systemd/system/app.service"],
                 "DropInPaths": ["/etc/systemd/system/app.service.d/override.conf"]}
        self.assertEqual(pp.dep_paths(props), [
            "/usr/bin/python3", "/srv/app.py", "/etc/app.conf",
            "/bin/mkdir", "/run/app",
            "/etc/systemd/system/app.service",
            "/etc/systemd/system/app.service.d/override.conf"])

    def test_start_epoch_unix_and_wallclock(self):
        self.assertEqual(pp.start_epoch({"ExecMainStartTimestamp": ["@1783109467"]}),
                          1783109467)
        import time
        wall = pp.start_epoch({"ExecMainStartTimestamp": ["Fri 2026-07-03 23:11:07 EEST"]})
        self.assertEqual(wall, int(time.mktime(time.strptime(
            "2026-07-03 23:11:07", "%Y-%m-%d %H:%M:%S"))))
        self.assertIsNone(pp.start_epoch({"ExecMainStartTimestamp": ["n/a"]}))
        self.assertIsNone(pp.start_epoch({}))


class TestDecision(ProbeHarness):
    def test_script_newer_than_start_flags(self):
        units = {"svc.service": self.props(started=self.mtime - 100)}
        findings = self.run_probe(units)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.cls, "PROC drift")
        self.assertEqual(f.fields["unit"], "svc.service")
        self.assertEqual(f.fields["scope"], "system")
        # /usr/bin/python3 predates the fixture start; the SCRIPT is the offender named
        self.assertEqual(f.fields["path"], self.script)

    def test_script_older_than_start_clean(self):
        units = {"svc.service": self.props(started=self.mtime + 100)}
        self.assertEqual(self.run_probe(units), [])

    def test_same_second_not_flagged(self):
        units = {"svc.service": self.props(started=self.mtime)}
        self.assertEqual(self.run_probe(units), [])

    def test_directory_argv_token_is_noise_not_drift(self):
        # e.g. atd's `find ... -newercc /run/systemd` — a directory whose mtime always moves
        adir = os.path.join(self.tmp, "hotdir")
        os.makedirs(adir)
        units = {"svc.service": [("ExecMainStartTimestamp", f"@{self.mtime + 100}"),
                                   ("ExecStart", exec_block("/bin/find", adir, "-type", "f")),
                                   ("FragmentPath", self.unitfile),
                                   ("DropInPaths", "")]}
        os.utime(adir, (self.mtime + 500, self.mtime + 500))
        self.assertEqual(self.run_probe(units), [])

    def test_one_finding_per_unit_even_with_multiple_drifted_deps(self):
        with open(self.unitfile, "w") as fh:
            fh.write("[Service]\n# edited\n")
        units = {"svc.service": self.props(started=self.mtime - 100)}
        self.assertEqual(len(self.run_probe(units)), 1)

    def test_unit_file_drift_alone_flags(self):
        os.utime(self.script, (self.mtime - 500, self.mtime - 500))
        os.utime(self.unitfile, (self.mtime + 500, self.mtime + 500))
        units = {"svc.service": self.props(started=self.mtime)}
        findings = self.run_probe(units)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].fields["path"], self.unitfile)

    def test_exclusion_globs(self):
        units = {"svc.service": self.props(started=self.mtime - 100),
                 "dev-hack.service": self.props(started=self.mtime - 100)}
        config = make_config(self.tmp, QQ_PROC_PROBE_EXCLUDE="dev-*.service")
        findings = self.run_probe(units, config=config)
        self.assertEqual([f.fields["unit"] for f in findings], ["svc.service"])

    def test_user_scope_probed_and_labeled(self):
        findings = self.run_probe({}, user_units={"usvc.service":
                                                     self.props(started=self.mtime - 100)},
                                    scopes=("system", "user"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].fields["scope"], "user")

    def test_systemctl_unavailable_fails_soft(self):
        config = make_config(self.tmp)
        with mock.patch.object(pp, "_run", lambda argv, timeout=0: None):
            self.assertEqual(pp.probe(config), [])


class TestFindingsChannel(ProbeHarness):
    def test_render_and_parse_roundtrip(self):
        units = {"svc.service": self.props(started=self.mtime - 100)}
        f = self.run_probe(units)[0]
        line = f.render()
        self.assertTrue(line.startswith("- [PROC drift] svc.service (system): running process "
                                          "predates on-disk change – "))
        self.assertIn(self.script, line)
        self.assertEqual(parse_line(line).cls, "PROC drift")
        self.assertEqual(parse_line(line).render(), line)   # verbatim round-trip

    def test_write_findings_auto_resolve_cycle(self):
        config = make_config(self.tmp)
        store = Store(config)
        units = {"svc.service": self.props(started=self.mtime - 100)}
        findings = self.run_probe(units, config=config)
        path = pp.write_findings(store, findings)
        with open(path, encoding="utf-8") as fh:
            ff = FindingsFile.parse(fh.read())
        self.assertEqual(len(ff.get_section("PROC")), 1)
        self.assertIn("PROC drift", ff.render())
        # existing sections survive the PROC write
        self.assertIn("TIER1", ff.sections)
        # ... and a clean probe run EMPTIES the section (auto-resolve = the restart cleared it)
        pp.write_findings(store, [])
        with open(path, encoding="utf-8") as fh:
            ff2 = FindingsFile.parse(fh.read())
        self.assertEqual(ff2.get_section("PROC"), [])
        self.assertNotIn("PROC drift", ff2.render())


if __name__ == "__main__":
    unittest.main()
