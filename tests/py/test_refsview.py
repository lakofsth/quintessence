# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""Unit tests for quintessence.refsview (B2 read-side revalidation): the (head, line_ts)
join, per-status change rules (ok/waived re-hash file refs;
missing-at-write annotates only on APPEARANCE; suspect always; fp-error never; git/unit/probe
never re-fingerprinted on read), and the two hard gates — READ-ONLY (a render never mutates
refs.jsonl or anything else in the state dir) and BYTE-PARITY (no refs / no change / QQ_BIND=0
all render byte-identical to pre-B2). End-to-end CLI shape is tests/test-reval.sh's job."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from quintessence import cli
from quintessence.ask import Ask
from quintessence.config import Config
from quintessence.refsview import ANNOTATION_PREFIX, RefsView
from quintessence.store import Store

TS = "2026-07-01T10:00:00Z"


def make_store(base: str, **overrides) -> Store:
    qdir = os.path.join(base, "store")
    over = {"QUINTESSENCE_DIR": qdir,
            "QQ_MEMDIR": os.path.join(base, "mem"),
            "QQ_STATE_DIR": os.path.join(base, "state")}
    over.update(overrides)
    cfg = Config(env={}, config_file="/nonexistent", overrides=over)
    os.makedirs(qdir, exist_ok=True)
    return Store(cfg)


def write_refs(store: Store, records: list) -> str:
    refs_dir = store.state_dir / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    path = refs_dir / "refs.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return str(path)


def rec(head="T", line_ts=TS, kind="file", ident="/x", fp=None, status="ok", **extra):
    d = {"head": head, "line_ts": line_ts, "kind": kind, "id": ident,
         "fp": fp, "asof": TS, "status": status}
    d.update(extra)
    return d


def sha_fp(path: str) -> str:
    import hashlib
    with open(path, "rb") as fh:
        return "sha256:" + hashlib.sha256(fh.read()).hexdigest()


class RefsViewHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.store = make_store(self.tmp)
        self.artifact = os.path.join(self.tmp, "artifact.txt")
        with open(self.artifact, "w") as fh:
            fh.write("v1\n")

    def view(self):
        return RefsView(self.store.config)

    def line(self, text="claim about the artifact"):
        return f"> updated: {TS} {text}"


