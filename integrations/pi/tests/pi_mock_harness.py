"""Run the real Pi binary against mock_openai.py, for local end-to-end checks.

Needs Node and Pi installed; nothing here touches a model or the network beyond
127.0.0.1. Used by tests/test_pi_profile_e2e.py (skipped when Pi is absent) and by
the upstream reproduction scripts.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILE = HERE.parent
MOCK = HERE / "mock_openai.py"


def find_pi() -> list[str] | None:
    """argv prefix that starts Pi, or None. LLM_ROUTER_PI_BIN wins over PATH."""
    explicit = os.environ.get("LLM_ROUTER_PI_BIN")
    if explicit:
        return [explicit]
    found = shutil.which("pi")
    return [found] if found else None


class MockServer:
    def __init__(self, script: list, workdir: Path) -> None:
        self.log = workdir / "requests.jsonl"
        sp = workdir / "script.json"
        sp.write_text(json.dumps(script))
        self.proc = subprocess.Popen(
            [sys.executable, str(MOCK), "--script", str(sp), "--log", str(self.log)],
            stdout=subprocess.PIPE, text=True,
        )
        line = self.proc.stdout.readline()
        self.port = int(line.split()[1])

    def requests(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text().splitlines() if x.strip()]

    def close(self) -> None:
        self.proc.terminate()
        self.proc.wait(5)


def write_agent_dir(agent_dir: Path, port: int, context_window: int = 32768, vision: bool = False,
                    provider: str = "mock") -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "auth.json").write_text("{}")
    (agent_dir / "models.json").write_text(json.dumps({"providers": {provider: {
        "baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions", "apiKey": "x",
        "models": [{"id": "m", "name": "m", "input": ["text", "image"] if vision else ["text"],
                    "contextWindow": context_window, "maxTokens": 4096,
                    "reasoning": False, "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}))
    shutil.copy(PROFILE / "agent" / "settings.json", agent_dir / "settings.json")
    shutil.copytree(PROFILE / "agent" / "agents", agent_dir / "agents", dirs_exist_ok=True)


def _pgrep(pattern: str) -> list[str]:
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()


def run_pi(prompt: str, cwd: Path, agent_dir: Path, extensions: list[str], extra_env: dict | None = None,
           sigint_after_tool: str | None = None, timeout_s: float = 60, extra_args: list[str] | None = None,
           provider: str = "mock", sigint_when_running: str | None = None) -> dict:
    """Run Pi once. SIGINT goes to Pi's process group 1.5 s after `sigint_after_tool` starts,
    and, with `sigint_when_running`, only once a process matching that pattern exists."""
    pi = find_pi()
    assert pi, "Pi not found (set LLM_ROUTER_PI_BIN)"
    cmd = pi + ["--offline", "--provider", provider, "--model", "m", "--no-session", "--mode", "json"]
    for e in extensions:
        cmd += ["-e", str(PROFILE / "extensions" / e)]
    cmd += (extra_args or []) + ["-p", prompt]
    env = dict(os.environ, PI_CODING_AGENT_DIR=str(agent_dir), PI_OFFLINE="1", **(extra_env or {}))
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    lines: list[str] = []
    sent = False
    while time.monotonic() - t0 < timeout_s:
        r, _, _ = select.select([p.stdout], [], [], 0.2)
        if r:
            line = p.stdout.readline()
            if line:
                lines.append(line)
                if sigint_after_tool and not sent and '"tool_execution_start"' in line and f'"{sigint_after_tool}"' in line:
                    if sigint_when_running:
                        deadline = time.monotonic() + 30
                        while not _pgrep(sigint_when_running) and time.monotonic() < deadline:
                            time.sleep(0.2)
                    time.sleep(1.5)
                    os.killpg(p.pid, signal.SIGINT)
                    sent = True
                continue
        if p.poll() is not None:
            break
    try:
        p.wait(15)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait(5)
    rest = p.stdout.read() or ""
    lines += rest.splitlines(keepends=True)
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return {"rc": p.returncode, "events": events, "stderr": p.stderr.read(), "sigint_sent": sent,
            "elapsed_s": time.monotonic() - t0}


def final_text(events: list[dict]) -> str:
    for e in reversed(events):
        if e.get("type") == "message_end" and (e.get("message") or {}).get("role") == "assistant":
            return "".join(b.get("text", "") for b in e["message"].get("content") or [] if b.get("type") == "text")
    return ""


def tool_calls(events: list[dict]) -> list[dict]:
    return [{"name": e.get("toolName"), "args": e.get("args")} for e in events if e.get("type") == "tool_execution_start"]


def tool_results(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        if e.get("type") == "tool_execution_end":
            r = e.get("result") or {}
            txt = "".join(b.get("text", "") for b in (r.get("content") or []) if isinstance(b, dict))
            out.append({"name": e.get("toolName"), "text": txt, "is_error": bool(e.get("isError"))})
    return out


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="pi-profile-"))
