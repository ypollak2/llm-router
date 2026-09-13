"""The routing trace must distinguish "routed" from "replaced a Claude turn".

Every counter in this project has historically answered the first question
while being read as the second: the session banner reports "routes: 200 · local
(170)" from classification-time rows, and the savings ledger credited drafts
that echo mode then discarded. The trace exists so the two cannot be confused.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEW = ROOT / "scripts" / "trace_view.py"


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _routes(path: Path) -> str:
    return subprocess.run(
        [sys.executable, str(VIEW), "--routes", "--file", str(path)],
        capture_output=True, text=True, timeout=60,
    ).stdout


def _rows(invocation: str, substituted: bool | None, chain: list[str]):
    rows = [
        {"ts": 1.0, "run": "r", "seq": 1, "event": "route.prompt",
         "invocation": invocation, "prompt": "p", "session": "s"},
        {"ts": 1.1, "run": "r", "seq": 2, "event": "route.decision",
         "invocation": invocation, "task_type": "query", "complexity": "simple",
         "needs_tools": False, "chain": chain},
    ]
    if substituted is not None:
        rows.append({"ts": 1.2, "run": "r", "seq": 3, "event": "route.outcome",
                     "invocation": invocation, "model": "ollama/x",
                     "substituted": substituted, "latency_ms": 10})
    return rows


def test_echo_is_not_counted_as_replacing_a_turn(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, _rows("1.0", substituted=False, chain=["ollama/x"]))
    out = _routes(path)
    assert "advisory only" in out, out
    assert "replaced a Claude turn on 0" in out, out


def test_block_is_counted_as_replacing_a_turn(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, _rows("1.0", substituted=True, chain=["ollama/x"]))
    out = _routes(path)
    assert "YES" in out, out
    assert "replaced a Claude turn on 1" in out, out


def test_an_empty_chain_is_reported_as_no_eligible_model(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, _rows("1.0", substituted=None, chain=[]))
    out = _routes(path)
    assert "no free-tier model was eligible" in out, out


def test_a_chain_serialised_to_a_string_still_renders(tmp_path):
    """trace._clip turns a long list into a JSON string; joining it naively
    printed one character per column."""
    path = tmp_path / "t.jsonl"
    rows = _rows("1.0", substituted=False, chain='["ollama/qwen3-coder:30b"]')
    _write(path, rows)
    out = _routes(path)
    assert "ollama/qwen3-coder:30b" in out, out
    assert "o,l,l,a,m,a" not in out, out
