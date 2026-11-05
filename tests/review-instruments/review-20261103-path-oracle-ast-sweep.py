#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Retained by fix-review 20261102T222427Z (report: a private review record).
# Not a gate: tests/run.sh does not run it. Usage: python3 tests/review-instruments/review-20261103-path-oracle-ast-sweep.py .
"""item2-path-oracle-ast-sweep.py ENGINE — fix-review 20261102T222427Z, item 2 (22df7f1 class sweep).
Derives, by AST rather than by grep, every `in` / `not in` comparison and every assertIn/assertNotIn call in tests/py/*.py whose LEFT
operand is a string literal and whose RIGHT operand's source text names a path-like value (contains
'path', 'dir', 'tmp', 'base', 'file', or is a bare name p/f/fp/d/paths/files/out/err/stdout/stderr).
Then, for each, reports whether the literal is a word a scratch directory could plausibly carry
(a token of [A-Za-z0-9._-]{3,}) — those are the candidate sites of the invariant
a-test-oracle-over-paths-does-not-read-the-scratch-directory-name. Candidates are for a reader to
classify; the instrument does not decide whether the right operand is an absolute path."""
import ast, os, re, sys
eng = sys.argv[1]
PATHY = re.compile(r"path|dir|tmp|base|file|\bp\b|\bf\b|\bfp\b|\bd\b|\bout\b|\berr\b|stdout|stderr")
n_cmp = 0; rows = []
for fn in sorted(os.listdir(os.path.join(eng, "tests/py"))):
    if not fn.endswith(".py"): continue
    src = open(os.path.join(eng, "tests/py", fn)).read()
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("assertIn", "assertNotIn") and len(node.args) >= 2
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            n_cmp += 1
            rs = ast.get_source_segment(src, node.args[1]) or ""
            if PATHY.search(rs) and re.fullmatch(r"[A-Za-z0-9._-]{3,}", node.args[0].value):
                rows.append("%s:%d  %s(%r, %s)" % (fn, node.lineno, node.func.attr, node.args[0].value, rs[:70]))
            continue
        if not isinstance(node, ast.Compare): continue
        for op, right in zip(node.ops, node.comparators):
            if not isinstance(op, (ast.In, ast.NotIn)): continue
            if not (isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)): continue
            n_cmp += 1
            rs = ast.get_source_segment(src, right) or ""
            if PATHY.search(rs) and re.fullmatch(r"[A-Za-z0-9._-]{3,}", node.left.value):
                rows.append("%s:%d  %r %s %s" % (fn, node.lineno, node.left.value,
                            "in" if isinstance(op, ast.In) else "not in", rs[:70]))
print("string-literal in/not-in comparisons and assertIn/NotIn calls: %d; path-like right operand with a word literal: %d" % (n_cmp, len(rows)))
for r in rows: print("  " + r)
