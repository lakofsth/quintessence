#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""proc-probe.py — the B4 proc-vs-disk probe (standalone entry point).

Flags any RUNNING service whose ExecStart script / listed dep / unit file mtime postdates
ExecMainStartTimestamp — "running process predates on-disk change", i.e. a service still
running code that has since changed underneath it. Rides consistency-audit.sh before its change
gate; findings land in the pending-findings PROC section (auto-resolve: restart clears on the
next run). QQ_PROC_PROBE_EXCLUDE (csv, globs) skips expected drifters.

  proc-probe.py                 print current findings (both scopes)
  proc-probe.py --write         also persist them to the PROC section
  proc-probe.py --scope user    one scope only
  proc-probe.py --json          machine-readable

Always exits 0 on probe-side failure (fail-soft toward the audit runner). Logic lives in
quintessence.procprobe; this file is the path shim.
"""
import os
import sys

ENGINE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, ENGINE)

from quintessence.procprobe import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
