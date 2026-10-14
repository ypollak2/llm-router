"""A fixture HOME as a 15.4.0 user has it: state, three wired hosts, stale hooks.

``build(home)`` writes it into ``home``; state lives in ``home/.llm-router``.
Hook files carry ``# llm_router-hook-version: 1`` (the packaged ones are far
newer), so an upgrade that skips any host shows up as a version mismatch.

Code-backed files: ``discovery.json`` (the model map), ``routing.yaml`` and
``profile.yaml`` (priorities), ``usage.db`` (history). ``examples/`` has no
reader in the code today; it is kept as a user directory the upgrade must not
touch (a pure preservation check, not a feature).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

OLD_HOOK = "#!/usr/bin/env python3\n# llm_router-hook-version: 1\nprint('old 15.4.0 hook')\n"

#: Files that must come through an upgrade byte-identical (state-relative).
PRESERVED = (
    "discovery.json",
    "routing.yaml",
    "profile.yaml",
    "examples/classify-1.txt",
    "examples/classify-2.txt",
)
USAGE_ROWS = 3


def build(home: Path) -> Path:
    from llm_router import install_hooks as ih

    state = home / ".llm-router"
    state.mkdir(parents=True)
    (state / "discovery.json").write_text(json.dumps({
        "cached_at": time.time(),
        "models": {"ollama/mine:7b": {"model_id": "ollama/mine:7b", "provider": "ollama"}},
    }))
    (state / "routing.yaml").write_text("routing:\n  code:\n    model: ollama/mine:7b\n")
    (state / "profile.yaml").write_text(
        "routing:\n  model_priority:\n    - tier: free_local\n      models: ['ollama/mine:7b']\n"
    )
    (state / "examples").mkdir()
    (state / "examples" / "classify-1.txt").write_text("fix this bug -> code\n")
    (state / "examples" / "classify-2.txt").write_text("what is rust -> query\n")
    con = sqlite3.connect(state / "usage.db")
    con.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY, model TEXT, cost REAL)")
    con.executemany("INSERT INTO usage (model, cost) VALUES (?, ?)",
                    [("ollama/mine:7b", 0.0)] * USAGE_ROWS)
    con.commit()
    con.close()

    # Claude Code (config dir is state/claude-home while LLM_ROUTER_HOME is set)
    hooks = state / "claude-home" / "hooks"
    hooks.mkdir(parents=True)
    for _src, dst_name, _event, _t in ih._HOOK_DEFS:
        (hooks / dst_name).write_text(OLD_HOOK)
    (state / "claude-home" / "settings.json").write_text(json.dumps({"theme": "dark-mine"}))

    # Codex and Gemini CLI, wired by 15.4.0
    shared = state / "hooks"
    shared.mkdir()
    for name in ("codex-auto-route.py", "codex-post-tool.py", "codex-stop.py",
                 "gemini-cli-auto-route.py", "gemini-cli-post-tool.py", "gemini-cli-session-end.py"):
        (shared / name).write_text(OLD_HOOK)
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text('model = "mine"\n')
    (home / ".gemini").mkdir()
    (home / ".gemini" / "settings.json").write_text(json.dumps({"theme": "gemini-mine"}))
    return state
