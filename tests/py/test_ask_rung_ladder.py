# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Certification check for `a-rung-that-fails-to-complete-falls-to-the-next`.

    Every configured endpoint is asked in turn until one answers: a rung that certifies and
    then fails to complete is followed by the next rung, raw retrieval is what a question gets
    only when every rung has failed, and the rung that answered is named on a surface a person
    or a check can read.

Written against that sentence, not against the code that satisfies it, so the seam is the one
place the sentence can be observed from outside: `httpdirect.urlopen`, the proxy-free opener every
store-text HTTP call goes through. Every rung the ladder
reaches shows up there twice — once as the certification probe (`/health` or `/v1/models`) and
once as the completion POST — and nothing about how `ask()` is arranged internally is visible
to, or assumed by, the assertions below. A ladder rewritten around a different helper still has
to make those calls in that order to satisfy the sentence.

The sentence has five directions, and the tests are grouped by them:

  * a rung that certifies and then fails to complete is followed by the next one. "Fails to
    complete" is not one shape: the transport can raise, the server can answer 500, the server
    can answer 200 with no answer text in it, and the server can answer 200 with a body that is
    not the shape the caller reads. A ladder that steps down on the first of those and gives up
    on the others holds the sentence only for the failure someone happened to reproduce.
  * a rung that fails to CERTIFY is skipped, and skipping it is not the end of the ladder.
  * the rungs are asked in configured order, each at most once.
  * raw retrieval is what the question gets only when every rung has failed — never while a
    configured rung is still unasked.
  * the rung that answered is named: `result["endpoint"]` is the rung that produced the answer,
    which is the rung the ladder stopped at, not the one it started at.

Two of these hold at any revision where the ladder probes before it picks (probe-skipping, and
stopping at the first rung that answers); they are here because the sentence has them and a
ladder rebuilt around completion failures is exactly where they would be lost.

The stand-in server answers its probe the way the two real ones do — `{"status": "ok"}` at
llama-server's `/health`, an OpenAI-shaped model list at ollama's `/v1/models` — rather than 200
with an empty body. A probe stub that returns a body no server returns is a stub of a server
that does not exist, and a ladder is entitled to read the body it certified against.
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence import ask as ask_module  # noqa: E402
from quintessence.ask import Ask, render  # noqa: E402
from quintessence.config import Config  # noqa: E402

ANSWER = "the grounded answer [1]"

# How a rung behaves when the ladder reaches its completion call. Every one of these except
# ANSWERS is a rung that certified and then failed to complete.
ANSWERS = "answers"
RAISES = "raises"            # transport gave way (socket timeout, connection reset)
HTTP_500 = "http-500"        # the server answered, with an error
NO_TEXT = "no-text"          # 200, and neither content nor reasoning_content carries an answer
MALFORMED = "malformed"      # 200, and the body is not the shape the completion reader reads


class _Rung:
    """One configured endpoint: whether it certifies, and what its completion call does.

    `model` marks it as the kind of server that SELECTS a model by name (ollama); such a rung
    is probed at `/v1/models` and is asked with that name in the body."""

    def __init__(self, url, certifies=True, completion=ANSWERS, answer=ANSWER, model=None):
        self.url = url
        self.certifies = certifies
        self.completion = completion
        self.answer = answer
        self.model = model


