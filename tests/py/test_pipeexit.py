# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""A reader that closes early ends every entry point with 141 and no message.

INVARIANTS.md: `a-reader-that-closes-early-ends-every-entry-point-with-141-and-no-message`.

Two halves. TestEveryEntryPointEndsThroughPipeexit DERIVES the entry points (every tracked file
outside tests/ with an `if __name__ == "__main__":` block, extensionless ones included) and
requires each block to end in `pipeexit.run(...)`, with nothing before it but imports and
`sys.path` set-up, so no work runs outside the handler. TestRunUnderAClosedReader runs
`pipeexit.run` itself under a reader that takes one byte and exits, for each way a `main` can
end. Run this file under each Python you support: the failure it pins was in the interpreter's
shutdown flush, and on 3.13 the bare write raised early enough to hide it (tests/py/
test_dispatcher.py's menu test was green on 3.13 and red on 3.14 at 883712f).
"""
import ast
import fcntl
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _treefiles  # noqa: E402


def _main_block(tree):
    for node in tree.body:
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"
                and len(node.test.comparators) == 1
                and isinstance(node.test.comparators[0], ast.Constant)
                and node.test.comparators[0].value == "__main__"):
            return node
    return None


def _is_call(stmt, owner, attr):
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute) and stmt.value.func.attr == attr
            and ast.unparse(stmt.value.func.value) == owner)


def block_problem(source, name="<src>"):
    """None when the source's __main__ block (if any) ends in pipeexit.run(...) with only imports
    and sys.path calls before it; otherwise what is wrong. Raises ValueError with no block."""
    block = _main_block(ast.parse(source, name))
    if block is None:
        raise ValueError(f"{name}: no __main__ block")
    if block.orelse:
        return "the __main__ block has an else branch"
    *before, last = block.body
    if not _is_call(last, "pipeexit", "run"):
        return f"ends in `{ast.unparse(last)}`, not pipeexit.run(...)"
    for stmt in before:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) and \
                ast.unparse(stmt.value.func).startswith("sys.path."):
            continue
        return f"runs `{ast.unparse(stmt)}` before pipeexit.run, outside its handler"
    return None


def entry_points():
    """{relative path: source} for every tracked file outside tests/ with a __main__ block.

    Every such file is tried as Python, whatever its name or shebang: the MCP servers' first line
    is `#!/usr/bin/env -S uv run --script`, which names no python, so a shebang guess missed them.
    A file that does not parse is not Python and is skipped, except a `.py` file, which must parse:
    skipping that would let a broken entry point drop out of the pin silently."""
    out = {}
    for rel in _treefiles.engine_files(ENGINE):
        if rel.startswith("tests/"):
            continue
        path = os.path.join(ENGINE, rel)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as f:
            raw = f.read()
        src = raw.decode("utf-8", errors="replace")
        try:
            tree = ast.parse(src, rel)
        except (SyntaxError, ValueError):
            if rel.endswith(".py"):
                raise
            continue
        if _main_block(tree) is not None:
            out[rel] = src
    return out


class TestEveryEntryPointEndsThroughPipeexit(unittest.TestCase):
    def test_every_derived_entry_point_ends_through_pipeexit_run(self):
        found = entry_points()
        # Positive control on the derivation: the known entry points are in it.
        for known in ("qq", "qq-search", "quote-mask.py", "tools/gen_config_docs.py"):
            self.assertIn(known, found)
        bad = {rel: p for rel, src in found.items() if (p := block_problem(src, rel))}
        self.assertEqual(bad, {})

    def test_the_derivation_does_not_depend_on_the_shebang(self):
        # The MCP servers run under `#!/usr/bin/env -S uv run --script`, whose first line names no
        # python: a shebang guess left them outside the pin (fix-review 20261105T123503Z, N1).
        # The servers are named by suffix, not listed: a tree may hold more or fewer of them.
        found = entry_points()
        servers = [rel for rel in _treefiles.engine_files(ENGINE)
                   if rel.endswith("-mcp") and not rel.startswith("tests/")]
        for known in ("qq-remote-mcp", "qq-search-mcp"):   # positive control on the suffix walk
            self.assertIn(known, servers)
        for rel in servers:
            self.assertIn(rel, sorted(found))

    def test_the_register_states_the_derived_count(self):
        # The register's site-derivation line said 17 while the pin derived 14: hold the number
        # it states to the number derived, so an entry point added or lost moves both or neither.
        with open(os.path.join(ENGINE, "tests", "INVARIANTS.md"), encoding="utf-8") as f:
            text = f.read()
        head = "## `a-reader-that-closes-early-ends-every-entry-point-with-141-and-no-message`"
        section = text.split(head, 1)[1].split("\n## ", 1)[0]
        stated = re.findall(r"that is (\d+) files", section)
        self.assertEqual(len(stated), 1, "the site-derivation line states no single count")
        self.assertEqual(int(stated[0]), len(entry_points()))

    def test_the_block_check_can_fail(self):
        # Negative controls: each shape the check must refuse is refused.
        refused = [
            'if __name__ == "__main__":\n    sys.exit(main())\n',
            'if __name__ == "__main__":\n    main()\n',
            'if __name__ == "__main__":\n    warm()\n    pipeexit.run(main)\n',
            'if __name__ == "__main__":\n    pipeexit.run(main)\nelse:\n    pass\n',
            'if __name__ == "__main__":\n    other.run(main)\n',
        ]
        for src in refused:
            self.assertIsNotNone(block_problem(src), src)
        self.assertIsNone(block_problem(
            'if __name__ == "__main__":\n    sys.path.insert(0, "x")\n'
            '    from quintessence import pipeexit\n    pipeexit.run(main, 1)\n'))


# A main per way of ending, each writing more than the pipe holds. NEAR is a pipe and 512 bytes:
# the tail fits stdout's buffer on every Python (8 KiB on 3.13), so the write RETURNS and only a
# flush can meet the closed reader — the shape that went red on CI. FAR is four pipes, which
# raises inside main on 3.13 and 3.14 both.
_MAINS = {
    "near-write-return": "sys.stdout.write('x' * NEAR); return 0",
    "far-write-return": "sys.stdout.write('x' * FAR); return 0",
    "many-prints-return": "[print('y' * 63) for _ in range(FAR // 64)]; return 0",
    "near-write-sys-exit": "sys.stdout.write('x' * NEAR); sys.exit(3)",
    "near-write-raise": "sys.stdout.write('x' * NEAR); raise ValueError('boom')",
}
_EXPECTED_WITH_A_FULL_READER = {"near-write-return": 0, "far-write-return": 0,
                                "many-prints-return": 0, "near-write-sys-exit": 3,
                                "near-write-raise": 1}


class TestRunUnderAClosedReader(unittest.TestCase):
    def setUp(self):
        r, w = os.pipe()
        try:
            self.pipe = fcntl.fcntl(w, fcntl.F_GETPIPE_SZ)
        finally:
            os.close(r)
            os.close(w)
        self.tmp = tempfile.mkdtemp(prefix="qq-pipeexit-")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp])

    def _script(self, body):
        path = os.path.join(self.tmp, "entry.py")
        with open(path, "w") as f:
            f.write(textwrap.dedent(f"""\
                import sys
                sys.path.insert(0, {ENGINE!r})
                from quintessence import pipeexit
                NEAR, FAR = {self.pipe + 512}, {4 * self.pipe}
                def main():
                    {body}
                pipeexit.run(main)
                """))
        return path

    def _run(self, body, reader):
        cmd = f'"{sys.executable}" "{self._script(body)}" | {reader}; echo "EXIT=${{PIPESTATUS[0]}}" >&2'
        # The traceback is read as text: pin it uncoloured (3.13+ colours it under FORCE_COLOR).
        env = {k: v for k, v in os.environ.items() if k != "FORCE_COLOR"}
        env["PYTHON_COLORS"] = "0"
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=30,
                           env=env)
        rest, _, code = p.stderr.rpartition("EXIT=")
        return int(code.strip()), rest

    def test_a_closed_reader_is_141_and_silent_for_every_way_main_ends(self):
        reader = f'"{sys.executable}" -c "import os; os.read(0, 1)"'
        for name, body in _MAINS.items():
            with self.subTest(name):
                code, err = self._run(body, reader)
                self.assertEqual((code, err), (141, ""))

    def test_a_reader_that_reads_everything_sees_mains_own_exit(self):
        # Control: run() changes nothing when the reader stays, so 141 above is the pipe's doing.
        for name, body in _MAINS.items():
            with self.subTest(name):
                code, err = self._run(body, "cat >/dev/null")
                self.assertEqual(code, _EXPECTED_WITH_A_FULL_READER[name])
                if name == "near-write-raise":
                    self.assertIn("ValueError: boom", err)
                else:
                    self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
