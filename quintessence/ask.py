# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.ask — L2: retrieval-as-QA over a quintessence.search.SearchIndex.

Port of qqask_core.py behind the same config discipline: all configuration (endpoints,
redaction file, similarity floor) is resolved ONCE from the `Config` passed to `Ask.__init__`,
no ambient `os.environ` reads below the constructor.

P8 RECALL COMPOSITION: `Ask` itself has NO multi-store awareness and needs none —
`search_index` only ever has to satisfy `.search(question, k=k, source=source)`, so a caller
composing across a search path (qq-ask, qq-search-mcp's ask_continuity) just hands `Ask` a
`quintessence.searchcompose.ComposedSearchIndex` instead of a bare `SearchIndex`; the interface
is identical either way. The only P8-aware code IN this module is additive: `_source_entry`
copies a hit's 'store' key into its rendered source entry ONLY when present (a composed hit
carries one; a bare SearchIndex hit never does) — so redaction (`filter_redacted`) and the A7
min-sim floor apply to composed results exactly as they do today, unmodified, and a degenerate
(single-store) `ask` renders byte-identical to pre-P8.

RATIFIED DEVIATION (not silent — see the report this phase ships with): qqask_core.py's
`_DEFAULT_ENDPOINTS` used to fall back to a hardcoded local-deployment pair whenever
`QQ_ASK_ENDPOINTS` was unset. `QQ_ASK_ENDPOINTS`'s KeyDef (quintessence/config.py, landed in
P1) already retires this default to EMPTY: with nothing configured, `ask` degrades to raw
retrieval instead of probing a stranger's unrelated local ports — this
port completes that already-ratified change rather than reintroducing the hardcoded pair.
CUTOVER CONSEQUENCE (was flagged, since remediated): any deployment relying on the old
hardcoded fallback without an explicit `QQ_ASK_ENDPOINTS` in its own config needs that value
added explicitly before/at cutover, or `qq ask` degrades to raw-retrieval-only — a deliberate,
actioned step per deployment, not a code-level concern.

A7 (evidence-pack similarity floor): `QQ_ASK_MIN_SIM` (see its KeyDef for the calibration and
the exact judgment call) gates a NEW refusal path — if every retrieved hit clears redaction but
none clears the floor, `ask()` returns a distinct "nothing relevant in the store" result WITHOUT
ever calling a completion endpoint, rather than shipping weak evidence and trusting the prompt
alone. The floor is applied ONLY to embedder-ranked hits; keyword-fallback (`degraded=True`)
hits use a term-overlap score on an incomparable scale and are exempt by construction — the
existing degrade ladder (I4: embedder down -> keyword recall; no endpoint -> snippets) and the
grounded-or-refuse completion prompt (I5) are otherwise UNCHANGED.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Optional

from . import heads
from .config import Config
from .refsview import RefsView
from .search import SearchIndex
from . import httpdirect

HOME = os.path.expanduser("~")

# ---- endpoint policy (unchanged from qqask_core; not a Config concern — see the module
# docstring's note on QQ_ASK_ENDPOINTS's now-empty default) -----------------------------------
_BUDGET = {"http://127.0.0.1:8090": 200_000, "http://127.0.0.1:11435": 36_000}
_DEFAULT_BUDGET = 36_000


# The grammar of a whole number in a `url=<whole number>` config value: plain ASCII digits,
# nothing else. Not `int()`'s grammar — int() also reads `1_0` as 10 (digit separator), `+1`
# as 1 (sign) and `٣` as 3 (Unicode decimal digit), and each of those is a hand-typo that
# would pin a value the operator never wrote. Not `str.isdigit()` either — it accepts
# characters int() rejects (`"²".isdigit()` is True while `int("²")` raises). `[0-9]+` closes
# both directions: what passes it always converts, and negatives — including llama-server's
# -1 "any slot" sentinel — are rejected by the grammar itself.
_WHOLE_NUMBER_RE = re.compile(r"[0-9]+")


