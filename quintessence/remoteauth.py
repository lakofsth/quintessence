# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""remoteauth — bearer-token admission shared by every hosted MCP face.

MOVED HERE VERBATIM from `qq-remote-mcp`, the store face (read verbs over the continuity
store), so that any further hosted face — a separate PROCESS, so that neither can serve the
other's verbs whatever its config says — imports this rather than growing a second admission
check: two copies of a token comparison drift, and the drift is silent because each copy has
its own passing tests. One implementation.

The functions below are unchanged from their original site. What is new is only that the audit
prefix is now a variable: each face sets `AUDIT_PREFIX` at import so its journald lines say
which face admitted the request.
"""
import contextvars
import hmac
import os
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)) + "/..")
from quintessence.remotepolicy import (  # noqa: E402
    PROFILE_STANDARD,
    PROFILE_WIDE,
    PROFILES,
)

# Set by each face at import ("qq-remote" for the store face). It is the only thing in a journald
# line that says WHICH hosted surface a request reached, when more than one writes the journal.
AUDIT_PREFIX = "qq-remote"


# The profile for the request currently being served, set by the auth middleware from the bearer
# token. A ContextVar (not a global) because the ASGI app is concurrent: two surfaces may be
# mid-call at once and must not see each other's profile.
_profile: contextvars.ContextVar[str] = contextvars.ContextVar("qq_remote_profile",
                                                               default=PROFILE_STANDARD)


def audit(event: str, **fields) -> None:
    """One line per call to stderr -> journald. A hosted face is a surface where the request did
    not originate from the operator at a keyboard, so every call is logged: which face
    (AUDIT_PREFIX), who (profile), what verb, and how long (D5's latency measurement). Verbs
    that can withhold content log that too."""
    kv = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"[{AUDIT_PREFIX}] {event} {kv}", file=sys.stderr, flush=True)


# ---- auth: the bearer token IS the profile selector (D10) ----------------------------------
def load_tokens(path: str) -> dict[str, str]:
    """Parse the token file: lines of `<profile> <token>`, '#' comments, blanks skipped.

    Refuses (raises) rather than degrades: a token file that is group/other-readable, missing a
    profile, carrying a short token, or reusing the same secret for both profiles is a
    misconfiguration that would quietly weaken the boundary, so it stops the server instead. An
    ABSENT or unreadable token file is the same class of thing (nothing to auth against is not
    a green light) and gets the same worded refusal, not a raw traceback.
    """
    try:
        st = os.stat(path)
    except OSError as e:
        raise SystemExit(f"[qq-remote] refusing to start: can't read {path} "
                         f"({e.strerror or e}) — create it (one `<profile> <token>` line per "
                         f"profile) or fix its permissions")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit(f"[qq-remote] refusing to start: {path} is group/other-accessible "
                         f"(mode {stat.S_IMODE(st.st_mode):04o}); chmod 600 it")
    tokens: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = list(f)
    except OSError as e:
        raise SystemExit(f"[qq-remote] refusing to start: can't read {path} ({e.strerror or e})")
    for ln in lines:
        ln = ln.split("#", 1)[0].strip()
        if not ln:
            continue
        parts = ln.split()
        if len(parts) != 2 or parts[0] not in PROFILES:
            raise SystemExit(f"[qq-remote] refusing to start: bad line in {path} "
                             f"(want `<{'|'.join(PROFILES)}> <token>`)")
        tokens[parts[0]] = parts[1]
    for prof in PROFILES:
        if prof not in tokens:
            raise SystemExit(f"[qq-remote] refusing to start: no `{prof}` token in {path}")
        if len(tokens[prof]) < 32:
            raise SystemExit(f"[qq-remote] refusing to start: `{prof}` token is shorter than 32 "
                             f"chars — generate with `openssl rand -hex 32`")
    if hmac.compare_digest(tokens[PROFILE_STANDARD], tokens[PROFILE_WIDE]):
        raise SystemExit("[qq-remote] refusing to start: standard and wide tokens are identical — "
                         "the wide profile would then be reachable by the standard credential, "
                         "which is the entire boundary D10 puts between them")
    return tokens


def profile_for(header: str | None, tokens: dict[str, str]) -> str | None:
    """Constant-time match of a bearer header to a profile. None = reject."""
    if not header or not header.startswith("Bearer "):
        return None
    presented = header[len("Bearer "):].strip()
    match = None
    for prof in PROFILES:  # compare against BOTH, always, so timing can't reveal which one hit
        if hmac.compare_digest(presented, tokens[prof]):
            match = prof
    return match


def auth_wrapper(app, tokens: dict[str, str]):
    """Pure-ASGI gate in front of the MCP app. The lifespan scope passes through untouched (the
    inner app's session manager needs it); every HTTP request must carry a recognised bearer
    token, and the token it carries selects the deny profile for that call; any other scope type
    is closed. INVARIANTS.md: the-remote-face-names-only-what-policy-allows."""
    async def wrapped(scope, receive, send):
        kind = scope.get("type")
        if kind == "lifespan":
            return await app(scope, receive, send)
        if kind != "http":
            # Only HTTP carries the bearer this gate checks. A websocket (or any future scope
            # type) used to pass through unauthenticated, safe only because no such route
            # existed (posture review 2026-10-08, L4). Close it; never forward it.
            audit("reject", reason=f"non-http-scope:{kind}", path=scope.get("path", ""))
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        prof = profile_for(headers.get("authorization"), tokens)
        if prof is None:
            audit("reject", reason="bad-or-missing-bearer", path=scope.get("path", ""))
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain"),
                                    (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": b"unauthorized\n"})
            return
        token = _profile.set(prof)
        try:
            return await app(scope, receive, send)
        finally:
            _profile.reset(token)
    return wrapped
