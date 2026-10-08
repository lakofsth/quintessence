# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.httpdirect — the one HTTP opener for calls that carry store text.

urllib's default opener honours `http_proxy`/`HTTP_PROXY` for every host that `no_proxy` does
not exempt, and on Linux a loopback host is NOT exempt by default: `proxy_bypass("localhost:
11434")` is False with `http_proxy` set and `no_proxy` unset (posture review 2026-10-08, M6).
Every embedding request, endpoint probe and completion call therefore handed HEAD and memory
text to whatever proxy a managed laptop's environment named.

This opener has NO proxy handler, so the request goes to the endpoint the configuration names
and nowhere else. It is deliberately not configurable: the endpoints are the operator's own
model servers, local or on the LAN, and a proxy between the store and its own embedder is never
the intended path. INVARIANTS.md: store-text-leaves-only-for-the-endpoint-named."""
from __future__ import annotations

import urllib.request

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def urlopen(url_or_request, timeout: float | None = None):
    """Drop-in for `urllib.request.urlopen` minus proxy handling. Returns the response as a
    context manager, exactly as the stock call does."""
    return _OPENER.open(url_or_request, timeout=timeout)
