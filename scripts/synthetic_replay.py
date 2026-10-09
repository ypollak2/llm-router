#!/usr/bin/env python3
"""$0 replay of synthetic sessions through every llm-router classification door.

    python3 scripts/synthetic_replay.py --corpus sessions.jsonl --out replay_out/ \
        [--doors hook,proxy,gateway,mcp,sdk,agent-route] [--sessions N]

Owner decision D-42 (2026-10-09): synthetic sessions are evidence for MECHANICS gates
only (M0-3, P0.1-a archiving, P0.8 field completeness, P0.9 latency mechanics, P1.1-a,
P1.6-a/b). They are always labelled non-organic and never mixed into organic counts.
Nothing this script prints is a routing-quality or North Star number.

WHAT IT DOES
  * Creates a scratch root (``--work``, default a fresh temp dir) with its own HOME and
    ``LLM_ROUTER_HOME``. Every child (hooks, the proxy, the in-process doors' worker)
    runs with that HOME, never the operator's.
  * Starts a stub upstream on an ephemeral 127.0.0.1 port that answers Anthropic
    ``/v1/messages`` (JSON and SSE), OpenAI ``/v1/chat/completions`` and Ollama
    ``/api/tags`` with a tiny canned reply. No provider is ever called: every child also
    loads ``scripts/_replay_netguard.py`` as ``sitecustomize``, which refuses any
    connection that is not the stub or this run's proxy, and the run exits 1 if one was
    attempted.
  * Tags every row it causes as synthetic through the two existing mechanisms:
    ``LLM_ROUTER_SYNTHETIC=1`` (``routing_quality.detect_synthetic``: usage.db
    provenance / session_spend ``synthetic``) and ``LLM_ROUTER_SESSION_KIND=harness``
    (``session_kind``: the ``session_kind`` field of proxy and agent-route rows, and the
    SessionStart tag file). Session ids are rewritten to ``synth-replay-<hash>``, which
    ``groundtruth_sources.is_synthetic_session`` reads as authored, so readers that
    filter on the id drop them too. No new field was added.
  * Drives, per record: SessionStart once per session, then auto-route
    (UserPromptSubmit) for ``turn_first``, agent-route (PreToolUse Agent) for
    ``subagent``, the proxy for ``turn_first``/``continuation``, the gateway and SDK
    classify paths and the MCP classify path; Stop after every turn and SessionEnd once
    at the end, the order Claude Code fires them.
  * Writes ``summary.json`` and ``summary.md`` to ``--out``: per-door decisions
    (distributions and field presence), latency p50/p95 with n, ledger field
    completeness per writer, archive behaviour, and a door-agreement matrix with Wilson
    95% intervals. Counts and hashes only; no message text leaves the scratch root.

``summary.json["deterministic"]`` is identical for a fixed corpus (tested);
``summary.json["timing"]`` is wall-clock and is not.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
#: The copies Claude Code runs (hooks.json: ${CLAUDE_PLUGIN_ROOT}/hooks/<name>.py).
HOOKS_DIR = ROOT / "hooks"
GUARD_SRC = Path(__file__).resolve().parent / "_replay_netguard.py"

ALL_DOORS = ("hook", "proxy", "gateway", "mcp", "sdk", "agent-route")
#: Ports a replay must never bind or contact (the operator's live router processes).
FORBIDDEN_PORTS = frozenset({8787, 8797, 8798})
STEPS = ("turn_first", "continuation", "subagent")
CHILD_TIMEOUT_S = 60.0
PROXY_START_TIMEOUT_S = 30.0
RUN_TIME_LIMIT_S = 3600.0
SESSION_PREFIX = "synth-replay-"


# ── corpus ───────────────────────────────────────────────────────────────────


class CorpusError(ValueError):
    pass


def load_corpus(path: Path, max_sessions: int | None = None) -> list[dict]:
    """Expanded records in replay order: sessions in order of first appearance, records in
    file order within a session. Records are DELTAS (corpus README): ``request`` is every
    earlier ``messages`` list of the same ``(session_id, thread)`` plus this one. Raises
    CorpusError on an empty or malformed corpus."""
    raw: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError as exc:
                raise CorpusError(f"line {lineno}: not JSON ({exc})") from None
            if not isinstance(rec, dict) or not rec.get("session_id") \
                    or rec.get("step") not in STEPS or not isinstance(rec.get("messages"), list):
                raise CorpusError(f"line {lineno}: needs session_id, step in {STEPS}, messages[]")
            rec["_line"] = lineno
            raw.append(rec)
    if not raw:
        raise CorpusError("corpus is empty")
    order: list[str] = []
    for r in raw:
        if r["session_id"] not in order:
            order.append(r["session_id"])
    if max_sessions is not None:
        order = order[:max_sessions]
    rank = {sid: i for i, sid in enumerate(order)}
    raw = sorted((r for r in raw if r["session_id"] in rank), key=lambda r: (rank[r["session_id"]], r["_line"]))
    hist: dict[tuple[str, str], list] = defaultdict(list)
    out: list[dict] = []
    for i, r in enumerate(raw):
        thread = str(r.get("thread") or ("main" if r["step"] != "subagent" else f"sub-{r.get('turn')}"))
        key = (r["session_id"], thread)
        hist[key] = hist[key] + [m for m in r["messages"] if isinstance(m, dict)]
        cat = str(r.get("intended_category") or "")
        out.append({
            "idx": i, "corpus_sid": r["session_id"], "sid": replay_sid(r["session_id"]),
            "turn": int(r.get("turn") or 0), "step": r["step"], "thread": thread,
            "main": thread == "main", "request": list(hist[key]), "delta": r["messages"],
            "intended": cat.split("/", 1)[0] or None, "intended_complexity": (cat.split("/", 1) + [None])[1],
            "lang": r.get("lang"),
        })
    return out


def corpus_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def replay_sid(corpus_sid: str) -> str:
    """Deterministic, authored-looking session id (never the corpus id itself)."""
    return SESSION_PREFIX + hashlib.sha256(str(corpus_sid).encode()).hexdigest()[:16]


def _block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict) and block.get("type") == "text":
        return str(block.get("text") or "")
    return ""


def message_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(t for t in (_block_text(b) for b in content) if t)
    return ""


def latest_user_text(messages: list) -> str:
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            t = message_text(m)
            if t.strip():
                return t
    return ""


def agent_calls(records: list[dict]) -> dict[str, dict]:
    """``{brief text: Agent tool_use input}`` from the main-thread history, so the
    agent-route hook sees the tool_input the parent actually sent."""
    out: dict[str, dict] = {}
    for r in records:
        for m in r["delta"]:
            content = m.get("content") if isinstance(m, dict) else None
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task"):
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    if inp.get("prompt"):
                        out.setdefault(str(inp["prompt"]), inp)
    return out


# ── synthetic request envelope ───────────────────────────────────────────────
# The corpus omits the system prompt and the tool definitions (its README: a ~52 KB
# floor). A proxy body without them is not what Claude Code sends, so a fixed,
# invented envelope of about that size is added. Deterministic; no real text.

_SYS_SENTENCES = (
    "You are a coding assistant working inside a fictional repository.",
    "Prefer small, reviewable changes and explain what changed.",
    "Run the project's tests after an edit and report the command and its output.",
    "Never invent file contents; read a file before editing it.",
    "Keep answers short and lead with the result.",
    "Ask before any action that cannot be undone.",
)
_TOOL_NAMES = ("Bash", "Read", "Edit", "Write", "Glob", "Grep", "Agent", "WebFetch", "WebSearch",
               "TodoWrite", "NotebookEdit", "Monitor", "TaskStop", "Skill", "SendMessage")


def synthetic_system(target_bytes: int = 14_000) -> list[dict]:
    parts = ["Primary working directory: /work/meridian", "Platform: darwin"]
    i = 0
    while sum(len(p) + 1 for p in parts) < target_bytes:
        parts.append(f"Rule {i}: {_SYS_SENTENCES[i % len(_SYS_SENTENCES)]}")
        i += 1
    return [{"type": "text", "text": "\n".join(parts)}]


def synthetic_tools(target_bytes: int = 38_000) -> list[dict]:
    per = max(200, target_bytes // len(_TOOL_NAMES))
    tools = []
    for name in _TOOL_NAMES:
        desc = f"The {name} tool. " + ("It does one fictional thing and reports the outcome. " * (per // 55))
        tools.append({"name": name, "description": desc[:per],
                      "input_schema": {"type": "object", "properties": {
                          "input": {"type": "string", "description": "what to act on"}},
                          "required": ["input"]}})
    return tools


SYSTEM_BLOCKS = synthetic_system()
TOOL_DEFS = synthetic_tools()
SUBAGENT_TOOL_DEFS = [t for t in TOOL_DEFS if t["name"] not in ("Agent", "Task")]


#: A code-heavy, invented ``<system-reminder>`` block of the kind Claude Code prepends to
#: a user message (file context, memory). The proxy strips reminders before classifying
#: (``proxy.steps._REMINDER_RE``); the ``reminder`` variant checks that it does.
REMINDER_BLOCK = {"type": "text", "text": "<system-reminder>\nContents of /work/meridian/src/app.py:\n"
                  + "def handler(req):\n    return compile_and_run(req.code)  # refactor, debug, unit test\n" * 20
                  + "</system-reminder>"}


def _with_reminder(messages: list) -> list:
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        m = out[i]
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
            out[i] = dict(m, content=[REMINDER_BLOCK] + blocks)
            break
    return out


PROXY_VARIANTS = ("nosys", "bare", "reminder")


def proxy_body(rec: dict, variant: str = "full") -> dict:
    """The body Claude Code would send for this step. ``variant``: ``full`` (system +
    tools), ``nosys`` (tools, no system prompt), ``bare`` (neither), ``reminder`` (full
    plus a code-heavy ``<system-reminder>`` block in the newest user message)."""
    sid = rec["sid"] + ("" if variant == "full" else f"-{variant}")
    body: dict[str, Any] = {
        "model": "claude-opus-5-5", "max_tokens": 4096, "stream": True,
        "thinking": {"type": "adaptive"},
        "messages": _with_reminder(rec["request"]) if variant == "reminder" else rec["request"],
        "metadata": {"user_id": json.dumps({"session_id": sid})},
    }
    if variant in ("full", "reminder"):
        body["system"] = SYSTEM_BLOCKS
    if variant in ("full", "nosys", "reminder"):
        body["tools"] = TOOL_DEFS if rec["main"] else SUBAGENT_TOOL_DEFS
    return body


# ── statistics ───────────────────────────────────────────────────────────────


def percentile(values: list[float], q: float) -> float | None:
    """Nearest rank on the n-1 scale (the rule ``llm-router kpi`` and hook_wall use)."""
    if not values:
        return None
    v = sorted(values)
    k = max(0, min(len(v) - 1, round(q * (len(v) - 1))))
    return round(v[k], 2)


def wilson(k: int, n: int, z: float = 1.959964) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "ci95": wilson(k, n)}


def size_summary(values: list[int]) -> dict:
    return {"n": len(values), "p50_bytes": percentile([float(v) for v in values], 0.5),
            "p95_bytes": percentile([float(v) for v in values], 0.95)}


# ── stub upstream ────────────────────────────────────────────────────────────

STUB_TEXT = "ok"


class _StubHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "replay-stub/1"

    def log_message(self, *args: Any) -> None:  # silence
        return

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _send(self, status: int, payload: Any, ctype: str = "application/json") -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _count(self, kind: str) -> None:
        with self.server.lock:  # type: ignore[attr-defined]
            self.server.hits[kind] += 1  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/tags"):
            self._count("ollama_tags")
            self._send(200, {"models": []})
        elif path.startswith("/v1/models"):
            self._count("models")
            self._send(200, {"data": [], "object": "list"})
        else:
            self._count("other_get")
            self._send(200, {})

    def do_HEAD(self) -> None:  # noqa: N802
        self._count("other_head")
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = self._body()
        model = str(body.get("model") or "stub-model")
        if path.endswith("/messages/count_tokens"):
            self._count("anthropic_count_tokens")
            self._send(200, {"input_tokens": 1})
        elif path.endswith("/messages"):
            self._count("anthropic_messages")
            if body.get("stream"):
                self._sse(model)
            else:
                self._send(200, {
                    "id": "msg_replaystub", "type": "message", "role": "assistant", "model": model,
                    "content": [{"type": "text", "text": STUB_TEXT}], "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1,
                              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}})
        elif path.endswith("/chat/completions"):
            self._count("openai_chat")
            self._send(200, {
                "id": "chatcmpl-replaystub", "object": "chat.completion", "created": 0, "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": STUB_TEXT}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        elif path.startswith("/api/"):
            # Ollama generate/chat/show/pull: refused, so no model is ever "loaded".
            self._count("ollama_other")
            self._send(404, {"error": "replay stub: no models"})
        else:
            self._count("other_post")
            self._send(404, {"error": "replay stub: unknown path"})

    def _sse(self, model: str) -> None:
        events = [
            ("message_start", {"type": "message_start", "message": {
                "id": "msg_replaystub", "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 0,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": STUB_TEXT}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 1}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        data = b"".join(f"event: {e}\ndata: {json.dumps(d)}\n\n".encode() for e, d in events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class StubServer:
    """Threaded stub on 127.0.0.1:<ephemeral>. ``hits`` counts requests per kind."""

    def __init__(self) -> None:
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.httpd.daemon_threads = True
        self.httpd.hits = Counter()  # type: ignore[attr-defined]
        self.httpd.lock = threading.Lock()  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        if self.port in FORBIDDEN_PORTS:  # vanishingly unlikely; refuse rather than risk it
            self.httpd.server_close()
            raise RuntimeError(f"ephemeral port {self.port} is a forbidden router port")
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def hits(self) -> Counter:
        return self.httpd.hits  # type: ignore[attr-defined]

    def __enter__(self) -> StubServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def free_port() -> int:
    """An ephemeral port that is not one of the forbidden router ports."""
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in FORBIDDEN_PORTS:
            return port
    raise RuntimeError("could not find a free ephemeral port")



# ── scratch environment ──────────────────────────────────────────────────────

#: Switches that keep every child at $0 and in-process: no LLM classifier layer, no
#: direct execution, no Codex/CLI delegation, no warm-up, watchdog, judge drain, OKF
#: index, verify worker, pxpipe or benchmark fetch. Each name is read by the code it
#: names (see the PR description for file:line); an unknown name is inert.
DISABLE_ENV = {
    "LLM_ROUTER_HOOK_LLM_LAYER": "off",
    "LLM_ROUTER_CLASSIFY_LOCAL_ONLY": "true",
    "LLM_ROUTER_DIRECT_EXECUTION": "false",
    "LLM_ROUTER_SUBAGENT_DIRECT": "off",
    "LLM_ROUTER_AGENT_ROUTE_CODEX": "off",
    "LLM_ROUTER_SUBAGENT_CLI_DELEGATION": "off",
    "LLM_ROUTER_OLLAMA_WARMUP": "off",
    "LLM_ROUTER_OLLAMA_WATCHDOG": "off",
    "LLM_ROUTER_JUDGE_AUTODRAIN": "off",
    "LLM_ROUTER_OKF_AUTOINDEX": "off",
    "LLM_ROUTER_VERIFY": "off",
    "LLM_ROUTER_PXPIPE_ENABLED": "off",
    "LLM_ROUTER_AUTO_BENCHMARK_FETCH": "off",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "LITELLM_TELEMETRY": "False",
}

#: Executables a child might shell out to that must never run for real here. Each is
#: replaced by a script that exits 1 (``security`` would read the login Keychain).
_SHADOWED_BINARIES = ("security", "ollama", "codex", "gemini", "claude", "npx", "launchctl",
                      "systemctl", "open", "osascript")


class Scratch:
    """The run's private world: HOME, LLM_ROUTER_HOME, guard, violations file, stub bin."""

    def __init__(self, root: Path, stub: StubServer, proxy_port: int | None) -> None:
        self.root = root
        self.home = root / "home"
        self.state = root / "state"
        self.guard_dir = root / "guard"
        self.bin = root / "bin"
        self.violations = root / "net_violations.jsonl"
        self.workspace = root / "workspace"
        self.tmp = root / "tmp"
        self.marker = f"replay-{hashlib.sha256(str(root).encode()).hexdigest()[:12]}"
        for d in (self.home, self.state, self.guard_dir, self.bin, self.workspace, self.tmp,
                  self.home / ".claude" / "projects"):
            d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(GUARD_SRC, self.guard_dir / "sitecustomize.py")
        for name in _SHADOWED_BINARIES:
            p = self.bin / name
            p.write_text("#!/bin/sh\nexit 1\n")
            p.chmod(0o755)
        self.violations.touch()
        self.stub = stub
        self.proxy_port = proxy_port

    def allow(self) -> str:
        items = [f"127.0.0.1:{self.stub.port}"]
        if self.proxy_port:
            items.append(f"127.0.0.1:{self.proxy_port}")
        return ",".join(items)

    def env(self, **extra: str) -> dict[str, str]:
        """Child environment built from nothing: none of the operator's variables."""
        stub = self.stub.url
        env = {
            "HOME": str(self.home),
            "LLM_ROUTER_HOME": str(self.state),
            "LLM_ROUTER_DB_PATH": str(self.state / "usage.db"),
            "PATH": os.pathsep.join([str(self.bin), str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
            "LANG": "en_US.UTF-8",
            "TMPDIR": str(self.tmp),
            "PYTHONPATH": os.pathsep.join([str(self.guard_dir), str(ROOT / "src")]),
            "PYTHONDONTWRITEBYTECODE": "1",
            # synthetic tagging: the existing mechanisms (module docstring)
            "LLM_ROUTER_SYNTHETIC": "1",
            "LLM_ROUTER_SESSION_KIND": "harness",
            "LLM_ROUTER_ALLOW_STUBS": "1",
            # network guard; REPLAY_MARKER finds this run's detached grandchildren
            "LLM_ROUTER_REPLAY_ALLOW": self.allow(),
            "LLM_ROUTER_REPLAY_VIOLATIONS": str(self.violations),
            "LLM_ROUTER_REPLAY_MARKER": self.marker,
            # every Ollama reader points at the stub, which lists no models
            "OLLAMA_HOST": stub, "OLLAMA_BASE_URL": stub, "OLLAMA_URL": stub,
            "LLM_ROUTER_OLLAMA_URL": stub,
        }
        if self.proxy_port:  # Claude Code's own environment when it runs behind the proxy
            env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{self.proxy_port}"
        env.update(DISABLE_ENV)
        env.update(extra)
        return env

    def violation_rows(self) -> list[dict]:
        out = []
        for line in self.violations.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"kind": "unparseable"})
        return out

    def descendants(self) -> list[int]:
        """PIDs of live processes carrying this run's marker in their environment
        (``/proc/<pid>/environ`` on Linux, ``ps -E`` on macOS): the children and the
        detached grandchildren they spawned."""
        needle = f"LLM_ROUTER_REPLAY_MARKER={self.marker}"
        proc = Path("/proc")
        if proc.is_dir():
            found = []
            for d in proc.iterdir():
                if d.name.isdigit() and int(d.name) != os.getpid():
                    try:
                        if needle.encode() in (d / "environ").read_bytes().split(b"\0"):
                            found.append(int(d.name))
                    except OSError:
                        continue
            return found
        try:
            out = subprocess.run(["/bin/ps", "-E", "-ww", "-o", "pid=,command="], capture_output=True,
                                 text=True, timeout=10, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        pids = []
        for line in out.splitlines():
            if needle in line:
                pid = int(line.split(None, 1)[0])
                if pid != os.getpid():
                    pids.append(pid)
        return pids


# ── ledgers ──────────────────────────────────────────────────────────────────

#: Per-row fields any existing writer uses to say "not organic traffic".
TAG_FIELDS = ("session_kind", "provenance", "is_simulated", "synthetic", "session_id")


def row_is_synthetic(row: dict) -> bool:
    """A row carries a synthetic tag: ``session_kind=harness``, ``provenance`` test /
    unattributed, ``is_simulated``/``synthetic`` set, or a replay session id."""
    if str(row.get("session_kind") or "").lower() == "harness":
        return True
    if str(row.get("provenance") or "").lower() in ("test", "unattributed", "synthetic"):
        return True
    if row.get("is_simulated") in (1, True, "1") or row.get("synthetic") in (1, True, "1", "true"):
        return True
    sid = row.get("session_id")
    return isinstance(sid, str) and sid.startswith(SESSION_PREFIX)


def row_contradicts(row: dict) -> bool:
    """A row that says it is real traffic (``provenance=runtime`` or ``session_kind`` organic)."""
    return str(row.get("provenance") or "").lower() == "runtime" \
        or str(row.get("session_kind") or "").lower() == "organic"


COMPLETENESS_FIELDS = ("session_id", "reason", "reason_code", "task_id")


def _jsonl_rows(path: Path) -> list[dict]:
    rows = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


def _sqlite_tables(path: Path) -> dict[str, tuple[set[str], list[dict]]]:
    """``{table: (columns, rows)}`` of a sqlite file, read-only. Never raises."""
    import sqlite3

    out: dict[str, tuple[set[str], list[dict]]] = {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        con.row_factory = sqlite3.Row
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for name in sorted(names):
            if name.startswith("sqlite_"):
                continue
            try:
                cols = {r[1] for r in con.execute(f'PRAGMA table_info("{name}")')}
                rows = [dict(r) for r in con.execute(f'SELECT * FROM "{name}"')]
            except sqlite3.Error:
                continue
            out[name] = (cols, rows)
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def writer_completeness(state: Path) -> dict[str, dict]:
    """Per ledger (JSONL file or sqlite table) under ``state``: rows, the share carrying
    each COMPLETENESS_FIELDS entry (only where the writer's schema has it), the share
    tagged synthetic, rows that claim to be real, and whether the schema has any tag
    field at all. Keys are paths relative to ``state``; sorted, so output is stable."""
    report: dict[str, dict] = {}

    def add(name: str, rows: list[dict], schema: set[str]) -> None:
        report[name] = {
            "rows": len(rows),
            "fields": {f: rate(sum(1 for r in rows if r.get(f) not in (None, "")), len(rows))
                       for f in COMPLETENESS_FIELDS if f in schema},
            "tag_fields_in_schema": sorted(f for f in TAG_FIELDS if f in schema),
            "synthetic": rate(sum(1 for r in rows if row_is_synthetic(r)), len(rows)),
            "claims_real": sum(1 for r in rows if row_contradicts(r)),
        }

    merged: dict[str, tuple[set[str], list[dict]]] = {}
    for path in sorted(state.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(state))
        if path.name.endswith(".jsonl"):
            rows = _jsonl_rows(path)
            if rows:
                schema, acc = merged.setdefault(_generic_name(rel), (set(), []))
                schema.update(*(r.keys() for r in rows))
                acc.extend(rows)
        elif path.suffix == ".db":
            for name, (cols, rows) in _sqlite_tables(path).items():
                if rows:
                    merged[f"{rel}:{name}"] = (cols, rows)
    for name, (schema, rows) in merged.items():
        add(name, rows, schema)
    return dict(sorted(report.items()))


def _generic_name(rel: str) -> str:
    """``projects/<pid>/session_context_<sid>.jsonl`` -> ``projects/*/session_context_*.jsonl``:
    a writer, not one of its files. Hash-like path parts become ``*``."""
    import re

    parts = rel.split("/")
    parts = [re.sub(r"(synth-replay-[0-9a-f]{16}(-[a-z]+)?|[0-9a-f]{16,64})", "*", p) for p in parts]
    return "/".join(parts)


# ── child processes ──────────────────────────────────────────────────────────


def run_hook(hook: str, payload: dict, env: dict[str, str], cwd: Path) -> tuple[int | None, str, float]:
    """Run ``hooks/<hook>.py`` the way the host does: a fresh interpreter, the payload on
    stdin. Returns (exit code, or None when killed at the timeout; stdout; wall ms)."""
    cmd = [sys.executable, str(HOOKS_DIR / f"{hook}.py")]
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env, cwd=str(cwd))
    try:
        out, _ = proc.communicate(json.dumps(payload).encode(), timeout=CHILD_TIMEOUT_S)
        rc: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        rc = None
    return rc, out.decode("utf-8", "replace"), (time.perf_counter() - t0) * 1000.0


def stdout_kind(out: str) -> str:
    """What a hook printed, as a label: never the text."""
    s = out.strip()
    if not s:
        return "empty"
    try:
        j = json.loads(s.splitlines()[-1]) if not s.startswith("{") else json.loads(s)
    except ValueError:
        return "text"
    if not isinstance(j, dict):
        return "other_json"
    if j.get("decision") == "block":
        return "block"
    hso = j.get("hookSpecificOutput")
    if isinstance(hso, dict):
        return "permission_" + str(hso.get("permissionDecision")) if hso.get("permissionDecision") \
            else "context"
    return "system_message" if "systemMessage" in j else "json"


class JsonlTail:
    """New rows appended to a JSONL file since the last read."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = path.stat().st_size if path.exists() else 0

    def new_rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("rb") as fh:
            fh.seek(self.offset)
            data = fh.read()
        cut = data.rfind(b"\n") + 1  # only whole lines
        self.offset += cut
        rows = []
        for line in data[:cut].decode("utf-8", "replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows


class Proxy:
    """One ``llm-router proxy`` subprocess on an ephemeral port, upstream = the stub.

    Flags are the installed default (``proxy_default.DEFAULT_STEPS`` = off,
    ``DEFAULT_TIERS`` = conversation) plus ``--no-warm-up``."""

    def __init__(self, scratch: Scratch, port: int) -> None:
        self.scratch, self.port = scratch, port
        self.proc: subprocess.Popen | None = None
        self.log = scratch.root / "proxy.stderr"

    def __enter__(self) -> Proxy:
        if self.port in FORBIDDEN_PORTS:
            raise RuntimeError(f"refusing forbidden port {self.port}")
        env = self.scratch.env(LLM_ROUTER_PROXY_UPSTREAM=self.scratch.stub.url)
        cmd = [sys.executable, "-m", "llm_router.cli", "proxy", "--host", "127.0.0.1",
               "--port", str(self.port), "--steps", "off", "--tiers", "conversation", "--no-warm-up"]
        with self.log.open("wb") as err:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=err, env=env, cwd=str(self.scratch.workspace))
        deadline = time.monotonic() + PROXY_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"proxy exited rc={self.proc.returncode} (see {self.log})")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return self
            except OSError:
                time.sleep(0.1)
        self.__exit__()
        raise RuntimeError(f"proxy did not listen within {PROXY_START_TIMEOUT_S}s")

    def __exit__(self, *exc: Any) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)

    def post(self, body: dict) -> tuple[int, float, int]:
        """POST /v1/messages; (status, wall ms, request bytes). The reply is read to the end."""
        import urllib.error
        import urllib.request

        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/messages", data=data, method="POST",
            headers={"content-type": "application/json", "anthropic-version": "2023-06-01",
                     "x-api-key": "sk-ant-replay-stub"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=CHILD_TIMEOUT_S) as resp:
                resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            exc.read()
            status = exc.code
        except OSError:
            status = 0
        return status, (time.perf_counter() - t0) * 1000.0, len(data)


# ── decisions ────────────────────────────────────────────────────────────────

#: complexity -> the router profile it selects (router._resolve_profile: simple=budget,
#: moderate=balanced, complex/deep=premium); used as the tier of doors that emit only a
#: complexity (hook, gateway, sdk, mcp).
PROFILE_OF = {"simple": "budget", "moderate": "balanced", "complex": "premium", "deep_reasoning": "premium"}
#: Every door's tier on one ordinal scale, for the agreement matrix.
TIER_NORM = {"budget": "low", "balanced": "mid", "premium": "high",
             "haiku": "low", "sonnet": "mid", "opus": "high", "fable": "high"}


def norm_tier(tier: Any) -> str | None:
    if not tier:
        return None
    t = str(tier).lower()
    for key, val in TIER_NORM.items():
        if key in t:
            return val
    return None


def decision(rec: dict, door: str, **fields: Any) -> dict:
    d = {"door": door, "sid": rec["sid"], "idx": rec["idx"], "turn": rec["turn"], "step": rec["step"],
         "main": rec["main"], "intended_category": rec["intended"], "lang": rec["lang"],
         "task_type": None, "complexity": None, "tier": None, "layer": None, "ok": False,
         "error": None, "latency_ms": None}
    d.update(fields)
    if d.get("tier") is None and d.get("complexity"):
        d["tier"] = PROFILE_OF.get(str(d["complexity"]))
    d["tier_norm"] = norm_tier(d.get("tier"))
    return d


class Replayer:
    """Drives one corpus through the selected doors inside one Scratch."""

    def __init__(self, scratch: Scratch, doors: set[str], proxy: Proxy | None) -> None:
        self.s = scratch
        self.doors = doors
        self.proxy = proxy
        self.decisions: list[dict] = []
        self.lifecycle: list[dict] = []  # non-classifying hooks: name, wall ms, rc
        self.archive: dict[str, list] = defaultdict(list)
        self.sizes: dict[str, list[int]] = defaultdict(list)
        self.proxy_tail = JsonlTail(scratch.state / "proxy_calls.jsonl")
        self.agent_tail = JsonlTail(scratch.state / "agent_calls_ledger.jsonl")
        self.hook_errors: Counter = Counter()

    # -- plumbing --
    def _env(self, sid: str) -> dict[str, str]:
        return self.s.env(CLAUDE_CODE_SESSION_ID=sid, REPLAY_CWD=str(self.s.workspace))

    def _transcript(self, sid: str) -> Path:
        p = self.s.home / ".claude" / "projects" / "-work-replay" / f"{sid}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _append_transcript(self, sid: str, messages: list, seq: list[int]) -> None:
        with self._transcript(sid).open("a", encoding="utf-8") as fh:
            for m in messages:
                seq[0] += 1
                role = m.get("role")
                ev = {"type": role, "sessionId": sid, "uuid": f"{sid}-{seq[0]}",
                      "parentUuid": f"{sid}-{seq[0] - 1}" if seq[0] > 1 else None,
                      "timestamp": f"2026-01-01T00:{seq[0] // 60 % 60:02d}:{seq[0] % 60:02d}.000Z",
                      "cwd": str(self.s.workspace), "isSidechain": False,
                      "message": dict(m, **({"id": f"msg_replay_{seq[0]}", "model": "claude-opus-5-5"}
                                            if role == "assistant" else {}))}
                fh.write(json.dumps(ev) + "\n")

    def _payload(self, sid: str, event: str, **extra: Any) -> dict:
        p = {"session_id": sid, "transcript_path": str(self._transcript(sid)), "cwd": str(self.s.workspace),
             "permission_mode": "default", "hook_event_name": event}
        p.update(extra)
        return p

    def _lifecycle(self, hook: str, event: str, sid: str, **extra: Any) -> str:
        rc, out, ms = run_hook(hook, self._payload(sid, event, **extra), self._env(sid), self.s.workspace)
        self.lifecycle.append({"hook": hook, "event": event, "wall_ms": ms, "rc": rc})
        if rc != 0:
            self.hook_errors[f"{hook}:{event}:rc={rc}"] += 1
        return out

    def _context_file(self, sid: str) -> Path | None:
        hits = sorted(self.s.state.glob(f"projects/*/session_context_{sid}.jsonl"))
        return hits[0] if hits else None

    def _context_lines(self, sid: str) -> int | None:
        p = self._context_file(sid)
        if p is None:
            return None
        try:
            return sum(1 for _ in p.open(encoding="utf-8", errors="replace"))
        except OSError:
            return None

    # -- doors --
    def _auto_route(self, rec: dict, prompt: str) -> None:
        sid = rec["sid"]
        lc = self.s.state / f"last_classification_{sid}.json"
        before = lc.read_bytes() if lc.exists() else None
        rc, out, ms = run_hook("auto-route", self._payload(sid, "UserPromptSubmit", prompt=prompt),
                               self._env(sid), self.s.workspace)
        after = lc.read_bytes() if lc.exists() else None
        kind = stdout_kind(out)
        if rc != 0:
            self.decisions.append(decision(rec, "hook", error=f"rc={rc}", latency_ms=ms, stdout=kind))
            return
        if after is None or after == before:
            self.decisions.append(decision(rec, "hook", error=f"no_decision:{kind}", latency_ms=ms, stdout=kind))
            return
        j = json.loads(after)
        self.decisions.append(decision(rec, "hook", ok=True, latency_ms=ms, stdout=kind,
                                       task_type=j.get("task_type"), complexity=j.get("complexity"),
                                       layer=j.get("method")))

    def _agent_route(self, rec: dict, tool_input: dict) -> None:
        sid = rec["sid"]
        payload = self._payload(sid, "PreToolUse", tool_name="Agent", tool_input=tool_input,
                                tool_use_id=f"toolu_replay_{rec['idx']:06d}")
        rc, out, ms = run_hook("agent-route", payload, self._env(sid), self.s.workspace)
        kind = stdout_kind(out)
        rows = [r for r in self.agent_tail.new_rows() if r.get("session_id") == sid]
        layer = rows[-1].get("decision") if rows else None
        model = None
        try:
            hso = json.loads(out.strip() or "{}").get("hookSpecificOutput") or {}
            model = (hso.get("updatedInput") or {}).get("model")
        except (ValueError, AttributeError):
            pass
        ok = rc == 0 and (layer is not None or kind != "empty")
        self.decisions.append(decision(rec, "agent-route", ok=ok, latency_ms=ms, stdout=kind, tier=model,
                                       layer=(str(layer).split(":", 1)[0] if layer else kind),
                                       error=None if ok else (f"rc={rc}" if rc else f"no_decision:{kind}")))

    def _proxy(self, rec: dict, variant: str = "full") -> None:
        door = "proxy" if variant == "full" else f"proxy-{variant}"
        status, ms, size = self.proxy.post(proxy_body(rec, variant))  # type: ignore[union-attr]
        self.sizes[f"{door}:{rec['step']}"].append(size)
        row = None
        deadline = time.monotonic() + 5.0
        target = rec["sid"] + ("" if variant == "full" else f"-{variant}")
        while row is None and time.monotonic() < deadline:
            for r in self.proxy_tail.new_rows():
                if r.get("session_id") == target:
                    row = r
            if row is None:
                time.sleep(0.02)
        if status != 200 or row is None:
            self.decisions.append(decision(rec, door, latency_ms=ms,
                                           error=f"http={status}" if status != 200 else "no_ledger_row"))
            return
        self.decisions.append(decision(
            rec, door, ok=True, latency_ms=ms,
            task_type=row.get("tier_task_type") or row.get("task_type"),
            complexity=row.get("tier_complexity") or row.get("complexity"),
            tier=row.get("tier"), layer=row.get("tier_reason") or row.get("cls_source"),
            step_class=row.get("step_class"), proxy_decision=row.get("decision"), proxy_reason=row.get("reason"),
            router_ms=(row["tier_decision_s"] * 1000.0) if isinstance(row.get("tier_decision_s"), (int, float))
            else None,
            session_kind=row.get("session_kind")))

    # -- one session --
    def session(self, recs: list[dict], agent_inputs: dict[str, dict]) -> None:
        sid = recs[0]["sid"]
        hooks_on = bool(self.doors & {"hook", "agent-route"})
        seq = [0]
        turns = sorted({r["turn"] for r in recs})
        if hooks_on:
            self._lifecycle("session-start", "SessionStart", sid, source="startup")
        stop_checks = []
        for turn in turns:
            for rec in (r for r in recs if r["turn"] == turn):
                delta = rec["delta"]
                if rec["step"] == "turn_first":
                    prompt = latest_user_text(delta)
                    if rec["main"]:
                        self._append_transcript(sid, delta[:-1], seq)
                    if "hook" in self.doors:
                        self._auto_route(rec, prompt)
                    if rec["main"]:
                        self._append_transcript(sid, delta[-1:], seq)
                elif rec["step"] == "subagent":
                    brief = latest_user_text(delta)
                    inp = dict(agent_inputs.get(brief) or {"description": "sub-agent", "prompt": brief,
                                                           "subagent_type": "general-purpose"})
                    if "agent-route" in self.doors:
                        self._agent_route(rec, inp)
                        self._lifecycle("subagent-start", "SubagentStart", sid,
                                        agent_type=inp.get("subagent_type"), agent_id=f"agent-{rec['idx']}")
                elif rec["main"]:
                    self._append_transcript(sid, delta, seq)
                    if "agent-route" in self.doors and _has_agent_result(delta):
                        for hook in ("cc-usage-track", "agent-depth-release"):
                            self._lifecycle(hook, "PostToolUse", sid, tool_name="Agent",
                                            tool_input=_agent_input(delta), tool_response={"content": [
                                                {"type": "text", "text": "done"}]})
                if "proxy" in self.doors:
                    self._proxy(rec)
                    if rec["step"] == "turn_first":
                        for variant in PROXY_VARIANTS:
                            self._proxy(rec, variant)
            if hooks_on:
                had = self._context_file(sid) is not None
                self._lifecycle("session-end", "Stop", sid, stop_hook_active=False)
                stop_checks.append({"turn": turn, "had_file": had, "has_file": self._context_file(sid) is not None})
        if hooks_on:
            lines = self._context_lines(sid)
            had = self._context_file(sid) is not None
            self._lifecycle("session-end", "SessionEnd", sid, reason="other")
            self.archive[sid] = [{"turns": len(turns), "stops": stop_checks, "lines_before_end": lines,
                                  "had_file_before_end": had, "has_file_after_end": self._context_file(sid) is not None}]


def _has_agent_result(delta: list) -> bool:
    return _agent_input(delta) is not None


def _agent_input(delta: list) -> dict | None:
    for m in delta:
        content = m.get("content") if isinstance(m, dict) else None
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task"):
                return b.get("input") if isinstance(b.get("input"), dict) else {}
    return None


def archive_checks(archive: dict[str, list]) -> dict:
    """P0.1-a mechanics: Stop never archives; SessionEnd archives; the session file keeps
    >= 5 events for every session of >= 5 turns."""
    sessions = [v[0] for v in archive.values() if v]
    stops = [c for s in sessions for c in s["stops"] if c["had_file"]]
    stop_bad = sum(1 for c in stops if not c["has_file"])
    ends = [s for s in sessions if s["had_file_before_end"]]
    end_bad = sum(1 for s in ends if s["has_file_after_end"])
    long_ = [s for s in sessions if s["turns"] >= 5]
    short_bad = sum(1 for s in long_ if (s["lines_before_end"] or 0) < 5)
    no_file = sum(1 for s in sessions if not s["had_file_before_end"])

    def verdict(bad: int, n: int) -> str:
        return "NO_DATA" if n == 0 else ("PASS" if bad == 0 else "FAIL")

    return {
        "sessions": len(sessions), "sessions_without_context_file": no_file,
        "lines_before_end": sorted(s["lines_before_end"] or 0 for s in sessions),
        "checks": {
            "stop_never_archives": {"verdict": verdict(stop_bad, len(stops)),
                                    "detail": f"{stop_bad} of {len(stops)} Stops with a session file removed it"},
            "session_end_archives": {"verdict": verdict(end_bad, len(ends)),
                                     "detail": f"{end_bad} of {len(ends)} SessionEnds left the file in place"},
            "keeps_ge5_events_when_ge5_turns": {
                "verdict": verdict(short_bad, len(long_)),
                "detail": f"{short_bad} of {len(long_)} sessions with >=5 turns had <5 lines before SessionEnd"},
        },
    }


# ── in-process doors (gateway, sdk, mcp), run in a scratch-env worker ─────────

INPROC_DOORS = ("gateway", "sdk", "mcp")
PROMPT_STEPS = ("turn_first", "subagent")


def run_inprocess(scratch: Scratch, recs: list[dict], doors: set[str]) -> list[dict]:
    """Run the in-process doors in ONE child with the scratch environment and the guard,
    so this process never imports llm_router with the operator's HOME."""
    wanted = [d for d in INPROC_DOORS if d in doors]
    if not wanted:
        return []
    jobs = scratch.root / "inproc_jobs.jsonl"
    results = scratch.root / "inproc_results.jsonl"
    with jobs.open("w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps({"idx": r["idx"], "step": r["step"], "request": r["request"],
                                 "prompt": latest_user_text(r["delta"]) if r["step"] in PROMPT_STEPS else None})
                     + "\n")
    cmd = [sys.executable, str(Path(__file__).resolve()), "--_inproc-worker", str(jobs), str(results),
           ",".join(wanted)]
    proc = subprocess.run(cmd, env=scratch.env(), cwd=str(scratch.workspace), stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=(scratch.root / "inproc.stderr").open("wb"),
                          timeout=max(600.0, 2.0 * len(recs)), check=False)
    by_idx = {r["idx"]: r for r in recs}
    out = []
    for row in _jsonl_rows(results):
        rec = by_idx[row.pop("idx")]
        out.append(decision(rec, row.pop("door"), **row))
    if proc.returncode != 0:
        out.append({"door": "inproc-worker", "sid": "-", "idx": -1, "step": "-", "ok": False,
                    "error": f"worker rc={proc.returncode}"})
    return out


