# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence — clean-room python package for the quintessence session-continuity tool.

The package is layered: L0 store / L1 domain / L2 services / L3 surfaces. It currently holds
L0 (read side) + L1: Config, the store locator + state lock, Head/MemoryFact parsers,
SlugResolver, and Finding records + renderer. Stdlib-only, python 3.11+.
"""
from __future__ import annotations

__all__ = ["config", "store", "heads", "memory", "slugs", "findings"]
