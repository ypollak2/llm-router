"""Fail-open seam between the server doors and the engine (PLAN v16 P1.6 / D-72).

The rules route; the engine only logs (shadow) or, under ``LLM_ROUTER_ENGINE_MODE=decide``,
decides. An engine import error or exception must never change a rules route, so the server
doors (gateway, proxy backends, router, MCP ensemble, delegate) call the engine only through
here. Each function records the failure with ``failopen`` (``LR-FO-ENGINE-DOOR``) and returns the
"use the rules" answer. The hooks do the same inline (they are standalone scripts).
"""
from __future__ import annotations

import importlib

_CODE = "LR-FO-ENGINE-DOOR"


def _record(exc: BaseException) -> None:
    try:
        from llm_router import failopen

        failopen.record(_CODE, exc)
    except Exception:  # noqa: BLE001 — accounting must never raise into a door
        pass


def _engine():
    return importlib.import_module("llm_router.engine")


def decides() -> bool:
    """True when the engine decides this route. False on any engine failure (rules route)."""
    try:
        return bool(_engine().decides())
    except Exception as exc:  # noqa: BLE001
        _record(exc)
        return False


def decision(text: str, *, door: str, proxy: bool = False):
    """The engine's Decision when it decides, else None. None also on failure: use the rules."""
    try:
        eng = _engine()
        if not eng.decides():
            return None
        return eng.decide_for_proxy(text) if proxy else eng.decide(text, door=door)
    except Exception as exc:  # noqa: BLE001
        _record(exc)
        return None


def shadow(text: str, *, door: str, rules_task: str | None, rules_complexity: str | None) -> None:
    """Log the engine's Decision beside the rules class (background). Never raises."""
    try:
        _engine().shadow(text, door=door, rules_task=rules_task,
                         rules_complexity=rules_complexity, background=True)
    except Exception as exc:  # noqa: BLE001
        _record(exc)


def bind_session(session_id: str | None):
    """Bind the session for hook-Decision reuse. Only in decide mode; shadow and off do no engine
    work (None). Returns a token for ``unbind_session``."""
    try:
        eng = _engine()
        if not eng.decides():
            return None
        return (eng, eng.bind_session(session_id))
    except Exception as exc:  # noqa: BLE001
        _record(exc)
        return None


def unbind_session(bound) -> None:
    if bound is None:
        return
    try:
        eng, token = bound
        eng.unbind_session(token)
    except Exception as exc:  # noqa: BLE001
        _record(exc)
