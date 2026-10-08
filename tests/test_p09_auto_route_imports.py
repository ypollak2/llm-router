"""PLAN v16 P0.9 task 5: auto-route's module import does not load what the
prompt path may never use.

M4.1 measured `import` (hook clock start to main()) at ~77% of slow auto-route
time [M41]. `python -X importtime` on the hook module (origin/main a0d8440, warm,
n = 3) put two module-level imports at ~150 ms of ~350 ms:

* ``llm_router.config`` (pydantic, ~105 ms) -- imported only for
  ``validate_ollama_url``, to check the Ollama URL of the classifier auto-detect.
  The default URL (``http://localhost:11434``) needs no check: the validator
  returns it unchanged, and the fallback is the same URL.
* ``llm_router.profiles`` (yaml + model_aliases, ~43 ms) -- read only by
  ``_get_selected_model``, after a route is decided.

Both are now imported where they are used.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "auto-route.py"

_PROBE = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("auto_route_probe", {hook!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(json.dumps({{m: m in sys.modules for m in {mods!r}}}))
"""

_HEAVY = ["llm_router.config", "llm_router.profiles", "pydantic", "yaml"]


def _import_in_fresh_process(tmp_path: Path, ollama_url: str | None) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
           "LLM_ROUTER_HOME": str(tmp_path / "state")}
    if ollama_url is not None:
        env["LLM_ROUTER_OLLAMA_URL"] = ollama_url
    (tmp_path / "state").mkdir(exist_ok=True)
    code = _PROBE.format(hook=str(HOOK), mods=_HEAVY)
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode()[-2000:]
    return json.loads(proc.stdout.decode().strip().splitlines()[-1])


def test_importing_the_hook_with_the_default_ollama_url_loads_neither_config_nor_profiles(tmp_path):
    loaded = _import_in_fresh_process(tmp_path, None)
    assert loaded == {m: False for m in _HEAVY}, loaded


def test_a_non_default_ollama_url_is_still_validated(tmp_path):
    """The security check (CHZ-SEC-06) still runs for any URL that is not the default."""
    loaded = _import_in_fresh_process(tmp_path, "http://localhost:9")
    assert loaded["llm_router.config"] is True


@pytest.mark.parametrize("raw,probed", [
    ("http://localhost:11434", "http://localhost:11434"),
    ("file:///etc/passwd", "http://localhost:11434"),
    ("http://169.254.169.254", "http://localhost:11434"),
    ("http://ollama.lan:11434", "http://ollama.lan:11434"),
])
def test_the_auto_detect_probe_url_is_unchanged(raw, probed):
    spec = importlib.util.spec_from_file_location("auto_route_url", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._auto_detect_probe_url(raw) == probed


def test_selected_model_still_reads_the_routing_table():
    from llm_router.profiles import ROUTING_TABLE
    from llm_router.types import RoutingProfile, TaskType

    spec = importlib.util.spec_from_file_location("auto_route_sel", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    model, provider = mod._get_selected_model("code", "complex")
    head = ROUTING_TABLE[(RoutingProfile.PREMIUM, TaskType.CODE)][0]
    want_provider = head.split("/")[0] if "/" in head else head
    assert provider == want_provider
    assert model == (f"ollama/{mod.OLLAMA_MODEL}" if want_provider == "ollama" else head)
    assert mod._get_selected_model("nonsense", "complex") == ("unknown", "unknown")
