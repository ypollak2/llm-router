"""Install llm_router hooks and rules globally into Claude Code.

Copies hook scripts to ``~/.claude/hooks/``, registers them in
``~/.claude/settings.json``, and installs routing rules to
``~/.claude/rules/``.

Can be run as:
  - CLI: ``llm_router-install-hooks``
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
import time
from pathlib import Path
from llm_router.tool_surface import localize  # CHZ-SURF-01


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


def _legacy_llm_router_paths() -> list[Path]:
    """RED2-4-01: pre-rebrand 'llm-router' artifacts that shipped before the
    LLM Router rename. The orphaned rules file contradicts the current advise-mode
    llm_router.md (it declares routing a HARD CONSTRAINT / forbids using your own
    tools), so it must be removed on install (migration) and uninstall. Never
    referenced by the current codebase; safe to delete."""
    paths = [_RULES_DST / "llm-router.md"]
    hooks_dir = _HOOKS_DST
    if hooks_dir.exists():
        paths.extend(sorted(hooks_dir.glob("llm-router-*.py")))
    return paths


def _migrate_remove_legacy_llm_router() -> list[str]:
    """Remove the conflicting pre-rebrand llm-router.md/hooks on install/upgrade."""
    actions: list[str] = []
    for p in _legacy_llm_router_paths():
        if p.exists():
            try:
                p.unlink()
                actions.append(f"Removed conflicting legacy artifact {p}")
            except OSError:
                pass
    return actions


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

_RULES_VERSION_RE = re.compile(r"<!--\s*llm_router-rules-version:\s*(\d+)\s*-->")
_HOOK_VERSION_RE = re.compile(r"#\s*llm_router-hook-version:\s*(\d+)")


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


def _files_differ(src: Path, dst: Path) -> bool:
    """True if the two files' contents differ (byte comparison).

    Used by the content-aware update path (RED2-6-01) to detect a hook/rules
    file whose behaviour drifted from the bundled copy without a version bump.
    On any read error, report 'differ' so the safer action (re-copy) is taken.
    """
    try:
        return src.read_bytes() != dst.read_bytes()
    except OSError:
        return True


def _command_script_path(command: str) -> Path | None:
    """Extract the script path from a Python hook command, if present."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None

    if len(parts) >= 2 and Path(parts[0]).name.startswith("python") and parts[1].endswith(".py"):
        return Path(os.path.expanduser(parts[1]))
    return None


def _backup_before_overwrite(dst: Path) -> Path | None:
    """RED1-7-02: preserve the file about to be overwritten, so a hand-edited
    managed hook/rules file is never SILENTLY and PERMANENTLY destroyed.

    Returns the backup path or None if the backup could not be written (the
    caller MUST NOT overwrite when None is returned — RED1-8-02).

    RED1-8-03/RED2-8-02: never clobber an existing backup. The plain ``<dst>.bak``
    is written only if absent (it holds the FIRST captured edit); a subsequent
    drift event writes a timestamped ``<dst>.<ts>.bak`` instead, so no earlier
    hand-edit is ever lost.
    """
    try:
        primary = dst.with_suffix(dst.suffix + ".bak")
        if not primary.exists():
            shutil.copy2(dst, primary)
            return primary
        ts = time.strftime("%Y%m%d-%H%M%S")
        alt = dst.with_suffix(dst.suffix + f".{ts}.bak")
        # Guard the sub-second collision case so we still never clobber.
        n = 0
        while alt.exists():
            n += 1
            alt = dst.with_suffix(dst.suffix + f".{ts}-{n}.bak")
        shutil.copy2(dst, alt)
        return alt
    except OSError:
        return None


# CHZ-SURF-01: stdlib-only support modules copied ALONGSIDE the hooks, so a hook
# running under an interpreter without `llm_router` importable can still load them by
# path. `tool_surface` answers "which tool name is actually registered?" — without
# it a hook has to guess, and guessing is what made every routing hint 404 under
# the consolidated default. (src relative to the package dir, dst under ~/.claude/hooks/)
_HOOK_SUPPORT_FILES: tuple[tuple[str, str], ...] = (
    ("tool_surface.py", "llm_router_tool_surface.py"),
)


def _sync_hook_support_files() -> list[str]:
    """Copy the stdlib-only support modules next to the installed hooks.

    Content-addressed like the hooks themselves: re-copied whenever the bytes
    differ. These are generated artifacts (never hand-edited), so no backup dance
    is needed — but a failure is reported rather than swallowed, because a missing
    support module silently degrades routing hints.
    """
    msgs: list[str] = []
    for src_name, dst_name in _HOOK_SUPPORT_FILES:
        src = _PACKAGE_DIR / src_name
        dst = _HOOKS_DST / dst_name
        if not src.exists():
            continue
        try:
            if dst.exists() and not _files_differ(src, dst):
                continue
            _HOOKS_DST.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            msgs.append(f"Synced hook support module {dst_name}")
        except OSError as e:
            msgs.append(f"Failed to sync {dst_name}: {e} (routing hints may name unregistered tools)")
    return msgs


