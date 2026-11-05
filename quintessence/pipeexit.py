# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""How every entry point ends: a reader that closes early is exit 141, quietly.

A shell tool whose reader goes away (`qq digest | head -1`, a hook keeping the first line, a
`grep -q` that stops at its first match) dies of SIGPIPE: exit 141, no message. Python ignores
SIGPIPE and raises BrokenPipeError instead, and WHERE it raises depends on how much of the
output is still in stdout's buffer. A write the pipe cannot take whole returns once its tail fits
the buffer, so a reader that then goes away fails only the interpreter's shutdown flush, outside
every handler: CPython prints `Exception ignored while flushing sys.stdout: BrokenPipeError` and
exits 120. Measured 2026-11-04 with a 64 KiB pipe and a reader that takes one byte: one write of
66,000 bytes returns on 3.13 and 3.14 alike; one of 100,000 to 140,000 bytes raises on 3.13 and
returns on 3.14, whose io.DEFAULT_BUFFER_SIZE is 128 KiB where 3.13's is 8 KiB. qq's menu (about
112 KB) is in that window, which is how CI on 3.14 caught what 3.13 had hidden.

run() therefore flushes stdout itself, inside its own handler, on every way `main` can end — a
return, a `sys.exit`, or any other exception — and on BrokenPipeError points fd 1 at /dev/null
(so the shutdown flush has somewhere harmless to write what is still buffered) and exits 141.
An exception other than BrokenPipeError is re-raised unchanged after the flush, so an entry
point's own handlers and its traceback behave as before.

INVARIANTS.md: `a-reader-that-closes-early-ends-every-entry-point-with-141-and-no-message`.
"""
import os
import sys

EXIT_SIGPIPE = 141   # 128 + SIGPIPE(13): what a shell reports for a process SIGPIPE killed


def _stdout_to_devnull() -> None:
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass   # no stdout fd to repoint (closed or replaced): nothing is left to flush into it


def run(main, *args, **kwargs):
    """Call `main(*args, **kwargs)` and exit with its result, flushing stdout first; a reader that
    closed early makes it exit 141 with nothing on stderr. Never returns."""
    try:
        try:
            code = main(*args, **kwargs)
        except SystemExit as e:
            code = e.code
        finally:
            # On a return, a sys.exit and an exception alike. If main raised and this flush
            # raises BrokenPipeError, that replaces main's exception: the reader is gone, so
            # what main would have reported has nowhere to go on stdout either.
            if sys.stdout is not None:
                sys.stdout.flush()
    except BrokenPipeError:
        _stdout_to_devnull()
        sys.exit(EXIT_SIGPIPE)
    sys.exit(code)
