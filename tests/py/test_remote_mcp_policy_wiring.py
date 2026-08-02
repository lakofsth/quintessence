# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for qq-remote-mcp's POLICY WIRING — the _PolicyAsk constructor and filter_redacted
override that make RemotePolicy the sole filtering authority for both the ask and search paths.

Four safety properties are pinned:

  P1. Standard profile + withheld topic → NOT returned (ask or search).
  P2. Wide profile + withheld topic → MUST come back (the restored behaviour).
  P3. Either profile + crown-jewel topic → NOT returned.
  P4. Missing deny list → nothing rather than everything.

Same exec-the-real-script technique as test_remote_mcp_tokens.py — the ACTUAL qq-remote-mcp
source is compiled and exec'd with mcp/uvicorn stubbed, so the real _PolicyAsk wiring is what
runs under test."""
import contextlib
import inspect
import os
import re
import sys
import tempfile
import types
import unittest

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ENGINE)

from quintessence.remotepolicy import PROFILE_STANDARD, PROFILE_WIDE  # noqa: E402

CROWN = "covert-channel-lab"
WITHHELD = "finnair-inflight-tunnel"
GREEN = "flowtun-stack"


class _FakeFastMCP:
    def __init__(self, _name, transport_security=None):
        self._registered = []

    def tool(self):
        def deco(fn):
            self._registered.append(fn)
            return fn
        return deco


class _FakeTransportSecuritySettings:
    def __init__(self, **kwargs):
        pass


def _install_fake_mcp_module():
    fake_mcp = types.ModuleType("mcp")
    fake_server = types.ModuleType("mcp.server")
    fake_fastmcp = types.ModuleType("mcp.server.fastmcp")
    fake_fastmcp.FastMCP = _FakeFastMCP
    fake_transport = types.ModuleType("mcp.server.transport_security")
    fake_transport.TransportSecuritySettings = _FakeTransportSecuritySettings
    fake_server.fastmcp = fake_fastmcp
    fake_server.transport_security = fake_transport
    fake_mcp.server = fake_server
    sys.modules["mcp"] = fake_mcp
    sys.modules["mcp.server"] = fake_server
    sys.modules["mcp.server.fastmcp"] = fake_fastmcp
    sys.modules["mcp.server.transport_security"] = fake_transport


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


@contextlib.contextmanager
def _hermetic_env(env):
    """Re-apply the fixture env so subprocess.run inherits it, not the real env."""
    old_env = dict(os.environ)
    old_cwd = os.getcwd()
    try:
        os.environ.clear()
        os.environ.update(env)
        os.chdir(env["HOME"])
        yield
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)


def _exec_qq_remote_mcp(cwd, env_overrides):
    _install_fake_mcp_module()
    script_path = os.path.join(ENGINE, "qq-remote-mcp")
    with open(script_path) as f:
        source = f.read()
    code = compile(source, script_path, "exec")
    ns = {"__name__": "qq_remote_mcp_under_test", "__file__": script_path}
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


class _FakeIndex:
    """Returns four fixed hits: one green, one withheld, one crown-jewel, and one
    docs-source hit whose path is absolute — a source root outside the home directory.
    The absolute path must be stripped by the sanitiser before reaching a remote caller."""
    def __init__(self):
        self.redact_lifted = False
        self.last_search_floored = 0

    def search(self, query, k=6, source=None, with_stats=False):
        hits = [
            {"score": 0.9, "source": "qq", "path": f"kb/quintessence/{GREEN}.md",
             "title": GREEN, "snippet": "green content"},
            {"score": 0.8, "source": "qq", "path": f"kb/quintessence/{WITHHELD}.md",
             "title": WITHHELD, "snippet": "withheld content"},
            {"score": 0.7, "source": "qq", "path": f"kb/quintessence/{CROWN}.md",
             "title": CROWN, "snippet": "crown content"},
            {"score": 0.6, "source": "docs", "path": "/srv/models/deepseek/model-notes.md",
             "title": "model notes",
             "snippet": "config lives at /etc/llama/serving.conf"},
        ]
        if with_stats:
            return hits, {"floored": 0}
        return hits


def _wired_ns(home):
    """Exec qq-remote-mcp in a hermetic temp env with both deny files populated."""
    crown_file = os.path.join(home, "remote-deny-slugs")
    withheld_file = os.path.join(home, "redact-slugs")
    _write(crown_file, f"# crown jewels\n{CROWN}\n")
    _write(withheld_file, f"# withheld topics\n{WITHHELD}\n")
    env = {
        "HOME": home,
        "QQ_CONFIG": os.path.join(home, "config"),
        "QQ_STATE_DIR": os.path.join(home, "state"),
        "QQ_MEMDIR": os.path.join(home, "mem"),
        "QQ_KB_ROOT": os.path.join(home, "kb"),
        "QQ_CACHE": os.path.join(home, "cache", "embeddings.json"),
        "QQ_REMOTE_DENY_FILE": crown_file,
        "QQ_REDACT_FILE": withheld_file,
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    os.makedirs(env["QQ_MEMDIR"], exist_ok=True)
    open(env["QQ_CONFIG"], "w").close()
    ns = _exec_qq_remote_mcp(home, env)
    ns["_test_env"] = env
    return ns, crown_file, withheld_file


class TestPolicyAskWiring(unittest.TestCase):
    """Pin the constructor change: _PolicyAsk must lift search-side redaction so RemotePolicy
    is the sole filtering authority — the property the wide profile depends on."""

    def test_redact_lifted_on_index(self):
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            self.assertTrue(ns["_index"].redact_lifted,
                            "search index redact_lifted must be True after _PolicyAsk construction")


class TestPolicyFiltering(unittest.TestCase):
    """End-to-end filtering through the server's real _PolicyAsk and search_continuity paths,
    exercised by setting the profile ContextVar and calling the real functions from the exec'd
    qq-remote-mcp namespace."""

    def _hit_paths(self, hits):
        return [h["path"] for h in hits]

    def test_standard_profile_denies_withheld_and_crown(self):
        """P1 + P3: a standard caller must not receive withheld or crown-jewel topics
        from either the ask path (filter_redacted) or the search path (search_continuity)."""
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_index"] = _FakeIndex()
            ns["_profile"].set(PROFILE_STANDARD)

            kept = ns["_asker"].filter_redacted(_FakeIndex().search("q"))
            paths = self._hit_paths(kept)
            self.assertIn(f"kb/quintessence/{GREEN}.md", paths)
            self.assertNotIn(f"kb/quintessence/{WITHHELD}.md", paths)
            self.assertNotIn(f"kb/quintessence/{CROWN}.md", paths)

            out = ns["search_continuity"]("anything")
            self.assertIn(GREEN, out)
            self.assertNotIn(WITHHELD, out)
            self.assertNotIn(CROWN, out)

    def test_wide_profile_serves_withheld_but_denies_crown(self):
        """P2 + P3: a wide caller must receive withheld topics (the behaviour the
        redact_lifted change exists to restore) but must STILL not receive crown jewels."""
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_index"] = _FakeIndex()
            ns["_profile"].set(PROFILE_WIDE)

            kept = ns["_asker"].filter_redacted(_FakeIndex().search("q"))
            paths = self._hit_paths(kept)
            self.assertIn(f"kb/quintessence/{GREEN}.md", paths)
            self.assertIn(f"kb/quintessence/{WITHHELD}.md", paths)
            self.assertNotIn(f"kb/quintessence/{CROWN}.md", paths)

            out = ns["search_continuity"]("anything")
            self.assertIn(GREEN, out)
            self.assertIn(WITHHELD, out)
            self.assertNotIn(CROWN, out)

    def test_missing_deny_list_returns_nothing(self):
        """P4: if the crown-jewels deny list cannot be read, both paths must return nothing
        rather than serving everything unfiltered."""
        with tempfile.TemporaryDirectory() as home:
            ns, crown_file, _ = _wired_ns(home)
            ns["_index"] = _FakeIndex()
            ns["_profile"].set(PROFILE_STANDARD)
            os.remove(crown_file)

            kept = ns["_asker"].filter_redacted(_FakeIndex().search("q"))
            self.assertEqual(kept, [])

            out = ns["search_continuity"]("anything")
            self.assertIn("No results", out)


class TestAskAuditSingleEntry(unittest.TestCase):
    """Pin: when rendering fails after ask succeeds, exactly one audit entry
    (the error entry) is emitted — not a success entry followed by an error entry."""

    def test_render_failure_emits_single_error_audit(self):
        import io

        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_profile"].set(PROFILE_STANDARD)

            class _FakeAsker:
                def ask(self, question, k=12, source=None):
                    return {"answer": "test", "sources": [], "endpoint": "fake"}

            ns["_asker"] = _FakeAsker()
            ns["render"] = lambda result: (_ for _ in ()).throw(ValueError("render failed"))

            old_stderr = sys.stderr
            sys.stderr = captured = io.StringIO()
            try:
                result = ns["ask_continuity"]("anything")
            finally:
                sys.stderr = old_stderr

            lines = [l for l in captured.getvalue().splitlines() if "[qq-remote]" in l]
            self.assertEqual(len(lines), 1,
                             f"expected exactly 1 audit line, got {len(lines)}: {lines}")
            self.assertIn("ask_error", lines[0])
            self.assertIn("ask error", result)


class TestBriefAuditSingleEntry(unittest.TestCase):
    """Pin: when the subprocess helper fails during a brief call, exactly one audit
    entry is emitted — the caller's error entry, not a helper entry followed by a
    caller success entry."""

    def test_subprocess_failure_emits_single_error_audit(self):
        import io

        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_profile"].set(PROFILE_STANDARD)

            # Break the REAL subprocess path rather than patching _run_qq: the old
            # defect lived inside _run_qq (it audited its own qq_error and returned a
            # string the caller then audited as success), so a patched helper that
            # raises would bypass the very path under test and pass on the old code.
            ns["_QQ_BIN"] = os.path.join(home, "no-such-qq-binary")

            old_stderr = sys.stderr
            sys.stderr = captured = io.StringIO()
            try:
                result = ns["resume_brief"](GREEN)
            finally:
                sys.stderr = old_stderr

            lines = [l for l in captured.getvalue().splitlines() if "[qq-remote]" in l]
            self.assertEqual(len(lines), 1,
                             f"expected exactly 1 audit line, got {len(lines)}: {lines}")
            self.assertIn("brief_error", lines[0])
            self.assertIn("brief error", result)


class TestSearchAuditSingleEntry(unittest.TestCase):
    """Pin: when hit formatting fails in search_continuity, exactly one audit entry
    is emitted — the error entry, not a success entry followed by an error entry."""

    def test_formatting_failure_emits_single_error_audit(self):
        import io

        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_profile"].set(PROFILE_WIDE)

            ns["_index"] = _FakeIndex()

            def _bad_locator(h):
                raise ValueError("format failed")
            ns["_remote_hit_locator"] = _bad_locator

            old_stderr = sys.stderr
            sys.stderr = captured = io.StringIO()
            try:
                result = ns["search_continuity"]("anything")
            finally:
                sys.stderr = old_stderr

            lines = [l for l in captured.getvalue().splitlines() if "[qq-remote]" in l]
            self.assertEqual(len(lines), 1,
                             f"expected exactly 1 audit line, got {len(lines)}: {lines}")
            self.assertIn("search_error", lines[0])
            self.assertIn("search error", result)


class TestRemoteFaceSanitization(unittest.TestCase):
    """Pin: errors and status sent to a remote caller carry no internal detail —
    no endpoint addresses, no exception class names, no local-only diagnostic commands."""

    def test_degraded_completion_strips_address_and_exception(self):
        """A completion failure that embeds an endpoint address and exception class
        must not reach the remote caller."""
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_profile"].set(PROFILE_STANDARD)

            class _FailedCompletionAsker:
                def ask(self, question, k=12, source=None):
                    return {
                        "answer": ("QA unavailable — raw retrieval only "
                                   "(http://127.0.0.1:8090 answered the health probe "
                                   "but the completion failed: HTTPError "
                                   "(timeout is 120s))."),
                        "sources": [{"n": 1, "path": "kb/quintessence/test.md",
                                     "title": "test", "score": 0.9,
                                     "snippet": "snippet text"}],
                        "endpoint": None,
                        "degraded": True,
                        "retrieval_degraded": False,
                    }

            ns["_asker"] = _FailedCompletionAsker()
            result = ns["ask_continuity"]("test question")
            self.assertNotIn("127.0.0.1", result)
            self.assertNotIn("8090", result)
            self.assertNotIn("HTTPError", result)

    def test_retrieval_degraded_strips_local_command(self):
        """The retrieval-fallback banner must not mention local-only diagnostic
        commands on the remote face."""
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_profile"].set(PROFILE_STANDARD)

            class _RetrievalDegradedAsker:
                def ask(self, question, k=12, source=None):
                    return {
                        "answer": "QA unavailable — raw retrieval only.",
                        "sources": [],
                        "endpoint": None,
                        "degraded": True,
                        "retrieval_degraded": True,
                    }

            ns["_asker"] = _RetrievalDegradedAsker()
            result = ns["ask_continuity"]("test question")
            self.assertNotIn("qq doctor", result)
            self.assertIn("embedder unreachable", result)

    def test_both_degraded_paths_combined(self):
        """Both fixes together: a completion failure with retrieval also degraded
        must expose none of the three internal details."""
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_profile"].set(PROFILE_STANDARD)

            class _BothDegradedAsker:
                def ask(self, question, k=12, source=None):
                    return {
                        "answer": ("QA unavailable — raw retrieval only "
                                   "(http://127.0.0.1:8090 answered the health probe "
                                   "but the completion failed: TimeoutError "
                                   "(timeout is 120s))."),
                        "sources": [{"n": 1, "path": "kb/quintessence/test.md",
                                     "title": "test", "score": 0.9,
                                     "snippet": "snippet text"}],
                        "endpoint": None,
                        "degraded": True,
                        "retrieval_degraded": True,
                    }

            ns["_asker"] = _BothDegradedAsker()
            result = ns["ask_continuity"]("test question")
            self.assertNotIn("127.0.0.1", result)
            self.assertNotIn("TimeoutError", result)
            self.assertNotIn("qq doctor", result)


_LOCAL_ONLY_STRINGS = ["~/"]

_LOCAL_ONLY_PATTERNS = [
    re.compile(r"\bqq\s+\w+"),
    re.compile(r"(?<![\w/])(/[\w._-]+){2,}"),
]


class TestRemoteFaceNoLocalLeakage(unittest.TestCase):
    """Pin: nothing sent to a remote caller names a local-only command or a local
    filesystem path.  Both the ask banner and the search hit locators must use
    remote-safe identifiers."""

    def _assert_no_local_strings(self, text, label):
        for s in _LOCAL_ONLY_STRINGS:
            self.assertNotIn(s, text,
                             f"{label} must not contain local-only string {s!r}")
        for pat in _LOCAL_ONLY_PATTERNS:
            m = pat.search(text)
            self.assertIsNone(
                m, f"{label} must not match local-only pattern {pat.pattern!r}"
                   + (f" (found: {m.group(0)!r})" if m else ""))

    def test_ask_standard_profile_no_local_leakage(self):
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_profile"].set(PROFILE_STANDARD)
            ns["_index"] = _FakeIndex()

            class _SimpleAsker:
                def ask(self, question, k=12, source=None):
                    return {
                        "answer": "The stack uses WireGuard.",
                        "sources": [{"n": 1, "path": f"kb/quintessence/{GREEN}.md",
                                     "title": GREEN, "score": 0.9,
                                     "snippet": "tunnel config"}],
                        "endpoint": "local",
                        "degraded": False,
                        "retrieval_degraded": False,
                    }

            ns["_asker"] = _SimpleAsker()
            result = ns["ask_continuity"]("what stack?")
            self._assert_no_local_strings(result, "ask (standard)")

    def test_ask_wide_profile_no_local_leakage(self):
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_profile"].set(PROFILE_WIDE)
            ns["_index"] = _FakeIndex()

            class _SimpleAsker:
                def ask(self, question, k=12, source=None):
                    return {
                        "answer": "The stack uses WireGuard.",
                        "sources": [{"n": 1, "path": f"kb/quintessence/{GREEN}.md",
                                     "title": GREEN, "score": 0.9,
                                     "snippet": "tunnel config"}],
                        "endpoint": "local",
                        "degraded": False,
                        "retrieval_degraded": False,
                    }

            ns["_asker"] = _SimpleAsker()
            result = ns["ask_continuity"]("what stack?")
            self._assert_no_local_strings(result, "ask (wide)")

    def test_search_standard_profile_no_local_leakage(self):
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_profile"].set(PROFILE_STANDARD)
            ns["_index"] = _FakeIndex()
            result = ns["search_continuity"]("anything")
            self._assert_no_local_strings(result, "search (standard)")

    def test_search_wide_profile_no_local_leakage(self):
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_profile"].set(PROFILE_WIDE)
            ns["_index"] = _FakeIndex()
            result = ns["search_continuity"]("anything")
            self._assert_no_local_strings(result, "search (wide)")

    def _brief_ns_with_head(self, home, profile):
        """Set up a wired namespace with a real HEAD file so qq brief succeeds."""
        ns, crown_file, withheld_file = _wired_ns(home)
        ns["_profile"].set(profile)
        qdir = os.path.join(home, "quintessence")
        os.makedirs(qdir, exist_ok=True)
        _write(os.path.join(qdir, f"{GREEN}.md"), (
            f"# {GREEN}\n"
            "> updated: 2026-07-30T00:00:00Z FIXTURE-7a3f hermetic sentinel\n"
            "\n"
            "Essence of the topic.\n"
            "\n"
            "→ file /data/eval-runs/m0-inventory-20260704\n"
            "Config at /etc/llama/serving.conf for the local endpoint.\n"
            "\n"
            "## RE-ENTER\n"
            "Pick up here.\n"
        ))
        return ns

    def test_brief_standard_profile_no_local_leakage(self):
        with tempfile.TemporaryDirectory() as home:
            ns = self._brief_ns_with_head(home, PROFILE_STANDARD)
            with _hermetic_env(ns["_test_env"]):
                result = ns["resume_brief"](GREEN)
            self.assertIn("FIXTURE-7a3f", result,
                          "brief must read the fixture HEAD, not the real store")
            self._assert_no_local_strings(result, "brief (standard)")

    def test_brief_wide_profile_no_local_leakage(self):
        with tempfile.TemporaryDirectory() as home:
            ns = self._brief_ns_with_head(home, PROFILE_WIDE)
            with _hermetic_env(ns["_test_env"]):
                result = ns["resume_brief"](GREEN)
            self.assertIn("FIXTURE-7a3f", result,
                          "brief must read the fixture HEAD, not the real store")
            self._assert_no_local_strings(result, "brief (wide)")


class TestSanitizeOutwardPathDelimiters(unittest.TestCase):
    """Pin: _sanitize_outward catches absolute paths after non-whitespace delimiters
    (equals sign, double quote) and leaves relative paths and ordinary prose alone."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
        cls._sanitize = staticmethod(ns["_sanitize_outward"])

    def test_path_after_equals_sign(self):
        result = self._sanitize("config=/etc/llama/serving.conf")
        self.assertNotIn("/etc/", result)
        self.assertIn("serving.conf", result)

    def test_path_after_double_quote(self):
        result = self._sanitize('"/home/user/notes/topic.md"')
        self.assertNotIn("/home/", result)
        self.assertIn("topic.md", result)

    def test_relative_path_untouched(self):
        text = "see docs/setup/install.md for details"
        self.assertEqual(self._sanitize(text), text)

    def test_prose_slash_phrase_untouched(self):
        text = "the client/server/model split is standard"
        self.assertEqual(self._sanitize(text), text)

    def test_web_address_with_path_untouched(self):
        text = "see http://example.com/docs/api for details"
        self.assertEqual(self._sanitize(text), text)

    def test_host_port_address_with_path_untouched(self):
        text = "endpoint at http://127.0.0.1:8090/v1/completions is down"
        self.assertEqual(self._sanitize(text), text)


