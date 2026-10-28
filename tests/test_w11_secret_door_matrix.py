"""W1-1 / UN-04: the privacy door matrix. Every prompt-carrying call site that can send text off this machine is
classified, and a socket trace shows a secret prompt makes no non-loopback connection through any door.

* AST inventory: every call to ``route_and_call`` / ``route_and_stream`` / ``route_payload*`` / ``call_llm*`` /
  ``litellm.*completion`` under ``src/llm_router`` is keyed ``(file, enclosing function, callee)`` and must appear in
  ``MATRIX`` with a door class and a reason (and the table may not list a site that is gone). A new call site fails
  until somebody classifies it. The scan must find at least ``MIN_SITES`` sites, so an empty scan cannot pass.
* Socket trace: ``socket.socket.connect`` / ``connect_ex`` / ``getaddrinfo`` are replaced in-process. They record the
  peer and never open a connection (a loopback name resolves for real, nothing else does). A secret prompt goes
  through every door with a chain of a loopback Ollama model and a cloud model; the recorded peers must all be
  loopback. Positive controls: with the door open (``scan_secrets`` -> no hits, or ``private_dispatch_active`` ->
  False at the provider layer) the same prompt records a non-loopback peer, so the tracer can see a leak.
* ``judge_cascade.judge_inline`` sends prompt + response to a Haiku judge with no private check. It is not reachable
  from any entry point (see ``test_judge_cascade_is_not_reachable``); ``test_..._would_leak_if_wired`` documents
  the latent gap as a strict xfail.

No real network, no real model, tmp homes (the lc12 fixtures). Secrets are generated at run time.
"""
# ruff: noqa: F811  (fixtures are imported and used as arguments)
from __future__ import annotations

import ast
import asyncio
import collections
import ipaddress
import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import litellm
import pytest

from llm_router import local_privacy, providers, router
from llm_router.types import TaskType
from tests import secret_fixtures as fx
from tests.test_lc12 import passed, world  # noqa: F401
from tests.test_lc12c import LOCAL, e2e  # noqa: F401
from tests.test_lc8_routing import JOB

SRC = Path(__file__).resolve().parents[1] / "src" / "llm_router"
CLOUD = "openai/gpt-4o"
REAL_CALL_TEXT = router._call_text
CALLEES = {"route_and_call", "route_and_stream", "route_payload", "route_payload_async", "call_llm",
           "call_llm_stream", "call_llm_stream_events", "acompletion", "completion"}
MIN_SITES = 14  # the scope file names 14; the real scan finds more

# door classes
ROUTER = "router door"        # reaches route_and_call / route_and_stream: scan_secrets, local_only_chain, marker
PROVIDER = "provider guard"   # providers.* reads the marker and refuses a non-local model (_private_refusals)
OWN = "own check"             # the site checks for a secret itself before it sends
LOCAL_ONLY = "local by construction"
FIXED = "fixed prompt"        # no user text in the prompt
DEAD = "unreachable"          # no entry point reaches it; see test_judge_cascade_is_not_reachable

