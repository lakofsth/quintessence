# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.procprobe — B4 proc-vs-disk probe (reality binding).

The qq-ask-http class of reality self-divergence, generalized: a RUNNING service whose
ExecStart script (or a listed dep: any absolute-path argv token, the unit file itself, its
drop-ins) has an on-disk mtime NEWER than the process's ExecMainStartTimestamp is running
stale code — "running process predates on-disk change". Detection is mechanical (systemctl
show + stat); adjudication is the findings pipeline's job: detection reports, it never decides.

Findings go to the SAME pending-findings channel the consistency audit uses
($QQ_STATE_DIR/pending-findings.md), in their own auto-resolve PROC section: each probe run
emits its FULL current set (findings.sh section semantics), so a restart clears the finding on
the next run — the flag/clear loop the findings queue is built around. The probe rides
consistency-audit.sh, BEFORE the change gate (the gate protects the LLM spend; this is
milliseconds of systemctl + stat).

QQ_PROC_PROBE_EXCLUDE (csv, fnmatch globs) skips units where drift is expected/benign (e.g. a
dev service run from a working tree). Both systemd scopes are probed; a scope whose systemctl
is unreachable (no user session, no privileges) degrades silently to nothing — fail-soft
everywhere, the probe can only ever ADD findings lines, never break the audit or the queue.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import stat as statmod
import subprocess
import time
from datetime import datetime, timezone

from .findings import Finding, FindingsFile
from .store import Store, state_lock

SECTION = "PROC"
_SHOW_PROPS = "ExecMainStartTimestamp,ExecStart,ExecStartPre,FragmentPath,DropInPaths"
_TIMEOUT = 15

_ARGV_RE = re.compile(r"argv\[\]=([^;}]*)")


