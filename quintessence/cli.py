# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.cli — L3: the read-verb renderers behind the `qq` dispatcher.

Each `render_*` function takes already-resolved L0/L1 objects (Store, Config, FindingsFile) and
returns (stdout_text, exit_code) — no argv parsing, no environment reads, no file-path
resolution of its own. The `qq` entry-point script owns argv/env/exit; this module owns
rendering only ("L2 returns structured data; L3 renders it"). Every renderer here is
verified byte-for-byte against the P0 surface-freeze goldens (tests/test-surface-freeze.sh) and
against a live bash `qq-legacy` run, with ONE ratified exception: `render_digest`'s exit code
(see its docstring) always reports success — the legacy `set -e`/pipeline-exit-status bug
(frozen, not reproduced) that could abort the bash script before printing the "last touched"
footer on a store with <=12 HEADs is fixed here, not carried over.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Optional

from .config import Config
from .findings import FindingsFile, parse_line
from .heads import count_update_markers, head_meta, legacy_brief
from .refsview import RefsView
from .store import Store, StorePathError

RESERVED_HEADS = {"RUBRIC", "INDEX"}
DIGEST_MAX = 12   # matches qq digest's `max=12`

# JUDGMENT CALL (flagged for review): the SessionStart findings render was uncapped —
# `qq digest`'s PENDING CONSISTENCY FINDINGS block prints findings.render() in full, and
# QQ_SESSIONSTART_DIGEST=1 injects that whole digest into EVERY session's context. TIER1/XREF/
# AUDIT stay small in practice (auto-resolve; XREF is separately capped at QQ_XREF_MAX=5), but
# SALIENCE is APPEND-only by design (findings.sh's own docstring: "a captured item is cleared by
# editing it out... not auto-resolve") — an unattended queue grows without bound, and every line
# added compounds against every future session's cold-start budget, not just the one that
# surfaced it. 20 is picked from what this render is FOR: a SessionStart orientation glance, not
# the adjudication surface (`qq findings`/`qq findings next` already exist for that, uncapped,
# on request) — 20 terse one-line findings is roughly the same order of magnitude as
# DIGEST_MAX's 12 open-loop topics, generous enough that a genuinely-clean-except-a-few-things
# store never truncates, small enough to bound a pathological queue's context cost. Round-number
# choice, not a measured budget; revisit if it clips a real session's fast-Tier-1 output (T1
# findings before this cap NEVER dropped anything the model needed to act on).
FINDINGS_DIGEST_MAX = 20


# ---- menu -----------------------------------------------------------------------------------
def _compute_menu_text(store: Store) -> str:
    """Port of qq's `reindex_to`: the full topic table."""
    lines = [
        "# Quintessence — exit-note menu",
        "_Pick one to continue (`qq show <topic>`). Saving is automatic; resuming is your choice._",
        "",
        f"{'TOPIC':<26} | {'UPDATED':<20} | ESSENCE",
        f"{'-' * 26}-+-{'-' * 20}-+-------",
    ]
    for slug in store.list_head_slugs():
        text = store.read_head(slug)
        updated, essence = head_meta(text)
        lines.append(f"{slug:<26} | {updated:<20} | {essence}")
    return "\n".join(lines) + "\n"


def render_menu(store: Store) -> str:
    """Port of qq's `menu|index)` case: `[ -f "$INDEX" ] || reindex; cat "$INDEX"`. NOT a pure
    re-render — legacy serves INDEX.md's CACHED content verbatim whenever it already exists
    (which is the common case on a real store: `qq update` does not reindex, only `qq
    finalize`/`qq check --write`/`qq reindex` do — so INDEX.md routinely lags the live HEAD
    content, and `qq menu` faithfully shows that lag). Only a genuinely MISSING INDEX.md is
    regenerated. This was caught by live-store parity testing: an
    earlier version of this function always re-rendered fresh from the HEADs, which matched
    every P0 fixture (INDEX.md never exists there) but diverged from `qq-legacy menu` on the
    real ~/quintessence store, where INDEX.md exists and IS stale. Matches legacy's own
    lock-free, uncommitted local write (this is the one exception to "no writes" in the L3
    read-verb layer, ported verbatim because `qq menu` already does it in production).

    `tools-2` (2026-07-29 review): bare `qq`/`qq menu` is the single most likely first command
    on a fresh machine, and `qq` itself defaults the no-argv case to `menu` — but a store that
    has never been `qq init`'d has no qdir to write INDEX.md into, so the write above raised an
    uncaught FileNotFoundError while every sibling read verb (list/digest/check/show) already
    degrades cleanly, and `doctor` names the fix outright. Checked here, before either the read
    or the write, and reported the same way `doctor` reports it. Raises StorePathError (not a
    traversal in the literal sense, but the same "clean, agent-legible exit-2 refusal" plumbing
    the `qq` dispatcher's top-level `except StorePathError` already wires up) rather than adding
    a new exception type, since this render function has no way to hand the dispatcher a custom
    exit code of its own."""
    if not store.qdir.is_dir():
        raise StorePathError(f"qq menu: store dir missing: {store.qdir}  (run: qq init)")
    if store.index_path.is_file():
        return store.read_index()
    text = _compute_menu_text(store)
    store.index_path.write_text(text, encoding="utf-8")
    return text


