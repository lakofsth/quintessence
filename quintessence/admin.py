# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.admin — the setup/diagnostic verbs: `qq doctor`, `qq init`, `qq config`.

Ports of qq-legacy's `doctor()`, `qq_init()`, and `qq_config()` (P9, the release-gate legacy
port). NOT L0 write-path territory — none of these touch HEAD content or the git-tree lock;
they probe the environment, scaffold a store, or edit the per-install dotenv config file
(whose path is resolved independently of any store — see Config._bootstrap_config_path).

Departures from the bash bodies, each flagged at its site rather than made silently:
  - `config set` now runs Config.validate_set (D3, built FOR this wiring but never reachable
    from bash): unknown key -> warn-and-proceed; QUINTESSENCE_DIR/QQ_MEMDIR resolving under a
    temp root -> refuse without --force (the 2026-07 config-leak incident class).
  - doctor's embedder/ask probes import quintessence.search/.ask in-process instead of
    shelling out to `python3 -c` (we ARE that interpreter); probe text and degrade messages
    are unchanged, including the same fallback lines if the import itself fails.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Optional

from .atomicio import atomic_write_text
from .config import Config, _is_temp_path

HOME = os.path.expanduser("~")

# The write-path backstop hooks `qq init` installs — byte-identical to qq_init's heredocs
# (these land in USERS' stores; any drift here would fork the deployed hook population).
_PRE_COMMIT_HOOK = """#!/bin/sh
# quintessence write-path backstop: refuse a commit that did not hold the qq lock. qq-write /
# qq finalize export QQ_WRITE_TXN while holding the flock; a raw `git commit` won't have it.
if [ -z "${QQ_WRITE_TXN:-}" ]; then
  # NB: never print the manual override here — an agent blocked by this hook copies whatever
  # the refusal says (the skeleton-key-in-the-error-message failure).
  # The escape hatch is documented in ONBOARDING.md for humans who really need it.
  echo "quintessence: direct commit blocked – shared-state writes must go through qq." >&2
  echo "  add update:    qq update <topic> \\"text\\"" >&2
  echo "  edit a HEAD:   qq rewrite <topic> < new_content" >&2
  echo "  snapshot:      qq finalize <topic>     (docs: ONBOARDING.md)" >&2
  exit 1
fi
exit 0
"""

_PRE_PUSH_HOOK = """#!/bin/sh
# quintessence write-path backstop (push side): refuse a push outside the locked path.
if [ -z "${QQ_WRITE_TXN:-}" ]; then
  # No override in the refusal — see the pre-commit hook's note.
  echo "quintessence: direct push blocked – pushes go through the locked write-path (qq verbs)." >&2
  echo "  docs: ONBOARDING.md" >&2
  exit 1
fi
exit 0
"""

_GITIGNORE = """# runtime sentinels + lock — never tracked
.qq.lock
.checked-*
.hinted-*
.kb-primed-*
.rewake-*
"""


class AdminError(Exception):
    """A refused/failed admin operation. `exit_code` mirrors the legacy case's own exit/return
    status, same contract as write.WriteError."""

    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


