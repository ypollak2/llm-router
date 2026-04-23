"""Install llm-router hooks and rules globally into Claude Code.

Copies hook scripts to ``~/.claude/hooks/``, registers them in
``~/.claude/settings.json``, and installs routing rules to
``~/.claude/rules/``.

Can be run as:
  - CLI: ``llm-router-install-hooks``
  - MCP tool: ``llm_setup(action='install_hooks')``
  - Python: ``from llm_router.install_hooks import install; install()``
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path


def _python_exe() -> str:
    """Return the best Python interpreter path for use in hook command strings.

    Preference order:
    1. The interpreter currently running this code (most reliable — same venv/pipx env).
    2. ``python3`` on PATH (Linux/macOS standard).
    3. ``python`` on PATH (Windows fallback).
    """
    import shutil as _shutil
    current = sys.executable
    if current and Path(current).exists():
        return current
    if _shutil.which("python3"):
        return "python3"
    return "python"


# Where bundled hook scripts and rules live inside the package
_PACKAGE_DIR = Path(__file__).resolve().parent
_HOOKS_SRC = _PACKAGE_DIR / "hooks"
_RULES_SRC = _PACKAGE_DIR / "rules"

# Global Claude Code directories
_CLAUDE_DIR = Path.home() / ".claude"
_HOOKS_DST = _CLAUDE_DIR / "hooks"
_RULES_DST = _CLAUDE_DIR / "rules"
_SETTINGS_PATH = _CLAUDE_DIR / "settings.json"


def _claw_code_dir() -> Path | None:
    """Return the claw-code config directory, or None if not detected.

    Detection order:
    1. ``~/.claw-code/`` (primary — same as Claude Code uses ``~/.claude/``)
    2. ``$XDG_CONFIG_HOME/claw-code/`` (Linux XDG fallback)
    """
    primary = Path.home() / ".claw-code"
    if primary.exists():
        return primary
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg:
        xdg_path = Path(xdg) / "claw-code"
        if xdg_path.exists():
            return xdg_path
    return None


def claw_code_settings_path() -> Path | None:
    """Return the claw-code settings.json path if claw-code is installed."""
    d = _claw_code_dir()
    return d / "settings.json" if d is not None else None

# Provider API keys — used for post-install validation
_PROVIDER_KEYS: dict[str, str] = {
    "OPENAI_API_KEY": "OpenAI",
    "GEMINI_API_KEY": "Gemini",
    "ANTHROPIC_API_KEY": "Anthropic",
    "PERPLEXITY_API_KEY": "Perplexity",
    "GROQ_API_KEY": "Groq",
    "DEEPSEEK_API_KEY": "DeepSeek",
    "MISTRAL_API_KEY": "Mistral",
}
_SUBSCRIPTION_VAR = "LLM_ROUTER_CLAUDE_SUBSCRIPTION"

_RULES_VERSION_RE = re.compile(r"<!--\s*llm-router-rules-version:\s*(\d+)\s*-->")
_HOOK_VERSION_RE = re.compile(r"#\s*llm-router-hook-version:\s*(\d+)")


def _rules_version(path: Path) -> int:
    """Return the version number embedded in a rules file, or 0 if absent."""
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        m = _RULES_VERSION_RE.match(first_line)
        return int(m.group(1)) if m else 0
    except (OSError, IndexError):
        return 0


def _hook_version(path: Path) -> int:
    """Return the version number from a hook's second comment line, or 0 if absent."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[:5]:
            m = _HOOK_VERSION_RE.search(line)
            if m:
                return int(m.group(1))
        return 0
    except OSError:
        return 0


