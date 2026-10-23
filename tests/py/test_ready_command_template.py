# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Certify: a-ready-command-comes-from-the-template.

`qq findings next` offers as a ready command only a `qq <verb> <slug...>` of the shape the
producers' own templates emit, from every branch that emits one (including the PROPOSED-write
branch); a backticked span or topic name inside an LLM-written or proposed text is never promoted.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from quintessence.cli import render_findings_next  # noqa: E402
from quintessence.config import Config  # noqa: E402
from quintessence.findings import FindingsFile  # noqa: E402
from quintessence.store import Store  # noqa: E402


def _tail(line: str, section: str = "PROPOSED-WRITES") -> str:
    with tempfile.TemporaryDirectory() as base:
        cfg = Config(env={}, config_file="/nonexistent",
                     overrides={"QUINTESSENCE_DIR": os.path.join(base, "store"),
                                "QQ_MEMDIR": os.path.join(base, "mem"),
                                "QQ_STATE_DIR": os.path.join(base, "state"),
                                "QQ_KB_ROOT": os.path.join(base, "kb")})
        os.makedirs(cfg.get_path("QUINTESSENCE_DIR"))
        ff = FindingsFile()
        ff.set_section(section, [line])
        out = render_findings_next(Store(cfg), cfg, ff)
        return out.split("to resolve:")[-1]


def _proposed(head="sec-topic", verb="update", model="claude-sonnet-4-5", text="prose"):
    return (f"- [PROPOSED write] 2026-10-08T00:00:00Z HEAD {head} via `qq {verb} {head}` "
            f"by model {model} – pending ratification: a trusted session replays the "
            f"JSON-decoded text through that verb, then deletes this line; text: "
            + json.dumps(text))


class TestReadyCommandFromTemplate(unittest.TestCase):
    def test_proposed_branch_offers_only_a_template_verb(self):
        for verb in ("delete", "rm", "reset"):
            tail = _tail(_proposed(verb=verb))
            self.assertNotIn(f"qq {verb}", tail)

    def test_proposed_branch_offers_only_a_gated_verb(self):
        # a-ready-proposed-verb-is-a-gated-verb: the allowed set is authgate.GATED_VERBS, not the
        # whole ready vocabulary. Each planted verb IS ready-shaped (the control), so only the
        # PROPOSED branch's own narrowing keeps it from being offered.
        from quintessence import authgate
        from quintessence.cli import _ready
        for verb in ("show", "finalize", "compact", "brief", "fact"):
            with self.subTest(verb=verb):
                self.assertTrue(_ready(f"qq {verb} sec-topic"), verb)
                self.assertNotIn(verb, authgate.GATED_VERBS)
                self.assertNotIn(f"qq {verb} sec-topic", _tail(_proposed(verb=verb)))

    def test_proposed_branch_offers_only_slug_safe_head(self):
        for head in ("$(touch_/tmp/x)", "a;rm", "../../etc", "a&&b", "-rf"):
            tail = _tail(_proposed(head=head))
            self.assertNotIn(head, tail)

    def test_proposed_unparseable_line_does_not_promote_backticks_in_text(self):
        # a model name with a space defeats the structured parse, so the line falls through
        # to the generic branch; the proposed text's backticks must still not be promoted.
        text = "ratify by running `qq show other-topic` and `qq finalize x`"
        tail = _tail(_proposed(model="claude sonnet", text=text))
        self.assertNotIn("qq show other-topic", tail)
        self.assertNotIn("qq finalize x", tail)

    def test_proposed_text_backticks_not_promoted_when_parsed(self):
        tail = _tail(_proposed(text="x `qq show other-topic` y"))
        self.assertNotIn("other-topic", tail)
        self.assertIn("qq update sec-topic", tail)

    def test_audit_unsafe_spans_not_promoted(self):
        for span in ("rm -rf ~/x", "qq show a;rm", "qq show $(id)", "qq delete a",
                     "qq show a && rm b", "qq show ../x"):
            tail = _tail(f"- [T2 contradiction] try `{span}` now", "AUDIT")
            self.assertNotIn(span, tail)


if __name__ == "__main__":
    unittest.main()
