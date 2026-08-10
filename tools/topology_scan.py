#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""One-shot corpus-topology scan over the quintessence store (proposal #5 cheapest-test).

READ-ONLY: never writes into the HEAD store (QUINTESSENCE_DIR) or the memory dir (QQ_MEMDIR).
Uses quintessence.search.SearchIndex (warm embedding cache -> no re-embedding) for vectors, and
direct file reads (via the same kb/ symlink paths SearchIndex uses) for link/date/size
analysis, so the link graph reflects full file text rather than reassembled chunks.

LINK RESOLUTION (fixed 2026-07-03, clean-room P1): this scan used to resolve [[links]] by
EXACT FILENAME match only, which disagreed with `qq run_check`'s richer resolution (filenames
+ `name:` frontmatter + type-prefix-stripping + hyphen/underscore/case normalization + kb-doc
basenames) and produced 144 false "broken/variant link" findings on 2026-07-02 — adjudicated
that same night as the scanner being wrong, not the links. There is ONE SlugResolver and
everything imports it, so this script now imports `quintessence.slugs.SlugResolver` — THE resolution semantics, promoted from run_check itself —
instead of reimplementing an approximation of it. See §1a below: `variant_links` (a distinct
filename that only resolves after normalization) is now expected to be near-empty, because
SlugResolver resolves those cases directly; only genuinely nonexistent targets remain in
`missing_links`.

P4.5 (namespace design ruling, 2026-07-03): SlugResolver itself was reworked to resolve
per-tier (store-first authoritative, kb-doc a separate namespace, a same-basename cross-tier
match now an explicit ambiguity rather than silent-invisible or a false "collision"). This
script's own `ambiguous` section already asked resolve()/resolve_all() the right question
(resolve() is None but resolve_all() nonempty -> ambiguous, not missing) — that logic is
UNCHANGED here; it inherits the corrected semantics for free because it imports the resolver
rather than reimplementing it (the whole point of having one resolver).

P4.5 ALSO fixes this script's other two long-standing warts (flagged in the P4 acceptance
review, not previously actioned):
  1. Embedding used to go through the LEGACY `qqsearch_core` module (`import qqsearch_core as
     qc`), which has no `QQ_EMBED_NUM_GPU` hook at all — the GPU option genuinely could not
     reach this script's embed calls regardless of config. Repointed onto
     `quintessence.search.SearchIndex`, which resolves ALL its config (including
     QQ_EMBED_NUM_GPU) from a `quintessence.config.Config` — this script now
     benefits from the same GPU hook `qq search`/`qq ask` already have.
  2. SAMPLE_K was a hardcoded module constant (always 3). It's now `--sample-k N` (0 = every
     chunk, unsampled — full cohesion/similarity fidelity, but a real full-corpus embed if the
     cache is cold; see the CLI help). Default stays 3 for cadence/cost compatibility with every
     prior run of this script.
  3. The lowest-cohesion section used to report ONE mean-pairwise-cosine number per HEAD and
     stop there — not enough for a split adjudication to rule on ("low cohesion" doesn't say
     WHERE the HEAD wants to split). It now also runs a stdlib-only greedy agglomerative
     clustering over each low-cohesion HEAD's own chunk vectors and reports the proposed
     cluster groupings BY SECTION TITLE, so a split verdict has actual structure to look at
     (see `cluster_chunks`).

Per the P4.5 brief: this script does NOT run a full (`--sample-k 0`) store scan itself beyond
what parity/testing needs — the reviewing judge runs the real unsampled pass after acceptance.

