"""Every prompt's fate must be recorded, and the rate must exclude test runs.

Both invariants exist because of a 2026-09-13 investigation that produced two
wrong answers before the right one:

  * `ENFORCE=off`/`shadow` skipped routing while logging NOTHING. That branch was
    28.5% of one day's prompts and invisible in every report.
  * Test-suite runs wrote `session_id=unknown` into the production log — 1,037 of
    1,938 entries on one day — so every rate computed from it was wrong by about
    a factor of two, in the direction that looks like a regression.

These are cheap tests guarding expensive mistakes. Do not delete one to make a
change pass; add the logging instead.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"

TERMINAL = ("DIRECT:", "DIRECT SKIP:", "BYPASS", "CONTINUATION")


def _run_hook(prompt: str, home: Path, **env_extra) -> str:
    """Run the hook with HOME redirected, return its debug log."""
    env = dict(os.environ)
    env.update({"HOME": str(home), "LLM_ROUTER_HOME": str(home / ".llm-router")})
    env.pop("PYTEST_CURRENT_TEST", None)      # we want the production path here
    env.update(env_extra)
    payload = json.dumps({"session_id": "outcometest", "prompt": prompt,
                          "cwd": str(ROOT), "transcript_path": ""})
    subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True,
                   text=True, env=env, timeout=180)
    log = home / ".llm-router" / "auto-route-debug.log"
    return log.read_text(errors="replace") if log.exists() else ""


@pytest.mark.parametrize("mode", ["smart", "advise", "off", "shadow"])
def test_every_prompt_logs_exactly_one_outcome(tmp_path, mode):
    """No enforce mode may make a prompt vanish from the log.

    `off` and `shadow` used to produce prompt_len= and then silence.
    """
    text = _run_hook("what is the capital of Portugal?", tmp_path,
                     LLM_ROUTER_ENFORCE=mode)
    assert "prompt_len=" in text, f"mode={mode}: the hook did not run"
    found = [t for t in TERMINAL if t in text]
    assert found, (
        f"mode={mode}: prompt logged but NO terminal outcome. This is the exact "
        f"silent branch that cost a day of investigation.\n{text[-600:]}"
    )


def test_enforce_off_names_its_reason(tmp_path):
    """Not just 'a line exists' — the reason must be identifiable.

    The env value and the internal mode name differ: `LLM_ROUTER_ENFORCE=off`
    resolves to `_enforce_mode = "shadow"` (auto-route.py:3594). The log records
    the INTERNAL mode, which is the one that decided the outcome, so asserting
    on the env string would be asserting the wrong name.
    """
    text = _run_hook("what is 2+2?", tmp_path, LLM_ROUTER_ENFORCE="off")
    assert "enforcement disabled" in text, text[-600:]
    assert ("mode=shadow" in text or "mode=off" in text), text[-600:]


def test_pytest_runs_do_not_touch_the_production_log(tmp_path, monkeypatch):
    """With PYTEST_CURRENT_TEST set, writes go to the test log."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x (call)")
    spec = importlib.util.spec_from_file_location("ar_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ar_hook"] = mod
    spec.loader.exec_module(mod)
    path = mod._debug_log_path()
    assert path.name == "auto-route-debug.test.log", path
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    assert mod._debug_log_path().name == "auto-route-debug.log"


# ── the rate calculator ──────────────────────────────────────────────────────

def _rate_script(log: Path, *extra) -> str:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "routing_rate.py"),
         "--file", str(log), *extra],
        capture_output=True, text=True, timeout=60).stdout


def _fixture(path: Path, real_ok: int, real_total: int, unknown: int) -> None:
    lines = []
    n = 0
    for i in range(real_total):
        n += 1
        lines += [f"[2026-09-01 10:00:00] [INVOCATION START] ID={n}.0",
                  f"[2026-09-01 10:00:00] [INVOCATION {n}.0] prompt_len=20 session_id=3e33e160"]
        lines.append(f"[2026-09-01 10:00:00] [INVOCATION {n}.0] "
                     + ("DIRECT SUCCESS: model=ollama/x latency=10ms" if i < real_ok
                        else "DIRECT SKIP: context-dependent prompt"))
    for _ in range(unknown):
        n += 1
        lines += [f"[2026-09-01 10:00:00] [INVOCATION START] ID={n}.0",
                  f"[2026-09-01 10:00:00] [INVOCATION {n}.0] prompt_len=20 session_id=unknown",
                  f"[2026-09-01 10:00:00] [INVOCATION {n}.0] OUTPUTTING: tool=llm_query"]
    path.write_text("\n".join(lines) + "\n")


def test_rate_excludes_unknown_sessions(tmp_path):
    """60 of 100 real prompts routed; 900 test prompts must not dilute it.

    Counting them yields 6%, which is the error that was reported twice as a
    regression.
    """
    log = tmp_path / "f.log"
    _fixture(log, real_ok=60, real_total=100, unknown=900)
    out = _rate_script(log)
    assert "60.0%" in out, out
    assert "6.0%" not in out, "test-suite prompts diluted the denominator:\n" + out
    assert "excluded 900" in out, out


def test_rate_refuses_to_headline_a_small_sample(tmp_path):
    """20 prompts is not a measurement, however tempting the number."""
    log = tmp_path / "f.log"
    _fixture(log, real_ok=1, real_total=20, unknown=0)
    out = _rate_script(log)
    assert "too few to tell" in out, out
    assert "5.0%" not in out.split("overall")[0], out


def test_rate_surfaces_unlogged_prompts_as_a_bug(tmp_path):
    """A prompt with no terminal outcome is a hook defect, not a routing choice."""
    log = tmp_path / "f.log"
    log.write_text(
        "[2026-09-01 10:00:00] [INVOCATION START] ID=1.0\n"
        "[2026-09-01 10:00:00] [INVOCATION 1.0] prompt_len=20 session_id=3e33e160\n"
        "[2026-09-01 10:00:00] [INVOCATION 1.0] OUTPUT COMPLETE\n")
    out = _rate_script(log, "--min-sample", "1")
    assert "UNLOGGED" in out, out
    assert "bug in the" in out, out