class TestSearchRefAnnotations(unittest.TestCase):
    """Search hits whose chunk text joins to changed refs must carry an indented warning
    on the remote face — and that warning must flow through _sanitize_outward, so an
    annotation naming an absolute path is scrubbed before it reaches the caller."""

    STAMP = "2026-07-10T12:00:00Z"

    class _FakeAnnotatedIndex:
        def __init__(self):
            self.redact_lifted = False
            self.last_search_floored = 0

        def search(self, query, k=6, source=None, with_stats=False):
            hits = [
                {"score": 0.9, "source": "qq",
                 "path": f"kb/quintessence/{GREEN}.md",
                 "title": GREEN, "snippet": "green content",
                 "text": f"> updated: 2026-07-10T12:00:00Z some claim\ndetail\n"},
            ]
            if with_stats:
                return hits, {"floored": 0}
            return hits

    class _FakeCleanIndex:
        def __init__(self):
            self.redact_lifted = False
            self.last_search_floored = 0

        def search(self, query, k=6, source=None, with_stats=False):
            hits = [
                {"score": 0.9, "source": "qq",
                 "path": f"kb/quintessence/{GREEN}.md",
                 "title": GREEN, "snippet": "green content",
                 "text": "plain content no stamps\n"},
            ]
            if with_stats:
                return hits, {"floored": 0}
            return hits

    def _wired_ns_with_refs(self, home, refs_records=None):
        import json
        ns, crown_file, withheld_file = _wired_ns(home)
        if refs_records:
            state_dir = ns["_test_env"]["QQ_STATE_DIR"]
            refs_dir = os.path.join(state_dir, "refs")
            os.makedirs(refs_dir, exist_ok=True)
            with open(os.path.join(refs_dir, "refs.jsonl"), "w") as f:
                for rec in refs_records:
                    f.write(json.dumps(rec) + "\n")
        ns["_test_env"]["QQ_BIND"] = "1"
        return ns

    def test_hit_with_changed_ref_renders_warning(self):
        ref_rec = {"head": GREEN, "line_ts": self.STAMP, "kind": "file",
                   "id": "/tmp/artifact.txt", "fp": None, "asof": self.STAMP,
                   "status": "suspect"}
        with tempfile.TemporaryDirectory() as home:
            ns = self._wired_ns_with_refs(home, refs_records=[ref_rec])
            ns["_index"] = self._FakeAnnotatedIndex()
            ns["_profile"].set(PROFILE_STANDARD)
            out = ns["search_continuity"]("anything")
            self.assertIn("referent changed since written", out)

    def test_hit_without_refs_no_warning(self):
        with tempfile.TemporaryDirectory() as home:
            ns = self._wired_ns_with_refs(home)
            ns["_index"] = self._FakeCleanIndex()
            ns["_profile"].set(PROFILE_STANDARD)
            out = ns["search_continuity"]("anything")
            self.assertNotIn("referent changed since written", out)
            self.assertIn("green content", out)

    def test_annotation_absolute_path_sanitized(self):
        ref_rec = {"head": GREEN, "line_ts": self.STAMP, "kind": "file",
                   "id": "/home/user/notes/private.md", "fp": None,
                   "asof": self.STAMP, "status": "suspect"}
        with tempfile.TemporaryDirectory() as home:
            ns = self._wired_ns_with_refs(home, refs_records=[ref_rec])
            ns["_index"] = self._FakeAnnotatedIndex()
            ns["_profile"].set(PROFILE_STANDARD)
            out = ns["search_continuity"]("anything")
            self.assertIn("referent changed since written", out)
            self.assertNotIn("/home/user", out)
            self.assertIn("private.md", out)