stdlib-only (aside from the quintessence package itself, which is also stdlib-only).
"""
from __future__ import annotations
import argparse
import os, re, sys, math, datetime, statistics, hashlib, tempfile
import concurrent.futures as cf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quintessence import heads  # noqa: E402
from quintessence.config import Config  # noqa: E402
from quintessence.search import SearchIndex  # noqa: E402
from quintessence.slugs import SlugResolver, normalize as slug_normalize  # noqa: E402
from quintessence.store import Store  # noqa: E402

# syntax/doc-example tokens (mentions of the [[link]] syntax itself), not real references —
# ported verbatim from run_check's STOP list so this scan doesn't report e.g. a HEAD's own
# `[[links]]` example mention as a missing target (the ISM HEAD's ruling explicitly calls this
# out: "ignore meta-usage like the two [[links]] hits").
STOP_TOKENS = {"links", "link", "slug", "name", "their-name", "topic", "foo-bar", "bar"}

DEFAULT_SAMPLE_K = 3   # first-K chunks per HEAD used for vectors when sampling; 0 = unsampled.
                       # kept as the CLI default so every prior invocation of this script (and
                       # its cost/cadence expectations) is unaffected unless --sample-k is given.

# ---- clustering (new in P4.5: STRUCTURE for a split adjudication) ---------------------------
DEFAULT_MAX_CLUSTERS = 3     # don't propose splitting into more pieces than this by default —
                             # enough to see genuine multi-topic structure without over-fragmenting
DEFAULT_STOP_SIM = 0.75      # stop merging once the best remaining merge is already this cohesive
                             # (what's left doesn't need to be split further)


def cluster_chunks(vecs, cosine=None, max_clusters=DEFAULT_MAX_CLUSTERS, stop_sim=DEFAULT_STOP_SIM):
    """Greedy agglomerative (average-linkage) clustering over a HEAD's own chunk vectors,
    stdlib-only. Starts with every chunk as its own singleton cluster; repeatedly merges the
    PAIR of clusters with the highest average pairwise cosine ("average linkage") until either
    the best remaining merge similarity drops below `stop_sim` (what's left is already cohesive
    enough that forcing a further merge would blur genuinely distinct groups) or only
    `max_clusters` clusters remain (a ceiling so a large, noisy HEAD doesn't propose a dozen
    micro-clusters). O(n^2 . k) over n chunks and k merge steps — fine at the chunk counts a
    single HEAD has (tens, not thousands).

    Returns a list of clusters, each a list of ORIGINAL VECTOR INDICES (0-based, in `vecs`'
    order), sorted by each cluster's lowest index — i.e. in first-appearance order, so a
    reader sees a HEAD's own top-to-bottom structure rather than a reshuffled one. A HEAD with
    < 2 chunks returns a single one-cluster (or empty) list — nothing to cluster."""
    cosine = cosine or SearchIndex.cosine
    n = len(vecs)
    if n == 0:
        return []
    if n == 1:
        return [[0]]
    max_clusters = max(1, min(max_clusters, n))
    clusters = [[i] for i in range(n)]

    def avg_sim(a, b):
        return statistics.mean(cosine(vecs[i], vecs[j]) for i in a for j in b)

    while len(clusters) > max_clusters:
        best = None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                s = avg_sim(clusters[i], clusters[j])
                if best is None or s > best[0]:
                    best = (s, i, j)
        if best is None or best[0] < stop_sim:
            break
        _, i, j = best
        merged = clusters[i] + clusters[j]
        clusters = [c for k, c in enumerate(clusters) if k not in (i, j)] + [merged]
    clusters.sort(key=lambda c: min(c))
    return clusters


def head_vectors(idx: SearchIndex, sample_k: int, workers: int = 6):
    """Vectors (+ section titles) for a HEAD's chunks, scoped to the `qq` source only. cache
    where present, embed only misses in parallel.

    sample_k == 0: EVERY chunk (unsampled) — full cohesion/similarity fidelity, but a real
        embed of the whole `qq` (+ implicitly `mem`, since both are scanned for the corpus-scope
        filter below) corpus if the identity-scoped cache is cold; the judge's pass, not the
        default of this script.
    sample_k > 0: the first K chunks per HEAD in file order (top-of-file: update-lines +
        essence + RE-ENTER — a HEAD's core) — DEGRADED MODE, kept as the default for cost/
        cadence reasons unchanged from every prior run of this script.

    Returns (slug -> [vec, ...], slug -> [title, ...] (same order/length as the vector list),
    slug -> total_chunk_count)."""
    cache = idx._load_cache()
    sampled: dict[str, list[tuple[str, str, str]]] = {}   # slug -> [(key, title, text), ...]
    total_chunks: dict[str, int] = {}
    for label, path, title, text in idx.chunks():
        if label != "qq":
            continue
        slug = os.path.basename(path)[:-3]
        total_chunks[slug] = total_chunks.get(slug, 0) + 1
        lst = sampled.setdefault(slug, [])
        if sample_k == 0 or len(lst) < sample_k:
            key = hashlib.sha256(f"{idx.embed_model}\0{text}".encode()).hexdigest()
            lst.append((key, title, text))
    todo = {key: text for lst in sampled.values() for key, _title, text in lst if key not in cache}
    print(f"sampled {sum(len(v) for v in sampled.values())} chunks over {len(sampled)} HEADs "
          f"(sample_k={sample_k or 'unsampled'}); {len(todo)} cache-misses to embed", flush=True)
    done = 0
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(idx.embed, text, idx._doc_prefix()): key for key, text in todo.items()}
        for fut in cf.as_completed(futs):
            vec = fut.result()
            if vec is not None:
                cache[futs[fut]] = vec
            done += 1
            if done % 50 == 0:
                idx._save_cache(cache)
                print(f"embed: {done}/{len(todo)}", flush=True)
    if todo:
        idx._save_cache(cache)
    print(f"embed: complete ({done}/{len(todo)})", flush=True)
    by_head, by_head_titles, missing = {}, {}, 0
    for slug, lst in sampled.items():
        vecs = [cache[k] for k, _t, _tx in lst if k in cache]
        titles = [t for k, t, _tx in lst if k in cache]
        missing += len(lst) - len(vecs)
        if vecs:
            by_head[slug] = vecs
            by_head_titles[slug] = titles
    if missing:
        print(f"WARNING: {missing} sampled chunks lack a vector (embed failures)", flush=True)
    return by_head, by_head_titles, total_chunks


HOME = os.path.expanduser("~")
TODAY = datetime.datetime(2026, 7, 2, tzinfo=datetime.timezone.utc)
STALE_DAYS = 30
SIZE_LIMIT = 32 * 1024  # 32kB

LINK_RE = re.compile(r"\[\[([^\]|#]+)")


def parse_iso(s):
    try:
        d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d


def load_files(kb_root: str, exclude, label: str):
    """(slug -> abspath) for top-level *.md files SearchIndex would index under this label,
    excluding the derived-listing files it itself excludes (INDEX.md/MEMORY.md)."""
    root = os.path.join(kb_root, {"qq": "quintessence", "mem": "memory"}[label])
    out = {}
    for n in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        full = os.path.join(root, n)
        if not n.endswith(".md") or not os.path.isfile(full):
            continue
        if any(x in full for x in exclude):
            continue
        out[n[:-3]] = full
    return out


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--sample-k", type=int, default=DEFAULT_SAMPLE_K, metavar="N",
                    help=f"chunks sampled per HEAD for the similarity/cohesion sections; "
                         f"0 = unsampled (every chunk — a real full-corpus embed if the cache "
                         f"is cold; the judge's pass, not routine use). Default {DEFAULT_SAMPLE_K}.")
    return p.parse_args(argv)


def main():
    args = parse_args()
    sample_k = args.sample_k

    base_cfg = Config()
    kb_root = base_cfg.get_path("QQ_KB_ROOT")
    cfg = Config(overrides={
        "QUINTESSENCE_DIR": os.path.join(kb_root, "quintessence"),
        "QQ_MEMDIR": os.path.join(kb_root, "memory"),
        "QQ_KB_ROOT": kb_root,
    })
    idx = SearchIndex(cfg)

    qq_files = load_files(kb_root, idx.exclude, "qq")
    mem_files = load_files(kb_root, idx.exclude, "mem")
    all_files = {("qq", s): p for s, p in qq_files.items()}
    all_files.update({("mem", s): p for s, p in mem_files.items()})
    slug_index = {}  # slug -> (label, slug) key into all_files/body/outlinks ; flag collisions
    collisions = []
    for (label, slug), path in all_files.items():
        if slug in slug_index and slug_index[slug][0] != label:
            collisions.append(slug)
        slug_index.setdefault(slug, (label, slug))

    # ---- SlugResolver: THE resolution semantics (promoted from qq run_check; per-tier as of
    # the P4.5 namespace design ruling — see this module's docstring) --------------------------
    # Configured over the SAME kb_root this scan already loaded, with kb-doc resolution left ON
    # (default) — the adjudication ruling explicitly says kb/docs targets (e.g.
    # [[some-runbook-doc]] -> a doc under the configured kb docs source) count as resolvable,
    # not missing, even though they aren't nodes in THIS scan's qq/mem-only graph.
    resolver = SlugResolver(Store(cfg))
    _SOURCE_TO_LABEL = {"head": "qq", "memory": "mem"}   # SlugResolver source -> this scan's label

    # ---- read file bodies, extract links / size / newest update-line ----
    body = {}       # (label,slug) -> text
    size = {}        # (label,slug) -> bytes
    newest_update = {}  # (label,slug) -> datetime or None
    outlinks = {}    # (label,slug) -> set of resolved (label,slug) targets IN THIS SCAN'S graph
    unresolved = []  # list of (src_label, src_slug, raw_target) — SlugResolver found NOTHING
    ambiguous = []   # list of (src_label, src_slug, raw_target, [SlugMatch, ...]) — >1 distinct file
    inbound = {k: set() for k in all_files}  # (label,slug) -> set of (label,slug) sources

    for key, path in all_files.items():
        label, slug = key
        try:
            txt = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            txt = ""
        body[key] = txt
        size[key] = os.path.getsize(path)
        # the one reader (quintessence.heads), not a private regex — memory files have no
        # update-lines and simply yield an empty list here, exactly as the regex did
        dates = [parse_iso(it.timestamp) for it in heads.update_lines(txt) if it.timestamp]
        dates = [d for d in dates if d is not None]
        newest_update[key] = max(dates) if dates else None
        targets = set(m.strip() for m in LINK_RE.findall(txt))
        resolved = set()
        for t in targets:
            if slug_normalize(t) in STOP_TOKENS:
                continue   # syntax/doc-example mention, not a real reference (see STOP_TOKENS)
            match = resolver.resolve(t)
            if match is None:
                cands = resolver.resolve_all(t)
                if cands:
                    ambiguous.append((label, slug, t, cands))
                else:
                    unresolved.append((label, slug, t))
                continue
            graph_label = _SOURCE_TO_LABEL.get(match.source)
            tgt_key = (graph_label, match.slug) if graph_label else None
            # a kb-doc match (or a head/memory match outside THIS scan's all_files, e.g. a
            # differently-scoped kb symlink) is correctly "resolved", just not a graph node here.
            if tgt_key is not None and tgt_key in all_files and tgt_key != key:
                resolved.add(tgt_key)
                inbound[tgt_key].add(key)
        outlinks[key] = resolved

    # ---- missing_links: genuinely nonexistent targets (SlugResolver found nothing at all) --
    # `variant_links` (kept for report-shape continuity) is now expected to be near-empty:
    # SlugResolver already resolves the hyphen/underscore + prefix-stripped + name:-field cases
    # directly (that resolution happened above, silently, as a normal `resolve()` hit) — this
    # section is what's LEFT after the real fix, not a re-approximation of it.
    variant_links = []   # (src_label, src_slug, raw, intended slug) — kept empty by construction now
    missing_links = [(label, slug, t) for label, slug, t in unresolved]

    # ---- orphan HEADs: qq HEADs with zero inbound edges from any other file ----
    # intent-inbound: broken variant links whose probable target is this file — an orphan
    # that would NOT be an orphan if the naming drift were fixed gets flagged, not hidden.
    intent_inbound = {}
    for label, slug, t, pt in variant_links:
        if (label, slug) != slug_index.get(pt):
            intent_inbound.setdefault(pt, set()).add((label, slug))
    orphans = []
    for key in qq_files:
        k = ("qq", key)
        if not inbound[k]:
            age_days = (TODAY - newest_update[k]).days if newest_update[k] else None
            orphans.append((k, size[k], age_days, len(intent_inbound.get(key, ()))))
    orphans.sort(key=lambda x: -x[1])

    # ---- vectors: sampled (or unsampled) per-HEAD embedding, via SearchIndex ----
    by_head, by_head_titles, total_chunks = head_vectors(idx, sample_k)

    def mean_vec(vecs):
        n = len(vecs)
        dim = len(vecs[0])
        return [sum(v[i] for v in vecs) / n for i in range(dim)]

    head_mean = {s: mean_vec(vs) for s, vs in by_head.items() if vs}

    # top-10 most-similar HEAD pairs
    slugs = sorted(head_mean)
    pairs = []
    for i in range(len(slugs)):
        for j in range(i + 1, len(slugs)):
            a, b = slugs[i], slugs[j]
            sim = idx.cosine(head_mean[a], head_mean[b])
            pairs.append((sim, a, b))
    pairs.sort(key=lambda x: -x[0])
    top_pairs = pairs[:10]

    def link_status(a, b):
        ka, kb = ("qq", a), ("qq", b)
        fwd = kb in outlinks.get(ka, set())
        bwd = ka in outlinks.get(kb, set())
        if fwd and bwd:
            return "linked (mutual)"
        if fwd or bwd:
            return "linked (one-way, %s -> %s)" % ((a, b) if fwd else (b, a))
        return "UNLINKED"

    # internal dispersion (mean pairwise cosine among a HEAD's sampled chunks); need >=2
    cohesion = {}
    for slug, vecs in by_head.items():
        if len(vecs) < 2:
            continue
        sims = []
        for i in range(len(vecs)):
            for j in range(i + 1, len(vecs)):
                sims.append(idx.cosine(vecs[i], vecs[j]))
        cohesion[slug] = (statistics.mean(sims), len(vecs), total_chunks.get(slug, len(vecs)))
    lowest_cohesion = sorted(cohesion.items(), key=lambda kv: kv[1][0])[:5]

    # ---- STRUCTURE (new in P4.5): per-chunk clustering for the lowest-cohesion HEADs, so a
    # split adjudication has proposed groupings (by section title) to rule on, not just a bare
    # "this HEAD's cohesion is low" number. ----
    structure = {}   # slug -> list of clusters, each a list of titles
    for slug, (_m, _n, _tot) in lowest_cohesion:
        vecs = by_head.get(slug, [])
        titles = by_head_titles.get(slug, [])
        if len(vecs) < 3:
            continue   # nothing meaningful to split with <3 sampled chunks
        clusters = cluster_chunks(vecs, cosine=idx.cosine)
        if len(clusters) < 2:
            continue   # clustering collapsed back to one group -- no split signal at this sample
        structure[slug] = [[titles[i] if i < len(titles) else f"(chunk {i})" for i in c]
                            for c in clusters]

    # ---- size / staleness outliers (qq HEADs only, per task) ----
    outliers = []
    for slug in qq_files:
        k = ("qq", slug)
        sz = size[k]
        nu = newest_update[k]
        age = (TODAY - nu).days if nu else None
        big = sz > SIZE_LIMIT
        stale = age is not None and age > STALE_DAYS
        no_date = nu is None
        if big or stale or no_date:
            outliers.append((slug, sz, age, big, stale, no_date))
    outliers.sort(key=lambda x: -x[1])

    # ---- write report ----
    # was hardcoded to a one-off session's scratchpad path; QQ_TOPOLOGY_OUT_DIR overrides,
    # else a generic tempdir location (no deployment-specific/session-specific path baked in).
    out_dir = os.environ.get("QQ_TOPOLOGY_OUT_DIR") or os.path.join(tempfile.gettempdir(), "qq-topology-scan")
    os.makedirs(out_dir, exist_ok=True)
    lines = []
    A = lines.append
    A("# Corpus topology report — quintessence store (qq + mem)")
    A("")
    sample_desc = "unsampled (every chunk)" if sample_k == 0 else f"first {sample_k} chunks"
    A(f"Generated {TODAY.date().isoformat()} (one-shot, proposal #5 cheapest-test). "
      f"Read-only over `qq` (HEADs, {len(qq_files)} files) + `mem` (memory facts, "
      f"{len(mem_files)} files). Link/size/staleness sections (1a/1b/3) are computed over "
      f"FULL file text — exact. Similarity sections (2a/2b) use `quintessence.search."
      f"SearchIndex` ({sample_desc} per HEAD — pass `--sample-k 0` for the full-fidelity, "
      f"unsampled pass; that is a real full-corpus embed if the cache is cold, so it's the "
      f"judge's pass, not this scan's default). Vectors: "
      f"{sum(len(v) for v in by_head.values())} chunks over {len(by_head)} HEADs.")
    if collisions:
        A(f"\n**Slug collisions across qq/mem (ambiguous link targets):** {collisions}")
    A("")

    # -- 1a unresolved links --
    A("## 1a. Unresolved [[links]]")
    A(f"Total link instances parsed: {sum(len(LINK_RE.findall(body[k])) for k in all_files)}. "
      f"Resolution now uses `quintessence.slugs.SlugResolver` — THE resolution semantics "
      f"promoted from `qq run_check` (filenames + `name:` frontmatter + type-prefix-stripping "
      f"+ hyphen/underscore/case normalization + kb-doc basenames), per-tier as of the P4.5 "
      f"namespace design ruling (store resolution is authoritative; a same-basename kb doc no "
      f"longer collides with an exact store hit). Genuinely unresolved: {len(missing_links)} "
      f"instances across {len(set(t for _, _, t in missing_links))} distinct targets.")
    A("")
    if ambiguous:
        A(f"**{len(ambiguous)} link instance(s) are AMBIGUOUS** — the normalized target matches "
          f"more than one DISTINCT real file (a genuine slug collision, not a symlink-farm "
          f"mirror, which SlugResolver already dedups):")
        A("")
        A("| source | raw target | candidates |")
        A("|---|---|---|")
        for label, slug, t, cands in sorted(ambiguous, key=lambda x: (x[2], x[0], x[1])):
            cand_s = ", ".join(f"{c.source}:{c.slug}" for c in cands)
            A(f"| {label}:{slug} | `[[{t}]]` | {cand_s} |")
        A("")
    if missing_links:
        A("### Genuinely missing targets (SlugResolver found no candidate at all)")
        A("| source | raw target |")
        A("|---|---|")
        for label, slug, t in sorted(missing_links, key=lambda x: (x[2], x[0], x[1])):
            A(f"| {label}:{slug} | `[[{t}]]` |")
        A("")
    if not missing_links and not ambiguous:
        A("None — every wikilink in the corpus resolves unambiguously.")
        A("")

    # -- 1b orphan HEADs --
    A("## 1b. Orphan HEADs (zero inbound [[link]] from any other HEAD or memory)")
    A(f"{len(orphans)} of {len(qq_files)} HEADs have no inbound link. Sorted by size (desc).")
    A("")
    A("| HEAD | size (bytes) | newest update-line age (days) | >30d stale | broken variant links pointing here |")
    A("|---|---|---|---|---|")
    for (lb, slug), sz, age, n_intent in orphans:
        age_s = f"{age}d" if age is not None else "no update-line"
        stale_s = "YES" if (age is not None and age > STALE_DAYS) else ("n/a" if age is None else "no")
        A(f"| {slug} | {sz} | {age_s} | {stale_s} | {n_intent or ''} |")
    A("")

    # -- 2a similarity pairs --
    A(f"## 2a. Top-10 most-similar HEAD pairs (mean vector over {sample_desc})")
    A("")
    A("| rank | HEAD A | HEAD B | cosine | link status |")
    A("|---|---|---|---|---|")
    for rank, (sim, a, b) in enumerate(top_pairs, 1):
        A(f"| {rank} | {a} | {b} | {sim:.4f} | {link_status(a, b)} |")
    A("")
    unlinked_hi_sim = [(sim, a, b) for sim, a, b in top_pairs if link_status(a, b) == "UNLINKED"]
    if unlinked_hi_sim:
        A(f"**{len(unlinked_hi_sim)} of the top-10 pairs are UNLINKED despite high similarity** "
          "— merge/link candidates: " +
          "; ".join(f"{a}~{b} ({sim:.3f})" for sim, a, b in unlinked_hi_sim))
    else:
        A("All top-10 pairs are already linked — no unlinked-but-similar candidates surfaced "
          "in the top decile.")
    A("")

    # -- 2b cohesion + STRUCTURE --
    A(f"## 2b. Five lowest-cohesion HEADs (mean pairwise cosine among own sampled chunks — split candidates)")
    A(f"({len(cohesion)} multi-chunk HEADs scored on {sample_desc}; single-chunk HEADs "
      f"excluded — cohesion undefined.{'' if sample_k == 0 else ' INDICATIVE ONLY at this sample size.'})")
    A("")
    A("| HEAD | mean internal cosine | chunks sampled / total |")
    A("|---|---|---|")
    for slug, (m, n, tot) in lowest_cohesion:
        A(f"| {slug} | {m:.4f} | {n} / {tot} |")
    A("")
    if structure:
        A("### STRUCTURE — proposed cluster groupings for a split adjudication")
        A("Greedy agglomerative (average-linkage) clustering over each HEAD's own sampled chunk "
          f"vectors (stdlib only; see `cluster_chunks`), capped at {DEFAULT_MAX_CLUSTERS} "
          f"clusters, stopping early once the best remaining merge is already >= "
          f"{DEFAULT_STOP_SIM:.2f} cohesive. Section titles are each cluster's own chunk titles "
          "(top-to-bottom order preserved) — the candidate split boundaries, not a verdict.")
        A("")
        for slug, clusters in structure.items():
            A(f"**`{slug}`** — {len(clusters)} proposed cluster(s):")
            for i, titles in enumerate(clusters, 1):
                titles_s = "; ".join(titles) if titles else "(no titles)"
                A(f"  {i}. {titles_s}")
        A("")
    else:
        A("(No lowest-cohesion HEAD had >= 3 sampled chunks to cluster this run — re-run with "
          "a higher --sample-k, or 0 for unsampled, for STRUCTURE output.)")
        A("")

    # -- 3 size/staleness --
    A("## 3. Size / staleness outliers (qq HEADs only)")
    A(f"Threshold: >{SIZE_LIMIT} bytes ({SIZE_LIMIT // 1024}kB) OR newest update-line "
      f">{STALE_DAYS}d old (before {(TODAY - datetime.timedelta(days=STALE_DAYS)).date()}). "
      f"{len(outliers)} of {len(qq_files)} HEADs flagged.")
    A("")
    A("| HEAD | size (bytes) | >32kB | update-line age (days) | >30d stale | no update-line |")
    A("|---|---|---|---|---|---|")
    for slug, sz, age, big, stale, no_date in outliers:
        A(f"| {slug} | {sz} | {'YES' if big else ''} | {age if age is not None else '—'} | "
          f"{'YES' if stale else ''} | {'YES' if no_date else ''} |")
    A("")

    lines_report_body = "\n".join(lines)

    # ---- proposals (derived from the above; written by the script, not auto-actioned) ----
    proposals = []
    # naming-drift class: now handled transparently by SlugResolver at resolution time (it IS
    # run_check's normalization+prefix-stripping+name:-field logic, per-tier as of P4.5), so
    # `variant_links` stays empty by construction and there is nothing left to propose a repair
    # for. Only a genuine cross-namespace collision (SlugResolver.resolve() ambiguous) is still
    # actionable here.
    if ambiguous:
        distinct_ambig = sorted(set(t for _, _, t, _ in ambiguous))
        proposals.append((
            "Adjudicate ambiguous link target(s): " + ", ".join(f"`[[{t}]]`" for t in distinct_ambig[:6]) +
            (f" (+{len(distinct_ambig)-6} more)" if len(distinct_ambig) > 6 else ""),
            f"{len(ambiguous)} instances across {len(distinct_ambig)} targets resolve to more than one distinct file",
            "a genuine slug collision (not a symlink-farm mirror) — pick the intended target or rename one side"
        ))
    if missing_links:
        distinct_missing = sorted(set(t for _, _, t in missing_links))
        proposals.append((
            "Adjudicate genuinely-dangling targets: " + ", ".join(f"`[[{t}]]`" for t in distinct_missing[:6]) +
            (f" (+{len(distinct_missing)-6} more)" if len(distinct_missing) > 6 else ""),
            f"{len(missing_links)} instances across {len(distinct_missing)} targets — SlugResolver found no candidate under any of its resolution rules",
            "each is either a renamed/deleted node (fix the link) or a HEAD that was intended and never created (create or drop)"
        ))
    # merge/link candidates from unlinked-hi-sim pairs
    for sim, a, b in unlinked_hi_sim[:4]:
        proposals.append((
            f"Link (or evaluate merging) `{a}` <-> `{b}`",
            f"cosine {sim:.3f}, currently UNLINKED",
            "highest raw similarity with zero cross-reference — likely the same thread told twice"
        ))
    # split candidates from lowest cohesion (now with STRUCTURE, when available)
    for slug, (m, n, tot) in lowest_cohesion[:4]:
        clusters = structure.get(slug)
        if clusters:
            groups = "; ".join(f"[{', '.join(t)}]" for t in clusters)
            evidence = (f"internal cohesion {m:.4f} across its first {n} chunks (of {tot}; "
                        f"lowest in corpus, sampled) — proposed clusters: {groups}")
        else:
            evidence = f"internal cohesion {m:.4f} across its first {n} chunks (of {tot}; lowest in corpus, sampled)"
        proposals.append((
            f"Review `{slug}` for a split",
            evidence,
            "low mean pairwise similarity among its own sections suggests multiple loosely-related threads under one HEAD"
        ))
    # orphan + stale double-hit
    orphan_stale = [(slug, sz, age) for (lb, slug), sz, age, _n in orphans
                     if age is not None and age > STALE_DAYS]
    for slug, sz, age in orphan_stale[:3]:
        proposals.append((
            f"Triage `{slug}` (orphan + stale)",
            f"no inbound links, {sz}B, {age}d since last update",
            "unreferenced and aging — candidate for linking-in, archiving, or deliberate closure"
        ))
    # oversized HEADs
    already = {p[0].split("`")[1] for p in proposals if "`" in p[0]}
    for slug, sz, age, big, stale, no_date in [o for o in outliers if o[3] and o[0] not in already][:3]:
        proposals.append((
            f"Consider a size-driven split review for `{slug}`",
            f"{sz}B (> {SIZE_LIMIT}B threshold)",
            "large HEADs are the pattern that historically preceded the encode-gap split"
        ))
    proposals = proposals[:10]

    A2 = []
    A2.append("## Top actionable proposals")
    A2.append("")
    if proposals:
        for i, (p, ev, rat) in enumerate(proposals, 1):
            A2.append(f"{i}. **{p}**")
            A2.append(f"   - evidence: {ev}")
            A2.append(f"   - rationale: {rat}")
    else:
        A2.append("No proposals cleared the bar — see honesty note below.")

    report = lines_report_body + "\n" + "\n".join(A2) + "\n"
    report_path = os.path.join(out_dir, "topology-report.md")
    with open(report_path, "w") as f:
        f.write(report)

    # ---- console summary for the calling agent ----
    print("REPORT_PATH", report_path)
    print("SAMPLE_K", sample_k)
    print("HEAD_COUNT", len(qq_files))
    print("MEM_COUNT", len(mem_files))
    print("UNRESOLVED_LINKS", len(unresolved))
    print("AMBIGUOUS_LINKS", len(ambiguous))
    print("ORPHAN_HEADS", len(orphans))
    print("TOP_PAIR_UNLINKED", len(unlinked_hi_sim))
    print("OUTLIER_COUNT", len(outliers))
    print("STRUCTURE_HEADS", len(structure))
    print("TOP10_SIM_PAIRS")
    for sim, a, b in top_pairs:
        print(f"  {sim:.4f}  {a}  <->  {b}  [{link_status(a,b)}]")
    print("LOWEST5_COHESION")
    for slug, (m, n, tot) in lowest_cohesion:
        print(f"  {m:.4f}  {slug}  (sampled {n}/{tot})")


if __name__ == "__main__":
    main()
