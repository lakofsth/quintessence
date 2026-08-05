# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.reconcile — L2: config-vs-doc drift checker.

Port of config-reconcile.py behind the same config discipline: configuration (the registry
path, the snapshot path, the doc dir) resolves once from the `Config` passed to
`Reconcile.__init__`.
See config-reconcile.py's original docstring for the full design rationale (present-tense-only
scanning, the false-positive guards, the deploy-hook change-detection half) — unchanged here,
only re-hosted. Generic engine: with no registry configured (QQ_RECONCILE_REGISTRY unset/empty)
every call is a silent no-op, so a portable install is unaffected.
"""
from __future__ import annotations

import configparser
import fnmatch
import glob
import json
import os
import re
import subprocess

from .atomicio import atomic_write_json, best_effort_write
from .config import Config

_HEADING = re.compile(r"^(#{1,6})\s")
_HISTORY_HEADING = re.compile(
    r"^#{1,6}\s*(?:removed|purged|history|changelog|deprecated|superseded|"
    r"former|formerly|old|previous(?:ly)?|past|retired|archive[d]?)\b", re.I)
_RECONCILE_OK = re.compile(r"<!--\s*reconcile-ok\s*(?::\s*([^>]*?))?\s*-->", re.I)


def strip_history_sections(text: str) -> str:
    out: list = []
    skip_level = 0
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            level = len(m.group(1))
            if skip_level and level <= skip_level:
                skip_level = 0
            if not skip_level and _HISTORY_HEADING.match(line):
                skip_level = level
                continue
        if skip_level:
            continue
        out.append(line)
    return "\n".join(out)


def inline_exemptions(text: str):
    exempt_all = False
    toks: set = set()
    for m in _RECONCILE_OK.finditer(text):
        body = (m.group(1) or "").strip()
        if not body:
            exempt_all = True
        else:
            toks.update(t for t in re.split(r"[,\s]+", body) if t)
    return exempt_all, toks


class Reconcile:
    def __init__(self, config: Config):
        self.config = config
        self.qdir = config.get_path("QUINTESSENCE_DIR")
        self.memdir = config.get_path("QQ_MEMDIR")
        self.docdir = config.get_path("QQ_DOCDIR")
        self.registry = config.get_path("QQ_RECONCILE_REGISTRY")
        self.snapshot_path = config.get_path("QQ_RECONCILE_SNAPSHOT")

    # ---- live-value readers -----------------------------------------------------------------
    def read_live(self, spec: str):
        parts = spec.split()
        if not parts:
            return None
        kind = parts[0]
        try:
            if kind == "systemd" and len(parts) >= 3:
                unit, var = parts[1], parts[2]
                out = subprocess.run(["systemctl", "show", "-p", "Environment", unit],
                                     capture_output=True, text=True, timeout=10).stdout
                body = out.split("=", 1)[1] if "=" in out else ""
                for tok in body.split():
                    if tok.startswith(var + "="):
                        return tok[len(var) + 1:].strip()
                return None
            if kind == "envfile" and len(parts) >= 3:
                path, key = os.path.expanduser(parts[1]), parts[2]
                val = None
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if line.startswith(key + "="):
                            val = line[len(key) + 1:].strip().strip('"').strip("'")
                return val
            if kind == "cmd" and len(parts) >= 2:
                res = subprocess.run(spec.split(None, 1)[1], shell=True,
                                     capture_output=True, text=True, timeout=15)
                return res.stdout.strip() or None
        except Exception:
            return None
        return None

    # ---- snapshot (deploy-hook change-detection state) ---------------------------------------
    def _load_snapshot(self) -> dict:
        try:
            with open(self.snapshot_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save_snapshot(self, snap: dict) -> None:
        # Same shape as search.py's orphan-ages sidecar, and the same reason: QQ_RECONCILE_SNAPSHOT
        # is a name an operator sets, so this call site can reach the atomic write's name-length
        # refusal, and a bare `except OSError: pass` would swallow it into a snapshot that stops
        # being written with nothing said. Every other OSError keeps its old silence.
        with best_effort_write("reconcile snapshot", self.snapshot_path):
            atomic_write_json(self.snapshot_path, snap, indent=0, sort_keys=True)

    # ---- scannable present-tense surfaces -----------------------------------------------------
    def present_tense_surfaces(self):
        for f in sorted(glob.glob(os.path.join(self.qdir, "*.md"))):
            b = os.path.basename(f)[:-3]
            if b in ("INDEX", "RUBRIC"):
                continue
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if line.startswith("> essence:"):
                            yield (f"HEAD {b} (essence)", line.split(":", 1)[1], "essence", f)
                            break
            except OSError:
                continue
        for f in sorted(glob.glob(os.path.join(self.memdir, "*.md"))):
            b = os.path.basename(f)
            if b == "MEMORY.md":
                continue
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    yield (f"memory {b}", fh.read(), "memory", f)
            except OSError:
                continue
        if self.docdir and os.path.isdir(self.docdir):
            for f in sorted(glob.glob(os.path.join(self.docdir, "**", "*.md"), recursive=True)):
                try:
                    with open(f, encoding="utf-8", errors="replace") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                rel = os.path.relpath(f, self.docdir)
                yield (f"doc {rel}", strip_history_sections(raw), "doc", f)

    @staticmethod
    def _path_exempt(path: str, root: str, globs: list) -> bool:
        rel = os.path.relpath(path, root) if root else path
        return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(rel, g) for g in globs)

    # ---- entry point ---------------------------------------------------------------------------
    def run(self, explain: bool = False, commit: bool = False) -> list:
        """Returns the list of `- [T1 config-drift] ...` finding lines (empty = no drift). If
        `explain`, ALSO prints (not returns — matches the original's stdout-interleaved
        `--explain` diagnostics) a `# label: ...` line per registry section as it's evaluated."""
        if not self.registry or not os.path.isfile(self.registry):
            if explain:
                print(f"config-reconcile: no registry (QQ_RECONCILE_REGISTRY={self.registry!r}) – no-op")
            return []
        cp = configparser.ConfigParser()
        try:
            cp.read(self.registry, encoding="utf-8")
        except configparser.Error as e:
            return [f"- [T1 config-drift] registry parse error in {self.registry}: {e}"]

        surfaces = list(self.present_tense_surfaces())
        snap = self._load_snapshot()
        new_snap = dict(snap)
        findings: list = []
        for label in cp.sections():
            sec = cp[label]
            source = sec.get("source", "").strip()
            live = self.read_live(source) if source else None
            if explain:
                print(f"# {label}: source={source!r} live={live!r} last={snap.get(label)!r}")
            if live is None:
                continue
            new_snap[label] = live
            current = sec.get("current", "").strip()
            if current and current not in live:
                findings.append(
                    f"- [T1 config-drift] live {label} = {live!r} does NOT contain the registry's "
                    f"expected current token {current!r} – config moved to an unrecognized value; "
                    f"update the registry (and any docs) to the new value.")
            exempt_globs = [g.strip() for g in re.split(r"\|\|", sec.get("exempt", "")) if g.strip()]
            auto_stale: list = []
            prev = snap.get(label)
            if prev is not None and prev != live:
                findings.append(
                    f"- [T1 config-drift] {label} CHANGED since last check: {prev!r} -> {live!r} – "
                    f"review HEAD essences / memory facts / docs still naming the old value.")
                auto_stale.append(prev)
            declared = [t.strip() for t in re.split(r"\|\|", sec.get("stale", "")) if t.strip()]
            for tok in declared + auto_stale:
                if not tok or tok in live:
                    continue
                for dlabel, text, kind, path in surfaces:
                    if tok not in text:
                        continue
                    if current and current in text:
                        continue
                    if kind == "doc" and exempt_globs and self._path_exempt(path, self.docdir, exempt_globs):
                        continue
                    if exempt_globs and kind != "doc" and self._path_exempt(path, "", exempt_globs):
                        continue
                    ex_all, ex_toks = inline_exemptions(text)
                    if ex_all or tok in ex_toks:
                        continue
                    findings.append(
                        f"- [T1 config-drift] {dlabel} asserts a stale {label} – names {tok!r} "
                        f"but live {label} = {live!r}; refresh the doc or `qq waveoff`/update the registry.")
        if commit:
            self._save_snapshot(new_snap)
        seen: set = set()
        out = []
        for line in findings:
            if line not in seen:
                seen.add(line)
                out.append(line)
        return out
