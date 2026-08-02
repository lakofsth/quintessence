#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""staleness-xref.py — T2 STALENESS-XREF: memory-vs-HEAD supersession checker (CLI front-end).

Thin front-end over quintessence.xref.Xref: this script owns argv parsing and
printing only; a single Config + SearchIndex are resolved once and threaded down. See
quintessence/xref.py for the full design (detects memory facts a quintessence HEAD has likely
superseded via the existing embedding index; ADJUDICATION stays with the session model).

usage: staleness-xref.py                 print finding lines (empty = none; exit !=0 = index failed,
                                         caller must NOT clear the XREF section)
       staleness-xref.py --explain       tuning view: near-threshold pairs + why each did(n't) fire
       staleness-xref.py --waveoff M H   silence pair (memory M, HEAD H) until H updates again
       staleness-xref.py --waveoff-head H  bulk-waveoff every currently-FIRING pair for HEAD H

env: QQ_XREF_SIM (0.55)  QQ_XREF_DAYS (3)  QQ_XREF_MAX (5; newest-HEAD pairs first)
     QQ_XREF_GENERIC_TYPES (feedback; memory types skipped as generic working-style guidance —
     orthogonal to HEADs; per-memory `xref: track` force-includes, `xref: skip` force-excludes)
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from quintessence.config import Config  # noqa: E402
from quintessence.search import SearchIndex  # noqa: E402
from quintessence.xref import Xref  # noqa: E402


def main():
    args = sys.argv[1:]
    config = Config()
    idx = SearchIndex(config)
    xref = Xref(config, idx)

    if args and args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    # recall-06 (and its follow-up, this verification pass): xref.findings() -> Xref.pair_sims()
    # raises RuntimeError when the mem/qq index is incomplete (embedder down, or an empty
    # corpus). recall-06 first fixed this for the default (no-args) branch only; --explain
    # (xref.explain()) and --waveoff-head (xref.waveoff_head_all()) both transitively call
    # candidates() -> pair_sims() too and still let the SAME RuntimeError escape as a raw
    # traceback. Wrapping the dispatch here, once, covers every subcommand that reaches
    # pair_sims() so they all degrade identically -- `qq`'s own use of the identical Xref class
    # (both `qq check` and `qq waveoff --head`) already wraps this exact call for a clean
    # message. The usage docstring's own contract ("exit != 0 = index failed, caller must NOT
    # clear the XREF section") already covers a clean non-zero exit -- only the raw-traceback
    # PRESENTATION was the bug, here as there.
    try:
        if args and args[0] == "--waveoff":
            if len(args) != 3:
                sys.exit("usage: staleness-xref.py --waveoff <memory> <head-topic>")
            try:
                print(xref.waveoff(args[1], args[2]))
            except ValueError as e:
                sys.exit(str(e))
            return
        if args and args[0] == "--waveoff-head":
            if len(args) != 2:
                sys.exit("usage: staleness-xref.py --waveoff-head <head-topic>")
            print(xref.waveoff_head_all(args[1]))
            return
        if args and args[0] == "--explain":
            print(xref.explain())
            return
        if args:
            sys.exit(f"staleness-xref.py: unknown arg {args[0]} (see --help)")
        lines = xref.findings()
    except RuntimeError as e:
        sys.exit(f"staleness-xref.py: {e}")
    for ln in lines:
        print(ln)


if __name__ == "__main__":
    main()