# ---- qq doctor ---------------------------------------------------------------------------------
def doctor(config: Config, engine_dir: str) -> tuple[int, str]:
    """Port of qq-legacy `doctor()` — preflight: deps + store + corpus + (optional) embedder +
    ask-completion endpoint. Returns (exit_code, text): 1 iff a REQUIRED dep is missing,
    matching bash's `return 1`; every store/corpus/recall issue stays a ⚠/○ line, exit 0.

    The required-deps list still names bash/flock/jq: the python core doesn't need them, but
    the hook glue shipped beside it does (qq-lib.sh consumers: memory-commit.sh, the audit
    units) and the store hooks are /bin/sh — dropping them from doctor would green-light an
    install where half the plugin surface can't run. Probes the CONFIG-resolved store (same
    view bash's $QDIR gave), not the multi-store walk-up."""
    out: list[str] = []
    req_ok = True
    out.append("qq doctor – environment check")
    out.append("Required:")
    for c in ("bash", "git", "jq", "flock", "python3"):
        if shutil.which(c):
            out.append(f"  ✓ {c}")
        else:
            out.append(f"  ✗ {c} MISSING")
            req_ok = False
    out.append("Store & corpus:")
    qdir = config.get_path("QUINTESSENCE_DIR")
    if os.path.isdir(qdir):
        out.append(f"  ✓ store dir: {qdir}")
    else:
        out.append(f"  ⚠ store dir missing: {qdir}  (run: qq init)")
    if os.path.isdir(os.path.join(qdir, ".git")):
        out.append("  ✓ store is a git repo")
        for h in ("pre-commit", "pre-push"):
            hp = os.path.join(qdir, ".git", "hooks", h)
            if os.access(hp, os.X_OK):
                out.append(f"  ✓ write-path hook: {h}")
            else:
                out.append(f"  ⚠ missing write-path hook: {h}  (run: qq init)")
    else:
        out.append("  ⚠ store is not a git repo – finalize/journal needs git  (run: qq init)")
    # human-3 (2026-07-29 review): doctor used to report all-green with no git author identity
    # configured, and the FIRST write then failed outright (write.py's commit_push has the exact
    # same "tell me who you are"/"empty ident"/user.email detection this mirrors — kept in sync
    # by hand since doctor is a preflight, not a write, and must never attempt a commit itself).
    # A cheap `git config` read, same shape as every other check in this function: warn (⚠),
    # never fail the run (doctor's own contract: required-deps-missing is the only thing that
    # sets req_ok=False / exit 1). Scoped to the STORE, matching how commit_push actually
    # commits — a bare `git config` here would read whatever repo the process's cwd happens to
    # sit in (verification pass, 2026-07-29: an identity-less store probed from inside an
    # unrelated repo that HAS an identity used to print no warning at all, and the very next
    # write then failed with the exact error this check exists to preflight). `-C qdir` needs
    # qdir to actually EXIST (git errors "cannot change to ... No such file or directory"
    # otherwise, which would misreport as "no identity" even when one IS configured globally);
    # pre-`qq init`, with no store directory yet to scope to, `--global` is the closest read of
    # what a freshly-created repo there would actually inherit (never a stray local override
    # from wherever cwd happens to be, which is the same leak this fix closes for the other
    # branch) — this still leaves genuine SYSTEM-scope-only identities under-detected pre-init,
    # a narrower miss than the bug this replaces.
    git_cfg_cmd = (["git", "-C", qdir, "config"] if os.path.isdir(qdir)
                   else ["git", "config", "--global"])
    try:
        git_name = subprocess.run([*git_cfg_cmd, "user.name"],
                                   capture_output=True, text=True).stdout.strip()
        git_email = subprocess.run([*git_cfg_cmd, "user.email"],
                                    capture_output=True, text=True).stdout.strip()
    except OSError:
        git_name = git_email = ""   # git itself missing — already flagged above as a ✗ required dep
    if not git_name or not git_email:
        out.append("  ⚠ git author identity not configured – the first write will fail:")
        out.append("      git config --global user.name  \"Your Name\"")
        out.append("      git config --global user.email you@example.com")
    memdir = config.get_path("QQ_MEMDIR")
    if os.path.isdir(memdir):
        out.append(f"  ✓ memory dir: {memdir}")
    else:
        out.append(f"  ⚠ memory dir missing: {memdir}  (optional; only the T2 xref uses it)")
    kb = config.get_path("QQ_KB_ROOT")
    if os.path.isdir(kb):
        out.append(f"  ✓ corpus root: {kb}")
    else:
        out.append(f"  ⚠ corpus root missing: {kb}  (optional; only semantic recall uses it)")
    out.append("In-system contract:")
    contract_version = config.get_int("QQ_CONTRACT_VERSION")
    cf = os.path.join(engine_dir, "CONTRACT.md")
    if os.path.isfile(cf):
        try:
            m = re.search(r"qq-contract v(\d+)",
                          open(cf, encoding="utf-8", errors="replace").read())
        except OSError:
            m = None
        cv = m.group(1) if m else None
        # bash: [ "${cv:-0}" = "$QQ_CONTRACT_VERSION" ]; the mismatch line renders v${cv:-?}
        if (cv or "0") == str(contract_version):
            out.append(f"  ✓ CONTRACT.md v{cv} matches qq")
        else:
            out.append(f"  ⚠ CONTRACT.md v{cv or '?'} ≠ qq expects v{contract_version} "
                       f"– re-sync the injected contract")
    else:
        out.append(f"  ⚠ no CONTRACT.md beside qq ({cf}) – in-system contract not bundled "
                   f"here (ok if unused)")
    out.append("Semantic recall (optional):")
    # In-process ports of the bash `python3 -c` probes — same output text, same fallback lines
    # on failure (an import error here means THIS package is broken; the legacy fallback text
    # is kept verbatim so doctor's output stays diffable across engines).
    try:
        from .search import SearchIndex
        ok, detail = SearchIndex(config).probe_embedder()
        probe = ("ok::" if ok else "warn::") + detail
    except Exception:
        probe = "warn::could not import quintessence.search (python3?)"
    if probe.startswith("ok::"):
        out.append(f"  ✓ embedder {probe[4:]}")
    else:
        out.append(f"  ⚠ embedder {probe[6:]}  (core works without it; recall degrades to keyword)")
    # qq ask's cited answers additionally need a COMPLETION endpoint (QQ_ASK_ENDPOINTS) — a
    # separate dependency from the embedder above; surface its state so a degraded `ask` is
    # never a surprise. Unset is a deliberate default (R5), not a warning.
    try:
        from .ask import Ask
        from .search import SearchIndex
        eps = config.get("QQ_ASK_ENDPOINTS")
        if not eps:
            askp = "none::"
        else:
            ep = Ask(config, SearchIndex(config)).pick_endpoint()
            askp = (f"ok::{ep}" if ep
                    else f"warn::none of {len(eps)} configured endpoint(s) healthy")
    except Exception:
        askp = "warn::could not import quintessence.ask (python3?)"
    if askp.startswith("none::"):
        out.append("  ○ ask completion: QQ_ASK_ENDPOINTS unset – qq ask returns raw retrieval "
                   "(set it for cited answers)")
    elif askp.startswith("ok::"):
        out.append(f"  ✓ ask completion {askp[4:]}")
    else:
        out.append(f"  ⚠ ask completion {askp[6:]}  (qq ask degrades to raw retrieval)")
    if req_ok:
        out.append("Result: required deps OK.")
        rc = 0
    else:
        out.append("Result: MISSING required deps (see ✗).")
        rc = 1
    return rc, "\n".join(out) + "\n"


