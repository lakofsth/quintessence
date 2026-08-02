# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.authgate (the AUTHORING GATE — write-side model trust) and its
quintessence.write integration: transcript resolution + last-known-model detection (including
a live cross-check against the bash original, qq-redact.sh's qq_model_mode, so the two
implementations can never silently diverge), the slug matcher, the gate decision matrix, the
PROPOSED-WRITES queue append + Finding round-trip, and — through the write verbs — that a
diverted write never touches the HEAD/git while every REFUSAL path stays identical for gated
slugs too. End-to-end byte-parity through the real `qq` binary is tests/test-authgate.sh."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence import authgate
from quintessence import write as w
from quintessence.config import Config
from quintessence.findings import FindingsFile, parse_line
from quintessence.store import Store

ENGINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OPUS = "claude-opus-4-6"
FABLE = "acme-gated-5"
SONNET = "claude-sonnet-4-5"


def cfg(base: str, env: dict | None = None, **overrides) -> Config:
    over = {"QUINTESSENCE_DIR": os.path.join(base, "store"),
            "QQ_MEMDIR": os.path.join(base, "mem"),
            "QQ_STATE_DIR": os.path.join(base, "state")}
    over.update(overrides)
    return Config(env=env or {}, config_file="/nonexistent", overrides=over)