def check_and_update_hooks() -> list[str]:
    """Re-copy bundled hooks to ~/.claude/hooks/ if the installed versions are stale.

    Returns a list of human-readable update messages (one per updated hook).
    Called automatically on MCP server startup so existing users get hook updates
    after ``pip install --upgrade llm-routing`` without re-running install.
    Missing managed hooks are also restored.

    Existing files are overwritten when the bundled version is newer OR when the
    version stamps match but the installed bytes have drifted from the bundled
    copy (RED2-6-01: content changes without a stamp bump must still propagate).
    Before ANY such overwrite, the existing file is backed up to ``<name>.bak``
    and the backup path is reported (RED1-7-02: a user who hand-edited a managed
    hook must never silently lose that edit — it is recoverable and announced).
    """
    updates: list[str] = []
    updates.extend(_sync_hook_support_files())
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
            # RED2-6-01: content-aware, not purely version-stamp-gated. We never
            # downgrade (src_v < dst_v is left alone).
            _drifted = src_v == dst_v and _files_differ(src, dst)
            if src_v > dst_v or _drifted:
                # RED1-8-02: if the backup cannot be written, do NOT overwrite —
                # a hand-edited file must never be destroyed with no recovery path.
                backup = _backup_before_overwrite(dst)  # RED1-7-02
                if backup is None:
                    updates.append(
                        f"SKIPPED {dst_name}: could not back up existing file — "
                        f"update NOT applied (previous content preserved)"
                    )
                else:
                    try:
                        shutil.copy2(src, dst)
                        if sys.platform != "win32":
                            dst.chmod(0o755)
                        _where = f" (previous saved to {backup.name})"
                        if _drifted:
                            updates.append(f"Refreshed {dst_name} (content drift at v{src_v}){_where}")
                        else:
                            updates.append(f"Updated {dst_name} v{dst_v} → v{src_v}{_where}")
                    except OSError as e:
                        updates.append(f"Failed to update {dst_name}: {e}")

        legacy_msg = _sync_legacy_hook_alias(_HOOKS_DST, settings, src_name, dst_name, src)
        if legacy_msg:
            updates.append(legacy_msg)
    return updates


def _localized_rules_text(src: Path) -> str:
    """Bundled rules text with every tool name resolved for the ACTIVE tier.

    CHZ-SURF-01: the rules file is loaded into EVERY session and is the single
    strongest teacher of which tool to call. Shipping it with the legacy names
    while the default tier registers only the doors trained the model, in every
    session, to make a call that fails. Localizing at install/refresh time is the
    right boundary: the file is re-synced on upgrade and by the server at startup.
    """
    text = src.read_text(encoding="utf-8")
    try:
        return localize(text)
    except Exception:  # noqa: BLE001 — never fail an install over cosmetics
        return text


def _rules_content_differs(src: Path, dst: Path) -> bool:
    """Compare the INSTALLED file against what we would install now.

    Must compare against the localized text, not the raw bundle — otherwise the
    two never match and every startup reports a spurious rules refresh.
    """
    try:
        return dst.read_text(encoding="utf-8") != _localized_rules_text(src)
    except OSError:
        return True


def check_and_update_rules() -> str | None:
    """Re-copy bundled rules to ~/.claude/rules/ if the installed version is stale.

    Returns a status message if an update was applied, None if already up-to-date.
    Called automatically on MCP server startup so existing users get rule updates
    after ``pip install --upgrade llm-routing`` without re-running install.
    """
    rules_src = _RULES_SRC / "llm_router.md"
    rules_dst = _RULES_DST / "llm_router.md"

    if not rules_src.exists():
        return None

    src_version = _rules_version(rules_src)
    dst_version = _rules_version(rules_dst)

    # RED2-6-03: content-aware, same as check_and_update_hooks. Re-copy when the
    # bundled version is newer OR the versions match but the installed rules drifted
    # from bundled (a content change that forgot to bump the version stamp). Never
    # downgrade. Without this, a reworded rules file silently never reaches users.
    if src_version < dst_version:
        return None
    _drifted = src_version == dst_version and _rules_content_differs(rules_src, rules_dst)
    if src_version == dst_version and not _drifted:
        return None

    _RULES_DST.mkdir(parents=True, exist_ok=True)
    # RED1-7-02 / RED1-8-02: back up a possibly hand-edited rules file before
    # overwriting; if the backup cannot be written, skip the overwrite so the
    # user's content is never destroyed without a recovery path.
    _where = ""
    if rules_dst.exists():
        backup = _backup_before_overwrite(rules_dst)
        if backup is None:
            return (
                "SKIPPED routing rules update: could not back up existing file "
                "— update NOT applied (previous content preserved)"
            )
        _where = f" (previous saved to {backup.name})"
    rules_dst.write_text(_localized_rules_text(rules_src), encoding="utf-8")
    if _drifted:
        return f"Refreshed routing rules (content drift at v{src_version}){_where}"
    return f"Updated routing rules v{dst_version} → v{src_version}{_where}"


# Hook definitions: (source_filename, dest_filename, event, matcher)
_HOOK_DEFS = [
    ("session-start.py", "llm_router-session-start.py", "SessionStart", ""),
    ("auto-route.py", "llm_router-auto-route.py", "UserPromptSubmit", ""),
    ("status-bar.py", "llm_router-status-bar.py", "UserPromptSubmit", ""),
    ("enforce-route.py", "llm_router-enforce-route.py", "PreToolUse", ""),
    ("agent-route.py", "llm_router-agent-route.py", "PreToolUse", "Agent"),
    ("subagent-start.py", "llm_router-subagent-start.py", "SubagentStart", ""),
    ("usage-refresh.py", "llm_router-usage-refresh.py", "PostToolUse", "llm_|mcp__llm_router__llm"),
    ("cc-usage-track.py", "llm_router-cc-usage-track.py", "PostToolUse", "Agent"),
    # F5: releases the agent-depth slot agent-route.py's PreToolUse[Agent] took,
    # so depth is a LIVE nesting count. Without it, 3 lifetime spawns block all
    # further agents for the session.
    ("agent-depth-release.py", "llm_router-agent-depth-release.py", "PostToolUse", "Agent"),
    ("playwright-compress.py", "llm_router-playwright-compress.py", "PostToolUse", ""),
    ("bash-compress.py", "llm_router-bash-compress.py", "PostToolUse", ""),
    ("context-capture.py", "llm_router-context-capture.py", "PostToolUse", ""),
    ("session-end.py", "llm_router-session-end.py", "Stop", ""),
]

