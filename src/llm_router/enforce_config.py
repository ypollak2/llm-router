"""Single source of truth for the routing-enforcement mode.

Both the UserPromptSubmit banner (``auto-route.py``) and the PreToolUse enforcer
(``enforce-route.py``) resolve the mode through :func:`resolve_enforce_mode` so
they can never disagree — the banner always reflects what the enforcer will do.

Resolution priority (highest first):

  1. ``LLM_ROUTER_ENFORCE`` env var                 — ad-hoc, per-shell override
  2. ``./.llm_router.yml`` (cwd/ancestors) ``enforce:`` — per-repo policy
  3. ``~/.llm-router/routing.yaml`` ``enforce:``       — durable, cross-session default
  4. ``"smart"``                                — built-in default (block Q&A until routed, allow code/local work)

File config (2, 3) is what survives across sessions and launch methods; env
vars do NOT propagate to GUI/desktop/other-host sessions, which is why relying
on a ``~/.zshrc`` export produced inconsistent enforcement between sessions.

Modes (as understood by enforce-route.py):
  ``off``/``shadow`` observe-only · ``advise`` route-everywhere-never-block ·
  ``suggest``/``soft`` log-only · ``smart`` block Q&A / allow code ·
  ``hard`` block all work tools until routed.

YAML is parsed line-wise (no ``yaml`` import) so the hooks stay dependency-light
and fast on the critical path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Built-in default. "smart" = block Q&A/reasoning tools until the prompt is
# routed, while letting code/local-file work through — so offloadable work
# actually goes to cheaper models (the North Star) instead of Claude answering
# for free. "soft" (log-only, never blocks) saved nothing out of the box.
# Override per-repo via .llm_router.yml or globally via ~/.llm-router/routing.yaml;
# set LLM_ROUTER_ENFORCE=soft/off to relax.
DEFAULT_ENFORCE = "smart"


def _yaml_enforce(path: Path) -> str:
    """Read the ``enforce:`` scalar from a YAML file, or "" if absent/unreadable."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("enforce:"):
                value = stripped.split(":", 1)[1].strip()
                # tolerate quotes and trailing comments
                value = value.split("#", 1)[0].strip().strip("'\"")
                return value.lower()
    except OSError:
        pass
    return ""


def _repo_enforce(start: Path) -> str:
    """``enforce:`` from the nearest ``.llm_router.yml`` at or above ``start``."""
    try:
        for directory in [start, *start.parents]:
            candidate = directory / ".llm_router.yml"
            if candidate.exists():
                return _yaml_enforce(candidate)
    except OSError:
        pass
    return ""


# A session id arrives from the environment; only plain tokens may become
# a path component.
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")

def _session_enforce(home: Path) -> str:
    """Read this session's enforcement override, if it has one.

    GH#49: set-enforce wrote only the machine-global routing.yaml, which
    resolve_enforce_mode re-read on every hook call with no caching — so a
    change in one Claude Code window took effect immediately in every other
    window on the machine, while the command printed "Restart Claude Code for
    the change to take effect". Behaviour and message disagreed, and the blast
    radius was larger than either implied.

    Resolved as session-scoped. The id comes from CLAUDE_SESSION_ID, the same
    source session_spend and session_store already use.

    The id is environment-supplied, so it is treated as untrusted: anything
    that is not a plain safe token is ignored rather than being joined onto a
    path. Any read failure falls through to the next tier — enforcement
    resolution must never raise into a hook.
    """
    sid = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if not sid or not _SAFE_SESSION_ID.fullmatch(sid):
        return ""
    try:
        raw = (home / ".llm-router" / "sessions" / sid / "enforce").read_text()
    except (OSError, ValueError, UnicodeDecodeError):
        return ""
    return raw.strip().lower()


def resolve_enforce_mode(cwd: Path | None = None, home: Path | None = None) -> str:
    """Resolve the effective enforcement mode. See module docstring for priority.

    Returns a lowercase mode string; callers map it to their own display/behavior.
    Never raises — falls back to :data:`DEFAULT_ENFORCE`.
    """
    env = os.environ.get("LLM_ROUTER_ENFORCE", "").strip().lower()
    if env:
        return env

    # GH#49: below an explicit export (the strongest signal a user can give),
    # above a checked-in repo default (a deliberate in-session change should
    # beat one).
    home = home or Path.home()
    session = _session_enforce(home)
    if session:
        return session

    repo = _repo_enforce(cwd or Path.cwd())
    if repo:
        return repo

    global_cfg = _yaml_enforce(home / ".llm-router" / "routing.yaml")
    if global_cfg:
        return global_cfg

    return DEFAULT_ENFORCE
