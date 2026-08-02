#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""config-reconcile.py — CONFIG-vs-DOC drift checker (CLI front-end).

Thin front-end over quintessence.reconcile.Reconcile: this script owns argv parsing
and printing only; a single Config is resolved once and threaded down. See
quintessence/reconcile.py for the full design (present-tense-only scanning, false-positive
guards, the deploy-hook change-detection half) — unchanged here, only re-hosted.

usage: config-reconcile.py            print finding lines (empty = no drift)
       config-reconcile.py --explain  show each knob's live value + what it scanned
       config-reconcile.py --commit-snapshot  acknowledge (persist) any detected knob CHANGE
env:   QQ_RECONCILE_REGISTRY (registry path; unset => no-op)  QUINTESSENCE_DIR  QQ_MEMDIR
       QQ_DOCDIR (prose-doc dir to scan, default ~/docs; absent/empty => docs not scanned)
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from quintessence.config import Config  # noqa: E402
from quintessence.reconcile import Reconcile  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    explain = "--explain" in args
    commit = "--commit-snapshot" in args
    config = Config()
    reconcile = Reconcile(config)
    for line in reconcile.run(explain=explain, commit=commit):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
