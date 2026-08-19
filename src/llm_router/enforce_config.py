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


def resolve_enforce_mode(cwd: Path | None = None, home: Path | None = None) -> str:
    """Resolve the effective enforcement mode. See module docstring for priority.

    Returns a lowercase mode string; callers map it to their own display/behavior.
    Never raises — falls back to :data:`DEFAULT_ENFORCE`.
    """
    env = os.environ.get("LLM_ROUTER_ENFORCE", "").strip().lower()
    if env:
        return env

    repo = _repo_enforce(cwd or Path.cwd())
    if repo:
        return repo

    home = home or Path.home()
    global_cfg = _yaml_enforce(home / ".llm-router" / "routing.yaml")
    if global_cfg:
        return global_cfg

    return DEFAULT_ENFORCE