def _command_script_path(command: str) -> Path | None:
    """Extract the script path from a Python hook command, if present."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None

    if len(parts) >= 2 and Path(parts[0]).name.startswith("python") and parts[1].endswith(".py"):
        return Path(os.path.expanduser(parts[1]))
    return None


def check_and_update_hooks() -> list[str]:
    """Re-copy bundled hooks to ~/.claude/hooks/ if the installed versions are stale.

    Returns a list of human-readable update messages (one per updated hook).
    Called automatically on MCP server startup so existing users get hook updates
    after ``pip install --upgrade claude-code-llm-router`` without re-running install.
    Missing managed hooks are also restored. Existing files are only overwritten
    when the bundled version is newer, to avoid clobbering user-managed scripts.
    """
    updates: list[str] = []
    settings = _load_settings()
    for src_name, dst_name, _event, _matcher in _HOOK_DEFS:
        src = _HOOKS_SRC / src_name
        dst = _HOOKS_DST / dst_name
        if not src.exists():
            continue

        src_v = _hook_version(src)
        if not dst.exists():
            try:
                _HOOKS_DST.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                if sys.platform != "win32":
                    dst.chmod(0o755)
                updates.append(f"Restored missing {dst_name} v{src_v}")
            except OSError as e:
                updates.append(f"Failed to restore {dst_name}: {e}")
        else:
            dst_v = _hook_version(dst)
            if src_v > dst_v:
                try:
                    shutil.copy2(src, dst)
                    if sys.platform != "win32":
                        dst.chmod(0o755)
                    updates.append(f"Updated {dst_name} v{dst_v} → v{src_v}")
                except OSError as e:
                    updates.append(f"Failed to update {dst_name}: {e}")

        legacy_msg = _sync_legacy_hook_alias(_HOOKS_DST, settings, src_name, dst_name, src)
        if legacy_msg:
            updates.append(legacy_msg)
    return updates


def check_and_update_rules() -> str | None:
    """Re-copy bundled rules to ~/.claude/rules/ if the installed version is stale.

    Returns a status message if an update was applied, None if already up-to-date.
    Called automatically on MCP server startup so existing users get rule updates
    after ``pip install --upgrade claude-code-llm-router`` without re-running install.
    """
    rules_src = _RULES_SRC / "llm-router.md"
    rules_dst = _RULES_DST / "llm-router.md"

    if not rules_src.exists():
        return None

    src_version = _rules_version(rules_src)
    dst_version = _rules_version(rules_dst)

    if src_version <= dst_version:
        return None

    _RULES_DST.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rules_src, rules_dst)
    return f"Updated routing rules v{dst_version} → v{src_version}"


# Hook definitions: (source_filename, dest_filename, event, matcher)
_HOOK_DEFS = [
    ("session-start.py", "llm-router-session-start.py", "SessionStart", ""),
    ("auto-route.py", "llm-router-auto-route.py", "UserPromptSubmit", ""),
    ("enforce-route.py", "llm-router-enforce-route.py", "PreToolUse", ""),
    ("agent-route.py", "llm-router-agent-route.py", "PreToolUse", "Agent"),
    ("subagent-start.py", "llm-router-subagent-start.py", "SubagentStart", ""),
    ("usage-refresh.py", "llm-router-usage-refresh.py", "PostToolUse", "llm_|mcp__llm-router__llm"),
    ("cc-usage-track.py", "llm-router-cc-usage-track.py", "PostToolUse", "Agent"),
    ("playwright-compress.py", "llm-router-playwright-compress.py", "PostToolUse", ""),
    ("bash-compress.py", "llm-router-bash-compress.py", "PostToolUse", ""),
    ("session-end.py", "llm-router-session-end.py", "Stop", ""),
]

# claw-code hook definitions: same as above except:
#   - cc-usage-track.py omitted (no Anthropic OAuth subscription in claw-code)
#   - session-end and status-bar use claw-code variants (no CC pressure sections)
_CLAW_CODE_HOOK_DEFS = [
    ("session-start.py",            "llm-router-session-start.py",  "SessionStart",     ""),
    ("auto-route.py",               "llm-router-auto-route.py",     "UserPromptSubmit", ""),
    ("status-bar-clawcode.py",      "llm-router-status-bar.py",     "UserPromptSubmit", ""),
    ("enforce-route.py",            "llm-router-enforce-route.py",  "PreToolUse",       ""),
    ("agent-route.py",              "llm-router-agent-route.py",    "PreToolUse",       "Agent"),
    ("subagent-start.py",           "llm-router-subagent-start.py", "SubagentStart",    ""),
    ("usage-refresh.py",            "llm-router-usage-refresh.py",      "PostToolUse",  "llm_|mcp__llm-router__llm"),
    ("playwright-compress.py",      "llm-router-playwright-compress.py", "PostToolUse",  ""),
    ("bash-compress.py",            "llm-router-bash-compress.py",       "PostToolUse",  ""),
    ("session-end-clawcode.py",     "llm-router-session-end.py",         "Stop",         ""),
]


def _load_settings() -> dict:
    """Load ~/.claude/settings.json or return empty dict."""
    if _SETTINGS_PATH.exists():
        try:
            return json.loads(_SETTINGS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_settings(settings: dict) -> None:
    """Write settings.json atomically."""
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")


def _legacy_alias_path(hooks_dir: Path, src_name: str, dst_name: str) -> Path | None:
    """Return the legacy unprefixed hook path for a managed hook, if any."""
    if src_name == dst_name or not dst_name.startswith("llm-router-"):
        return None
    return hooks_dir / src_name


def _settings_reference_path(settings: dict, hook_path: Path) -> bool:
    """Return True when any configured hook command targets ``hook_path``."""
    normalized_target = Path(os.path.expanduser(str(hook_path)))
    for event_entries in settings.get("hooks", {}).values():
        for entry in event_entries:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks", []):
                command_path = _command_script_path(hook.get("command", ""))
                if command_path == normalized_target:
                    return True
    return False


def _sync_legacy_hook_alias(
    hooks_dir: Path,
    settings: dict,
    src_name: str,
    dst_name: str,
    src: Path,
) -> str | None:
    """Keep an existing llm-router legacy alias in sync with the canonical hook.

    We only sync unprefixed hook aliases when they are clearly managed by
    llm-router already, or when settings explicitly reference the alias path and
    the alias file is missing. This avoids overwriting unrelated third-party
    hook files with generic names like ``auto-route.py``.
    """
    alias_path = _legacy_alias_path(hooks_dir, src_name, dst_name)
    if alias_path is None:
        return None

    alias_exists = alias_path.exists()
    alias_managed = alias_exists and _hook_version(alias_path) > 0
    alias_referenced = _settings_reference_path(settings, alias_path)
    if not alias_managed and not (alias_referenced and not alias_exists):
        return None

    src_v = _hook_version(src)
    alias_v = _hook_version(alias_path) if alias_exists else 0
    if alias_exists and alias_v >= src_v:
        return None

    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, alias_path)
        if sys.platform != "win32":
            alias_path.chmod(0o755)
    except OSError as e:
        return f"Failed to sync legacy alias {src_name}: {e}"

    if alias_exists:
        return f"Updated legacy alias {src_name} v{alias_v} → v{src_v}"
    return f"Restored legacy alias {src_name} v{src_v}"


def _remove_legacy_hook_alias(hooks_dir: Path, src_name: str, dst_name: str) -> str | None:
    """Remove a managed legacy alias if it exists."""
    alias_path = _legacy_alias_path(hooks_dir, src_name, dst_name)
    if alias_path is None or not alias_path.exists() or _hook_version(alias_path) == 0:
        return None
    try:
        alias_path.unlink()
    except OSError as e:
        return f"Failed to remove legacy alias {src_name}: {e}"
    return f"Removed legacy alias {alias_path}"


def _normalize_command(command: str) -> str:
    """Normalize a hook command for comparison.

    Python hook commands are compared by script path rather than interpreter
    path so repeated installs with ``python`` vs ``python3`` or different venv
    shim paths do not create duplicate registrations.
    """
    try:
        script_path = _command_script_path(command)
    except ValueError:
        script_path = None

    if script_path is not None:
        return f"python::{script_path}"
    return command


def _register_hook(settings: dict, event: str, matcher: str, command: str) -> str:
    """Add or normalize a hook registration.

    Returns ``"added"``, ``"updated"``, or ``"existing"``.
    """
    hooks = settings.setdefault("hooks", {})
    event_hooks = hooks.setdefault(event, [])

    # Normalize the incoming command for comparison
    normalized_cmd = _normalize_command(command)

    matches: list[tuple[int, int]] = []
    for entry_idx, entry in enumerate(event_hooks):
        if not isinstance(entry, dict):
            continue
        if entry.get("matcher", "") != matcher:
            continue
        for hook_idx, hook in enumerate(entry.get("hooks", [])):
            existing_cmd = hook.get("command", "")
            if _normalize_command(existing_cmd) == normalized_cmd:
                matches.append((entry_idx, hook_idx))

    if not matches:
        event_hooks.append({
            "matcher": matcher,
            "hooks": [{"type": "command", "command": command}],
        })
        return "added"

    first_entry_idx, first_hook_idx = matches[0]
    first_entry = event_hooks[first_entry_idx]
    first_hook = first_entry["hooks"][first_hook_idx]
    changed = (
        first_hook.get("type") != "command"
        or first_hook.get("command", "") != command
        or len(matches) > 1
    )
    first_hook["type"] = "command"
    first_hook["command"] = command

    for entry_idx, hook_idx in reversed(matches[1:]):
        entry = event_hooks[entry_idx]
        hook_list = entry.get("hooks", [])
        if hook_idx < len(hook_list):
            del hook_list[hook_idx]
        if not hook_list:
            del event_hooks[entry_idx]

    return "updated" if changed else "existing"


def claude_desktop_config_path() -> Path | None:
    """Return the Claude Desktop config path for the current OS, or None."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "Claude" / "claude_desktop_config.json" if appdata else None
    # Linux / other
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "Claude" / "claude_desktop_config.json"