class _Response:
    """urlopen's context manager: `.status` for the probe, `.read()` for the body."""
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = json.dumps(payload if payload is not None else {}).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Ladder:
    """A stand-in for every endpoint the ask ladder can reach, and a record of what it asked.

    `calls` is the whole traffic in order — ("certify"|"complete", url) — which is what makes
    "asked in turn", "at most once" and "never asked at all" all readable off one object."""

    def __init__(self, *rungs):
        self.rungs = list(rungs)
        self.calls = []
        self.bodies = {}

    @property
    def urls(self):
        return [r.url for r in self.rungs]

    @property
    def models_setting(self):
        """QQ_ASK_MODELS for the rungs that select a model by name."""
        return ",".join(f"{r.url}={r.model}" for r in self.rungs if r.model)

    @property
    def asked(self):
        """The rungs whose COMPLETION was called, in order."""
        return [url for kind, url in self.calls if kind == "complete"]

    @property
    def certified(self):
        return [url for kind, url in self.calls if kind == "certify"]

    def _rung_for(self, url):
        for r in self.rungs:
            if url.startswith(r.url + "/"):
                return r
        raise AssertionError(f"the ladder reached an endpoint nothing configured: {url}")

    def urlopen(self, target, timeout=None):
        url = target if isinstance(target, str) else target.full_url
        rung = self._rung_for(url)
        if url.endswith("/v1/chat/completions"):
            self.calls.append(("complete", rung.url))
            if not isinstance(target, str):
                self.bodies[rung.url] = json.loads(target.data.decode())
            return self._complete(rung)
        self.calls.append(("certify", rung.url))
        if not rung.certifies:
            raise urllib.error.URLError("nothing listening")
        return self._certify(rung)

    @staticmethod
    def _certify(rung):
        if rung.model:
            return _Response(payload={"object": "list",
                                      "data": [{"id": rung.model, "object": "model"}]})
        return _Response(payload={"status": "ok"})

    @staticmethod
    def _complete(rung):
        if rung.completion is RAISES:
            raise urllib.error.URLError("the read timed out")
        if rung.completion is HTTP_500:
            raise urllib.error.HTTPError(rung.url, 500, "context window exceeded", {}, None)
        if rung.completion is NO_TEXT:
            return _Response(payload={"choices": [{"message": {"content": "",
                                                               "reasoning_content": ""}}]})
        if rung.completion is MALFORMED:
            return _Response(payload={"error": "no choices in this body"})
        return _Response(payload={"choices": [{"message": {"content": rung.answer}}]})


class _LadderCase(unittest.TestCase):
    HIT = {"score": 0.9, "source": "qq", "path": "p.md", "title": "t",
           "snippet": "s", "text": "body"}

    class _Index:
        def __init__(self, hits):
            self._hits = hits

        def search(self, query, k=6, source=None, min_sim=None):
            return list(self._hits)

    def run_ask(self, ladder, hits=None, **overrides):
        """Put a question to a ladder of rungs and return `ask()`'s result."""
        with tempfile.TemporaryDirectory() as base:
            over = {"QQ_ASK_ENDPOINTS": ",".join(ladder.urls),
                    "QQ_ASK_MODELS": ladder.models_setting,
                    "QQ_REDACT_FILE": os.path.join(base, "redact-slugs")}
            over.update(overrides)
            cfg = Config(env={}, config_file="/nonexistent", overrides=over)
            asker = Ask(cfg, self._Index([self.HIT] if hits is None else hits))
            with unittest.mock.patch.object(ask_module.httpdirect, "urlopen",
                                            ladder.urlopen):
                return asker.ask("a question the store can answer")