def _inproc_worker(jobs_path: str, out_path: str, doors_csv: str) -> int:  # pragma: no cover - child
    import asyncio

    doors = set(doors_csv.split(","))
    from llm_router import gateway

    captured: dict[str, Any] = {}
    real_classify = gateway._classify

    def spy_classify(prompt: str) -> tuple[str, str]:
        t, c = real_classify(prompt)
        captured["task_type"], captured["complexity"] = t, c
        return t, c

    if "sdk" in doors:
        import llm_router.hooks.direct_executor as de
        from llm_router import sdk

        def fake_chain(prompt: str, chain: list, task_type: str, *a: Any, **k: Any) -> None:
            captured["chain"], captured["agent"] = chain, False
            return None

        def fake_agent(prompt: str, chain: list, *a: Any, **k: Any) -> None:
            captured["chain"], captured["agent"] = chain, True
            return None

        de.execute_chain = fake_chain  # sdk.route imports these at call time
        de.execute_agent = fake_agent
        gateway._classify = spy_classify
    if "mcp" in doors:
        from llm_router import ensemble
    loop = asyncio.new_event_loop()
    with open(jobs_path, encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
        def emit(row: dict) -> None:
            fout.write(json.dumps(row) + "\n")
            fout.flush()

        for line in fin:
            job = json.loads(line)
            idx, prompt = job["idx"], job["prompt"]
            if "gateway" in doors:
                t0 = time.perf_counter()
                try:
                    text = gateway._latest_user_turn(job["request"]) or gateway._flatten(job["request"])
                    tt, cx = real_classify(text)
                    emit({"idx": idx, "door": "gateway", "ok": True, "task_type": tt, "complexity": cx,
                          "layer": "classify_signals:gateway_policy",
                          "latency_ms": (time.perf_counter() - t0) * 1000.0})
                except Exception as exc:  # noqa: BLE001 - recorded, never fatal
                    emit({"idx": idx, "door": "gateway", "error": type(exc).__name__})
            if prompt is None:
                continue
            if "sdk" in doors:
                captured.clear()
                t0 = time.perf_counter()
                try:
                    sdk.route(prompt)
                    err = None
                except sdk.RoutingError:
                    err = None  # expected: the fake executor never answers
                except Exception as exc:  # noqa: BLE001
                    err = type(exc).__name__
                ms = (time.perf_counter() - t0) * 1000.0
                chain = captured.get("chain") or []
                cx = "complex" if captured.get("agent") else captured.get("complexity")
                emit({"idx": idx, "door": "sdk", "ok": err is None and bool(captured.get("task_type")),
                      "error": err or (None if captured.get("task_type") else "no_classification"),
                      "task_type": captured.get("task_type"), "complexity": cx,
                      "layer": "agent_loop" if captured.get("agent") else "chain",
                      "chain_len": len(chain), "latency_ms": ms})
            if "mcp" in doors:
                t0 = time.perf_counter()
                try:
                    res = loop.run_until_complete(ensemble.classify_for_routing(prompt))
                    tt = getattr(res.inferred_task_type, "value", res.inferred_task_type)
                    cx = getattr(res.complexity, "value", res.complexity)
                    emit({"idx": idx, "door": "mcp", "ok": True, "task_type": tt, "complexity": cx,
                          "layer": str(res.classifier_model).split(":", 1)[0],
                          "latency_ms": (time.perf_counter() - t0) * 1000.0})
                except Exception as exc:  # noqa: BLE001
                    emit({"idx": idx, "door": "mcp", "error": type(exc).__name__,
                          "latency_ms": (time.perf_counter() - t0) * 1000.0})
    loop.close()
    return 0


# ── aggregation ──────────────────────────────────────────────────────────────

DECISION_FIELDS = ("task_type", "tier", "complexity", "layer")


def _dist(rows: list[dict], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(r.get(key)) for r in rows).items()))