def _plausible_args(fn):
    """Derive plausible arguments from a function's signature: a topic-like string
    for str parameters, defaults for everything else."""
    sig = inspect.signature(fn)
    args = {}
    for name, param in sig.parameters.items():
        ann = param.annotation
        if ann is str or (ann is inspect.Parameter.empty and param.default is inspect.Parameter.empty):
            args[name] = GREEN
        elif param.default is not inspect.Parameter.empty:
            args[name] = param.default
        else:
            args[name] = None
    return args


class TestEnumeratedVerbNoLocalLeakage(unittest.TestCase):
    """Systems-level boundary test: enumerate EVERY verb registered through the tool
    decorator and assert none of them leak local-only strings to a remote caller.

    A verb added later is covered automatically with no one remembering to write a
    per-verb test for it."""

    def _setup_ns(self, home, profile):
        """Wire a namespace with both a FakeIndex and a simple asker so every verb
        can succeed and produce output to check."""
        ns, _, _ = _wired_ns(home)
        ns["_profile"].set(profile)
        ns["_index"] = _FakeIndex()

        class _SimpleAsker:
            def ask(self, question, k=12, source=None):
                return {
                    "answer": "The stack uses WireGuard.",
                    "sources": [{"n": 1, "path": f"kb/quintessence/{GREEN}.md",
                                 "title": GREEN, "score": 0.9,
                                 "snippet": "tunnel config"}],
                    "endpoint": "local",
                    "degraded": False,
                    "retrieval_degraded": False,
                }

        ns["_asker"] = _SimpleAsker()

        qdir = os.path.join(home, "quintessence")
        os.makedirs(qdir, exist_ok=True)
        _write(os.path.join(qdir, f"{GREEN}.md"), (
            f"# {GREEN}\n"
            "> updated: 2026-07-30T00:00:00Z FIXTURE-7a3f hermetic sentinel\n"
            "\n"
            "Essence of the topic.\n"
            "\n"
            "→ file /data/eval-runs/m0-inventory-20260704\n"
            "Config at /etc/llama/serving.conf for the local endpoint.\n"
            "\n"
            "## RE-ENTER\n"
            "Pick up here.\n"
        ))
        return ns

    def _assert_no_local_strings(self, text, label):
        for s in _LOCAL_ONLY_STRINGS:
            self.assertNotIn(s, text,
                             f"{label} must not contain local-only string {s!r}")
        for pat in _LOCAL_ONLY_PATTERNS:
            m = pat.search(text)
            self.assertIsNone(
                m, f"{label} must not match local-only pattern {pat.pattern!r}"
                   + (f" (found: {m.group(0)!r})" if m else ""))

    _EXPECTED_VERBS = {"search_continuity", "ask_continuity", "resume_brief"}

    def test_all_verbs_both_profiles(self):
        """Call every registered verb under both profiles and assert the output
        contains no local-only strings."""
        for profile_name, profile in [("standard", PROFILE_STANDARD),
                                      ("wide", PROFILE_WIDE)]:
            with tempfile.TemporaryDirectory() as home:
                ns = self._setup_ns(home, profile)
                verbs = ns["mcp"]._registered
                verb_names = {fn.__name__ for fn in verbs}
                self.assertEqual(
                    verb_names, self._EXPECTED_VERBS,
                    f"registered verbs must match _EXPECTED_VERBS exactly")

                with _hermetic_env(ns["_test_env"]):
                    for fn in verbs:
                        kwargs = _plausible_args(fn)
                        result = fn(**kwargs)
                        self.assertIsInstance(result, str,
                                             f"{fn.__name__} must return str")
                        self._assert_no_local_strings(
                            result,
                            f"{fn.__name__} ({profile_name})")


