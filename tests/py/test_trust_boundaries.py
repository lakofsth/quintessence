# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Trust-boundary pins for the 2026-10-08 posture review (quintessence H1–H2, M1–M9, L1–L9).

Each class pins one invariant from tests/INVARIANTS.md by its slug. Every test here was seen red
at the reviewed tip before the fix it pins landed (the commit message names the mutation)."""
from __future__ import annotations

import ast
import glob
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ENGINE)

from quintessence import admin, atomicio, authgate, httpdirect, mcpedge  # noqa: E402
from quintessence import write as writemod  # noqa: E402
from quintessence.cli import render_fact, render_findings_next  # noqa: E402
from quintessence.config import Config  # noqa: E402
from quintessence.findings import FindingsFile  # noqa: E402
from quintessence.search import SearchIndex  # noqa: E402
from quintessence.store import Store, StorePathError  # noqa: E402
from quintessence.storepath import (PROJECT_STORE_DIRNAME, resolve_store_path,  # noqa: E402
                                    store_is_scaffolded)

_RUN = subprocess.run


def _cfg(base: str, env: dict | None = None, config_file: str | None = None, **over) -> Config:
    o = {"QUINTESSENCE_DIR": os.path.join(base, "store"),
         "QQ_MEMDIR": os.path.join(base, "mem"),
         "QQ_STATE_DIR": os.path.join(base, "state"),
         "QQ_KB_ROOT": os.path.join(base, "kb")}
    o.update(over)
    return Config(env=env if env is not None else {}, config_file=config_file or "/nonexistent",
                  overrides=o)


def _git_store(qdir: str) -> None:
    os.makedirs(qdir, exist_ok=True)
    _RUN(["git", "init", "-q", qdir], check=True)
    _RUN(["git", "-C", qdir, "config", "user.email", "t@t"], check=True)
    _RUN(["git", "-C", qdir, "config", "user.name", "t"], check=True)


def _scaffold(store_dir: str) -> None:
    """What `qq init --project` leaves behind, minus the git objects: enough for discovery."""
    hooks = os.path.join(store_dir, ".git", "hooks")
    os.makedirs(hooks, exist_ok=True)
    with open(os.path.join(hooks, "pre-commit"), "w", encoding="utf-8") as fh:
        fh.write(admin._PRE_COMMIT_HOOK)


# ---- the-mcp-passthrough-cannot-reach-a-shell ---------------------------------------------------
class _FakeFastMCP:
    def __init__(self, *a, **k):
        pass

    def tool(self):
        def deco(fn):
            return fn
        return deco

    def run(self):
        pass


def _exec_search_mcp(home: str) -> dict:
    fake_mcp = types.ModuleType("mcp")
    fake_server = types.ModuleType("mcp.server")
    fake_fastmcp = types.ModuleType("mcp.server.fastmcp")
    fake_fastmcp.FastMCP = _FakeFastMCP
    fake_server.fastmcp = fake_fastmcp
    fake_mcp.server = fake_server
    sys.modules["mcp"] = fake_mcp
    sys.modules["mcp.server"] = fake_server
    sys.modules["mcp.server.fastmcp"] = fake_fastmcp
    script = os.path.join(ENGINE, "qq-search-mcp")
    with open(script, encoding="utf-8") as f:
        code = compile(f.read(), script, "exec")
    ns = {"__name__": "qq_search_mcp_under_test", "__file__": script}
    old_cwd, old_env = os.getcwd(), dict(os.environ)
    try:
        os.chdir(home)
        os.environ.clear()
        os.environ.update({"HOME": home, "PATH": old_env.get("PATH", ""),
                           "QQ_CONFIG": os.path.join(home, "config"),
                           "QQ_STATE_DIR": os.path.join(home, "state"),
                           "QQ_MEMDIR": os.path.join(home, "mem"),
                           "QQ_KB_ROOT": os.path.join(home, "kb"),
                           "QUINTESSENCE_DIR": os.path.join(home, "quintessence")})
        os.makedirs(os.path.join(home, "quintessence"), exist_ok=True)
        exec(code, ns)
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)
    return ns


class TestMcpEdge(unittest.TestCase):
    def test_probe_ref_is_refused_in_every_flag_spelling(self):
        for args in (["update", "t", "--ref", "probe:id", "x"],
                     ["update", "t", "--ref=probe:id", "x"],
                     ["new", "t", "--ref", "probe:true", "essence"],
                     ["essence", "t", "--ref=probe:cat", "e"]):
            self.assertIsNotNone(mcpedge.check_passthrough(args), args)

    def test_probe_after_double_dash_is_literal_text_and_allowed(self):
        self.assertIsNone(mcpedge.check_passthrough(["update", "t", "--", "--ref=probe:x"]))

    def test_admin_verbs_and_unknown_verbs_are_refused(self):
        for args in (["config", "set", "PATH", "/x"], ["init"], ["reconcile"], ["bogus"], []):
            self.assertIsNotNone(mcpedge.check_passthrough(args), args)

    def test_everyday_verbs_pass(self):
        for args in (["menu"], ["list"], ["fact", "x"], ["new", "t", "e"], ["finalize", "t"],
                     ["delete", "t"], ["check"], ["findings", "next"]):
            self.assertIsNone(mcpedge.check_passthrough(args), args)

    def test_script_wiring_refuses_before_spawning_and_nulls_stdin(self):
        with tempfile.TemporaryDirectory() as home:
            ns = _exec_search_mcp(home)
            calls: list = []

            def fake_run(argv, **kw):
                calls.append((argv, kw))
                return types.SimpleNamespace(stdout="ok", stderr="", returncode=0)

            ns["subprocess"].run = fake_run
            try:
                out = ns["qq"](["update", "t", "--ref", "probe:id", "x"])
                self.assertEqual(calls, [], "a refused passthrough must never spawn qq")
                self.assertIn("refus", out.lower())
                ns["update_head"]("t", "--ref=probe:x")
                argv, kw = calls[-1]
                self.assertEqual(argv[1:], ["update", "t", "--", "--ref=probe:x"])
                self.assertIs(kw.get("stdin"), subprocess.DEVNULL)
                ns["qq"](["menu"])
                self.assertIs(calls[-1][1].get("stdin"), subprocess.DEVNULL)
            finally:
                ns["subprocess"].run = _RUN


# ---- config-set-writes-one-line-of-one-registered-shape ----------------------------------------
class TestConfigSet(unittest.TestCase):
    def _cfg_with_file(self, base: str) -> tuple[Config, str]:
        f = os.path.join(base, "config")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("QQ_DIGEST_PIN=keep\n")
        return _cfg(base, config_file=f), f

    def test_newline_in_value_is_refused_and_the_file_is_untouched(self):
        with tempfile.TemporaryDirectory() as base:
            cfg, f = self._cfg_with_file(base)
            for bad in ("x\nQUINTESSENCE_DIR=/srv/elsewhere", "x\rY=1"):
                with self.assertRaises(admin.AdminError):
                    admin.config_set(cfg, "QQ_DIGEST_PIN", bad, force=True)
            with open(f, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "QQ_DIGEST_PIN=keep\n")

    def test_key_must_be_an_upper_identifier(self):
        with tempfile.TemporaryDirectory() as base:
            cfg, f = self._cfg_with_file(base)
            for bad in ("path", "QQ-X", "1QQ", "QQ_X=Y", "QQ X", ""):
                with self.assertRaises(admin.AdminError):
                    admin.config_set(cfg, bad, "v", force=True)

    def test_gate_keys_need_a_terminal(self):
        with tempfile.TemporaryDirectory() as base:
            cfg, f = self._cfg_with_file(base)
            for key in sorted(admin.GATE_KEYS):
                with self.assertRaises(admin.AdminError):
                    admin.config_set(cfg, key, "0", force=True, interactive=False)
            admin.config_set(cfg, "QQ_AUTHOR_GATE", "0", force=True, interactive=True)
            with open(f, encoding="utf-8") as fh:
                self.assertIn("QQ_AUTHOR_GATE=0", fh.read())


# ---- the-authoring-gate-reads-its-policy-from-the-file -----------------------------------------
class TestAuthgateEnvBlind(unittest.TestCase):
    def _transcript(self, base: str, model: str) -> str:
        p = os.path.join(base, "t.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"model": model}}) + "\n")
        return p

    def test_env_cannot_switch_the_gate_off_or_borrow_trust(self):
        with tempfile.TemporaryDirectory() as base:
            f = os.path.join(base, "config")
            with open(f, "w", encoding="utf-8") as fh:
                fh.write("QQ_AUTHOR_GATE_SLUGS=sec-*\n")
            opus = self._transcript(base, "claude-opus-4-6")
            env = {"QQ_AUTHOR_GATE": "0", "QQ_MODEL_TRANSCRIPT": opus,
                   "QQ_SAFE_MODEL_PREFIX": "claude-", "QQ_WRITE_TRUSTED_MODEL": "x",
                   "QQ_AUTHOR_GATE_SLUGS": ""}
            cfg = _cfg(base, env=env, config_file=f)
            # file says gated, env says off / trusted: the file wins and no transcript is found
            self.assertEqual(authgate.gate_reason(cfg, "sec-topic"), "unknown")
            # the same keys as constructor overrides (the test seam) are still honoured
            cfg2 = _cfg(base, env=env, config_file=f, QQ_MODEL_TRANSCRIPT=opus)
            self.assertIsNone(authgate.gate_reason(cfg2, "sec-topic"))

    def test_every_config_read_in_authgate_is_env_blind(self):
        src = open(os.path.join(ENGINE, "quintessence", "authgate.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        offenders = []
        policy_lines = set()
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name == "_policy":
                policy_lines = set(range(fn.lineno, fn.end_lineno + 1))
        self.assertTrue(policy_lines, "_policy (the one trusted/untrusted branch) must exist")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "config":
                if node.lineno in policy_lines:
                    continue
                if node.func.attr not in ("get_noenv", "resolve_raw"):
                    offenders.append(f"{node.func.attr}@{node.lineno}")
        self.assertEqual(offenders, [], offenders)
        # and gate_reason, the write-trust decision, asks for the trusted resolution
        src_gate = src[src.index("def gate_reason("):]
        self.assertIn("model_identity(config, trusted=True)", src_gate)
        self.assertIn("trusted=True)", src_gate.split("model_mode(")[1][:80])


# ---- a-write-lands-inside-the-store-after-resolving ----------------------------------------------
class TestWriteTargets(unittest.TestCase):
    def test_update_through_a_symlink_out_of_the_store_is_refused(self):
        with tempfile.TemporaryDirectory() as base:
            cfg = _cfg(base)
            qdir = cfg.get_path("QUINTESSENCE_DIR")
            _git_store(qdir)
            victim = os.path.join(base, "victim.md")
            with open(victim, "w", encoding="utf-8") as fh:
                fh.write("# rc\nexport X=1\n")
            os.symlink(victim, os.path.join(qdir, "foo.md"))
            store = Store(cfg)
            with self.assertRaises(writemod.WriteError):
                writemod.update(store, "foo.md", "an update line\n")
            with self.assertRaises(writemod.WriteError):
                writemod.update(store, "foo", "an update line\n")
            with open(victim, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "# rc\nexport X=1\n")

    def test_update_through_a_symlinked_parent_is_refused(self):
        with tempfile.TemporaryDirectory() as base:
            cfg = _cfg(base)
            qdir = cfg.get_path("QUINTESSENCE_DIR")
            _git_store(qdir)
            outside = os.path.join(base, "outside")
            os.makedirs(outside)
            with open(os.path.join(outside, "x.md"), "w", encoding="utf-8") as fh:
                fh.write("# x\n")
            os.symlink(outside, os.path.join(qdir, "sub"))
            with self.assertRaises(writemod.WriteError):
                writemod.update(Store(cfg), "sub/x.md", "line\n")

    def test_topics_carry_no_control_characters(self):
        with tempfile.TemporaryDirectory() as base:
            cfg = _cfg(base)
            os.makedirs(cfg.get_path("QUINTESSENCE_DIR"))
            store = Store(cfg)
            for bad in ("a\nb", "a\rb", "a\x00b", "a\x1bb", "a\x7fb"):
                with self.assertRaises(StorePathError):
                    store.head_path(bad)
                with self.assertRaises(writemod.WriteError):
                    writemod._normalize_target(store, bad)

    def test_fact_cannot_traverse(self):
        with tempfile.TemporaryDirectory() as base:
            cfg = _cfg(base)
            os.makedirs(cfg.get_path("QQ_MEMDIR"))
            with open(os.path.join(base, "secret.md"), "w", encoding="utf-8") as fh:
                fh.write("secret\n")
            store = Store(cfg)
            with self.assertRaises(StorePathError):
                store.memory_path("../secret")
            with self.assertRaises(StorePathError):
                render_fact(store, "../secret.md")


# ---- a-project-store-is-one-this-operator-scaffolded --------------------------------------------
class TestProjectStoreTrust(unittest.TestCase):
    def _home(self, tmp):
        home = os.path.join(tmp, "home")
        os.makedirs(os.path.join(home, "quintessence"))
        return home

    @staticmethod
    def _user_cfg(home: str, **extra) -> Config:
        """The user store named in the CONFIG FILE (an override would pin it and skip discovery)."""
        f = os.path.join(home, "config")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(f"QUINTESSENCE_DIR={os.path.join(home, 'quintessence')}\n")
            for k, v in extra.items():
                fh.write(f"{k}={v}\n")
        return Config(env={}, config_file=f, overrides={})

    def test_a_bare_directory_is_not_a_project_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            proj = os.path.join(home, "repo")
            os.makedirs(os.path.join(proj, PROJECT_STORE_DIRNAME))
            cfg = self._user_cfg(home)
            sp = resolve_store_path(cwd=proj, env={}, config=cfg, home=home)
            self.assertEqual(len(sp.path), 1)

    def test_a_scaffolded_store_is_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            proj = os.path.join(home, "repo")
            pstore = os.path.join(proj, PROJECT_STORE_DIRNAME)
            _scaffold(pstore)
            cfg = self._user_cfg(home)
            sp = resolve_store_path(cwd=proj, env={}, config=cfg, home=home)
            self.assertEqual(sp.path[0].qdir, pstore)

    def test_hookspath_or_fsmonitor_in_git_config_disqualifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            pstore = os.path.join(tmp, PROJECT_STORE_DIRNAME)
            _scaffold(pstore)
            self.assertTrue(store_is_scaffolded(pstore))
            for line in ("[core]\n\thooksPath = /tmp/h\n", "[core]\n\tfsmonitor = /tmp/f\n"):
                with open(os.path.join(pstore, ".git", "config"), "w", encoding="utf-8") as fh:
                    fh.write(line)
                self.assertFalse(store_is_scaffolded(pstore), line)

    def test_allowlist_when_set_restricts_further(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(tmp)
            proj = os.path.join(home, "repo")
            pstore = os.path.join(proj, PROJECT_STORE_DIRNAME)
            _scaffold(pstore)
            cfg = self._user_cfg(home, QQ_PROJECT_STORES=os.path.join(home, "other"))
            self.assertEqual(len(resolve_store_path(cwd=proj, env={}, config=cfg, home=home).path), 1)
            cfg = self._user_cfg(home, QQ_PROJECT_STORES=proj)
            self.assertEqual(resolve_store_path(cwd=proj, env={}, config=cfg, home=home).path[0].qdir,
                             pstore)

    def test_corpus_walk_does_not_follow_a_symlink_out_of_its_root(self):
        with tempfile.TemporaryDirectory() as base:
            cfg = _cfg(base)
            kb = cfg.get_path("QQ_KB_ROOT")
            src = os.path.join(kb, "notes")
            os.makedirs(src)
            with open(os.path.join(src, "in.md"), "w", encoding="utf-8") as fh:
                fh.write("# in\n")
            outside = os.path.join(base, "outside")
            os.makedirs(outside)
            with open(os.path.join(outside, "leak.md"), "w", encoding="utf-8") as fh:
                fh.write("# leak\n")
            os.symlink(outside, os.path.join(src, "escape"))
            inside_link = os.path.join(kb, "alias")
            os.symlink(src, inside_link)   # a root that IS a symlink still walks its own tree
            idx = SearchIndex(cfg)
            files = sorted(os.path.basename(p) for p in idx._files(src))
            self.assertEqual(files, ["in.md"])
            self.assertEqual(sorted(os.path.basename(p) for p in idx._files(inside_link)), ["in.md"])


# ---- store-text-leaves-only-for-the-endpoint-named ---------------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    hits: list = []

    def do_GET(self):
        type(self).hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    do_POST = do_GET

    def log_message(self, *a):
        pass


class TestDirectHttp(unittest.TestCase):
    def test_local_endpoint_is_reached_despite_http_proxy(self):
        """As deployed: the proxy variables are in the environment BEFORE the interpreter starts,
        so every opener that reads them at construction reads them. A fresh subprocess is the
        only honest model of that; an in-process env change is read by nothing already built."""
        srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{port}/api/tags"
            prog = (
                "import json, sys, urllib.error, urllib.request\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from quintessence import httpdirect\n"
                "url = sys.argv[2]\n"
                "try:\n"
                "    urllib.request.urlopen(url, timeout=2); print('control:reached')\n"
                "except urllib.error.URLError:\n"
                "    print('control:proxied')\n"
                "with httpdirect.urlopen(url, timeout=5) as r:\n"
                "    print('direct:' + json.dumps(json.load(r)))\n")
            env = {k: v for k, v in os.environ.items()
                   if k.lower() not in ("http_proxy", "https_proxy", "no_proxy", "all_proxy")}
            env["http_proxy"] = "http://127.0.0.1:9/"   # discard port: nothing listens
            p = _RUN([sys.executable, "-I", "-c", prog, ENGINE, url], env=env,
                     capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            lines = p.stdout.splitlines()
            self.assertIn("control:proxied", lines, "negative control: the stock opener must have "
                          "gone to the proxy, or this pin proves nothing")
            self.assertIn('direct:{"ok": true}', lines)
            self.assertEqual(_Handler.hits, ["/api/tags"])
        finally:
            srv.shutdown()

    def test_no_stock_urlopen_outside_httpdirect(self):
        hits = []
        for p in glob.glob(os.path.join(ENGINE, "quintessence", "*.py")):
            if os.path.basename(p) == "httpdirect.py":
                continue
            src = open(p, encoding="utf-8").read()
            if re.search(r"urllib\.request\.urlopen\(", src):
                hits.append(os.path.basename(p))
        self.assertEqual(hits, [], hits)


# ---- the-remote-face-names-only-what-policy-allows ---------------------------------------------
class TestRemoteFace(unittest.TestCase):
    def test_name_hints_are_stripped(self):
        text = "----- no HEAD for 'fo' (skipping) -----  Did you mean: foo, secret-plan?\n"
        self.assertEqual(mcpedge.strip_name_hints(text), "----- no HEAD for 'fo' (skipping) -----\n")
        self.assertEqual(mcpedge.strip_name_hints("plain\n"), "plain\n")

    def test_k_is_clamped(self):
        self.assertEqual(mcpedge.clamp_k(-5), 1)
        self.assertEqual(mcpedge.clamp_k(0), 1)
        self.assertEqual(mcpedge.clamp_k(7), 7)
        self.assertEqual(mcpedge.clamp_k(10 ** 9), 50)
        self.assertEqual(mcpedge.clamp_k("12"), 12)
        self.assertEqual(mcpedge.clamp_k("junk"), 1)

    def test_remote_script_routes_through_the_helpers(self):
        src = open(os.path.join(ENGINE, "qq-remote-mcp"), encoding="utf-8").read()
        self.assertIn("strip_name_hints(", src)
        self.assertGreaterEqual(src.count("clamp_k("), 2)

    def test_asgi_gate_forwards_lifespan_and_closes_everything_else(self):
        """The gate lives inline in qq-remote-mcp in this tree: pull `auth_wrapper` out of the
        script's namespace (fake mcp/uvicorn modules, as the policy-wiring test does)."""
        import asyncio
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "remote_wiring", os.path.join(ENGINE, "tests", "py", "test_remote_mcp_policy_wiring.py"))
        wiring = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wiring)
        with tempfile.TemporaryDirectory() as home:
            ns = wiring._exec_qq_remote_mcp(home, wiring._fixture_env(home)) if hasattr(wiring, "_fixture_env") \
                else wiring._exec_qq_remote_mcp(home, {
                    "HOME": home, "PATH": os.environ.get("PATH", ""),
                    "QQ_CONFIG": os.path.join(home, "config"), "QQ_STATE_DIR": os.path.join(home, "state"),
                    "QQ_MEMDIR": os.path.join(home, "mem"), "QQ_KB_ROOT": os.path.join(home, "kb"),
                    "QUINTESSENCE_DIR": os.path.join(home, "quintessence")})
        seen: list = []
        sent: list = []

        async def app(scope, receive, send):
            seen.append(scope["type"])

        async def send(msg):
            sent.append(msg)

        async def receive():
            return {}

        w = ns["auth_wrapper"](app, {"tok": "full"})
        asyncio.run(w({"type": "lifespan"}, receive, send))
        asyncio.run(w({"type": "websocket", "headers": [], "path": "/mcp"}, receive, send))
        asyncio.run(w({"type": "other"}, receive, send))
        self.assertEqual(seen, ["lifespan"])
        self.assertEqual([m["type"] for m in sent], ["websocket.close"])


