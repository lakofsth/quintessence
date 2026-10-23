# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Certification check for the invariant

    a-named-endpoint-is-certified-against-what-it-says-it-serves — An endpoint whose request
    will name a model passes its probe only when that model is in the list the endpoint
    itself publishes; a process answering is not by itself a certification.

Written against the SENTENCE, not against any endpoint's code path, by a session that has not
seen the fix. Two surfaces in this engine send a request that NAMES a model, so both are in
scope and both are exercised here:

  * `Ask._complete` puts `body["model"]` on an endpoint listed in QQ_ASK_MODELS; its probe is
    `Ask.probe_endpoint`.
  * `SearchIndex._embed_call` puts `"model": self.embed_model` on QQ_OLLAMA_URL; its probe is
    `SearchIndex.probe_embedder`.

The fakes below answer EVERYTHING the probe might ask — /health, /v1/models and /api/tags all
return 200 — and vary only the CONTENT of the published list. That is the sentence's second
clause made mechanical: a probe that passes because a process answered passes these cases too,
and a probe that certifies against the list can only pass the ones where the name is in it.
Serving the list at both conventional paths, in both conventional shapes, keeps the check about
what was certified rather than about which path a given implementation chose to read.
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence import ask as ask_module
from quintessence import search as search_module
from quintessence.ask import Ask
from quintessence.config import Config
from quintessence.search import SearchIndex