def _url_int_entries(entries) -> dict:
    """`url=<whole number>` config entries, as {url: int}, skipping every entry whose value
    is not one. ONE parser for both keys shaped this way (QQ_ASK_BUDGETS, QQ_ASK_SLOTS),
    because both want the same answer to a malformed line: leave the endpoint out of the map,
    so it falls back to the unconfigured behaviour instead of carrying a value nobody can act
    on.

    "Whole number" means spelled in plain ASCII digits — see `_WHOLE_NUMBER_RE` for why the
    recognition is a grammar and not `int()`, whose wider acceptance (`1_0` read as 10, `+1`
    as 1, Unicode digits as their values) would send a typo'd config line out as a pin to a
    slot the operator never named, or a budget nobody wrote."""
    parsed = {}
    for entry in entries:
        url, sep, value = entry.partition("=")
        value = value.strip()
        if not url.strip() or not sep or not _WHOLE_NUMBER_RE.fullmatch(value):
            continue
        parsed[url.strip()] = int(value)
    return parsed


ENDPOINT_TIMEOUT = 2
COMPLETION_TIMEOUT = 120
MAX_TOKENS = 700

SNAPSHOT_BANNER = "point-in-time snapshot — verify load-bearing facts at source (qq show <topic>)"
TRANSCRIPT_BANNER = "transcript = unverified working memory, not a decision record — verify at source"

SYSTEM_PROMPT = (
    "You answer ONLY from the numbered evidence blocks the user provides. Cite the "
    "block number in brackets, e.g. [2], immediately after every claim. Quote exact "
    "values (numbers, names, paths, versions) verbatim from the evidence — never "
    "paraphrase a number or a proper noun, and never round or approximate one. Never use "
    "outside/background knowledge to answer a store fact, even if you are confident it is "
    "correct — the evidence is the only permitted source. If the evidence does not contain "
    "the answer, your ENTIRE reply must be exactly:\n"
    "NOT IN STORE\n"
    "with nothing before or after it — no guess, no hedge, no partial answer from general "
    "knowledge, no explanation."
)


