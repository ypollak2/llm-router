"""Local runtime detection and the one install offer (PLAN v16 P2.8 task 2).

Every place that used to hint "install Ollama" now takes its text from
``runtime_offer()``. The install command runs only through
``install_runtime(confirm=True)``; without ``confirm=True`` the runner is never
called. Nothing here raises.

Run ``python3 -m llm_router.runtime_offer`` to print the text (the shell hook does).
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import urllib.request
from typing import Callable

from llm_router.safe_subprocess import get_safe_env

log = logging.getLogger(__name__)

OLLAMA_INSTALL_COMMANDS = {
    "darwin": "brew install ollama",
    "linux": "curl -fsSL https://ollama.com/install.sh | sh",
    "windows": "winget install Ollama.Ollama",
}

# openai_compat runtimes: base URL the router can use once they are running.
RUNTIME_BASE_URLS = {
    "lmstudio": "http://localhost:1234/v1",
    "llamacpp": "http://localhost:8080/v1",
    "mlx": "http://localhost:8080/v1",
}

_INSTALL_TIMEOUT_S = 900


def _system_key(system: str | None = None) -> str | None:
    """Map ``platform.system()`` to a key of OLLAMA_INSTALL_COMMANDS, or None."""
    try:
        name = (system if system is not None else platform.system()).strip().lower()
    except Exception as exc:  # noqa: BLE001
        log.debug("platform.system failed: %s", exc)
        return None
    return name if name in OLLAMA_INSTALL_COMMANDS else None


def _http_ok(url: str) -> bool:
    """True when a loopback GET answers 2xx within 1 s. Never raises."""
    try:
        with urllib.request.urlopen(url, timeout=1) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001
        log.debug("probe %s failed: %s", url, exc)
        return False


def detect_runtimes(
    *,
    which: Callable[[str], str | None] = shutil.which,
    path_exists: Callable[[str], bool] = os.path.exists,
    http_ok: Callable[[str], bool] = _http_ok,
    system: str | None = None,
) -> list[str]:
    """Names of the local runtimes found: ollama, lmstudio, mlx, llamacpp."""
    found: list[str] = []
    try:
        if which("ollama") or http_ok("http://localhost:11434/api/tags"):
            found.append("ollama")
        if (
            http_ok("http://localhost:1234/v1/models")
            or path_exists("/Applications/LM Studio.app")
            or path_exists(os.path.expanduser("~/.lmstudio"))
        ):
            found.append("lmstudio")
        if which("mlx_lm.server") or which("mlx_lm.generate"):
            found.append("mlx")
        if (
            which("llama-server")
            or which("llama-cli")
            or http_ok("http://localhost:8080/health")
        ):
            found.append("llamacpp")
    except Exception as exc:  # noqa: BLE001
        log.debug("runtime detection failed: %s", exc)
    return found


def runtime_offer(system: str | None = None) -> str:
    """The one text shown wherever no local model runtime was found."""
    key = _system_key(system)
    if key:
        command = f"    {OLLAMA_INSTALL_COMMANDS[key]}"
    else:
        command = "\n".join(
            f"    {name}: {cmd}" for name, cmd in OLLAMA_INSTALL_COMMANDS.items()
        )
    return (
        "No local model runtime was found. For free local routing, install one:\n"
        f"{command}\n"
        "It runs only after you approve: "
        'llm_setup(action="install_runtime", confirm=True) or `llm-router onboard`.\n'
        "LM Studio, MLX and llama.cpp are also supported if already installed.\n"
        "API keys work too: set GEMINI_API_KEY or OPENAI_API_KEY in ~/.llm-router/.env"
    )


def _default_runner(command: str) -> int:
    return subprocess.run(  # noqa: S602
        command,
        shell=True,
        check=False,
        timeout=_INSTALL_TIMEOUT_S,
        env=get_safe_env(),
    ).returncode


def install_runtime(
    *,
    confirm: bool = False,
    runner: Callable[[str], int] | None = None,
    system: str | None = None,
) -> str:
    """Run the install command only when ``confirm is True``; else return the offer."""
    if confirm is not True:
        return runtime_offer(system)
    key = _system_key(system)
    if key is None:
        return "Unsupported OS for automatic install.\n" + runtime_offer(system)
    command = OLLAMA_INSTALL_COMMANDS[key]
    run = runner if runner is not None else _default_runner
    try:
        code = run(command)
    except Exception as exc:  # noqa: BLE001
        log.debug("install runner failed: %s", exc)
        return f"Install failed: `{command}` raised {type(exc).__name__}: {exc}"
    return f"Ran `{command}` (exit code {code})."


if __name__ == "__main__":
    print(runtime_offer())
