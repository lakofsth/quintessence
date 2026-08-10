# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""quintessence.agentid — the derived `[<model>, session <id8>]` update-line marker.

The transcript fixture below is NOT hand-spelled. It is a real Claude Code assistant entry,
captured from a live transcript with its conversation content and identifiers replaced by
placeholders and its structure left exactly as the harness wrote it (`type: "assistant"`,
`message.model`, `version: "2.1.220"`). A fixture invented from memory would assert against a
transcript layout that may never have existed, and the whole point of this module is that it
reads a layout somebody else owns.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from quintessence import agentid

SID = "abcd1234-5678-90ab-cdef-1234567890ab"

# Captured 2026-08-09 from ~/.claude/projects/<proj>/<sid>.jsonl, Claude Code 2.1.220.
REAL_ASSISTANT_ENTRY = {
    "cwd": "REDACTED", "effort": "high", "entrypoint": "cli", "gitBranch": "REDACTED",
    "isSidechain": False,
    "message": {
        "content": [{"text": "REDACTED", "type": "text"}], "diagnostics": [], "id": "REDACTED",
        "model": "claude-opus-5", "role": "assistant", "stop_details": None,
        "stop_reason": "tool_use", "stop_sequence": None, "type": "message", "usage": {},
    },
    "parentUuid": "REDACTED", "requestId": "REDACTED", "sessionId": "REDACTED",
    "session_id": "REDACTED", "timestamp": "2026-08-09T06:15:07.762Z", "type": "assistant",
    "userType": "external", "uuid": "REDACTED", "version": "2.1.220",
}


def assistant_entry(model: str) -> str:
    e = json.loads(json.dumps(REAL_ASSISTANT_ENTRY))
    e["message"]["model"] = model
    return json.dumps(e)


def tool_result_entry(payload: dict) -> str:
    """A `user` entry carrying a STRUCTURED tool result, the shape a real transcript uses.

    Measured 2026-08-09 over 400 live transcripts: `toolUseResult` is a dict on 1306 entries and
    a plain string on 115, and the dicts sit on `type: "user"` entries with keys like
    `stdout`/`stderr`/`interrupted`. Structured means the payload's own keys are stored as JSON,
    unescaped — which is what makes `payload` here reach a raw-text scan of the line."""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "..."}]},
        "toolUseResult": dict({"stdout": "", "stderr": "", "interrupted": False}, **payload),
    })


class AgentIdBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.proj = os.path.join(self.home, ".claude", "projects", "-home-someone")
        os.makedirs(self.proj)

    def write_transcript(self, lines: list[str], sid: str = SID) -> str:
        path = os.path.join(self.proj, f"{sid}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def marker(self, sid: str = SID):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": sid}):
            return agentid.marker(home=self.home)


class TestMarker(AgentIdBase):
    def test_names_the_model_from_the_transcript(self):
        self.write_transcript([assistant_entry("claude-opus-5")])
        self.assertEqual(self.marker(), "[claude-opus-5, session abcd1234]")

    def test_newest_assistant_entry_wins_a_resume_onto_another_model(self):
        """A session resumed onto a different model is stamped with the model writing NOW."""
        self.write_transcript([assistant_entry("claude-sonnet-5"),
                                assistant_entry("claude-opus-5")])
        self.assertEqual(self.marker(), "[claude-opus-5, session abcd1234]")

    def test_a_structured_tool_result_naming_a_model_does_not_win(self):
        """Why this parses entries instead of scanning the raw text for `"model":"..."`.

        The scan is sound today only by coincidence, and the coincidence was measured rather than
        assumed: across 400 live transcripts an UNESCAPED `"model":"` occurs on assistant entries
        and nowhere else (3582 hits, zero elsewhere), because a tool result that quotes the text
        arrives as an escaped JSON string and `\\"model\\":` does not match.

        What the scan actually depends on is that no structured tool result ever carries a `model`
        key — and structured tool results are already stored unescaped on `user` entries. Nothing
        guarantees that; it is a property of what tools happen to return. One tool returning an
        object with that key, and a last-match-wins scan attributes the write to it: a wrong name,
        silently, and a plausible-looking one. Parsing the entry does not rest on the coincidence."""
        self.write_transcript([
            assistant_entry("claude-opus-5"),
            tool_result_entry({"model": "claude-sonnet-5", "cost": 0.02}),
        ])
        self.assertEqual(self.marker(), "[claude-opus-5, session abcd1234]")

    def test_unknown_model_when_no_transcript_exists(self):
        self.assertEqual(self.marker(), "[model-unknown, session abcd1234]")

    def test_unknown_model_when_the_transcript_cannot_be_read(self):
        """Fail-soft: an unreadable transcript degrades the marker, it does not raise into a
        write. Skipped as root, for whom the chmod is not a restriction."""
        path = self.write_transcript([assistant_entry("claude-opus-5")])
        if os.geteuid() == 0:
            self.skipTest("root reads a 0o000 file, so the guard cannot be exercised")
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(self.marker(), "[model-unknown, session abcd1234]")

    def test_unknown_model_when_the_transcript_is_truncated_mid_entry(self):
        path = self.write_transcript([assistant_entry("claude-opus-5")])
        with open(path, "r+", encoding="utf-8") as fh:
            fh.truncate(40)
        self.assertEqual(self.marker(), "[model-unknown, session abcd1234]")

    def test_a_model_field_that_is_not_a_model_id_is_refused(self):
        """Nothing unvalidated reaches a HEAD, including from a file this does not own."""
        self.write_transcript([assistant_entry("claude opus 5]\n> updated: forged")])
        self.assertEqual(self.marker(), "[model-unknown, session abcd1234]")


