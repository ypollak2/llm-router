"""PLAN v16 PG4 / P2-G-3: the auto-route hook's per-prompt work, counted not timed.

Measured on a fresh clone (docs/bugs/AUTOROUTE-LAT-1.md): the hook's CPU time per prompt
fell by about a third, and its slow tail was a synchronous Ollama probe at import plus
three heavy imports (pydantic-settings via ``llm_router.config``, structlog + rich via
``llm_router.logging``, the SDK via the package ``__init__``). Wall-clock is too noisy
on a shared machine to assert on, so these tests count what the host actually waits on:
modules imported, network connects, processes spawned, work done on the sync path.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
HOOK = SRC / "llm_router" / "hooks" / "auto-route.py"

#: What a routed prompt must NOT import. Each was measured with ``python -X importtime``
#: at 6-45 ms on the hook's path; none is needed to decide a route and print the hint.
HEAVY = [
    "structlog", "rich", "pydantic", "pydantic_settings", "llm_router.config", "llm_router.sdk",
    "llm_router.response_router", "importlib.metadata", "urllib.request", "http.client",
]

PAYLOAD = {
    "hook_event_name": "UserPromptSubmit", "session_id": "pg4-fixture",
    "cwd": "/Users/dev/llm-router",
    "prompt": "Refactor the retry logic in the proxy server so that it backs off "
              "exponentially, and add a unit test for the failure path.",
}


def _env(home: Path) -> dict[str, str]:
    (home / "state").mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home), "LLM_ROUTER_HOME": str(home / "state"), "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(SRC), "LLM_ROUTER_SYNTHETIC": "1", "LANG": "en_US.UTF-8",
        # A closed port: any probe fails fast instead of reaching a real daemon.
        "OLLAMA_HOST": "http://127.0.0.1:9",
    }


# Runs the hook as __main__ (the way the host does) and reports, on stderr's last line,
# what it loaded and which network / process calls it made.
_DRIVER = textwrap.dedent(
    """
    import json, runpy, socket, subprocess, sys
    calls = {"connect": 0, "popen": 0}
    _connect = socket.socket.connect
    def connect(self, *a, **k):
        calls["connect"] += 1
        return _connect(self, *a, **k)
    socket.socket.connect = connect
    _popen = subprocess.Popen.__init__
    def popen(self, *a, **k):
        calls["popen"] += 1
        return _popen(self, *a, **k)
    subprocess.Popen.__init__ = popen
    heavy = json.loads(sys.argv[2])
    sys.argv = [sys.argv[1]]
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    except SystemExit:
        pass
    loaded = [m for m in heavy if m in sys.modules]
    sys.stderr.write("\\nPG4:" + json.dumps({"loaded": loaded, **calls}) + "\\n")
    """
)


def _run_hook(tmp_path: Path, payload: dict = PAYLOAD, extra_env: dict | None = None) -> dict:
    env = _env(tmp_path)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(HOOK), json.dumps(HEAVY)],
        input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=120,
    )
    line = next((x for x in proc.stderr.splitlines() if x.startswith("PG4:")), None)
    assert line, f"driver reported nothing (rc={proc.returncode}): {proc.stderr[-600:]}"
    assert proc.stdout.strip(), f"hook printed nothing: {proc.stderr[-400:]}"
    json.loads(proc.stdout.splitlines()[-1])  # stdout is still the JSON the host parses
    return json.loads(line[4:])


def test_a_routed_prompt_imports_none_of_the_heavy_modules(tmp_path):
    first = _run_hook(tmp_path)
    second = _run_hook(tmp_path)  # second prompt of a session: caches and files exist now
    assert first["loaded"] == [], f"heavy modules imported on the sync path: {first['loaded']}"
    assert second["loaded"] == [], f"heavy modules imported on the sync path: {second['loaded']}"


def test_a_routed_prompt_makes_no_network_connect_with_a_cold_discovery_cache(tmp_path):
    # No discovery.json: the hook used to probe Ollama synchronously, here and on every
    # later prompt while the probe kept failing.
    assert not (tmp_path / "state" / "discovery.json").exists()
    report = _run_hook(tmp_path)
    assert report["connect"] == 0, "the sync path made a network connect"


def test_a_cold_discovery_cache_costs_one_detached_child_per_window(tmp_path):
    first = _run_hook(tmp_path)
    second = _run_hook(tmp_path)
    assert (tmp_path / "state" / "discovery.probe_attempt").exists()
    # The only process the hook may start on a fresh state dir is the detached probe, and
    # only on the first prompt: the attempt stamp throttles the second.
    assert second["popen"] < first["popen"], (first, second)


def test_the_fixture_prompt_is_actually_routed(tmp_path):
    """Guard against a vacuous pass: the driver must have run the full routing path."""
    env = _env(tmp_path)
    proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(PAYLOAD),
                          capture_output=True, text=True, env=env, timeout=120)
    ctx = json.loads(proc.stdout.splitlines()[-1])["hookSpecificOutput"]["additionalContext"]
    assert "ROUTE:" in ctx and "llm(task=" in ctx
    rows = [json.loads(x) for x in (tmp_path / "state" / "hook_latency.jsonl").read_text().splitlines()]
    assert {"import", "classify"} <= set(rows[-1]["phases_ms"]), rows[-1]


# ── package __init__: nothing eager ──────────────────────────────────────────


def _py(code: str, tmp_path: Path) -> str:
    proc = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True,
                          text=True, env=_env(tmp_path), timeout=120)
    assert proc.returncode == 0, proc.stderr[-800:]
    return proc.stdout.strip()


def test_importing_the_package_imports_neither_the_sdk_nor_importlib_metadata(tmp_path):
    out = _py(
        """
        import sys, llm_router.paths
        print(json_ := [m for m in ("llm_router.sdk", "llm_router.response_router", "importlib.metadata")
                        if m in sys.modules])
        """,
        tmp_path,
    )
    assert out == "[]"


def test_the_package_exports_and_version_still_resolve_on_first_use(tmp_path):
    out = _py(
        """
        import sys, llm_router
        from llm_router import route, RouteResult, RoutingError
        assert llm_router.route is route and callable(route)
        assert issubclass(RoutingError, Exception) and RouteResult
        assert callable(llm_router.route_response_explanations)
        assert isinstance(llm_router.__version__, str) and llm_router.__version__[0].isdigit()
        assert {"route", "__version__"} <= set(dir(llm_router))
        try:
            llm_router.no_such_name
        except AttributeError:
            print("ok")
        """,
        tmp_path,
    )
    assert out == "ok"


# ── config_lite ───────────────────────────────────────────────────────────────


def test_config_lite_defaults_are_the_declared_routerconfig_defaults():
    from llm_router.config import RouterConfig
    from llm_router.config_lite import DEFAULTS

    declared = {name: RouterConfig.model_fields[name].default for name in DEFAULTS}
    assert declared == DEFAULTS


def test_config_lite_skips_the_import_when_nothing_can_set_the_field(tmp_path):
    out = _py(
        """
        import sys
        from llm_router.config_lite import config_value
        v = config_value("session_context_max_tokens_draft")
        print(v, "llm_router.config" in sys.modules, "pydantic_settings" in sys.modules)
        """,
        tmp_path,
    )
    assert out == "3000 False False"


@pytest.mark.parametrize("how", ["env", "state_env_file", "cwd_env_file"])
def test_config_lite_takes_the_real_path_when_something_can_set_the_field(tmp_path, how):
    env = _env(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    if how == "env":
        env["SESSION_CONTEXT_MAX_TOKENS_DRAFT"] = "1234"
    elif how == "state_env_file":
        (tmp_path / "state" / ".env").write_text("SESSION_CONTEXT_MAX_TOKENS_DRAFT=1234\n")
    else:
        (cwd / ".env").write_text("SESSION_CONTEXT_MAX_TOKENS_DRAFT=1234\n")
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys\nfrom llm_router.config_lite import config_value\n"
         "print(config_value('session_context_max_tokens_draft'), 'llm_router.config' in sys.modules)"],
        capture_output=True, text=True, env=env, cwd=cwd, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-600:]
    assert proc.stdout.split() == ["1234", "True"]


# ── lazy logging keeps stdout clean ──────────────────────────────────────────


def test_lazy_logging_configures_structlog_the_moment_anything_imports_it(tmp_path):
    out = _py(
        """
        import sys, io
        from llm_router.logging import configure_logging_lazily, get_logger
        configure_logging_lazily()
        log = get_logger("pg4.test")                       # no structlog import yet
        assert "structlog" not in sys.modules
        log.debug("dropped below the stdlib level")        # still no import
        assert "structlog" not in sys.modules, "a filtered call imported structlog"
        import structlog                                   # a DIRECT importer, as failopen does
        cfg = structlog.get_config()
        assert cfg["wrapper_class"] is structlog.stdlib.BoundLogger, cfg["wrapper_class"]
        real_stdout = sys.stdout
        sys.stdout = io.StringIO()
        log.warning("must not reach stdout")
        structlog.get_logger("pg4.direct").warning("must not reach stdout either")
        leaked = sys.stdout.getvalue()
        sys.stdout = real_stdout
        print("leaked=" + repr(leaked))
        """,
        tmp_path,
    )
    assert out.splitlines()[-1] == "leaked=''"


def test_lazy_logging_leaves_an_unconfigured_process_alone(tmp_path):
    # Without configure_logging_lazily the filtered-call shortcut must stay off:
    # structlog's default logger prints debug lines, and that behaviour is unchanged.
    out = _py(
        """
        import sys
        from llm_router.logging import get_logger
        log = get_logger("pg4.plain")
        log.debug("x")
        print("structlog" in sys.modules)
        """,
        tmp_path,
    )
    assert out.splitlines()[-1] == "True"


# ── model discovery: the hook never waits on Ollama ──────────────────────────


def _write_cache(path: Path, models: list[str], age_s: float) -> None:
    path.write_text(json.dumps({
        "cached_at": time.time() - age_s,
        "models": {f"ollama/{m}": {"provider": "ollama"} for m in models},
    }))


@pytest.fixture
def md(monkeypatch, tmp_path):
    from llm_router import model_discovery

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    for var in ("LLM_ROUTER_OLLAMA_MODEL", "OLLAMA_BUDGET_MODELS", "OLLAMA_MODELS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(model_discovery, "probe_ollama", lambda: pytest.fail("the no-wait path probed"))
    spawned: list[int] = []
    monkeypatch.setattr(model_discovery, "_spawn_probe_child", lambda: spawned.append(1) or True)
    model_discovery.spawned = spawned
    return model_discovery


def test_nowait_stale_cache_is_used_and_refreshed_once_per_window(md, tmp_path):
    _write_cache(tmp_path / "discovery.json", ["old:7b"], age_s=3 * 24 * 3600)
    assert md.available_ollama_models_nowait() == ["old:7b"]
    assert md.available_ollama_models_nowait() == ["old:7b"]
    assert md.spawned == [1], "one refresh child per window, not one per prompt"


def test_nowait_missing_cache_is_empty_and_still_refreshes(md):
    assert md.available_ollama_models_nowait() == []
    assert md.spawned == [1]


def test_nowait_fresh_cache_and_env_override_spawn_nothing(md, tmp_path, monkeypatch):
    _write_cache(tmp_path / "discovery.json", ["fresh:7b"], age_s=60)
    assert md.available_ollama_models_nowait() == ["fresh:7b"]
    monkeypatch.setenv("OLLAMA_MODELS", "a:1,b:2")
    assert md.available_ollama_models_nowait() == ["a:1", "b:2"]
    assert md.spawned == []


def test_nowait_refresh_false_never_spawns(md):
    assert md.available_ollama_models_nowait(refresh=False) == []
    assert md.spawned == []


def test_the_attempt_window_reopens_after_retry_seconds(md):
    t0 = 1_000_000.0
    assert md._claim_probe_attempt(t0) is True
    assert md._claim_probe_attempt(t0 + md.PROBE_RETRY_S - 1) is False
    assert md._claim_probe_attempt(t0 + md.PROBE_RETRY_S + 1) is True


def test_available_ollama_models_still_probes_for_every_other_caller(monkeypatch, tmp_path):
    # chain_builder, session-start and the CLI keep the probing resolver.
    from llm_router import model_discovery

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    for var in ("LLM_ROUTER_OLLAMA_MODEL", "OLLAMA_BUDGET_MODELS", "OLLAMA_MODELS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(model_discovery, "probe_ollama", lambda: ["today:latest"])
    assert model_discovery.available_ollama_models() == ["today:latest"]


def test_importing_the_hook_as_a_module_never_starts_the_refresh_child(tmp_path):
    out = _py(
        f"""
        import importlib.util, subprocess
        spawned = []
        real = subprocess.Popen.__init__
        subprocess.Popen.__init__ = lambda self, *a, **k: spawned.append(a) or real(self, *a, **k)
        spec = importlib.util.spec_from_file_location("_ar_pg4", {str(HOOK)!r})
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        print(len(spawned))
        """,
        tmp_path,
    )
    assert out.splitlines()[-1] == "0"