def door_summary(decisions: list[dict]) -> dict[str, dict]:
    """Per door: calls, outcomes, decision distributions and field presence (deterministic)."""
    by_door: dict[str, list[dict]] = defaultdict(list)
    for d in decisions:
        by_door[d["door"]].append(d)
    out: dict[str, dict] = {}
    for door in sorted(by_door):
        rows = by_door[door]
        ok = [r for r in rows if r.get("ok")]
        out[door] = {
            "calls": len(rows), "by_step": _dist(rows, "step"), "ok": len(ok),
            "errors": dict(sorted(Counter(str(r.get("error")) for r in rows if not r.get("ok")).items())),
            "present": {f: rate(sum(1 for r in ok if r.get(f) not in (None, "")), len(ok)) for f in DECISION_FIELDS},
            "task_type": _dist(ok, "task_type"), "tier": _dist(ok, "tier"),
            "complexity": _dist(ok, "complexity"), "layer": _dist(ok, "layer"),
            "turn_first_task_type": _dist([r for r in ok if r["step"] == "turn_first"], "task_type"),
            "task_type_eq_intended": rate(
                sum(1 for r in ok if r.get("task_type") and r.get("task_type") == r.get("intended_category")),
                sum(1 for r in ok if r.get("task_type"))),
        }
        extra = {k: _dist(ok, k) for k in ("stdout", "step_class", "proxy_decision", "proxy_reason",
                                           "session_kind") if any(k in r for r in ok)}
        if extra:
            out[door]["extra"] = extra
    return out