#: (file, enclosing function, callee) -> (n sites, class, why/evidence)
MATRIX: dict[tuple[str, str, str], tuple[int, str, str]] = {
    # ---- the router doors: route_and_call callers inherit the marker (router.py scan_secrets / local_only_chain)
    ("commands/benchmark.py", "_run_async", "route_and_call"): (1, ROUTER, "benchmark prompts through route_and_call"),
    ("context.py", "auto_summarize_session", "route_and_call"): (1, ROUTER, "session summary through route_and_call"),
    ("integrations/agno.py", "RouteredModel._aroute", "route_and_call"): (1, ROUTER, "agno adapter"),
    ("orchestrator.py", "run_pipeline", "route_and_call"): (1, ROUTER, "orchestrator"),
    ("orchestrator.py", "auto_orchestrate", "route_and_call"): (1, ROUTER, "orchestrator decompose"),
    ("quickstart.py", "main._test", "route_and_call"): (1, ROUTER, "fixed test prompt, router anyway"),
    ("route_server.py", "route_payload_async", "route_and_call"): (1, ROUTER, "route server + gateway core; typed 422"),
    ("tools/agentic.py", "_default_planner.planner_model", "route_and_call"): (1, ROUTER, "MCP agentic planner"),
    ("tools/fs.py", "llm_fs_find", "route_and_call"): (1, ROUTER, "MCP fs"),
    ("tools/fs.py", "llm_fs_rename", "route_and_call"): (1, ROUTER, "MCP fs"),
    ("tools/fs.py", "llm_fs_edit_many", "route_and_call"): (1, ROUTER, "MCP fs"),
    ("tools/fs.py", "llm_fs_analyze_context", "route_and_call"): (1, ROUTER, "MCP fs"),
    ("tools/media.py", "llm_image", "route_and_call"): (1, ROUTER, "MCP media; no local image model, so refused"),
    ("tools/media.py", "llm_video", "route_and_call"): (1, ROUTER, "MCP media"),
    ("tools/media.py", "llm_audio", "route_and_call"): (1, ROUTER, "MCP media"),
    ("tools/routing.py", "llm_route", "route_and_call"): (1, ROUTER, "MCP llm_route"),
    ("tools/routing.py", "llm_auto", "route_and_call"): (1, ROUTER, "MCP llm_auto"),
    ("tools/text.py", "llm_query", "route_and_call"): (1, ROUTER, "MCP llm query"),
    ("tools/text.py", "llm_research", "route_and_call"): (1, ROUTER, "MCP"),
    ("tools/text.py", "llm_generate", "route_and_call"): (1, ROUTER, "MCP"),
    ("tools/text.py", "llm_analyze", "route_and_call"): (1, ROUTER, "MCP"),
    ("tools/text.py", "llm_reason", "route_and_call"): (1, ROUTER, "MCP"),
    ("tools/text.py", "llm_code", "route_and_call"): (1, ROUTER, "MCP"),
    ("tools/text.py", "llm_edit", "route_and_call"): (1, ROUTER, "MCP llm_edit (exempt only from eligibility)"),
    ("tools/text.py", "llm_text_job", "route_and_call"): (1, ROUTER, "MCP text job; also pass-only local"),
    ("tui/cli.py", "_run_classic_mode", "route_and_call"): (1, ROUTER, "TUI classic mode"),
    # ---- gateway / SDK doors reach the router through these (route_payload_async) or their own chain cut
    ("gateway.py", "_route", "route_payload_async"): (1, ROUTER, "gateway /route; chat, responses, messages, ollama"),
    ("gateway.py", "route", "route_payload_async"): (1, ROUTER, "gateway POST /route"),
    ("route_server.py", "make_handler._Handler.do_POST", "route_payload"): (1, ROUTER, "stand-alone route server HTTP"),
    ("route_server.py", "route_payload", "route_payload_async"): (1, ROUTER, "stand-alone route server"),
    ("tui/app.py", "run_dashboard.stream_worker", "route_and_stream"): (1, ROUTER, "TUI streaming"),
    # ---- the provider layer itself
    ("providers.py", "call_llm", "acompletion"): (2, PROVIDER, "private_dispatch_active -> _private_refusals"),
    ("providers.py", "_stream_events", "acompletion"): (1, PROVIDER, "same refusals (LC-12h)"),
    ("providers.py", "call_llm_stream", "call_llm_stream_events"): (1, PROVIDER, "wrapper over the guarded stream"),
    ("router.py", "_call_text", "call_llm"): (1, PROVIDER, "the router's one text dispatch"),
    ("router.py", "route_and_stream", "call_llm_stream_events"): (1, ROUTER, "stream door: own scan, private=True"),
    # ---- direct provider callers that do not set the marker
    ("tools/routing.py", "llm_stream", "call_llm_stream"): (1, OWN, "scan_secrets + local_only_chain, private=_secret"),
    ("classifier.py", "classify_complexity", "call_llm"): (1, OWN, "contains_secret -> heuristic, no model call"),
    ("judge.py", "_evaluate_background", "call_llm"): (1, OWN, "queue refuses under private_dispatch_active"),
    ("ensemble.py", "local_llm_classify", "call_llm"): (1, LOCAL_ONLY, "discovered Ollama model; env override is the user's"),
    ("tools/setup.py", "_setup_test", "call_llm"): (1, FIXED, "'Reply with exactly: OK'"),
    ("resolver/calibrate.py", "CloudCompleter._litellm", "completion"): (1, FIXED, "calibration probes, allow_paid only"),
    ("judge_cascade.py", "judge_inline", "call_llm"): (1, DEAD, "prompt+response to a Haiku judge; no entry point"),
}