def make_store(base: str, env: dict | None = None, **overrides) -> Store:
    c = cfg(base, env=env, **overrides)
    qdir = c.get_path("QUINTESSENCE_DIR")
    os.makedirs(qdir, exist_ok=True)
    subprocess.run(["git", "init", "-q", qdir], check=True)
    subprocess.run(["git", "-C", qdir, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", qdir, "config", "user.name", "t"], check=True)
    return Store(c)


def transcript(base: str, model: str, name: str = "t.jsonl") -> str:
    path = os.path.join(base, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"type":"user","message":{"role":"user"}}\n')
        fh.write(json.dumps({"message": {"model": model, "role": "assistant"}}) + "\n")
    return path


def read_head(store: Store, slug: str) -> str:
    with open(store.head_path(slug), encoding="utf-8") as f:
        return f.read()


def proposed_lines(store: Store) -> list:
    path = os.path.join(store.config.get_path("QQ_STATE_DIR"), "pending-findings.md")
    try:
        with open(path, encoding="utf-8") as fh:
            return FindingsFile.parse(fh.read()).get_section(authgate.SECTION)
    except OSError:
        return []


# ---- model detection -----------------------------------------------------------------------
class TestModelIdentity(unittest.TestCase):
    def test_last_model_line_wins(self):
        with tempfile.TemporaryDirectory() as base:
            tp = os.path.join(base, "t.jsonl")
            with open(tp, "w") as fh:
                fh.write(json.dumps({"message": {"model": OPUS}}) + "\n")
                fh.write(json.dumps({"message": {"model": SONNET}}) + "\n")
                fh.write('{"type":"user","no_model_here":true}\n')
            c = cfg(base, QQ_MODEL_TRANSCRIPT=tp)
            self.assertEqual(authgate.model_identity(c), SONNET)

    def test_no_transcript_is_empty(self):
        with tempfile.TemporaryDirectory() as base:
            c = cfg(base, QQ_MODEL_TRANSCRIPT=os.path.join(base, "gone.jsonl"))
            self.assertEqual(authgate.model_identity(c), "")

    def test_transcript_without_model_line_is_empty(self):
        with tempfile.TemporaryDirectory() as base:
            tp = os.path.join(base, "t.jsonl")
            with open(tp, "w") as fh:
                fh.write('{"type":"user"}\n')
            self.assertEqual(authgate.model_identity(cfg(base, QQ_MODEL_TRANSCRIPT=tp)), "")

    def test_session_id_derivation_and_override_precedence(self):
        with tempfile.TemporaryDirectory() as base:
            sid = "aaaa-bbbb"
            proj = os.path.join(base, "home", ".claude", "projects", "-some-dir")
            os.makedirs(proj)
            with open(os.path.join(proj, sid + ".jsonl"), "w") as fh:
                fh.write(json.dumps({"message": {"model": OPUS}}) + "\n")
            with mock.patch.dict(os.environ, {"HOME": os.path.join(base, "home")}):
                c = cfg(base, env={"CLAUDE_CODE_SESSION_ID": sid})
                self.assertEqual(authgate.model_identity(c), OPUS)
                # an explicit QQ_MODEL_TRANSCRIPT beats the session-id derivation
                tp = transcript(base, SONNET)
                c2 = cfg(base, env={"CLAUDE_CODE_SESSION_ID": sid}, QQ_MODEL_TRANSCRIPT=tp)
                self.assertEqual(authgate.model_identity(c2), SONNET)

    def test_pathological_session_id_never_globs(self):
        with tempfile.TemporaryDirectory() as base:
            for sid in ("", "../escape", "a/b"):
                c = cfg(base, env={"CLAUDE_CODE_SESSION_ID": sid})
                self.assertIsNone(authgate.transcript_path(c))


class TestModelMode(unittest.TestCase):
    def matrix_cfg(self, base, **overrides):
        return cfg(base, QQ_WRITE_TRUSTED_MODEL=FABLE, **overrides)

    def test_matrix(self):
        with tempfile.TemporaryDirectory() as base:
            c = self.matrix_cfg(base)
            self.assertEqual(authgate.model_mode(c, ""), "unknown")
            self.assertEqual(authgate.model_mode(c, FABLE), "fable")
            self.assertEqual(authgate.model_mode(c, OPUS), "opus")
            self.assertEqual(authgate.model_mode(c, SONNET), "unknown")
            self.assertEqual(authgate.model_mode(c, "local-llama"), "unknown")

    def test_empty_gated_id_never_reports_fable(self):
        # ade82f8's direction: on a gate-off install an empty QQ_WRITE_TRUSTED_MODEL must not
        # make an empty/unknown model be reported as 'fable'.
        with tempfile.TemporaryDirectory() as base:
            c = cfg(base)   # QQ_WRITE_TRUSTED_MODEL default: empty
            self.assertEqual(authgate.model_mode(c, ""), "unknown")
            self.assertEqual(authgate.model_mode(c, FABLE), "unknown")
            self.assertEqual(authgate.model_mode(c, OPUS), "opus")

    def test_explicitly_empty_safe_prefix_collapses_to_default(self):
        # bash parity: ${QQ_SAFE_MODEL_PREFIX:-claude-opus-} collapses empty to the default,
        # so opus stays detectable and nothing else can spuriously match ""*.
        with tempfile.TemporaryDirectory() as base:
            c = cfg(base, QQ_SAFE_MODEL_PREFIX="")
            self.assertEqual(authgate.model_mode(c, OPUS), "opus")
            self.assertEqual(authgate.model_mode(c, SONNET), "unknown")

    def test_parity_with_bash_qq_model_mode(self):
        """The python port and qq-redact.sh's qq_model_mode must agree on the whole matrix,
        driven through a REAL bash source of the shipped script (the same cross-engine
        discipline as test_config_parity)."""
        with tempfile.TemporaryDirectory() as base:
            cases = [(OPUS, "opus"), (FABLE, "fable"), (SONNET, "unknown"), ("", "unknown")]
            for model, want in cases:
                if model:
                    tp = transcript(base, model, name=f"{want}-{model}.jsonl")
                else:
                    tp = os.path.join(base, "empty.jsonl")
                    open(tp, "w").close()
                env = dict(os.environ)
                env["QQ_WRITE_TRUSTED_MODEL"] = FABLE
                env["QQ_CONFIG"] = "/nonexistent"
                r = subprocess.run(
                    ["bash", "-c",
                     f'. "{ENGINE}/qq-redact.sh"; qq_model_mode "$1"', "_", tp],
                    capture_output=True, text=True, env=env)
                bash_mode = r.stdout.strip()
                c = cfg(base, QQ_WRITE_TRUSTED_MODEL=FABLE, QQ_MODEL_TRANSCRIPT=tp)
                self.assertEqual(authgate.model_mode(c), bash_mode,
                                 f"python/bash divergence for model {model!r}")
                self.assertEqual(bash_mode, want)


# ---- slug matching ---------------------------------------------------------------------------
class TestSlugGated(unittest.TestCase):
    def test_exact_and_prefix(self):
        self.assertTrue(authgate.slug_gated("ssh-exposure", ["ssh-exposure"]))
        self.assertTrue(authgate.slug_gated("sec-incident-42", ["sec-*"]))
        self.assertFalse(authgate.slug_gated("insecurity", ["sec-*"]))
        self.assertFalse(authgate.slug_gated("roadmap", ["sec-*", "ssh-exposure"]))

    def test_no_substring_matching(self):
        # deliberate narrowing vs qq_slug_redacted's raw-substring fallback (that one matches
        # PATHS on read surfaces; a write target is a slug and substring would over-gate).
        self.assertFalse(authgate.slug_gated("my-sec-notes", ["sec-*"]))
        self.assertFalse(authgate.slug_gated("a-ssh-exposure-b", ["ssh-exposure"]))

    def test_path_and_md_normalization(self):
        self.assertTrue(authgate.slug_gated("sec-topic.md", ["sec-*"]))
        self.assertTrue(authgate.slug_gated("/abs/store/sec-topic.md", ["sec-*"]))
        self.assertTrue(authgate.slug_gated("subdir/sec-topic", ["sec-topic"]))
        self.assertFalse(authgate.slug_gated("nope", ["sec-*"]))

    def test_empty_entries_never_match(self):
        self.assertFalse(authgate.slug_gated("anything", []))
        self.assertFalse(authgate.slug_gated("anything", [""]))


# ---- the gate decision ------------------------------------------------------------------------
class TestGateReason(unittest.TestCase):
    def test_disabled_gate_is_none(self):
        with tempfile.TemporaryDirectory() as base:
            c = cfg(base, QQ_AUTHOR_GATE="0", QQ_AUTHOR_GATE_SLUGS="sec-*",
                    QQ_MODEL_TRANSCRIPT="/nonexistent")
            self.assertIsNone(authgate.gate_reason(c, "sec-topic"))

    def test_empty_slug_list_is_none(self):
        with tempfile.TemporaryDirectory() as base:
            c = cfg(base, QQ_MODEL_TRANSCRIPT="/nonexistent")
            self.assertIsNone(authgate.gate_reason(c, "sec-topic"))

    def test_non_matching_slug_skips_model_detection(self):
        with tempfile.TemporaryDirectory() as base:
            c = cfg(base, QQ_AUTHOR_GATE_SLUGS="sec-*", QQ_MODEL_TRANSCRIPT="/nonexistent")
            with mock.patch.object(authgate, "model_identity",
                                    side_effect=AssertionError("must not be called")):
                self.assertIsNone(authgate.gate_reason(c, "roadmap"))

    def test_trusted_models_pass(self):
        with tempfile.TemporaryDirectory() as base:
            for model in (OPUS, FABLE):
                tp = transcript(base, model, name=model + ".jsonl")
                c = cfg(base, QQ_AUTHOR_GATE_SLUGS="sec-*", QQ_WRITE_TRUSTED_MODEL=FABLE,
                        QQ_MODEL_TRANSCRIPT=tp)
                self.assertIsNone(authgate.gate_reason(c, "sec-topic"), model)

    def test_untrusted_and_unknown_divert(self):
        with tempfile.TemporaryDirectory() as base:
            tp = transcript(base, SONNET)
            c = cfg(base, QQ_AUTHOR_GATE_SLUGS="sec-*", QQ_MODEL_TRANSCRIPT=tp)
            self.assertEqual(authgate.gate_reason(c, "sec-topic"), SONNET)
            c2 = cfg(base, QQ_AUTHOR_GATE_SLUGS="sec-*", QQ_MODEL_TRANSCRIPT="/nonexistent")
            self.assertEqual(authgate.gate_reason(c2, "sec-topic"), "unknown")


# ---- the queue + Finding round-trip -------------------------------------------------------------
class TestQueueProposal(unittest.TestCase):
    def test_append_accumulates_and_preserves_other_sections(self):
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            path = os.path.join(store.config.get_path("QQ_STATE_DIR"), "pending-findings.md")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            ff = FindingsFile()
            ff.set_section("TIER1", ["- [T1 fix] INDEX.md was stale → reindexed + committed (auto)"])
            with open(path, "w") as fh:
                fh.write(ff.serialize())
            n1 = authgate.queue_proposal(store, "update", "sec-a", "first\n", SONNET)
            n2 = authgate.queue_proposal(store, "rewrite", "sec-a", "# whole\n\nfile\n", "unknown")
            self.assertNotIn("\n", n1)   # the stderr notice is ONE line
            self.assertNotIn("\n", n2)
            with open(path) as fh:
                parsed = FindingsFile.parse(fh.read())
            self.assertEqual(len(parsed.get_section(authgate.SECTION)), 2)   # append, not replace
            self.assertEqual(parsed.get_section("TIER1"),
                              ["- [T1 fix] INDEX.md was stale → reindexed + committed (auto)"])

    def test_finding_round_trip_recovers_verbatim_text(self):
        text = "> updated: 2026-07-04T00:00:00Z line one\nline two\n"
        with tempfile.TemporaryDirectory() as base:
            store = make_store(base)
            authgate.queue_proposal(store, "update", "sec-a", text, SONNET)
            [line] = proposed_lines(store)
            f = parse_line(line)
            self.assertEqual(f.cls, "PROPOSED write")
            self.assertEqual(f.fields["head"], "sec-a")
            self.assertEqual(f.fields["verb"], "update")
            self.assertEqual(f.fields["model"], SONNET)
            self.assertEqual(f.fields["text"], text)          # verbatim, newlines intact
            self.assertEqual(f.render(), line)                # byte round-trip


# ---- write-verb integration ---------------------------------------------------------------------
GATED = {"QQ_AUTHOR_GATE_SLUGS": "sec-*", "QQ_WRITE_TRUSTED_MODEL": FABLE}


class TestVerbIntegration(unittest.TestCase):
    def gated_store(self, base, model=None) -> Store:
        tp = transcript(base, model) if model else "/nonexistent"
        return make_store(base, QQ_MODEL_TRANSCRIPT=tp, **GATED)

    def test_update_diverts_head_and_git_untouched(self):
        with tempfile.TemporaryDirectory() as base:
            store = self.gated_store(base, SONNET)
            w.new(store, "roadmap", "not gated")          # untrusted, NON-gated slug: lands
            # seed the gated HEAD as a trusted writer would have
            opus_store = Store(store.config.with_overrides(
                QQ_MODEL_TRANSCRIPT=transcript(base, OPUS, name="opus.jsonl")))
            w.new(opus_store, "sec-topic", "seed")
            before = read_head(store, "sec-topic")
            log_before = subprocess.run(["git", "-C", str(store.qdir), "rev-parse", "HEAD"],
                                          capture_output=True, text=True).stdout
            with self.assertRaises(w.WriteGateDiverted) as ctx:
                w.update(store, "sec-topic", "an untrusted claim\n")
            self.assertEqual(ctx.exception.exit_code, 0)
            self.assertEqual(read_head(store, "sec-topic"), before)
            log_after = subprocess.run(["git", "-C", str(store.qdir), "rev-parse", "HEAD"],
                                          capture_output=True, text=True).stdout
            self.assertEqual(log_before, log_after)       # no commit
            [line] = proposed_lines(store)
            self.assertIn("an untrusted claim", line)

    def test_update_missing_head_still_raises_engine_error(self):
        # a gated slug's REFUSAL paths stay identical: the gate never converts a would-fail
        # write into a queued "success".
        with tempfile.TemporaryDirectory() as base:
            store = self.gated_store(base, SONNET)
            with self.assertRaises(w.WriteError) as ctx:
                w.update(store, "sec-missing", "text\n")
            self.assertNotIsInstance(ctx.exception, w.WriteGateDiverted)
            self.assertIn("--prepend-update needs an existing", str(ctx.exception))
            self.assertEqual(proposed_lines(store), [])

    def test_new_diverts_and_duplicate_still_refuses(self):
        with tempfile.TemporaryDirectory() as base:
            store = self.gated_store(base, SONNET)
            with self.assertRaises(w.WriteGateDiverted):
                w.new(store, "sec-fresh", "an essence")
            self.assertFalse(store.has_head("sec-fresh"))
            [line] = proposed_lines(store)
            self.assertIn("an essence", line)
            # duplicate refusal precedes the gate
            opus_store = Store(store.config.with_overrides(
                QQ_MODEL_TRANSCRIPT=transcript(base, OPUS, name="opus.jsonl")))
            w.new(opus_store, "sec-exists", "seed")
            with self.assertRaises(w.WriteError) as ctx:
                w.new(store, "sec-exists", "again")
            self.assertNotIsInstance(ctx.exception, w.WriteGateDiverted)
            self.assertIn("already exists", str(ctx.exception))

    def test_rewrite_and_essence_divert(self):
        with tempfile.TemporaryDirectory() as base:
            store = self.gated_store(base, SONNET)
            opus_store = Store(store.config.with_overrides(
                QQ_MODEL_TRANSCRIPT=transcript(base, OPUS, name="opus.jsonl")))
            w.new(opus_store, "sec-topic", "seed")
            before = read_head(store, "sec-topic")
            whole = "# Quintessence — sec-topic\n> updated: x\n> essence: rewritten\n\n## Notes\n"
            with self.assertRaises(w.WriteGateDiverted):
                w.rewrite(store, "sec-topic", whole)
            with self.assertRaises(w.WriteGateDiverted):
                w.essence(store, "sec-topic", "a new essence")
            self.assertEqual(read_head(store, "sec-topic"), before)
            lines = proposed_lines(store)
            self.assertEqual([parse_line(ln).fields.get("verb") for ln in lines],
                              ["rewrite", "essence"])
            self.assertEqual(parse_line(lines[0]).fields["text"], whole)   # verbatim whole file
            # rewrite of a MISSING gated head still refuses (never queues)
            with self.assertRaises(w.WriteError) as ctx:
                w.rewrite(store, "sec-gone", whole)
            self.assertNotIsInstance(ctx.exception, w.WriteGateDiverted)

    def test_trusted_and_disabled_pass_through(self):
        with tempfile.TemporaryDirectory() as base:
            store = self.gated_store(base, OPUS)
            out = w.new(store, "sec-topic", "seed")
            self.assertIn("committed + mirrored", out)
            out = w.update(store, "sec-topic", "trusted line\n")
            self.assertIn("committed + mirrored", out)
            self.assertIn("trusted line", read_head(store, "sec-topic"))
            self.assertEqual(proposed_lines(store), [])
            # gate disabled entirely: even the unknown model writes
            off = Store(store.config.with_overrides(QQ_AUTHOR_GATE="0",
                                                      QQ_MODEL_TRANSCRIPT="/nonexistent"))
            out = w.update(off, "sec-topic", "gate-off line\n")
            self.assertIn("committed + mirrored", out)
            self.assertEqual(proposed_lines(store), [])

    def test_queue_failure_is_loud_not_silent(self):
        with tempfile.TemporaryDirectory() as base:
            store = self.gated_store(base, SONNET)
            opus_store = Store(store.config.with_overrides(
                QQ_MODEL_TRANSCRIPT=transcript(base, OPUS, name="opus.jsonl")))
            w.new(opus_store, "sec-topic", "seed")
            with mock.patch.object(authgate, "queue_proposal",
                                    side_effect=OSError("disk full")):
                with self.assertRaises(w.WriteError) as ctx:
                    w.update(store, "sec-topic", "text\n")
            self.assertNotIsInstance(ctx.exception, w.WriteGateDiverted)
            self.assertEqual(ctx.exception.exit_code, 2)
            self.assertIn("queueing the proposal FAILED", str(ctx.exception))
            self.assertNotIn("text", read_head(store, "sec-topic"))

    def test_concurrent_create_caught_by_under_lock_recheck(self):
        """The closed race: a gated topic created between the pre-lock existence check and
        the write lock is caught by the gate_check re-evaluation under the lock — the
        untrusted update is diverted to the queue, never lands.  The file must exist on
        disk (simulating the concurrent qq new having completed) so the engine's own
        pre-lock is_file() check passes."""
        with tempfile.TemporaryDirectory() as base:
            store = self.gated_store(base, SONNET)
            opus_store = Store(store.config.with_overrides(
                QQ_MODEL_TRANSCRIPT=transcript(base, OPUS, name="opus.jsonl")))
            w.new(opus_store, "sec-topic", "seed")
            before = read_head(store, "sec-topic")
            call_count = [0]
            def exists_flips(_store, _topic):
                call_count[0] += 1
                if call_count[0] == 1:
                    return False
                return True
            with mock.patch.object(w, '_gate_target_exists', side_effect=exists_flips):
                with self.assertRaises(w.WriteGateDiverted):
                    w.update(store, "sec-topic", "sneaky line\n")
            self.assertEqual(call_count[0], 2)
            self.assertEqual(read_head(store, "sec-topic"), before)
            lines = proposed_lines(store)
            self.assertEqual(len(lines), 1)
            self.assertIn("sneaky line", lines[0])


if __name__ == "__main__":
    unittest.main()