# ---- digest ---------------------------------------------------------------------------------
_DATE10_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _epoch_of(updated_raw: str) -> int:
    m = _DATE10_RE.search(updated_raw)
    if not m:
        return 0
    try:
        y, mo, d = (int(x) for x in m.group(0).split("-"))
        return int(datetime(y, mo, d, tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0


def _truncate_essence(ess: str, width: int = 160) -> str:
    return ess[: width - 1] + "…" if len(ess) > width else ess


def _rank_by_epoch(rows: list[tuple[int, str, str]]) -> list[tuple[int, str, str]]:
    """rows: (epoch, slug, essence). Order: epoch descending, matching bash's
    `sort -t$'\\t' -k1,1nr`. GNU sort's tie-break for lines whose key field compares equal is an
    implementation detail of its merge, not a documented total order — rather than guess it (and
    risk silently diverging on a future store shape), shell out to the SAME `sort` binary the
    legacy engine uses, so ties resolve identically forever on any host with coreutils sort."""
    if not rows:
        return []
    payload = "\n".join(f"{e}\t{s}\t{ess}" for e, s, ess in rows)
    proc = subprocess.run(["sort", "-t", "\t", "-k1,1nr"], input=payload,
                           capture_output=True, text=True, check=True)
    out: list[tuple[int, str, str]] = []
    for line in proc.stdout.split("\n"):
        if not line:
            continue
        epoch_s, slug, ess = line.split("\t", 2)
        out.append((int(epoch_s), slug, ess))
    return out


CORPUS_REFS_DIGEST_MAX = 10   # same glance-not-adjudication rationale as FINDINGS_DIGEST_MAX


def _corpus_ref_lines(config: Config) -> list:
    """Corpus-bound render surface: one line per corpus-bound record whose
    referent needs attention — `suspect` (referent moved after binding) or `missing-at-write`
    (born stale; the bind ran inside a hook, so nobody saw the write-time warn). HEAD-bound
    records are NOT listed (brief/show annotate those inline); corpus files have no qq read
    path, so the SessionStart glance is their honest annotation point. Fail-soft: any problem
    reading refs.jsonl = no section."""
    out = []
    try:
        if not config.get_bool("QQ_BIND"):
            return []
        path = os.path.join(config.get_path("QQ_STATE_DIR"), "refs", "refs.jsonl")
        latest: dict = {}   # (corpus,file,kind,id) -> record; last record wins, like RefsView
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or not rec.get("corpus"):
                    continue
                latest[(rec.get("corpus"), rec.get("file"),
                        rec.get("kind"), rec.get("id"))] = rec
        for rec in latest.values():
            status = rec.get("status")
            if status == "suspect":
                since = (rec.get("suspect_asof") or rec.get("asof") or "")[:10]
                out.append(f"  [suspect] {rec.get('corpus')}:{rec.get('file')} -> "
                            f"{rec.get('kind')} {rec.get('id')} (moved since {since})")
            elif status == "missing-at-write":
                # SEED-sourced born-stale is a one-time audit report the seed run hands the
                # operator, NOT a standing banner: the 2026-07-04 live seed surfaced ~120 of
                # them in three benign classes (other-host artifacts probed locally, negative
                # claims whose subject is SUPPOSED to be absent, historical facts) — a digest
                # carrying those every session is the perpetual-annotation anti-pattern D6
                # exists to kill. COMMIT-sourced born-stale still shows: committing a fact
                # file re-vouches its claims, so a dead referent there is fresh drift.
                if str(rec.get("src") or "").startswith("seed:"):
                    continue
                out.append(f"  [born-stale] {rec.get('corpus')}:{rec.get('file')} -> "
                            f"{rec.get('kind')} {rec.get('id')} "
                            f"(absent when bound {(rec.get('asof') or '')[:10]})")
    except Exception:
        return []
    return sorted(out)


def render_digest(store: Store, config: Config, findings: Optional[FindingsFile] = None) -> str:
    """Port of qq's `digest()`. RATIFIED DEVIATION from legacy, recorded with the P0
    surface-freeze goldens: legacy's "last touched" footer is written by code that runs only if
    the ranked-list pipeline's last statement (`[ N -gt 12 ] && printf ...`) exits 0 — under
    `set -e` a store with <= DIGEST_MAX (12) HEADs makes that test FALSE, which aborts the whole
    script before the footer prints (and before `qq` returns 0). That was frozen as pre-existing
    behavior in P0's goldens (digest-plain.golden / digest-over-max-quirk.golden), NOT a design.
    This port fixes it: the footer prints whenever the activity log is non-empty regardless of
    HEAD count, and the caller (`qq` entry point) always exits 0 for `digest`. See
    tests/test-surface-freeze.sh for both the new behavior and a documented comparison against
    qq-legacy's old one."""
    out: list[str] = []
    findings = findings if findings is not None else FindingsFile()
    fcount = findings.count()
    if fcount > 0:
        out.append(
            f"QUINTESSENCE – PENDING CONSISTENCY FINDINGS ({fcount}; review when it suits – an "
            f"ignored finding re-presents next session; [T2 stale?] pairs adjudicated as fine: "
            f"`qq waveoff <memory> <head>` keeps them quiet until the HEAD moves again):")
        lines = findings.render_lines()
        shown, remainder = lines[:FINDINGS_DIGEST_MAX], len(lines) - FINDINGS_DIGEST_MAX
        out.append("\n".join(shown))
        if remainder > 0:
            out.append(f"  (+{remainder} more – `qq findings` for the full list)")
        out.append("")
    ref_lines = _corpus_ref_lines(config)
    if ref_lines:
        out.append(
            f"QUINTESSENCE – CORPUS CLAIMS vs REALITY ({len(ref_lines)}; a memory/doc file's "
            f"bound referent is missing or has moved – fix the file (rebind fires on its "
            f"commit), or waive/retire the record per the refs runbook):")
        shown, remainder = ref_lines[:CORPUS_REFS_DIGEST_MAX], \
            len(ref_lines) - CORPUS_REFS_DIGEST_MAX
        out.append("\n".join(shown))
        if remainder > 0:
            out.append(f"  (+{remainder} more – see $QQ_STATE_DIR/refs/refs.jsonl)")
        out.append("")
    out.append("QUINTESSENCE OPEN LOOPS – most-recently-worked first; "
                "`qq brief <topic>` to resume, `qq menu` for all:")

    pin = config.get_str("QQ_DIGEST_PIN")
    slugs = store.list_head_slugs()
    if pin and pin in slugs:
        _, ess = head_meta(store.read_head(pin))
        out.append(f"  {'MAP':<10} {pin:<28} {_truncate_essence(ess)}")

    now = int(datetime.now(timezone.utc).timestamp())
    rows: list[tuple[int, str, str]] = []
    for slug in slugs:
        if slug == pin:
            continue
        updated, ess = head_meta(store.read_head(slug))
        rows.append((_epoch_of(updated), slug, ess))
    ranked = _rank_by_epoch(rows)

    total = len(slugs)
    for n, (epoch, slug, ess) in enumerate(ranked, start=1):
        if n > DIGEST_MAX:
            continue
        if epoch > 0:
            days = (now - epoch) // 86400
            agestr = "today" if days <= 0 else f"{days}d"
            if days >= 14:
                agestr += "!stale"
        else:
            agestr = "?"
        out.append(f"  {agestr:<10} {slug:<28} {_truncate_essence(ess)}")
    if total > DIGEST_MAX:
        out.append(f"  (+{total - DIGEST_MAX} older – run `qq menu`)")

    actlog = store.state_dir / "activity.log"
    if actlog.is_file() and actlog.stat().st_size > 0:
        lines = actlog.read_text(encoding="utf-8", errors="replace").split("\n")
        lines = [ln for ln in lines if ln]
        seen: list[str] = []
        for ln in reversed(lines):
            parts = ln.split("\t")
            if len(parts) < 2:
                continue
            topic = parts[1]
            if topic not in seen:
                seen.append(topic)
            if len(seen) >= 4:
                break
        if seen:
            out.append(f"last touched: {', '.join(seen)}")

    return "\n".join(out) + "\n"


# ---- list / path ------------------------------------------------------------------------------
def render_list(store: Store) -> str:
    slugs = store.list_head_slugs()
    return "".join(f"{s}\n" for s in slugs)


def render_path(store: Store, topic: str) -> str:
    return f"{store.head_path(topic)}\n"


def render_memdir(store: Store) -> str:
    """`qq memdir` — the atomic-fact memory directory of the WRITE-TARGET store (the project
    store when run inside a project, else the user store; `--global` forces the user store).
    The agent-facing entry point for the multi-store memory location: a skill/agent
    writes a project's facts under this dir instead of guessing the path."""
    return f"{store.memdir}\n"


def render_fact(store: Store, name: str) -> str:
    """`qq fact <name>` — print an atomic memory FACT verbatim (READ-ONLY). The memory store is
    the atomic-fact companion to HEADs; unlike HEADs it has NO qq write-path (facts are curated
    out-of-band), so this is a read-only viewer, deliberately DISTINCT from `qq show` (HEADs) — the
    boundary that stops a model from generalising to `qq update <fact>`. `<name>` is the slug as
    `qq search` emits it (with or without the `.md` suffix). On a miss, suggest near names."""
    slug = name[:-3] if name.endswith(".md") else name
    p = store.memdir / f"{slug}.md"
    if not p.is_file():
        names = [q.stem for q in sorted(store.memdir.glob("*.md"))] if store.memdir.is_dir() else []
        near = difflib.get_close_matches(slug, names, n=3)
        hint = ("  Did you mean: " + ", ".join(near) + "?") if near else ""
        return f"----- no memory fact '{slug}' (in {store.memdir}) -----{hint}\n"
    return p.read_text()


# ---- show / brief -----------------------------------------------------------------------------
# B2 read-side revalidation rides both verbs via RefsView: rendered
# '> updated:' lines whose bound referents moved get a '⚠ referent changed since written: ...'
# line inserted after them. ANNOTATION ONLY — RefsView is read-only by construction (the B2
# hard constraint: no status transitions, no refs.jsonl writes from a read path), fail-soft,
# and byte-identical for a HEAD with no refs (the parity gate the goldens pin).
def _head_hint(store: Store, topic: str, slugs: "list[str] | None" = None) -> str:
    if slugs is None:
        slugs = store.list_head_slugs()
    near = difflib.get_close_matches(topic, slugs, n=3)
    return ("  Did you mean: " + ", ".join(near) + "?") if near else ""


def render_show(store: Store, topics: list[str]) -> str:
    view = RefsView(store.config)
    parts = []
    slugs = None
    for t in topics:
        if store.has_head(t):
            parts.append(f"===== quintessence: {t} =====\n")
            parts.append(view.annotate_text(t, store.read_head(t)))
            parts.append("\n")
        else:
            if slugs is None:
                slugs = store.list_head_slugs()
            hint = _head_hint(store, t, slugs)
            parts.append(f"----- no HEAD for '{t}' (skipping) -----{hint}\n")
    return "".join(parts)


def brief_one(store: Store, topic: str, view: "Optional[RefsView]" = None) -> str:
    f = store.head_path(topic)
    if f.is_file():
        text = store.read_head(topic)
        tot, printed = legacy_brief(text)
        if view is None:
            view = RefsView(store.config)
        printed = view.annotate_lines(topic, printed)
        header = (f"===== quintessence: {topic} (BRIEF – newest 3 of {tot} update-lines + "
                  f"RE-ENTER; full: qq show {topic}) =====")
        body = "\n".join([header] + printed)
        return body + "\n\n"
    hint = _head_hint(store, topic)
    return f"----- no HEAD for '{topic}' (skipping) -----{hint}\n"


def render_brief(store: Store, topics: list[str]) -> str:
    view = RefsView(store.config)   # one refs.jsonl read for the whole multi-topic render
    return "".join(brief_one(store, t, view=view) for t in topics)


# ---- findings -------------------------------------------------------------------------------
def render_findings_list(findings: FindingsFile) -> str:
    lines = findings.render_lines()
    return "\n".join(lines) + "\n" if lines else ""


# ---- check ------------------------------------------------------------------------------------
def render_check_readonly(tier1_lines: list[str], xref_lines: list[str], skip_xref: bool) -> str:
    """Port of qq-legacy `check)`'s non-`--write` branch: print the fast (T1 + config-drift)
    block, then the slow (T2 xref) block if either is non-empty; a bare "consistent" message
    (worded per whether xref ran at all) only when NEITHER produced anything. `xref_lines` is
    always a plain list here (skipped and failed both collapse to [] before this call — see the
    `qq` dispatcher, which is the one place that still knows WHY it's empty for the --write
    summary line below)."""
    parts: list[str] = []
    if tier1_lines:
        parts.append("\n".join(tier1_lines) + "\n")
    if xref_lines:
        parts.append("\n".join(xref_lines) + "\n")
    if not tier1_lines and not xref_lines:
        parts.append(("qq check: no Tier-1 findings (xref skipped) – consistent.\n" if skip_xref
                       else "qq check: no Tier-1 / T2-xref findings – consistent.\n"))
    return "".join(parts)


def render_check_write_summary(tier1_count: int, xref_count: Optional[int], findings_path: str) -> str:
    """Port of qq-legacy `check --write`'s final summary line. `xref_count` is None when the
    xref step was skipped OR failed (both print the literal "skipped" qq-legacy itself prints in
    both cases — a pre-existing legacy wart, not one this port introduces; see the P4 report)."""
    xcnt = "skipped" if xref_count is None else str(xref_count)
    return f"qq check: {tier1_count} Tier-1 + {xcnt} T2-xref finding(s) → {findings_path}\n"


_BACKTICK_RE = re.compile(r"`([^`]+)`")


def render_findings_next(store: Store, config: Config, findings: FindingsFile) -> str:
    """Port of qq's `findings_next`: full adjudication context for the topmost pending finding
    (findings_render order: TIER1 then XREF then AUDIT then SALIENCE). Uses the STRUCTURED
    Finding record (quintessence.findings) rather than re-deriving fields from the rendered
    prose by regex — the one exception is the generic branch's "ready commands"
    extraction, which really is "whatever backtick-quoted command the finding's OWN prose
    already names," not a field any producer emits.

    Built by direct string concatenation (not a "\\n".join of lines) because this renderer
    embeds two kinds of pre-formatted multi-line blob (a `cat`-equivalent raw memory-file read,
    and brief_one()'s own output) whose trailing-newline count is already exactly right — join
    would silently add an extra blank line at each such boundary."""
    lines = findings.render_lines()
    if not lines:
        return "qq findings next: no pending findings – clean.\n"
    line = lines[0]
    finding = parse_line(line)
    out = line + "\n" + "\n"   # echo "$line"; echo

    if finding.cls == "T2 stale?" and {"memory", "head"} <= finding.fields.keys():
        m = finding.fields["memory"]
        h = finding.fields["head"]
        kb_root = store.config.get_path("QQ_KB_ROOT")
        mpath = os.path.join(kb_root, "memory", f"{m}.md")
        out += f"----- memory: {m} (full body) -----\n"
        if os.path.isfile(mpath):
            with open(mpath, encoding="utf-8", errors="replace") as fh:
                out += fh.read()
        else:
            out += f"(memory file not found at {mpath})\n"
        out += "\n"
        out += brief_one(store, h)
        out += "to resolve:\n"
        out += f"  compatible, not stale:  qq waveoff {m} {h}\n"
        out += f"  genuinely stale:        edit {mpath}, then re-run: qq check --write\n"
        return out

    if finding.cls == "T1 size" and "head" in finding.fields:
        h = finding.fields["head"]
        if store.has_head(h):
            text = store.read_head(h)
            size = len(text.encode("utf-8"))
            n_upd = count_update_markers(text)
            out += f"HEAD {h}: {size} bytes total, {n_upd} update-lines\n"
            out += "\n"
        out += "to resolve:\n"
        if finding.fields.get("kind") == "update-lines":
            out += f"  qq compact {h}\n"
        else:
            out += (f"  condense the ## sections in {h} by hand (qq rewrite); read cheaply "
                    f"meanwhile: qq brief {h}\n")
        return out

    cmds = _BACKTICK_RE.findall(line)
    out += "to resolve:\n"
    for c in cmds:
        out += f"  {c}\n"
    out += "  or: edit the source file directly, then re-run: qq check --write\n"
    return out


# ---- usage ------------------------------------------------------------------------------------
USAGE_TEXT = """\
qq – quintessence plumbing: session-continuity HEADs (menu/show/finalize). The model
writes HEAD content (per RUBRIC.md); qq handles the mechanical parts.

usage: qq [command] [args]                     (no command = menu)

 READ / RESUME
  menu                     full topic menu: every HEAD with updated-time + essence (alias: index)
  digest                   compact recency-ranked open-loops view (SessionStart orientation)
  findings [next|list]     pending consistency findings: bare list (`list` = same), or `next` = one finding with
                            full adjudication context (memory body / HEAD brief / ready commands)
  list                     bare topic names
  show <topic> [t2 ...]    print full HEAD(s) (alias: load; --unredacted lifts withheld-topics refusal)
  brief <topic> [t2 ...]   condensed HEAD: newest 3 update-lines + RE-ENTER section (--unredacted ditto)
  search <query>           associative (semantic) recall over HEADs/docs/memory
  ask <question>           retrieval-as-QA: cited answer from a local model over HEADs/docs/memory
                            (--transcripts: opt-in session-transcript corpus, not the live store)
  fact <name>              print an atomic memory FACT verbatim (read-only; the `qq fact …` a
                            search hit shows). Distinct from `show` (HEADs) – facts have no qq write.

 WRITE  (one tool – no separate binary; the common op is the safe default)
  update <topic> [text]    add an update-line (merge-safe; THE common write; text arg or stdin)
  essence <topic> <text>   set/refresh the one-line essence (creates it if absent)
  new <topic> [essence]    scaffold a fresh HEAD
  rewrite <topic>          RARE whole-file replace from stdin (auto-guarded against concurrent edits;
                            refuses a FUTURE-stamped '> updated:' line – pass --allow-future for a
                            deliberate timestamp repair/migration)
  --ref kind:id            (on any write verb above, repeatable) bind an explicit reality-binding
                            referent the prose doesn't name; kinds: file/git/unit/probe/port/rfile

 LIFECYCLE / MAINTENANCE
  finalize <topic>         snapshot HEAD -> journal + reindex + mirror (aliases: checkpoint, save; does NOT close)
  compact <topic> [N]      fold old update-lines to journal, keep newest N (default 5); body kept
  delete <topic>           retire a HEAD: journal its final state, then remove it (recoverable; alias: rm)
  check [--write] [--fast] consistency findings: fast T1 (deterministic) first, then slow T2 staleness-xref (--write queues; --fast/--no-xref skips the xref)
  reconcile [--explain]    config-drift only: live config knobs vs HEAD essences + memory facts + docs (read-only)
  waveoff <memory> <head>  adjudicate a [T2 stale?] pair compatible (or `--head <head>` to bulk-clear)
  reindex                  regenerate INDEX.md from the HEADs
  path <topic>             print the HEAD's file path (resolved across the search path)
  memdir [--global]        print the write-target store's atomic-fact memory dir (where a project's facts go)
  init [dir]               create the USER store (git + write-path hooks) at dir or $QUINTESSENCE_DIR; records it as the global default
  init --project [dir]     create a PROJECT store at $PWD/.quintessence (or <dir>/.quintessence); NOT recorded globally – found by walk-up from cwd
  doctor                   preflight: check deps, store, corpus, and the (optional) embedder + ask endpoint
  config [get|set|...]     read/write the per-install config file (env > file > default)

Docs: ONBOARDING.md (beside this script).
Retired with the bash engine: the standalone `qq-write` binary – no binary of that name exists
  any more; its functionality lives in `qq update`/`qq rewrite` above. (`qq-search`/`qq-ask`
  still exist beside this script, but only as internal exec targets for `qq search`/`qq ask`
  and the hooks – not documented entry points of their own.)
"""


def render_usage() -> str:
    return USAGE_TEXT