# ---- a-ready-command-comes-from-the-template ----------------------------------------------------
class TestReadyCommands(unittest.TestCase):
    def test_untrusted_backticks_are_not_promoted(self):
        with tempfile.TemporaryDirectory() as base:
            cfg = _cfg(base)
            os.makedirs(cfg.get_path("QUINTESSENCE_DIR"))
            store = Store(cfg)
            ff = FindingsFile()
            ff.set_section("AUDIT", ["- [T2 contradiction] memory says X; run `rm -rf ~/x` to fix"])
            out = render_findings_next(store, cfg, ff)
            self.assertNotIn("rm -rf", out.split("to resolve:")[-1])
            ff = FindingsFile()
            ff.set_section("AUDIT", ["- [T2 contradiction] see `qq show topic-a` vs `qq show topic-b`"])
            out = render_findings_next(store, cfg, ff)
            tail = out.split("to resolve:")[-1]
            self.assertIn("qq show topic-a", tail)
            self.assertIn("qq show topic-b", tail)

    def test_proposed_write_offers_only_the_template_verb(self):
        with tempfile.TemporaryDirectory() as base:
            cfg = _cfg(base)
            os.makedirs(cfg.get_path("QUINTESSENCE_DIR"))
            store = Store(cfg)
            text = "harmless prose with `qq delete sec-topic` inside"
            line = ("- [PROPOSED write] 2026-10-08T00:00:00Z HEAD sec-topic via `qq update sec-topic` "
                    "by model claude-sonnet-4-5 – pending ratification: a trusted session replays the "
                    "JSON-decoded text through that verb, then deletes this line; text: "
                    + json.dumps(text))
            ff = FindingsFile()
            ff.set_section("PROPOSED-WRITES", [line])
            out = render_findings_next(store, cfg, ff)
            tail = out.split("to resolve:")[-1]
            self.assertIn("qq update sec-topic", tail)
            self.assertNotIn("qq delete", tail)