class TestChangeRules(RefsViewHarness):
    def test_no_refs_file_no_annotations_identical_output(self):
        v = self.view()
        lines = [self.line(), "body"]
        self.assertEqual(v.annotate_lines("T", lines), lines)
        text = f"# T\n{self.line()}\n"
        self.assertIs(v.annotate_text("T", text), text)   # not just equal: the same object

    def test_unchanged_file_ref_stays_silent(self):
        write_refs(self.store, [rec(ident=self.artifact, fp=sha_fp(self.artifact))])
        v = self.view()
        lines = [self.line()]
        self.assertEqual(v.annotate_lines("T", lines), lines)

    def test_changed_file_ref_annotates_after_the_line(self):
        write_refs(self.store, [rec(ident=self.artifact, fp=sha_fp(self.artifact))])
        with open(self.artifact, "w") as fh:
            fh.write("v2 — moved on\n")
        out = self.view().annotate_lines("T", [self.line(), "next"])
        self.assertEqual(out, [
            self.line(),
            f"{ANNOTATION_PREFIX}{self.artifact} (2026-07-01)",
            "next"])

    def test_tilde_id_expands_for_rehash(self):
        # B1 records ids in ~-form when the prose used it; the re-hash must expand. HOME is
        # pinned to the fixture dir so the test is deterministic wherever tmp lives.
        from unittest import mock
        home_rel = "~/artifact.txt"
        write_refs(self.store, [rec(ident=home_rel, fp=sha_fp(self.artifact))])
        with open(self.artifact, "a") as fh:
            fh.write("more\n")
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            out = self.view().annotate_lines("T", [self.line()])
        self.assertEqual(len(out), 2)
        self.assertIn(home_rel, out[1])

    def test_deleted_referent_annotates(self):
        write_refs(self.store, [rec(ident=self.artifact, fp=sha_fp(self.artifact))])
        os.unlink(self.artifact)
        out = self.view().annotate_lines("T", [self.line()])
        self.assertEqual(len(out), 2)

    def test_missing_at_write_still_missing_stays_silent(self):
        gone = os.path.join(self.tmp, "never-existed")
        write_refs(self.store, [rec(ident=gone, fp=None, status="missing-at-write")])
        lines = [self.line()]
        self.assertEqual(self.view().annotate_lines("T", lines), lines)

    def test_missing_at_write_appearance_annotates(self):
        late = os.path.join(self.tmp, "late-artifact")
        write_refs(self.store, [rec(ident=late, fp=None, status="missing-at-write")])
        with open(late, "w") as fh:
            fh.write("now it exists\n")
        out = self.view().annotate_lines("T", [self.line()])
        self.assertEqual(len(out), 2)

    def test_suspect_always_annotates_any_kind(self):
        write_refs(self.store, [rec(kind="git", ident="dist@abc1234",
                                     fp="git:full:default", status="suspect")])
        out = self.view().annotate_lines("T", [self.line()])
        self.assertEqual(len(out), 2)
        self.assertIn("dist@abc1234", out[1])

    def test_non_file_kinds_never_refingerprinted_on_read(self):
        # a git/unit/probe/port/rfile ref with status ok must stay silent regardless of
        # reality — the read path runs no subprocesses (the read-path budget; probe commands NEVER
        # run on read, no ss, no ssh).
        # One id is a REAL file with a deliberately stale fp: if the kind-gate were
        # removed, _fp_file would succeed there and the mismatch would annotate, so
        # this test fails for the right reason rather than passing vacuously on ids
        # that cannot be fingerprinted at all.
        realfile = os.path.join(self.tmp, "unit-shaped-real-file")
        with open(realfile, "w") as fh:
            fh.write("content the recorded fp does not match\n")
        write_refs(self.store, [
            rec(kind="git", ident="dist@abc1234", fp="git:doesnotmatter:default"),
            rec(kind="unit", ident=realfile, fp="sha256:stale"),
            rec(kind="probe", ident="false", fp="sha256:stale"),
            rec(kind="port", ident="65534", fp="listen:ghostd"),
        ])
        lines = [self.line()]
        self.assertEqual(self.view().annotate_lines("T", lines), lines)

    def test_suspect_port_annotates(self):
        # port refs, read side: suspect-only, pure annotation (the sweep is what flips it)
        write_refs(self.store, [rec(kind="port", ident="11435", fp="listen:llama-server",
                                     status="suspect")])
        out = self.view().annotate_lines("T", [self.line()])
        self.assertEqual(len(out), 2)
        self.assertIn("11435", out[1])

    def test_fp_error_record_skipped(self):
        write_refs(self.store, [rec(ident=self.artifact, fp=None, status="fp-error")])
        lines = [self.line()]
        self.assertEqual(self.view().annotate_lines("T", lines), lines)

    def test_waived_compares_against_refreshed_fp(self):
        write_refs(self.store, [rec(ident=self.artifact, fp=sha_fp(self.artifact),
                                     status="waived")])
        lines = [self.line()]
        self.assertEqual(self.view().annotate_lines("T", lines), lines)
        with open(self.artifact, "a") as fh:
            fh.write("post-waive change\n")
        self.assertEqual(len(self.view().annotate_lines("T", lines)), 2)

    def test_join_is_head_and_line_ts_scoped(self):
        write_refs(self.store, [rec(ident=self.artifact, fp="sha256:definitely-changed")])
        v = self.view()
        other_head = v.annotate_lines("OTHER", [self.line()])
        self.assertEqual(len(other_head), 1)          # same ts, different head: no join
        other_ts = v.annotate_lines("T", ["> updated: 2026-06-30T09:00:00Z older line"])
        self.assertEqual(len(other_ts), 1)            # same head, different ts: no join

    def test_last_record_wins_per_ref(self):
        # a superseding record (e.g. the resolver's rewrite, or a re-bind) replaces the older
        # one in the view — here the newer record carries the CURRENT fp, so no annotation.
        write_refs(self.store, [
            rec(ident=self.artifact, fp="sha256:old-and-stale"),
            rec(ident=self.artifact, fp=sha_fp(self.artifact)),
        ])
        lines = [self.line()]
        self.assertEqual(self.view().annotate_lines("T", lines), lines)

    def test_qq_bind_zero_disables_read_side(self):
        write_refs(self.store, [rec(ident=self.artifact, fp="sha256:changed")])
        store0 = make_store(self.tmp, QQ_BIND="0")
        lines = [self.line()]
        self.assertEqual(RefsView(store0.config).annotate_lines("T", lines), lines)

    def test_corrupt_lines_never_break_the_view(self):
        path = write_refs(self.store, [rec(ident=self.artifact, fp="sha256:changed")])
        with open(path, "a") as fh:
            fh.write("not json at all\n{\"half\": \n[1,2,3]\n")
        out = self.view().annotate_lines("T", [self.line()])
        self.assertEqual(len(out), 2)   # the good record still annotates


class TestReadOnly(RefsViewHarness):
    def test_render_never_mutates_state(self):
        path = write_refs(self.store, [
            rec(ident=self.artifact, fp="sha256:changed"),
            rec(ident=os.path.join(self.tmp, "gone"), fp=None, status="missing-at-write"),
            rec(kind="git", ident="dist@abc1234", fp="x", status="suspect"),
        ])
        with open(path, "rb") as fh:
            before = fh.read()
        state_before = sorted(os.listdir(self.store.state_dir / "refs"))
        v = self.view()
        v.annotate_lines("T", [self.line()])
        v.annotate_text("T", f"# T\n{self.line()}\n")
        v.chunk_annotations("T", self.line())
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)   # NO status transitions from the read path
        self.assertEqual(sorted(os.listdir(self.store.state_dir / "refs")), state_before)


