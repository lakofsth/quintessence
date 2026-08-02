# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.storepath — resolve the ORDERED SEARCH PATH of stores.

Today quintessence resolves exactly one store (Config -> QUINTESSENCE_DIR, single dir). This
module adds the resolver that turns cwd + environment into an ordered list of store locations,
most-specific first (project store, then user store) — mirroring Claude Code's own CLAUDE.md
hierarchy.

P1 SCOPE: this is a PURE resolver only. Nothing here is wired into the `qq` entry point yet, so
importing/constructing a StorePath cannot change any command's behaviour. The composition of
reads/writes across the path is P2+. The point of landing it standalone is to pin the discovery
+ precedence rules under unit test before anything depends on them.

The two frozen invariants this resolver must honour:
  * DEGENERATE CASE = byte-identical. Run from a dir with no project store and no explicit
    QUINTESSENCE_DIR, resolve_store_path returns a one-element path == today's single store — so
    an existing install on the default path behaves exactly as it did before.
  * AN ENV/CLI QUINTESSENCE_DIR STILL PINS. If QUINTESSENCE_DIR comes from a constructor override
    or the process environment (a deliberate one-off "use THIS store"), discovery is skipped and
    the path is that one store, flagged explicit=True. A value recorded in the config FILE is NOT
    a pin — `qq init` writes the user-store location there, and treating that as a pin would
    silently disable per-project discovery for every mainstream user who ever ran `qq init`. A
    file-recorded (or defaulted) QUINTESSENCE_DIR just relocates the user store; discovery runs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Mapping, Optional

from .config import Config

# The project-store directory name, discovered by walking up from cwd (git-style) or found under
# $CLAUDE_PROJECT_DIR. A single root: HEADs at its top level, journal/ and memory/ beneath it.
PROJECT_STORE_DIRNAME = ".quintessence"


@dataclass(frozen=True)
class StoreLocation:
    """One store on the search path. `qdir` is the HEAD-tree root (equivalent to a resolved
    QUINTESSENCE_DIR). `kind` is 'project' | 'user' | 'explicit' — used for the recall/menu
    provenance tag (P2) and to answer "am I writing to a project store" (P3)."""
    qdir: str
    kind: str

    @property
    def memdir(self) -> str:
        """Where this store's atomic-fact memory lives. A project store keeps memory under its own
        single root (<qdir>/memory); the user/explicit store keeps the split top-level layout the
        Config registry defines (QQ_MEMDIR, default ~/.quintessence-memory) — resolved by the
        caller, not derivable from qdir, so this property only answers the PROJECT convention.
        For user/explicit stores the caller must use Config.get_path('QQ_MEMDIR') instead."""
        return os.path.join(self.qdir, "memory")


@dataclass(frozen=True)
class StorePath:
    """An ordered search path of stores, MOST-SPECIFIC FIRST. `path[0]` is the write target."""
    path: List[StoreLocation]
    explicit: bool = False   # True iff QUINTESSENCE_DIR was set explicitly (discovery skipped)

    @property
    def write_target(self) -> StoreLocation:
        return self.path[0]

    @property
    def qdirs(self) -> List[str]:
        return [loc.qdir for loc in self.path]

    def __post_init__(self):
        if not self.path:
            raise ValueError("a StorePath must contain at least the user store")


def _quintessence_dir_is_env_pinned(config: Config) -> bool:
    """True iff QUINTESSENCE_DIR comes from a constructor override or the environment — a
    deliberate one-off pin that skips project discovery. A value from the
    config FILE or the registry default is NOT a pin: it merely locates the user store (`qq init`
    records the user-store path in the file, and that must not disable per-project layering)."""
    return config.raw_source("QUINTESSENCE_DIR") in ("override", "env")


def _find_project_store(cwd: str, env: Mapping[str, str], home: str,
                        user_qdir: str) -> Optional[str]:
    """Locate a project store, or None. Precedence:
      1. $CLAUDE_PROJECT_DIR/.quintessence if CLAUDE_PROJECT_DIR is set AND that dir exists.
      2. else walk UP from cwd looking for a `.quintessence/` directory, stopping BELOW `home`
         (and at the filesystem root for a cwd outside home).
    Two exclusions, applied to every candidate however it was found:
      * one that resolves to the user store's own qdir (a project store is never the user store);
      * one whose project ROOT is `home` itself — the home level IS the user layer (exactly as in
        the CLAUDE.md hierarchy this mirrors), so `$HOME/.quintessence` is never a project store.
        A stray dir there (rsync, unzip, some other tool) is thus inert instead of silently
        shadowing every $HOME-rooted invocation; `qq init --project` refuses to create one.
    Together these make the $HOME run degenerate BY CONSTRUCTION."""
    user_real = os.path.realpath(user_qdir)
    home_real = os.path.realpath(home)

    def _accept(candidate: str) -> Optional[str]:
        if not os.path.isdir(candidate):
            return None
        if os.path.realpath(candidate) == user_real:
            return None
        if os.path.realpath(os.path.dirname(candidate)) == home_real:
            return None       # project root == $HOME: the home level is the user layer
        return candidate

    cpd = env.get("CLAUDE_PROJECT_DIR")
    if cpd:
        hit = _accept(os.path.join(cpd, PROJECT_STORE_DIRNAME))
        if hit:
            return hit

    # Walk up from cwd. Stop BELOW home (its .quintessence is excluded above anyway) or at the
    # filesystem root.
    cur = os.path.realpath(cwd)
    while True:
        if cur == home_real:
            break
        hit = _accept(os.path.join(cur, PROJECT_STORE_DIRNAME))
        if hit:
            return hit
        parent = os.path.dirname(cur)
        if parent == cur:      # filesystem root
            break
        cur = parent
    return None


def resolve_store_path(cwd: Optional[str] = None,
                       env: Optional[Mapping[str, str]] = None,
                       config: Optional[Config] = None,
                       home: Optional[str] = None) -> StorePath:
    """Resolve the ordered store search path. PURE except for os.path.isdir probes.

    args default to the live process (cwd, os.environ, a fresh Config, ~) so a caller can just
    call resolve_store_path(); tests inject a synthesized env/cwd/home and a Config built over a
    fake env to exercise every branch hermetically.
    """
    cwd = cwd if cwd is not None else os.getcwd()
    env = env if env is not None else os.environ
    config = config if config is not None else Config(env=env)
    home = home if home is not None else os.path.expanduser("~")

    user_qdir = config.get_path("QUINTESSENCE_DIR")

    # Invariant 3: an env/CLI QUINTESSENCE_DIR pins to that one store; discovery is skipped.
    # (A file-recorded value is only the user-store location — it flows into user_qdir above and
    # discovery still runs.)
    if _quintessence_dir_is_env_pinned(config):
        return StorePath([StoreLocation(user_qdir, "explicit")], explicit=True)

    locations: List[StoreLocation] = []
    project = _find_project_store(cwd, env, home, user_qdir)
    if project is not None:
        locations.append(StoreLocation(project, "project"))
    locations.append(StoreLocation(user_qdir, "user"))

    # De-dup while preserving order (defensive: the _find_project_store guard already excludes the
    # user store, but a symlink farm could still surface the same realpath twice).
    seen = set()
    deduped: List[StoreLocation] = []
    for loc in locations:
        key = os.path.realpath(loc.qdir)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(loc)

    return StorePath(deduped)
