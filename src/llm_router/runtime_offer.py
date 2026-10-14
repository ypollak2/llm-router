"""Local runtime detection and the one install offer (PLAN v16 P2.8 task 2).

Every place that used to hint "install Ollama" now takes its text from
``runtime_offer()``. The install command runs only through
``install_runtime(confirm=True)``, which only the interactive ``llm-router
onboard`` calls after a human answers ``y`` at a terminal; the MCP
``llm_setup(action="install_runtime")`` only returns the command, because a flag
the model sets is not a human's approval. Nothing here raises.

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


def _health_url(key: str, fallback: str) -> str:
    """Health URL from local_platforms, so LLM_ROUTER_LOCAL_*_PORT overrides apply."""
    try:
        from llm_router.local_platforms import PLATFORM_MAP

        return PLATFORM_MAP[key].health_url
    except Exception as exc:  # noqa: BLE001
        log.debug("local_platforms lookup for %s failed: %s", key, exc)
        return fallback


def runtime_status(
    *,
    which: Callable[[str], str | None] = shutil.which,
    path_exists: Callable[[str], bool] = os.path.exists,
    http_ok: Callable[[str], bool] = _http_ok,
) -> dict[str, str]:
    """Local runtimes found, each ``"running"`` (its server answers) or ``"installed"``.

    Names: ollama, lmstudio, mlx, llamacpp. llama.cpp and MLX share port 8080, so
    a server answering there is MLX only when MLX, and not llama.cpp, is installed.
    """
    found: dict[str, str] = {}
    try:
        if http_ok(_health_url("ollama", "http://localhost:11434/api/tags")):
            found["ollama"] = "running"
        elif which("ollama"):
            found["ollama"] = "installed"
        if http_ok(_health_url("lmstudio", "http://localhost:1234/v1/models")):
            found["lmstudio"] = "running"
        elif path_exists("/Applications/LM Studio.app") or path_exists(
            os.path.expanduser("~/.lmstudio")
        ):
            found["lmstudio"] = "installed"
        mlx = bool(which("mlx_lm.server") or which("mlx_lm.generate"))
        llama = bool(which("llama-server") or which("llama-cli"))
        on_8080 = http_ok(_health_url("llamacpp", "http://localhost:8080/v1/models"))
        if mlx:
            found["mlx"] = "running" if on_8080 and not llama else "installed"
        if llama or (on_8080 and not mlx):
            found["llamacpp"] = "running" if on_8080 else "installed"
    except Exception as exc:  # noqa: BLE001
        log.debug("runtime detection failed: %s", exc)
    return found


def detect_runtimes(**kwargs) -> list[str]:
    """Names of the local runtimes found, running or only installed."""
    return list(runtime_status(**kwargs))


def runtime_offer(system: str | None = None, *, api_keys: bool = True) -> str:
    """The one text shown wherever no local model runtime was found.

    ``api_keys=False`` leaves out the API-key line for callers that list it themselves.
    """
    key = _system_key(system)
    if key:
        command = f"    {OLLAMA_INSTALL_COMMANDS[key]}"
    else:
        command = "\n".join(
            f"    {name}: {cmd}" for name, cmd in OLLAMA_INSTALL_COMMANDS.items()
        )
    text = (
        "No local model runtime was found. For free local routing, install one:\n"
        f"{command}\n"
        "Run it yourself, or run `llm-router onboard`, which asks before installing.\n"
        "LM Studio, MLX and llama.cpp are also supported if already installed."
    )
    if api_keys:
        text += "\nAPI keys work too: set GEMINI_API_KEY or OPENAI_API_KEY in ~/.llm-router/.env"
    return text


def install_runtime_hint(system: str | None = None) -> str:
    """What the MCP tool returns: the command, never a run of it."""
    return (
        runtime_offer(system)
        + "\nThe installer is not run from a tool call: it downloads and runs a system "
        "installer that may ask for your password, so it needs you at a terminal."
    )


def _default_runner(command: str) -> int:
    if "|" in command and shutil.which("bash"):
        # pipefail: a failed download must not exit 0 through `| sh`.
        argv: str | list[str] = ["bash", "-o", "pipefail", "-c", command]
    else:
        argv = command
    return subprocess.run(  # noqa: S602
        argv,
        shell=isinstance(argv, str),
        check=False,
        timeout=_INSTALL_TIMEOUT_S,
        env=get_safe_env(),
    ).returncode


def install_runtime(
    *,
    confirm: bool = False,
    runner: Callable[[str], int] | None = None,
    system: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    """Run the install command only when ``confirm is True``; else return the offer.

    Success means exit code 0 and an ``ollama`` binary on PATH afterwards.
    """
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
    if code != 0:
        return f"Install failed: `{command}` exited with code {code}."
    if not which("ollama"):
        return f"`{command}` finished, but `ollama` is still not on PATH. Check its output above."
    return f"Installed Ollama with `{command}`. Start it with `ollama serve`, then pull a model."


if __name__ == "__main__":
    print(runtime_offer())