class TestARungThatFailsToCompleteFallsToTheNext(_LadderCase):
    """The first direction, once per shape a completion can fail in."""

    def _falls_through(self, failure):
        first, second = "http://127.0.0.1:8090", "http://127.0.0.1:11435"
        ladder = _Ladder(_Rung(first, completion=failure), _Rung(second, answer="from the second"))
        result = self.run_ask(ladder)
        self.assertEqual(ladder.asked, [first, second],
                         f"a rung that certified and then failed to complete ({failure}) must be "
                         f"followed by the next rung; the ladder asked {ladder.asked}")
        self.assertFalse(result["degraded"],
                         "a later rung answered, so the question must not get raw retrieval")
        self.assertEqual(result["answer"], "from the second")

    def test_transport_failure_falls_to_the_next_rung(self):
        self._falls_through(RAISES)

    def test_server_error_falls_to_the_next_rung(self):
        self._falls_through(HTTP_500)

    def test_a_200_with_no_answer_text_falls_to_the_next_rung(self):
        # The server answered and the answer was empty. It did not complete, so the ladder owes
        # the question the next rung, not a blank line and not raw retrieval.
        self._falls_through(NO_TEXT)

    def test_a_200_the_completion_reader_cannot_read_falls_to_the_next_rung(self):
        self._falls_through(MALFORMED)

    def test_the_next_rung_is_asked_as_itself(self):
        # Falling to a rung and then asking it under the previous rung's identity is not asking
        # it: an endpoint that SELECTS a model by name 404s on a name it does not hold, so a
        # ladder that carries the first rung's body down has fallen to a rung it has guaranteed
        # will fail.
        first, second = "http://127.0.0.1:8090", "http://127.0.0.1:11434"
        ladder = _Ladder(_Rung(first, completion=RAISES),
                         _Rung(second, model="qwen3:4b-instruct"))
        self.run_ask(ladder)
        self.assertEqual(ladder.bodies[second].get("model"), "qwen3:4b-instruct",
                         "the rung the ladder fell to must be asked with its own model name")
        self.assertNotIn("model", ladder.bodies[first],
                         "the rung that has no configured model must be asked without one")


class TestEveryRungIsAskedInTurn(_LadderCase):
    """Asked IN TURN: configured order, each rung once, and no rung skipped over."""

    THREE = ("http://127.0.0.1:8090", "http://127.0.0.1:11435", "http://127.0.0.1:8095")

    def test_rungs_are_asked_in_configured_order_until_one_answers(self):
        a, b, c = self.THREE
        ladder = _Ladder(_Rung(a, completion=RAISES), _Rung(b, completion=NO_TEXT),
                         _Rung(c, answer="from the third"))
        result = self.run_ask(ladder)
        self.assertEqual(ladder.asked, [a, b, c])
        self.assertEqual(result["answer"], "from the third")

    def test_no_rung_is_asked_twice(self):
        a, b, c = self.THREE
        ladder = _Ladder(_Rung(a, completion=RAISES), _Rung(b, completion=HTTP_500),
                         _Rung(c, completion=MALFORMED))
        self.run_ask(ladder)
        self.assertEqual(ladder.asked, [a, b, c],
                         "each configured rung is asked exactly once, in order")

    def test_a_rung_that_answers_stops_the_ladder(self):
        a, b, c = self.THREE
        ladder = _Ladder(_Rung(a), _Rung(b), _Rung(c))
        self.run_ask(ladder)
        self.assertEqual(ladder.asked, [a],
                         "the ladder is asked in turn UNTIL one answers; the first one did")

    def test_a_rung_that_fails_to_certify_is_skipped_and_the_ladder_goes_on(self):
        a, b, c = self.THREE
        ladder = _Ladder(_Rung(a, certifies=False), _Rung(b, completion=RAISES),
                         _Rung(c, answer="from the third"))
        result = self.run_ask(ladder)
        self.assertEqual(ladder.asked, [b, c],
                         "a rung that never certified is not asked to complete, and its failure "
                         "does not end the ladder")
        self.assertEqual(result["answer"], "from the third")