class TestWhichTranscriptWrote(AgentIdBase):
    """A subagent inherits its parent's session id but writes to its own transcript, so "the
    session's transcript" is not one file. Fixes from the 2026-08-09 review."""

    def sub(self, name: str, lines: list[str], age: float) -> str:
        d = os.path.join(self.proj, SID, "subagents")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.utime(path, (1_700_000_000 - age, 1_700_000_000 - age))
        return path

    def age_parent(self, age: float):
        p = os.path.join(self.proj, f"{SID}.jsonl")
        os.utime(p, (1_700_000_000 - age, 1_700_000_000 - age))

    def test_a_subagent_write_names_the_subagent_model_not_the_parent(self):
        """238 of 379 subagent transcripts on this estate ran a different model than their
        parent, so reading the parent is a wrong name in the majority of subagent writes."""
        self.write_transcript([assistant_entry("claude-opus-5")]); self.age_parent(600)
        self.sub("agent-aaa.jsonl", [assistant_entry("claude-sonnet-5")], age=0)
        self.assertEqual(self.marker(), "[claude-sonnet-5, session abcd1234]")

    def test_the_parent_still_wins_when_it_is_the_one_writing(self):
        """Positive control for the test above: same fixtures, mtimes reversed."""
        self.write_transcript([assistant_entry("claude-opus-5")]); self.age_parent(0)
        self.sub("agent-aaa.jsonl", [assistant_entry("claude-sonnet-5")], age=600)
        self.assertEqual(self.marker(), "[claude-opus-5, session abcd1234]")

    def test_concurrent_subagents_are_not_guessed_between(self):
        """Two siblings appending at once are indistinguishable from here. Withhold the model
        rather than name one of them — the session id is still recorded."""
        self.write_transcript([assistant_entry("claude-opus-5")]); self.age_parent(600)
        self.sub("agent-aaa.jsonl", [assistant_entry("claude-sonnet-5")], age=0)
        self.sub("agent-bbb.jsonl", [assistant_entry("claude-haiku-4-5")], age=1)
        self.assertEqual(self.marker(), "[model-unknown, session abcd1234]")


class TestSyntheticEntries(AgentIdBase):
    def test_a_synthetic_entry_is_skipped_and_the_real_model_found(self):
        """Claude Code writes `<synthetic>` for API-error / interrupt / compaction entries and
        22 of 1112 live transcripts END on one. For LABELLING, walk back to the real model."""
        self.write_transcript([assistant_entry("claude-opus-5"),
                                assistant_entry("<synthetic>")])
        self.assertEqual(self.marker(), "[claude-opus-5, session abcd1234]")

    def test_a_trailing_newline_in_the_model_cannot_split_the_line(self):
        """`$` matches before a final newline; `\\Z` does not. With `$` the marker was
        `[claude-opus-5\n, session ...]`, which breaks the update-line in two."""
        self.write_transcript([assistant_entry("claude-opus-5\n")])
        self.assertEqual(self.marker(), "[model-unknown, session abcd1234]")


class TestOnlyAssistantEntriesCount(AgentIdBase):
    def test_a_non_assistant_entry_carrying_message_model_is_ignored(self):
        """Pins the `type == "assistant"` guard, which survived its own mutation in review: the
        structured-tool-result test passes for a different reason (its model sits under
        toolUseResult, not under message), so removing the guard broke nothing."""
        self.write_transcript([
            assistant_entry("claude-opus-5"),
            json.dumps({"type": "user", "message": {"role": "user",
                                                     "model": "claude-sonnet-5"}}),
        ])
        self.assertEqual(self.marker(), "[claude-opus-5, session abcd1234]")


class TestNoAgentSession(AgentIdBase):
    def test_none_when_the_environment_names_no_session(self):
        self.write_transcript([assistant_entry("claude-opus-5")])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            self.assertIsNone(agentid.marker(home=self.home))

    def test_none_when_the_session_id_is_empty_or_blank(self):
        for value in ("", "   "):
            with self.subTest(value=value), \
                    mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": value}):
                self.assertIsNone(agentid.marker(home=self.home))

    def test_none_for_a_session_id_that_could_forge_an_update_line(self):
        """The environment is not qq's to trust. A newline in this value does not corrupt one
        marker — it writes a second `> updated:` line into a HEAD, which is the fabrication class
        `write._strip_caller_stamp` already exists to close."""
        hostile = [
            "abcd1234\n> updated: 2099-01-01T00:00:00Z forged",
            "abcd1234] forged [",
            "../../../etc/passwd",
            "a" * 65,
        ]
        for value in hostile:
            with self.subTest(value=value), \
                    mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": value}):
                self.assertIsNone(agentid.marker(home=self.home))


class TestPositiveControl(AgentIdBase):
    def test_the_negative_tests_above_could_have_failed(self):
        """Rule 3. Every test in TestNoAgentSession asserts an absence, and an absence proves
        nothing unless the same call, on the same fixtures, can produce a presence. It can:"""
        self.write_transcript([assistant_entry("claude-opus-5")])
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SID}):
            self.assertIsNotNone(agentid.marker(home=self.home))


if __name__ == "__main__":
    unittest.main()