def _run(argv: list, timeout: int = _TIMEOUT) -> "str | None":
    """stdout on success, None on ANY failure (missing binary, nonzero, timeout)."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    return r.stdout if r.returncode == 0 else None


def _scope_argv(scope: str) -> list:
    return ["systemctl", "--user"] if scope == "user" else ["systemctl"]


def list_running(scope: str) -> list:
    out = _run(_scope_argv(scope) + ["list-units", "--type=service", "--state=running",
                                       "--no-legend", "--plain"])
    if out is None:
        return []
    return [ln.split()[0] for ln in out.splitlines() if ln.strip()]


def show_unit(unit: str, scope: str) -> dict:
    """Property map for `unit`; values are LISTS (ExecStart/ExecStartPre can repeat).
    Prefers `--timestamp=unix` (exact epoch, no tz parsing); falls back to the default
    wall-clock rendering on a systemd too old for the flag."""
    out = _run(_scope_argv(scope) + ["show", "--timestamp=unix", "-p", _SHOW_PROPS,
                                       "--", unit])
    if out is None:
        out = _run(_scope_argv(scope) + ["show", "-p", _SHOW_PROPS, "--", unit])
    props: dict = {}
    for ln in (out or "").splitlines():
        k, sep, v = ln.partition("=")
        if sep:
            props.setdefault(k, []).append(v)
    return props


def start_epoch(props: dict) -> "int | None":
    for v in props.get("ExecMainStartTimestamp", []):
        v = v.strip()
        if v.startswith("@"):
            try:
                return int(float(v[1:]))
            except ValueError:
                continue
        # wall-clock fallback: "Fri 2026-07-03 23:11:07 EEST" — systemctl renders LOCAL time,
        # mktime interprets local time; the tz NAME is redundant and ignored.
        parts = v.split()
        if len(parts) >= 3:
            try:
                return int(time.mktime(time.strptime(f"{parts[1]} {parts[2]}",
                                                       "%Y-%m-%d %H:%M:%S")))
            except ValueError:
                continue
    return None


def dep_paths(props: dict) -> list:
    """The on-disk artifacts a running unit depends on, for a `proc` referent: every
    absolute-path token in ExecStart/ExecStartPre argv (interpreter, script, AND flag-fed
    paths like --config=/etc/x — a 'listed dep' in the most literal sense), plus the unit
    file and its drop-ins. First-seen order, deduped."""
    out: list = []
    seen: set = set()

    def add(p: str) -> None:
        if p and p not in seen:
            seen.add(p)
            out.append(p)

    for key in ("ExecStart", "ExecStartPre"):
        for v in props.get(key, []):
            for m in _ARGV_RE.finditer(v):
                for tok in m.group(1).split():
                    cand = tok
                    if not tok.startswith("/") and "=" in tok:
                        cand = tok.split("=", 1)[1]
                    if cand.startswith("/"):
                        add(cand)
    for key in ("FragmentPath", "DropInPaths"):
        for v in props.get(key, []):
            for tok in v.split():
                if tok.startswith("/"):
                    add(tok)
    return out


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def probe_unit(unit: str, scope: str) -> "Finding | None":
    """ONE finding per drifted unit (the unit is the actionable object — a restart reconciles
    every path at once), naming the first offending path."""
    props = show_unit(unit, scope)
    started = start_epoch(props)
    if started is None:
        return None
    for path in dep_paths(props):
        try:
            st = os.stat(path)
        except OSError:
            continue   # a vanished dep is the audit sweep's class, not this probe's
        if not statmod.S_ISREG(st.st_mode):
            continue   # only regular files: a DIRECTORY argv token (e.g. atd's find over
                        # /run/systemd) has a constantly-moving mtime — pure noise
        if int(st.st_mtime) > started:
            return Finding(cls="PROC drift", fields={
                "unit": unit, "scope": scope, "path": path,
                "mtime": _iso(st.st_mtime), "started": _iso(started)})
    return None


def probe(config, scopes: tuple = ("system", "user")) -> list:
    exclude = config.get_list("QQ_PROC_PROBE_EXCLUDE")
    findings: list = []
    for scope in scopes:
        for unit in list_running(scope):
            if any(fnmatch.fnmatch(unit, pat) for pat in exclude):
                continue
            try:
                f = probe_unit(unit, scope)
            except Exception:
                continue   # one broken unit never kills the sweep over the rest
            if f is not None:
                findings.append(f)
    return findings


def write_findings(store: Store, findings: list) -> str:
    """Full-replace the PROC section (auto-resolve semantics, same discipline as
    checks.persist_findings and the same state_lock/atomic-replace write path). Returns the
    pending-findings path."""
    path = os.path.join(store.config.get_path("QQ_STATE_DIR"), "pending-findings.md")
    lines = [f.render() for f in findings]
    with state_lock(store):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                ff = FindingsFile.parse(fh.read())
        except OSError:
            ff = FindingsFile()
        ff.set_section(SECTION, lines)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(ff.serialize())
        os.replace(tmp, path)
    return path


def main(argv: list) -> int:
    """proc-probe.py [--write] [--json] [--scope system|user] — never exits nonzero for a
    probe-side failure (fail-soft toward the audit runner that calls it)."""
    from .config import Config
    write = "--write" in argv
    as_json = "--json" in argv
    scopes: tuple = ("system", "user")
    if "--scope" in argv:
        i = argv.index("--scope")
        if i + 1 < len(argv) and argv[i + 1] in ("system", "user"):
            scopes = (argv[i + 1],)
    try:
        config = Config()
        findings = probe(config, scopes=scopes)
        if as_json:
            print(json.dumps([{"class": f.cls, **f.fields} for f in findings]))
        else:
            for f in findings:
                print(f.render())
        if write:
            path = write_findings(Store(config), findings)
            print(f"proc-probe: {len(findings)} finding(s) -> {SECTION} section of {path}")
    except Exception as e:
        print(f"proc-probe: error (fail-soft): {e!r}")
    return 0
