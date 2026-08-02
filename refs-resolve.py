#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""refs-resolve.py — the B3 reality-binding trigger resolver (standalone entry point).

Invoked by:
  - a post-commit hook in a repo you want bound (yours to install; it just shells out here):
        refs-resolve.py commit --repo <repo-toplevel> [--sha <sha>]
    derives {repo, sha, changed_paths}, appends the event to $QQ_STATE_DIR/refs/events.jsonl,
    marks matching file/git refs suspect (the "push" side of reality binding).
  - user-scope systemd .path units — a templated unit (e.g. qq-refs-watch@.path) per
    watched directory:
        refs-resolve.py pathwatch --dir <watched-directory>
  - a one-time operator invocation for the corpus backfill:
        refs-resolve.py corpus-seed
    binds every currently-tracked *.md of every QQ_BIND_CORPUS corpus (idempotent). The
    per-commit corpus rebind needs NO extra verb — `commit` detects a corpus repo itself.
  - the revalidation sweep (an ExecStart of the consistency-audit train, or by hand):
        refs-resolve.py sweep
    re-fingerprints the kinds with no other revalidation path (unit incl. the legacy
    sha256e format migration, port, rfile, git, probe, corpus files, fp-error retry,
    suspect auto-clear); prints its count summary (the audit unit appends it to audit.log).

CONTRACT WITH ITS CALLERS: always exits 0 and prints nothing on success — a binding failure
must never fail a commit or flap a oneshot unit. Errors go to $QQ_STATE_DIR/refs/resolver.log
(one line each). All real logic lives in quintessence.refsresolve; this file is argv + the
never-fail shell.
"""
import os
import sys

ENGINE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, ENGINE)


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in ("commit", "pathwatch", "corpus-seed", "sweep"):
        print("usage: refs-resolve.py commit --repo <path> [--sha <sha>] | "
              "refs-resolve.py pathwatch --dir <path> | refs-resolve.py corpus-seed | "
              "refs-resolve.py sweep",
              file=sys.stderr)
        return 0   # even a usage error must not fail the calling hook
    mode, rest = args[0], args[1:]
    opts = {}
    i = 0
    while i < len(rest):
        if rest[i] in ("--repo", "--sha", "--dir") and i + 1 < len(rest):
            opts[rest[i][2:]] = rest[i + 1]
            i += 2
        else:
            i += 1

    from quintessence.config import Config
    from quintessence.store import Store
    from quintessence import refsresolve

    store = Store(Config())
    if mode == "commit" and opts.get("repo"):
        refsresolve.resolve_commit(store, opts["repo"], opts.get("sha"))
    elif mode == "pathwatch" and opts.get("dir"):
        refsresolve.resolve_pathwatch(store, opts["dir"])
    elif mode == "corpus-seed":
        n = refsresolve.corpus_seed(store)
        print(f"corpus-seed: {n} record(s) bound")   # operator verb — visible, unlike the hooks
    elif mode == "sweep":
        c = refsresolve.sweep(store)
        print("sweep: {swept} swept / {suspected} suspected / {cleared} cleared / "
              "{upgraded} upgraded / {filled} filled / {probe_errors} probe-errors / "
              "{fp_error_left} fp-error left".format(**c))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)   # never fail the caller; details (if reachable) are in resolver.log