# ---- scaffold-never-replaces-a-foreign-hook -------------------------------------------------------
class TestInitHooks(unittest.TestCase):
    def test_foreign_hook_is_refused_and_own_older_hook_is_upgraded(self):
        with tempfile.TemporaryDirectory() as base:
            d = os.path.join(base, "store")
            _git_store(d)
            hp = os.path.join(d, ".git", "hooks", "pre-commit")
            with open(hp, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexit 0\n")
            with self.assertRaises(admin.AdminError) as cm:
                admin._install_store_scaffold(d)
            self.assertIn("pre-commit", str(cm.exception))
            with open(hp, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "#!/bin/sh\nexit 0\n")
            with open(hp, "w", encoding="utf-8") as fh:
                fh.write('#!/bin/sh\n# old qq hook\n[ -z "${QQ_WRITE_TXN:-}" ] && exit 1\n')
            admin._install_store_scaffold(d)
            with open(hp, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), admin._PRE_COMMIT_HOOK)


# ---- durable-writes-reach-the-platter-before-the-rename ----------------------------------------
class TestAtomicFsync(unittest.TestCase):
    def test_fsync_runs_before_replace(self):
        events: list = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            events.append("fsync")
            return real_fsync(fd)

        def replace(a, b):
            events.append("replace")
            return real_replace(a, b)

        with tempfile.TemporaryDirectory() as base:
            os.fsync, os.replace = fsync, replace
            try:
                atomicio.atomic_write_text(os.path.join(base, "f"), "x\n")
            finally:
                os.fsync, os.replace = real_fsync, real_replace
        self.assertIn("fsync", events)
        self.assertLess(events.index("fsync"), events.index("replace"))


if __name__ == "__main__":
    unittest.main()