def latency_summary(decisions: list[dict]) -> dict[str, dict]:
    by: dict[str, list[float]] = defaultdict(list)
    router: dict[str, list[float]] = defaultdict(list)
    for d in decisions:
        if isinstance(d.get("latency_ms"), (int, float)):
            by[d["door"]].append(float(d["latency_ms"]))
        if isinstance(d.get("router_ms"), (int, float)):
            router[d["door"]].append(float(d["router_ms"]))
    out = {}
    for door, v in sorted(by.items()):
        out[door] = {"n": len(v), "p50_ms": percentile(v, 0.5), "p95_ms": percentile(v, 0.95),
                     "max_ms": round(max(v), 2)}
        if router.get(door):
            r = router[door]
            out[door]["router_decision"] = {"n": len(r), "p50_ms": percentile(r, 0.5), "p95_ms": percentile(r, 0.95)}
    return out


def lifecycle_latency(rows: list[dict]) -> dict[str, dict]:
    by: dict[str, list[float]] = defaultdict(list)
    rcs: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        key = f"{r['hook']}:{r['event']}"
        by[key].append(r["wall_ms"])
        rcs[key][str(r["rc"])] += 1
    return {k: {"n": len(v), "p50_ms": percentile(v, 0.5), "p95_ms": percentile(v, 0.95), "rc": dict(rcs[k])}
            for k, v in sorted(by.items())}