class TestRenderIntegration(RefsViewHarness):
    def _head_text(self):
        return (f"# Quintessence — T\n"
                f"{self.line()}\n"
                f"> essence: e\n\n"
                f"## RE-ENTER HERE\nbody\n")

    def _place_head(self):
        (self.store.qdir / "T.md").write_text(self._head_text(), encoding="utf-8")

    def test_show_and_brief_annotate_changed_ref(self):
        self._place_head()
        write_refs(self.store, [rec(ident=self.artifact, fp=sha_fp(self.artifact))])
        with open(self.artifact, "a") as fh:
            fh.write("drift\n")
        show = cli.render_show(self.store, ["T"])
        brief = cli.render_brief(self.store, ["T"])
        expected = f"{ANNOTATION_PREFIX}{self.artifact} (2026-07-01)"
        self.assertIn(f"{self.line()}\n{expected}\n", show)
        self.assertIn(f"{self.line()}\n{expected}\n", brief)

    def test_show_and_brief_byte_identical_when_unchanged(self):
        self._place_head()
        write_refs(self.store, [rec(ident=self.artifact, fp=sha_fp(self.artifact))])
        with_refs_show = cli.render_show(self.store, ["T"])
        with_refs_brief = cli.render_brief(self.store, ["T"])
        # remove the refs file: the no-binding rendering is the parity baseline
        os.unlink(self.store.state_dir / "refs" / "refs.jsonl")
        self.assertEqual(with_refs_show, cli.render_show(self.store, ["T"]))
        self.assertEqual(with_refs_brief, cli.render_brief(self.store, ["T"]))

    def test_ask_evidence_header_carries_annotation(self):
        write_refs(self.store, [rec(ident=self.artifact, fp=sha_fp(self.artifact))])
        with open(self.artifact, "a") as fh:
            fh.write("drift\n")
        ask = Ask(self.store.config, search_index=None)
        hits = [{"path": "quintessence/T.md", "title": "T", "score": 0.9,
                 "text": self.line(), "snippet": "s"}]
        evidence, sources = ask.build_evidence(hits, budget_chars=10_000)
        header, rest = evidence.split("\n", 1)
        self.assertTrue(header.startswith("[1] quintessence/T.md"))
        self.assertTrue(rest.startswith(f"{ANNOTATION_PREFIX}{self.artifact} (2026-07-01)\n"))
        self.assertEqual(len(sources), 1)

    def test_ask_evidence_identical_without_changes(self):
        write_refs(self.store, [rec(ident=self.artifact, fp=sha_fp(self.artifact))])
        ask = Ask(self.store.config, search_index=None)
        hits = [{"path": "quintessence/T.md", "title": "T", "score": 0.9,
                 "text": self.line(), "snippet": "s"}]
        with_refs = ask.build_evidence(hits, budget_chars=10_000)
        os.unlink(self.store.state_dir / "refs" / "refs.jsonl")
        self.assertEqual(with_refs, ask.build_evidence(hits, budget_chars=10_000))


class TestCorpusRecordsInvisible(RefsViewHarness):
    """Corpus records live in the same refs.jsonl but carry
    {corpus, file} instead of {head, line_ts} — the view's loader requires head+line_ts, so
    they can never join a rendered line. Pinned so the by-construction skip can't regress."""

    def test_corpus_record_never_annotates_or_registers(self):
        write_refs(self.store, [{"corpus": "mem", "file": "fact.md", "kind": "file",
                                  "id": self.artifact, "fp": "sha256:absolutely-not-current",
                                  "asof": TS, "status": "suspect"}])
        v = self.view()
        self.assertFalse(v.has_refs("T"))
        lines = [self.line()]
        self.assertEqual(v.annotate_lines("T", lines), lines)


class TestRetiredStatus(RefsViewHarness):
    """`retired` is TERMINAL — never annotated, never re-hashed, even when the referent
    demonstrably changed (that silence is the point: adjudication's off-switch for a hot dir
    suspect-once can't quiet). Pinned so `_changed`'s status gate can't regress."""

    def test_retired_file_ref_never_annotates_despite_real_change(self):
        write_refs(self.store, [rec(ident=self.artifact, fp=sha_fp(self.artifact),
                                     status="retired")])
        with open(self.artifact, "w") as fh:
            fh.write("v2 — really changed\n")
        v = self.view()
        lines = [self.line()]
        self.assertEqual(v.annotate_lines("T", lines), lines)

    def test_retired_born_stale_never_annotates_on_appearance(self):
        # missing-at-write annotates on APPEARANCE; the same record retired must not
        gone = os.path.join(self.tmp, "late.txt")
        write_refs(self.store, [rec(ident=gone, fp=None, status="retired")])
        with open(gone, "w") as fh:
            fh.write("appeared\n")
        v = self.view()
        lines = [self.line()]
        self.assertEqual(v.annotate_lines("T", lines), lines)


if __name__ == "__main__":
    unittest.main()
