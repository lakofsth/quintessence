# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit test for qq-search-mcp's P8 module-level wiring (the ACTUAL script, not a mirror):
degenerate (single-store) startup builds a bare SearchIndex exactly as pre-P8; a >=2-store
search path at startup builds a ComposedSearchIndex. qq-search-mcp isn't otherwise covered by
this suite (it needs the real `mcp`/`uv` stack to run for real, neither of which this harness
depends on) — this test stubs `mcp.server.fastmcp.FastMCP` as a no-op and execs the script's
actual top-level module code so the P8 wiring itself (not a hand-written mirror of it) is what
gets pinned under test."""
import os
import sys
import tempfile
import types
import unittest

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ENGINE)

from quintessence.search import SearchIndex
from quintessence.searchcompose import ComposedSearchIndex


class _FakeFastMCP:
    def __init__(self, _name):
        pass

    def tool(self):
        def deco(fn):
            return fn
        return deco

    def run(self):  # pragma: no cover - never invoked (script exec stops before __main__ block)
        pass


def _install_fake_mcp_module():
    fake_mcp = types.ModuleType("mcp")
    fake_server = types.ModuleType("mcp.server")
    fake_fastmcp = types.ModuleType("mcp.server.fastmcp")
    fake_fastmcp.FastMCP = _FakeFastMCP
    fake_server.fastmcp = fake_fastmcp
    fake_mcp.server = fake_server
    sys.modules["mcp"] = fake_mcp
    sys.modules["mcp.server"] = fake_server
    sys.modules["mcp.server.fastmcp"] = fake_fastmcp


def _exec_qq_search_mcp(cwd: str, env_overrides: dict) -> dict:
    """Exec the REAL qq-search-mcp script body (module-level wiring only — `__name__` is
    deliberately NOT "__main__" so the `mcp.run()` tail never fires) under a chdir + env
    override, and return its module namespace."""
    _install_fake_mcp_module()
    script_path = os.path.join(ENGINE, "qq-search-mcp")
    with open(script_path) as f:
        source = f.read()
    code = compile(source, script_path, "exec")
    ns = {"__name__": "qq_search_mcp_under_test", "__file__": script_path}
    old_cwd = os.getcwd()
    old_env = dict(os.environ)
    try:
        os.chdir(cwd)
        os.environ.clear()
        os.environ.update(env_overrides)
        exec(code, ns)
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)
    return ns


class TestQqSearchMcpP8Wiring(unittest.TestCase):
    def test_degenerate_home_run_builds_a_bare_searchindex(self):
        with tempfile.TemporaryDirectory() as home:
            env = {
                "HOME": home,
                "QQ_CONFIG": os.path.join(home, "config"),
                "QQ_STATE_DIR": os.path.join(home, "state"),
                "QQ_MEMDIR": os.path.join(home, "mem"),
                "QQ_KB_ROOT": os.path.join(home, "kb"),
                "QQ_CACHE": os.path.join(home, "cache", "embeddings.json"),
                "PATH": os.environ.get("PATH", ""),
            }
            os.makedirs(env["QQ_MEMDIR"], exist_ok=True)
            open(env["QQ_CONFIG"], "w").close()
            ns = _exec_qq_search_mcp(home, env)
            self.assertIsInstance(ns["_index"], SearchIndex)
            self.assertNotIsInstance(ns["_index"], ComposedSearchIndex)

    def test_multi_store_run_builds_a_composed_index(self):
        with tempfile.TemporaryDirectory() as home:
            proj = os.path.join(home, "work", "repo")
            pstore = os.path.join(proj, ".quintessence")
            os.makedirs(pstore, exist_ok=True)
            with open(os.path.join(pstore, "ptopic.md"), "w") as f:
                f.write("# ptopic\ncontent\n")
            env = {
                "HOME": home,
                "QQ_CONFIG": os.path.join(home, "config"),
                "QQ_STATE_DIR": os.path.join(home, "state"),
                "QQ_MEMDIR": os.path.join(home, "mem"),
                "QQ_KB_ROOT": os.path.join(home, "kb"),
                "QQ_CACHE": os.path.join(home, "cache", "embeddings.json"),
                "PATH": os.environ.get("PATH", ""),
            }
            os.makedirs(env["QQ_MEMDIR"], exist_ok=True)
            os.makedirs(os.path.join(home, "quintessence"), exist_ok=True)   # the user store dir
            open(env["QQ_CONFIG"], "w").close()
            ns = _exec_qq_search_mcp(proj, env)   # cwd INSIDE the project at server startup
            self.assertIsInstance(ns["_index"], ComposedSearchIndex)
            self.assertEqual(len(ns["_index"].indexes), 2)
            self.assertEqual([loc.kind for loc, _ix in ns["_index"].indexes], ["project", "user"])


class TestSearchContinuityFlooredMessage(unittest.TestCase):
    """When search returns no results because every candidate fell below the relevance floor,
    search_continuity must emit a distinct message (not just 'No results') so an agent knows the
    store had content but nothing strong."""

    class _FakeFlooredIndex:
        def __init__(self):
            self.last_search_floored = 3

        def search(self, query, k=6, source=None, with_stats=False):
            if with_stats:
                return [], {"floored": 3}
            return []

    class _FakeEmptyIndex:
        def __init__(self):
            self.last_search_floored = 0

        def search(self, query, k=6, source=None, with_stats=False):
            if with_stats:
                return [], {"floored": 0}
            return []

    def _wired_ns(self, home: str) -> dict:
        env = {
            "HOME": home,
            "QQ_CONFIG": os.path.join(home, "config"),
            "QQ_STATE_DIR": os.path.join(home, "state"),
            "QQ_MEMDIR": os.path.join(home, "mem"),
            "QQ_KB_ROOT": os.path.join(home, "kb"),
            "QQ_CACHE": os.path.join(home, "cache", "embeddings.json"),
            "PATH": os.environ.get("PATH", ""),
        }
        os.makedirs(env["QQ_MEMDIR"], exist_ok=True)
        open(env["QQ_CONFIG"], "w").close()
        return _exec_qq_search_mcp(home, env)

    def test_floored_out_gets_distinct_message(self):
        with tempfile.TemporaryDirectory() as home:
            ns = self._wired_ns(home)
            ns["_index"] = self._FakeFlooredIndex()
            out = ns["search_continuity"]("anything")
            self.assertIn("relevance floor", out)
            self.assertIn("3 candidate(s)", out)

    def test_true_empty_gets_plain_no_results(self):
        with tempfile.TemporaryDirectory() as home:
            ns = self._wired_ns(home)
            ns["_index"] = self._FakeEmptyIndex()
            out = ns["search_continuity"]("anything")
            self.assertIn("No results", out)
            self.assertNotIn("relevance floor", out)


class TestSearchContinuityRefAnnotations(unittest.TestCase):
    """When a search hit's chunk text contains update-line stamps that join to changed
    refs, the rendered output must carry an indented warning line under the hit's snippet.
    When no refs exist, the output must be byte-identical to the pre-annotation rendering."""

    STAMP = "2026-07-10T12:00:00Z"

    class _FakeIndexWithText:
        def __init__(self, text):
            self.last_search_floored = 0
            self._text = text

        def search(self, query, k=6, source=None, with_stats=False):
            hits = [{"score": 0.9, "source": "qq", "path": "kb/quintessence/mytopic.md",
                     "title": "mytopic", "snippet": "short summary", "text": self._text}]
            if with_stats:
                return hits, {"floored": 0}
            return hits

    def _wired_ns(self, home, refs_records=None):
        import json
        env = {
            "HOME": home,
            "QQ_CONFIG": os.path.join(home, "config"),
            "QQ_STATE_DIR": os.path.join(home, "state"),
            "QQ_MEMDIR": os.path.join(home, "mem"),
            "QQ_KB_ROOT": os.path.join(home, "kb"),
            "QQ_CACHE": os.path.join(home, "cache", "embeddings.json"),
            "QQ_BIND": "1",
            "PATH": os.environ.get("PATH", ""),
        }
        os.makedirs(env["QQ_MEMDIR"], exist_ok=True)
        open(env["QQ_CONFIG"], "w").close()
        if refs_records:
            refs_dir = os.path.join(env["QQ_STATE_DIR"], "refs")
            os.makedirs(refs_dir, exist_ok=True)
            with open(os.path.join(refs_dir, "refs.jsonl"), "w") as f:
                for rec in refs_records:
                    f.write(json.dumps(rec) + "\n")
        return _exec_qq_search_mcp(home, env)

    def test_hit_with_changed_ref_renders_warning(self):
        text = f"> updated: {self.STAMP} some claim\ndetail line\n"
        ref_rec = {"head": "mytopic", "line_ts": self.STAMP, "kind": "file",
                   "id": "/tmp/artifact.txt", "fp": None, "asof": self.STAMP,
                   "status": "suspect"}
        with tempfile.TemporaryDirectory() as home:
            ns = self._wired_ns(home, refs_records=[ref_rec])
            ns["_index"] = self._FakeIndexWithText(text)
            out = ns["search_continuity"]("anything")
            self.assertIn("referent changed since written", out)
            self.assertIn("/tmp/artifact.txt", out)

    def test_hit_without_refs_renders_identically(self):
        text = "plain content with no update stamps\n"
        with tempfile.TemporaryDirectory() as home:
            ns = self._wired_ns(home)
            ns["_index"] = self._FakeIndexWithText(text)
            out = ns["search_continuity"]("anything")
            self.assertNotIn("referent changed since written", out)
            self.assertIn("short summary", out)

    def test_hit_with_unchanged_ref_no_warning(self):
        # A refs record EXISTS for the hit's stamp and its referent is a real file
        # whose current fingerprint matches the recorded one, so the no-warning
        # outcome exercises the annotation comparison rather than the empty
        # refs-file shortcut (which the without-refs test above already covers).
        from quintessence.refs import _fp_file
        text = f"> updated: {self.STAMP} some claim\n"
        with tempfile.TemporaryDirectory() as home:
            artifact = os.path.join(home, "artifact.txt")
            with open(artifact, "w") as f:
                f.write("stable content\n")
            fp, _status = _fp_file(artifact)
            ref_rec = {"head": "mytopic", "line_ts": self.STAMP, "kind": "file",
                       "id": artifact, "fp": fp, "asof": self.STAMP,
                       "status": "ok"}
            ns = self._wired_ns(home, refs_records=[ref_rec])
            ns["_index"] = self._FakeIndexWithText(text)
            out = ns["search_continuity"]("anything")
            self.assertNotIn("referent changed since written", out)


class TestSearchContinuityDegradedWarning(unittest.TestCase):
    """recall-01: the `search_continuity` MCP tool used to render hits with no indication the
    embedder was down and SearchIndex had fallen back to keyword grep — qq-search's CLI checks
    this exact `degraded` hit tag and warns loudly on stderr; the MCP tool has no stderr channel
    a caller reads, so the same check now folds the same warning into the returned text."""

    class _FakeDegradedIndex:
        def search(self, query, k=6, source=None, with_stats=False):
            hits = [{"score": 0.5, "source": "qq", "path": "p.md", "title": "t",
                     "snippet": "s", "degraded": True}]
            if with_stats:
                return hits, {"floored": 0}
            return hits

    class _FakeCleanIndex:
        def search(self, query, k=6, source=None, with_stats=False):
            hits = [{"score": 0.9, "source": "qq", "path": "p.md", "title": "t", "snippet": "s"}]
            if with_stats:
                return hits, {"floored": 0}
            return hits

    def _wired_ns(self, home: str) -> dict:
        env = {
            "HOME": home,
            "QQ_CONFIG": os.path.join(home, "config"),
            "QQ_STATE_DIR": os.path.join(home, "state"),
            "QQ_MEMDIR": os.path.join(home, "mem"),
            "QQ_KB_ROOT": os.path.join(home, "kb"),
            "QQ_CACHE": os.path.join(home, "cache", "embeddings.json"),
            "PATH": os.environ.get("PATH", ""),
        }
        os.makedirs(env["QQ_MEMDIR"], exist_ok=True)
        open(env["QQ_CONFIG"], "w").close()
        return _exec_qq_search_mcp(home, env)

    def test_degraded_hit_gets_the_loud_warning(self):
        with tempfile.TemporaryDirectory() as home:
            ns = self._wired_ns(home)
            ns["_index"] = self._FakeDegradedIndex()
            out = ns["search_continuity"]("anything")
            self.assertTrue(out.startswith(ns["RETRIEVAL_DEGRADED_WARNING"]))

    def test_clean_hit_has_no_warning(self):
        with tempfile.TemporaryDirectory() as home:
            ns = self._wired_ns(home)
            ns["_index"] = self._FakeCleanIndex()
            out = ns["search_continuity"]("anything")
            self.assertNotIn(ns["RETRIEVAL_DEGRADED_WARNING"], out)


if __name__ == "__main__":
    unittest.main()
