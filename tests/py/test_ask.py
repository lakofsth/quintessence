# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.ask: Config plumbing, the minimum-similarity floor
+ its "nothing relevant" refusal path (never applied to keyword-fallback hits), the redaction
ladder (unchanged from qqask_core.filter_redacted), and the no-completion-endpoint degrade
path (degrade behaviour preserved)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

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