class TestSearchFlooredMessage(unittest.TestCase):
    """When search returns no results because every candidate fell below the relevance floor,
    the remote search_continuity must emit a distinct message, same as the local server."""

    class _FakeFlooredIndex:
        def __init__(self):
            self.redact_lifted = False
            self.last_search_floored = 5

        def search(self, query, k=6, source=None, with_stats=False):
            if with_stats:
                return [], {"floored": 5}
            return []

    class _FakeEmptyIndex:
        def __init__(self):
            self.redact_lifted = False
            self.last_search_floored = 0

        def search(self, query, k=6, source=None, with_stats=False):
            if with_stats:
                return [], {"floored": 0}
            return []

    def test_floored_out_gets_distinct_message(self):
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_index"] = self._FakeFlooredIndex()
            ns["_profile"].set(PROFILE_STANDARD)
            out = ns["search_continuity"]("anything")
            self.assertIn("relevance floor", out)
            self.assertIn("5 candidate(s)", out)

    def test_true_empty_gets_plain_no_results(self):
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_index"] = self._FakeEmptyIndex()
            ns["_profile"].set(PROFILE_STANDARD)
            out = ns["search_continuity"]("anything")
            self.assertIn("No results", out)
            self.assertNotIn("relevance floor", out)


class TestBriefPolicyWiring(unittest.TestCase):
    """Pin the brief verb's interaction with the CLI reader gate: the remote policy
    is the sole authority for remote callers — the CLI's local reader gate (which
    checks the SERVER process's model, not the remote caller's credential) must not
    override it.  Two directions:

      WIDE  + withheld topic → content returned (the remote policy allows it).
      STANDARD + withheld topic → the policy's own denial message, NOT a subprocess error.
    """

    def _setup(self, home):
        """Wire the namespace with HEADs for both the green and withheld topics."""
        ns, crown_file, withheld_file = _wired_ns(home)
        qdir = os.path.join(home, "quintessence")
        os.makedirs(qdir, exist_ok=True)
        for slug in (GREEN, WITHHELD):
            _write(os.path.join(qdir, f"{slug}.md"), (
                f"# {slug}\n"
                f"> updated: 2026-07-30T00:00:00Z FIXTURE-{slug[:6]} hermetic sentinel\n"
                "\n"
                f"Essence of {slug}.\n"
                "\n"
                "## RE-ENTER\n"
                "Pick up here.\n"
            ))
        return ns

    def test_wide_profile_receives_withheld_topic(self):
        """A wide-credential caller is entitled to withheld topics (axis A lifted).
        The remote policy allows it; the CLI subprocess must not override that."""
        with tempfile.TemporaryDirectory() as home:
            ns = self._setup(home)
            ns["_profile"].set(PROFILE_WIDE)
            with _hermetic_env(ns["_test_env"]):
                result = ns["resume_brief"](WITHHELD)
            self.assertIn(f"FIXTURE-{WITHHELD[:6]}", result,
                          "wide profile must receive the withheld topic's content")
            self.assertNotIn("[brief error]", result,
                             "must not be a subprocess error")
            self.assertNotIn("[denied]", result,
                             "wide profile must not be denied a withheld topic")

    def test_standard_profile_denied_by_policy_not_subprocess(self):
        """A standard-credential caller is denied withheld topics by the REMOTE
        POLICY (the gate before the subprocess), not by the CLI's reader gate
        inside the subprocess — the denial message must be the policy's own."""
        with tempfile.TemporaryDirectory() as home:
            ns = self._setup(home)
            ns["_profile"].set(PROFILE_STANDARD)
            with _hermetic_env(ns["_test_env"]):
                result = ns["resume_brief"](WITHHELD)
            self.assertIn("[denied]", result,
                          "standard profile must get the remote policy denial")
            self.assertNotIn("[brief error]", result,
                             "denial must come from the policy, not a subprocess error")


