#!/usr/bin/env python3
"""Generate an asciinema v2 .cast file for the llm-router demo SVG."""

import json
import sys
from pathlib import Path

# ANSI helpers
R  = "\033[0m"
G  = "\033[32m"
Y  = "\033[33m"
RE = "\033[31m"
B  = "\033[1m"
D  = "\033[2m"
C  = "\033[36m"  # cyan

WIDTH  = 105
HEIGHT = 44

# ── Output lines ──────────────────────────────────────────────────────────────

DEMO_LINES = [
    "",
    f"{B}llm-router demo{R}  — how smart routing handles your prompts",
    "",
    f"  Active config: Claude Code subscription",
    f"  {D}(no routing history yet — showing examples){R}",
    "",
    f"  {'Prompt':<46}  {'Task':<8}  {'Complexity':<12}  {'Model':<19}  Cost",
    "  " + "─" * 101,
    f"  {'\"what does os.path.join do?\"':<46}  {'query':<8}  {G}{'simple':<12}{R}  {'Claude Haiku':<19}  {G}$0.00001{R}",
    f"  {'\"why is my async code slow?\"':<46}  {'analyze':<8}  {Y}{'moderate':<12}{R}  {'Claude Sonnet':<19}  {Y}$0.003{R}",
    f"  {'\"implement a Redis-backed rate limiter\"':<46}  {'code':<8}  {RE}{'complex':<12}{R}  {'Claude Opus':<19}  {Y}$0.015{R}",
    f"  {'\"prove the halting problem is undecidable\"':<46}  {'analyze':<8}  {RE}{'deep_reason':<12}{R}  {'Opus+thinking':<19}  {RE}$0.030{R}",
    f"  {'\"research latest Gemini 2.5 benchmarks\"':<46}  {'research':<8}  {Y}{'moderate':<12}{R}  {'Perplexity Sonar':<19}  {Y}$0.002{R}",
    f"  {'\"write a hero section for a SaaS landing\"':<46}  {'generate':<8}  {Y}{'moderate':<12}{R}  {'Claude Sonnet':<19}  {Y}$0.001{R}",
    f"  {'\"generate a dashboard screenshot mockup\"':<46}  {'image':<8}  {'—':<12}  {'Flux Pro':<19}  {RE}$0.040{R}",
    "  " + "─" * 101,
    "",
    f"  {B}Savings vs always-Opus:{R}  {G}$0.105 → $0.091  (13% cheaper){R}",
    "",
    f"  {Y}→{R} Run {B}llm-router status{R} for cumulative savings.",
    f"  {Y}→{R} Run {B}llm-router dashboard{R} to see live routing decisions.",
    "",
]

SEP = "─" * 62

STATUS_LINES = [
    "",
    SEP,
    f"  {B}llm-router status{R}",
    SEP,
    "",
    f"  {B}Claude Code subscription{R}  (2m ago)",
    f"    {'session (5h)':<16} {G}████████████████{R}░░░░  {G}82.0%{R}",
    f"    {'weekly':<16} {G}████{R}░░░░░░░░░░░░░░░░  {G}21.0%{R}",
    f"    {'weekly sonnet':<16} {Y}████████{R}░░░░░░░░░░░░  {Y}40.0%{R}",
    "",
    f"  {B}Routing savings{R}",
    f"    {B}today{R}                 {G}$0.063{R} saved  ({G}28%{R} cheaper)",
    f"  {G}{'█' * 8}{Y}{'░' * 20}{R}  340 calls",
    f"    {B}7 days{R}               {G}$0.869{R} saved  ({G}36%{R} cheaper)",
    f"  {G}{'█' * 10}{Y}{'░' * 18}{R}  1,560 calls",
    f"    {B}30 days{R}              {G}$1.061{R} saved  ({G}35%{R} cheaper)",
    f"  {G}{'█' * 10}{Y}{'░' * 18}{R}  2,026 calls",
    "",
    f"  {B}Top models used{R}",
    f"    {'gpt-4o':<34}  594×  $0.4432",
    f"    {'gemini-2.5-flash':<34}  591×  $0.3944",
    f"    {'gpt-5.4 (Codex)':<34}  283×  $0.0000",
    f"    {'gpt-4o-mini':<34}  206×  $0.2000",
    "",
    f"  {B}Subcommands{R}",
    f"    {B}llm-router update{R}     — update hooks to latest version",
    f"    {B}llm-router doctor{R}     — full health check",
    f"    {B}llm-router dashboard{R}  — web dashboard (localhost:7337)",
    SEP,
    "",
]


# ── Cast builder ──────────────────────────────────────────────────────────────

def build_cast(out_path: Path) -> None:
    events: list[tuple[float, str, str]] = []
    t = 0.5  # initial pause

    def emit(text: str, delay: float = 0.0) -> None:
        nonlocal t
        t += delay
        events.append((round(t, 4), "o", text))

    def type_cmd(cmd: str) -> None:
        emit(f"{C}${R} ", 0.0)
        for ch in cmd:
            emit(ch, 0.08)
        emit("\r\n", 0.12)

    def print_lines(lines: list[str], gap: float = 0.04) -> None:
        for line in lines:
            emit(line + "\r\n", gap)

    # ── llm-router demo ───────────────────────────────────────────────────────
    type_cmd("llm-router demo")
    emit("", 0.1)
    print_lines(DEMO_LINES, gap=0.035)

    # pause before next command
    emit("", 1.8)

    # ── llm-router status ─────────────────────────────────────────────────────
    type_cmd("llm-router status")
    emit("", 0.1)
    print_lines(STATUS_LINES, gap=0.04)

    # trailing cursor blink pause
    emit("", 2.5)

    # ── Write file ────────────────────────────────────────────────────────────
    header = {
        "version": 2,
        "width": WIDTH,
        "height": HEIGHT,
        "title": "llm-router — smart model routing for Claude Code",
        "env": {"TERM": "xterm-256color", "SHELL": "/bin/zsh"},
    }

    with out_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        for ts, kind, data in events:
            f.write(json.dumps([ts, kind, data]) + "\n")

    total = round(t, 1)
    print(f"Written {len(events)} events, {total}s  →  {out_path}", file=sys.stderr)


if __name__ == "__main__":
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/demo.cast")
    dest.parent.mkdir(parents=True, exist_ok=True)
    build_cast(dest)
