"""Health check — verify every component is wired up correctly.

Comprehensive diagnostic tool to check hooks, MCP registration, API keys,
Ollama availability, and host-specific configurations.
"""

import json
import os
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

from llm_router.terminal_style import Color


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
    _re = re.compile(r"#\s*llm-router-hook-version:\s*(\d+)")
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[:5]:
            m = _re.search(line)
            if m:
                return int(m.group(1))
    except OSError:
        pass
    return 0


# ── Doctor implementation ───────────────────────────────────────────────────

def _run_doctor_host(host: str) -> None:
    """Run host-specific installation checks for vscode, cursor, or claude."""
    valid_hosts = {"claude", "vscode", "cursor", "all"}
    if host not in valid_hosts:
        print(f"  Unknown host: {host}. Valid options: {', '.join(sorted(valid_hosts))}")
        return

    hosts_to_check = list({"claude", "vscode", "cursor"}) if host == "all" else [host]

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
                    if "llm-router" in data.get("servers", {}):
                        print(_ok(f"llm-router registered in {mcp_json}"))
                    else:
                        print(
                            _fail(
                                f"llm-router not in servers ({mcp_json})",
                                fix="llm-router install --host vscode",
                            )
                        )
                        issues.append("llm-router not registered in VS Code mcp.json")
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
            cursor_rules = Path.home() / ".cursor" / "rules" / "llm-router.md"

            if mcp_json.exists():
                try:
                    data = json.loads(mcp_json.read_text())
                    if "llm-router" in data.get("mcpServers", {}):
                        print(_ok(f"llm-router registered in {mcp_json}"))
                    else:
                        print(
                            _fail(
                                f"llm-router not in mcpServers ({mcp_json})",
                                fix="llm-router install --host cursor",
                            )
                        )
                        issues.append("llm-router not registered in Cursor mcp.json")
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

        if not issues:
            print(_green(f"  ✓ {h} is correctly configured"))
        else:
            print(_red(f"  {len(issues)} issue(s) found for {h}"))


def _run_doctor(host: Optional[str] = None) -> None:
    """Comprehensive health check — verify every component is wired up."""
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

    # ── 2. Routing rules ───────────────────────────────────────────────────
    print(f"\n{_bold('  Routing rules')}")
    rules_dst = _RULES_DST / "llm-router.md"
    if rules_dst.exists():
        print(_ok("llm-router.md"))
    else:
        print(_fail("llm-router.md — not installed", fix="llm-router install"))
        issues.append("Routing rules not installed")

    # ── 3. Claude Code MCP registration ────────────────────────────────────
    print(f"\n{_bold('  Claude Code MCP')}")
    settings: dict = {}
    if _SETTINGS_PATH.exists():
        try:
            settings = json.loads(_SETTINGS_PATH.read_text())
        except Exception:
            pass
    registered_cc = "llm-router" in settings.get("mcpServers", {})
    if registered_cc:
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
            if "llm-router" in cfg.get("mcpServers", {}):
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
    except Exception:
        print(
            _warn(
                f"not reachable at {ollama_url} — optional, but saves API cost"
            )
        )

    # ── 6. Usage data freshness ────────────────────────────────────────────
    print(f"\n{_bold('  Usage data (Claude subscription pressure)')}")
    usage_path = Path.home() / ".llm-router" / "usage.json"
    if not usage_path.exists():
        print(
            _warn(
                "usage.json not found — run `llm_check_usage` in Claude Code to populate"
            )
        )
    else:
        try:
            data = json.loads(usage_path.read_text())
            age_s = time.time() - data.get("updated_at", 0)
            if age_s < 1800:
                print(_ok(f"fresh ({int(age_s / 60)}m old)"))
            elif age_s < 3600:
                print(
                    _warn(
                        f"getting stale ({int(age_s / 60)}m old) — run `llm_check_usage`"
                    )
                )
            else:
                print(
                    _fail(
                        f"stale ({int(age_s / 3600)}h old) — routing may use wrong pressure",
                        fix="Run llm_check_usage in Claude Code",
                    )
                )
                issues.append("Usage data is stale")
        except Exception as e:
            print(_fail(f"could not read usage.json: {e}"))

    # ── 7. Provider keys ───────────────────────────────────────────────────
    print(f"\n{_bold('  Provider API keys')}")
    for line in check_api_keys():
        print(f"  {line}")

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
            if "llm-router" in cc_settings.get("mcpServers", {}):
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
        from importlib.metadata import version

        v = version("claude-code-llm-router")
        print(_ok(f"claude-code-llm-router {v}"))
    except Exception:
        print(_warn("could not determine installed version"))

    # ── Summary ────────────────────────────────────────────────────────────
    print()
    if not issues:
        print(_green(_bold("  ✓ All checks passed. LLM Router is healthy.")))
    else:
        print(_red(_bold(f"  {len(issues)} issue(s) found:")))
        for issue in issues:
            print(f"    {_red('•')} {issue}")
    print()


def cmd_doctor(args: list[str]) -> int:
    """Execute: llm-router doctor [--host claude|vscode|cursor|all]"""
    host_flag = None
    if "--host" in args:
        idx = args.index("--host")
        host_flag = args[idx + 1] if idx + 1 < len(args) else None

    _run_doctor(host=host_flag)
    return 0
