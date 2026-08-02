# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.remotepolicy — L1: the read-deny policy for the HOSTED remote face.

Design source: the `qq-remote-connector` HEAD, decisions D3 and D10.

D3 named the blocking dependency for the whole remote face: today `filter_redacted` lives ONLY
in `ask.py`, so `search` and `brief` return UNREDACTED results. Nothing may be exposed to a
hosted surface until a deny policy covers EVERY remote read verb. This module is that policy,
hoisted out of `ask` so one object can gate all of them.

TWO ORTHOGONAL AXES (D10, Thomas's ruling — conflating them was the flaw in the first proposal):

  AXIS A — WITHHELD TOPICS (`QQ_REDACT_FILE`, the existing redact-slugs list).
      Model-dependent. A slug is on this list because injecting it into certain readers'
      involuntary recall can cause a silent downgrade; the risk exists only for those readers,
      not for every model. This axis is therefore LIFTABLE: it does not describe data that must
      not leave the box, it describes data that a limited reader profile should not receive
      unasked.

  AXIS B — NEVER-SHARED (`QQ_REMOTE_DENY_FILE`, new). Content that must not leave the box into a
      hosted context AT ALL, independent of which model is asking. This axis is NEVER liftable on
      any profile — no credential, no flag, no env var relaxes it.

THE PROFILE IS SELECTED BY CREDENTIAL, NOT BY FLAG (D10). The remote face's bearer token maps to
a profile: the default token serves PROFILE_STANDARD (axis A + axis B); a second, deliberately
presented token serves PROFILE_WIDE (axis B only, axis A lifted — for when the operator knows the
surface is not serving a limited reader). Which credential you present IS the switch, so relaxing
axis A takes a distinct secret rather than a misconfiguration — the same reasoning D4 applied to
the write path (structure is the boundary; a runtime flag is one bad env var from exposure).

FAIL-CLOSED, LOUDLY. The never-shared list (axis B) MISSING or UNREADABLE denies EVERYTHING (and
the server refuses to start — see qq-remote-mcp), always: absent = misconfiguration. The
withheld-topics file (axis A) follows the same rule when the operator has EXPLICITLY configured its
path (via env, config file, or override) — they asked for it, so silently ignoring it would be
worse. But when axis A is left at its built-in default and no file exists there, the list is
treated as empty: nothing withheld on that axis, and the server starts. This lets a fresh install
run the remote interface without creating a file for a feature the operator may not know exists.
An EMPTY BUT PRESENT file on either axis is a different thing: it is the operator explicitly
stating "this list is empty", and it allows.

Matching semantics are a deliberate re-implementation of `Ask._slug_redacted` rather than a call
into it (that method is private to the ask path, which has its own parity test suite). The two
are pinned together by tests/py/test_remote_policy.py::test_matcher_parity_with_ask, so a change
to either without the other fails the build.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional, Sequence

PROFILE_STANDARD = "standard"  # axis A + axis B  — the default credential
PROFILE_WIDE = "wide"          # axis B only      — axis A lifted by a distinct credential
PROFILES = (PROFILE_STANDARD, PROFILE_WIDE)

# A topic name that is safe to hand to the `qq` CLI as a positional argument. Anchored, no
# leading dash (which argparse would read as a flag), no path separators, no shell metacharacters
# — the remote face shells out to `qq brief <topic>` with an argument LIST (so there is no shell
# to inject into), but a topic like "--help" or "../../etc/passwd" would still be a bug.
_SAFE_TOPIC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_CONTROL_CHAR = re.compile(r"[\x00-\x1f\x7f]")

MAX_TOPIC_LEN = 128


class DenyListUnavailable(RuntimeError):
    """A deny-list file is missing or unreadable — the policy fails closed and denies all."""


def is_safe_topic(topic: str) -> bool:
    return bool(topic) and len(topic) <= MAX_TOPIC_LEN and bool(_SAFE_TOPIC.match(topic))


def read_entries(path: str) -> Optional[list[str]]:
    """Parse a deny-list file: one slug per line, '#' starts a comment, blanks skipped.

    Returns None — NOT an empty list — when the file cannot be read. The caller must treat that
    as deny-all; conflating "unreadable" with "empty" is precisely the fail-open bug this module
    exists to prevent.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    out: list[str] = []
    for ln in raw:
        ln = re.sub(r"\s+#.*$", "", ln).strip()
        if ln and not ln.startswith("#"):
            if _CONTROL_CHAR.search(ln):
                return None
            out.append(ln)
    return out


def slug_matches(path: str, entries: Sequence[str]) -> bool:
    """Does `path` match any deny entry? Semantics ported from Ask._slug_redacted (parity-tested):
    basename with a trailing '.md' stripped; a trailing '*' on an entry is a prefix match (and
    also matches the prefix appearing anywhere in the full path); otherwise exact basename match
    or a plain substring hit on the full path."""
    base = os.path.basename(path)
    if base.endswith(".md"):
        base = base[:-3]
    for e in entries:
        if e.endswith("*"):
            pre = e[:-1]
            if base.startswith(pre) or pre in path:
                return True
        elif base == e or e in path:
            return True
    return False


class RemotePolicy:
    """The deny policy for one remote request, at one profile.

    Cheap to construct per request (two small file reads) — deliberately NOT cached, so editing a
    deny list takes effect on the next call rather than at the next restart. A deny list you have
    to remember to reload is a deny list that will one day be stale at the wrong moment.
    """

    def __init__(self, config, profile: str = PROFILE_STANDARD):
        if profile not in PROFILES:
            raise ValueError(f"unknown remote profile: {profile!r}")
        self.profile = profile
        self.crown_file = config.get_path("QQ_REMOTE_DENY_FILE")
        self.withheld_file = config.get_path("QQ_REDACT_FILE")
        self._withheld_explicit = config.raw_source("QQ_REDACT_FILE") != "default"

    def entries(self) -> list[str]:
        """The deny entries in force for this profile.

        Raises DenyListUnavailable if the never-shared list is missing or unreadable (always), or
        if the withheld-topics file is unreadable when present, or missing when explicitly
        configured (via env, config file, or constructor override).
        A withheld-topics file left at its built-in
        default that does not exist is treated as an empty list — nothing withheld on that axis —
        so a fresh install can start without creating a file for a feature the operator may not use.
        """
        crown = read_entries(self.crown_file)
        withheld = read_entries(self.withheld_file)
        if crown is None:
            if os.path.exists(self.crown_file):
                raise DenyListUnavailable(
                    f"never-shared list exists but cannot be read: {self.crown_file}")
            raise DenyListUnavailable(
                f"never-shared list not found: {self.crown_file} – "
                f"the remote interface requires this file before it will serve "
                f"anything; an empty file means nothing is barred")
        if withheld is None:
            if self._withheld_explicit:
                if os.path.exists(self.withheld_file):
                    raise DenyListUnavailable(
                        f"withheld-topics list exists but cannot be read: {self.withheld_file}")
                raise DenyListUnavailable(
                    f"withheld-topics list not found: {self.withheld_file}")
            if os.path.exists(self.withheld_file):
                raise DenyListUnavailable(
                    f"withheld-topics list exists but cannot be read: {self.withheld_file}")
            withheld = []
        if self.profile == PROFILE_WIDE:
            return list(crown)  # axis A lifted — axis B is never liftable
        return [*crown, *withheld]

    def denies(self, path: str) -> bool:
        """True if `path` must not be served on this profile. Fails CLOSED: an unavailable deny
        list denies everything rather than serving it."""
        try:
            return slug_matches(path, self.entries())
        except DenyListUnavailable:
            return True

    def topic_denied(self, topic: str) -> bool:
        """Deny check for a bare topic name (the `brief` verb), matched as if it were <topic>.md.
        An unsafe topic name is denied outright rather than passed to the CLI."""
        if not is_safe_topic(topic):
            return True
        return self.denies(f"{topic}.md")

    def filter_hits(self, hits: Iterable[dict]) -> list[dict]:
        """Drop denied hits from a search result set. Fails closed to an EMPTY result set."""
        try:
            entries = self.entries()
        except DenyListUnavailable:
            return []
        return [h for h in hits if h.get("path") and not slug_matches(h["path"], entries)]

    def preflight(self) -> None:
        """Raise DenyListUnavailable if the policy cannot be evaluated cleanly. The never-shared
        list must always be present and readable; the withheld-topics list must be readable when
        present, and must exist when explicitly configured. Called at server startup so a
        misconfigured deploy fails to boot LOUDLY instead of silently denying every request (which
        is safe, but looks like a broken store rather than a broken config)."""
        self.entries()