def in_process_hook_latency(state: Path) -> dict[str, dict]:
    """``hook_latency.jsonl`` as the hooks wrote it (elapsed from the hook's first statement)."""
    by: dict[str, list[float]] = defaultdict(list)
    added: dict[str, list[float]] = defaultdict(list)
    for name in ("hook_latency.jsonl.1", "hook_latency.jsonl"):
        for r in _jsonl_rows(state / name):
            key = f"{r.get('hook')}:{r.get('event')}"
            if isinstance(r.get("elapsed_ms"), (int, float)):
                by[key].append(float(r["elapsed_ms"]))
            if isinstance(r.get("router_added_ms"), (int, float)):
                added[key].append(float(r["router_added_ms"]))
    out = {}
    for k, v in sorted(by.items()):
        out[k] = {"n": len(v), "p50_ms": percentile(v, 0.5), "p95_ms": percentile(v, 0.95)}
        if added.get(k):
            out[k]["router_added"] = {"n": len(added[k]), "p50_ms": percentile(added[k], 0.5),
                                      "p95_ms": percentile(added[k], 0.95)}
    return out


def agreement_matrix(decisions: list[dict], steps: tuple[str, ...] | None = None) -> dict[str, dict]:
    """Pairwise disagreement on records both doors decided. task_type is compared as each
    door labels it (the vocabularies differ: the proxy has no introspect/coordinate);
    tier on the normalised low/mid/high scale (:data:`TIER_NORM`)."""
    keyed: dict[int, dict[str, dict]] = defaultdict(dict)
    for d in decisions:
        if d.get("ok") and (steps is None or d["step"] in steps):
            keyed[d["idx"]][d["door"]] = d
    doors = sorted({door for v in keyed.values() for door in v})
    out: dict[str, dict] = {}
    for i, a in enumerate(doors):
        for b in doors[i + 1:]:
            both = [v for v in keyed.values() if a in v and b in v]
            if not both:
                continue
            res: dict[str, Any] = {"records": len(both)}
            for field, name in (("task_type", "task_type"), ("tier_norm", "tier")):
                pairs = [(v[a].get(field), v[b].get(field)) for v in both if v[a].get(field) and v[b].get(field)]
                res[name] = rate(sum(1 for x, y in pairs if x != y), len(pairs))
            res["task_type_pairs"] = dict(sorted(Counter(
                f"{v[a].get('task_type')}|{v[b].get('task_type')}" for v in both
                if v[a].get("task_type") and v[b].get("task_type")).items()))
            out[f"{a} vs {b}"] = res
    return out


