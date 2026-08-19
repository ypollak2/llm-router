"""Regression: CHZ-ST-001 (session_id path traversal) + CHZ-ST-003 (fail-open).

ST-001: per-session state files are named ``<prefix>_{session_id}`` under
``~/.llm-router``. An unsanitized session_id like ``../../tmp/evil`` escaped the
state dir → arbitrary file write. Every path builder now runs `_safe_sid`.

ST-003: the hook constructs a module-level PolicyManager at import, which
mkdir'd ``~/.llm-router/policies`` — a read-only state dir raised PermissionError
and crashed the hook (exit 1). It must fail OPEN (exit 0, no decision).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"


def _load():
    spec = importlib.util.spec_from_file_location("llm_router_auto_route_hook_iso", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load()


@pytest.mark.parametrize("evil", [
    "../../../tmp/evil",
    "../../etc/passwd",
    "a/b/c",
    "..",
    "foo/../../bar",
    "\x00nul",
])
def test_safe_sid_neutralizes_traversal(evil: str) -> None:
    safe = hook._safe_sid(evil)
    assert "/" not in safe, f"path separator survived sanitization of {evil!r}"
    assert "\x00" not in safe, "NUL byte survived sanitization"
    # The built path must stay inside the router dir.
    p = hook._last_route_path(evil).resolve()
    assert str(p).startswith(str(hook._ROUTER_DIR.resolve()) + os.sep), (
        f"CHZ-ST-001 regression: session_id {evil!r} escaped the state dir → {p}"
    )


def test_traversal_session_id_writes_no_file_outside_state_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".llm-router").mkdir(parents=True)
    outside = tmp_path / "OUTSIDE_MARKER.json"
    # session_id that, unsanitized, would resolve to `outside`
    evil_sid = f"../../{outside.stem}"
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1",
        "OPENAI_API_KEY": "", "GEMINI_API_KEY": "",
    })
    # A code prompt so the hook attempts to save last_route.
    payload = f'{{"prompt": "refactor the module please", "session_id": "{evil_sid}"}}'
    subprocess.run([sys.executable, str(HOOK_PATH)], input=payload, text=True,
                   capture_output=True, env=env, timeout=30)
    assert not outside.exists(), "CHZ-ST-001: traversal session_id wrote outside the state dir"
    # And parent dirs of tmp must not have gained a stray last_route_ file.
    strays = list(tmp_path.glob("last_route_*.json"))
    assert not strays, f"stray state files escaped: {strays}"


def test_readonly_state_dir_fails_open(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".llm-router").mkdir(parents=True)
    # Make the state dir read-only AFTER creation.
    os.chmod(home / ".llm-router", 0o500)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1",
        "OPENAI_API_KEY": "", "GEMINI_API_KEY": "",
    })
    try:
        proc = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input='{"prompt": "hello there how are you", "session_id": "ro-1"}',
            text=True, capture_output=True, env=env, timeout=30,
        )
    finally:
        os.chmod(home / ".llm-router", 0o700)  # restore so tmp cleanup works
    assert proc.returncode == 0, (
        "CHZ-ST-003 regression: read-only ~/.llm-router crashed the hook "
        f"(exit {proc.returncode}) instead of failing open.\nstderr:\n{proc.stderr[-500:]}"
    )