# ---- qq config ---------------------------------------------------------------------------------
def _config_file_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return []


def config_get(config: Config, key: str) -> Optional[str]:
    """`qq config get <KEY>` — the FILE's value only (grep ^KEY= | head -1 | cut -d= -f2-):
    deliberately not Config.get, which folds in env + defaults; `get` answers "what does the
    durable file say", the question you ask when debugging why a fresh shell differs."""
    for ln in _config_file_lines(config.config_path):
        if ln.startswith(f"{key}="):
            return ln.split("=", 1)[1]
    return None


def config_set(config: Config, key: str, value: str, *,
               allow_relocate: bool = False, force: bool = False,
               env: Optional[dict] = None) -> tuple[str, list[str]]:
    """`qq config set <KEY> <VALUE>` — rewrite-or-append the dotenv file, guarded. Returns
    (success_message, warnings); refusals raise AdminError(2). Guard order matches bash
    (QQ_SANDBOX, then the relocate keys), with D3's validate_set appended as the third gate:
      1. QQ_SANDBOX set -> refuse: the config file's path resolves independently of the store,
         so a context that fenced only QUINTESSENCE_DIR can still clobber the real user config.
      2. QUINTESSENCE_DIR/QQ_MEMDIR -> refuse without QQ_ALLOW_RELOCATE (env, or
         allow_relocate=True for qq init's own legitimate write).
      3. D3 (NEW here — validate_set existed but bash never called it): unknown key ->
         warn-and-proceed; a guarded path key resolving under a temp root -> refuse without
         `force` (`qq config set --force`, deliberately NOT named in the refusal — the
         config-leak incident was agent-made, and A2 says never print the skeleton key)."""
    environ = os.environ if env is None else env
    f = config.config_path
    if environ.get("QQ_SANDBOX"):
        raise AdminError(
            f"qq config set: refused – QQ_SANDBOX is set, so this context must not mutate the "
            f"global config ({f}).\n"
            f"  set QQ_CONFIG=<scratch> to persist to a throwaway file, or unset QQ_SANDBOX "
            f"to allow.", 2)
    if key in ("QUINTESSENCE_DIR", "QQ_MEMDIR") and not allow_relocate \
            and not environ.get("QQ_ALLOW_RELOCATE"):
        raise AdminError(
            f"qq config set: '{key}' relocates your durable store – refusing without "
            f"QQ_ALLOW_RELOCATE.\n"
            f"  this key controls WHERE your quintessence lives; an unintended change could\n"
            f"  repoint the whole store. To relocate deliberately:\n"
            f"    QQ_ALLOW_RELOCATE=1 qq config set {key} <path>", 2)
    warnings: list[str] = []
    # D3's temp-path refusal defends the DURABLE config file. A QQ_CONFIG that is ITSELF
    # temp-rooted is by definition not durable — it's the sanctioned fenced-sandbox pattern
    # the QQ_SANDBOX refusal above explicitly recommends ("set QQ_CONFIG=<scratch> to persist
    # to a throwaway file"), and every test harness uses it. Guarding writes INTO a scratch
    # file would break that pattern while protecting nothing, so the check self-disarms there.
    if _is_temp_path(config.config_path):
        force = True
    result = config.validate_set(key, value, force=force)
    if not result.ok:
        raise AdminError(f"qq config set: {result.message}", 2)
    if result.message:
        warnings.append(f"qq config set: {result.message}")
    lines = _config_file_lines(f)
    kept = [ln for ln in lines if not ln.startswith(f"{key}=")]
    kept.append(f"{key}={value}")
    atomic_write_text(f, "\n".join(kept) + "\n")
    return f"config: set {key} in {f}", warnings


