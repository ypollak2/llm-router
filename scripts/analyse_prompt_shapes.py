#!/usr/bin/env python3
"""What shape are the user's real prompts? Run before building a prompt classifier.

Written because a "mechanical prompt" classifier was built and validated against
invented examples — "What is the value of MAX_RETRIES?", "Which file defines
LRUCache?" — where it scored 11/11. Run against 169 real prompts from five days
of transcripts it matched **zero**.

The prompts were not questions. Median length 11 words, and the most common
openings are `go`, `yes`, `Proceed`, `continue`, `keep going`, `I want`,
`Show me`. They are continuations of an ongoing conversation, whose meaning
lives in the previous turn rather than in the text.

That kills zero-claude-for-mechanical-prompts as a saving: there is no
population to intercept. It also generalises — validate any prompt classifier
against real transcripts before building on it, because a classifier tested on
its author's examples measures the author.
"""
from __future__ import annotations

import collections
import glob
import json
import os
import sys
import time


def real_prompts(days: float = 5.0) -> list[str]:
    cutoff = time.time() - days * 86400
    found: list[str] = []
    for path in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        if os.path.getmtime(path) < cutoff:
            continue
        for line in open(path, errors="ignore"):
            try:
                record = json.loads(line)
            except Exception:
                continue
            if record.get("type") != "user":
                continue
            content = (record.get("message") or {}).get("content")
            if isinstance(content, str) and content.strip():
                found.append(content.strip())
            elif isinstance(content, list):
                for block in content:
                    if (isinstance(block, dict) and block.get("type") == "text"
                            and block.get("text", "").strip()):
                        found.append(block["text"].strip())
    # Drop hook injections and tool plumbing — they are not things a user typed.
    return [p for p in found
            if not p.startswith(("ROUTING NOTICE", "<", "[", "Caveat:", "⚡", "🧠", "💡"))
            and "system-reminder" not in p[:200]]


def main() -> int:
    days = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    prompts = real_prompts(days)
    if not prompts:
        print("no prompts found")
        return 1
    lengths = sorted(len(p.split()) for p in prompts)
    print(f"{len(prompts)} real prompts over {days:g} days")
    print(f"median {lengths[len(lengths) // 2]} words, "
          f"{sum(1 for n in lengths if n <= 25) / len(lengths):.0%} under 25 words\n")
    print("most common opening word:")
    openings = collections.Counter(p.split()[0].lower().strip(",.") for p in prompts if p.split())
    for word, count in openings.most_common(15):
        print(f"   {word:14s} {count}")
    print("\nshortest prompts:")
    for p in sorted(prompts, key=lambda x: len(x.split()))[:12]:
        print("   ", p.replace("\n", " ")[:88])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
