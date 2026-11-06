#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Retained by fix-review 20261105T123503Z (report: a private review record).
# Not a gate: tests/run.sh does not run it. Usage: bash tests/review-instruments/review-20261105-pipeexit-other-errors-probe.sh ENGINE [PYTHON]
# Runs quintessence.pipeexit.run over mains that fail in ways OTHER than a closed stdout reader,
# and prints exit status and the last stderr line for each:
#   other-fd-epipe   main writes to a pipe whose reader is closed (not stdout)  -> 141, silent
#   stdout-enospc    stdout is /dev/full, main returns 0                         -> 1, OSError traceback
#   keyboardint      main raises KeyboardInterrupt                               -> 130-ish/1, traceback
#   stderr-closed    stdout to a full reader, stderr into a closed reader, main writes to stderr
set -u
ENGINE="$(cd "${1:-.}" && pwd)"; PY="${2:-python3}"; S="$(mktemp -d)"; trap 'rm -rf "$S"' EXIT
mk() { printf 'import os, sys\nsys.path.insert(0, %s)\nfrom quintessence import pipeexit\ndef main():\n    %s\npipeexit.run(main)\n' "'$ENGINE'" "$2" > "$S/$1.py"; }
mk other-fd-epipe "r, w = os.pipe(); os.close(r); os.write(w, b'x'); return 0"
mk stdout-enospc  "sys.stdout.write('x' * 10); return 0"
mk keyboardint    "raise KeyboardInterrupt"
mk stderr-closed  "import time; time.sleep(0.3); sys.stderr.write('e' * 200000); return 0"
show() { printf '%-15s exit=%-4s stderr_last=%s\n' "$1" "$2" "$(tail -1 "$S/err" | cut -c1-90)"; }
"$PY" "$S/other-fd-epipe.py" 2>"$S/err"; show other-fd-epipe $?
"$PY" "$S/stdout-enospc.py" >/dev/full 2>"$S/err"; show stdout-enospc $?
"$PY" "$S/keyboardint.py" 2>"$S/err"; show keyboardint $?
bash -c "\"$PY\" \"$S/stderr-closed.py\" 2>&1 >/dev/null | head -c1 >/dev/null; echo \${PIPESTATUS[0]} > \"$S/code\"" 2>"$S/err"; show stderr-closed "$(cat "$S/code")"
