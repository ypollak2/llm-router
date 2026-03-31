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
import re
import shutil
from pathlib import Path


# Where bundled hook scripts and rules live inside the package
_PACKAGE_DIR = Path(__file__).resolve().parent
_HOOKS_SRC = _PACKAGE_DIR / "hooks"
_RULES_SRC = _PACKAGE_DIR / "rules"

# Global Claude Code directories
_CLAUDE_DIR = Path.home() / ".claude"
_HOOKS_DST = _CLAUDE_DIR / "hooks"
_RULES_DST = _CLAUDE_DIR / "rules"
_SETTINGS_PATH = _CLAUDE_DIR / "settings.json"

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


def check_and_update_hooks() -> list[str]:
    """Re-copy bundled hooks to ~/.claude/hooks/ if the installed versions are stale.

    Returns a list of human-readable update messages (one per updated hook).
    Called automatically on MCP server startup so existing users get hook updates
    after ``pip install --upgrade claude-code-llm-router`` without re-running install.
    Only overwrites hooks that were originally installed by llm-router (identified
    by the ``# llm-router-hook-version:`` marker in the file).
    """
    updates: list[str] = []
    for src_name, dst_name, _event, _matcher in _HOOK_DEFS:
        src = _HOOKS_SRC / src_name
        dst = _HOOKS_DST / dst_name
        if not src.exists() or not dst.exists():
            continue
        src_v = _hook_version(src)
        dst_v = _hook_version(dst)
        if src_v <= dst_v:
            continue
        try:
            shutil.copy2(src, dst)
            updates.append(f"Updated {dst_name} v{dst_v} → v{src_v}")
        except OSError as e:
            updates.append(f"Failed to update {dst_name}: {e}")
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
    ("agent-route.py", "llm-router-agent-route.py", "PreToolUse", "Agent"),
    ("usage-refresh.py", "llm-router-usage-refresh.py", "PostToolUse", "llm_"),
    ("session-end.py", "llm-router-session-end.py", "Stop", ""),
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


def _register_hook(settings: dict, event: str, matcher: str, command: str) -> bool:
    """Add a hook to settings if not already present. Returns True if added."""
    hooks = settings.setdefault("hooks", {})
    event_hooks = hooks.setdefault(event, [])

    # Check if already registered
    for entry in event_hooks:
        if not isinstance(entry, dict):
            continue
        for h in entry.get("hooks", []):
            if h.get("command", "") == command:
                return False

    event_hooks.append({
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command}],
    })
    return True


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
        dst.chmod(0o755)
        actions.append(f"Copied {src_name} → {dst}")

        command = f"python3 {dst}"
        if _register_hook(settings, event, matcher, command):
            actions.append(f"Registered {event} hook: {dst_name}")
        else:
            actions.append(f"Hook already registered: {dst_name}")

    _save_settings(settings)

    # ── Copy routing rules ───────────────────────────────────────────────
    _RULES_DST.mkdir(parents=True, exist_ok=True)

    rules_src = _RULES_SRC / "llm-router.md"
    rules_dst = _RULES_DST / "llm-router.md"

    if rules_src.exists():
        shutil.copy2(rules_src, rules_dst)
        actions.append(f"Installed routing rules → {rules_dst}")
    else:
        actions.append(f"SKIP rules: source not found at {rules_src}")

    return actions


def uninstall() -> list[str]:
    """Remove hooks and rules. Returns list of actions taken."""
    actions: list[str] = []
    settings = _load_settings()

    # Remove hook files and settings entries
    for _, dst_name, event, _ in _HOOK_DEFS:
        dst = _HOOKS_DST / dst_name
        command = f"python3 {dst}"

        if dst.exists():
            dst.unlink()
            actions.append(f"Removed {dst}")

        # Remove from settings
        hooks = settings.get("hooks", {})
        event_hooks = hooks.get(event, [])
        filtered = [
            entry for entry in event_hooks
            if not any(
                h.get("command", "") == command
                for h in entry.get("hooks", [])
            )
        ]
        if len(filtered) < len(event_hooks):
            hooks[event] = filtered
            actions.append(f"Unregistered {event} hook: {dst_name}")

    _save_settings(settings)

    # Remove rules
    rules_dst = _RULES_DST / "llm-router.md"
    if rules_dst.exists():
        rules_dst.unlink()
        actions.append(f"Removed {rules_dst}")

    return actions


def main() -> None:
    """CLI entry point for llm-router-install-hooks."""
    import sys

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