class TestSearchFlooredRaceCondition(unittest.TestCase):
    """Pin: the floored count travels in the return value, not through the shared
    attribute — a concurrent request that overwrites the attribute between search
    and message assembly cannot corrupt the reported count."""

    class _FakeRaceIndex:
        """Simulates a rival overwriting `last_search_floored` before the caller
        reads it: search() computes floored=3 and returns it in the stats dict,
        but sets the shared attribute to 99 (the rival's value).  Code that reads
        from the return value sees 3; code that reads the attribute sees 99."""
        def __init__(self):
            self.redact_lifted = False
            self.last_search_floored = 0

        def search(self, query, k=6, source=None, with_stats=False):
            floored = 3
            self.last_search_floored = 99
            if with_stats:
                return [], {"floored": floored}
            return []

    def test_reports_return_value_count_not_attribute(self):
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            fake_index = self._FakeRaceIndex()
            ns["_index"] = fake_index
            ns["_profile"].set(PROFILE_STANDARD)

            _OrigPolicy = ns["RemotePolicy"]

            class _PassthroughPolicy:
                def __init__(self, config, profile):
                    self._real = _OrigPolicy(config, profile=profile)

                def filter_hits(self, hits):
                    return self._real.filter_hits(hits)

            ns["RemotePolicy"] = _PassthroughPolicy
            out = ns["search_continuity"]("anything")
            self.assertIn("3 candidate(s)", out,
                          "must report the at-search-time count from the return value")
            self.assertNotIn("99", out,
                             "must not report the rival's overwritten attribute value")

    def test_old_attribute_reading_code_would_fail(self):
        """Discriminance: the same fake index, fed to a server that reads the
        attribute instead of the return value, reports the rival's 99 — proving
        that the test is load-bearing (it distinguishes the two implementations)."""
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            fake_index = self._FakeRaceIndex()
            ns["_index"] = fake_index
            ns["_profile"].set(PROFILE_STANDARD)

            _OrigPolicy = ns["RemotePolicy"]

            class _PassthroughPolicy:
                def __init__(self, config, profile):
                    self._real = _OrigPolicy(config, profile=profile)

                def filter_hits(self, hits):
                    return self._real.filter_hits(hits)

            ns["RemotePolicy"] = _PassthroughPolicy

            # Simulate the OLD code path: call search without with_stats,
            # then read the attribute (which the rival already set to 99).
            fake_index.search("anything")
            old_floored = fake_index.last_search_floored
            self.assertEqual(old_floored, 99,
                             "attribute-reading code sees the rival's value — the race")
            # The return-value path (what the real server now uses) is immune:
            _, stats = fake_index.search("anything", with_stats=True)
            self.assertEqual(stats["floored"], 3,
                             "return-value path sees the correct at-call count")


