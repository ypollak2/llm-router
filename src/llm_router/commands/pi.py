"""`llm-router pi` -- run the Pi coding agent on a local Ollama model with the
llm-router local agent profile (integrations/pi/).

Pi (https://pi.dev, npm `@earendil-works/pi-coding-agent`) was the best of three
local harness routes in the 2026-10-04 harness-parity probes: 8 of 11 required
capabilities at >=19/20 with qwen3.6, all local. The profile fixes the gaps those
probes found (cancellation, write paths, questions, sub-agents, compaction,
silent truncation, images); integrations/pi/README.md lists each fix with the
measurement behind it.

This command only assembles and starts the run. It:
  1. resolves the profile directory (env override, packaged copy, source tree);
  2. writes a Pi agent directory under $LLM_ROUTER_HOME/pi/agent: a models.json
     for the one Ollama model (context window from the server; images only with
     --vision),
     the profile's settings.json and sub-agent definitions;
  3. execs `pi --offline --provider ollama --model M` with every profile
     extension and the profile's system rules.
It never changes Ollama's settings and never starts a model: `/api/show` and
`/api/ps` are read-only and do not load anything.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

#: Loaded in the main agent, in this order.
EXTENSIONS = ("cancel.ts", "paths.ts", "context-guard.ts", "compaction.ts", "question.ts", "subagent.ts")
#: Loaded in sub-agents: no `subagent` (no recursion) and no `question` (nobody sees it).
CHILD_EXTENSIONS = ("cancel.ts", "paths.ts", "context-guard.ts", "compaction.ts")
SYSTEM_RULES = "system-rules.md"
DEFAULT_MAX_OUTPUT = 8192
#: The Pi version the profile was built and measured against. Others get a warning.
TESTED_PI_VERSION = "0.99."
_SHOW_TIMEOUT_S = 3.0


class ProfileError(RuntimeError):
    """The profile or Pi cannot be found; the message says what to do."""


def resolve_profile_dir() -> Path:
    """Where the profile files live. LLM_ROUTER_PI_PROFILE_DIR, then the copy packaged
    in the wheel, then a source checkout. Each candidate must contain extensions/."""
    candidates = []
    override = os.environ.get("LLM_ROUTER_PI_PROFILE_DIR", "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    here = Path(__file__).resolve()
    candidates.append(here.parents[1] / "_pi_profile")          # wheel: llm_router/_pi_profile
    candidates.append(here.parents[3] / "integrations" / "pi")  # source: <repo>/integrations/pi
    for c in candidates:
        if (c / "extensions" / EXTENSIONS[0]).is_file():
            return c
    raise ProfileError(
        "llm-router pi: the Pi profile files were not found (looked in "
        + ", ".join(str(c) for c in candidates)
        + "). Set LLM_ROUTER_PI_PROFILE_DIR to a checkout's integrations/pi."
    )


def find_pi() -> list[str]:
    """argv prefix that starts Pi: LLM_ROUTER_PI_BIN, else `pi` on PATH."""
    explicit = os.environ.get("LLM_ROUTER_PI_BIN", "").strip()
    if explicit:
        return [explicit]
    found = shutil.which("pi")
    if found:
        return [found]
    raise ProfileError(
        "llm-router pi: the `pi` command was not found. Install it with "
        "`npm install -g @earendil-works/pi-coding-agent`, or set LLM_ROUTER_PI_BIN."
    )


def pi_version(pi_cmd: list[str]) -> str | None:
    """`pi --version`, or None when it cannot be read."""
    import subprocess

    try:
        out = subprocess.run(pi_cmd + ["--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    v = (out.stdout or "").strip().splitlines()
    return v[0].strip() if out.returncode == 0 and v else None


def ollama_base_url() -> str:
    from llm_router.config import validate_ollama_url

    raw = os.environ.get("OLLAMA_BASE_URL", "") or os.environ.get("OLLAMA_URL", "") or "http://localhost:11434"
    url = validate_ollama_url(raw.strip().rstrip("/"))
    if not url:
        raise ProfileError(f"llm-router pi: refusing the Ollama URL {raw!r} (not a safe http(s) URL).")
    return url.rstrip("/")


def model_capabilities(base_url: str, model: str) -> list[str] | None:
    """The model's capability list from Ollama's /api/show (does not load the model).
    None when the server cannot say -- unknown, not "no capabilities"."""
    req = urllib.request.Request(
        f"{base_url}/api/show", data=json.dumps({"model": model}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_SHOW_TIMEOUT_S) as resp:
            body = json.loads(resp.read())
    except (OSError, ValueError):
        return None
    caps = body.get("capabilities")
    return [str(c) for c in caps] if isinstance(caps, list) else None


def resolve_context(base_url: str, model: str, explicit: int | None) -> tuple[int, str]:
    """Context window Pi must plan for: --context, LLM_ROUTER_PI_CONTEXT, then what the
    server reports or is configured with (llm_router.local_context_guard)."""
    if explicit:
        return int(explicit), "--context"
    raw = os.environ.get("LLM_ROUTER_PI_CONTEXT", "").strip()
    if raw:
        try:
            if int(raw) > 0:
                return int(raw), "LLM_ROUTER_PI_CONTEXT"
        except ValueError:
            raise ProfileError(f"llm-router pi: LLM_ROUTER_PI_CONTEXT={raw!r} is not a number.") from None
    from llm_router.local_context_guard import effective_window

    return effective_window(base_url=base_url, model=model)


def build_models_json(base_url: str, model: str, context_window: int, vision: bool,
                      max_output: int = DEFAULT_MAX_OUTPUT) -> dict:
    return {
        "providers": {
            "ollama": {
                "baseUrl": f"{base_url}/v1",
                "api": "openai-completions",
                "apiKey": "ollama",
                "models": [{
                    "id": model,
                    "name": model,
                    # Images are declared only on request (--vision). Measured 2026-10-04,
                    # qwen3.6:35b-a3b-coding: declared text-only, Pi drops the image and the
                    # model says it cannot see it (10/10 explicit); declared with images,
                    # the model returned a confident wrong code in 20/20 trials, although
                    # Ollama lists "vision" for it. A silent wrong answer is worse than an
                    # explicit "cannot see", so a server capability alone does not enable it.
                    "input": ["text", "image"] if vision else ["text"],
                    "contextWindow": int(context_window),
                    # Pi's field is maxTokens; unknown keys (an earlier "maxOutput") are
                    # silently ignored and Pi then asks for 16384.
                    "maxTokens": int(min(max_output, max(1024, context_window // 4))),
                    "reasoning": False,
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                }],
            }
        }
    }


def prepare_agent_dir(agent_dir: Path, profile: Path, models: dict) -> None:
    """Write the Pi agent directory. Profile-owned files are overwritten on every run so
    the profile cannot drift; anything else in the directory (auth.json, sessions) is kept."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "models.json").write_text(json.dumps(models, indent=1) + "\n", encoding="utf-8")
    shutil.copyfile(profile / "agent" / "settings.json", agent_dir / "settings.json")
    agents = agent_dir / "agents"
    agents.mkdir(exist_ok=True)
    for src in sorted((profile / "agent" / "agents").glob("*.md")):
        shutil.copyfile(src, agents / src.name)
    auth = agent_dir / "auth.json"
    if not auth.exists():
        auth.write_text("{}\n", encoding="utf-8")


