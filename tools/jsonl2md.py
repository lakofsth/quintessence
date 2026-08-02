#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Thomas Lakofski
"""jsonl2md.py — Claude Code session-transcript -> human-readable markdown converter.

Cheapest-test tool for Proposal #1 (transcript-corpus QA). Keeps ONLY conversational
text: user text messages and assistant text blocks (role-labeled turn headers). Strips
tool_use inputs, tool_result contents, thinking blocks, and non-conversational JSONL
record types (system, attachment, mode, queue-operation, ai-title, file-history-snapshot,
last-prompt, permission-mode) plus meta user entries (isMeta, e.g. the local-command-caveat
wrapper). Never touches anything outside the given output path.

Usage: jsonl2md.py <in.jsonl> <out.md>
"""
import json
import sys


def extract_texts(content):
    """Return list of non-empty text strings from a message.content value. Only 'text'
    blocks (or a bare string content) count as conversation; tool_use/tool_result/
    thinking/fallback blocks are dropped here, by construction."""
    texts = []
    if isinstance(content, str):
        s = content.strip()
        if s:
            texts.append(s)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = (block.get("text") or "").strip()
                if t:
                    texts.append(t)
    return texts


def convert(in_path, out_path):
    turn = 0
    kept_lines = 0
    total_lines = 0
    parse_errors = 0
    with open(in_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    out = []
    out.append(f"# session transcript — {in_path.split('/')[-1]}\n\n")
    for line in lines:
        total_lines += 1
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        t = d.get("type")
        if t not in ("user", "assistant"):
            continue
        if t == "user" and d.get("isMeta"):
            continue
        msg = d.get("message") or {}
        role = msg.get("role", t)
        texts = extract_texts(msg.get("content"))
        if not texts:
            continue
        turn += 1
        kept_lines += 1
        ts = d.get("timestamp", "")
        out.append(f"## turn {turn} — {role}\n\n")
        if ts:
            out.append(f"_{ts}_\n\n")
        for txt in texts:
            out.append(txt + "\n\n")
    with open(out_path, "w", encoding="utf-8") as of:
        of.write("".join(out))
    return {
        "turns": turn,
        "jsonl_lines_total": total_lines,
        "jsonl_lines_kept": kept_lines,
        "parse_errors": parse_errors,
    }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    stats = convert(sys.argv[1], sys.argv[2])
    print(json.dumps(stats))