def _scan() -> collections.Counter:
    found: collections.Counter = collections.Counter()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = path.relative_to(SRC).as_posix()

        def visit(node, stack):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                stack = stack + [node.name]
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
                if name in CALLEES and (name != "completion" or "litellm" in ast.dump(f)):
                    found[(rel, ".".join(stack) or "<module>", name)] += 1
            for child in ast.iter_child_nodes(node):
                visit(child, stack)
        visit(tree, [])
    return found


def classify_sites(found: collections.Counter, matrix=MATRIX) -> list[str]:
    """Problems: a site that is not in the matrix, a count that differs, a matrix row with no site."""
    problems = []
    for key, n in sorted(found.items()):
        if key not in matrix:
            problems.append(f"UNCLASSIFIED call site {key[0]}::{key[1]} calls {key[2]} x{n}")
        elif matrix[key][0] != n:
            problems.append(f"{key[0]}::{key[1]} {key[2]}: {n} sites, matrix says {matrix[key][0]}")
    for key in sorted(matrix):
        if key not in found:
            problems.append(f"STALE matrix row {key[0]}::{key[1]} {key[2]}: no such call site")
    return problems


def test_every_prompt_carrying_call_site_is_classified():
    found = _scan()
    assert sum(found.values()) >= MIN_SITES, "the scan found too few call sites: an empty scan passes everything"
    assert classify_sites(found) == []


def test_matrix_summary_counts_the_sites():
    found = _scan()
    by_class = collections.Counter()
    for key, n in found.items():
        by_class[MATRIX[key][1]] += n
    assert sum(by_class.values()) == sum(found.values())
    assert by_class[DEAD] == 1 and by_class[ROUTER] >= 25


def test_negative_control_an_unclassified_call_site_fails(tmp_path, monkeypatch):
    """A temp copy of the tree with one new ``litellm.acompletion`` caller: the classification test must go RED."""
    import shutil
    copy = tmp_path / "llm_router"
    shutil.copytree(SRC, copy, ignore=shutil.ignore_patterns("__pycache__", "data", "*.json", "*.gz"))
    (copy / "new_door.py").write_text(
        "import litellm\n\nasync def leak(prompt):\n    return await litellm.acompletion(model='x', messages=[prompt])\n")
    monkeypatch.setitem(globals(), "SRC", copy)
    problems = classify_sites(_scan())
    assert problems == ["UNCLASSIFIED call site new_door.py::leak calls acompletion x1"]
    # and the real assertion, as the real test makes it, fails on this tree
    with pytest.raises(AssertionError):
        assert classify_sites(_scan()) == []


def test_judge_cascade_is_not_reachable():
    """``judge_cascade`` -> ``streaming_judge`` -> nobody. Import graph over src plus the string check for dynamic
    imports; wiring either module in fails here until the private check below is added."""
    importers: dict[str, set[str]] = collections.defaultdict(set)
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        mod = path.relative_to(SRC).with_suffix("").as_posix().replace("/", ".")
        for target in ("judge_cascade", "streaming_judge"):
            if target in text and mod != target:
                importers[target].add(mod)
    # env_registry only names the env vars; it imports nothing
    importers = {k: {m for m in v if m != "env_registry"} for k, v in importers.items()}
    assert importers.get("judge_cascade") == {"streaming_judge"}
    assert importers.get("streaming_judge", set()) == set()