class Ask:
    """Retrieval-as-QA service. Construct with a Config and a SearchIndex built from the SAME
    Config (callers own the pairing — see qq-ask/qq-search-mcp for the wiring)."""

    def __init__(self, config: Config, search_index: SearchIndex):
        self.config = config
        self.search_index = search_index
        self.endpoints = config.get_list("QQ_ASK_ENDPOINTS")
        self.unredacted = config.get_bool("QQ_ASK_UNREDACTED")
        # (search_index is None in unit tests that exercise filter_redacted in isolation)
        if self.unredacted and search_index is not None:
            # QQ_ASK_UNREDACTED is `ask`'s OWN documented escape hatch and predates search-side
            # filtering. `ask` retrieves THROUGH SearchIndex, so once search learned to filter,
            # this hatch silently stopped working unless a second, separately-named variable was
            # also set — the hatch was documented unconditionally and quietly did nothing. Lift
            # the search-side filter for this instance so one variable means one thing.
            self.search_index.redact_lifted = True
        self.redact_file = config.get_path("QQ_REDACT_FILE")
        self.min_sim = config.get_float("QQ_ASK_MIN_SIM")
        # Evidence-pack size is a property of the ENDPOINT, not of the store: a GPU model chews
        # 200k chars, a CPU fallback cannot. Measured 2026-07-13 on a 4B Q8 at 8 CPU threads —
        # prefill runs ~99 tok/s and DECAYS to ~73 tok/s as the context grows, so the default 36k
        # pack (~10k tokens) spent 114s in prefill alone and was cancelled by COMPLETION_TIMEOUT
        # before generating a single token. Overriding the budget per endpoint is what makes a slow
        # fallback usable, and it is a deployment fact, so it belongs in config, not in the literal
        # below (which stays as the default for the endpoints it names).
        self.budgets = dict(_BUDGET)
        self.budgets.update(_url_int_entries(config.get_list("QQ_ASK_BUDGETS")))

        # Model NAME per endpoint, same `url=value` shape as the budgets above. Only endpoints
        # that SELECT a model by name need one: llama-server serves whatever it loaded and
        # ignores the field, ollama selects by it and 404s on a name it does not have. Which
        # server sits on which port is a deployment fact, so it is config, not a literal here.
        self.models = {}
        for entry in config.get_list("QQ_ASK_MODELS"):
            url, _, name = entry.partition("=")
            if url.strip() and name.strip():
                self.models[url.strip()] = name.strip()

        # SLOT per endpoint, same `url=value` shape again. Only for a resident multi-slot
        # llama-server this client SHARES: `id_slot` tells the server which slot serves the
        # request instead of letting it place the request itself, which on an idle server
        # means slot 0 — so every unpinned client lands on the same slot, and a client that
        # reads slot 0 to decide the server is busy waits behind traffic that was never meant
        # to be there. Which port holds a multi-slot server, and which slot is this client's,
        # is a deployment fact (the estate's map is the operator's carrier map), so it is
        # config rather than a literal here.
        self.slots = _url_int_entries(config.get_list("QQ_ASK_SLOTS"))

    def _budget_for(self, endpoint: str) -> int:
        return self.budgets.get(endpoint, _DEFAULT_BUDGET)

    def _model_for(self, endpoint: str) -> Optional[str]:
        """The model name a request to this endpoint will carry, or None if it will carry none.
        ONE derivation, read by the probe and by the completion call alike, so what the probe
        certifies cannot drift from what the request actually needs."""
        return self.models.get(endpoint)

    def _slot_for(self, endpoint: str) -> Optional[int]:
        """The slot a request to this endpoint will be pinned to, or None if it will name none.
        None and 0 are DIFFERENT answers — 0 is a real slot, and the one an unpinned request
        lands on — so callers test against None and never for truthiness."""
        return self.slots.get(endpoint)

    # The per-endpoint directives `_complete` merges into the request body: the body key, and
    # the accessor that derives its value for an endpoint. ONE account, walked to build the
    # body, so a directive added later cannot be added to the body and forgotten by the test
    # that holds every directive to the same rule — omit it entirely unless config names a
    # well-formed value for THIS endpoint. That rule is not fastidiousness: each of these is an
    # instruction to the server about how to serve this request, and a wrong one is refused
    # rather than approximated — a 404 for a model name the server does not hold, a rejection
    # for a slot it does not have. `ask` reads any of those as "this rung failed" and steps
    # down its ladder, so a bad directive costs the answer a healthy tier and says nothing.
    _BODY_DIRECTIVES = (
        ("model", "_model_for"),
        ("id_slot", "_slot_for"),
    )

    @staticmethod
    def _served_models(payload) -> set:
        """The model names an OpenAI-shaped `/v1/models` body says the server can serve, read
        from `data[].id`. ollama tags an untagged pull `:latest` and answers to either spelling,
        so both are entered — the probe certifies against what the server will ACCEPT, not
        against a stricter string compare that would fail a name the completion would have
        selected fine."""
        names = set()
        data = payload.get("data") if isinstance(payload, dict) else None
        for entry in data or []:
            name = entry.get("id") if isinstance(entry, dict) else None
            if isinstance(name, str) and name:
                names.add(name)
                if name.endswith(":latest"):
                    names.add(name[: -len(":latest")])
        return names

    def probe_endpoint(self, endpoint: str) -> bool:
        # A probe answers ONE question — will the request this ladder is about to send succeed
        # here? — so it certifies whatever the SERVER makes that request depend on. The two arms
        # below are the two shapes `_complete` builds, and both read `_model_for`, the same
        # derivation `_complete` builds the body from: what is certified cannot drift from what
        # is sent, because there is no second list to keep in step.
        #
        # NO NAME (llama-server): the body names no model, the server answers with whatever it
        # loaded, and a process that answers is the whole precondition. /health is the right
        # question and it answers 503 while the model is still loading — the property the ladder
        # relies on to skip a tier that is not ready yet, so it must keep using /health.
        #
        # A NAME (ollama): the body names a model, and a name the server does not hold is a 404
        # on the completion. ollama does not serve /health at all (404); it answers /v1/models,
        # and that answer LISTS what it can serve — so read the list and certify the configured
        # name is in it, rather than reading a 200 as the answer to a question nobody asked. A
        # status alone certifies that a process is up, which is not what the request needs.
        #
        # The ON-DEMAND tier survives this unchanged, which is the point of certifying against
        # the LIST rather than against residency: ollama lists a model it is holding no VRAM
        # for, and the completion call pays the load (~3s, page-cache warm) well inside
        # COMPLETION_TIMEOUT. A tier that had to be resident to be picked could not idle at zero.
        model = self._model_for(endpoint)
        path = "/health" if model is None else "/v1/models"
        try:
            with httpdirect.urlopen(f"{endpoint}{path}", timeout=ENDPOINT_TIMEOUT) as r:
                if not 200 <= r.status < 300:
                    return False
                if model is None:
                    return True
                served = self._served_models(json.load(r))
        except Exception:
            # Fail CLOSED, including on a body that will not parse: an answer the probe cannot
            # read certifies nothing, and "certifies nothing" is not "ready". The cost of being
            # wrong here is one rung skipped rather than one question lost, because a rung that
            # does not certify falls to the next one (see ask()).
            return False
        return model in served

    def certified_endpoints(self):
        """Every configured rung that certifies, in ladder order, probed lazily — a rung is
        probed only when the ladder actually reaches it, so an answer from the top rung still
        costs one probe."""
        for e in self.endpoints:
            if self.probe_endpoint(e):
                yield e

    def pick_endpoint(self) -> Optional[str]:
        """The first rung that certifies. `qq doctor` reports this one as the endpoint `ask`
        would reach; ask() itself walks the whole ladder (see its own comment). Both read
        certified_endpoints(), so they cannot disagree about which rungs are up."""
        return next(self.certified_endpoints(), None)

    # ---- redaction (unchanged semantics from qqask_core.filter_redacted) -------------------
    def _redact_entries(self) -> list:
        try:
            with open(self.redact_file) as f:
                raw = f.read().splitlines()
        except OSError:
            return []
        out = []
        for ln in raw:
            ln = re.sub(r"\s+#.*$", "", ln).strip()
            if ln and not ln.startswith("#"):
                out.append(ln)
        return out

    def redact_active(self) -> bool:
        return bool(self._redact_entries())

    @staticmethod
    def _slug_redacted(path: str, entries: list) -> bool:
        base = os.path.basename(path)
        if base.endswith(".md"):
            base = base[:-3]
        for e in entries:
            if e.endswith("*"):
                pre = e[:-1]
                if base.startswith(pre) or pre in path:
                    return True
            elif base == e or e in path:
                return True
        return False

    def filter_redacted(self, hits: list) -> list:
        if self.unredacted:
            return hits
        entries = self._redact_entries()
        if not entries:
            return hits
        return [h for h in hits if not self._slug_redacted(h["path"], entries)]

    # ---- evidence pack -----------------------------------------------------------------------
    _NEWEST_UPDATE_CAP = 280

    def _parse_newest_update(self, path: str):
        """Return (rest, full_line) for the file's first REAL update-line, per the one reader
        (heads.update_lines). Until 2026-08-09 this matched a stripped line against its own
        regex, which made it the sole reader that tolerated leading whitespace — so the write
        path's one-space neutralization of a quoted update-line did not neutralize it HERE, and
        the evidence pack could date a source by a quoted line every other surface ignored. It
        also only looked at the first 20 raw lines; the reader reads the header region."""
        full = os.path.join(HOME, path)
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                items = heads.update_lines(f.read())
        except OSError:
            return None, None
        if not items:
            return None, None
        return items[0].rest.strip(), items[0].marker.strip()

    def _updated_date(self, path: str):
        date, _ = self._parse_newest_update(path)
        return date

    # ---- source-entry shape: shared by build_evidence/_degraded/_no_evidence -----------------
    @staticmethod
    def _source_entry(i: int, h: dict, snippet: bool = False,
                      updated: str = None, warnings: list = None) -> dict:
        """{n, path, title, score} (+ snippet when asked), plus 'store', 'updated', 'warnings'
        — each ADDITIVELY, only when present."""
        entry = {"n": i, "path": h["path"], "title": h["title"], "score": h["score"]}
        if "source" in h:
            entry["source"] = h["source"]
        if snippet:
            entry["snippet"] = h["snippet"]
        if "store" in h:
            entry["store"] = h["store"]
        if updated is not None:
            entry["updated"] = updated
        if warnings:
            entry["warnings"] = list(warnings)
        return entry

    def build_evidence(self, hits: list, budget_chars: int):
        # B2 read-side revalidation: evidence-block HEADERS carry the
        # '⚠ referent changed since written' annotations for any bound update-line the chunk is
        # about to put in front of the model. Join is by slug (the hit path's basename) + the
        # chunk's own '> updated:' stamps — refs are keyed by HEAD slug, and only a store HEAD's
        # chunks contain bindable stamped lines, so a non-store doc simply never joins.
        # RefsView is fail-soft + read-only by construction; with no refs the evidence pack is
        # byte-identical to pre-B2.
        try:
            view = RefsView(self.config)
        except Exception:
            view = None
        blocks, sources, used = [], [], 0
        for i, h in enumerate(hits, 1):
            date, newest_line = self._parse_newest_update(h["path"])
            head = f"[{i}] {h['path']} › {h['title']}" + (f" (updated {date})" if date else "")
            annotations = []
            if view is not None:
                slug = os.path.basename(h["path"])
                if slug.endswith(".md"):
                    slug = slug[:-3]
                annotations = view.chunk_annotations(slug, h["text"])
                for ann in annotations:
                    head += f"\n{ann}"
            if newest_line and newest_line not in h["text"]:
                capped = newest_line[:self._NEWEST_UPDATE_CAP]
                if len(newest_line) > self._NEWEST_UPDATE_CAP:
                    capped += "…"
                head += f"\nnewest-update: {capped}"
            block = f"{head}\n{h['text']}\n"
            if used + len(block) > budget_chars and blocks:
                break
            blocks.append(block)
            used += len(block)
            sources.append(self._source_entry(i, h, updated=date,
                                              warnings=annotations or None))
        return "\n".join(blocks), sources

    # ---- completion call ---------------------------------------------------------------------
    def _complete(self, endpoint: str, question: str, evidence: str) -> str:
        body = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Evidence:\n\n{evidence}\n\nQuestion: {question}"},
            ],
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        # Each directive is omitted entirely for an endpoint config names no value for, rather
        # than sent as a placeholder: a wrong model name is a 404 at an endpoint that selects
        # by name, and a slot the server does not have is a refused request. `_model_for` is
        # the same reading `probe_endpoint` certifies against, so the name sent here is the
        # name the probe checked was served. Tested against None, not for truthiness, because
        # slot 0 is a real slot.
        for key, accessor in self._BODY_DIRECTIVES:
            value = getattr(self, accessor)(endpoint)
            if value is not None:
                body[key] = value
        req = urllib.request.Request(
            f"{endpoint}/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with httpdirect.urlopen(req, timeout=COMPLETION_TIMEOUT) as r:
            data = json.load(r)
        return self._answer_text(data["choices"][0]["message"])

    # `enable_thinking: False` above asks a Qwen-family chat template to skip its reasoning pass.
    # That key is a TEMPLATE CONVENTION, not a protocol guarantee, and the endpoint behind
    # the configured port is swappable at will (the operator's own substrate config), so a
    # profile that ignores it is a matter of when, not if. Two shapes appear when it is ignored:
    # the reasoning arrives inline in `content` (llama-server run with --reasoning-format none),
    # or the server splits it out and `content` comes back EMPTY (the default deepseek format,
    # measured on the qwen3.6-35b-a3b substrate 2026-07-24: no switch -> 169 completion tokens of
    # `reasoning_content` beside a 1-word `content`). Untreated, the first turns an answer into a
    # reasoning dump and the second returns a blank line as if it were an answer.
    _THINK_BLOCK = re.compile(r"\A\s*<(think|thinking)\b[^>]*>.*?</\1>\s*", re.DOTALL | re.IGNORECASE)
    _THINK_UNCLOSED = re.compile(r"\A\s*<(think|thinking)\b[^>]*>.*\Z", re.DOTALL | re.IGNORECASE)

    @staticmethod
    def _answer_text(message: dict) -> str:
        """The model's answer, whatever shape the server returned it in. Raises ValueError when
        there is no answer at all, so ask() degrades with a NAMED reason (the same principle as
        _degraded's `reason`) instead of rendering an empty answer as a real one."""
        content = (message.get("content") or "").strip()
        content = Ask._THINK_BLOCK.sub("", content).strip()
        # An unclosed opening tag means generation was cut off mid-reasoning (MAX_TOKENS), so
        # nothing after it is an answer either — drop the lot and fall through.
        content = Ask._THINK_UNCLOSED.sub("", content).strip()
        if content:
            return content
        reasoning = (message.get("reasoning_content") or "").strip()
        if reasoning:
            return reasoning
        raise ValueError("model returned no answer text (content and reasoning_content both empty)")

    # ---- degraded / no-evidence renderings --------------------------------------------------
    @staticmethod
    def _degraded(hits: list, reason: Optional[str] = None, retrieval_degraded: bool = False) -> dict:
        # `reason` exists because the single old message ("no completion endpoint healthy") was a
        # MISDIAGNOSIS in the commonest failure: a healthy-but-slow endpoint that blows
        # COMPLETION_TIMEOUT reported as if nothing were running. Measured 2026-07-13 — a CPU
        # endpoint spent 114s on prefill alone and got cancelled, and the user was told to go look
        # for a dead server. Name what actually happened.
        #
        # `retrieval_degraded` (recall-01) is an ORTHOGONAL signal to `degraded` above: `degraded`
        # here means "no completion endpoint answered" (raw retrieval only); `retrieval_degraded`
        # means the RETRIEVAL itself fell back to keyword grep (embedder unreachable) — qq-search
        # already surfaces this loudly (see search.py's `degraded` hit tag + qq-search's stderr
        # warning); Ask silently dropped it. Threaded through so a caller/renderer can warn on it
        # independent of whether a completion endpoint was ever reached.
        sources = [Ask._source_entry(i, h, snippet=True) for i, h in enumerate(hits, 1)]
        why = reason or "no completion endpoint healthy"
        return {
            "answer": f"QA unavailable — raw retrieval only ({why}).",
            "sources": sources,
            "endpoint": None,
            "degraded": True,
            "retrieval_degraded": retrieval_degraded,
        }

    def _no_evidence(self, hits: list, retrieval_degraded: bool = False) -> dict:
        """A7: every hit (if any) scored below QQ_ASK_MIN_SIM — refuse before ever calling a
        completion endpoint. Weak sources are still surfaced (with score) for the caller's own
        judgment; `refused` distinguishes this from the no-endpoint `degraded` path.

        recall-04: the refusal message used to unconditionally claim a similarity-floor
        comparison happened, even when `hits` was EMPTY (e.g. keyword fallback found zero term
        matches — no floor was ever consulted). Branch the wording on that instead."""
        sources = [self._source_entry(i, h, snippet=True) for i, h in enumerate(hits, 1)]
        if hits:
            answer = (f"NOTHING RELEVANT IN STORE — {len(hits)} retrieved chunk(s) but none "
                      f"cleared the minimum similarity floor ({self.min_sim:.2f}); refusing "
                      f"rather than shipping weak evidence to the model.")
        else:
            answer = ("NOTHING RELEVANT IN STORE — no chunk matched the query at all; refusing "
                      "rather than shipping weak evidence to the model.")
        return {
            "answer": answer,
            "sources": sources,
            "endpoint": None,
            "degraded": False,
            "refused": True,
            "retrieval_degraded": retrieval_degraded,
        }

    # ---- entry point --------------------------------------------------------------------------
    def ask(self, question: str, k: int = 12, source: Optional[str] = None) -> dict:
        """Retrieve -> redact -> A7 floor -> probe -> complete (or degrade). Returns
        {answer, sources: [{n, path, title, score[, updated][, warnings][, store]}],
        endpoint, degraded, retrieval_degraded[, refused]}."""
        hits = self.search_index.search(question, k=k, source=source, min_sim=0)
        hits = self.filter_redacted(hits)
        # recall-01: SearchIndex tags EVERY hit `degraded: True` when it fell back to keyword
        # grep (embedder unreachable) — grep_fallback never returns a MIXED list, so hits[0] is
        # representative of the whole batch. `qq search`/qq-search already warn loudly on this
        # exact signal (see search.py's module docstring + qq-search's stderr check); ask() used
        # to read it ONLY to exempt keyword hits from the min-sim floor below, then drop it on
        # the floor — never surfacing it to a caller, MCP included. Compute it once, thread it
        # through every return path.
        retrieval_degraded = bool(hits) and bool(hits[0].get("degraded"))
        if not hits or (not hits[0].get("degraded") and hits[0]["score"] < self.min_sim):
            return self._no_evidence(hits, retrieval_degraded)
        # THE LADDER. A rung that certifies can still fail to complete — a timeout, a context
        # overflow, an answer with no text in it — and that is a fact about THAT rung, not about
        # the ladder: the rung below is usually slower and still perfectly able to answer.
        # Stepping down to it is the whole reason several endpoints are configured rather than
        # one, so a single rung's failure is not the ladder's verdict and raw retrieval is
        # reserved for every rung failing.
        #
        # The evidence pack is rebuilt per rung, not once before the loop: its size is a
        # property of the ENDPOINT (`_budget_for`), and a pack sized for the GPU tier is exactly
        # what the CPU tier below it cannot chew inside COMPLETION_TIMEOUT.
        failures = []
        for endpoint in self.certified_endpoints():
            evidence, sources = self.build_evidence(hits, self._budget_for(endpoint))
            try:
                answer = self._complete(endpoint, question, evidence)
            except Exception as e:  # noqa: BLE001
                # Name the rung and the exception CLASS: the two failures look identical from
                # the outside but need opposite fixes — a socket timeout means the model is too
                # slow for its evidence budget (shrink QQ_ASK_BUDGETS for it), while an
                # HTTPError usually means the pack overflowed the server's context window (raise
                # its -c, or shrink the budget). Both were hit within an hour of each other on
                # 2026-07-13; the old single message ("no completion endpoint healthy")
                # described neither.
                failures.append(f"{endpoint} certified but the completion failed: "
                                f"{type(e).__name__}")
                continue
            # WHICH RUNG ANSWERED is part of the answer. `render` prints it and `--json` /
            # ask_continuity carry it, so a ladder that quietly fell past its top rung for a
            # month is visible in any single answer rather than only in a doctor run.
            return {"answer": answer, "sources": sources, "endpoint": endpoint,
                    "degraded": False, "retrieval_degraded": retrieval_degraded}
        if failures:
            return self._degraded(
                hits, "every rung failed — " + "; ".join(failures)
                      + f" (completion timeout is {COMPLETION_TIMEOUT}s)",
                retrieval_degraded=retrieval_degraded)
        if not self.endpoints:
            return self._degraded(hits, "QQ_ASK_ENDPOINTS is empty — no completion endpoint "
                                        "is configured", retrieval_degraded=retrieval_degraded)
        return self._degraded(
            hits, f"none of the {len(self.endpoints)} configured endpoint(s) certified",
            retrieval_degraded=retrieval_degraded)


RETRIEVAL_DEGRADED_WARNING = "⚠ embedder unreachable — showing KEYWORD fallback results (run `qq doctor`)."


def render(result: dict, banner: Optional[str] = None) -> str:
    """Shared text rendering for the CLI and the MCP tool: answer, sources, banner. `banner`
    defaults to SNAPSHOT_BANNER (the MCP tool always gets the default — never pass a banner
    there); the qq-ask CLI passes TRANSCRIPT_BANNER in --transcripts mode.

    recall-01: when `result['retrieval_degraded']` is set, prepend the SAME loud warning
    qq-search already prints to stderr on an embedder-down keyword-fallback answer — ask_continuity
    (the MCP tool) has no stderr channel a caller reads, so this is the only surface that reaches
    it; the qq-ask CLI's --json path gets the flag directly in the dict instead."""
    lines = []
    if result.get("retrieval_degraded"):
        lines.append(RETRIEVAL_DEGRADED_WARNING)
        lines.append("")
    lines.append(result["answer"])
    lines.append("")
    # The rung that answered, named on the surface a PERSON reads. The dict has carried
    # `endpoint` all along for a caller that parses it, and the CLI's --json still hands that
    # straight over; the text surface dropped it, which is where it was needed once ask() began
    # stepping down a ladder — an answer from the third rung and an answer from the first read
    # identically otherwise. Omitted, not blanked, on a degraded/refused result, where the
    # answer itself already says no rung answered.
    if result.get("endpoint"):
        lines.append(f"answered by {result['endpoint']}")
        lines.append("")
    if result["sources"]:
        lines.append("sources:")
        for s in result["sources"]:
            store_tag = f", {s['store']}" if "store" in s else ""
            updated_tag = f", updated {s['updated']}" if s.get("updated") else ""
            lines.append(f"  [{s['n']}] {s['path']} › {s['title']} (score {s['score']:.3f}{updated_tag}{store_tag})")
            if s.get("snippet"):
                lines.append(f"      {s['snippet']}")
            for w in s.get("warnings", []):
                lines.append(f"      {w}")
        lines.append("")
    lines.append(banner if banner is not None else SNAPSHOT_BANNER)
    return "\n".join(lines)
