"""Health check — verify every component is wired up correctly.

Comprehensive diagnostic tool to check hooks, MCP registration, API keys,
Ollama availability, and host-specific configurations.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import NamedTuple, Optional

from llm_router.terminal_style import Color
from llm_router.tool_surface import route_call, route_tool  # CHZ-SURF-01


# ── Formatting utilities ────────────────────────────────────────────────────

def _bold(text: str) -> str:
    """Bold text."""
    return f"\033[1m{text}\033[0m"


def _green(text: str) -> str:
    """Green text."""
    return Color.CONFIDENCE_GREEN(text)


def _red(text: str) -> str:
    """Red text."""
    return Color.WARNING_RED(text)


def _yellow(text: str) -> str:
    """Yellow text."""
    return f"\033[33m{text}\033[0m"


def _dim(text: str) -> str:
    """Dim text."""
    return f"\033[2m{text}\033[0m"


def _ok(text: str) -> str:
    """Formatted success message."""
    return f"  {_green('✓')} {text}"


def _fail(text: str, fix: Optional[str] = None) -> str:
    """Formatted failure message."""
    msg = f"  {_red('✗')} {text}"
    if fix:
        msg += f" (fix: {_dim(fix)})"
    return msg


def _warn(text: str) -> str:
    """Formatted warning message."""
    return f"  {_yellow('⚠')} {text}"


# ── Hook utilities ──────────────────────────────────────────────────────────

def _hook_version_num(path: Path) -> int:
    """Read the version number embedded in a hook file header."""
    _re = re.compile(r"#\s*llm_router-hook-version:\s*(\d+)")
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[:5]:
            m = _re.search(line)
            if m:
                return int(m.group(1))
    except OSError:
        pass
    return 0


# ── Doctor implementation ───────────────────────────────────────────────────

def _extract_toml_string(text: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
    return m.group(1) if m else None


def _extract_toml_section_string(text: str, section: str, key: str) -> str | None:
    start = re.search(rf"^\[{re.escape(section)}\]\s*$", text, re.MULTILINE)
    if not start:
        return None
    next_section = re.search(r"^\[", text[start.end():], re.MULTILINE)
    section_text = (
        text[start.end(): start.end() + next_section.start()]
        if next_section
        else text[start.end():]
    )
    return _extract_toml_string(section_text, key)


def _codex_checks() -> tuple[list[str], list[str]]:
    """(report lines, issues) for the Codex CLI wiring.

    Checks what Codex actually reads: `[mcp_servers.llm_router]` in
    config.toml with a runnable command, `codex mcp list` agreeing, and every
    llm_router hook in hooks.json carrying a matching trust record (Codex
    skips an untrusted hook silently, so a hook without one is a failure,
    not a warning). Legacy config.yaml / config.json entries are reported as
    ignored. The opt-in gateway provider table is informational only.
    """
    from llm_router import codex_host

    lines: list[str] = []
    issues: list[str] = []
    codex_dir = Path.home() / ".codex"
    config_toml = codex_dir / "config.toml"
    hooks_json = codex_dir / "hooks.json"
    fix = "llm-router install"

    text = ""
    if config_toml.exists():
        try:
            text = config_toml.read_text()
        except OSError as e:
            lines.append(_fail(f"could not read {config_toml}: {e}"))
            issues.append("Codex config.toml unreadable")
            return lines, issues
    else:
        lines.append(_fail(f"config.toml not found at {config_toml}", fix=fix))
        issues.append("Codex config.toml missing")
        return lines, issues

    # 1. MCP server
    entry = codex_host.read_mcp_server(text)
    if entry is None:
        lines.append(_fail("[mcp_servers.llm_router] missing from config.toml — Codex cannot see llm-router", fix=fix))
        issues.append("Codex MCP server not registered")
    else:
        problems = _mcp_command_problems(entry, "Codex")
        if problems:
            for p in problems:
                lines.append(_fail(p, fix=fix))
            issues.append("Codex MCP command cannot start")
        else:
            lines.append(_ok(f"MCP server registered in config.toml ({entry.get('command')})"))
            tools = entry.get("tools") if isinstance(entry.get("tools"), dict) else {}
            door = (tools.get("llm") or {}).get("approval_mode")
            if door != "approve" and entry.get("default_tools_approval_mode") != "approve":
                lines.append(_warn(
                    "the `llm` tool is not auto-approved — `codex exec` (approval policy never) "
                    "fails every routed call with 'requires approval'", ))
                lines.append(_dim(f"    fix: {fix}"))

    # 2. Codex's own view
    binary = shutil.which("codex")
    if binary and entry is not None:
        try:
            proc = subprocess.run(
                [binary, "mcp", "list"], capture_output=True, text=True, timeout=5, check=False,
                env={**os.environ, "CODEX_HOME": str(codex_dir)},
            )
            listed = "llm_router" in (proc.stdout or "")
        except (OSError, subprocess.SubprocessError):
            listed = None
        if listed is True:
            lines.append(_ok("`codex mcp list` shows llm_router"))
        elif listed is False:
            lines.append(_fail("`codex mcp list` does not show llm_router", fix=fix))
            issues.append("codex mcp list missing llm_router")
        else:
            lines.append(_warn("`codex mcp list` did not answer in 5 s — skipped"))
    elif not binary:
        lines.append(_dim("    codex binary not on PATH — skipped `codex mcp list`"))

    # 3. Hooks + trust
    ours = str(Path.home() / ".llm-router" / "hooks")
    if hooks_json.exists():
        try:
            doc = json.loads(hooks_json.read_text())
        except (OSError, ValueError):
            doc = None
        if doc is None:
            lines.append(_warn(f"{hooks_json} is not valid JSON — hooks disabled"))
        else:
            wanted = {}
            for event, groups in (doc.get("hooks") or {}).items():
                if event not in codex_host.EVENT_LABELS or not isinstance(groups, list):
                    continue
                for gi, g in enumerate(groups):
                    for hi, hnd in enumerate((g.get("hooks") or []) if isinstance(g, dict) else []):
                        if isinstance(hnd, dict) and ours in str(hnd.get("command", "")):
                            key = codex_host.hook_state_key(hooks_json, event, gi, hi)
                            wanted[key] = (event, codex_host.hook_trust_hash(event, hnd, g.get("matcher")))
            have = codex_host.read_trust_records(text)
            if not wanted:
                lines.append(_warn("no llm_router hook in hooks.json — Codex gets pull routing only (no ⚡ ROUTE hint)"))
            for key, (event, digest) in wanted.items():
                if have.get(key) == digest:
                    lines.append(_ok(f"{event} hook trusted"))
                elif key in have:
                    lines.append(_fail(f"{event} hook trust record is stale — Codex skips it silently", fix=fix))
                    issues.append(f"Codex {event} hook untrusted (modified)")
                else:
                    lines.append(_fail(f"{event} hook has no trust record — Codex skips it silently", fix=fix))
                    issues.append(f"Codex {event} hook untrusted")
    else:
        lines.append(_warn("hooks.json not found — Codex gets pull routing only (no ⚡ ROUTE hint)"))

    # 4. AGENTS.md
    agents = codex_dir / "AGENTS.md"
    try:
        has_block = agents.exists() and codex_host.AGENTS_BLOCK_START in agents.read_text()
    except OSError:
        has_block = False
    lines.append(_ok("routing rules block in AGENTS.md") if has_block
                 else _warn("no routing rules block in AGENTS.md", ))

    # 5. Gateway (opt-in) and the one setting that breaks Codex
    if _extract_toml_string(text, "model_provider") == "llm_router":
        lines.append(_fail(
            "Codex model_provider is force-set to 'llm_router' — this breaks Codex CLI "
            "(gateway wire-format mismatch)", fix=fix,
        ))
        issues.append("Codex model_provider forced to llm_router (breaks Codex)")
    if "[model_providers.llm_router]" in text:
        lines.append(_dim("    opt-in gateway provider registered (use -c model_provider=llm_router per call)"))

    # 6. Legacy leftovers
    for legacy in ("config.yaml", "config.json", "rules/llm_router.md", "instructions.md"):
        p = codex_dir / legacy
        try:
            if p.exists() and "llm_router" in p.read_text(errors="ignore"):
                lines.append(_warn(f"{legacy} mentions llm_router but Codex never reads it — re-run install to clean up"))
        except OSError:
            pass

    return lines, issues


def _codex_report(issues: list[str]) -> list[str]:
    lines, found = _codex_checks()
    issues.extend(found)
    return lines


def _run_doctor_host(host: str) -> None:
    """Run host-specific installation checks."""
    valid_hosts = {"claude", "vscode", "cursor", "codex", "all"}
    if host not in valid_hosts:
        print(f"  Unknown host: {host}. Valid options: {', '.join(sorted(valid_hosts))}")
        return

    hosts_to_check = ["claude", "vscode", "cursor", "codex"] if host == "all" else [host]

    for h in hosts_to_check:
        print(f"\n{_bold(f'  Host: {h}')}")
        issues: list[str] = []

        if h == "claude":
            # Check hooks
            from llm_router.install_hooks import _HOOKS_DST, _HOOK_DEFS

            for _, dst_name, event, _ in _HOOK_DEFS:
                dst = _HOOKS_DST / dst_name
                if dst.exists():
                    print(_ok(f"{dst_name}  ({event})"))
                else:
                    print(
                        _fail(
                            f"{dst_name}  — not installed",
                            fix="llm-router install",
                        )
                    )
                    issues.append(f"Hook {dst_name} missing")

            # Check uvx
            if shutil.which("uvx"):
                print(_ok("uvx found in PATH"))
            else:
                print(_warn("uvx not in PATH — install via: pip install uv"))

        elif h == "vscode":
            if sys.platform == "darwin":
                mcp_json = (
                    Path.home()
                    / "Library"
                    / "Application Support"
                    / "Code"
                    / "User"
                    / "mcp.json"
                )
            elif sys.platform == "win32":
                mcp_json = (
                    Path(os.getenv("APPDATA", "")) / "Code" / "User" / "mcp.json"
                )
            else:
                mcp_json = (
                    Path.home() / ".config" / "Code" / "User" / "mcp.json"
                )

            if mcp_json.exists():
                try:
                    data = json.loads(mcp_json.read_text())
                    if "llm_router" in data.get("servers", {}):
                        print(_ok(f"llm_router registered in {mcp_json}"))
                    else:
                        print(
                            _fail(
                                f"llm_router not in servers ({mcp_json})",
                                fix="llm-router install --host vscode",
                            )
                        )
                        issues.append("llm_router not registered in VS Code mcp.json")
                except Exception as e:
                    print(_fail(f"could not parse {mcp_json}: {e}"))
            else:
                print(
                    _fail(
                        f"mcp.json not found at {mcp_json}",
                        fix="llm-router install --host vscode",
                    )
                )
                issues.append("VS Code mcp.json missing")

            if shutil.which("uvx"):
                print(_ok("uvx found in PATH"))
            else:
                print(
                    _warn(
                        "uvx not in PATH — required for VS Code MCP server"
                    )
                )

        elif h == "cursor":
            mcp_json = Path.home() / ".cursor" / "mcp.json"
            cursor_rules = Path.home() / ".cursor" / "rules" / "llm_router.md"

            if mcp_json.exists():
                try:
                    data = json.loads(mcp_json.read_text())
                    if "llm_router" in data.get("mcpServers", {}):
                        print(_ok(f"llm_router registered in {mcp_json}"))
                    else:
                        print(
                            _fail(
                                f"llm_router not in mcpServers ({mcp_json})",
                                fix="llm-router install --host cursor",
                            )
                        )
                        issues.append("llm_router not registered in Cursor mcp.json")
                except Exception as e:
                    print(_fail(f"could not parse {mcp_json}: {e}"))
            else:
                print(
                    _fail(
                        f"mcp.json not found at {mcp_json}",
                        fix="llm-router install --host cursor",
                    )
                )
                issues.append("Cursor mcp.json missing")

            if cursor_rules.exists():
                print(_ok(f"routing rules installed ({cursor_rules})"))
            else:
                print(_warn(f"routing rules not found at {cursor_rules}"))

        elif h == "codex":
            for line in _codex_report(issues):
                print(f"  {line}")
        else:
            print(_red(f"  {len(issues)} issue(s) found for {h}"))


def _render_host_explainer() -> str:
    """Always-up-to-date explanation of why the host model (Claude Code,
    Cursor, Codex CLI, ...) runs on a frontier model like Opus 4.7 and
    not on a local one — and what that means for the savings LLM Router can
    deliver. Surfaced via ``llm_router doctor --explain-host`` so the
    answer lives next to the savings posture report.
    """
    # chz-surface-ok: explanatory prose about the cost model, not an instruction
    # to call anything — the names here are illustrative, not a call-to-action.
    return (
        f"\n{_bold('  Why is my host on Opus 4.7 / Sonnet 4.6 and not on a local model?')}\n\n"

        f"  {_bold('Short answer')}\n"
        "    The host (Claude Code, Cursor, Codex CLI) is the agent loop —\n"
        "    it reads tool results, decides the next action, generates code,\n"
        "    and drives the conversation. LLM Router routes the LLM *calls* the\n"
        "    host makes on your behalf (llm_query, llm_code, llm_research)\n"
        "    but it does not replace the host itself. The host model is\n"
        "    whatever Claude Code is configured to use; today that's Opus 4.7\n"
        "    (1M context) by default.\n\n"

        f"  {_bold('Why not just run the host on Ollama?')}\n"
        "    Three reasons in descending order of importance:\n\n"
        "      1. Agent-loop reasoning is the hardest LLM task. The host has\n"
        "         to hold the conversation, plan multi-step solutions, generate\n"
        "         working code, and recover from tool failures. Local models\n"
        "         (qwen3.5, llama-3) drop coherence after 2-3 turns of that\n"
        "         work — great at single-shot answers, not at multi-turn\n"
        "         orchestration.\n\n"
        "      2. Tool-call format conformance. The host must emit tool calls\n"
        "         in very specific JSON every time. Frontier models get this\n"
        "         right >99% of the time; mid-tier local models miss enough\n"
        "         that the agent stalls. Anthropic/OpenAI tune their models\n"
        "         specifically for this; local wrappers compound failure rate.\n\n"
        "      3. Claude Code's UX assumes Opus-class reasoning. Plan mode,\n"
        "         the 1M context window, the way it handles ambiguity — all\n"
        "         designed around Opus capabilities. Swapping the model would\n"
        "         degrade UX in subtle, hard-to-debug ways.\n\n"

        f"  {_bold('What LLM Router CAN save')}\n"
        "      * Cost of LLM calls the host makes (llm_query → Haiku/Flash\n"
        "        instead of Opus). Visible in routing_decisions.\n"
        "      * Tokens the host has to *process* (response_router compresses\n"
        "        explanations in MCP responses before Claude reads them).\n"
        "      * Wasted tool-call cycles (sidecar pre-executes deterministic\n"
        "        prompts like 'show me my routing today').\n"
        "      * Quota burned classifying conversational follow-ups\n"
        "        (continuation bypass + short-followup pattern).\n\n"

        f"  {_bold('What LLM Router CANT save')}\n"
        "      * The host model's own reasoning between tool calls. That's\n"
        "        Opus time, full price, no intercept point.\n"
        "      * Conversation history shipped through Opus on every turn.\n"
        "      * Tool-call decisions the host makes (Read file X, Run Bash Y) —\n"
        "        those decisions ARE the agent loop.\n\n"

        f"  {_bold('Workarounds if you need more headroom')}\n"
        "      1. /model claude-sonnet-4-6 — drops the host to Sonnet for the\n"
        "         rest of the conversation. Sonnet handles tool orchestration\n"
        "         at ~5x lower cost than Opus 4.7. Best for routine work.\n"
        "      2. /clear between unrelated tasks — drops the 1M context so\n"
        "         each new request starts cheap. Best for topic switches.\n"
        "      3. LLM_ROUTER_SIDECAR_PREFETCH=1 — opt into the sidecar so\n"
        "         introspection prompts skip the host entirely.\n"
        "      4. Pair-mode subscriptions (Codex CLI / Gemini CLI) — LLM Router\n"
        "         injects these ahead of paid externals when available, so\n"
        "         routed work runs on your existing subscription quota\n"
        "         instead of API spend.\n"
    )


class _RoutingDecisionState(NamedTuple):
    """What `routing_decisions` actually told us — three states, not two.

    GH#55: doctor collapsed "table unreadable", "table empty but the machine is
    busy" and "machine genuinely idle" into one message, "No routing decisions
    today yet — trigger a few llm_* tool calls and re-run". A reporter who had
    just made four llm() calls was told to make some calls.

    The reason the three diverge at all: four different functions are named
    `log_routing_decision` (cost, routing_hints, model_tracking,
    lineage.decision_logger) and they write to four different destinations.
    Only cost's writes this table, so a session that routed through another
    path leaves it empty while usage/claude_usage fill up — which is exactly
    what the reporter saw, and the inverse of what this machine shows.
    """

    readable: bool
    rows: int
    other_activity: int
    summary: str


def _routing_decision_state(db_path: Path) -> _RoutingDecisionState:
    """Report the routing_decisions table honestly. Never reports 'idle' on error."""
    import sqlite3  # imported locally, matching the rest of this module

    if not Path(db_path).is_file():
        return _RoutingDecisionState(
            False, 0, 0,
            f"usage database not found at {db_path} — nothing has been recorded yet",
        )
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM routing_decisions "
                "WHERE date(timestamp,'localtime')=date('now','localtime') "
                "  AND COALESCE(reason_code,'') != 'sidecar_backfill'"
            ).fetchone()[0]
        except sqlite3.Error as e:
            # NOT 0 rows. An unreadable table is a different fact from an empty
            # one, and reporting them alike is what made GH#55 unreproducible.
            return _RoutingDecisionState(
                False, 0, 0,
                f"could not read routing_decisions ({e}) — this is a storage "
                f"problem, not an absence of activity",
            )
        try:
            other = conn.execute(
                "SELECT COUNT(*) FROM usage "
                "WHERE date(timestamp,'localtime')=date('now','localtime')"
            ).fetchone()[0]
        except sqlite3.Error:
            other = 0
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if rows:
        return _RoutingDecisionState(True, rows, other, f"{rows} routing decisions today")
    if other:
        return _RoutingDecisionState(
            True, 0, other,
            f"routing_decisions is empty today, but usage has {other} row(s) — "
            f"this session recorded through a different writer, so decision-level "
            f"metrics are unavailable even though routing happened",
        )
    return _RoutingDecisionState(True, 0, 0, "no routing activity recorded today")


def _mcp_command_problems(entry: object, label: str) -> list[str]:
    """Validate a registered MCP entry can actually START. Returns problems, or [].

    GH#41: every MCP check in this file asked only whether the KEY "llm_router"
    was present in mcpServers. A pipx install of 13.0.2 had the key, a
    `uv run --directory <site-packages>` command that could never start, and
    `claude mcp list` reporting CONNECTION_CLOSED — while doctor printed 0
    issues. Presence of a registration is not evidence it works; this reads the
    command back and checks it is runnable.
    """
    problems: list[str] = []

    if not isinstance(entry, dict):
        return [f"{label}: mcpServers.llm_router is {type(entry).__name__}, expected an object"]

    command = entry.get("command")
    if not command or not isinstance(command, str):
        return [f"{label}: mcpServers.llm_router has no 'command'"]

    # A bare name must be on PATH; an absolute/relative path must exist and be executable.
    if os.sep in command:
        cmd_path = Path(command).expanduser()
        if not cmd_path.exists():
            problems.append(f"{label}: command does not exist: {command}")
        elif not os.access(cmd_path, os.X_OK):
            problems.append(f"{label}: command is not executable: {command}")
    elif shutil.which(command) is None:
        problems.append(f"{label}: command not found on PATH: {command}")

    # `uv run --directory DIR` is only valid against a real project root. This is
    # the specific shape that shipped broken: DIR was a site-packages path.
    args = entry.get("args") or []
    if isinstance(args, list) and "--directory" in args:
        try:
            project_dir = Path(args[args.index("--directory") + 1])
        except IndexError:
            problems.append(f"{label}: --directory given with no path")
        else:
            if not (project_dir / "pyproject.toml").exists():
                problems.append(
                    f"{label}: 'uv run --directory {project_dir}' has no pyproject.toml — "
                    f"this is a packaged install, not a source checkout, so the server "
                    f"cannot start (CONNECTION_CLOSED)"
                )

    return problems


def _check_savings_posture() -> list[str]:
    """Return rendered status lines for each quota-savings configuration check.

    Each line is one of ``_ok`` / ``_warn`` / ``_fail`` with a short
    actionable suggestion so the user knows exactly what env var or
    setting to flip. We check seven things in order of leverage:

    1. **OpenRouter key** — biggest unlock. Single key gives access to
       deepseek-v4-flash, qwen3-235b, claude-sonnet-4 via OpenRouter,
       which the ``cost_aggressive`` policy is wired for.
    2. **DeepSeek key** — direct access to deepseek-v4-flash /
       deepseek-v4-pro. Optional but unlocks the cheapest non-local
       reasoning tier when OpenRouter isn't set.
    3. **Sidecar pre-execution** — ``LLM_ROUTER_SIDECAR_PREFETCH=1`` lets
       the hook answer introspection prompts without any tool calls.
    4. **Response router** — ``LLM_ROUTER_RESPONSE_ROUTER=on`` (default on)
       compresses explanations in MCP tool responses before Claude reads
       them. Warn if explicitly disabled.
    5. **Enforcement mode** — strict / hard mode actually blocks
       bypasses; smart is the safe default. Off / shadow is a foot-gun.
    6. **Hook hint freshness** — the auto-route hook should be writing
       ``~/.llm-router/last_classification_<session_id>.json`` on every prompt.
       The doctor checks the most-recently-modified shard; a stale file
       (> 1h) means the hook isn't firing in any session.
    7. **Today's simple-share** — if any routing happened today, what
       fraction was classified ``simple``? Pre-fix this was 0%; healthy
       posture is > 30% on a chat-heavy session, > 50% on info-gathering.

    Failures here are advisory — they're rendered but don't append to
    the doctor's ``issues`` list, since "LLM Router works" and "LLM Router is
    optimally configured" are different bars.
    """
    import sqlite3
    import time
    from pathlib import Path

    lines: list[str] = []

    # 1. OpenRouter key — single biggest unlock.
    if os.environ.get("OPENROUTER_API_KEY"):
        lines.append(_ok("OPENROUTER_API_KEY set — full leaderboard pool reachable"))
    elif (Path.home() / ".llm-router" / "openrouter-routerarena.env").exists():
        lines.append(_warn(
            "OPENROUTER_API_KEY stored at ~/.llm-router/openrouter-routerarena.env "
            "but NOT loaded into env. Source the file before benchmark runs."
        ))
    else:
        lines.append(_warn(
            "OPENROUTER_API_KEY not set — deepseek-v4-flash / qwen3-235b / "
            "qwen3-coder-next unreachable. One key unlocks the leaderboard pool."
        ))

    # 2. DeepSeek key (direct).
    if os.environ.get("DEEPSEEK_API_KEY"):
        lines.append(_ok("DEEPSEEK_API_KEY set — direct deepseek-v4-flash reachable"))
    else:
        lines.append(_warn(
            "DEEPSEEK_API_KEY not set — direct deepseek-v4-flash unreachable "
            "(OpenRouter can still route there if its key is set)."
        ))

    # 3. Sidecar pre-execution.
    sidecar_value = os.environ.get("LLM_ROUTER_SIDECAR_PREFETCH", "").strip().lower()
    if sidecar_value in {"1", "true", "yes", "on"}:
        lines.append(_ok("LLM_ROUTER_SIDECAR_PREFETCH=on — introspection prompts pre-executed"))
    else:
        lines.append(_warn(
            "LLM_ROUTER_SIDECAR_PREFETCH not set — introspection prompts ('show me "
            f"my routing today', 'git status') still go through {route_tool('llm_query')} + tool "
            "calls. Set =1 to let the hook pre-execute and inject the result."
        ))

    # 4. Response router.
    rr_value = os.environ.get("LLM_ROUTER_RESPONSE_ROUTER", "on").strip().lower()
    if rr_value == "off":
        lines.append(_warn(
            "LLM_ROUTER_RESPONSE_ROUTER=off — MCP responses go to Claude unchanged. "
            "Default is on; you've explicitly disabled it."
        ))
    else:
        lines.append(_ok(
            f"LLM_ROUTER_RESPONSE_ROUTER={rr_value or 'on'} — explanations compressed "
            "before they hit Claude's context"
        ))

    # 5. Enforcement mode. Resolve through the same single source of truth the
    #    hooks use (env > repo .llm_router.yml > ~/.llm-router/routing.yaml > "smart"),
    #    so doctor reports what the enforcer ACTUALLY does — not a bare-env guess.
    #    (Reading os.environ only made doctor claim "smart/blocked" even when
    #    routing.yaml pinned "advise", i.e. never-block.)
    try:
        from llm_router.enforce_config import resolve_enforce_mode
        enforce = resolve_enforce_mode()
    except Exception:
        enforce = os.environ.get("LLM_ROUTER_ENFORCE", "").strip().lower() or "smart"
    if enforce in {"advise", "advisory"}:
        lines.append(_ok(
            f"LLM_ROUTER_ENFORCE={enforce} — route everywhere, NEVER block a tool. "
            "Routing is a helpful suggestion; Claude always keeps the final call."
        ))
    elif enforce in {"off", "shadow"}:
        lines.append(_warn(
            f"LLM_ROUTER_ENFORCE={enforce} — route directives are advisory only. "
            "Claude can bypass without consequence; quota savings are best-effort."
        ))
    elif enforce in {"suggest", "soft"}:
        lines.append(_ok(
            f"LLM_ROUTER_ENFORCE={enforce} — log-only; nudges but never blocks"
        ))
    elif enforce in {"hard", "strict"}:
        lines.append(_ok(
            f"LLM_ROUTER_ENFORCE={enforce} — all work tools blocked until routed; "
            "bypasses are blocked"
        ))
    else:
        # smart is the built-in default (F01/North Star): enforce routing out of the box.
        lines.append(_ok("LLM_ROUTER_ENFORCE=smart (default) — blocks Q&A until routed, allows code work"))

    # 5b. Loophole → LLM Router routing (P5). Loophole only hits the FULL router
    #     (policy + metering) when LLM_ROUTER_URL points at a live gateway; without
    #     it, the swarm silently falls back to local Ollama and its spend is
    #     neither policy-routed nor metered.
    _loophole_installed = (
        (Path.home() / ".claude" / "commands" / "loophole.md").exists()
        or shutil.which("loophole") is not None
    )
    if _loophole_installed:
        llm_router_url = os.environ.get("LLM_ROUTER_URL", "").strip()
        if llm_router_url:
            lines.append(_ok(
                f"LLM_ROUTER_URL={llm_router_url} — loophole routes through the full "
                "LLM Router router (policy + metering)"
            ))
        else:
            lines.append(_warn(
                "LLM_ROUTER_URL not set but loophole is installed — the swarm falls "
                "back to local Ollama instead of the full router (no cheap-first "
                "policy, no metering). Start the gateway and export "
                "LLM_ROUTER_URL=http://127.0.0.1:17900 to fix."
            ))

    # 6. Hook hint freshness — per-session shards since INV-007.
    # Find the newest last_classification_*.json across all sessions; that's
    # the closest proxy for "is any session's hook still firing".
    import glob as _glob
    shard_paths = sorted(
        _glob.glob(str(Path.home() / ".llm-router" / "last_classification_*.json")),
        key=lambda p: Path(p).stat().st_mtime if Path(p).exists() else 0,
        reverse=True,
    )
    if shard_paths:
        newest = Path(shard_paths[0])
        try:
            age = time.time() - newest.stat().st_mtime
        except OSError:
            age = None
        sid_suffix = newest.stem.removeprefix("last_classification_")[:8]
        if age is None:
            lines.append(_warn(f"last_classification_{sid_suffix}*.json unreadable"))
        elif age < 3600:
            lines.append(_ok(
                f"last_classification_{sid_suffix}*.json fresh ({int(age)}s, "
                f"{len(shard_paths)} session shard(s)) — hook hint bridge active"
            ))
        else:
            mins = int(age // 60)
            lines.append(_warn(
                f"newest last_classification_*.json is {mins}m old — auto-route "
                "hook may not be firing. Check ~/.llm-router/auto-route-debug.log for "
                "INVOCATION lines."
            ))
    else:
        lines.append(_warn(
            "~/.llm-router/last_classification_*.json missing — hook hint bridge "
            "has not run yet. Send any prompt to create it."
        ))

    # 7. Today's simple-share — the smoking-gun metric from the
    # earlier diagnostic. Pre-fix: 0/31 simple today. Healthy: > 30%.
    db = Path.home() / ".llm-router" / "usage.db"
    if db.is_file():
        _state = _routing_decision_state(db)
        try:
            conn = sqlite3.connect(str(db))
            row = conn.execute(
                "SELECT "
                "  SUM(CASE WHEN complexity='simple' THEN 1 ELSE 0 END), "
                "  COUNT(*) "
                "FROM routing_decisions "
                "WHERE date(timestamp,'localtime')=date('now','localtime') "
                "  AND COALESCE(reason_code,'') != 'sidecar_backfill'"
            ).fetchone()
            conn.close()
            simple_n, total_n = (row[0] or 0), (row[1] or 0)
        except sqlite3.Error:
            simple_n, total_n = 0, 0
        if total_n == 0:
            # GH#55: say WHICH of the three states this is.
            lines.append(_warn(_state.summary))
        else:
            share = 100.0 * simple_n / total_n
            if share >= 30.0:
                lines.append(_ok(
                    f"Today's simple-share: {simple_n}/{total_n} ({share:.1f}%) — "
                    "boundary fix is firing"
                ))
            elif share > 0.0:
                lines.append(_warn(
                    f"Today's simple-share: {simple_n}/{total_n} ({share:.1f}%) — "
                    "below 30% target. Most prompts still classifying as moderate; "
                    "check classifier."
                ))
            else:
                lines.append(_warn(
                    f"Today's simple-share: 0/{total_n} — boundary fix isn't reaching "
                    "the router. Verify auto-route hook is installed with today's source."
                ))
    else:
        lines.append(_warn("~/.llm-router/usage.db missing — no telemetry to score"))

    return lines


def _tool_surface_phantoms() -> list[str]:
    """Tier entries naming a tool nothing implements, across every tier.

    Checks all tiers rather than only the active one: a defect in a tier this
    machine does not run is still shipped to every user who does run it, and
    doctor is the thing people paste into bug reports.
    """
    from llm_router.tool_surface import phantom_tools

    found: list[str] = []
    for tier in ("core", "routing", "consolidated"):
        for name in phantom_tools(tier):
            if name not in found:
                found.append(name)
    return found


def _seats_report() -> list[str]:
    """Detect seats (3 s budget), persist them, and render one line per seat."""
    try:
        from llm_router import seats as _seats
        seats = _seats.refresh_seats()
    except Exception as e:  # noqa: BLE001 -- doctor must never crash on a probe
        return [_warn(f"seat detection failed: {e}")]

    lines: list[str] = []
    for name, seat in (
        ("Claude", seats.claude),
        ("Codex", seats.codex),
        ("Gemini CLI", seats.gemini),
        ("Ollama", seats.ollama),
    ):
        if seat.present:
            text = f"{name:<11}{seat.label()}"
            if seat.plan_stale:
                lines.append(_warn(
                    f"{text} -- the plan claim in the login token is past its window; "
                    "login still works, so it still counts as a seat"
                ))
            else:
                lines.append(_ok(text))
        elif seat.kind == "api-key":
            lines.append(_dim(f"    {name:<11}api key only (billed per call, not a seat)"))
        else:
            lines.append(_dim(f"    {name:<11}not logged in"))

    bucket = sorted(seats.free_bucket())
    if bucket:
        lines.append(_ok(f"free bucket: {', '.join(bucket)}"))
    else:
        lines.append(_warn(
            "no seat found -- every routed call is billed to an API key; "
            "log in to Claude Code, Codex, or start Ollama to get a free tier"
        ))
    sub = seats.subscription_provider()
    env_sub = os.environ.get("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "").strip()
    if sub and not env_sub:
        lines.append(_dim(f"    subscription provider defaults to '{sub}' (from the Claude seat)"))
    elif env_sub and sub and env_sub.lower() != sub:
        lines.append(_warn(
            f"LLM_ROUTER_SUBSCRIPTION_PROVIDER={env_sub} but the detected seat is '{sub}'"
        ))
    lines.append(_dim(f"    cached in ~/.llm-router/{_seats.SEATS_FILE_NAME}"))
    return lines


def _run_doctor(host: Optional[str] = None) -> tuple[int, list[str]]:
    """Comprehensive health check — verify every component is wired up.

    Returns:
        (exit_code, issues) where exit_code is 0 for success, 1 for failure
    """
    if host:
        _run_doctor_host(host)
        # Fall through to also run the full general checks
        print()

    """Comprehensive general health check — verify every component is wired up."""
    from llm_router.install_hooks import (
        _HOOKS_DST,
        _HOOK_DEFS,
        _RULES_DST,
        _SETTINGS_PATH,
        check_api_keys,
        claude_desktop_config_path,
    )

    issues: list[str] = []

    print(f"\n{_bold('llm-router doctor')}\n")

    # ── 1. Hooks ───────────────────────────────────────────────────────────
    print(_bold("  Hooks"))
    for src_name, dst_name, event, _ in _HOOK_DEFS:
        dst = _HOOKS_DST / dst_name
        if dst.exists():
            # Check version freshness (assume src_name is in same directory)
            from llm_router.install_hooks import _HOOKS_SRC
            src = _HOOKS_SRC / src_name
            if src.exists():
                src_v = _hook_version_num(src)
                dst_v = _hook_version_num(dst)
                if src_v > dst_v:
                    print(
                        _warn(
                            f"{dst_name}  v{dst_v} installed, v{src_v} available"
                        )
                    )
                    issues.append(
                        f"Hook {dst_name} is outdated — run `llm-router install --force`"
                    )
                else:
                    print(_ok(f"{dst_name}  ({event})"))
            else:
                print(_ok(f"{dst_name}  ({event})"))
        else:
            print(
                _fail(
                    f"{dst_name}  ({event})  — not installed",
                    fix="llm-router install",
                )
            )
            issues.append(f"Hook {dst_name} not installed")

    # ── 1b. Hook Python path validation (B1 from audit) ─────────────────
    print(f"\n{_bold('  Hook interpreter paths')}")
    if _SETTINGS_PATH.exists():
        try:
            _settings_data = json.loads(_SETTINGS_PATH.read_text())
            _all_hooks = _settings_data.get("hooks", {})
            for _event, _entries in _all_hooks.items():
                if not isinstance(_entries, list):
                    continue
                for _entry in _entries:
                    for _hook in _entry.get("hooks", []):
                        _cmd = _hook.get("command", "")
                        if "llm_router" not in _cmd:
                            continue
                        # Extract Python interpreter path (first token)
                        _parts = _cmd.split()
                        if _parts:
                            _interp = _parts[0]
                            if os.path.exists(_interp):
                                print(_ok(f"{os.path.basename(_parts[-1])} → {_interp}"))
                            else:
                                print(
                                    _fail(
                                        f"{os.path.basename(_parts[-1])} → {_interp} NOT FOUND",
                                        fix="llm-router install --force",
                                    )
                                )
                                issues.append(
                                    f"Hook interpreter missing: {_interp} — "
                                    f"run `llm-router install --force` to fix"
                                )
        except Exception as _e:
            print(_warn(f"Could not parse settings.json: {_e}"))

    # ── 1c. Duplicate hook detection ──────────────────────────────────────
    if _SETTINGS_PATH.exists():
        try:
            _settings_data = json.loads(_SETTINGS_PATH.read_text())
            _all_hooks = _settings_data.get("hooks", {})
            for _event, _entries in _all_hooks.items():
                if not isinstance(_entries, list):
                    continue
                _seen_scripts: dict[str, int] = {}
                for _entry in _entries:
                    for _hook in _entry.get("hooks", []):
                        _cmd = _hook.get("command", "")
                        if "llm_router" not in _cmd:
                            continue
                        _script = _cmd.split()[-1] if _cmd.split() else _cmd
                        _seen_scripts[_script] = _seen_scripts.get(_script, 0) + 1
                for _script, _count in _seen_scripts.items():
                    if _count > 1:
                        print(
                            _warn(
                                f"Duplicate: {os.path.basename(_script)} registered "
                                f"{_count}x in {_event} — manual cleanup needed in settings.json"
                            )
                        )
                        issues.append(f"Duplicate hook: {os.path.basename(_script)} ({_count}x in {_event})")
        except Exception:
            pass

    # ── 2. Routing rules ───────────────────────────────────────────────────
    print(f"\n{_bold('  Routing rules')}")
    rules_dst = _RULES_DST / "llm_router.md"
    if rules_dst.exists():
        print(_ok("llm_router.md"))
    else:
        print(_fail("llm_router.md — not installed", fix="llm-router install"))
        issues.append("Routing rules not installed")

    # ── 3. Claude Code MCP registration ────────────────────────────────────
    print(f"\n{_bold('  Claude Code MCP')}")
    settings: dict = {}
    if _SETTINGS_PATH.exists():
        try:
            settings = json.loads(_SETTINGS_PATH.read_text())
        except Exception:
            pass
    registered_cc = "llm_router" in settings.get("mcpServers", {})
    if registered_cc:
        # GH#41: registered is not the same as runnable.
        _cc_problems = _mcp_command_problems(
            settings["mcpServers"]["llm_router"], "~/.claude/settings.json"
        )
        if _cc_problems:
            for _p in _cc_problems:
                print(_fail(_p, fix="llm-router install"))
            issues.extend(_cc_problems)
        else:
            print(_ok("MCP server registered in ~/.claude/settings.json"))
    else:
        print(
            _fail(
                "MCP server not registered",
                fix="llm-router install",
            )
        )
        issues.append("MCP server not registered in Claude Code")

    # ── 4. Claude Desktop ──────────────────────────────────────────────────
    # ── 4b. Codex CLI (only when installed) ──────────────────────────────────
    # A present host with no registration is a FAIL: the user installed
    # llm-router expecting both hosts wired, and a one-way install looks
    # identical from inside Claude Code.
    try:
        from llm_router.host_detect import detect_hosts as _detect_hosts
        _codex_present = _detect_hosts()["codex"].present
    except Exception:  # noqa: BLE001
        _codex_present = False
    if _codex_present:
        print(f"\n{_bold('  Codex CLI')}")
        for line in _codex_report(issues):
            print(f"  {line}")

    print(f"\n{_bold('  Claude Desktop')}")
    desktop_path = claude_desktop_config_path()
    if desktop_path is None:
        print(_warn("not supported on this platform"))
    elif not desktop_path.exists():
        print(
            _warn(
                f"config not found ({desktop_path}) — Claude Desktop may not be installed"
            )
        )
    else:
        try:
            cfg = json.loads(desktop_path.read_text())
            if "llm_router" in cfg.get("mcpServers", {}):
                _dt_problems = _mcp_command_problems(
                    cfg["mcpServers"]["llm_router"], "Claude Desktop"
                )
                if _dt_problems:
                    for _p in _dt_problems:
                        print(_fail(_p, fix="llm-router install"))
                    issues.extend(_dt_problems)
                else:
                    print(_ok(f"registered ({desktop_path})"))
            else:
                print(
                    _fail(
                        "not registered in Claude Desktop",
                        fix="llm-router install",
                    )
                )
                issues.append("MCP server not registered in Claude Desktop")
        except Exception as e:
            print(_fail(f"could not read config: {e}"))

    # ── 5. Ollama ──────────────────────────────────────────────────────────
    print(f"\n{_bold('  Ollama (optional — free local classifier)')}")
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    try:
        req = urllib.request.Request(f"{ollama_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            model_names = [m.get("name", "") for m in data.get("models", [])]
            if model_names:
                preview = ", ".join(model_names[:3])
                if len(model_names) > 3:
                    preview += f" +{len(model_names) - 3} more"
                print(_ok(f"running — {len(model_names)} model(s): {preview}"))
            else:
                print(
                    _warn(
                        "running but no models pulled — run `ollama pull qwen2.5:0.5b`"
                    )
                )

            # ── 5a. Ensemble classifier models (GH#62) ──────────────────────
            #    Ollama being "up" says nothing about whether the specific
            #    model(s) the LLM-first ensemble classifier will actually
            #    request are among what's installed. A miss here degrades
            #    silently to the heuristic fallback (still functional), so this
            #    is read-only and non-fatal — a warning, never a doctor failure.
            from llm_router import ensemble as _ensemble

            _primary = _ensemble.primary_model()
            if not _ensemble.model_installed(_primary, model_names):
                _bare_primary = _primary.removeprefix("ollama/")
                print(
                    _warn(
                        f"ensemble primary model '{_bare_primary}' not installed — "
                        "the LLM-first classifier will silently fall back to the "
                        f"heuristic (fix: `ollama pull {_bare_primary}` or "
                        "`export LLM_ROUTER_ENSEMBLE_PRIMARY=ollama/<an installed model>`)"
                    )
                )
                issues.append(
                    f"Ensemble primary model '{_primary}' not installed in Ollama"
                )

            _secondary = _ensemble.secondary_model()
            if _secondary and not _ensemble.model_installed(_secondary, model_names):
                _bare_secondary = _secondary.removeprefix("ollama/")
                print(
                    _warn(
                        f"ensemble secondary (tiebreak) model '{_bare_secondary}' not "
                        "installed — the tiebreak vote will silently be skipped "
                        f"(fix: `ollama pull {_bare_secondary}` or "
                        "`export LLM_ROUTER_ENSEMBLE_SECONDARY=ollama/<an installed model>`)"
                    )
                )
                issues.append(
                    f"Ensemble secondary model '{_secondary}' not installed in Ollama"
                )
    except Exception:
        print(
            _warn(
                f"not reachable at {ollama_url} — optional, but saves API cost"
            )
        )

    # ── 5b. Gateway daemon — interpreter drift ─────────────────────────────
    #    If the venv is rebuilt under a different Python (e.g. uv switches
    #    3.14 → 3.11), the running daemon keeps its in-memory modules but the
    #    site-packages tree it imports from is deleted; the first lazy import
    #    (e.g. anyio._backends._asyncio) then 500s. Compare the daemon's
    #    runtime interpreter against what's on disk and demand a restart on
    #    mismatch.
    print(f"\n{_bold('  Gateway daemon (interpreter drift)')}")
    _gw_base = (
        os.environ.get("LLM_ROUTER_URL", "http://127.0.0.1:17900")
        .rstrip("/")
        .removesuffix("/v1")
    )
    _kickstart_fix = "launchctl kickstart -k gui/$UID/com.llm_router.gateway"
    _gw_data = None
    try:
        _gw_req = urllib.request.Request(f"{_gw_base}/healthz", method="GET")
        with urllib.request.urlopen(_gw_req, timeout=2) as _gw_resp:
            _gw_data = json.loads(_gw_resp.read())
    except Exception:
        print(_warn(f"gateway not reachable at {_gw_base} — drift check skipped"))
    if _gw_data is not None:
        _daemon_py = _gw_data.get("python")
        _daemon_exe = _gw_data.get("executable")
        if not _daemon_py or not _daemon_exe:
            print(
                _warn(
                    "daemon predates the drift check (no interpreter info in "
                    f"/healthz) — restart to enable: {_kickstart_fix}"
                )
            )
        elif not os.path.exists(_daemon_exe):
            print(
                _fail(
                    f"daemon interpreter deleted: {_daemon_exe} — daemon "
                    "running on orphaned interpreter — restart required",
                    fix=_kickstart_fix,
                )
            )
            issues.append(
                "Gateway daemon running on a deleted interpreter — "
                f"restart required: {_kickstart_fix}"
            )
        else:
            _disk_py = ""
            try:
                _pv = subprocess.run(
                    [_daemon_exe, "-V"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                _pv_out = (_pv.stdout or _pv.stderr or "").strip()
                _disk_py = _pv_out.split()[-1] if _pv_out else ""
            except Exception:
                pass
            if not _disk_py:
                print(_warn(f"could not determine on-disk version of {_daemon_exe}"))
            elif _disk_py != _daemon_py:
                print(
                    _fail(
                        f"daemon runtime is Python {_daemon_py} but "
                        f"{_daemon_exe} is now Python {_disk_py} — daemon "
                        "running on orphaned interpreter — restart required",
                        fix=_kickstart_fix,
                    )
                )
                issues.append(
                    f"Gateway daemon on orphaned interpreter ({_daemon_py} "
                    f"runtime vs {_disk_py} on disk) — restart required: "
                    f"{_kickstart_fix}"
                )
            else:
                print(
                    _ok(
                        f"daemon Python {_daemon_py} matches on-disk venv "
                        f"({_daemon_exe})"
                    )
                )

    # ── 6. Usage data freshness ────────────────────────────────────────────
    print(f"\n{_bold('  Usage data (Claude subscription pressure)')}")
    usage_path = Path.home() / ".llm-router" / "usage.json"
    # Quota pressure IS the headline of subscription mode. When that mode is on
    # and the file is absent, the statusline shows the user nothing and doctor
    # used to still print "all checks passed" — a check that misses the thing the
    # user is looking at is worse than no check (task 15).
    _subscription_on = os.environ.get("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if not usage_path.exists():
        _msg = (
            f"usage.json not found — quota will render as unmeasured. "
            f"Run `{route_tool('llm_check_usage')}` in Claude Code, or reinstall to seed it"
        )
        if _subscription_on:
            print(_fail(_msg, fix="llm-router install   (seeds the placeholder)"))
            issues.append("Quota data missing while subscription mode is on")
        else:
            print(_warn(_msg))
    else:
        try:
            data = json.loads(usage_path.read_text())
        except Exception as e:
            print(_fail(f"could not read usage.json: {e}"))
            data = None

        if data is None:
            pass  # already reported
        elif data.get("pending"):
            # The install-time placeholder. Not an error — but say so plainly,
            # rather than measuring an age against a zero timestamp and calling
            # the file decades stale.
            print(
                _warn(
                    "placeholder only — no refresh has run yet. Quota appears "
                    "once the first session populates it."
                )
            )
        else:
            age_s = time.time() - data.get("updated_at", 0)
            if age_s < 1800:
                print(_ok(f"fresh ({int(age_s / 60)}m old)"))
            elif age_s < 3600:
                print(
                    _warn(
                        f"getting stale ({int(age_s / 60)}m old) — run `{route_tool('llm_check_usage')}`"
                    )
                )
            else:
                print(
                    _fail(
                        f"stale ({int(age_s / 3600)}h old) — routing may use wrong pressure",
                        fix=f"Run {route_tool('llm_check_usage')} in Claude Code",
                    )
                )
                issues.append("Usage data is stale")

    # ── 7. Provider keys ───────────────────────────────────────────────────
    # ── 6b. Seats: which subscriptions this machine is logged in to ─────────
    # A seat is a subscription already paid for, so routed work landing on it
    # costs nothing extra. This is what the free bucket is derived from; a
    # plan claim past its window is a hint, not a verdict (login status is
    # the fact), so it warns and never fails.
    print(f"\n{_bold('  Seats (subscriptions this machine is logged in to)')}")
    for line in _seats_report():
        print(f"  {line}")

    print(f"\n{_bold('  Provider API keys')}")
    for line in check_api_keys():
        print(f"  {line}")

    # ── 7b. Provider circuit breakers (INV-HEALTH-001, audit C10) ───────────
    # doctor runs as a separate process from the MCP server, so it cannot read
    # the router's in-memory HealthTracker directly. The router persists a
    # wall-clock snapshot of breaker state; read it here so a tripped breaker is
    # visible in `doctor` instead of doctor reporting "all healthy" while the
    # router actively skips a provider on every request.
    print(f"\n{_bold('  Provider circuit breakers')}")
    try:
        from llm_router.health import read_health_snapshot

        snap = read_health_snapshot()
        providers = (snap or {}).get("providers", {})
        open_breakers = [
            name for name, st in providers.items()
            if st.get("circuit_state") in ("open", "rate_limited")
        ]
        if not providers:
            print(_dim("  no snapshot yet (router has not recorded a failure this session)"))

        # GH#57: DIRECT-execution timeouts never reached the breaker, so this
        # section stayed empty while the local path failed on every prompt and
        # said so only in a debug log. Report the observed latencies here, with
        # advice that distinguishes "your timeout is too low" from "this machine
        # is too slow for local routing" — telling someone with a 150s model to
        # raise a timeout sends them in circles.
        try:
            from llm_router.direct_diagnostics import current_advice

            _timeout = float(os.environ.get("LLM_ROUTER_OLLAMA_TIMEOUT", "4"))
            _advice = current_advice(timeout_s=_timeout)
            if _advice is not None:
                print(_warn(f"DIRECT execution: {_advice.message}"))
                issues.append(f"DIRECT execution timing out: {_advice.kind}")
        except Exception:
            pass
        else:
            for name in sorted(providers):
                st = providers[name]
                state = st.get("circuit_state", "unknown")
                mark = _green("✓") if state == "closed" else _yellow("⚠")
                detail = f"failures={st.get('consecutive_failures', 0)}"
                print(f"  {mark} {name}: circuit {state} ({detail})")
        for name in open_breakers:
            # Surface in the summary so doctor does NOT print "all healthy" while a
            # breaker the router enforces is open (the exact C10 divergence).
            issues.append(
                f"Provider '{name}' circuit breaker is open — router is skipping it; "
                f"run {route_call('llm_health')} for live detail"
            )
    except Exception as _h_err:  # fail-open: health reporting must never break doctor
        print(_dim(f"  (health snapshot unavailable: {_h_err})"))

    # ── 8. claw-code ───────────────────────────────────────────────────────
    print(
        f"\n{_bold('  claw-code (optional — open-source Claude Code alternative)')}"
    )
    try:
        from llm_router.install_hooks import (
            _CLAW_CODE_HOOK_DEFS,
            _claw_code_dir,
        )

        cc_dir = _claw_code_dir()
        if cc_dir is None:
            print(
                _dim(
                    "  not detected (install at github.com/claw-code/claw-code)"
                )
            )
        else:
            cc_hooks_dst = cc_dir / "hooks"
            cc_settings = {}
            cc_settings_path = cc_dir / "settings.json"
            if cc_settings_path.exists():
                try:
                    cc_settings = json.loads(cc_settings_path.read_text())
                except Exception:
                    pass
            for _, dst_name, event, _ in _CLAW_CODE_HOOK_DEFS:
                dst = cc_hooks_dst / dst_name
                if dst.exists():
                    print(_ok(f"{dst_name}  ({event})"))
                else:
                    print(
                        _fail(
                            f"{dst_name}  — not installed",
                            fix="llm-router install --claw-code",
                        )
                    )
                    issues.append(f"claw-code hook {dst_name} not installed")
            if "llm_router" in cc_settings.get("mcpServers", {}):
                print(
                    _ok("MCP server registered in claw-code settings.json")
                )
            else:
                print(
                    _fail(
                        "MCP server not registered in claw-code",
                        fix="llm-router install --claw-code",
                    )
                )
                issues.append("MCP server not registered in claw-code")
    except Exception:
        # claw-code not installed or issue importing
        pass

    # ── 9. Version ────────────────────────────────────────────────────────
    print(f"\n{_bold('  Version')}")
    try:
        from llm_router import __version__ as project_version
        print(_ok(f"llm_router {project_version}"))
    except Exception:
        try:
            from importlib.metadata import version
            v = version("llm_router")
            print(_ok(f"llm_router {v}"))
        except Exception:
            print(_warn("could not determine installed version"))

    # ── 10. Quota savings posture ─────────────────────────────────────────
    # Verifies the features that drive cost-savings in a live session are
    # actually wired up. Each finding suggests a concrete env var or
    # config change so the operator can close the gap.
    print(f"\n{_bold('  Quota savings posture')}")
    _savings_warnings = _check_savings_posture()
    for line in _savings_warnings:
        print(line)
    # Posture warnings are advisory — surface them in the summary but
    # don't fail the doctor. The user wanted to *see* whether config is
    # optimal, not be blocked by it.

    # ── 11. Tool surface vs ground truth (RED4-02) ────────────────────────
    # doctor used to exit 0 with output byte-for-byte identical to a healthy run
    # while a bogus canonical tool name sat in the CORE tier. It checked a
    # PARALLEL path: tool_surface.unregistered() validates the tier constants
    # against _TIERS, which IS the tier constants, so renaming a tool inside
    # CORE_TOOLS leaves the check reporting clean. This resolves against what is
    # actually implemented in llm_router/tools/ instead.
    print(f"\n{_bold('  Tool surface')}")
    try:
        phantoms = _tool_surface_phantoms()
    except Exception as exc:  # noqa: BLE001 — a broken check must not mask the rest
        phantoms = []
        print(_warn(f"tool-surface ground-truth check could not run: {exc}"))
    if phantoms:
        print(_fail(
            f"tier offers {len(phantoms)} tool(s) nothing implements: "
            f"{', '.join(phantoms)}",
            fix="a hint naming these fails with 'No such tool available' and the "
                "caller silently falls back to the expensive model",
        ))
        issues.append(
            f"tool surface names unimplemented tool(s): {', '.join(phantoms)}"
        )
    else:
        print(_ok("every offered tool resolves to a real implementation"))

    # ── Summary ────────────────────────────────────────────────────────────
    print()
    print(_bold("  NOT CHECKED by doctor"))
    # A green doctor implies "your install is fine". doctor checks perhaps a
    # dozen things; stating the rest is what makes a pass honest instead of
    # merely reassuring. RED4-02's real damage was that a passing run was read
    # as evidence of far more than it measured.
    for _unchecked in (
        "live routing accuracy — whether hints actually reach a cheaper model "
        "(run scripts/trace_northstar.py for the real hook path)",
        "quality of routed answers — no judge runs here",
        "cost/savings correctness — figures are reported, not verified",
        "provider availability under load, rate limits, or quota exhaustion",
        "hook behaviour on prompts other than the synthetic probe above",
    ):
        print(_dim(f"    · {_unchecked}"))
    print()

    if not issues:
        print(_green(_bold("  ✓ All doctor checks passed (see NOT CHECKED above).")))
        exit_code = 0
    else:
        print(_red(_bold(f"  ✗ {len(issues)} issue(s) found:")))
        for issue in issues:
            print(f"    {_red('•')} {issue}")
        exit_code = 1
    print()

    return exit_code, issues


def cmd_doctor(args: list[str]) -> int:
    """Execute: llm_router doctor [--host H] [--posture] [--explain-host]

    Flags:
        --host H        Run host-specific checks (claude|vscode|cursor|codex|all)
                        IN ADDITION to the general health checks.
        --posture       Print ONLY the quota-savings posture section.
                        Skips the long general health scan; ideal for
                        a fast in-session "am I configured for max
                        savings?" check.
        --explain-host  Print the always-up-to-date explainer for why
                        the host runs on Opus and what routing can vs
                        can't save. Skips everything else.

    Returns:
        0 if all checks passed, 1 if issues found.
    """
    if "--explain-host" in args:
        print(_render_host_explainer())
        return 0

    if "--posture" in args:
        print(f"\n{_bold('  Quota savings posture')}")
        for line in _check_savings_posture():
            print(line)
        print()
        return 0

    host_flag = None
    if "--host" in args:
        idx = args.index("--host")
        host_flag = args[idx + 1] if idx + 1 < len(args) else None

    exit_code, _ = _run_doctor(host=host_flag)
    return exit_code