# ── rendering ────────────────────────────────────────────────────────────────


def _fmt_rate(r: dict | None) -> str:
    if not r or not r.get("n"):
        return "n=0"
    ci = r.get("ci95") or [0.0, 0.0]
    return f"{r['rate']:.3f} ({r['k']}/{r['n']}; CI {ci[0]:.3f}-{ci[1]:.3f})"


def render_md(summary: dict) -> str:
    det, timing, run = summary["deterministic"], summary["timing"], summary["run"]
    c = det["corpus"]
    lines = [
        "# Synthetic replay summary", "",
        "**SYNTHETIC / non-organic** (owner decision D-42): evidence for mechanics gates only. Not a",
        "routing-quality, accuracy, North Star or organic-session number. `intended_category` is",
        "authorial intent, not ground truth.", "",
        f"- corpus sha256 `{c['sha256'][:16]}`: {c['sessions']} sessions, {c['records']} records {c['by_step']}",
        f"- git `{run.get('git_sha')}`; doors {', '.join(det['doors'])}; wall {timing.get('wall_s')} s",
        f"- outbound attempts refused by the guard: **{det['network']['violations']}** "
        f"{det['network']['violation_kinds']}; stub hits {timing.get('stub_hits')}",
        f"- lingering child processes after the run: {timing.get('lingering_children')}",
        f"- synthetic tag on rows of writers that have a tag field: "
        f"{_fmt_rate(det['synthetic_tagging']['rows_in_writers_with_a_tag_field'])}; rows claiming real traffic: "
        f"{det['synthetic_tagging']['rows_claiming_real']}; writers with no tag field at all: "
        f"{det['synthetic_tagging']['writers_without_any_tag_field']}",
        f"- **hook vs proxy task_type on turn_first**: "
        f"{_fmt_rate((det['headline']['hook_vs_proxy_turn_first'] or {}).get('task_type'))}", "",
        "## Per door", "",
        "| door | calls | ok | task_type | tier | complexity | layer | p50 ms | p95 ms | n |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for door, d in det["doors_summary"].items():
        lat = timing["latency"].get(door, {})
        p = d["present"]
        lines.append(f"| {door} | {d['calls']} | {d['ok']} | {p['task_type']['rate']} | {p['tier']['rate']} | "
                     f"{p['complexity']['rate']} | {p['layer']['rate']} | {lat.get('p50_ms')} | "
                     f"{lat.get('p95_ms')} | {lat.get('n', 0)} |")
    lines += ["", "Field columns: share of decided calls carrying the field. Latency: wall time per call as the",
              "caller sees it (hooks: process start to exit; proxy: request to last byte; in-process: the call).", ""]
    for door, d in det["doors_summary"].items():
        lines.append(f"- **{door}** errors {d['errors']}; task_type {d['task_type']}; tier {d['tier']}; "
                     f"layer {d['layer']}")
    for title, key in (("turn_first only", "turn_first"), ("all steps", "all")):
        lines += ["", f"## Door agreement, {title} (disagreement rate, Wilson 95% CI)", "",
                  "| pair | records | task_type disagree | tier disagree |", "|---|---|---|---|"]
        for pair, r in det["agreement"][key].items():
            lines.append(f"| {pair} | {r['records']} | {_fmt_rate(r['task_type'])} | {_fmt_rate(r['tier'])} |")
    env = det["proxy_envelope"]
    lines += ["", "## Proxy label vs request envelope (turn_first)", "",
              f"- proxy task_type with system+tools: {env.get('full')}",
              f"- without system prompt: {env.get('nosys')}",
              f"- without system prompt and tools (a tool-less call is a side_call, never classified): {env.get('bare')}",
              f"- with a code-heavy <system-reminder> in the prompt message: {env.get('reminder')}",
              f"- request bytes: {det['request_sizes']}", "",
              "## Ledger completeness per writer", "",
              "| writer | rows | synthetic-tagged | claims real | session_id | reason | reason_code | task_id |",
              "|---|---|---|---|---|---|---|---|"]
    for name, w in det["ledgers"].items():
        f = w["fields"]
        cells = [_fmt_rate(f[k]) if k in f else "-" for k in ("session_id", "reason", "reason_code", "task_id")]
        tag = _fmt_rate(w["synthetic"]) if w["tag_fields_in_schema"] else "no tag field"
        lines.append(f"| {name} | {w['rows']} | {tag} | {w['claims_real']} | " + " | ".join(cells) + " |")
    arch = det["archive"]
    lines += ["", "## Archive behaviour (P0.1-a)", "",
              f"- sessions {arch['sessions']}; without a session_context file {arch['sessions_without_context_file']};"
              f" lines before SessionEnd {arch['lines_before_end']}"]
    for k, v in arch["checks"].items():
        lines.append(f"- {k}: **{v['verdict']}** ({v['detail']})")
    lines += ["", "## Hook latency (P0.9 mechanics)", "", "| hook:event | n | p50 ms (wall) | p95 ms (wall) |",
              "|---|---|---|---|"]
    for k, v in timing["lifecycle"].items():
        lines.append(f"| {k} | {v['n']} | {v['p50_ms']} | {v['p95_ms']} |")
    lines += ["", "In-process (`hook_latency.jsonl`, from the hook's first statement):", ""]
    for k, v in timing["in_process_hooks"].items():
        lines.append(f"- {k}: n={v['n']} p50 {v['p50_ms']} p95 {v['p95_ms']}"
                     + (f"; router_added p50 {v['router_added']['p50_ms']} p95 {v['router_added']['p95_ms']}"
                        if "router_added" in v else ""))
    lines.append("")
    return "\n".join(lines)


# ── run ──────────────────────────────────────────────────────────────────────


def _git_sha() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10, check=False).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _reap(scratch: Scratch, wait_s: float = 60.0) -> dict:
    """Wait for this run's detached grandchildren (marker in their environment) to exit;
    terminate, by PID, any that outlive ``wait_s``. Never matches by name."""
    deadline = time.monotonic() + wait_s
    pids = scratch.descendants()
    seen = len(pids)
    while pids and time.monotonic() < deadline:
        time.sleep(0.5)
        pids = scratch.descendants()
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except OSError:
            pass
    return {"seen_after_run": seen, "terminated_after_wait": killed}


