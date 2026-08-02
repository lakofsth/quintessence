#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""gen_config_docs.py – renders quintessence.config.KEYS into CONFIG.md.

The KEYS registry in quintessence/config.py is the single source of truth for every
QQ_*/deployment key; this tool is the ONE renderer, and tests/py/test_config_docs.py asserts
the committed CONFIG.md is byte-identical to a fresh render – so a KeyDef edit that isn't
mirrored into CONFIG.md fails the suite instead of silently drifting.

Defaults are rendered with a portable stand-in Config (empty env, a nonexistent config file)
so every literal/derived default is the true built-in default – NOT whatever this machine's
$HOME or XDG_* vars happen to be. `~` in the registry's HOME-derived defaults is substituted
back in explicitly (KEYS bakes the *builder's* os.path.expanduser("~") at import time, which
would otherwise leak the machine that ran this generator into a doc meant to ship publicly).

Usage:
    python3 tools/gen_config_docs.py            # print the rendered doc to stdout
    python3 tools/gen_config_docs.py --write PATH   # write the rendered doc to PATH
    python3 tools/gen_config_docs.py --check PATH   # exit 1 (+ diff) if PATH has drifted
"""
from __future__ import annotations

import argparse
import difflib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINE = os.path.dirname(_HERE)
if _ENGINE not in sys.path:
    sys.path.insert(0, _ENGINE)

from quintessence.config import KEYS, HOME, Config  # noqa: E402

HEADER = """<!-- GENERATED FILE – do not hand-edit.
     Source of truth: quintessence/config.py KEYS registry.
     Regenerate: python3 tools/gen_config_docs.py --write CONFIG.md
     Drift is caught by tests/py/test_config_docs.py: docs generate from the registry,
     so they cannot silently diverge from it. -->

# Configuration reference

Every setting quintessence resolves, in one table, generated from the `KEYS` registry in
`quintessence/config.py` – the single source of truth. Resolution order for a
registered key is **constructor override > environment variable > the dotenv config file
(`$QQ_CONFIG`, default `~/.config/quintessence/config`) > this table's default**, computed
once per `Config` instance. See `README.md` (`## Configuration`) for the short version and
`qq config set` / `qq config show` / `qq config get <KEY>` / `qq config path` for managing the
durable file – `show` dumps it, `get` reads one key, which is how the shipped skills read theirs.

"""


def _portable_default(kd) -> str:
    """The effective built-in default for `kd`, as a portable (never this-host-specific)
    string."""
    cfg = Config(env={}, config_file="/nonexistent/quintessence-docgen-config")
    try:
        value = cfg.get(kd.name)
    except Exception as exc:  # pragma: no cover - defensive; a KeyDef bug should fail loudly
        return f"*(error resolving default: {exc})*"
    if value is None:
        return "*(unset)*"
    if isinstance(value, list):
        text = ", ".join(str(v) for v in value) if value else "*(empty)*"
    elif isinstance(value, bool):
        text = str(value)
    else:
        text = str(value)
    # Portability: KEYS bakes HOME (this generator-run's os.path.expanduser("~")) into any
    # literal/derived path default; fold it back to the portable "~" so the committed doc
    # never names the machine that generated it.
    if HOME and HOME != "/" and HOME in text:
        text = text.replace(HOME, "~")
    return text or "*(empty)*"


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


LEGEND = """\
The three boolean types in the Type column differ in what counts as true:
`bool_truthy_exact_one` is true only when set to exactly `1`; `bool_default_true_falsy_zero`
is true unless set to exactly `0` (unset means true); `bool_truthy_nonempty` is true when set
to any non-empty value.
"""


def render() -> str:
    lines = [HEADER.rstrip("\n"), "", LEGEND.rstrip("\n"), ""]
    lines.append("| Var | Type | Default | Scope | Description |")
    lines.append("|---|---|---|---|---|")
    for kd in KEYS:
        default = _escape_cell(_portable_default(kd))
        description = _escape_cell(kd.description)
        lines.append(f"| `{kd.name}` | `{kd.type}` | {default} | {kd.scope} | {description} |")
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", metavar="PATH", help="write the rendered doc to PATH")
    ap.add_argument("--check", metavar="PATH", help="exit 1 (with a diff) if PATH != fresh render")
    args = ap.parse_args(argv)

    rendered = render()

    if args.check:
        try:
            with open(args.check) as f:
                committed = f.read()
        except OSError as exc:
            print(f"gen_config_docs: cannot read {args.check}: {exc}", file=sys.stderr)
            return 1
        if committed != rendered:
            diff = difflib.unified_diff(
                committed.splitlines(keepends=True), rendered.splitlines(keepends=True),
                fromfile=f"{args.check} (committed)", tofile="(fresh render)")
            sys.stdout.writelines(diff)
            print(f"\ngen_config_docs: {args.check} has DRIFTED from the KEYS registry "
                  f"– regenerate with --write.", file=sys.stderr)
            return 1
        print(f"gen_config_docs: {args.check} matches the KEYS registry.")
        return 0

    if args.write:
        with open(args.write, "w") as f:
            f.write(rendered)
        print(f"gen_config_docs: wrote {args.write}")
        return 0

    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
