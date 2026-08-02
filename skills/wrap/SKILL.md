---
name: wrap
description: Session-end loose-ends sweep – finalize HEADs, run the consistency check, verify/commit the session's work, optional doc backfill, and a clean-close summary. Use when the user says /wrap, asks to "wrap up", close out, or end the session cleanly.
---

# Wrap – session-close sweep

Pairs with the `/quintessence` capture skill: where quintessence writes the continuity HEADs *as
you work*, `/wrap` is the end-of-session sweep that finalizes them and ties off loose ends. Drive
the store through `qq <verb>` on PATH (never edit the store or run git in it directly). Run the
steps, then report concisely.

1. **Finalize HEADs** – `qq finalize <topic>` (alias: `checkpoint`) for each HEAD worked this
   session, which snapshots it to the journal. If unsure which, check `qq digest` or recent
   activity.
2. **Consistency** – run `qq check`; surface any non-trivial findings (ignore known, intentional
   to-write `[[link]]` markers – a link to a HEAD you mean to write later is not an error).
   If findings are pending, resolve at least one before closing; `qq findings next` gives
   one finding with its context and resolution commands. Otherwise record an explicit one-line
   defer in the summary. An untouched finding re-presents every session until acted on.
3. **Commits** – `git status --short` in the repos you touched this session. If the deployment
   defines a standard repo set, also check those: `qq config get QQ_WRAP_REPOS` returns a
   colon-separated, PATH-style list of repo paths (empty if unset, in which case just use the
   repos you touched). Commit THIS session's work with clear messages. Uncommitted changes you
   did not author this session: flag them, do not auto-commit – surface them for the user's
   call.
4. **Doc backfill** – if substantive work happened that the project's docs should record, update
   or note the relevant doc, and catch anywhere a session ended abruptly and left something
   unwritten. A deployment may point at its docs tree via `qq config get QQ_WRAP_DOCS`.
5. **Summary** – a tight clean-close report: what's committed and finalized, any flagged items
   needing the user's decision, and any genuinely-open loops. State those plainly, and do not
   pester to keep finished threads open; closing done threads is good hygiene. Don't manufacture
   follow-ups.

**Local enrichment (optional):** `qq config get QQ_WRAP_EXTRA` – if it returns a path to an
existing file, read it and fold its deployment-specific close-out guidance into the sweep above.

Keep it brief and honest.