def run(corpus: Path, out: Path, doors: set[str], *, sessions: int | None = None,
        work: Path | None = None, keep_work: bool = False) -> int:
    """Replay ``corpus`` and write summary.json / summary.md to ``out``. Returns the exit
    code: 0 ok, 1 a guard violation or a door could not run, 2 bad corpus."""
    try:
        recs = load_corpus(corpus, sessions)
    except (CorpusError, OSError, UnicodeDecodeError) as exc:
        sys.stderr.write(f"synthetic_replay: {exc}\n")
        return 2
    t_start = time.monotonic()
    corpus, out = corpus.resolve(), out.resolve()
    root = Path(tempfile.mkdtemp(prefix="synthetic-replay-", dir=str(work.resolve()) if work else None)).resolve()
    rc = 0
    try:
        with StubServer() as stub:
            proxy_port = free_port() if "proxy" in doors else None
            scratch = Scratch(root, stub, proxy_port)
            import importlib.util

            spec = importlib.util.spec_from_file_location("_replay_netguard_self", GUARD_SRC)
            guard = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(guard)  # type: ignore[union-attr]
            guard.install(allow=scratch.allow(), violations=str(scratch.violations))
            try:
                rc = _run_in(scratch, stub, recs, corpus, out, doors, root, keep_work, t_start)
            finally:
                guard.uninstall()
    finally:
        if not keep_work:
            shutil.rmtree(root, ignore_errors=True)
    return rc