# Sidecar shell scripts session-start.py shells out to (start-ollama.sh,
# start-pxpipe.sh) — not hook-event scripts themselves (no event/matcher to
# register), just plain files that must land next to it. These were never
# wired into install() at all: same-named source/dest, so no llm_router- prefix
# rename like the Python hooks above get.
_SIDECAR_SCRIPTS = ["start-ollama.sh", "start-pxpipe.sh"]

# claw-code hook definitions: same as above except:
#   - cc-usage-track.py omitted (no Anthropic OAuth subscription in claw-code)
#   - session-end and status-bar use claw-code variants (no CC pressure sections)
_CLAW_CODE_HOOK_DEFS = [
    ("session-start.py",            "llm_router-session-start.py",  "SessionStart",     ""),
    ("auto-route.py",               "llm_router-auto-route.py",     "UserPromptSubmit", ""),
    ("status-bar-clawcode.py",      "llm_router-status-bar.py",     "UserPromptSubmit", ""),
    ("enforce-route.py",            "llm_router-enforce-route.py",  "PreToolUse",       ""),
    ("agent-route.py",              "llm_router-agent-route.py",    "PreToolUse",       "Agent"),
    ("subagent-start.py",           "llm_router-subagent-start.py", "SubagentStart",    ""),
    ("usage-refresh.py",            "llm_router-usage-refresh.py",      "PostToolUse",  "llm_|mcp__llm_router__llm"),
    # F5: release the agent-depth slot (claw-code also uses the agent-route.py incrementer).
    ("agent-depth-release.py",      "llm_router-agent-depth-release.py", "PostToolUse",  "Agent"),
    ("playwright-compress.py",      "llm_router-playwright-compress.py", "PostToolUse",  ""),
    ("bash-compress.py",            "llm_router-bash-compress.py",       "PostToolUse",  ""),
    ("context-capture.py",          "llm_router-context-capture.py",     "PostToolUse",  ""),
    ("session-end-clawcode.py",     "llm_router-session-end.py",         "Stop",         ""),
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
    """Write settings.json atomically, backing up an unparseable existing file.

    CHZ-PKG-008: ``_load_settings`` silently returns ``{}`` when the existing
    settings.json can't be parsed, so a malformed-but-user-authored file was
    then overwritten and lost with no backup. Before overwriting, if the current
    file exists and does NOT parse as JSON, copy it to a timestamped ``.bak`` so
    the user can recover it. The write itself is atomic (tmp + os.replace).
    """
    import time

    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _SETTINGS_PATH.exists():
        try:
            json.loads(_SETTINGS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            try:
                backup = _SETTINGS_PATH.with_name(
                    f"settings.json.corrupt.{int(time.time())}.bak"
                )
                backup.write_bytes(_SETTINGS_PATH.read_bytes())
            except OSError:
                pass
    tmp = _SETTINGS_PATH.with_name("settings.json.tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, _SETTINGS_PATH)


def _legacy_alias_path(hooks_dir: Path, src_name: str, dst_name: str) -> Path | None:
    """Return the legacy unprefixed hook path for a managed hook, if any."""
    if src_name == dst_name or not dst_name.startswith("llm_router-"):
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
    """Keep an existing llm_router legacy alias in sync with the canonical hook.

    We only sync unprefixed hook aliases when they are clearly managed by
    llm_router already, or when settings explicitly reference the alias path and
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


def _hook_is_registered(settings: dict, event: str, matcher: str, command: str) -> bool:
    """Return True when the nested Claude Code hook settings contain a command."""
    normalized_cmd = _normalize_command(command)
    hooks = settings.get("hooks", {})
    event_hooks = hooks.get(event, []) if isinstance(hooks, dict) else []
    for entry in event_hooks:
        if not isinstance(entry, dict) or entry.get("matcher", "") != matcher:
            continue
        for hook in entry.get("hooks", []):
            if isinstance(hook, dict) and _normalize_command(hook.get("command", "")) == normalized_cmd:
                return True
    return False


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
        # C2: LOCALAPPDATA is the fallback when APPDATA is unset (common in
        # some CI, Docker, and non-standard Windows environments).
        appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA", "")
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
    """Register llm_router in ~/.claude.json so `claude -p` (non-interactive) picks it up.

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
                [claude_bin, "mcp", "add", "--scope", "user", "llm_router", cmd_str] + args,
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                return ["Registered llm_router MCP server in ~/.claude.json (via claude mcp add)"]
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
        if "llm_router" in servers:
            return ["MCP server already in ~/.claude.json: llm_router"]
        servers["llm_router"] = mcp_entry
        _CLAUDE_JSON_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return ["Registered llm_router MCP server in ~/.claude.json (direct merge)"]
    except OSError as e:
        return [f"WARNING: could not register MCP in ~/.claude.json: {e}"]


def _uninstall_claude_code_cli() -> list[str]:
    """Remove llm_router from ~/.claude.json."""
    import subprocess as _sp

    claude_bin = shutil.which("claude")
    if claude_bin:
        try:
            result = _sp.run(
                [claude_bin, "mcp", "remove", "--scope", "user", "llm_router"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return ["Removed llm_router from ~/.claude.json (via claude mcp remove)"]
        except Exception:
            pass

    try:
        if not _CLAUDE_JSON_PATH.exists():
            return []
        data = json.loads(_CLAUDE_JSON_PATH.read_text(encoding="utf-8"))
        if "llm_router" not in data.get("mcpServers", {}):
            return []
        del data["mcpServers"]["llm_router"]
        _CLAUDE_JSON_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return ["Removed llm_router from ~/.claude.json"]
    except OSError:
        return []


def _install_claude_desktop() -> list[str]:
    """Add llm_router to Claude Desktop's claude_desktop_config.json.

    Safe merge — never overwrites unrelated entries. Returns actions taken.
    """
    config_path = claude_desktop_config_path()
    if config_path is None:
        return ["SKIP Claude Desktop: unsupported platform"]

    llm_router_bin = shutil.which("llm_router") or "llm_router"
    entry = {"command": llm_router_bin, "args": []}

    config = _load_desktop_config(config_path)
    servers = config.setdefault("mcpServers", {})

    if "llm_router" in servers:
        return ["Claude Desktop: llm_router already registered"]

    servers["llm_router"] = entry
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return [f"Registered llm_router in Claude Desktop → {config_path}"]


def _uninstall_claude_desktop() -> list[str]:
    """Remove llm_router from Claude Desktop config. Returns actions taken."""
    config_path = claude_desktop_config_path()
    if config_path is None or not config_path.exists():
        return []

    config = _load_desktop_config(config_path)
    if "llm_router" not in config.get("mcpServers", {}):
        return []

    del config["mcpServers"]["llm_router"]
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return [f"Removed llm_router from Claude Desktop → {config_path}"]


def check_api_keys() -> list[str]:
    """Return human-readable lines describing which provider keys are set.

    Used by --check and post-install output to warn when no external providers
    are configured (router will still work via Claude subscription, but users
    should know the fallback chain will be limited).
    """
    lines: list[str] = []
    subscription_on = os.environ.get(_SUBSCRIPTION_VAR, "").lower() in ("1", "true", "yes")
    gemini_subscription_on = os.environ.get("LLM_ROUTER_GEMINI_SUBSCRIPTION", "").lower() in ("1", "true", "yes")

    found: list[str] = []
    missing: list[str] = []
    for var, label in _PROVIDER_KEYS.items():
        if os.environ.get(var):
            found.append(label)
        else:
            missing.append(label)

    if subscription_on:
        lines.append(f"  ✓  Claude subscription mode active ({_SUBSCRIPTION_VAR}=true)")
    if gemini_subscription_on:
        lines.append("  ✓  Gemini subscription mode active (LLM_ROUTER_GEMINI_SUBSCRIPTION=true)")

    if not subscription_on and not gemini_subscription_on:
        lines.append(f"  ⬜  Claude subscription mode off (set {_SUBSCRIPTION_VAR}=true to enable)")

    if found:
        lines.append(f"  ✓  API keys set: {', '.join(found)}")
    else:
        lines.append("  ⬜  No external provider API keys found in environment")

    if not subscription_on and not gemini_subscription_on and not found:
        lines.append(
            "  ⚠️   No providers configured — set at least one API key or"
            " subscription mode"
        )

    return lines


def install(force: bool = False) -> list[str]:
    """Install hooks and rules globally. Returns list of actions taken.
    
    Args:
        force: If True, overwrite existing hooks even if they appear up to date.
    """
    actions: list[str] = []

    # ── Copy hook scripts ────────────────────────────────────────────────
    _HOOKS_DST.mkdir(parents=True, exist_ok=True)
    actions.extend(_sync_hook_support_files())  # CHZ-SURF-01
    settings = _load_settings()

    for src_name, dst_name, event, matcher in _HOOK_DEFS:
        src = _HOOKS_SRC / src_name
        dst = _HOOKS_DST / dst_name

        if not src.exists():
            actions.append(f"SKIP {src_name}: source not found at {src}")
            continue

        # Check if we should skip based on same-content (unless forced)
        if not force and dst.exists():
            try:
                if src.read_bytes() == dst.read_bytes():
                    # Check if registration is also correct
                    command = f"{_python_exe()} {dst}"
                    already_registered = _hook_is_registered(settings, event, matcher, command)
                    if already_registered:
                        continue
            except OSError:
                pass

        # RED1-9-01 / RED1-11-02: back up a hand-edited managed hook before install
        # overwrites it, and if the backup CANNOT be written, SKIP the overwrite —
        # never destroy a user's edit with no recovery path (parity with the
        # auto-update path). Only when the installed file differs from bundled.
        if dst.exists() and _files_differ(src, dst):
            _b = _backup_before_overwrite(dst)
            if _b is None:
                actions.append(
                    f"SKIPPED {src_name}: could not back up existing file — not overwritten"
                )
                continue
            actions.append(f"Backed up existing {dst_name} → {_b.name}")
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

    # ── Copy sidecar scripts (not hook-event scripts, just files
    # session-start.py shells out to) ──────────────────────────────────────
    for name in _SIDECAR_SCRIPTS:
        src = _HOOKS_SRC / name
        dst = _HOOKS_DST / name
        if not src.exists():
            actions.append(f"SKIP {name}: source not found at {src}")
            continue
        if not force and dst.exists() and src.read_bytes() == dst.read_bytes():
            continue
        shutil.copy2(src, dst)
        if sys.platform != "win32":
            dst.chmod(0o755)
        actions.append(f"Copied {name} → {dst}")

    _save_settings(settings)

    # ── Register MCP server globally ─────────────────────────────────────
    # Build the entry using the installed llm_router binary when available
    # (pip install), falling back to uv run for development installs.
    llm_router_bin = shutil.which("llm_router")
    if llm_router_bin:
        mcp_entry: dict = {"command": llm_router_bin, "args": []}
    else:
        uv_path = shutil.which("uv") or "uv"
        project_dir = str(_PACKAGE_DIR.parent.parent)
        mcp_entry = {"command": uv_path, "args": ["run", "--directory", project_dir, "llm_router"]}

    # ~/.claude/settings.json — Claude Desktop / interactive Claude Code
    settings2 = _load_settings()
    mcp_servers = settings2.setdefault("mcpServers", {})
    if "llm_router" not in mcp_servers:
        mcp_servers["llm_router"] = mcp_entry
        _save_settings(settings2)
        actions.append("Registered llm_router MCP server in ~/.claude/settings.json")
    else:
        actions.append("MCP server already in ~/.claude/settings.json: llm_router")

    # ~/.claude.json — Claude Code CLI (`claude -p`, non-interactive, agent mode)
    actions.extend(_install_claude_code_cli(mcp_entry))

    # ── Copy routing rules ───────────────────────────────────────────────
    _RULES_DST.mkdir(parents=True, exist_ok=True)

    rules_src = _RULES_SRC / "llm_router.md"
    rules_dst = _RULES_DST / "llm_router.md"

    if rules_src.exists():
        # RED1-11-01: back up a hand-edited rules file before overwriting; if the
        # backup can't be written, skip the overwrite (never destroy user content
        # with no recovery path) — parity with the auto-update path.
        # CHZ-SURF-01: write the LOCALIZED text (tool names resolved for the active
        # tier), and compare against it — comparing against the raw bundle would
        # report drift on every run.
        if rules_dst.exists() and _rules_content_differs(rules_src, rules_dst):
            _rb = _backup_before_overwrite(rules_dst)
            if _rb is None:
                actions.append(
                    "SKIPPED routing rules: could not back up existing file — not overwritten"
                )
            else:
                actions.append(f"Backed up existing llm_router.md → {_rb.name}")
                rules_dst.write_text(_localized_rules_text(rules_src), encoding="utf-8")
                actions.append(f"Installed routing rules → {rules_dst}")
        else:
            rules_dst.write_text(_localized_rules_text(rules_src), encoding="utf-8")
            actions.append(f"Installed routing rules → {rules_dst}")
    else:
        actions.append(f"SKIP rules: source not found at {rules_src}")

    # RED2-4-01: heal the pre-rebrand conflict on every install/upgrade — remove
    # the orphaned llm-router.md (which declares routing a HARD CONSTRAINT and
    # contradicts the advise-mode llm_router.md just written above) and its dormant
    # llm-router-*.py hooks.
    actions.extend(_migrate_remove_legacy_llm_router())

    # ── Install statusLine command ──────────────────────────────────────
    statusline_src = _HOOKS_SRC / "statusline-command.sh"
    statusline_dst = _HOOKS_DST / "llm_router-statusline.sh"
    if statusline_src.exists():
        shutil.copy2(statusline_src, statusline_dst)
        if sys.platform != "win32":
            statusline_dst.chmod(0o755)
        actions.append(f"Installed statusline → {statusline_dst}")

        # Register statusLine in settings.json
        # C1: On Windows, bash may not be available. Only register if bash is
        # found in PATH (covers WSL/Git Bash users); skip gracefully otherwise.
        import shutil as _shutil_sl
        settings3 = _load_settings()
        if sys.platform == "win32" and not _shutil_sl.which("bash"):
            actions.append("statusLine skipped on Windows (bash not in PATH — install Git Bash or WSL)")
        else:
            statusline_cmd = f"bash {statusline_dst}"
            current_sl = settings3.get("statusLine")
            _already_ours = (
                isinstance(current_sl, dict) and current_sl.get("command") == statusline_cmd
            )
            if not _already_ours:
                # RED4-01 (P0): this used to assign straight over settings.json's
                # statusLine. A user's own status line — Powerline, a custom
                # script, anything — was destroyed silently: no backup, no
                # warning, and uninstall did not put it back. "We only touch one
                # key" is not a defence when that key IS the feature the user
                # configured.
                #
                # Three things happen before the write and all three are load
                # bearing: CAPTURE so uninstall can undo it, BACKUP so the file is
                # recoverable even if the manifest is lost, and WARN so the user
                # learns at install time instead of by noticing it is gone.
                from llm_router import install_manifest as _im

                _is_ours = isinstance(current_sl, dict) and "llm_router-statusline.sh" in str(
                    current_sl.get("command", "")
                )
                if not _is_ours and _im.find("json_key", _SETTINGS_PATH, key="statusLine") is None:
                    # Captured once, by the first install that sees a foreign
                    # value. A re-install finds llm_router's own command in the key,
                    # and re-capturing would overwrite the user's original with
                    # llm_router's replacement — destroying precisely what the record
                    # exists to preserve.
                    _im.record(
                        "json_key",
                        _SETTINGS_PATH,
                        key="statusLine",
                        had_key=current_sl is not None,
                        previous=current_sl,
                    )
                    if current_sl is not None:
                        _b = _backup_before_overwrite(_SETTINGS_PATH)
                        _where = f"; backup at {_b.name}" if _b else ""
                        actions.append(
                            "WARNING: replacing an existing statusLine in "
                            f"settings.json{_where}. The original is recorded and "
                            "`llm_router uninstall` restores it."
                        )

                settings3["statusLine"] = {
                    "type": "command",
                    "command": statusline_cmd,
                }
                _save_settings(settings3)
                actions.append("Registered statusLine command in settings.json")
            else:
                actions.append("statusLine already configured")

    # ── Register in Claude Desktop ────────────────────────────────────────
    actions.extend(_install_claude_desktop())

    # ── Populate the agentic-model registry (Fix #3) ──────────────────────
    # Probe which installed Ollama models can actually drive the tool-loop, so
    # the dynamic model picker has verified verdicts without a cold-probe stall
    # on first agentic use. Runs detached (best-effort) — never blocks install.
    try:
        from llm_router.agentic_registry import populate_in_background
        if populate_in_background():
            actions.append("Probing local models for agentic capability (background; see `llm_router probe`)")
    except Exception:
        pass

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
    if "llm_router" in mcp_servers:
        del mcp_servers["llm_router"]
        _save_settings(settings2)
        actions.append("Removed llm_router MCP server from ~/.claude/settings.json")
    actions.extend(_uninstall_claude_code_cli())

    # Remove rules
    rules_dst = _RULES_DST / "llm_router.md"
    if rules_dst.exists():
        rules_dst.unlink()
        actions.append(f"Removed {rules_dst}")

    # RED2-4-01: also remove PRE-REBRAND "llm-router" artifacts that earlier
    # versions installed under the old identity. They are never referenced by the
    # current codebase, so uninstall previously left them behind forever — and the
    # orphaned llm-router.md rules file actively contradicts the current advise-mode
    # llm_router.md (it declares routing a HARD CONSTRAINT), silently overriding intent
    # in every session. Clean them up on uninstall, and on install (see
    # _migrate_remove_legacy_llm_router below) so an upgrade heals the conflict.
    for _legacy in _legacy_llm_router_paths():
        if _legacy.exists():
            try:
                _legacy.unlink()
                actions.append(f"Removed legacy pre-rebrand artifact {_legacy}")
            except OSError:
                pass

    # RED2-5-01: remove the statusLine script + its settings.json registration.
    # install() copies llm_router-statusline.sh and registers a `bash <path>`
    # statusLine command; uninstall previously left both, so Claude Code kept
    # executing the llm_router script on every render after the user uninstalled.
    statusline_dst = _HOOKS_DST / "llm_router-statusline.sh"
    if statusline_dst.exists():
        try:
            statusline_dst.unlink()
            actions.append(f"Removed {statusline_dst}")
        except OSError:
            pass
    settings_sl = _load_settings()
    current_sl = settings_sl.get("statusLine")
    if isinstance(current_sl, dict) and "llm_router-statusline.sh" in str(
        current_sl.get("command", "")
    ):
        # RED4-01: restore rather than delete. Deleting leaves a user who HAD a
        # status line with none at all, which is the same loss as the original
        # defect — just discovered at uninstall instead of install.
        from llm_router import install_manifest as _im

        _rec = _im.find("json_key", _SETTINGS_PATH, key="statusLine")
        if _rec is not None:
            actions += _im._restore_json_key(
                _SETTINGS_PATH, "statusLine", bool(_rec.get("had_key")), _rec.get("previous")
            )
        else:
            del settings_sl["statusLine"]
            _save_settings(settings_sl)
            actions.append("Removed statusLine command from ~/.claude/settings.json")

    # RED4-08: the hook support modules copied by _sync_hook_support_files()
    # carry no event/matcher, so the _HOOK_DEFS removal loop never saw them and
    # they were left behind in ~/.claude/hooks/ after uninstall.
    for _src_name, _dst_name in _HOOK_SUPPORT_FILES:
        _support = _HOOKS_DST / _dst_name
        if _support.exists():
            try:
                _support.unlink()
                actions.append(f"Removed hook support module {_support}")
            except OSError as e:
                actions.append(f"  could not remove {_support}: {e}")

    # RED2-5-02: remove the sidecar helper scripts install() copied into the
    # hooks dir. They carry no event/matcher so the _HOOK_DEFS removal loop above
    # never touched them, leaving them orphaned on disk after uninstall.
    for _name in _SIDECAR_SCRIPTS:
        _sidecar = _HOOKS_DST / _name
        if _sidecar.exists():
            try:
                _sidecar.unlink()
                actions.append(f"Removed sidecar script {_sidecar}")
            except OSError:
                pass

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

    # ── Copy sidecar scripts (see install() for why) ────────────────────────
    for name in _SIDECAR_SCRIPTS:
        src = _HOOKS_SRC / name
        dst = hooks_dst / name
        if not src.exists():
            actions.append(f"SKIP {name}: source not found at {src}")
            continue
        shutil.copy2(src, dst)
        if sys.platform != "win32":
            dst.chmod(0o755)
        actions.append(f"Copied {name} → {dst}")

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
    llm_router_bin = shutil.which("llm_router") or "llm_router"
    mcp_entry = {"command": llm_router_bin, "args": []}
    mcp_servers = settings.setdefault("mcpServers", {})
    if "llm_router" not in mcp_servers:
        mcp_servers["llm_router"] = mcp_entry
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        actions.append(f"Registered llm_router MCP server in {settings_path}")
    else:
        actions.append("MCP server already registered in claw-code")

    return actions


def uninstall_claw_code() -> list[str]:
    """Remove llm_router hooks and MCP registration from claw-code. Returns actions taken."""
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
    if "llm_router" in mcp_servers:
        del mcp_servers["llm_router"]
        actions.append("Removed llm_router MCP server from claw-code")

    # RED2-5-02: remove the sidecar helper scripts install_claw_code() copied in.
    for _name in _SIDECAR_SCRIPTS:
        _sidecar = hooks_dst / _name
        if _sidecar.exists():
            try:
                _sidecar.unlink()
                actions.append(f"Removed sidecar script {_sidecar}")
            except OSError:
                pass

    # RED2-5-02: strip the LLM_ROUTER_CLAW_CODE=true marker install_claw_code() wrote
    # into ~/.claw-code/.env, so the claw-code host stops believing llm_router is
    # active after uninstall. Parse-and-rewrite, dropping only that line.
    env_path = cc_dir / ".env"
    if env_path.exists():
        try:
            _lines = env_path.read_text(encoding="utf-8").splitlines()
            _kept = [ln for ln in _lines if not ln.strip().startswith("LLM_ROUTER_CLAW_CODE")]
            if len(_kept) != len(_lines):
                _body = "\n".join(_kept)
                env_path.write_text(_body + ("\n" if _body else ""), encoding="utf-8")
                actions.append(f"Removed LLM_ROUTER_CLAW_CODE flag from {env_path}")
        except OSError:
            pass

    if settings_path.exists():
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    return actions


# ── IDE config installation (pull-routing: VS Code/Copilot, Windsurf, Cursor) ──

_VSCODE_MCP_CONTENT = localize("""\
{
  "servers": {
    "llm_router": {
      "type": "stdio",
      "command": "llm_router",
      "args": [],
      "description": "LLM Router smart LLM router — routes tasks to the cheapest capable model (Ollama → Gemini Flash → GPT-4o-mini → Claude). Call llm_code for coding tasks, llm_query for questions, llm_analyze for analysis, llm_research for web search. Each call routes to a cheaper capable model before using Claude quota."
    }
  }
}
""")

_WINDSURF_MCP_CONTENT = localize("""\
{
  "mcpServers": {
    "llm_router": {
      "command": "llm_router",
      "args": [],
      "description": "LLM Router smart LLM router — routes tasks to the cheapest capable model (Ollama → Gemini Flash → GPT-4o-mini → Claude). Call llm_code for coding tasks, llm_query for questions, llm_analyze for analysis, llm_research for web search. Each call routes to a cheaper capable model before using Claude quota."
    }
  }
}
""")

_CURSOR_RULE_CONTENT = localize("""\
---
description: Route tasks through LLM Router to save tokens and cost
globs: ["**/*"]
alwaysApply: true
---

# LLM Router routing — use MCP tools before answering directly

LLM Router MCP tools route each task to the cheapest model that can handle it.
Calling them before generating your own answer routes to a cheaper capable model,
conserving Claude quota.

> **Important — pull routing:** Unlike Claude Code (which intercepts prompts
> automatically via hooks), Cursor uses pull routing: YOU must call the tool.
> The tool returns the response; relay it to the user instead of generating
> your own answer.

## When to call which tool

| User request type | Tool to call first |
|---|---|
| Write / refactor / fix code | `llm_code(prompt=..., complexity="moderate")` |
| Quick question / definition / how-to | `llm_query(prompt=..., complexity="simple")` |
| Debug / analyze / review code | `llm_analyze(prompt=...)` |
| Research / current events / docs | `llm_research(prompt=...)` |
| Write prose / docs / email | `llm_generate(prompt=...)` |
| Deep reasoning / proofs / root cause | `llm_reason(prompt=...)` |

## Rules

1. Call the matching tool **before** generating your own response.
2. Return the tool's output to the user **as-is** (don't paraphrase).
3. Only use native Cursor intelligence for file navigation, terminal commands,
   or when all LLM Router MCP servers are unavailable.
4. If `llm_code` or similar is unavailable, proceed normally and note it.
""")


def install_ide_configs(project_dir: Path | None = None) -> list[str]:
    """Write pull-routing IDE config files into the given project directory.

    Creates:
      <project_dir>/.vscode/mcp.json          — VS Code / GitHub Copilot Chat
      <project_dir>/.windsurf/mcp.json        — Windsurf / Cascade
      <project_dir>/.cursor/rules/use-llm_router.mdc — Cursor agent rules

    Pull routing note: these IDEs call LLM Router tools only when the model decides
    to (unlike Claude Code's UserPromptSubmit hooks which intercept every prompt
    automatically). The Cursor rule file nudges the model to call LLM Router first,
    approximating push routing without a true hook mechanism.

    Args:
        project_dir: Root of the target project. Defaults to the current directory.

    Returns:
        List of human-readable action strings describing what was written.
    """
    root = Path(project_dir) if project_dir else Path.cwd()
    actions: list[str] = []

    configs: list[tuple[Path, str]] = [
        (root / ".vscode" / "mcp.json", _VSCODE_MCP_CONTENT),
        (root / ".windsurf" / "mcp.json", _WINDSURF_MCP_CONTENT),
        (root / ".cursor" / "rules" / "use-llm_router.mdc", _CURSOR_RULE_CONTENT),
    ]

    for path, content in configs:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing == content:
                actions.append(f"Already up-to-date: {path}")
                continue
        path.write_text(content, encoding="utf-8")
        actions.append(f"Wrote {path}")

    return actions


def uninstall_ide_configs(project_dir: Path | None = None) -> list[str]:
    """Remove LLM Router-managed IDE config from the given project directory.

    RED2-11-01/02: ``.vscode/mcp.json`` and ``.windsurf/mcp.json`` are SHARED
    config files — a user keeps their own MCP servers there. Wholesale-unlinking
    them (the previous behaviour) destroyed unrelated user config. Remove ONLY the
    llm_router entry surgically. Only a dedicated llm_router-authored file
    (``.cursor/rules/use-llm_router.mdc``) is safe to delete outright.

    RED2-9-03: the project-scoped .github/copilot-instructions.md and Trae .rules
    are written via _append_routing_rules → recorded in the install manifest
    (created-vs-appended aware), so the manifest replay removes them correctly.
    """
    root = Path(project_dir) if project_dir else Path.cwd()
    actions: list[str] = []
    from llm_router import install_manifest as _im

    # Shared MCP config — surgical removal of the llm_router entry only.
    actions += _im._remove_json_key(root / ".vscode" / "mcp.json", "servers", "llm_router")
    actions += _im._remove_json_key(root / ".windsurf" / "mcp.json", "mcpServers", "llm_router")

    # Dedicated llm_router-authored rule file — safe to remove entirely.
    mdc = root / ".cursor" / "rules" / "use-llm_router.mdc"
    if mdc.exists():
        try:
            mdc.unlink()
            actions.append(f"Removed {mdc}")
        except OSError:
            pass

    return actions


def main() -> None:
    """CLI entry point for llm_router-install-hooks."""

    args = sys.argv[1:]
    cmd = args[0] if args else "install"

    if cmd == "uninstall":
        # RED2-7-01: delegate to the single uninstall implementation so this
        # frozen public entry point (`llm_router-install-hooks uninstall`) cleans up
        # everything install could have created — claw-code + IDE configs
        # included — exactly like `llm_router uninstall`. Previously main() called
        # only uninstall(), leaving a full parallel claw-code install behind.
        from llm_router.commands.uninstall import _run_uninstall
        _run_uninstall(args[1:])
        return

    if cmd == "ide":
        # Install pull-routing configs into the current project directory
        project_dir = Path(args[1]) if len(args) > 1 else Path.cwd()
        print("\n╔══════════════════════════════════════════╗")
        print( "║   LLM Router — Install IDE Configs       ║")
        print( "╚══════════════════════════════════════════╝\n")
        print(f"  Target: {project_dir}\n")
        ide_actions = install_ide_configs(project_dir)
        for a in ide_actions:
            print(f"  {a}")
        print()
        _print_pull_routing_notice()
        return

    if cmd in ("--help", "-h", "help"):
        _print_help()
        return

    # Default: install Claude Code hooks + IDE configs
    print("\n╔══════════════════════════════════════════╗")
    print("║   LLM Router — Install Global Hooks      ║")
    print("╚══════════════════════════════════════════╝\n")

    actions = install()
    for a in actions:
        print(f"  {a}")

    print("\n✓ LLM Router hooks installed globally.")
    print("  Every Claude Code session will now auto-route tasks.")
    print("  Restart Claude Code to activate.\n")

    # Also write IDE configs into cwd if it looks like a project root
    cwd = Path.cwd()
    if (cwd / "pyproject.toml").exists() or (cwd / "package.json").exists() or (cwd / ".git").exists():
        print("  Installing pull-routing IDE configs for this project...\n")
        ide_actions = install_ide_configs(cwd)
        for a in ide_actions:
            print(f"  {a}")
        print()
        _print_pull_routing_notice()

    print("  To uninstall: llm_router-install-hooks uninstall\n")


def _print_pull_routing_notice() -> None:
    print("  ╔──────────────────────────────────────────────────────────────╗")
    print("  │  Push vs Pull routing — important difference                │")
    print("  │                                                              │")
    print("  │  Claude Code (push):  hooks intercept EVERY prompt          │")
    print("  │    → routing is automatic, transparent, zero effort         │")
    print("  │                                                              │")
    print("  │  Copilot / Cursor / Windsurf (pull):  the model DECIDES     │")
    print("  │    → tools appear in the model's tool list; it calls them   │")
    print("  │    → the Cursor rule nudges the model to call LLM Router first  │")
    print("  │    → NOT guaranteed on every turn (model may skip)          │")
    print("  │                                                              │")
    print("  │  For guaranteed routing, use Claude Code.                   │")
    print("  ╚──────────────────────────────────────────────────────────────╝")
    print()


def _print_help() -> None:
    print(localize("""
llm_router-install-hooks — Install LLM Router routing into your dev environment

USAGE
  llm_router-install-hooks               Install Claude Code hooks + IDE configs (default)
  llm_router-install-hooks uninstall     Remove Claude Code hooks
  llm_router-install-hooks ide [DIR]     Install pull-routing IDE configs into DIR (default: cwd)
  llm_router-install-hooks --help        Show this help

WHAT GETS INSTALLED

  Claude Code (push routing — automatic, every prompt):
    ~/.claude/hooks/         UserPromptSubmit + PostToolUse hooks
    ~/.claude/settings.json  MCP server registration + statusLine
    ~/.claude/rules/         Routing rules (llm_router.md)

  VS Code / GitHub Copilot (pull routing — model decides):
    .vscode/mcp.json         MCP server config for Copilot Chat agent mode
    Requires: VS Code ≥ 1.99, Copilot subscription, agent mode enabled

  Windsurf / Cascade (pull routing — model decides):
    .windsurf/mcp.json       MCP server config for Cascade agent

  Cursor (pull routing with nudge — model usually calls):
    .cursor/rules/use-llm_router.mdc  Agent rule that instructs Cursor to call
                                  llm_code / llm_query / llm_analyze first

PUSH vs PULL — THE KEY DIFFERENCE

  Push (Claude Code):  LLM Router suggests a route on every prompt BEFORE the LLM
    sees it, with no extra effort from you. In advise mode nothing is ever
    blocked — Claude keeps the final call — so how much you save depends on
    your task mix, not a guarantee.

  Pull (Copilot/Cursor/Windsurf):  The LLM sees the prompt, then DECIDES
    whether to call a LLM Router tool. The Cursor .mdc rule makes this more
    likely, but it is not guaranteed.
    → For the highest savings, use Claude Code.
    → For Cursor: the rule approximates push routing in agent mode.
    → For Copilot: explicitly invoke tools (@llm_router) or use agent mode.

SUPPORTED PROVIDERS (after keys are set)
  Ollama (free, local) · Gemini Flash · GPT-4o-mini · Claude Haiku ···

EXAMPLES
  # Install everything (Claude Code + IDE configs for this project)
  cd my-project && llm_router-install-hooks

  # Only write IDE configs (no Claude Code hooks)
  llm_router-install-hooks ide

  # Write IDE configs to a specific project
  llm_router-install-hooks ide ~/projects/my-app
""".strip()))


if __name__ == "__main__":
    main()
