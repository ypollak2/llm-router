"""PLAN v16 P0.9 task 5: auto-route's module import does not load what the
prompt path may never use.

M4.1 measured `import` (hook clock start to main()) at ~77% of slow auto-route
time [M41]. `python -X importtime` on the hook module (a0d8440, warm, n = 3) put
``llm_router.profiles`` (yaml + model_aliases) at ~43 ms of ~350 ms. It is read
only by ``_get_selected_model``, after a route is decided, and is now imported
there. (The other large module-level import, ``llm_router.config`` for the
classifier auto-detect, was removed with that auto-detect by P0.7-c, #305; it is
pinned here so it does not come back.)
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "auto-route.py"

_PROBE = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("auto_route_probe", {hook!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(json.dumps({{m: m in sys.modules for m in {mods!r}}}))
"""

_HEAVY = ["llm_router.profiles", "llm_router.model_aliases", "yaml", "llm_router.config", "pydantic"]


def test_importing_the_hook_loads_neither_profiles_nor_config(tmp_path):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
           "LLM_ROUTER_HOME": str(tmp_path / "state")}
    (tmp_path / "state").mkdir()
    code = _PROBE.format(hook=str(HOOK), mods=_HEAVY)
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode()[-2000:]
    loaded = json.loads(proc.stdout.decode().strip().splitlines()[-1])
    assert loaded == {m: False for m in _HEAVY}, loaded


def test_selected_model_still_reads_the_routing_table():
    from llm_router.profiles import ROUTING_TABLE
    from llm_router.types import RoutingProfile, TaskType

    spec = importlib.util.spec_from_file_location("auto_route_sel", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for task, cx, profile, tt in (("code", "complex", RoutingProfile.PREMIUM, TaskType.CODE),
                                  ("query", "simple", RoutingProfile.BUDGET, TaskType.QUERY)):
        head = ROUTING_TABLE[(profile, tt)][0]
        want_provider = head.split("/")[0] if "/" in head else head
        model, provider = mod._get_selected_model(task, cx)
        assert provider == want_provider
        assert model == (f"ollama/{mod.OLLAMA_MODEL}" if want_provider == "ollama" else head)
    assert mod._get_selected_model("nonsense", "complex") == ("unknown", "unknown")