def _load_desktop_config(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# Path that `claude mcp add --scope user` writes to (Claude Code CLI global config)
_CLAUDE_JSON_PATH = Path.home() / ".claude.json"


def _install_claude_code_cli(mcp_entry: dict) -> list[str]:
    """Register llm-router in ~/.claude.json so `claude -p` (non-interactive) picks it up.

    Claude Code CLI reads mcpServers from ~/.claude.json (user scope), while
    ~/.claude/settings.json is used by Claude Desktop. We try two approaches:
    1. Shell out to `claude mcp add --scope user` — canonical, handles edge cases.
    2. Direct JSON merge into ~/.claude.json as fallback (no claude CLI required).
    """
    import subprocess as _sp

    # Try `claude mcp add --scope user` first
    claude_bin = shutil.which("claude")
    if claude_bin:
        cmd_str = mcp_entry["command"]
        args = mcp_entry.get("args", [])
        try:
            result = _sp.run(
                [claude_bin, "mcp", "add", "--scope", "user", "llm-router", cmd_str] + args,
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                return ["Registered llm-router MCP server in ~/.claude.json (via claude mcp add)"]
        except Exception:
            pass  # fall through to direct JSON approach

    # Direct JSON merge fallback (works without the claude CLI — Docker/CI/headless)
    try:
        data: dict = {}
        if _CLAUDE_JSON_PATH.exists():
            try:
                data = json.loads(_CLAUDE_JSON_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        servers = data.setdefault("mcpServers", {})
        if "llm-router" in servers:
            return ["MCP server already in ~/.claude.json: llm-router"]
        servers["llm-router"] = mcp_entry
        _CLAUDE_JSON_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return ["Registered llm-router MCP server in ~/.claude.json (direct merge)"]
    except OSError as e:
        return [f"WARNING: could not register MCP in ~/.claude.json: {e}"]


def _uninstall_claude_code_cli() -> list[str]:
    """Remove llm-router from ~/.claude.json."""
    import subprocess as _sp

    claude_bin = shutil.which("claude")
    if claude_bin:
        try:
            result = _sp.run(
                [claude_bin, "mcp", "remove", "--scope", "user", "llm-router"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return ["Removed llm-router from ~/.claude.json (via claude mcp remove)"]
        except Exception:
            pass

    try:
        if not _CLAUDE_JSON_PATH.exists():
            return []
        data = json.loads(_CLAUDE_JSON_PATH.read_text(encoding="utf-8"))
        if "llm-router" not in data.get("mcpServers", {}):
            return []
        del data["mcpServers"]["llm-router"]
        _CLAUDE_JSON_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return ["Removed llm-router from ~/.claude.json"]
    except OSError:
        return []


def _install_claude_desktop() -> list[str]:
    """Add llm-router to Claude Desktop's claude_desktop_config.json.

    Safe merge — never overwrites unrelated entries. Returns actions taken.
    """
    config_path = claude_desktop_config_path()
    if config_path is None:
        return ["SKIP Claude Desktop: unsupported platform"]

    llm_router_bin = shutil.which("llm-router") or "llm-router"
    entry = {"command": llm_router_bin, "args": []}

    config = _load_desktop_config(config_path)
    servers = config.setdefault("mcpServers", {})

    if "llm-router" in servers:
        return ["Claude Desktop: llm-router already registered"]

    servers["llm-router"] = entry
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return [f"Registered llm-router in Claude Desktop → {config_path}"]


def _uninstall_claude_desktop() -> list[str]:
    """Remove llm-router from Claude Desktop config. Returns actions taken."""
    config_path = claude_desktop_config_path()
    if config_path is None or not config_path.exists():
        return []

    config = _load_desktop_config(config_path)
    if "llm-router" not in config.get("mcpServers", {}):
        return []

    del config["mcpServers"]["llm-router"]
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return [f"Removed llm-router from Claude Desktop → {config_path}"]


def check_api_keys() -> list[str]:
    """Return human-readable lines describing which provider keys are set.

    Used by --check and post-install output to warn when no external providers
    are configured (router will still work via Claude subscription, but users
    should know the fallback chain will be limited).
    """
    lines: list[str] = []
    subscription_on = os.environ.get(_SUBSCRIPTION_VAR, "").lower() in ("1", "true", "yes")

    found: list[str] = []
    missing: list[str] = []
    for var, label in _PROVIDER_KEYS.items():
        if os.environ.get(var):
            found.append(label)
        else:
            missing.append(label)

    if subscription_on:
        lines.append(f"  ✓  Claude subscription mode active ({_SUBSCRIPTION_VAR}=true)")
    else:
        lines.append(f"  ⬜  Claude subscription mode off (set {_SUBSCRIPTION_VAR}=true to enable)")

    if found:
        lines.append(f"  ✓  API keys set: {', '.join(found)}")
    else:
        lines.append("  ⬜  No external provider API keys found in environment")

    if not subscription_on and not found:
        lines.append(
            "  ⚠️   No providers configured — set at least one API key or"
            f" {_SUBSCRIPTION_VAR}=true"
        )

    return lines


def install() -> list[str]:
    """Install hooks and rules globally. Returns list of actions taken."""
    actions: list[str] = []

    # ── Copy hook scripts ────────────────────────────────────────────────
    _HOOKS_DST.mkdir(parents=True, exist_ok=True)
    settings = _load_settings()

    for src_name, dst_name, event, matcher in _HOOK_DEFS:
        src = _HOOKS_SRC / src_name
        dst = _HOOKS_DST / dst_name

        if not src.exists():
            actions.append(f"SKIP {src_name}: source not found at {src}")
            continue

        shutil.copy2(src, dst)
        if sys.platform != "win32":
            dst.chmod(0o755)
        actions.append(f"Copied {src_name} → {dst}")

        command = f"{_python_exe()} {dst}"
        status = _register_hook(settings, event, matcher, command)
        if status == "added":
            actions.append(f"Registered {event} hook: {dst_name}")
        elif status == "updated":
            actions.append(f"Normalized {event} hook: {dst_name}")
        else:
            actions.append(f"Hook already registered: {dst_name}")

        legacy_msg = _sync_legacy_hook_alias(_HOOKS_DST, settings, src_name, dst_name, src)
        if legacy_msg:
            actions.append(legacy_msg)

    _save_settings(settings)

    # ── Register MCP server globally ─────────────────────────────────────
    # Build the entry using the installed llm-router binary when available
    # (pip install), falling back to uv run for development installs.
    llm_router_bin = shutil.which("llm-router")
    if llm_router_bin:
        mcp_entry: dict = {"command": llm_router_bin, "args": []}
    else:
        uv_path = shutil.which("uv") or "uv"
        project_dir = str(_PACKAGE_DIR.parent.parent)
        mcp_entry = {"command": uv_path, "args": ["run", "--directory", project_dir, "llm-router"]}

    # ~/.claude/settings.json — Claude Desktop / interactive Claude Code
    settings2 = _load_settings()
    mcp_servers = settings2.setdefault("mcpServers", {})
    if "llm-router" not in mcp_servers:
        mcp_servers["llm-router"] = mcp_entry
        _save_settings(settings2)
        actions.append("Registered llm-router MCP server in ~/.claude/settings.json")
    else:
        actions.append("MCP server already in ~/.claude/settings.json: llm-router")

    # ~/.claude.json — Claude Code CLI (`claude -p`, non-interactive, agent mode)
    actions.extend(_install_claude_code_cli(mcp_entry))

    # ── Copy routing rules ───────────────────────────────────────────────
    _RULES_DST.mkdir(parents=True, exist_ok=True)

    rules_src = _RULES_SRC / "llm-router.md"
    rules_dst = _RULES_DST / "llm-router.md"

    if rules_src.exists():
        shutil.copy2(rules_src, rules_dst)
        actions.append(f"Installed routing rules → {rules_dst}")
    else:
        actions.append(f"SKIP rules: source not found at {rules_src}")

    # ── Register in Claude Desktop ────────────────────────────────────────
    actions.extend(_install_claude_desktop())

    return actions


def uninstall() -> list[str]:
    """Remove hooks and rules. Returns list of actions taken."""
    actions: list[str] = []
    settings = _load_settings()

    # Remove hook files and settings entries
    for src_name, dst_name, event, _ in _HOOK_DEFS:
        dst = _HOOKS_DST / dst_name

        if dst.exists():
            dst.unlink()
            actions.append(f"Removed {dst}")

        legacy_msg = _remove_legacy_hook_alias(_HOOKS_DST, src_name, dst_name)
        if legacy_msg:
            actions.append(legacy_msg)

        # Remove from settings (normalize commands for matching)
        hooks = settings.get("hooks", {})
        event_hooks = hooks.get(event, [])
        # Build expected normalized command for this hook
        expected_cmd = f"{_python_exe()} {dst}"
        normalized_expected = _normalize_command(expected_cmd)
        
        filtered = [
            entry for entry in event_hooks
            if not any(
                _normalize_command(h.get("command", "")) == normalized_expected
                for h in entry.get("hooks", [])
            )
        ]
        if len(filtered) < len(event_hooks):
            hooks[event] = filtered
            actions.append(f"Unregistered {event} hook: {dst_name}")

    _save_settings(settings)

    # Remove MCP server registration (settings.json + .claude.json)
    settings2 = _load_settings()
    mcp_servers = settings2.get("mcpServers", {})
    if "llm-router" in mcp_servers:
        del mcp_servers["llm-router"]
        _save_settings(settings2)
        actions.append("Removed llm-router MCP server from ~/.claude/settings.json")
    actions.extend(_uninstall_claude_code_cli())

    # Remove rules
    rules_dst = _RULES_DST / "llm-router.md"
    if rules_dst.exists():
        rules_dst.unlink()
        actions.append(f"Removed {rules_dst}")

    # Remove from Claude Desktop
    actions.extend(_uninstall_claude_desktop())

    return actions


def install_claw_code() -> list[str]:
    """Install hooks and MCP server into claw-code's settings.json.

    Detects ``~/.claw-code/settings.json`` (or XDG fallback).  Uses
    claw-code-adapted hooks that omit the Claude Code subscription sections.
    Returns a list of human-readable actions taken.
    """
    actions: list[str] = []

    cc_dir = _claw_code_dir()
    if cc_dir is None:
        return ["SKIP claw-code: ~/.claw-code/ not found (claw-code may not be installed)"]

    hooks_dst = cc_dir / "hooks"
    settings_path = cc_dir / "settings.json"

    # ── Copy hook scripts ────────────────────────────────────────────────
    hooks_dst.mkdir(parents=True, exist_ok=True)

    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    for src_name, dst_name, event, matcher in _CLAW_CODE_HOOK_DEFS:
        src = _HOOKS_SRC / src_name
        dst = hooks_dst / dst_name

        if not src.exists():
            actions.append(f"SKIP {src_name}: source not found at {src}")
            continue

        shutil.copy2(src, dst)
        if sys.platform != "win32":
            dst.chmod(0o755)
        actions.append(f"Copied {src_name} → {dst}")

        command = f"{_python_exe()} {dst}"
        status = _register_hook(settings, event, matcher, command)
        if status == "added":
            actions.append(f"Registered {event} hook: {dst_name}")
        elif status == "updated":
            actions.append(f"Normalized {event} hook: {dst_name}")
        else:
            actions.append(f"Hook already registered: {dst_name}")

        legacy_msg = _sync_legacy_hook_alias(hooks_dst, settings, src_name, dst_name, src)
        if legacy_msg:
            actions.append(legacy_msg)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    # ── Set LLM_ROUTER_CLAW_CODE=true in ~/.claw-code/.env ───────────────
    # Ensures Ollama is always tried first for every chain (not just BUDGET),
    # because in claw-code every cloud API call costs money.
    env_path = cc_dir / ".env"
    claw_flag = "LLM_ROUTER_CLAW_CODE=true"
    try:
        existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        if "LLM_ROUTER_CLAW_CODE" not in existing:
            with env_path.open("a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(f"{claw_flag}\n")
            actions.append(f"Set {claw_flag} in {env_path}")
        else:
            actions.append(f"LLM_ROUTER_CLAW_CODE already set in {env_path}")
    except OSError as e:
        actions.append(f"WARN could not write {env_path}: {e}")

    # ── Register MCP server in claw-code settings ────────────────────────
    llm_router_bin = shutil.which("llm-router") or "llm-router"
    mcp_entry = {"command": llm_router_bin, "args": []}
    mcp_servers = settings.setdefault("mcpServers", {})
    if "llm-router" not in mcp_servers:
        mcp_servers["llm-router"] = mcp_entry
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        actions.append(f"Registered llm-router MCP server in {settings_path}")
    else:
        actions.append("MCP server already registered in claw-code")

    return actions


def uninstall_claw_code() -> list[str]:
    """Remove llm-router hooks and MCP registration from claw-code. Returns actions taken."""
    actions: list[str] = []

    cc_dir = _claw_code_dir()
    if cc_dir is None:
        return []

    hooks_dst = cc_dir / "hooks"
    settings_path = cc_dir / "settings.json"

    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    for src_name, dst_name, event, _ in _CLAW_CODE_HOOK_DEFS:
        dst = hooks_dst / dst_name
        if dst.exists():
            dst.unlink()
            actions.append(f"Removed {dst}")

        legacy_msg = _remove_legacy_hook_alias(hooks_dst, src_name, dst_name)
        if legacy_msg:
            actions.append(legacy_msg)

        hooks = settings.get("hooks", {})
        event_hooks = hooks.get(event, [])
        # Build expected normalized command for this hook
        expected_cmd = f"{_python_exe()} {dst}"
        normalized_expected = _normalize_command(expected_cmd)
        
        filtered = [
            entry for entry in event_hooks
            if not any(
                _normalize_command(h.get("command", "")) == normalized_expected
                for h in entry.get("hooks", [])
            )
        ]
        if len(filtered) < len(event_hooks):
            hooks[event] = filtered
            actions.append(f"Unregistered {event} hook: {dst_name}")

    # Remove MCP server
    mcp_servers = settings.get("mcpServers", {})
    if "llm-router" in mcp_servers:
        del mcp_servers["llm-router"]
        actions.append("Removed llm-router MCP server from claw-code")

    if settings_path.exists():
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    return actions


def main() -> None:
    """CLI entry point for llm-router-install-hooks."""

    if len(sys.argv) > 1 and sys.argv[1] == "uninstall":
        print("\nUninstalling LLM Router hooks...\n")
        actions = uninstall()
        for a in actions:
            print(f"  {a}")
        print("\nDone. Restart Claude Code to apply changes.\n")
        return

    print("\n╔══════════════════════════════════════════╗")
    print("║   LLM Router — Install Global Hooks      ║")
    print("╚══════════════════════════════════════════╝\n")

    actions = install()
    for a in actions:
        print(f"  {a}")

    print("\n✓ LLM Router hooks installed globally.")
    print("  Every Claude Code session will now auto-route tasks.")
    print("  Restart Claude Code to activate.\n")
    print("  To uninstall: llm-router-install-hooks uninstall\n")


if __name__ == "__main__":
    main()