def build_invocation(*, model: str, profile: Path, agent_dir: Path, pi_cmd: list[str], event_log: Path,
                     pi_args: list[str]) -> tuple[list[str], dict[str, str]]:
    """The exact argv and the environment additions for one Pi run."""
    ext_dir = profile / "extensions"
    argv = list(pi_cmd) + ["--offline", "--provider", "ollama", "--model", model,
                           "--append-system-prompt", str(profile / SYSTEM_RULES)]
    for name in EXTENSIONS:
        argv += ["-e", str(ext_dir / name)]
    argv += list(pi_args)
    env = {
        "PI_CODING_AGENT_DIR": str(agent_dir),
        "PI_OFFLINE": "1",
        "LLM_ROUTER_PI_CHILD_EXTENSIONS": os.pathsep.join(str(ext_dir / n) for n in CHILD_EXTENSIONS),
        "LLM_ROUTER_PI_EVENT_LOG": str(event_log),
    }
    return argv, env


def cmd_pi(args: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="llm-router pi",
        description="Run the Pi coding agent on a local Ollama model with the llm-router local agent profile. "
                    "Arguments after the options (or after --) are passed to pi, e.g. -p 'prompt' or --mode json.",
    )
    ap.add_argument("--model", default=os.environ.get("LLM_ROUTER_PI_MODEL", ""),
                    help="Ollama model name (default: $LLM_ROUTER_PI_MODEL)")
    ap.add_argument("--context", type=int, default=None,
                    help="context window Pi plans for (default: $LLM_ROUTER_PI_CONTEXT, else the server's)")
    vis = ap.add_mutually_exclusive_group()
    vis.add_argument("--vision", dest="vision", action="store_true", default=False,
                     help="declare image input (off by default: see integrations/pi/README.md, Images)")
    vis.add_argument("--no-vision", dest="vision", action="store_false", help="declare text input only (default)")
    ap.add_argument("--agent-dir", default=None, help="Pi agent directory (default: $LLM_ROUTER_HOME/pi/agent)")
    ap.add_argument("--print-command", action="store_true",
                    help="print the argv and environment as JSON instead of running pi")
    opts, pi_args = ap.parse_known_args(args)
    if pi_args and pi_args[0] == "--":
        pi_args = pi_args[1:]
    if not opts.model:
        print("llm-router pi: --model is required (or set LLM_ROUTER_PI_MODEL).", file=sys.stderr)
        return 2
    try:
        profile = resolve_profile_dir()
        pi_cmd = find_pi()
        base_url = ollama_base_url()
    except ProfileError as e:
        print(str(e), file=sys.stderr)
        return 2

    from llm_router.paths import llm_router_home

    home = llm_router_home() / "pi"
    agent_dir = Path(opts.agent_dir).expanduser() if opts.agent_dir else home / "agent"
    context_window, ctx_source = resolve_context(base_url, opts.model, opts.context)
    vision = bool(opts.vision)
    caps = model_capabilities(base_url, opts.model)
    server_vision = "unknown" if caps is None else ("yes" if "vision" in caps else "no")
    print(f"llm-router pi: model={opts.model} context={context_window} ({ctx_source}) "
          f"images={'yes' if vision else 'no (pass --vision to declare them)'} "
          f"(server lists vision: {server_vision})", file=sys.stderr)
    if ctx_source == "default":
        # Planning for 4096 when the server runs 32768 makes every prompt look oversized;
        # planning for 32768 when it runs 4096 lets Ollama truncate. Neither is a safe guess.
        print("llm-router pi: the context window is unknown: the model is not loaded (no /api/ps report) and "
              "OLLAMA_CONTEXT_LENGTH is not set in this shell. Pass --context N with the server's real window "
              "(or set LLM_ROUTER_PI_CONTEXT).", file=sys.stderr)
        return 2
    version = pi_version(pi_cmd)
    if version is None or not version.startswith(TESTED_PI_VERSION):
        print(f"llm-router pi: warning: pi {version or '(version unknown)'} -- this profile was built and measured "
              f"against {TESTED_PI_VERSION}x; its extensions use Pi's extension API and may need changes.", file=sys.stderr)

    prepare_agent_dir(agent_dir, profile, build_models_json(base_url, opts.model, context_window, vision))
    argv, env_add = build_invocation(model=opts.model, profile=profile, agent_dir=agent_dir, pi_cmd=pi_cmd,
                                     event_log=home / "events.jsonl", pi_args=pi_args)
    if opts.print_command:
        print(json.dumps({"argv": argv, "env": env_add}, indent=1))
        return 0
    env = dict(os.environ, **env_add)
    os.execvpe(argv[0], argv, env)
    return 127  # not reached: execvpe replaces this process or raises
