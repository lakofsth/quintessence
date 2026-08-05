# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.xref — L2: T2 staleness-xref, memory-vs-HEAD supersession checker.

Port of staleness-xref.py behind the same config discipline: configuration resolves once from
the `Config` + `SearchIndex` passed to `Xref.__init__` (the SAME SearchIndex a caller already
built for search/ask, so the embedding index is reused, not rebuilt twice).

Detects memory facts a quintessence HEAD has likely superseded: for each memory fact, find the
nearest HEADs via the existing embedding index (max cosine over chunk pairs — no second
embedder, no LLM at scan time) and flag pairs where similarity >= QQ_XREF_SIM AND the HEAD's
newest "> updated:" date is >= QQ_XREF_DAYS newer than the memory's content-anchored "moved"
time. Findings go to the XREF section of the pending-findings queue; ADJUDICATION stays with
the session model at presentation time (stale vs compatible -> edit the memory, or
`qq waveoff <memory> <head>`). See staleness-xref.py's original docstring for the full design
rationale (wave-off dedupe, content-anchored "moved" epoch) — unchanged here, only re-hosted.

PRESERVED DELIBERATELY (not consolidated onto Config, a judgment call — flagged for review):
`self.memdir` derives from `search_index.kb_root + "/memory"` (the "mem" corpus-source
directory the SAME index scores under the "mem" label), NOT from `QQ_MEMDIR`. On this
deployment `~/kb/memory` symlinks to `QQ_MEMDIR`'s target, so the two coincide today, but they
are not guaranteed to on every install — staleness-xref.py's own MEMDIR was always
`qc.KB + "/memory"`, and changing that silently (even to something that today resolves to the
same directory) would be exactly the kind of un-flagged behavior change parity testing exists
to catch. A future phase may want to reconcile this onto QQ_MEMDIR directly; not done here.
"""
from __future__ import annotations

import calendar
import hashlib
import math
import os
import re
import time
from typing import Optional

from .atomicio import atomic_write_lines, best_effort_write
from .config import Config
from .findings import Finding
from .search import SearchIndex


def _iso_to_epoch(iso: str) -> Optional[int]:
    s = iso.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return calendar.timegm(time.strptime(s, fmt))
        except ValueError:
            continue
    return None


class Xref:
    def __init__(self, config: Config, search_index: SearchIndex):
        self.config = config
        self.search_index = search_index
        self.qdir = config.get_path("QUINTESSENCE_DIR")
        self.memdir = os.path.join(search_index.kb_root, "memory")   # see module docstring
        self.waveoffs_path = config.get_path("QQ_XREF_WAVEOFFS")
        self.content_path = config.get_path("QQ_XREF_CONTENT")
        self.sim = config.get_float("QQ_XREF_SIM")
        self.days = config.get_float("QQ_XREF_DAYS")
        self.max_findings = config.get_int("QQ_XREF_MAX")
        self.generic_types = set(config.get_list("QQ_XREF_GENERIC_TYPES"))
        self.day_seconds = 86400.0

    # ---- HEAD "updated" reads --------------------------------------------------------------
    def head_updated_epoch(self, topic: str):
        path = os.path.join(self.qdir, topic + ".md")
        iso = None
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for ln in f:
                    m = re.match(r"^> updated:\s*(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z?)?)", ln)
                    if m:
                        iso = m.group(1)
                        break
        except OSError:
            pass
        if iso:
            e = _iso_to_epoch(iso)
            if e is not None:
                return e, iso
        try:
            e = os.path.getmtime(path)
            return e, time.strftime("%FT%TZ", time.gmtime(e))
        except OSError:
            return None, None

    # ---- pairwise similarity via the shared index ------------------------------------------
    def pair_sims(self) -> dict:
        idx = self.search_index.build_index()
        miss = [d for d in self.search_index.last_build_dropped if d[0] in ("mem", "qq")]
        if miss:
            raise RuntimeError(f"index incomplete: {len(miss)} mem/qq chunk(s) failed to embed "
                               f"(e.g. {miss[0][1]}) – refusing to risk masking a stale pair")
        mem: dict = {}
        qq: dict = {}
        for lb, path, _title, _txt, v in idx:
            n = math.sqrt(sum(x * x for x in v))
            if not n:
                continue
            nv = [x / n for x in v]
            b = os.path.basename(path)[:-3]
            if lb == "mem":
                mem.setdefault(b, []).append(nv)
            elif lb == "qq" and b != "RUBRIC":
                qq.setdefault(b, []).append(nv)
        if not mem or not qq:
            raise RuntimeError("index empty for mem/qq – embedder down or kb unwired")
        sims = {}
        for mname, mvs in mem.items():
            for hname, hvs in qq.items():
                sims[(mname, hname)] = max(
                    sum(a * b for a, b in zip(mv, hv)) for mv in mvs for hv in hvs)
        return sims

    # ---- memory frontmatter -----------------------------------------------------------------
    def _mem_meta(self, mempath: str):
        typ = override = None
        try:
            with open(mempath, encoding="utf-8", errors="replace") as f:
                in_fm = False
                for i, ln in enumerate(f):
                    s = ln.strip()
                    if i == 0 and s == "---":
                        in_fm = True
                        continue
                    if in_fm and s == "---":
                        break
                    if i > 40:
                        break
                    m = re.match(r"type:\s*([\w-]+)", s)
                    if m:
                        typ = m.group(1)
                    m2 = re.match(r"xref:\s*([\w-]+)", s)
                    if m2:
                        override = m2.group(1)
        except OSError:
            pass
        return typ, override

    def is_generic(self, mempath: str) -> bool:
        typ, override = self._mem_meta(mempath)
        if override == "track":
            return False
        if override == "skip":
            return True
        return typ in self.generic_types

    # ---- wave-off state ----------------------------------------------------------------------
    def load_waveoffs(self) -> dict:
        out = {}
        try:
            with open(self.waveoffs_path, encoding="utf-8") as f:
                for ln in f:
                    parts = ln.rstrip("\n").split("\t")
                    if len(parts) >= 3:
                        out[(parts[0], parts[1])] = parts[2]
        except OSError:
            pass
        return out

    def _persist_waveoffs(self, rows: dict) -> None:
        now = time.strftime('%FT%TZ', time.gmtime())
        atomic_write_lines(self.waveoffs_path,
            (f"{m}\t{h}\t{iso}\t{now}\n" for (m, h), iso in sorted(rows.items())))

    @staticmethod
    def _waved_still_quiet(waved_iso: str, head_e: float, head_iso: str) -> bool:
        if len(waved_iso) == 10:
            return waved_iso >= head_iso[:10]
        return (_iso_to_epoch(waved_iso) or 0) >= head_e

    # ---- content-anchored "moved" epoch -------------------------------------------------------
    def content_hash(self, topic: str):
        path = os.path.join(self.qdir, topic + ".md")
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                body = "".join(ln for ln in f if not ln.startswith("> updated:"))
        except OSError:
            return None
        return hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()

    def load_content_store(self) -> dict:
        out = {}
        try:
            with open(self.content_path, encoding="utf-8") as f:
                for ln in f:
                    p = ln.rstrip("\n").split("\t")
                    if len(p) >= 3:
                        try:
                            out[p[0]] = (p[1], float(p[2]))
                        except ValueError:
                            pass
        except OSError:
            pass
        return out

    def save_content_store(self, store: dict) -> None:
        """Write the content-hash store.

        All three callers treat this as best-effort and wrap it in `atomicio.best_effort_write`:
        the store is a cache of hashes that rebuilds itself from the HEADs, so a failed save must
        not fail a `qq check`. The decision stays at the callers rather than moving in here, so a
        caller that does want the failure -- there is none today -- gets it by not wrapping."""
        atomic_write_lines(self.content_path,
            (f"{topic}\t{h}\t{e:.0f}\n" for topic, (h, e) in sorted(store.items())))

    def head_moved_epoch(self, topic: str, store: dict):
        head_e, head_iso = self.head_updated_epoch(topic)
        ch = self.content_hash(topic)
        prev = store.get(topic)
        dirty = False
        if ch is not None and prev and prev[0] == ch:
            me = prev[1]
        else:
            me = head_e if head_e is not None else 0.0
            if ch is not None:
                store[topic] = (ch, me)
                dirty = True
        miso = time.strftime("%FT%TZ", time.gmtime(me)) if me else (head_iso or "")
        return me, miso, head_iso, dirty

    # ---- candidates / findings -----------------------------------------------------------------
    def candidates(self) -> list:
        sims = self.pair_sims()
        waved = self.load_waveoffs()
        store = self.load_content_store()
        dirty = False
        moved: dict = {}
        rows = []
        for (mname, hname), sim in sims.items():
            if sim < self.sim:
                continue
            mempath = os.path.join(self.memdir, mname + ".md")
            try:
                # KNOWN GAP: this still trusts file mtime, which a restore/re-clone
                # resets wholesale. The durable fix ("memories get a dated field maintained by
                # the write path, mtime demoted to fallback") is NEITHER P4 NOR P5 as currently
                # scoped: the "write path" that would need to stamp it is the Claude Code memory
                # tool's own save mechanism (QQ_MEMDIR's files), which quintessence's own P5
                # (qq-write, the HEAD store's write path) does not touch or control. Flagged as
                # an open scope gap for the review gate — P4 build report, 2026-07-03.
                mem_e = os.path.getmtime(mempath)
            except OSError:
                continue
            if self.is_generic(mempath):
                continue
            if hname not in moved:
                me, miso, disp, d = self.head_moved_epoch(hname, store)
                dirty = dirty or d
                moved[hname] = (me, miso, disp)
            moved_e, moved_iso, disp_iso = moved[hname]
            if moved_e is None:
                continue
            mem_iso = time.strftime("%Y-%m-%d", time.gmtime(mem_e))
            if moved_e - mem_e < self.days * self.day_seconds:
                verdict = "date-gate"
            elif (mname, hname) in waved and self._waved_still_quiet(waved[(mname, hname)], moved_e, moved_iso):
                verdict = "waved-off"
            else:
                verdict = "FIRE"
            rows.append((moved_e, sim, mname, mem_iso, hname, disp_iso, verdict))
        if dirty:
            with best_effort_write("xref content store", self.content_path):
                self.save_content_store(store)
        rows.sort(key=lambda r: (-r[0], -r[1]))
        return rows

    def finding_records(self) -> list[Finding]:
        """Same FIRING pairs as findings() (the producer emits a typed Finding — fields
        the "T2 stale?" renderer in quintessence.findings needs — instead of a pre-rendered
        prose line a consumer would have to regex back apart). Added for P4 (checks.py wires
        this into `qq check`'s XREF section); findings() below is now a thin render of this,
        kept for staleness-xref.py's existing standalone CLI behavior."""
        out, fired = [], 0
        for _he, sim, m, mi, h, hi, verdict in self.candidates():
            if verdict != "FIRE":
                continue
            fired += 1
            if fired > self.max_findings:
                break
            out.append(Finding(cls="T2 stale?", fields={
                "memory": m, "mem_date": mi, "head": h, "head_date": hi, "sim": sim,
            }))
        return out

    def findings(self) -> list:
        return [f.render() for f in self.finding_records()]

    # ---- wave-off mutation --------------------------------------------------------------------
    # Wave-off mutation runs under state_lock, matching persist_findings: the read-modify-write
    # span (load_waveoffs -> mutate -> _persist_waveoffs, and the paired content-store read/
    # save) was, and without this, still would be, genuinely UNLOCKED — two concurrent
    # single-pair waveoffs race exactly like the findings-file write race. This is a real fix,
    # not a bug-for-bug port.
    # `waveoff()` locks only when it's the TOP-LEVEL call (no `_rows`/`_store` passed in) — the
    # nested calls `waveoff_head_all` makes already run under ITS OWN outer lock, and a second
    # `state_lock()` from the same process would self-deadlock (a new fd's flock blocks on the
    # first, even within one process).
    def waveoff(self, mname: str, hname: str, _store=None, _rows=None, _quiet=False) -> str:
        if _rows is None and _store is None:
            from .store import Store, state_lock
            with state_lock(Store(self.config)):
                return self._waveoff_locked(mname, hname)
        return self._waveoff_locked(mname, hname, _store=_store, _rows=_rows, _quiet=_quiet)

    def _waveoff_locked(self, mname: str, hname: str, _store=None, _rows=None,
                         _quiet=False) -> str:
        """The original `waveoff()` body, unchanged — see `waveoff` above for the locking that
        now wraps it."""
        if not os.path.exists(os.path.join(self.memdir, mname + ".md")):
            raise ValueError(f"xref waveoff: no memory file '{mname}'")
        store = _store if _store is not None else self.load_content_store()
        me, miso, head_iso, dirty = self.head_moved_epoch(hname, store)
        if head_iso is None:
            raise ValueError(f"xref waveoff: no HEAD '{hname}'")
        if dirty and _store is None:
            with best_effort_write("xref content store", self.content_path):
                self.save_content_store(store)
        rows = _rows if _rows is not None else self.load_waveoffs()
        rows[(mname, hname)] = miso
        if _rows is None:
            self._persist_waveoffs(rows)
        return f"waved off: {mname} vs {hname} – quiet until {hname}'s content changes"

    def waveoff_head_all(self, hname: str) -> str:
        fired = [(m, h) for _he, _s, m, _mi, h, _hi, v in self.candidates() if v == "FIRE" and h == hname]
        if not fired:
            return f"no FIRING pairs for HEAD '{hname}'"
        from .store import Store, state_lock
        with state_lock(Store(self.config)):
            store = self.load_content_store()
            rows = self.load_waveoffs()
            for m, h in fired:
                self._waveoff_locked(m, h, _store=store, _rows=rows, _quiet=True)
            self._persist_waveoffs(rows)
            with best_effort_write("xref content store", self.content_path):
                self.save_content_store(store)
        return f"waved off {len(fired)} pair(s) for HEAD {hname}: " + ", ".join(m for m, _ in fired)

    def explain(self) -> str:
        rows = self.candidates()
        nfire = sum(1 for r in rows if r[6] == "FIRE")
        out = [f"T2 staleness-xref – sim>={self.sim} head-newer-by>={self.days:g}d max={self.max_findings} "
               f"(env QQ_XREF_SIM/DAYS/MAX); {len(rows)} pair(s) over threshold, "
               f"{nfire} FIRE (top {self.max_findings} queue). First 40 rows (newest-HEAD order):"]
        for _he, sim, m, mi, h, hi, verdict in rows[:40]:
            out.append(f"  {verdict:<9} sim {sim:.3f}  mem {m} ({mi})  vs  HEAD {h} (updated {hi})")
        if len(rows) > 40:
            out.append(f"  (+{len(rows) - 40} more over-threshold pairs not shown)")
        return "\n".join(out)