def config_cmd(config: Config, args: list[str]) -> tuple[int, str, str]:
    """The `qq config [show|get|set|path]` case switch — returns (rc, stdout, stderr)."""
    sub = args[0] if args else "show"
    f = config.config_path
    if sub == "path":
        return 0, f + "\n", ""
    if sub == "show":
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                return 0, fh.read(), ""
        except OSError:
            return 0, f"# no config file at {f} (env + built-in defaults in effect)\n", ""
    if sub == "get":
        if len(args) < 2:
            return 2, "", "usage: qq config get <KEY>\n"
        key = args[1]
        v = config_get(config, key)
        # missing key -> silent exit 1: legacy runs under `set -o pipefail`, so its
        # `grep ^KEY= | head | cut` pipeline reports grep's miss — callers (qq_init's own
        # QQ_MEMDIR comparison) rely on the empty-output convention, scripts on the rc.
        if v is not None:
            return 0, v + "\n", ""
        # ...but empty STDOUT here means only "the file does not set it", and that reads as
        # "not configured" to a human, which is wrong (and dangerously so for a key whose
        # default turns a protection ON — 2026-07-29: this exact output convinced a reviewing
        # session that a populated redact list was absent). If the key resolves to something
        # anyway, say where from — on STDERR, so every existing stdout/rc contract is untouched.
        note = ""
        try:
            effective = config.get(key)
        except KeyError:
            effective = None
            note = (f"qq config get: '{key}' is not in the config file and is not a registered "
                    f"key – check the spelling (`qq config show`, CONFIG.md).\n")
        if effective is not None and effective != "" and effective != []:
            layer = config.raw_source(key)
            where = {"env": "the environment", "default": "the built-in default"}.get(layer, layer)
            # A csv key resolves to a LIST (since the 2026-07-29 round-1 fix), and a bare
            # f-string of one prints Python repr — ['https://claude.ai'] — on a surface a
            # human reads and then pastes into the config file. Render it back in the
            # file's own syntax (2026-07-29 verification round, F4).
            shown = ", ".join(str(x) for x in effective) if isinstance(effective, list) \
                else effective
            note = (f"qq config get: '{key}' is not set in the config file, but it IS in effect "
                    f"from {where}: {shown}\n"
                    f"  (stdout stays empty and the exit code stays 1, for scripts that test "
                    f"whether the FILE sets it.)\n")
        return 1, "", note
    if sub == "set":
        rest = args[1:]
        force = "--force" in rest
        rest = [a for a in rest if a != "--force"]
        if len(rest) < 2:
            return 2, "", "usage: qq config set <KEY> <VALUE>\n"
        key, value = rest[0], " ".join(rest[1:])   # bash: v="$*" — spaces re-joined
        try:
            msg, warnings = config_set(config, key, value, force=force)
        except AdminError as e:
            return e.exit_code, "", str(e) + "\n"
        return 0, msg + "\n", "".join(w + "\n" for w in warnings)
    return 2, "", "usage: qq config [show | get <KEY> | set <KEY> <VALUE> | path]\n"