class _Answer:
    """urlopen's context manager, reduced to what a probe reads: a status and a body. `read()`
    with no arguments is what `json.load(r)` calls, so one fake serves that and `json.loads(
    r.read())` alike."""

    def __init__(self, status=200, body=b""):
        self.status = status
        self.code = status
        self._body = body

    def getcode(self):
        return self.status

    def read(self, *a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _openai_list(*names):
    """What an OpenAI-compatible server publishes at /v1/models."""
    return json.dumps({"object": "list",
                       "data": [{"id": n, "object": "model"} for n in names]}).encode()


def _ollama_tags(*names):
    """What ollama publishes at /api/tags."""
    return json.dumps({"models": [{"name": n} for n in names]}).encode()


def _endpoint_serving(base, *names, **kw):
    """Routes for ONE endpoint that is up and answering. It serves /health 200 (a live process),
    and publishes `names` at both list paths. `body` overrides the published list with a literal
    payload, for the cases where the answer carries no usable list at all."""
    body = kw.get("body")
    status = kw.get("status", 200)
    return {
        f"{base}/health": (status, b'{"status":"ok"}'),
        f"{base}/v1/models": (status, _openai_list(*names) if body is None else body),
        f"{base}/api/tags": (status, _ollama_tags(*names) if body is None else body),
    }


def _serving(routes, seen=None):
    """A urlopen stand-in over a url -> (status, body) map. A url with no route raises, which is
    what an endpoint that is not there does — so a probe cannot pass on a path nobody serves."""

    def fake_urlopen(req, timeout=None, *a, **kw):
        url = getattr(req, "full_url", req)
        if seen is not None:
            seen.append(url)
        if url not in routes:
            raise urllib.error.URLError(f"no route for {url}")
        status, body = routes[url]
        return _Answer(status=status, body=body)

    return fake_urlopen


def _asker(base, endpoints, models, **over):
    cfg = Config(env={}, config_file="/nonexistent",
                 overrides=dict({"QQ_ASK_ENDPOINTS": endpoints,
                                 "QQ_ASK_MODELS": models,
                                 "QQ_REDACT_FILE": os.path.join(base, "redact-slugs")}, **over))
    return Ask(cfg, None)


NAMED = "http://127.0.0.1:11434"
OTHER = "http://127.0.0.1:11436"
UNNAMED = "http://127.0.0.1:8095"
MODEL = "qwen3:4b-instruct"
ELSEWHERE = "llama3.2:1b"


class TestNamedAskEndpointIsCertifiedAgainstItsPublishedList(unittest.TestCase):
    """The first direction of the sentence, and the one an "is it up?" probe misses: the process
    answers, so every liveness signal is up, and the only thing wrong is that the model the
    request will name is not among the models this endpoint says it serves."""

    def _probe(self, routes, endpoint=NAMED, seen=None):
        with tempfile.TemporaryDirectory() as base:
            asker = _asker(base, endpoint, f"{NAMED}={MODEL}")
            with unittest.mock.patch.object(ask_module.httpdirect, "urlopen",
                                            _serving(routes, seen)):
                return asker.probe_endpoint(endpoint)

    def test_fails_when_the_published_list_lacks_the_named_model(self):
        # Answering is not certifying. Every path is 200; the list simply does not carry the
        # name the completion will send, so the completion is a 404 waiting to happen and the
        # ladder must be told now, not after it has committed to this tier.
        self.assertFalse(self._probe(_endpoint_serving(NAMED, ELSEWHERE)),
                         "a live endpoint publishing a list WITHOUT the named model was "
                         "certified; the probe read the answer, not the list")

    def test_passes_when_the_published_list_carries_the_named_model(self):
        # The other side of "only when": the sentence is a necessary condition, not a ban. A
        # check that failed here would pin nothing, since refusing everything satisfies it.
        self.assertTrue(self._probe(_endpoint_serving(NAMED, ELSEWHERE, MODEL)),
                        "an endpoint publishing the named model was refused")

    def test_fails_when_the_answer_carries_no_list_at_all(self):
        # A 200 from something that is not a model index — a proxy, a wrong port, a server that
        # answers every path. There is no list, so there is nothing the name can be in.
        self.assertFalse(self._probe(_endpoint_serving(NAMED, body=b'{"status":"ok"}')),
                         "a 200 carrying no published list was read as a certification")

    def test_fails_when_the_published_list_is_empty(self):
        # ollama answers its index while holding no model at all. That is a real state, and it
        # is exactly the state in which the name is not in the list.
        self.assertFalse(self._probe(_endpoint_serving(NAMED)),
                         "an endpoint publishing an EMPTY list was certified")

    def test_fails_when_the_answer_is_not_a_list_it_can_read(self):
        self.assertFalse(self._probe(_endpoint_serving(NAMED, body=b"OK")),
                         "an unparseable answer was read as a certification")

    def test_fails_when_the_endpoint_is_not_answering_at_all(self):
        # The guard the old behaviour did hold; certifying the name must not cost it.
        self.assertFalse(self._probe({}), "an unreachable endpoint was certified")

    def test_fails_on_a_non_2xx_answer_that_names_the_model(self):
        # A list served under an error status is not a publication.
        self.assertFalse(self._probe(_endpoint_serving(NAMED, MODEL, status=503)),
                         "a 503 was read as a certification because its body named the model")

    def test_certification_reads_the_list_this_endpoint_publishes(self):
        # "the list the ENDPOINT ITSELF publishes". Two named endpoints, each configured for a
        # different model, and each publishing only its own: neither may be certified by the
        # other's list, and probing one must not go and ask the other.
        routes = {}
        routes.update(_endpoint_serving(NAMED, MODEL))
        routes.update(_endpoint_serving(OTHER, ELSEWHERE))
        with tempfile.TemporaryDirectory() as base:
            models = f"{NAMED}={MODEL},{OTHER}={MODEL}"
            asker = _asker(base, f"{NAMED},{OTHER}", models)
            seen = []
            with unittest.mock.patch.object(ask_module.httpdirect, "urlopen",
                                            _serving(routes, seen)):
                self.assertTrue(asker.probe_endpoint(NAMED),
                                "the endpoint publishing the named model was refused")
                seen.clear()
                self.assertFalse(asker.probe_endpoint(OTHER),
                                 "an endpoint was certified for a model only its NEIGHBOUR "
                                 "publishes")
            self.assertEqual([u for u in seen if u.startswith(NAMED)], [],
                             "probing one endpoint went and read another endpoint's list")


class TestTheLadderFallsPastAnUncertifiedEndpoint(unittest.TestCase):
    """Reachability: the certification has to be the thing that picks a tier, or it is a fact
    nobody consults. Same shape as the ladder's existing reason for probing at all."""

    def test_pick_endpoint_skips_a_named_endpoint_that_does_not_publish_its_model(self):
        routes = {}
        routes.update(_endpoint_serving(NAMED, ELSEWHERE))
        routes.update(_endpoint_serving(UNNAMED))
        with tempfile.TemporaryDirectory() as base:
            asker = _asker(base, f"{NAMED},{UNNAMED}", f"{NAMED}={MODEL}")
            with unittest.mock.patch.object(ask_module.httpdirect, "urlopen",
                                            _serving(routes)):
                self.assertEqual(asker.pick_endpoint(), UNNAMED,
                                 "the ladder settled on a live endpoint that does not publish "
                                 "the model its request will name")


class TestAnEndpointThatNamesNoModelIsOutOfScope(unittest.TestCase):
    """The sentence's subject is "an endpoint whose request WILL NAME a model". An endpoint that
    sends no model key selects nothing, so there is no name to certify and its probe must stay
    what it was — this is the scope line, and a check that erased it would be over-reach."""

    def test_unnamed_endpoint_is_certified_by_answering_alone(self):
        with tempfile.TemporaryDirectory() as base:
            asker = _asker(base, UNNAMED, f"{NAMED}={MODEL}")
            seen = []
            with unittest.mock.patch.object(
                    ask_module.httpdirect, "urlopen",
                    _serving({f"{UNNAMED}/health": (200, b'{"status":"ok"}')}, seen)):
                self.assertTrue(asker.probe_endpoint(UNNAMED),
                                "an endpoint that names no model was made to publish one")
            self.assertEqual(seen, [f"{UNNAMED}/health"])


class TestTheEmbedderEndpointIsCertifiedAgainstItsPublishedList(unittest.TestCase):
    """The engine's OTHER request that names a model. The sentence is about the class, not about
    one call site, so this surface is held to it too."""

    URL = "http://127.0.0.1:11434"
    EMBED = "qwen3-embedding:0.6b"

    def _index(self, base):
        cfg = Config(env={}, config_file="/nonexistent",
                     overrides={"QQ_KB_ROOT": os.path.join(base, "kb"),
                                "QQ_CACHE": os.path.join(base, "cache", "embeddings.json"),
                                "QQ_OLLAMA_URL": self.URL,
                                "QQ_EMBED_MODEL": self.EMBED})
        return SearchIndex(cfg)

    def _probe(self, routes):
        with tempfile.TemporaryDirectory() as base:
            idx = self._index(base)
            with unittest.mock.patch.object(search_module.httpdirect, "urlopen",
                                            _serving(routes)):
                return idx.probe_embedder()

    def test_fails_when_the_published_list_lacks_the_embed_model(self):
        ok, detail = self._probe(_endpoint_serving(self.URL, "nomic-embed-text:latest"))
        self.assertFalse(ok, f"a live embedder not publishing {self.EMBED} was certified: {detail}")

    def test_passes_when_the_published_list_carries_the_embed_model(self):
        ok, detail = self._probe(_endpoint_serving(self.URL, "nomic-embed-text:latest", self.EMBED))
        self.assertTrue(ok, f"an embedder publishing {self.EMBED} was refused: {detail}")


if __name__ == "__main__":
    unittest.main()
