# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for qq-remote-mcp's `load_tokens()` (the ACTUAL script, not a mirror) — same
exec-the-real-script-with-mcp-stubbed technique as test_search_mcp_wiring.py's qq-search-mcp
tests, because qq-remote-mcp imports `mcp`/`uvicorn` at module scope and neither optional
dependency can be assumed installed (an earlier version of this suite stubbed only `mcp`
because uvicorn happened to be present on the author's machine — that assumption failed in CI,
so both are stubbed; the module needs `mcp.server.fastmcp.FastMCP` and
`mcp.server.transport_security.TransportSecuritySettings` just to finish importing). `__name__` is deliberately NOT "__main__", so `main()` — the only
thing that would bind a socket — never runs; HARD SAFETY: this suite never starts the remote
server.

Verification pass (2026-07-29): `load_tokens()`'s own docstring says every misconfiguration is
a clean logged refusal to start (SystemExit with a worded message), but a token file that was
simply ABSENT or permission-denied raised a raw FileNotFoundError/PermissionError instead —
`os.stat(path)` and the subsequent `open(path)` were unguarded. These tests pin the fix: both
cases now raise the same style of SystemExit refusal as every other load_tokens() misconfig,
and a well-formed file still loads (the refactor that batches `open()`'s read under its own
try/except must not change the happy path)."""
import os
import sys
import tempfile
import types
import unittest

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ENGINE)


class _FakeFastMCP:
    def __init__(self, _name, instructions=None, transport_security=None):
        pass

    def tool(self):
        def deco(fn):
            return fn
        return deco

    def run(self):  # pragma: no cover - never invoked (script exec stops before __main__ block)
        pass


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
    # uvicorn is stubbed too, unconditionally: the script only IMPORTS it at module
    # scope (uvicorn.run lives inside main(), which never executes here), and relying
    # on a real install made the suite host-dependent — green on the author's machine,
    # 35 failures + 6 errors in CI, where nothing installs the optional remote deps.
    sys.modules["uvicorn"] = types.ModuleType("uvicorn")


def _exec_qq_remote_mcp(cwd: str, env_overrides: dict) -> dict:
    """Exec the REAL qq-remote-mcp script body (module-level wiring only — see module docstring
    for why `__name__` is never "__main__") under a chdir + env override, and return its module
    namespace so tests can reach the real `load_tokens` function directly."""
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


def _base_env(home: str) -> dict:
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
    return env


class TestLoadTokensCleanRefusal(unittest.TestCase):
    def _load_tokens(self, home: str):
        ns = _exec_qq_remote_mcp(home, _base_env(home))
        return ns["load_tokens"]

    def test_missing_file_refuses_clean_not_a_raw_exception(self):
        with tempfile.TemporaryDirectory() as home:
            load_tokens = self._load_tokens(home)
            missing = os.path.join(home, "no-such-token-file")
            with self.assertRaises(SystemExit) as cm:
                load_tokens(missing)
            self.assertIn("refusing to start", str(cm.exception))
            self.assertIn(missing, str(cm.exception))

    def test_permission_denied_file_refuses_clean_not_a_raw_exception(self):
        with tempfile.TemporaryDirectory() as home:
            load_tokens = self._load_tokens(home)
            path = os.path.join(home, "unreadable-token-file")
            with open(path, "w") as f:
                f.write("standard " + "a" * 40 + "\n")
            os.chmod(path, 0o000)   # rmtree can still remove a 0o000 file (dir perms govern
                                     # unlink, not the file's own bits), so no cleanup needed
            with self.assertRaises(SystemExit) as cm:
                load_tokens(path)
            self.assertIn("refusing to start", str(cm.exception))

    def test_well_formed_file_still_loads(self):
        """Regression guard on the refactor itself: batching the file's lines into a list under
        their own try/except (so a mid-read OSError also gets the clean message) must not change
        the happy path's result."""
        with tempfile.TemporaryDirectory() as home:
            load_tokens = self._load_tokens(home)
            path = os.path.join(home, "token-file")
            std = "s" * 40
            wide = "w" * 40
            with open(path, "w") as f:
                f.write(f"# comment\nstandard {std}\nwide {wide}\n")
            os.chmod(path, 0o600)
            tokens = load_tokens(path)
            self.assertEqual(tokens, {"standard": std, "wide": wide})


if __name__ == "__main__":
    unittest.main()
