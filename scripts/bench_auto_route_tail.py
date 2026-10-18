#!/usr/bin/env python3
"""Where does the auto-route hook's p95 tail come from? Synthetic, scratch-HOME, subprocess.

Runs ``hooks/auto-route.py`` (this working tree, with this tree's ``.venv`` python) as a real
subprocess N times per cell and reports p50/p95 of ``elapsed_ms`` and ``router_added_ms`` plus
the mean of every ``phases_ms`` entry, all read from the hook's own ``hook_latency.jsonl`` row.

Everything lives under ``--scratch``: HOME, LLM_ROUTER_HOME (state), a git repo (the edit
target) and a fake Ollama (a thread in this process) -- no real model, no network, never
``~/.llm-router`` / ``~/.claude``.

Cells (``--cells``):

``plain-<N>``     a plain prompt with a CC transcript of N lines (5000 / 20000 / 50000):
                  isolates ``session_io`` and ``import``.
``tooly-<N>``     the same with a text turn only every 2000 lines (tool-heavy tail: the tail
                  window must double to find enough turns).
``bigtool-<N>``   ``tooly-<N>`` with 40 KB tool results (N=5000 is a 200 MB transcript).
``contended-<N>`` ``plain-<N>`` while two other processes append to the SAME session store in a tight
                  loop (what context-capture's per-tool-call hooks and the MCP server do): lock
                  waits, compaction under the lock, the archive fsync.
``edit``          ``LLM_ROUTER_ZERO_CLAUDE_SCOPE=edit`` and an edit-class prompt naming a file in
                  the fixture repo, fake Ollama answering instantly: the ``zce`` phase end to end.
``edit-busy``     the same, with a stale model-discovery cache and a fake Ollama that is slow on
                  the metadata endpoints (``--busy-s``, default 0.4 s: a busy server answering
                  just inside the hook's 0.5 s probe timeouts).

    .venv/bin/python scripts/bench_auto_route_tail.py --scratch /path/to/scratch -n 30
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "auto-route.py"
MODEL = "fakemodel:latest"
HELPERS_OLD = "def count_vowels(s):\n    return sum(1 for c in s if c in 'aeiou')\n"
HELPERS_NEW = "def count_vowels(s):\n    return sum(1 for c in s.lower() if c in 'aeiou')\n"


class _Fake(BaseHTTPRequestHandler):
    meta_delay = 0.0

    def log_message(self, *a):  # noqa: D401
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        time.sleep(type(self).meta_delay)
        if self.path.startswith("/api/tags"):
            self._json({"models": [{"name": MODEL}]})
        elif self.path.startswith("/api/ps"):
            self._json({"models": [{"name": MODEL}]})
        else:
            self._json({})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        text = "ok"
        if b"old_string" in raw and b"count_vowels" in raw:
            text = json.dumps([{"file": "helpers.py", "old_string": HELPERS_OLD.splitlines()[1],
                                "new_string": HELPERS_NEW.splitlines()[1]}])
        line = json.dumps({"message": {"role": "assistant", "content": text}, "done": True,
                           "prompt_eval_count": 5, "eval_count": 5}) + "\n"
        body = line.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve() -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def make_transcript(path: Path, lines: int, gap: int = 10, blob_bytes: int = 1800) -> None:
    """A CC-shaped transcript: a text turn pair every ``gap`` lines, tool_use/tool_result between."""
    blob = "x" * blob_bytes
    with path.open("w", encoding="utf-8") as fh:
        for i in range(lines):
            k = i % gap
            if k == 0:
                msg = {"role": "user", "content": f"please look at module {i} and fix the thing"}
            elif k == gap - 1:
                msg = {"role": "assistant", "content": [{"type": "text", "text": f"Done with step {i}."}]}
            elif k % 2 == 1:
                msg = {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash",
                                                         "input": {"command": f"ls {i}"}}]}
            else:
                msg = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i-1}",
                                                    "content": blob}]}
            fh.write(json.dumps({"type": msg["role"], "message": msg}) + "\n")


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    (repo / "helpers.py").write_text(HELPERS_OLD)
    (repo / "pyproject.toml").write_text('[tool.ruff]\nline-length = 100\n[tool.ruff.lint]\nselect = ["E", "F"]\n')
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "GIT_CONFIG_GLOBAL": "/dev/null"}
    for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=b", "-c", "user.email=b@b", "commit", "-qm", "i"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env, capture_output=True)
    return repo


_HAMMER = (
    "import sys,time\n"
    "from llm_router import session_store as s\n"
    "sid=sys.argv[1]\n"
    "i=0\n"
    "while True:\n"
    "    i+=1\n"
    "    s.record_event(sid,'tool_call',f'tool call {i} '+'y'*300,role='user')\n"
    "    time.sleep(0.004)\n"
)


def pct(vals: list[float], q: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def run_cell(name: str, scratch: Path, n: int, port: int, busy_s: float) -> dict:
    home = scratch / "home"
    state = home / "state"
    if state.exists():
        shutil.rmtree(state)
    state.mkdir(parents=True)
    repo = make_repo(scratch)
    edit = name.startswith("edit")
    transcript = scratch / "transcript.jsonl"
    base = name.replace("contended-", "plain-")
    if name.startswith("bigtool-"):  # 40 KB tool results (file reads, screenshots), text turn every 2000 lines
        make_transcript(transcript, int(name.split("-")[1]), gap=2000, blob_bytes=40_000)
    elif name.startswith("tooly-"):  # a tool-heavy tail: a text turn only every 2000 lines
        make_transcript(transcript, int(name.split("-")[1]), gap=2000)
    else:
        make_transcript(transcript, int(base.split("-")[1]) if base.startswith("plain-") else 5000)
    _Fake.meta_delay = busy_s if name == "edit-busy" else 0.0
    url = f"http://127.0.0.1:{port}"
    env = {
        "HOME": str(home), "LLM_ROUTER_HOME": str(state),
        "PATH": f"{REPO / '.venv' / 'bin'}:/usr/bin:/bin",
        "LLM_ROUTER_OLLAMA_URL": url, "OLLAMA_BASE_URL": url, "OLLAMA_HOST": url,
        "LLM_ROUTER_OLLAMA_MODEL": MODEL, "LLM_ROUTER_SYNTHETIC": "1", "UV_OFFLINE": "1",
        "LLM_ROUTER_NO_DETACHED_SPAWN": "1",
        # this tree's package, not whatever the venv's editable install points at (A/B against a base tree)
        "PYTHONPATH": str(REPO / "src"),
    }
    if edit:
        env["LLM_ROUTER_ZERO_CLAUDE_SCOPE"] = "edit"
    prompt = "fix count_vowels in helpers.py so it ignores case" if edit else "thanks, that looks right to me"
    payload = {"session_id": f"bench-{name}", "hook_event_name": "UserPromptSubmit", "prompt": prompt,
               "cwd": str(repo if edit else scratch), "transcript_path": str(transcript)}
    lat = state / "hook_latency.jsonl"
    rows: list[dict] = []
    outcomes: dict[str, int] = {}
    hammers: list[subprocess.Popen] = []
    for i in range(n + 3):  # 3 untimed warm-ups: bytecode, caches, a populated session store
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--", "."], check=True,
                       env={"PATH": "/usr/bin:/bin"}, capture_output=True)
        if name == "edit-busy":  # stale discovery cache every run
            for p in state.glob("discovery*"):
                p.unlink()
        if name.startswith("contended-") and i == 3 and not hammers:
            hammers = [subprocess.Popen([str(REPO / ".venv" / "bin" / "python"), "-c", _HAMMER, payload["session_id"]],
                                        env=env, cwd=str(scratch), stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL) for _ in range(2)]
            time.sleep(0.5)
        before = lat.stat().st_size if lat.exists() else 0
        t0 = time.perf_counter()
        proc = subprocess.run([str(REPO / ".venv" / "bin" / "python"), str(HOOK)], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=120)
        wall = (time.perf_counter() - t0) * 1000
        new = lat.read_text()[before:].strip().splitlines() if lat.exists() else []
        row = next((json.loads(x) for x in reversed(new) if '"auto-route"' in x), None)
        if i < 3:
            continue
        if row is None:
            outcomes["no_row"] = outcomes.get("no_row", 0) + 1
            continue
        row["wall_ms"] = wall
        rows.append(row)
        key = "block:" + ("applied" if "APPLIED" in proc.stdout else "other") if '"block"' in proc.stdout else "pass"
        outcomes[key] = outcomes.get(key, 0) + 1
    for h in hammers:  # our own children only
        h.terminate()
        h.wait()
    el = [r["elapsed_ms"] for r in rows]
    ra = [r.get("router_added_ms", r["elapsed_ms"]) for r in rows]
    phases: dict[str, list[float]] = {}
    for r in rows:
        for k, v in (r.get("phases_ms") or {}).items():
            phases.setdefault(k, []).append(v)
    return {
        "cell": name, "n": len(rows), "outcomes": outcomes,
        "elapsed_p50": pct(el, .5), "elapsed_p95": pct(el, .95),
        "added_p50": pct(ra, .5), "added_p95": pct(ra, .95),
        "wall_p50": pct([r["wall_ms"] for r in rows], .5),
        "phase_mean": {k: round(statistics.mean(v), 1) for k, v in sorted(phases.items())},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", required=True)
    ap.add_argument("-n", type=int, default=30)
    ap.add_argument("--busy-s", type=float, default=0.4)
    ap.add_argument("--cells", default="plain-5000,plain-20000,plain-50000,edit,edit-busy")
    a = ap.parse_args()
    scratch = Path(a.scratch).resolve()
    if "/.llm-router" in str(scratch) or str(scratch) == str(Path.home()):
        print("refusing a scratch dir that looks like real state", file=sys.stderr)
        return 2
    scratch.mkdir(parents=True, exist_ok=True)
    srv, port = _serve()
    for cell in a.cells.split(","):
        res = run_cell(cell, scratch, a.n, port, a.busy_s)
        print(json.dumps(res), flush=True)
    srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
