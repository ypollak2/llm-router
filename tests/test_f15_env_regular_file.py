"""F15: a non-regular ``.env`` (FIFO, directory) is never opened.

pydantic-settings >= 2.15 opens FIFOs; ``open()`` on a FIFO with no writer blocks
forever, which hung the UserPromptSubmit hook (session_store.get_mode ->
config_lite.config_value -> get_config). Each case runs in a subprocess under a
hard timeout so a regression fails instead of wedging the suite.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from llm_router import paths

pytestmark = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")

_SNIPPETS = {
    "get_config": "from llm_router.config import get_config; print(get_config().llm_router_profile)",
    "config_value": (
        "from llm_router import config_lite; "
        "print(config_lite.config_value('llm_router_persist_ttl_days'))"
    ),
    "hook_get_mode": (
        "from llm_router import session_store; print(session_store.get_mode())"
    ),
    "could_be_set_then_real": (
        "from llm_router import config_lite; "
        "import llm_router.config; print(config_lite.config_value('session_context_enabled'))"
    ),
}


def _run(snippet: str, cwd, home) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "LLM_ROUTER_HOME": str(home / ".llm-router"),
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
    }
    return subprocess.run(
        [sys.executable, "-c", snippet], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=5,
    )


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".llm-router").mkdir(parents=True)
    return h


@pytest.fixture
def cwd(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    return d


@pytest.mark.parametrize("name", sorted(_SNIPPETS))
def test_fifo_dotenv_in_cwd_does_not_hang(name, cwd, home):
    os.mkfifo(cwd / ".env")
    r = _run(_SNIPPETS[name], cwd, home)  # TimeoutExpired == hang == fail
    assert r.returncode == 0, r.stderr[-800:]


def test_fifo_state_dotenv_does_not_hang(cwd, home):
    os.mkfifo(home / ".llm-router" / ".env")
    r = _run(_SNIPPETS["get_config"], cwd, home)
    assert r.returncode == 0, r.stderr[-800:]


def test_directory_named_dotenv_is_ignored(cwd, home):
    (cwd / ".env").mkdir()
    r = _run(_SNIPPETS["get_config"], cwd, home)
    assert r.returncode == 0, r.stderr[-800:]


def test_regular_dotenv_still_read(cwd, home):
    (cwd / ".env").write_text("LLM_ROUTER_PERSIST_TTL_DAYS=7\n")
    r = _run(_SNIPPETS["config_value"], cwd, home)
    assert r.returncode == 0, r.stderr[-800:]
    assert r.stdout.strip() == "7"


def test_regular_file_or_none(tmp_path):
    f = tmp_path / "a"
    f.write_text("x")
    os.mkfifo(tmp_path / "p")
    (tmp_path / "d").mkdir()
    assert paths.regular_file_or_none(f) == f
    assert paths.regular_file_or_none(tmp_path / "p") is None
    assert paths.regular_file_or_none(tmp_path / "d") is None
    assert paths.regular_file_or_none(tmp_path / "missing") is None