def _run_in(scratch: Scratch, stub: StubServer, recs: list[dict], corpus: Path, out: Path,
            doors: set[str], root: Path, keep_work: bool, t_start: float) -> int:
    rc = 0
    proxy_port = scratch.proxy_port
    proxy_cm = Proxy(scratch, proxy_port) if proxy_port else None
    errors: list[str] = []
    rep = Replayer(scratch, doors, proxy_cm)
    try:
        if proxy_cm:
            proxy_cm.__enter__()
        by_sid: dict[str, list[dict]] = defaultdict(list)
        for r in recs:
            by_sid[r["sid"]].append(r)
        inputs = agent_calls(recs)
        for sid in by_sid:
            rep.session(by_sid[sid], inputs)
        rep.decisions.extend(run_inprocess(scratch, recs, doors))
    except Exception as exc:  # noqa: BLE001 - reported, exit 1
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        if proxy_cm:
            proxy_cm.__exit__()
    reaped = _reap(scratch)
    violations = scratch.violation_rows()
    dec = [d for d in rep.decisions if d["door"] != "inproc-worker"]
    errors += [d["error"] for d in rep.decisions if d["door"] == "inproc-worker"]
    turn_first = [d for d in dec if d["step"] == "turn_first" and d.get("ok")]
    summary = {
        "deterministic": {
            "label": "synthetic-non-organic (D-42): mechanics evidence only",
            "corpus": {"sha256": corpus_digest(corpus), "sessions": len({r["sid"] for r in recs}),
                       "records": len(recs), "by_step": _dist(recs, "step")},
            "doors": sorted(doors),
            "doors_summary": door_summary(dec),
            "agreement": {"turn_first": agreement_matrix(dec, ("turn_first",)),
                          "all": agreement_matrix(dec)},
            "proxy_envelope": {v: _dist([d for d in turn_first if d["door"] == door], "task_type")
                               for v, door in [("full", "proxy")]
                               + [(x, f"proxy-{x}") for x in PROXY_VARIANTS]},
            "request_sizes": {k: size_summary(v) for k, v in sorted(rep.sizes.items())},
            "ledgers": writer_completeness(scratch.state),
            "archive": archive_checks(rep.archive),
            "hook_exit_codes": dict(sorted(rep.hook_errors.items())),
            "network": {"violations": len(violations),
                        "violation_kinds": _dist(violations, "kind") if violations else {},
                        "violation_targets": sorted({f"{v.get('host')}:{v.get('port')}" for v in violations})},
            "errors": errors,
        },
        "timing": {
            "wall_s": round(time.monotonic() - t_start, 1),
            "latency": latency_summary(dec),
            "lifecycle": lifecycle_latency(rep.lifecycle),
            "in_process_hooks": in_process_hook_latency(scratch.state),
            "stub_hits": dict(sorted(stub.hits.items())),
            "lingering_children": reaped,
        },
        "run": {"git_sha": _git_sha(), "python": sys.version.split()[0],
                "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "work_dir_kept": str(root) if keep_work else None},
    }
    det = summary["deterministic"]
    led = det["ledgers"]
    capable = [w for w in led.values() if w["tag_fields_in_schema"]]
    det["synthetic_tagging"] = {
        "rows_in_writers_with_a_tag_field": rate(sum(w["synthetic"]["k"] for w in capable),
                                                  sum(w["rows"] for w in capable)),
        "rows_claiming_real": sum(w["claims_real"] for w in led.values()),
        "writers_without_any_tag_field": {n: w["rows"] for n, w in led.items()
                                          if not w["tag_fields_in_schema"]},
    }
    det["headline"] = {
        "hook_vs_proxy_turn_first": det["agreement"]["turn_first"].get("hook vs proxy"),
        "proxy_task_type_by_envelope": det["proxy_envelope"],
    }
    if violations or errors:
        rc = 1
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "summary.md").write_text(render_md(summary), encoding="utf-8")
    return rc


def _parse_doors(text: str) -> set[str]:
    doors = {d.strip() for d in text.split(",") if d.strip()}
    unknown = doors - set(ALL_DOORS)
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown door(s): {sorted(unknown)}; choose from {ALL_DOORS}")
    return doors


def _on_term(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt  # unwind through every finally: proxy stopped, stub closed, scratch removed


def _alarm(_signum: int, _frame: Any) -> None:
    raise TimeoutError(f"synthetic_replay exceeded {RUN_TIME_LIMIT_S:.0f}s")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--_inproc-worker":
        return _inproc_worker(*argv[1:4])
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--doors", type=_parse_doors, default=set(ALL_DOORS))
    ap.add_argument("--sessions", type=int, default=None, help="replay only the first N sessions")
    ap.add_argument("--work", type=Path, default=None, help="parent dir for the scratch root (default: TMPDIR)")
    ap.add_argument("--keep-work", action="store_true", help="keep the scratch root for debugging")
    ap.add_argument("--time-limit-s", type=float, default=RUN_TIME_LIMIT_S)
    a = ap.parse_args(argv)
    prev_term = signal.signal(signal.SIGTERM, _on_term)
    prev_alarm = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(max(1, int(a.time_limit_s)))
    try:
        return run(a.corpus, a.out, a.doors, sessions=a.sessions, work=a.work, keep_work=a.keep_work)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev_alarm)
        signal.signal(signal.SIGTERM, prev_term)


if __name__ == "__main__":
    sys.exit(main())
