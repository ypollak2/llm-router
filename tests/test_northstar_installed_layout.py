"""Phase 0.2d — northstar's exclusions must hold in an INSTALLED layout.

``northstar`` used to load ``scripts/groundtruth/sources.py`` and
``scripts/northstar/edit_survival.py`` by path, relative to ``__file__``. The
wheel ships only ``llm_router/`` (no ``scripts/``), so in a pip/npm install
both loaders silently returned None and every ground-truth exclusion
(benchmark-sandbox workspaces, synthetic sessions, harness artefacts) and the
edit-survival judgement switched off. Measured on real data, 30-day window:
653 sessions against 295, 2,137 user prompts against 1,108.

This test reproduces that layout: it copies the package to a temp directory
with no ``scripts/`` anywhere above it and runs northstar from the copy in a
fresh interpreter, then asserts the exclusions by their REASON.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import llm_router

SID_REAL = "aaaaaaaa-1111-2222-3333-444444444444"
SID_SANDBOX = "bbbbbbbb-1111-2222-3333-444444444444"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _user(sid: str, text: str, ts: float) -> dict:
    return {"parentUuid": None, "isSidechain": False, "type": "user", "uuid": str(ts),
            "timestamp": _iso(ts), "sessionId": sid,
            "message": {"role": "user", "content": text}}


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


_PROBE = """
import json, sys
from llm_router import northstar as ns
root = sys.argv[1]
units = list(ns.units(days=None, root=__import__("pathlib").Path(root)))
print(json.dumps({
    "module": ns.__file__,
    "sessions": sorted({u["session_id"] for u in units}),
    "user_prompts": sum(1 for u in units if u["kind"] == ns.UNIT_USER_PROMPT),
    "edits": [[u["outcome"], u["signal"]] for u in units if u["kind"] == ns.UNIT_ROUTED_EDIT],
}))
"""


def test_exclusions_hold_when_scripts_dir_is_absent(tmp_path):
    # An installed-wheel-like layout: <prefix>/site-packages/llm_router, and
    # no scripts/ at <prefix> (which is where parents[2] of northstar.py lands).
    prefix = tmp_path / "prefix"
    site = prefix / "site-packages"
    src_pkg = Path(llm_router.__file__).resolve().parent
    shutil.copytree(src_pkg, site / "llm_router",
                    ignore=shutil.ignore_patterns("__pycache__"))
    assert not (prefix / "scripts").exists()

    projects = tmp_path / "claude_projects"
    base = 1_800_000_000
    real = [_user(SID_REAL, f"please help with unrelated task number {i}", base + i)
            for i in range(5)]
    # classify_drop: a harness-injected turn is not a human prompt.
    real.append(_user(SID_REAL, "[Tool output] some pasted tool result text here", base + 10))
    _write_jsonl(projects / "-Users-x-proj" / f"{SID_REAL}.jsonl", real)
    # _SANDBOX_PROJECT: a /tmp benchmark workspace is dropped entirely.
    _write_jsonl(projects / "-private-tmp-bq-claude" / f"{SID_SANDBOX}.jsonl",
                 [_user(SID_SANDBOX, "what is the current value of MAX_VALUE", base)])

    home = tmp_path / "router_home"
    # edit_survival.judge_row: an applied edit whose file no longer exists is
    # "redone". Without the judge the row degrades to unknown.
    _write_jsonl(home / "edit_outcomes.jsonl", [
        {"ts": base + 1, "session_id": SID_REAL, "file": str(tmp_path / "gone.py"),
         "model": "ollama/qwen3.5", "applied": True, "survived": None},
    ])

    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR")}
    env.update(PYTHONPATH=str(site), LLM_ROUTER_HOME=str(home),
               CLAUDE_PROJECTS_DIR=str(projects), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", _PROBE, str(projects)],
                          capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert Path(out["module"]).resolve().is_relative_to(site.resolve()), out["module"]
    assert out["sessions"] == [SID_REAL], "sandbox workspace session was not excluded"
    assert out["user_prompts"] == 5, "harness-artefact turn was counted as a user prompt"
    assert out["edits"] == [["redo", "edit_ledger_redone"]], out["edits"]
