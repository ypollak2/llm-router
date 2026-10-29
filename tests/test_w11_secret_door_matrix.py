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
* Sink inventory (``_scan_sinks``): a second scan keyed by what leaves the machine, not by the router's own names: CLI
  runners (run_codex / run_gemini_cli / run_claude), direct_executor call_gemini / call_openai, urlopen / open_url,
  httpx / requests / aiohttp requests, subprocess to a model CLI, anthropic / openai SDK clients, a bare litellm
  ``completion``. Every hit has a row or its file has a pinned-count row; ``probe`` rows are the audited "carries no
  prompt text" allowlist. Known limit: ``subprocess.*`` is counted only when the call names a model CLI. Swapping one sink for another inside an audited file keeps its pinned count (design limit). UNSCANNED_FAMILIES lists what no name scan can see (the OTLP exporter).
* LIMITS of the router-door trace: the router doors are traced at the ``litellm.acompletion`` stub boundary (the
  stub dials its model's host through the patched socket). It proves the guard layers never call ``acompletion`` with
  a cloud model for a secret prompt, not that litellm's own httpx transport would reach the patched connect. Only the
  SDK door and the hook sinks below run a real urllib transport. The ``net`` fixture also replaces
  ``_build_and_filter_chain`` and ``get_model_chain`` (chain-builder filtering is bypassed on purpose; the positive
  control shows the block is ``scan_secrets`` and the marker).
* ``judge_cascade.judge_inline`` sends prompt + response to a Haiku judge with no private check. It is not reachable
  from any entry point (``test_judge_cascade_is_not_reachable``); ``test_..._would_leak_if_wired`` is a strict xfail.
* LEAK sinks (strict xfail, ``test_leak_*``): direct_executor call_gemini / call_openai, auto-route API classifiers
  (UN-04d). llm_codex, llm_gemini and the agent-route doors are fixed by UN-04b (#772): class `guarded`, plain tests. The playwright-compress Gemini
  leak is fixed (UN-04c, #775): class `guarded`, and its test is a plain passing test.

No real network, no real model, tmp homes (the lc12 fixtures). Secrets are generated at run time.
"""
# ruff: noqa: F811  (fixtures are imported and used as arguments)
from __future__ import annotations

import ast
import asyncio
import collections
import ipaddress
import re
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



# ------------------------------------------------------------------------------------------------ the sink inventory
# The call-name scan above finds the router's own entry points. A model call can also leave through a sink that
# never names them: a CLI runner, a urllib / httpx / aiohttp / requests request, a subprocess to a model CLI, an
# anthropic / openai SDK client, a bare ``from litellm import completion``. This second scan keys every such call
# ``(file, function, callee)``. A hit needs a SINK_FUNCS row or, failing that, its file in SINK_FILES (the count of
# the file's remaining hits is pinned, so a new sink in an audited file fails too). Classes:
#   LEAK        a secret prompt reaches the sink; a strict xfail below traces it (fix PR: UN-04b)
#   probe       audited: carries no prompt text (reason on the row)
#   local       prompt text to the configured Ollama base (loopback unless the user configured otherwise)
#   router door / proxy / primitive / fixed prompt / opt-in: see the row's reason
SINK_NAMES = {"run_codex", "run_gemini_cli", "run_claude", "call_gemini", "call_openai", "execute_chain",
              "execute_agent", "urlopen", "open_url", "create_subprocess_exec", "create_subprocess_shell",
              "safe_subprocess_exec", "build_request"}
SINK_HTTP_VERBS = {"post", "get", "put", "request", "stream", "send"}
SINK_HTTP_MODULES = ("httpx", "requests", "aiohttp")
SINK_SDK = {"Anthropic", "AsyncAnthropic", "OpenAI", "AsyncOpenAI"}
MODEL_CLI = re.compile(r"codex|gemini|claude|judge_cmd", re.I)  # subprocess.* counts only when it names a model CLI

SINK_FUNCS: dict[tuple[str, str, str], tuple[int, str, str]] = {
    ('model_evaluator.py', 'eval_codex_model', 'run_codex'): (1, 'fixed prompt', 'BenchmarkSuite prompt, no user text'),
    ('model_evaluator.py', 'eval_ollama_model', 'open_url'): (1, 'fixed prompt', 'BenchmarkSuite prompt to local Ollama'),
    ('tools/codex.py', 'llm_codex', 'run_codex'): (1, 'guarded', 'secret door present (#772 UN-04b): local_privacy.guard_cloud_cli before run_codex'),
    ('tools/gemini_cli.py', 'llm_gemini', 'run_gemini_cli'): (1, 'guarded', 'secret door present (#772 UN-04b): local_privacy.guard_cloud_cli before run_gemini_cli'),
    ('hooks/agent-route.py', '_try_direct_subagent', 'execute_agent'): (1, 'guarded', 'secret door present (#772 UN-04b): main() _secret_door runs before the direct, NS3 and CLI paths'),
    ('hooks/agent-route.py', '_try_direct_subagent', 'execute_chain'): (1, 'guarded', 'secret door present (#772 UN-04b): main() _secret_door (the executor itself is still unguarded, see call_gemini / call_openai)'),
    ('hooks/agent-route.py', '_try_cli_delegation', 'run_gemini_cli'): (1, 'guarded', 'secret door present (#772 UN-04b): main() _secret_door'),
    ('hooks/agent-route.py', '_run_codex_agent_inner', 'run_codex'): (1, 'guarded', 'secret door present (#772 UN-04b): main() _secret_door'),
    ('hooks/direct_executor.py', 'call_gemini', 'urlopen'): (1, 'LEAK', 'urllib POST of the prompt to Google; scrub_text redacts the secret value, the rest of the prompt still leaves'),
    ('hooks/direct_executor.py', 'call_openai', 'urlopen'): (1, 'LEAK', 'urllib POST of the prompt to OpenAI; scrub_text redacts the secret value, the rest of the prompt still leaves'),
    ('hooks/direct_executor.py', '<module>', 'call_gemini'): (1, 'LEAK', '_PROVIDER_CALLS entry for call_gemini'),
    ('hooks/direct_executor.py', '<module>', 'call_openai'): (1, 'LEAK', '_PROVIDER_CALLS entry for call_openai'),
    ('hooks/direct_executor.py', '_ollama_open', 'open_url'): (1, 'local', 'loopback Ollama via open_url'),
    ('hooks/auto-route.py', 'classify_with_gemini', 'urlopen'): (1, 'LEAK', 'first 500 chars of the prompt to Gemini; opt-in LLM_ROUTER_CLASSIFY_LOCAL_ONLY=0'),
    ('hooks/auto-route.py', 'classify_with_openai', 'urlopen'): (1, 'LEAK', 'first 500 chars of the prompt to OpenAI; opt-in LLM_ROUTER_CLASSIFY_LOCAL_ONLY=0'),
    ('hooks/auto-route.py', '_ollama_open', 'urlopen'): (1, 'local', 'loopback Ollama'),
    ('hooks/auto-route.py', '_ollama_open', 'open_url'): (1, 'local', 'loopback Ollama'),
    ('hooks/auto-route.py', '_fetch_usage_inline', 'urlopen'): (1, 'probe', 'Anthropic usage endpoint, no prompt text'),
    ('hooks/auto-route.py', 'main', 'execute_agent'): (2, 'local', 'draft chain filtered to ollama/codex/gemini_cli (_free_tier_draft_chain, auto-route.py:3834); direct_executor executes ollama/gemini/openai only, so the draft runs on Ollama'),
    ('hooks/auto-route.py', 'main', 'execute_chain'): (1, 'local', 'same draft chain filter'),
    ('hooks/playwright-compress.py', '_try_gemini', 'urlopen'): (1, 'guarded', 'secret door present (UN-04c, #775): _cloud_blocked -> local_privacy.scan_secrets, fails closed, no cloud: override'),
    ('hooks/playwright-compress.py', '_ollama_open', 'urlopen'): (1, 'local', 'Ollama base; a remote OLLAMA_HOST is refused for a snapshot holding a secret by _cloud_blocked in _try_ollama (#775)'),
    ('hooks/playwright-compress.py', '_ollama_open', 'open_url'): (1, 'local', 'loopback Ollama'),
    ('resolver/calibrate.py', 'CloudCompleter._cli', 'run_codex'): (1, 'fixed prompt', 'calibration probes'),
}
SINK_FILES: dict[str, tuple[int, str, str]] = {
    'agentic/react.py': (1, 'local', "prompt text to the configured Ollama base (open_url / OLLAMA_BASE_URL); a non-loopback base is the user's config and is refused on the secret door"),
    'agentic_registry.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'alerts.py': (1, 'probe', 'outbound text is a scrubbed alert / savings digest / team report built from counters, not prompt text'),
    'benchmark_fetcher.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'claude_agent.py': (1, 'primitive', 'the CLI runner itself; every caller has its own row'),
    'codex_agent.py': (1, 'primitive', 'the CLI runner itself; every caller has its own row'),
    'commands/doctor.py': (3, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'commands/pi.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'commands/update.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'commands/verify.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'config.py': (2, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'context_summary.py': (2, 'local', "prompt text to the configured Ollama base (open_url / OLLAMA_BASE_URL); a non-loopback base is the user's config and is refused on the secret door"),
    'control_plane/client.py': (3, 'opt-in', 'opt-in; CPClient has no callers in src, heartbeat payload is caller-supplied'),
    'cost.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'digest.py': (1, 'probe', 'outbound text is a scrubbed alert / savings digest / team report built from counters, not prompt text'),
    'discover.py': (3, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'discovery_rescan.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'frameworks/openai_agents.py': (1, 'router door', 'an adapter whose client points at the llm-router gateway (a router door)'),
    'gemini_cli_agent.py': (1, 'primitive', 'the CLI runner itself; every caller has its own row'),
    'gemini_cli_quota.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'hook_client.py': (2, 'local', "prompt text to the configured Ollama base (open_url / OLLAMA_BASE_URL); a non-loopback base is the user's config and is refused on the secret door"),
    'hooks/agent_loop.py': (1, 'local', "prompt text to the configured Ollama base (open_url / OLLAMA_BASE_URL); a non-loopback base is the user's config and is refused on the secret door"),
    'hooks/session-end.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'hooks/session-start.py': (2, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'hooks/tool_intercept.py': (1, 'local', "prompt text to the configured Ollama base (open_url / OLLAMA_BASE_URL); a non-loopback base is the user's config and is refused on the secret door"),
    'hooks/usage-refresh.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'integrations/helicone.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'library/sealer.py': (1, 'local', "prompt text to the configured Ollama base (open_url / OLLAMA_BASE_URL); a non-loopback base is the user's config and is refused on the secret door"),
    'local_agent/compact.py': (1, 'local', "prompt text to the configured Ollama base (open_url / OLLAMA_BASE_URL); a non-loopback base is the user's config and is refused on the secret door"),
    'local_agent/proxy_step.py': (2, 'local', "prompt text to the configured Ollama base (open_url / OLLAMA_BASE_URL); a non-loopback base is the user's config and is refused on the secret door"),
    'local_check/judge.py': (2, 'opt-in', 'explicit opt-in feature; text leaves by design when enabled, not traced'),
    'local_classifier.py': (2, 'local', "prompt text to the configured Ollama base (open_url / OLLAMA_BASE_URL); a non-loopback base is the user's config and is refused on the secret door"),
    'local_context_guard.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'local_platforms.py': (2, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'local_privacy.py': (2, 'local', "prompt text to the configured Ollama base (open_url / OLLAMA_BASE_URL); a non-loopback base is the user's config and is refused on the secret door"),
    'media.py': (16, 'router door', 'reached only through route_and_call (_call_media / dispatch after the chain cut, or the SDK secret door)'),
    'model_catalog.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'model_discovery.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'model_pull.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'ollama_watchdog.py': (3, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'proxy/backends.py': (3, 'local', "prompt text to the configured Ollama base (open_url / OLLAMA_BASE_URL); a non-loopback base is the user's config and is refused on the secret door"),
    'proxy/failopen_shim.py': (3, 'proxy', "proxy class, owner-gated, log-only by D-28: replay_http replays the client request to Anthropic; claude_judge sends request context + reply to the claude CLI"),
    'proxy/server.py': (7, 'proxy', "proxy class, owner-gated, log-only by D-28: replay_http replays the client request to Anthropic; claude_judge sends request context + reply to the claude CLI"),
    'quickstart.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'resolver/calibrate.py': (2, 'fixed prompt', 'benchmark / calibration probes, no user text'),
    'resolver/inventory.py': (2, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'resource_map.py': (2, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'router.py': (3, 'router door', 'reached only through route_and_call (_call_media / dispatch after the chain cut, or the SDK secret door)'),
    'runtime_offer.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'safe_subprocess.py': (1, 'primitive', 'the CLI runner itself; every caller has its own row'),
    'sdk.py': (2, 'router door', 'reached only through route_and_call (_call_media / dispatch after the chain cut, or the SDK secret door)'),
    'semantic_cache.py': (1, 'local', "prompt text to the configured Ollama base (open_url / OLLAMA_BASE_URL); a non-loopback base is the user's config and is refused on the secret door"),
    'semantic_classify.py': (1, 'local', "prompt text to the configured Ollama base (open_url / OLLAMA_BASE_URL); a non-loopback base is the user's config and is refused on the secret door"),
    'service_manager.py': (2, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'session_broker.py': (1, 'router door', 'codex_adapter jobs are reached through route_and_call, so the router door guards them'),
    'shadow_frontier.py': (3, 'proxy', "proxy class, owner-gated, log-only by D-28: replay_http replays the client request to Anthropic; claude_judge sends request context + reply to the claude CLI"),
    'stats.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'statusline_segments.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'team.py': (1, 'probe', 'outbound text is a scrubbed alert / savings digest / team report built from counters, not prompt text'),
    'toolkit/adapters/ollama.py': (1, 'local', "prompt text to the configured Ollama base (open_url / OLLAMA_BASE_URL); a non-loopback base is the user's config and is refused on the secret door"),
    'tools/admin.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'tools/agoragentic.py': (8, 'opt-in', 'opt-in (SEC-003 env); agoragentic_execute (:76-100) posts task + input to agoragentic.com with NO secret check, not traced'),
    'tools/subscription.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'vision_registry.py': (3, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
    'warm.py': (1, 'probe', 'health, discovery, usage, pricing or update request; no prompt text'),
}


#: Families the sink scan cannot see (no urlopen / httpx name), audited by reading. A new one needs a line here.
UNSCANNED_FAMILIES = {
    "tracing.py:53-85 OTLP span exporter": "opt-in via OTEL_EXPORTER_OTLP_ENDPOINT; span attributes are metadata, but "
                                           "record_span_exception(str(exc)) can carry provider error text that echoes request content",
}


def _scan_sinks() -> collections.Counter:
    found: collections.Counter = collections.Counter()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = path.relative_to(SRC).as_posix()

        def visit(node, stack):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                stack = stack + [node.name]
            if isinstance(node, ast.Call):
                f = node.func
                dotted = ast.unparse(f)
                name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
                if name and name.lstrip("_") in SINK_NAMES:
                    name = name.lstrip("_")  # `from ... import execute_chain as _execute_chain`
                hit = None
                if name in SINK_NAMES or name in SINK_SDK:
                    hit = name
                elif name in ("Client", "AsyncClient", "ClientSession") and any(m in dotted for m in SINK_HTTP_MODULES):
                    hit = name
                elif (name in SINK_HTTP_VERBS and isinstance(f, ast.Attribute)
                      and (any(dotted.startswith(m + ".") for m in SINK_HTTP_MODULES)
                           or re.search(r"(^|\.)(client|http|_client)$", ast.unparse(f.value)))):
                    hit = "http." + name
                elif name == "create" and re.search(r"(messages|completions|responses)\.create$", dotted):
                    hit = "sdk.create"
                elif (dotted.split(".")[0] == "subprocess" and name in {"run", "Popen", "check_output", "check_call", "call"}
                      and MODEL_CLI.search(ast.dump(node))):
                    hit = "subprocess." + name
                elif name == "completion" and isinstance(f, ast.Name):
                    hit = "completion"  # a bare `from litellm import completion`
                if hit:
                    found[(rel, ".".join(stack) or "<module>", hit)] += 1
            for child in ast.iter_child_nodes(node):
                visit(child, stack)
        visit(tree, [])
    return found


def classify_sinks(found: collections.Counter, funcs=None, files=None) -> list[str]:
    funcs = SINK_FUNCS if funcs is None else funcs
    files = SINK_FILES if files is None else files
    problems, rest = [], collections.Counter()
    for key, n in sorted(found.items()):
        if key in funcs:
            if funcs[key][0] != n:
                problems.append(f"{key[0]}::{key[1]} {key[2]}: {n} sites, row says {funcs[key][0]}")
        else:
            rest[key[0]] += n
    for key in sorted(funcs):
        if key not in found:
            problems.append(f"STALE sink row {key[0]}::{key[1]} {key[2]}")
    for file, n in sorted(rest.items()):
        if file not in files:
            problems.append(f"UNCLASSIFIED sink in {file} ({n} call(s)): "
                            + ", ".join(f"{k[1]}:{k[2]}" for k in sorted(found) if k[0] == file and k not in funcs))
        elif files[file][0] != n:
            problems.append(f"{file}: {n} unclassified sinks, row says {files[file][0]}")
    for file in sorted(files):
        if file not in rest:
            problems.append(f"STALE sink file row {file}")
    return problems


def test_every_off_machine_sink_is_classified():
    found = _scan_sinks()
    assert sum(found.values()) >= 100, "the sink scan found too few sites: an empty scan passes everything"
    assert classify_sinks(found) == []
    assert {v[1] for v in SINK_FUNCS.values()} | {v[1] for v in SINK_FILES.values()} == {
        "probe", "local", "primitive", "router door", "proxy", "fixed prompt", "opt-in", "LEAK", "guarded"}


def test_unscanned_families_are_named():
    tracing = (SRC / "tracing.py").read_text()
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in tracing and "OTLPSpanExporter" in tracing and UNSCANNED_FAMILIES


def test_sink_summary_counts():
    found = _scan_sinks()
    by_class: collections.Counter = collections.Counter()
    for n, cls, _why in list(SINK_FUNCS.values()) + list(SINK_FILES.values()):
        by_class[cls] += n
    assert sum(by_class.values()) == sum(found.values())
    leaks = sorted({f"{k[0]}::{k[1]}" for k in SINK_FUNCS if SINK_FUNCS[k][1] == "LEAK"})
    assert by_class["LEAK"] == 6 and len(leaks) == 5, (by_class["LEAK"], leaks)


def test_negative_control_a_new_urlopen_call_site_fails(tmp_path, monkeypatch):
    """A temp copy of the tree with a new urllib sink in a new file, and one in an already audited file."""
    import shutil
    copy = tmp_path / "llm_router"
    shutil.copytree(SRC, copy, ignore=shutil.ignore_patterns("__pycache__", "data", "*.json", "*.gz"))
    (copy / "new_sink.py").write_text(
        "import urllib.request\n\ndef leak(prompt):\n"
        "    req = urllib.request.Request('https://example.invalid', data=prompt.encode())\n"
        "    return urllib.request.urlopen(req)\n")
    with open(copy / "stats.py", "a") as fh:
        fh.write("\n\ndef extra(u):\n    import urllib.request\n    return urllib.request.urlopen(u)\n")
    monkeypatch.setitem(globals(), "SRC", copy)
    assert classify_sinks(_scan_sinks()) == [
        "UNCLASSIFIED sink in new_sink.py (1 call(s)): leak:urlopen",
        "stats.py: 2 unclassified sinks, row says 1"]
    with pytest.raises(AssertionError):
        assert classify_sinks(_scan_sinks()) == []


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
    DOORS[door](net, _secret())
    t = net.trace
    assert t.off_machine == [], f"{door}: secret prompt reached {t.off_machine}"
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
    net.monkeypatch.setattr(local_privacy, "private_dispatch_active", lambda: False)
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


# ------------------------------------------------------------------------------------------------ sink leaks (UN-04b)
MARK = "w11-marker-7f3a"  # non-secret words that ride along with the secret, to see the prompt arrive


# One runner per LEAK sink. A runner drives the sink with a given prompt and the fixture returns everything that
# reached it: the argv and stdin of a model-CLI subprocess, or the url and body of a urlopen request (recorded before
# any DNS, nothing is sent). Per sink two tests:
#   test_reach_*  (plain test)  a NON-secret prompt reaches the sink, so the harness can see it: the positive control
#   test_leak_*   (strict xfail) the ONE assertion "the secret is not in what reached the sink". It fails today and
#                 XPASSes once the sink is guarded (refused before the sink), which is the signal UN-04b gets.
# Fixed sinks (UN-04b #772, UN-04c #775) are plain tests named test_guarded_*; the rest stay strict xfails.
class _FakeProc:
    returncode = 0

    def __init__(self, sink):
        self.sink = sink

    async def communicate(self, input=None):
        if input:
            self.sink.append(input.decode() if isinstance(input, bytes) else str(input))
        return b"", b""

    async def wait(self):
        return 0

    def kill(self):
        pass


@pytest.fixture
def cli_trace(monkeypatch, tmp_path):
    """Every model-CLI subprocess is recorded (argv and stdin) and never started."""
    sent: list[str] = []

    async def create_subprocess_exec(*argv, **kw):
        sent.append(" ".join(str(a) for a in argv))
        return _FakeProc(sent)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    import llm_router.codex_agent as ca
    import llm_router.gemini_cli_agent as ga
    monkeypatch.setattr(ca, "find_codex_binary", lambda: "/nonexistent/codex")
    monkeypatch.setattr(ga, "find_gemini_binary", lambda: "/nonexistent/gemini")
    monkeypatch.setattr(ga, "_GEMINI_BINARY_PATH", "/nonexistent/gemini")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    return sent


@pytest.fixture
def hook_net(monkeypatch, tmp_path):
    """Every urllib request is recorded (url + body) before any DNS and never sent."""
    import urllib.request
    sent: list[str] = []

    def urlopen(req, *a, **k):
        data = getattr(req, "data", None)
        sent.append(f"{getattr(req, 'full_url', req)} {data.decode(errors='replace') if data else ''}")
        raise OSError("W1-1 trace: not sent")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "k-" + "x" * 20)
    monkeypatch.setenv("OPENAI_API_KEY", "k-" + "y" * 20)
    return sent


def _load_hook(name):
    import importlib.util
    path = SRC / "hooks" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_un04", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _quiet(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception:  # noqa: BLE001 - the sink was fed; what it does next is not the question
        return None


def _run_llm_codex(env, text, mp):
    from llm_router.tools import codex
    mp.setattr(codex, "is_codex_available", lambda: True)
    _swallow(codex.llm_codex(prompt=f"explain {text}"))


def _run_llm_gemini(env, text, mp):
    from llm_router.tools import gemini_cli
    mp.setattr(gemini_cli, "is_gemini_cli_available", lambda: True)
    _swallow(gemini_cli.llm_gemini(prompt=f"explain {text}"))


def _run_direct(provider):
    def run(env, text, mp):
        from llm_router.hooks import direct_executor as de
        model = "gemini-2.5-flash" if provider == "gemini" else "gpt-4o-mini"
        _quiet(de.execute_chain, f"explain {text}", [de.ModelSpec(provider, model)], "query", timeout=2)
    return run


def _run_agent_route_direct(env, text, mp):
    from llm_router.hooks import chain_builder
    from llm_router.hooks import direct_executor as de
    mod = _load_hook("agent-route")
    mp.setenv("LLM_ROUTER_SUBAGENT_DIRECT", "on")  # off by default (owner decision 2026-10-02)
    mp.setattr(chain_builder, "build_chain", lambda *a, **k: [de.ModelSpec("gemini", "gemini-2.5-flash")])
    mp.setattr(chain_builder, "needs_claude_tools", lambda *a, **k: False)
    mp.setattr(chain_builder, "get_current_pressure", lambda: ("green", 5))
    _quiet(mod._try_direct_subagent, f"explain {text}", "query", "simple", "s1")


def _run_agent_route_cli(cli):
    def run(env, text, mp):
        import llm_router.codex_agent as ca
        import llm_router.gemini_cli_agent as ga
        from llm_router.hooks import chain_builder
        mod = _load_hook("agent-route")
        mp.setattr(chain_builder, "needs_claude_tools", lambda *a, **k: True)
        mp.setattr(ca, "is_codex_available", lambda: cli == "codex")
        mp.setattr(ga, "is_gemini_cli_available", lambda: cli == "gemini")
        _quiet(mod._try_cli_delegation, f"fix {text}", "code", "complex", "s1")
    return run


def _run_auto_route(fn):
    def run(env, text, mp):
        mp.setenv("LLM_ROUTER_CLASSIFY_LOCAL_ONLY", "0")  # the API classifier layer is opt-in
        _quiet(getattr(_load_hook("auto-route"), fn), f"explain {text}")
    return run


def _run_playwright(env, text, mp):
    _quiet(_load_hook("playwright-compress")._try_gemini, f"page text with a token {text}")


#: id -> (runner, fixture name holding what reached the sink, where the leak is)
LEAK_RUNNERS = {
    "llm_codex": (_run_llm_codex, "cli_trace", "tools/codex.py:34"),
    "llm_gemini": (_run_llm_gemini, "cli_trace", "tools/gemini_cli.py:33"),
    "direct_gemini": (_run_direct("gemini"), "hook_net", "hooks/direct_executor.py:435 call_gemini"),
    "direct_openai": (_run_direct("openai"), "hook_net", "hooks/direct_executor.py:477 call_openai"),
    "agent_route_direct": (_run_agent_route_direct, "hook_net", "hooks/agent-route.py:1360-1362"),
    "agent_route_codex": (_run_agent_route_cli("codex"), "cli_trace", "hooks/agent-route.py:1678"),
    "agent_route_gemini_cli": (_run_agent_route_cli("gemini"), "cli_trace", "hooks/agent-route.py:1882"),
    "auto_route_gemini": (_run_auto_route("classify_with_gemini"), "hook_net", "hooks/auto-route.py:2882"),
    "auto_route_openai": (_run_auto_route("classify_with_openai"), "hook_net", "hooks/auto-route.py:2852"),
    "playwright_gemini": (_run_playwright, "hook_net", "hooks/playwright-compress.py:193"),
}


@pytest.mark.parametrize("sink", sorted(LEAK_RUNNERS))
def test_reach_a_plain_prompt_reaches_the_sink(sink, request):
    """Positive control, no secret: the harness sees what the sink is given (so an empty result below means a guard)."""
    run, fixture, _where = LEAK_RUNNERS[sink]
    sunk = request.getfixturevalue(fixture)
    run(None, f"{MARK} hello world", request.getfixturevalue("monkeypatch"))
    assert any(MARK in x for x in sunk), f"{sink}: the plain prompt never reached the sink"


def _leak(sink, request):
    run, fixture, _where = LEAK_RUNNERS[sink]
    sunk = request.getfixturevalue(fixture)
    secret = _secret()
    run(None, f"{MARK} {secret}", request.getfixturevalue("monkeypatch"))
    # The single assertion: no part of a secret-bearing prompt reached the sink (NFR-PRIV "secret -> local only").
    # Some sinks redact the secret value (direct_executor scrub_text) and still send the rest of the prompt.
    assert not any(MARK in x for x in sunk), (
        f"{sink} ({_where}): a secret-bearing prompt reached the sink; secret value itself present: "
        f"{any(secret in x for x in sunk)}")


_LEAK = pytest.mark.xfail(strict=True, reason="UN-04 leak: a secret prompt reaches a cloud sink; fix in UN-04b")


def test_guarded_llm_codex_refuses_a_secret(request):
    """tools/codex.py:34 llm_codex -> run_codex (codex CLI -> OpenAI)."""
    _leak("llm_codex", request)


def test_guarded_llm_gemini_refuses_a_secret(request):
    """tools/gemini_cli.py:33 llm_gemini -> run_gemini_cli (gemini CLI -> Google)."""
    _leak("llm_gemini", request)


@_LEAK
def test_leak_direct_executor_call_gemini(request):
    """hooks/direct_executor.py:435 call_gemini (reached by agent-route.py:1360-1362; the SDK door has its own guard)."""
    _leak("direct_gemini", request)


@_LEAK
def test_leak_direct_executor_call_openai(request):
    """hooks/direct_executor.py:477 call_openai."""
    _leak("direct_openai", request)


def test_guarded_agent_route_secret_door_holds_a_secret_and_only_a_secret():
    """The agent-route sinks (_try_direct_subagent, _try_cli_delegation, _run_codex_agent_inner) are guarded by
    main()'s ``_secret_door`` (UN-04b, #772), not inside each helper, so a helper called directly still reaches its
    sink (``test_reach_*``). The end-to-end main() traces are tests/test_un04b_secret_door_cli_and_agent_route.py;
    this pins the door's verdict that the rows rely on."""
    door = _load_hook("agent-route")._secret_door
    assert door(f"explain {_secret()}")[1] is True
    assert door("explain a mutex")[1] is False


@_LEAK
def test_leak_auto_route_classify_with_gemini(request):
    """hooks/auto-route.py:2882: first 500 chars of every prompt, opt-in (LLM_ROUTER_CLASSIFY_LOCAL_ONLY=0)."""
    _leak("auto_route_gemini", request)


@_LEAK
def test_leak_auto_route_classify_with_openai(request):
    """hooks/auto-route.py:2852, same opt-in."""
    _leak("auto_route_openai", request)


def test_playwright_compress_gemini_refuses_a_secret_snapshot(request):
    """hooks/playwright-compress.py _try_gemini was a leak; UN-04c (#775) added the secret door (_cloud_blocked)."""
    _leak("playwright_gemini", request)