# ------------------------------------------------------------------------------------------------ the socket trace
class Trace:
    def __init__(self):
        self.peers: list[str] = []

    @staticmethod
    def loopback(host) -> bool:
        if host in ("localhost", "ip6-localhost"):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    @property
    def off_machine(self) -> list[str]:
        return [p for p in self.peers if not self.loopback(p)]


@pytest.fixture
def trace(monkeypatch):
    t = Trace()
    real_gai = socket.getaddrinfo

    def peer(addr):
        t.peers.append(addr[0] if isinstance(addr, tuple) else "unix")

    def connect(self, addr, *a):
        peer(addr)
        raise ConnectionRefusedError("W1-1 trace: no connection is ever made")

    def connect_ex(self, addr, *a):
        peer(addr)
        return 111

    def getaddrinfo(host, *a, **k):
        t.peers.append(host if isinstance(host, str) else repr(host))
        if isinstance(host, str) and Trace.loopback(host):
            return real_gai(host, *a, **k)
        raise socket.gaierror("W1-1 trace: no resolution")
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return t


@pytest.fixture
def net(e2e, trace, monkeypatch):
    """The real ``_call_text`` and provider layer over a chain [loopback Ollama, cloud]; the only 'network' is a
    ``litellm.acompletion`` that connects to its model's host (and is refused by the trace)."""
    monkeypatch.setattr(router, "_call_text", REAL_CALL_TEXT)
    e2e.state["chain"] = [LOCAL, CLOUD]
    monkeypatch.setattr("llm_router.profiles.get_model_chain", lambda *a, **k: [LOCAL, CLOUD])
    monkeypatch.setattr(router, "get_model_chain", lambda *a, **k: [LOCAL, CLOUD])
    monkeypatch.setattr("llm_router.tools.routing.route_and_call", router.route_and_call)
    monkeypatch.setattr("llm_router.tools.fs.route_and_call", router.route_and_call)
    monkeypatch.setattr("llm_router.tools.media.route_and_call", router.route_and_call)

    async def acompletion(**kw):
        host = "127.0.0.1" if str(kw.get("model", "")).startswith("ollama") else "203.0.113.9"  # TEST-NET-3
        socket.create_connection((host, 11434 if host == "127.0.0.1" else 443))
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    monkeypatch.setenv("OLLAMA_API_BASE", "http://127.0.0.1:11434")
    e2e.trace = trace
    e2e.monkeypatch = monkeypatch
    return e2e


def _ctx():
    ctx = MagicMock()
    ctx.info = AsyncMock()
    return ctx


def _secret() -> str:
    return fx.real_shaped("github_token", 3)


def _swallow(coro):
    try:
        return asyncio.run(coro)
    except Exception as e:  # noqa: BLE001 - a refusal or a refused connection is the point; the trace is the result
        return e


def _door_route_and_call(net, s):
    return _swallow(router.route_and_call(TaskType.QUERY, f"explain {s}"))


def _door_route_and_stream(net, s):
    async def go():
        async for _ in router.route_and_stream(TaskType.QUERY, f"explain {s}"):
            pass
    return _swallow(go())


def _door_mcp_query(net, s):
    from llm_router.tools import text
    return _swallow(text.llm_query(prompt=f"explain {s}", ctx=_ctx()))


def _door_mcp_route(net, s):
    from llm_router.tools import routing
    return _swallow(routing.llm_route(prompt=f"explain {s}", ctx=_ctx()))


def _door_mcp_stream(net, s):
    from llm_router.tools import routing
    return _swallow(routing.llm_stream(prompt=f"explain {s}", ctx=_ctx()))