class TestRawRetrievalIsTheLastResort(_LadderCase):
    """Raw retrieval is what a question gets ONLY when every rung has failed."""

    def test_raw_retrieval_only_after_every_rung_has_been_asked(self):
        a, b = "http://127.0.0.1:8090", "http://127.0.0.1:11435"
        ladder = _Ladder(_Rung(a, completion=RAISES), _Rung(b, completion=NO_TEXT))
        result = self.run_ask(ladder)
        self.assertEqual(ladder.asked, [a, b],
                         "raw retrieval was returned with a configured rung still unasked")
        self.assertTrue(result["degraded"])
        self.assertIsNone(result["endpoint"])
        self.assertIn("raw retrieval", result["answer"])

    def test_a_mixed_ladder_reaches_raw_retrieval_only_at_the_bottom(self):
        a, b, c = ("http://127.0.0.1:8090", "http://127.0.0.1:11435", "http://127.0.0.1:8095")
        ladder = _Ladder(_Rung(a, certifies=False), _Rung(b, completion=HTTP_500),
                         _Rung(c, completion=MALFORMED))
        result = self.run_ask(ladder)
        self.assertEqual(ladder.certified, [a, b, c],
                         "every configured rung is offered its certification probe")
        self.assertEqual(ladder.asked, [b, c],
                         "every rung that certified is asked before raw retrieval is the answer")
        self.assertTrue(result["degraded"])

    def test_one_working_rung_below_a_failing_one_is_never_raw_retrieval(self):
        a, b = "http://127.0.0.1:8090", "http://127.0.0.1:11435"
        ladder = _Ladder(_Rung(a, completion=RAISES), _Rung(b))
        result = self.run_ask(ladder)
        self.assertFalse(result["degraded"],
                         "a question whose second rung answers must not be given raw retrieval")
        self.assertNotIn("QA unavailable", result["answer"])


class TestTheRungThatAnsweredIsNamed(_LadderCase):
    """A person or a check must be able to read WHICH rung answered — after a fall-through the
    honest name is the rung that produced the text, never the rung the ladder started at."""

    def test_the_named_rung_is_the_one_that_answered_not_the_one_first_tried(self):
        a, b = "http://127.0.0.1:8090", "http://127.0.0.1:11435"
        ladder = _Ladder(_Rung(a, completion=RAISES), _Rung(b, answer="from the second"))
        result = self.run_ask(ladder)
        self.assertEqual(result["endpoint"], b,
                         "the answer came from the second rung and must be attributed to it")

    def test_the_named_rung_is_the_one_that_answered_three_rungs_down(self):
        a, b, c = ("http://127.0.0.1:8090", "http://127.0.0.1:11435", "http://127.0.0.1:8095")
        ladder = _Ladder(_Rung(a, certifies=False), _Rung(b, completion=NO_TEXT),
                         _Rung(c, answer="from the third"))
        result = self.run_ask(ladder)
        self.assertEqual(result["endpoint"], c)


class TestThePrintedAnswerNamesTheRungThatAnswered(_LadderCase):
    """The same sentence on the surface a PERSON reads: what `render()` prints (the text the
    qq-ask CLI shows) carries `answered by <result["endpoint"]>`, including after a step-down.
    Deleting the line, or printing the first-picked rung, must turn these red."""

    @staticmethod
    def _printed_rung(text):
        lines = [l for l in text.splitlines() if l.startswith("answered by ")]
        assert len(lines) == 1, f"expected exactly one 'answered by' line, got {lines}"
        return lines[0][len("answered by "):]

    def test_a_stepped_down_answer_is_printed_as_the_second_rung(self):
        a, b = "http://127.0.0.1:8090", "http://127.0.0.1:11435"
        ladder = _Ladder(_Rung(a, completion=RAISES), _Rung(b, answer="from the second"))
        result = self.run_ask(ladder)
        self.assertEqual(result["endpoint"], b)
        printed = render(result)
        self.assertEqual(self._printed_rung(printed), b,
                         "the printed text must name the rung that answered, not the first tried")
        self.assertNotIn(a, printed)

    def test_control_a_first_rung_answer_is_printed_as_the_first_rung(self):
        a, b = "http://127.0.0.1:8090", "http://127.0.0.1:11435"
        ladder = _Ladder(_Rung(a, answer="from the first"), _Rung(b))
        result = self.run_ask(ladder)
        self.assertEqual(self._printed_rung(render(result)), a)
        self.assertEqual(self._printed_rung(render(result)), result["endpoint"])


if __name__ == "__main__":
    unittest.main()
