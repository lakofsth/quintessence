# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.ask: Config plumbing, the minimum-similarity floor
+ its "nothing relevant" refusal path (never applied to keyword-fallback hits), the redaction
ladder (unchanged from qqask_core.filter_redacted), and the no-completion-endpoint degrade
path (degrade behaviour preserved)."""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence import ask as ask_module
from quintessence.config import Config
from quintessence.ask import Ask, render, RETRIEVAL_DEGRADED_WARNING


class FakeIndex:
    """A stand-in SearchIndex: .search() returns whatever hits a test wires up."""
    def __init__(self, hits):
        self._hits = hits
        self.last_search_floored = 0

    def search(self, query, k=6, source=None, min_sim=None):
        return list(self._hits)


def make_ask(base: str, hits, **overrides) -> Ask:
    over = {"QQ_ASK_ENDPOINTS": "", "QQ_REDACT_FILE": os.path.join(base, "redact-slugs")}
    over.update(overrides)
    cfg = Config(env={}, config_file="/nonexistent", overrides=over)
    return Ask(cfg, FakeIndex(hits))


class TestConfigPlumbing(unittest.TestCase):
    def test_reads_endpoints_redact_file_and_floor_once(self):
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [], QQ_ASK_ENDPOINTS="http://a:1,http://b:2", QQ_ASK_MIN_SIM="0.4")
            self.assertEqual(asker.endpoints, ["http://a:1", "http://b:2"])
            self.assertEqual(asker.min_sim, 0.4)

    def test_default_endpoints_empty_per_ratified_r5_default(self):
        with tempfile.TemporaryDirectory() as base:
            cfg = Config(env={}, config_file="/nonexistent",
                         overrides={"QQ_REDACT_FILE": os.path.join(base, "redact-slugs")})
            asker = Ask(cfg, FakeIndex([]))
            self.assertEqual(asker.endpoints, [])


class TestMinSimFloor(unittest.TestCase):
    def _hit(self, score, path="kb/quintessence/x.md", degraded=False):
        h = {"score": score, "source": "qq", "path": path, "title": "t", "snippet": "s", "text": "body"}
        if degraded:
            h["degraded"] = True
        return h

    def test_all_hits_below_floor_refuses_without_calling_endpoint(self):
        with tempfile.TemporaryDirectory() as base:
            hits = [self._hit(0.2), self._hit(0.1)]
            asker = make_ask(base, hits, QQ_ASK_MIN_SIM="0.35",
                              QQ_ASK_ENDPOINTS="http://127.0.0.1:1")   # dead port; must never be probed
            called = {"n": 0}
            asker.probe_endpoint = lambda e: called.__setitem__("n", called["n"] + 1) or False
            result = asker.ask("nonsense query")
            self.assertTrue(result.get("refused"))
            self.assertFalse(result["degraded"])
            self.assertIn("NOTHING RELEVANT", result["answer"])
            self.assertEqual(called["n"], 0, "the floor short-circuit must skip the endpoint probe entirely")

    def test_best_hit_at_or_above_floor_proceeds_past_the_floor(self):
        with tempfile.TemporaryDirectory() as base:
            hits = [self._hit(0.5), self._hit(0.2)]
            asker = make_ask(base, hits, QQ_ASK_MIN_SIM="0.35", QQ_ASK_ENDPOINTS="")
            result = asker.ask("a real question")
            # no endpoint configured at all -> falls through to the (unchanged) degraded path,
            # NOT the refusal path -- proves the floor check passed and normal flow continued.
            self.assertFalse(result.get("refused"))
            self.assertTrue(result["degraded"])

    def test_no_hits_at_all_refuses(self):
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [], QQ_ASK_MIN_SIM="0.35")
            result = asker.ask("anything")
            self.assertTrue(result.get("refused"))

    def test_floor_not_applied_to_degraded_keyword_fallback_hits(self):
        """A grep_fallback score is a term-overlap fraction, not a cosine similarity -- even a
        LOW keyword-overlap score must not be refused by the embedder-calibrated floor."""
        with tempfile.TemporaryDirectory() as base:
            hits = [self._hit(0.1, degraded=True)]
            asker = make_ask(base, hits, QQ_ASK_MIN_SIM="0.35", QQ_ASK_ENDPOINTS="")
            result = asker.ask("anything")
            self.assertFalse(result.get("refused"), "degraded (keyword-fallback) hits must bypass the cosine floor")
            self.assertTrue(result["degraded"])   # still degrades on the (separate) no-endpoint path


class TestNoEvidenceMessage(unittest.TestCase):
    """recall-04: the refusal message used to unconditionally claim a similarity-floor
    comparison happened, even with zero hits retrieved at all (no floor was ever consulted)."""

    def _hit(self, score, degraded=False):
        h = {"score": score, "source": "qq", "path": "p.md", "title": "t", "snippet": "s", "text": "b"}
        if degraded:
            h["degraded"] = True
        return h

    def test_zero_hits_message_does_not_claim_a_floor_comparison(self):
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [], QQ_ASK_MIN_SIM="0.35")
            result = asker.ask("nothing matches this at all")
            self.assertTrue(result["refused"])
            self.assertIn("no chunk matched the query at all", result["answer"])
            self.assertNotIn("similarity floor", result["answer"])

    def test_below_floor_message_names_the_floor_and_count(self):
        with tempfile.TemporaryDirectory() as base:
            hits = [self._hit(0.2), self._hit(0.1)]
            asker = make_ask(base, hits, QQ_ASK_MIN_SIM="0.35")
            result = asker.ask("weak match")
            self.assertTrue(result["refused"])
            self.assertIn("2 retrieved chunk(s)", result["answer"])
            self.assertIn("similarity floor (0.35)", result["answer"])


class TestRetrievalDegradedSurfacing(unittest.TestCase):
    """recall-01: a keyword-fallback (embedder-down) answer used to be indistinguishable from a
    real semantic one in Ask's return dict — the ONE place ask_continuity (qq-search-mcp) could
    ever learn this happened, since the MCP tool returns a single string with no stderr a caller
    reads. `retrieval_degraded` threads the SearchIndex-level `degraded` hit tag through every
    ask() return path, and render() (shared by qq-ask's text mode and ask_continuity) surfaces
    the same loud warning qq-search already prints to stderr."""

    def _degraded_hit(self, score=0.6):
        return {"score": score, "source": "qq", "path": "p.md", "title": "t", "snippet": "s",
                "text": "b", "degraded": True}

    def test_refusal_path_carries_the_flag(self):
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [], QQ_ASK_MIN_SIM="0.35")
            result = asker.ask("anything")
            self.assertFalse(result["retrieval_degraded"])   # no hits at all -> nothing to flag

    def test_degraded_keyword_hit_sets_the_flag_even_though_floor_is_bypassed(self):
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [self._degraded_hit()], QQ_ASK_ENDPOINTS="")
            result = asker.ask("anything")
            self.assertTrue(result["retrieval_degraded"])

    def test_successful_completion_still_carries_the_flag(self):
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [self._degraded_hit()], QQ_ASK_ENDPOINTS="http://example:9")
            asker.probe_endpoint = lambda e: True
            asker._complete = lambda endpoint, q, ev: "the answer [1]"
            result = asker.ask("anything")
            self.assertFalse(result["degraded"])
            self.assertTrue(result["retrieval_degraded"])

    def test_real_embedder_hit_never_sets_the_flag(self):
        with tempfile.TemporaryDirectory() as base:
            hit = {"score": 0.6, "source": "qq", "path": "p.md", "title": "t", "snippet": "s", "text": "b"}
            asker = make_ask(base, [hit], QQ_ASK_ENDPOINTS="")
            result = asker.ask("anything")
            self.assertFalse(result["retrieval_degraded"])

    def test_render_prepends_the_same_warning_qq_search_uses_on_stderr(self):
        result = {"answer": "the answer [1].", "sources": [], "endpoint": "http://e:9",
                  "degraded": False, "retrieval_degraded": True}
        out = render(result)
        self.assertTrue(out.startswith(RETRIEVAL_DEGRADED_WARNING))
        self.assertIn("the answer [1].", out)

    def test_render_omits_the_warning_when_not_degraded(self):
        result = {"answer": "the answer [1].", "sources": [], "endpoint": "http://e:9",
                  "degraded": False, "retrieval_degraded": False}
        self.assertNotIn(RETRIEVAL_DEGRADED_WARNING, render(result))


class TestRedaction(unittest.TestCase):
    def _hit(self, path, score=0.6):
        return {"score": score, "source": "qq", "path": path, "title": "t", "snippet": "s", "text": "body"}

    def test_redact_file_drops_matching_hit_keeps_others(self):
        with tempfile.TemporaryDirectory() as base:
            with open(os.path.join(base, "redact-slugs"), "w") as f:
                f.write("secret-topic\n")
            hits = [self._hit("quintessence/secret-topic.md"), self._hit("quintessence/public-topic.md")]
            asker = make_ask(base, hits)
            out = asker.filter_redacted(hits)
            self.assertEqual([h["path"] for h in out], ["quintessence/public-topic.md"])

    def test_unredacted_escape_hatch_bypasses_redaction(self):
        with tempfile.TemporaryDirectory() as base:
            with open(os.path.join(base, "redact-slugs"), "w") as f:
                f.write("secret-topic\n")
            hits = [self._hit("quintessence/secret-topic.md")]
            asker = make_ask(base, hits, QQ_ASK_UNREDACTED="1")
            out = asker.filter_redacted(hits)
            self.assertEqual(len(out), 1)

    def test_no_redact_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as base:
            hits = [self._hit("quintessence/anything.md")]
            asker = make_ask(base, hits)
            self.assertEqual(asker.filter_redacted(hits), hits)


class TestDegradeLadder(unittest.TestCase):
    def _hit(self, score=0.6):
        return {"score": score, "source": "qq", "path": "p.md", "title": "t", "snippet": "s", "text": "body"}

    def test_no_healthy_endpoint_degrades_with_marker(self):
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [self._hit()], QQ_ASK_ENDPOINTS="http://127.0.0.1:1")
            result = asker.ask("a real question")
            self.assertTrue(result["degraded"])
            self.assertIn("QA unavailable", result["answer"])
            self.assertIsNone(result["endpoint"])

    def test_completion_call_success_returns_grounded_answer(self):
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [self._hit()], QQ_ASK_ENDPOINTS="http://example:9")
            asker.probe_endpoint = lambda e: True
            asker._complete = lambda endpoint, q, ev: "the answer [1]"
            result = asker.ask("a real question")
            self.assertFalse(result["degraded"])
            self.assertEqual(result["answer"], "the answer [1]")
            self.assertEqual(result["endpoint"], "http://example:9")

    def test_completion_call_exception_degrades(self):
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [self._hit()], QQ_ASK_ENDPOINTS="http://example:9")
            asker.probe_endpoint = lambda e: True
            def boom(*a, **k):
                raise RuntimeError("timeout")
            asker._complete = boom
            result = asker.ask("a real question")
            self.assertTrue(result["degraded"])


class TestAnswerText(unittest.TestCase):
    """_answer_text: the answer survives a substrate profile that ignores `enable_thinking`,
    in either shape it can arrive in (reasoning inline in content / split into
    reasoning_content). See the comment above _THINK_BLOCK for the measured behaviour."""

    def test_plain_content_passes_through(self):
        self.assertEqual(Ask._answer_text({"content": "  Helsinki [1].  "}), "Helsinki [1].")

    def test_inline_think_block_is_dropped(self):
        msg = {"content": "<think>\nThe user asks for a capital.\n</think>\n\nHelsinki [1]."}
        self.assertEqual(Ask._answer_text(msg), "Helsinki [1].")

    def test_thinking_tag_spelling_and_attributes(self):
        msg = {"content": "<thinking id='1'>weighing it up</thinking>Helsinki [1]."}
        self.assertEqual(Ask._answer_text(msg), "Helsinki [1].")

    def test_think_block_only_inside_the_answer_is_kept(self):
        # Only a LEADING block is reasoning; a tag quoted mid-answer is evidence being cited.
        msg = {"content": "The log shows <think>x</think> in the output [1]."}
        self.assertEqual(Ask._answer_text(msg), "The log shows <think>x</think> in the output [1].")

    def test_unclosed_think_block_falls_back(self):
        # Cut off mid-reasoning by MAX_TOKENS: nothing after the tag is an answer either.
        msg = {"content": "<think>step one, step two, step", "reasoning_content": "Helsinki [1]."}
        self.assertEqual(Ask._answer_text(msg), "Helsinki [1].")

    def test_empty_content_falls_back_to_reasoning_content(self):
        msg = {"content": "", "reasoning_content": "  Helsinki [1].  "}
        self.assertEqual(Ask._answer_text(msg), "Helsinki [1].")

    def test_content_wins_over_reasoning_content(self):
        msg = {"content": "Helsinki [1].", "reasoning_content": "let me think about capitals"}
        self.assertEqual(Ask._answer_text(msg), "Helsinki [1].")

    def test_no_answer_at_all_raises_so_ask_degrades_with_a_reason(self):
        with self.assertRaises(ValueError):
            Ask._answer_text({"content": "", "reasoning_content": ""})
        with self.assertRaises(ValueError):
            Ask._answer_text({})

    def test_empty_completion_degrades_rather_than_answering_blank(self):
        with tempfile.TemporaryDirectory() as base:
            hit = {"score": 0.6, "source": "qq", "path": "p.md", "title": "t",
                   "snippet": "s", "text": "body"}
            asker = make_ask(base, [hit], QQ_ASK_ENDPOINTS="http://example:9")
            asker.probe_endpoint = lambda e: True
            asker._complete = lambda endpoint, q, ev: Ask._answer_text({"content": ""})
            result = asker.ask("a real question")
            self.assertTrue(result["degraded"])
            self.assertIn("ValueError", result["answer"])


class TestRender(unittest.TestCase):
    def test_render_default_banner_is_snapshot(self):
        from quintessence.ask import render, SNAPSHOT_BANNER
        r = {"answer": "a", "sources": []}
        self.assertEqual(render(r).splitlines()[-1], SNAPSHOT_BANNER)

    def test_render_transcript_banner_override(self):
        from quintessence.ask import render, TRANSCRIPT_BANNER
        r = {"answer": "a", "sources": []}
        self.assertEqual(render(r, banner=TRANSCRIPT_BANNER).splitlines()[-1], TRANSCRIPT_BANNER)


class TestStoreProvenance(unittest.TestCase):
    """P8 recall composition: a hit tagged with 'store' (by quintessence.searchcompose) carries
    that provenance through Ask into the rendered sources — ADDITIVELY, only when present. A
    bare (non-composed) hit never has this key, so a degenerate ask() renders byte-identical to
    pre-P8 in every path (build_evidence, _degraded, _no_evidence)."""

    def _hit(self, score, store=None, path="quintessence/x.md", degraded=False):
        h = {"score": score, "source": "qq", "path": path, "title": "t", "snippet": "s", "text": "body"}
        if store is not None:
            h["store"] = store
        if degraded:
            h["degraded"] = True
        return h

    def test_build_evidence_carries_store_when_present(self):
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [])
            _evidence, sources = asker.build_evidence([self._hit(0.6, store="project")], 10_000)
            self.assertEqual(sources[0]["store"], "project")

    def test_build_evidence_omits_store_key_when_absent(self):
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [])
            _evidence, sources = asker.build_evidence([self._hit(0.6)], 10_000)
            self.assertNotIn("store", sources[0])

    def test_degraded_path_carries_store_when_present(self):
        result = self._degraded_result([self._hit(0.5, store="user")])
        self.assertEqual(result["sources"][0]["store"], "user")

    def test_no_evidence_path_carries_store_when_present(self):
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [], QQ_ASK_MIN_SIM="0.9")
            result = asker._no_evidence([self._hit(0.1, store="project")])
            self.assertEqual(result["sources"][0]["store"], "project")

    @staticmethod
    def _degraded_result(hits):
        from quintessence.ask import Ask
        return Ask._degraded(hits)

    def test_render_appends_store_tag_only_when_present(self):
        from quintessence.ask import render
        composed = {"answer": "a", "sources": [{"n": 1, "path": "p.md", "title": "t",
                                                  "score": 0.5, "store": "project"}]}
        out = render(composed)
        self.assertIn("project", out)
        self.assertIn("(score 0.500, project)", out)

    def test_render_no_store_tag_when_absent_byte_identical_shape(self):
        from quintessence.ask import render
        bare = {"answer": "a", "sources": [{"n": 1, "path": "p.md", "title": "t", "score": 0.5}]}
        out = render(bare)
        self.assertIn("(score 0.500)", out)
        self.assertNotIn("project", out)
        self.assertNotIn("user", out)


class TestRecencySignals(unittest.TestCase):
    """Recency: updated dates in citations, newest-update context in evidence,
    staleness annotations threaded to rendered warnings."""

    def _hit(self, score=0.6, path="p.md", text="body"):
        return {"score": score, "source": "qq", "path": path, "title": "t",
                "snippet": "s", "text": text}

    # ---- citation updated-date rendering ---------------------------------------------------

    def test_render_citation_carries_updated_date(self):
        result = {"answer": "a", "sources": [
            {"n": 1, "path": "p.md", "title": "t", "score": 0.5,
             "updated": "2026-07-30T09:00Z change"}]}
        out = render(result)
        self.assertIn("(score 0.500, updated 2026-07-30T09:00Z change)", out)

    def test_render_citation_omits_updated_when_absent(self):
        result = {"answer": "a", "sources": [
            {"n": 1, "path": "p.md", "title": "t", "score": 0.5}]}
        out = render(result)
        self.assertIn("(score 0.500)", out)
        self.assertNotIn("updated", out)

    def test_render_citation_updated_before_store(self):
        result = {"answer": "a", "sources": [
            {"n": 1, "path": "p.md", "title": "t", "score": 0.5,
             "updated": "2026-07-30T09:00Z note", "store": "project"}]}
        out = render(result)
        self.assertIn("(score 0.500, updated 2026-07-30T09:00Z note, project)", out)

    # ---- staleness warnings in rendered citations ------------------------------------------

    def test_render_warning_lines_under_citation(self):
        result = {"answer": "a", "sources": [
            {"n": 1, "path": "p.md", "title": "t", "score": 0.5,
             "warnings": ["⚠ referent changed since written: /home (2026-07-30)"]}]}
        out = render(result)
        lines = out.splitlines()
        citation_idx = next(i for i, l in enumerate(lines) if l.strip().startswith("[1]"))
        self.assertEqual(lines[citation_idx + 1],
                         "      ⚠ referent changed since written: /home (2026-07-30)")

    def test_render_no_warning_lines_when_no_warnings(self):
        result = {"answer": "a", "sources": [
            {"n": 1, "path": "p.md", "title": "t", "score": 0.5}]}
        out = render(result)
        self.assertNotIn("⚠", out)

    # ---- newest-update context in evidence -------------------------------------------------

    def test_evidence_includes_newest_update_when_not_in_chunk(self):
        home = os.path.expanduser("~")
        with tempfile.TemporaryDirectory() as base:
            fpath = os.path.join(base, "test-topic.md")
            with open(fpath, "w") as f:
                f.write("# test\n> updated: 2026-07-30T09:00Z major change\n\nContent\n")
            rel = os.path.relpath(fpath, home)
            hit = self._hit(path=rel, text="chunk without the update line")
            asker = make_ask(base, [])
            evidence, _sources = asker.build_evidence([hit], 10_000)
            self.assertIn("newest-update:", evidence)
            self.assertIn("> updated: 2026-07-30T09:00Z major change", evidence)

    def test_evidence_suppresses_newest_update_when_chunk_contains_it(self):
        home = os.path.expanduser("~")
        with tempfile.TemporaryDirectory() as base:
            newest = "> updated: 2026-07-30T09:00Z major change"
            fpath = os.path.join(base, "test-topic.md")
            with open(fpath, "w") as f:
                f.write(f"# test\n{newest}\n\nContent\n")
            rel = os.path.relpath(fpath, home)
            hit = self._hit(path=rel, text=f"chunk with {newest} already in it")
            asker = make_ask(base, [])
            evidence, _sources = asker.build_evidence([hit], 10_000)
            self.assertNotIn("newest-update:", evidence)

    def test_evidence_newest_update_truncated_at_cap(self):
        home = os.path.expanduser("~")
        with tempfile.TemporaryDirectory() as base:
            long_line = "> updated: 2026-07-30T09:00Z " + "x" * 300
            fpath = os.path.join(base, "test-topic.md")
            with open(fpath, "w") as f:
                f.write(f"# test\n{long_line}\n\nContent\n")
            rel = os.path.relpath(fpath, home)
            hit = self._hit(path=rel, text="short chunk")
            asker = make_ask(base, [])
            evidence, _sources = asker.build_evidence([hit], 10_000)
            self.assertIn("newest-update:", evidence)
            for line in evidence.split("\n"):
                if line.startswith("newest-update:"):
                    content = line[len("newest-update: "):]
                    self.assertTrue(content.endswith("…"),
                                    "truncated newest-update must end with ellipsis")
                    self.assertEqual(len(content), Ask._NEWEST_UPDATE_CAP + 1)
                    break
            else:
                self.fail("newest-update line not found in evidence")

    # ---- build_evidence threads updated + warnings into source entries ---------------------

    def test_build_evidence_threads_updated_into_source(self):
        home = os.path.expanduser("~")
        with tempfile.TemporaryDirectory() as base:
            fpath = os.path.join(base, "t.md")
            with open(fpath, "w") as f:
                f.write("# test\n> updated: 2026-07-30T09:00Z note\n")
            rel = os.path.relpath(fpath, home)
            hit = self._hit(path=rel)
            asker = make_ask(base, [])
            _ev, sources = asker.build_evidence([hit], 10_000)
            self.assertEqual(sources[0]["updated"], "2026-07-30T09:00Z note")

    def test_build_evidence_omits_updated_when_no_update_line(self):
        home = os.path.expanduser("~")
        with tempfile.TemporaryDirectory() as base:
            fpath = os.path.join(base, "t.md")
            with open(fpath, "w") as f:
                f.write("# no update line\njust content\n")
            rel = os.path.relpath(fpath, home)
            hit = self._hit(path=rel)
            asker = make_ask(base, [])
            _ev, sources = asker.build_evidence([hit], 10_000)
            self.assertNotIn("updated", sources[0])

    def test_degraded_and_no_evidence_omit_updated_and_warnings(self):
        result = Ask._degraded([self._hit()])
        self.assertNotIn("updated", result["sources"][0])
        self.assertNotIn("warnings", result["sources"][0])
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [], QQ_ASK_MIN_SIM="0.9")
            result = asker._no_evidence([self._hit(0.1)])
            self.assertNotIn("updated", result["sources"][0])
            self.assertNotIn("warnings", result["sources"][0])


if __name__ == "__main__":
    unittest.main()


class AskEscapeHatchReachesSearch20260729(unittest.TestCase):
    """QQ_ASK_UNREDACTED is `ask`'s own documented escape hatch and predates search-side
    filtering. Since `ask` retrieves THROUGH SearchIndex, the day search learned to filter, this
    hatch silently stopped working unless a second, differently-named variable was also set —
    documented unconditionally, doing nothing. One variable, one meaning."""

    def _pair(self, flag):
        from quintessence.search import SearchIndex
        cfg = Config(env={"QQ_ASK_UNREDACTED": flag, "HOME": "/tmp/qq-hatch-test"},
                     config_file="/nonexistent")
        idx = SearchIndex(cfg)
        return Ask(cfg, idx), idx

    def test_hatch_lifts_the_search_side_filter_too(self):
        ask, idx = self._pair("1")
        self.assertTrue(ask.unredacted)
        self.assertTrue(idx.redact_lifted, "ask's escape hatch must lift search-side filtering")

    def test_no_hatch_leaves_search_filtering_on(self):
        ask, idx = self._pair("0")
        self.assertFalse(ask.unredacted)
        self.assertFalse(idx.redact_lifted)

    def test_ask_still_constructible_without_a_search_index(self):
        cfg = Config(env={"QQ_ASK_UNREDACTED": "1"}, config_file="/nonexistent")
        self.assertTrue(Ask(cfg, None).unredacted)   # must not raise


class _FakeResponse:
    """Stand-in for urlopen's context manager. Carries a status (probe reads it) and a body
    (the completion reads it), so ONE fake serves both call sites without branching."""
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = json.dumps(payload or {
            "choices": [{"message": {"content": "an answer [1]"}}]}).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestPerEndpointModelNaming(unittest.TestCase):
    """An endpoint that SELECTS a model by name (ollama) needs two things a llama-server
    endpoint does not: the model named in the body, and a health probe it actually serves.
    Both are keyed off QQ_ASK_MODELS, so an unnamed endpoint must be untouched — that is what
    keeps the resident-llama-server tiers behaving exactly as before."""

    OLLAMA = "http://127.0.0.1:11434"
    LLAMA = "http://127.0.0.1:8095"
    MODELS = f"{OLLAMA}=qwen3:4b-instruct"

    def _asker(self, base, **over):
        return make_ask(base, [], QQ_ASK_MODELS=self.MODELS, **over)

    def test_named_endpoint_is_probed_where_ollama_answers(self):
        # ollama serves /v1/models and 404s /health, so probing /health would mark a healthy
        # on-demand tier as down and the ladder would silently fall past it. The fake serves a
        # list CONTAINING the configured model because the probe now reads that list rather
        # than the status alone; what this test pins is still which PATH is asked.
        with tempfile.TemporaryDirectory() as base:
            asker = self._asker(base)
            seen = []

            def fake_urlopen(url, timeout=None):
                seen.append(url)
                return _FakeResponse(status=200,
                                     payload={"data": [{"id": "qwen3:4b-instruct"}]})

            with unittest.mock.patch.object(ask_module.httpdirect, "urlopen", fake_urlopen):
                self.assertTrue(asker.probe_endpoint(self.OLLAMA))
            self.assertEqual(seen, [f"{self.OLLAMA}/v1/models"])

    def test_unnamed_endpoint_still_probed_at_health(self):
        # llama-server answers /health 503 WHILE LOADING; that is how the ladder skips a tier
        # that is not ready. Probing /v1/models there would pick a server mid-load.
        with tempfile.TemporaryDirectory() as base:
            asker = self._asker(base)
            seen = []

            def fake_urlopen(url, timeout=None):
                seen.append(url)
                return _FakeResponse(status=200)

            with unittest.mock.patch.object(ask_module.httpdirect, "urlopen", fake_urlopen):
                self.assertTrue(asker.probe_endpoint(self.LLAMA))
            self.assertEqual(seen, [f"{self.LLAMA}/health"])

    def test_named_endpoint_sends_the_model_name(self):
        with tempfile.TemporaryDirectory() as base:
            asker = self._asker(base)
            sent = {}

            def fake_urlopen(req, timeout=None):
                sent.update(json.loads(req.data.decode()))
                return _FakeResponse()

            with unittest.mock.patch.object(ask_module.httpdirect, "urlopen", fake_urlopen):
                asker._complete(self.OLLAMA, "q", "evidence")
            self.assertEqual(sent.get("model"), "qwen3:4b-instruct")

    def test_unnamed_endpoint_sends_no_model_key(self):
        # Not a placeholder: a name an endpoint does not have is a 404, which the ladder reads
        # as "healthy but the completion failed" and gives up on rather than stepping down.
        with tempfile.TemporaryDirectory() as base:
            asker = self._asker(base)
            sent = {}

            def fake_urlopen(req, timeout=None):
                sent.update(json.loads(req.data.decode()))
                return _FakeResponse()

            with unittest.mock.patch.object(ask_module.httpdirect, "urlopen", fake_urlopen):
                asker._complete(self.LLAMA, "q", "evidence")
            self.assertNotIn("model", sent)

    def test_naming_a_model_leaves_that_endpoint_budget_alone(self):
        # The seam: budgets are keyed by the endpoint URL. Carrying the model in its own key
        # (rather than decorating the endpoint string) is what keeps that lookup intact.
        with tempfile.TemporaryDirectory() as base:
            asker = self._asker(base, QQ_ASK_BUDGETS=f"{self.OLLAMA}=8000")
            self.assertEqual(asker._budget_for(self.OLLAMA), 8000)
            self.assertEqual(asker.models[self.OLLAMA], "qwen3:4b-instruct")


class TestSlotPinning(unittest.TestCase):
    """`id_slot` names WHICH slot of a resident multi-slot llama-server serves this request.
    Unpinned, the server places the request on whichever slot it likes — slot 0 on an idle
    server, which is the slot HA voice is pinned to and the only slot `ha-model-router` asks
    about when it decides the backend is busy. So a `qq ask` landing there makes a voice
    question wait behind a 200k-character evidence pack, or be sent to the 4B floor with the
    125B loaded and idle (measured 2026-09-10: a trivial voice question answered after 32s).

    The other direction matters just as much: llama-server REFUSES a request naming a slot it
    does not have, and `ask` reads a refusal as "this rung failed" and steps down the ladder.
    So a value that is not a slot number must send nothing at all rather than something
    approximate — an endpoint whose config carries a typo must behave exactly like an endpoint
    with no slot configured, not like a rung that is down."""

    RESIDENT = "http://127.0.0.1:8090"
    OTHER = "http://127.0.0.1:11434"

    def _sent_body(self, asker, endpoint):
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent.update(json.loads(req.data.decode()))
            return _FakeResponse()

        with unittest.mock.patch.object(ask_module.httpdirect, "urlopen", fake_urlopen):
            asker._complete(endpoint, "q", "evidence")
        return sent

    def test_endpoint_with_a_slot_sends_it(self):
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [], QQ_ASK_SLOTS=f"{self.RESIDENT}=1")
            self.assertEqual(self._sent_body(asker, self.RESIDENT).get("id_slot"), 1)

    def test_the_slot_goes_on_the_wire_as_a_number_not_a_string(self):
        # llama-server parses `id_slot` as an integer; a quoted "1" is a type error there, and
        # the request is refused exactly like a slot that does not exist. The config value
        # arrives as text, so the conversion is the whole point of this assertion.
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [], QQ_ASK_SLOTS=f"{self.RESIDENT}=1")
            self.assertIsInstance(self._sent_body(asker, self.RESIDENT).get("id_slot"), int)

    def test_slot_zero_is_sent_rather_than_dropped_as_falsy(self):
        # 0 is a real slot — the voice slot — not "no slot". A presence test written as
        # `if slot:` instead of `if slot is not None:` drops it silently, and the endpoint
        # then behaves as if it had never been configured.
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [], QQ_ASK_SLOTS=f"{self.RESIDENT}=0")
            self.assertEqual(self._sent_body(asker, self.RESIDENT).get("id_slot"), 0)

    def test_endpoint_without_a_slot_sends_no_such_key(self):
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [], QQ_ASK_SLOTS=f"{self.RESIDENT}=1")
            self.assertNotIn("id_slot", self._sent_body(asker, self.OTHER))

    def test_no_slots_configured_at_all_sends_no_such_key(self):
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [])
            self.assertNotIn("id_slot", self._sent_body(asker, self.RESIDENT))

    def test_a_malformed_value_sends_no_such_key(self):
        # Each of these is a plausible hand-edit of the config line. None of them is a slot
        # number, and the requirement is the same for all: send nothing, so the rung answers
        # normally instead of being refused and read as down. "-1" is llama-server's own
        # "any slot" sentinel and is deliberately NOT honoured here — this key exists to pin a
        # slot, and a config that fails to name one has not pinned anything. "²" is here
        # because `str.isdigit()` accepts it while `int()` raises on it: a guard written with
        # isdigit alone crashes `ask` at construction rather than ignoring the entry.
        for value in ("one", "", "1.5", "-1", "1 2", "²", "0x1"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as base:
                asker = make_ask(base, [], QQ_ASK_SLOTS=f"{self.RESIDENT}={value}")
                self.assertNotIn("id_slot", self._sent_body(asker, self.RESIDENT))

    def test_pinning_a_slot_leaves_that_endpoints_other_per_endpoint_settings_alone(self):
        # The seam: budgets and model names are keyed by the endpoint URL too. Carrying the
        # slot in its own key, rather than decorating the endpoint string, is what keeps those
        # lookups intact — the same reason QQ_ASK_MODELS is its own key.
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [], QQ_ASK_SLOTS=f"{self.RESIDENT}=1",
                             QQ_ASK_BUDGETS=f"{self.RESIDENT}=8000",
                             QQ_ASK_MODELS=f"{self.RESIDENT}=some-model")
            self.assertEqual(asker._budget_for(self.RESIDENT), 8000)
            self.assertEqual(asker._model_for(self.RESIDENT), "some-model")
            self.assertEqual(asker._slot_for(self.RESIDENT), 1)

    def test_a_pinned_endpoint_is_still_probed_at_health(self):
        # Pinning a slot says nothing about which model the server serves, so it must not
        # move the endpoint onto the /v1/models probe the way naming a model does. A resident
        # llama-server answers /health 503 while loading, and that is how the ladder skips a
        # tier that is not ready yet.
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [], QQ_ASK_SLOTS=f"{self.RESIDENT}=1")
            seen = []

            def fake_urlopen(url, timeout=None):
                seen.append(url)
                return _FakeResponse(status=200)

            with unittest.mock.patch.object(ask_module.httpdirect, "urlopen", fake_urlopen):
                self.assertTrue(asker.probe_endpoint(self.RESIDENT))
            self.assertEqual(seen, [f"{self.RESIDENT}/health"])


class TestPerEndpointDirectives(unittest.TestCase):
    """Quantifies over the derivation rather than over the two directives that exist today.
    `Ask._BODY_DIRECTIVES` is the one table `_complete` builds the body by walking, so a third
    directive added tomorrow is held to the same rule by this test without anyone remembering
    to come back and add it here."""

    ENDPOINT = "http://127.0.0.1:8090"

    def test_the_table_is_the_one_account_and_every_accessor_exists(self):
        self.assertTrue(Ask._BODY_DIRECTIVES, "no per-endpoint directives declared")
        for key, accessor in Ask._BODY_DIRECTIVES:
            with self.subTest(directive=key):
                self.assertTrue(callable(getattr(Ask, accessor, None)),
                                f"{key} names accessor {accessor!r}, which is not a method")

    def test_every_directive_is_absent_when_config_names_none(self):
        # The general form of "a wrong directive is a refused request": with nothing
        # configured for an endpoint, the body must carry none of these keys at all — not a
        # default, not a zero, not an empty string.
        with tempfile.TemporaryDirectory() as base:
            asker = make_ask(base, [])
            sent = {}

            def fake_urlopen(req, timeout=None):
                sent.update(json.loads(req.data.decode()))
                return _FakeResponse()

            with unittest.mock.patch.object(ask_module.httpdirect, "urlopen", fake_urlopen):
                asker._complete(self.ENDPOINT, "q", "evidence")
            for key, _ in Ask._BODY_DIRECTIVES:
                with self.subTest(directive=key):
                    self.assertNotIn(key, sent)


class TestWholeNumberValueSpellings(unittest.TestCase):
    """`url=<whole number>` entries are read ONLY when the value is spelled in plain ASCII
    digits; every other spelling is ignored exactly like any other malformed entry, leaving
    the endpoint unconfigured.

    The gap this pins: `int()`'s grammar is wider than "whole number". It accepts digit
    separators ("1_0" -> 10), a leading sign ("+1" -> 1; "-0" -> 0, which then survives a
    `>= 0` guard), and every Unicode decimal digit (U+0663 -> 3, fullwidth one -> 1). A
    config line whose value only a Python cast recognises has not named a whole number, and
    honouring it puts a number on the wire that the operator never wrote — a slot the
    request never meant to pin, a budget the config never named. The failure the operator
    meets: the endpoint is served on the wrong slot / with the wrong pack size, silently.

    Quantified over BOTH keys shaped this way (QQ_ASK_SLOTS, QQ_ASK_BUDGETS): one
    recognition point parses both, so the sentence is one rule with two surfaces and a
    check on one key alone would leave the other unpinned.

    Directions pinned here:
      1. each non-plain-digit spelling leaves the endpoint unconfigured (slots: no `id_slot`
         on the wire; budgets: the unconfigured default), for both keys;
      2. plain ASCII digits ARE read — "1" and "0", including that 0 is a real value for
         both keys and not dropped as falsy;
      3. a bogus entry is ignored like any other malformed one: no crash at construction,
         and the other entries of the same list are still read."""

    PINNED_URL = "http://127.0.0.1:99"   # not in the _BUDGET defaults: unconfigured == _DEFAULT_BUDGET
    OTHER_URL = "http://127.0.0.1:98"

    BOGUS_SPELLINGS = ("1_0", "+1", "-0", "\u0663", "\uff11", "\u00b2", "-1", "1e3")
    WHOLE_NUMBER_KEYS = ("QQ_ASK_SLOTS", "QQ_ASK_BUDGETS")

    def _sent_body(self, asker, endpoint):
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent.update(json.loads(req.data.decode()))
            return _FakeResponse()

        with unittest.mock.patch.object(ask_module.httpdirect, "urlopen", fake_urlopen):
            asker._complete(endpoint, "q", "evidence")
        return sent

    def _assert_unconfigured(self, asker, url, key, msg):
        if key == "QQ_ASK_SLOTS":
            self.assertIsNone(asker._slot_for(url), msg)
            self.assertNotIn("id_slot", self._sent_body(asker, url), msg)
        else:
            self.assertEqual(asker._budget_for(url), ask_module._DEFAULT_BUDGET, msg)

    def test_bogus_spellings_leave_the_endpoint_unconfigured(self):
        for key in self.WHOLE_NUMBER_KEYS:
            for value in self.BOGUS_SPELLINGS:
                with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as base:
                    asker = make_ask(base, [], **{key: f"{self.PINNED_URL}={value}"})
                    self._assert_unconfigured(
                        asker, self.PINNED_URL, key,
                        f"{key}={value!r}: a value only int() recognises must not be read")

    def test_plain_ascii_digits_are_read(self):
        for key in self.WHOLE_NUMBER_KEYS:
            for value in ("1", "0"):
                with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as base:
                    asker = make_ask(base, [], **{key: f"{self.PINNED_URL}={value}"})
                    if key == "QQ_ASK_SLOTS":
                        self.assertEqual(asker._slot_for(self.PINNED_URL), int(value))
                        sent = self._sent_body(asker, self.PINNED_URL)
                        self.assertEqual(sent.get("id_slot"), int(value))
                    else:
                        self.assertEqual(asker._budget_for(self.PINNED_URL), int(value))

    def test_a_bogus_entry_is_ignored_like_any_other_malformed_entry(self):
        # The list survives: a bad line is skipped, not fatal, and does not shadow the
        # well-formed entries beside it.
        for key in self.WHOLE_NUMBER_KEYS:
            for value in self.BOGUS_SPELLINGS:
                with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as base:
                    asker = make_ask(base, [],
                                     **{key: f"{self.PINNED_URL}=1,{self.OTHER_URL}={value}"})
                    if key == "QQ_ASK_SLOTS":
                        self.assertEqual(asker._slot_for(self.PINNED_URL), 1)
                    else:
                        self.assertEqual(asker._budget_for(self.PINNED_URL), 1)
                    self._assert_unconfigured(
                        asker, self.OTHER_URL, key,
                        f"{key}: bogus {value!r} beside a good entry must be skipped, not fatal")