def _door_mcp_edit(net, s, tmp=None):
    from llm_router.tools import text
    import tempfile
    f = Path(tempfile.mkdtemp()) / "a.py"
    f.write_text("x = 1\n")
    return _swallow(text.llm_edit(task=f"fix {s}", files=[str(f)], ctx=_ctx()))


def _door_mcp_text_job(net, s):
    from llm_router.tools import text
    passed(net)
    return _swallow(text.llm_text_job(JOB, f"diff --git a/src/a.py b/src/a.py\n+key = {s}\n", _ctx()))


def _door_gateway(path, body):
    def go(net, s):
        from fastapi.testclient import TestClient

        from llm_router import gateway
        client = TestClient(gateway.app, base_url="http://127.0.0.1")
        try:
            return client.post(path, json=body(s))
        except Exception as e:  # noqa: BLE001
            return e
    return go


def _door_route_server(net, s):
    import llm_router.route_server as RS
    return _swallow(RS.route_payload_async({"prompt": f"explain {s}", "task_type": "query"}))


def _door_sdk(run):
    def go(net, s):
        from llm_router import sdk
        from llm_router.hooks import chain_builder, direct_executor as de
        net_mp = net.monkeypatch
        chain = [de.ModelSpec("openai", "gpt-4o"), de.ModelSpec("ollama", "qwen3:8b")]
        net_mp.setattr(chain_builder, "build_chain", lambda *a, **k: list(chain))
        net_mp.setattr(chain_builder, "needs_claude_tools", lambda *a, **k: False)
        net_mp.setattr(chain_builder, "get_current_pressure", lambda: ("green", 5))
        net_mp.setattr(sdk, "_decide", lambda *a, **k: ("query", "simple", None))
        net_mp.setattr(de, "_get_ollama_url", lambda: "http://127.0.0.1:11434")
        net_mp.setattr("llm_router.hooks.savings_logger.log_direct_to_db", lambda **k: None)
        net_mp.setattr("llm_router.hooks.savings_logger.log_direct_savings", lambda **k: None)
        try:
            return run(sdk, f"explain {s}")
        except Exception as e:  # noqa: BLE001
            return e
    return go


def _chat(s):
    return {"model": "auto", "messages": [{"role": "user", "content": f"explain {s}"}]}


DOORS = {
    "route_and_call": _door_route_and_call,
    "route_and_stream": _door_route_and_stream,
    "mcp llm_query": _door_mcp_query,
    "mcp llm_route": _door_mcp_route,
    "mcp llm_stream": _door_mcp_stream,
    "mcp llm_edit": _door_mcp_edit,
    "mcp llm_text_job": _door_mcp_text_job,
    "sdk route": _door_sdk(lambda sdk, p: sdk.route(p)),
    "sdk Router.run": _door_sdk(lambda sdk, p: sdk.Router().run(p)),
    "gateway /v1/chat/completions": _door_gateway("/v1/chat/completions", _chat),
    "gateway /v1/responses": _door_gateway("/v1/responses", lambda s: {"model": "auto", "input": f"explain {s}"}),
    "gateway /v1/messages": _door_gateway(
        "/v1/messages", lambda s: {**_chat(s), "max_tokens": 16}),
    "gateway /api/chat": _door_gateway("/api/chat", _chat),
    "gateway /api/generate": _door_gateway("/api/generate", lambda s: {"model": "auto", "prompt": f"explain {s}"}),
    "gateway /route": _door_gateway("/route", lambda s: {"prompt": f"explain {s}"}),
    "route_server": _door_route_server,
}
#: doors that are local-only by their own pass (a text job) and so have no cloud entry for a control to reach
NO_CONTROL = {"mcp llm_text_job"}


@pytest.mark.parametrize("door", sorted(DOORS))
def test_secret_prompt_makes_no_non_loopback_connection(door, net):
    mp = net.monkeypatch
    out = DOORS[door](net, _secret())
    t = net.trace
    assert t.off_machine == [], f"{door}: secret prompt reached {t.off_machine}"
    assert t.peers or isinstance(out, local_privacy.SecretLocalOnlyRefused) or out is not None, (
        f"{door}: nothing was traced and nothing was refused; the door did not run")
    assert t.peers, f"{door}: the loopback model was never tried, so the trace proved nothing"