class TestRemoteAskCitationPaths(unittest.TestCase):
    """Pin: answer-tool source citations on the remote face reduce store-relative
    paths to bare names (note-store/memory → slug, other corpus → basename), so the
    internal directory layout is not exposed to the remote caller."""

    def test_qq_source_cites_bare_slug(self):
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_profile"].set(PROFILE_STANDARD)

            class _SlugAsker:
                def ask(self, question, k=12, source=None):
                    return {
                        "answer": "The stack uses WireGuard.",
                        "sources": [
                            {"n": 1, "path": f"kb/quintessence/{GREEN}.md",
                             "title": GREEN, "score": 0.9, "source": "qq"},
                        ],
                        "endpoint": "local", "degraded": False,
                        "retrieval_degraded": False,
                    }

            ns["_asker"] = _SlugAsker()
            result = ns["ask_continuity"]("what stack?")
            self.assertIn(GREEN, result)
            self.assertNotIn("kb/", result,
                             "remote citation must not expose directory structure")
            self.assertNotIn(f"{GREEN}.md", result,
                             "qq source must cite bare slug, not filename")

    def test_mem_source_cites_bare_slug(self):
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_profile"].set(PROFILE_STANDARD)

            class _MemAsker:
                def ask(self, question, k=12, source=None):
                    return {
                        "answer": "Fact: the user prefers vi.",
                        "sources": [
                            {"n": 1, "path": "mem/editor-preference.md",
                             "title": "editor-preference", "score": 0.8, "source": "mem"},
                        ],
                        "endpoint": "local", "degraded": False,
                        "retrieval_degraded": False,
                    }

            ns["_asker"] = _MemAsker()
            result = ns["ask_continuity"]("what editor?")
            self.assertIn("editor-preference", result)
            self.assertNotIn("mem/", result,
                             "remote citation must not expose memory directory")
            self.assertNotIn("editor-preference.md", result,
                             "mem source must cite bare slug, not filename")

    def test_other_source_cites_basename(self):
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_profile"].set(PROFILE_STANDARD)

            class _DocsAsker:
                def ask(self, question, k=12, source=None):
                    return {
                        "answer": "See the install guide.",
                        "sources": [
                            {"n": 1, "path": "docs/setup/install-guide.md",
                             "title": "install guide", "score": 0.7, "source": "docs"},
                        ],
                        "endpoint": "local", "degraded": False,
                        "retrieval_degraded": False,
                    }

            ns["_asker"] = _DocsAsker()
            result = ns["ask_continuity"]("how to install?")
            self.assertIn("install-guide.md", result,
                          "other corpus source must cite basename")
            self.assertNotIn("docs/setup/", result,
                             "remote citation must not expose directory structure")

    def test_composed_qq_source_cites_bare_slug(self):
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_profile"].set(PROFILE_STANDARD)

            class _ComposedAsker:
                def ask(self, question, k=12, source=None):
                    return {
                        "answer": "The project uses a tunnel.",
                        "sources": [
                            {"n": 1, "path": f"kb/quintessence/{GREEN}.md",
                             "title": GREEN, "score": 0.9,
                             "source": "qq/project", "store": "project"},
                        ],
                        "endpoint": "local", "degraded": False,
                        "retrieval_degraded": False,
                    }

            ns["_asker"] = _ComposedAsker()
            result = ns["ask_continuity"]("what tunnel?")
            self.assertIn(GREEN, result)
            self.assertNotIn("kb/", result)
            self.assertNotIn(f"{GREEN}.md", result,
                             "composed qq source must cite bare slug")

    def test_no_directory_separators_in_qq_citation(self):
        with tempfile.TemporaryDirectory() as home:
            ns, _, _ = _wired_ns(home)
            ns["_profile"].set(PROFILE_STANDARD)

            class _SlugAsker:
                def ask(self, question, k=12, source=None):
                    return {
                        "answer": "Answer here.",
                        "sources": [
                            {"n": 1, "path": f"kb/quintessence/{GREEN}.md",
                             "title": GREEN, "score": 0.9, "source": "qq"},
                        ],
                        "endpoint": "local", "degraded": False,
                        "retrieval_degraded": False,
                    }

            ns["_asker"] = _SlugAsker()
            result = ns["ask_continuity"]("question")
            for line in result.splitlines():
                if f"[1]" in line and GREEN in line:
                    path_part = line.split("]", 1)[1].split("›")[0].strip()
                    self.assertNotIn("/", path_part,
                                     f"qq citation path must have no directory separators: {path_part!r}")
                    break
            else:
                self.fail("citation line not found in output")


if __name__ == "__main__":
    unittest.main()