# ---- qq init -----------------------------------------------------------------------------------
def _install_store_scaffold(d: str) -> None:
    """git repo + write-path backstop hooks + .gitignore — the shared tail of both init modes."""
    try:
        os.makedirs(d, exist_ok=True)
        os.makedirs(os.path.join(d, "journal"), exist_ok=True)
    except OSError:
        raise AdminError(f"qq init: cannot create {d}", 1)
    if not os.path.isdir(os.path.join(d, ".git")):
        r = subprocess.run(["git", "-C", d, "init", "-q"], capture_output=True)
        if r.returncode != 0:
            raise AdminError(f"qq init: git init failed in {d}", 1)
    hd = os.path.join(d, ".git", "hooks")
    for name, body in (("pre-commit", _PRE_COMMIT_HOOK), ("pre-push", _PRE_PUSH_HOOK)):
        hp = os.path.join(hd, name)
        with open(hp, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(hp, 0o755)
    with open(os.path.join(d, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write(_GITIGNORE)


def init(config: Config, args: list[str]) -> tuple[str, str]:
    """Port of qq-legacy `qq_init()` — create an empty store: git repo + the write-path
    backstop hooks. Returns (stdout, stderr); refusals raise AdminError.

    --project: create a PROJECT store at $PWD/.quintessence (or under
    the given project root) and DO NOT record it as the global QUINTESSENCE_DIR — a project
    store is discovered by walk-up from cwd, so pinning the user's global default to it would
    be exactly the footgun this mode avoids. Bare `qq init [dir]` is unchanged: it inits the
    user/global store and records it in the config file ("switch my store to here")."""
    project = False
    if args and args[0] == "--project":
        project = True
        args = args[1:]
    if project:
        d = args[0] if args else os.path.join(os.getcwd(), ".quintessence")
        # Walk-up discovery finds ONLY a directory literally named `.quintessence` — a custom
        # [dir] is therefore the PROJECT ROOT and the store goes at <dir>/.quintessence; a
        # path already ending in /.quintessence is used as-is.
        if os.path.basename(d) != ".quintessence":
            d = os.path.join(d, ".quintessence")
        # The home level IS the user layer: discovery deliberately never treats
        # $HOME/.quintessence as a project store, so creating one would be the
        # undiscoverable-store footgun. Refuse (hard, not a confirm — the caller is usually a
        # script/agent) and point at the verb that owns this level.
        if os.path.realpath(os.path.dirname(d)) == os.path.realpath(HOME):
            raise AdminError(
                "qq init --project: $HOME is the USER store's level – a project store here "
                "would never be discovered.\n"
                "  use 'qq init' for the user store, or run this from inside a project "
                "directory.", 1)
    else:
        d = args[0] if args else config.get_path("QUINTESSENCE_DIR")
    _install_store_scaffold(d)
    out: list[str] = []
    err: list[str] = []
    if project:
        out.append(f"qq init: initialized PROJECT store at {d}")
        out.append("  • git repo + write-path hooks (pre-commit / pre-push) installed")
        out.append("  • NOT recorded in the global config – this store is used automatically whenever qq")
        out.append(f"    runs inside {os.path.dirname(d)} (walk-up discovery); the user store stays your default")
        out.append("  • first HEAD:  qq new <topic>   (run from inside the project)")
    else:
        # bash: `QQ_ALLOW_RELOCATE=1 qq_config set ... >/dev/null || true` — stdout suppressed,
        # stderr passed through, failure never aborts the init (the store itself is already
        # scaffolded; recording it is best-effort). Bash then printed the "recorded" line
        # UNCONDITIONALLY — honest there, since its set could never refuse under
        # ALLOW_RELOCATE; here D3 CAN refuse (a temp-rooted store dir), so the line prints
        # only when the recording actually happened.
        recorded = True
        try:
            config_set(config, "QUINTESSENCE_DIR", d, allow_relocate=True)
        except AdminError as e:
            recorded = False
            err.append(str(e))
        out.append(f"qq init: initialized store at {d}")
        out.append("  • git repo + write-path hooks (pre-commit / pre-push) installed")
        if recorded:
            out.append(f"  • recorded QUINTESSENCE_DIR={d} in {config.config_path}")
        # An explicit QQ_MEMDIR in the environment is the same "switch my store to here"
        # intent as the store dir itself — persist it, or a later fresh shell silently reverts
        # the memory dir to the default (the install is defined in the config file, not the
        # shell environment).
        env_memdir = os.environ.get("QQ_MEMDIR")
        if env_memdir and config_get(config, "QQ_MEMDIR") != env_memdir:
            try:
                config_set(config, "QQ_MEMDIR", env_memdir, allow_relocate=True)
                out.append(f"  • recorded QQ_MEMDIR={env_memdir} in {config.config_path}")
            except AdminError as e:
                err.append(str(e))
        # Legacy's advice line pointed at `qq-write` (its own era's entry point); that binary
        # left the tree with the bash engine, and `qq new` has been the sole creation
        # entrypoint since the carried ruling — the advice follows the ruling.
        out.append("  • first HEAD:  qq new <topic> \"one-line essence\"   (see ONBOARDING.md)")
        if os.getcwd() != HOME and d == config.get_path("QUINTESSENCE_DIR"):
            out.append(f"  • (to make a PROJECT-scoped store in {os.getcwd()} instead, "
                       f"run: qq init --project)")
    return ("\n".join(out) + "\n", "".join(e + "\n" for e in err))