@pytest.mark.parametrize("door", sorted(set(DOORS) - NO_CONTROL))
def test_positive_control_door_open_records_a_non_loopback_peer(door, net):
    """Same door, secret detection off: the cloud entry is tried and the tracer sees it."""
    mp = net.monkeypatch
    mp.setattr(local_privacy, "scan_secrets", lambda *a, **k: [])
    mp.setattr(local_privacy, "contains_secret", lambda *a, **k: False)
    if door == "mcp llm_stream":  # the stream tool dials the first chain entry only
        mp.setattr("llm_router.profiles.get_model_chain", lambda *a, **k: [CLOUD, LOCAL])
    DOORS[door](net, _secret())
    assert net.trace.off_machine, f"{door}: with the door open the tracer saw no off-machine peer"


def test_positive_control_provider_layer_marker_off_connects(net):
    mp = net.monkeypatch
    """The provider layer alone: the marker set and ``private_dispatch_active`` real -> refused, no peer; patched to
    False -> the cloud model is dialled."""
    msgs = [{"role": "user", "content": f"explain {_secret()}"}]

    async def go():
        tok = local_privacy.set_secret_dispatch(True)
        try:
            return await providers.call_llm(CLOUD, msgs)
        finally:
            local_privacy.reset_secret_dispatch(tok)
    out = _swallow(go())
    assert net.trace.peers == [] and out is not None
    mp.setattr(local_privacy, "private_dispatch_active", lambda: False)
    _swallow(go())
    assert net.trace.off_machine == ["203.0.113.9"]


def test_provider_stream_layer_refuses_a_cloud_model_under_the_marker(net):
    async def go():
        tok = local_privacy.set_secret_dispatch(True)
        try:
            async for _ in providers.call_llm_stream_events(CLOUD, [{"role": "user", "content": _secret()}]):
                pass
        finally:
            local_privacy.reset_secret_dispatch(tok)
    out = _swallow(go())
    assert isinstance(out, Exception) and net.trace.peers == []


# ------------------------------------------------------------------------------------------------ latent findings
def test_judge_cascade_would_leak_if_wired(net):
    """``judge_inline`` carries the prompt (a secret included) and the response to a Haiku judge model without
    ``private`` / the marker. Unreachable today (above); this records what wiring it in would do. Strict xfail:
    the day the private check is added it XPASSes and the mark must be removed."""
    from llm_router import judge_cascade
    _swallow(judge_cascade.judge_inline(f"explain {_secret()}", "an answer", "query"))
    assert net.trace.off_machine == []


test_judge_cascade_would_leak_if_wired = pytest.mark.xfail(
    strict=True, reason="UN-04 finding: judge_cascade.judge_inline sends prompt+response to a cloud judge with no "
                        "private check (unreachable today)")(test_judge_cascade_would_leak_if_wired)


def test_ensemble_classifier_with_an_explicit_cloud_primary_would_leak(net):
    """``LLM_ROUTER_ENSEMBLE_PRIMARY`` names the classifier model and is not required to be local; the classifier
    call sets no marker, so a secret prompt goes where the user pointed it. Opt-in config, strict xfail."""
    from llm_router import ensemble
    _swallow(ensemble.local_llm_classify(f"explain {_secret()}", CLOUD, timeout_seconds=5))
    assert net.trace.off_machine == []


test_ensemble_classifier_with_an_explicit_cloud_primary_would_leak = pytest.mark.xfail(
    strict=True, reason="UN-04 finding: ensemble.local_llm_classify trusts LLM_ROUTER_ENSEMBLE_PRIMARY to be local")(
    test_ensemble_classifier_with_an_explicit_cloud_primary_would_leak)
