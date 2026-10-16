"""F1: importing llm_router.gateway must not mutate the host process env."""
import os
import subprocess
import sys

VAR = "LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS"


def _clean_env():
    env = {k: v for k, v in os.environ.items() if k != VAR}
    return env


def test_import_gateway_leaves_env_unset():
    code = (
        "import os, llm_router.gateway\n"
        f"print(repr(os.environ.get('{VAR}')))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], env=_clean_env(), capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "None"


def test_sdk_classify_path_leaves_env_unset():
    code = (
        "import os\n"
        "from llm_router import sdk\n"
        "import llm_router.gateway\n"
        f"print(repr(os.environ.get('{VAR}')))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], env=_clean_env(), capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "None"


def test_gateway_main_sets_env(monkeypatch):
    import uvicorn

    from llm_router import gateway, net_bind

    monkeypatch.delenv(VAR, raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(net_bind, "refuse_public_bind_or_exit", lambda *a, **k: None)
    gateway.main()
    assert os.environ.get(VAR) == "codex,gemini_cli"


def test_gateway_main_respects_operator_value(monkeypatch):
    import uvicorn

    from llm_router import gateway, net_bind

    monkeypatch.setenv(VAR, "")
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(net_bind, "refuse_public_bind_or_exit", lambda *a, **k: None)
    gateway.main()
    assert os.environ.get(VAR) == ""
