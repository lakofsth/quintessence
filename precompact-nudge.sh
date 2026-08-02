#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
# PreCompact hook — context compaction is imminent and LOSSY. Nudge the model to capture the
# session's working context into a quintessence HEAD NOW, while it still knows the essence
# (compaction is salvage, ex-post; a HEAD written at peak context is the real save). Reads the
# hook JSON on stdin for .trigger (auto|manual). Silent + fail-open: emits one systemMessage,
# never blocks. Pairs with the SessionStart contract + the Stop finalize-check.
trigger="$(jq -r '.trigger // "auto"' 2>/dev/null)" || trigger="auto"
[ -n "$trigger" ] || trigger="auto"   # empty/malformed payload → "auto"
jq -n --arg t "$trigger" '{systemMessage: ("Context compaction (" + $t + ") is imminent and lossy. If this session did substantive work, capture it into a quintessence HEAD NOW — `qq update <topic> \"<text>\"` then `qq finalize <topic>` (direct Edit/Write under the store is blocked/unsupported — it sits unattributed and gets silently folded into the next legitimate `qq` commit) — before the working context is lost.")}' 2>/dev/null || true
exit 0
