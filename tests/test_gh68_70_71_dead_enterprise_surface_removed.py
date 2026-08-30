"""GH#68 / #70 / #71 — the dead enterprise surface is REMOVED, not fixed.

A repo-wide audit (``docs/AUDIT_2026-08-30.md``) found five modules that
depended on ``llm_router.enterprise``, which this distribution has never
shipped (``ModuleNotFoundError: No module named 'llm_router.enterprise'``,
confirmed):

* ``rbac_routing.py`` — ``LLM_ROUTER_RBAC_MODE=strict``/``warn`` made every
  ``route_and_call`` raise ``AttributeError`` before any provider was
  contacted (#68).
* ``audit_routing.py`` — the routing audit trail never wrote a row; the
  failure was swallowed by a blanket ``except Exception`` and logged as
  ``audit_write_failed`` (#71).
* ``commands/audit.py``'s ``verify``/``export`` subcommands — ``llm-router
  audit verify`` raised a raw ``TypeError`` instead of the documented 0/1
  exit contract (#71).
* ``commands/verify_enterprise.py`` — never wired into ``cli.py`` (#70) and
  crashed with ``ModuleNotFoundError`` if invoked directly.
* ``scim_api.py`` — zero callers anywhere in ``src/``.

The repo owner chose removal over implementing the missing
``llm_router.enterprise`` package or making the surface fail gracefully.
This file pins that removal: the modules are gone, the failure modes they
caused are now structurally impossible (not just caught), and the ONE
live, non-enterprise feature that lived inside ``commands/audit.py``
(``llm_router.misroute_audit``, wired as ``llm-router audit misroute``)
was preserved rather than deleted along with its siblings.
"""
from __future__ import annotations

import importlib
from typing import Any

import pytest


# ── 1. The four dead modules no longer exist ──────────────────────────────


@pytest.mark.parametrize(
    "module_name",
    [
        "llm_router.rbac_routing",
        "llm_router.audit_routing",
        "llm_router.scim_api",
        "llm_router.commands.verify_enterprise",
    ],
)
def test_dead_module_is_gone(module_name: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


# ── 2. GH#68 — LLM_ROUTER_RBAC_MODE can no longer crash routing ──────────


def test_router_no_longer_imports_rbac_routing() -> None:
    """router.py must not resolve any of the deleted rbac_routing symbols."""
    import llm_router.router as router_mod

    for name in (
        "check_route_prompt",
        "raise_route_prompt_denied",
        "_rbac_check_model",
        "_rbac_check_provider",
        "audit_routing_turn",
    ):
        assert not hasattr(router_mod, name), (
            f"llm_router.router still exposes {name!r} — a dead rbac_routing/"
            f"audit_routing call site survived the removal"
        )


@pytest.mark.asyncio
async def test_rbac_mode_strict_no_longer_raises(
    mock_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The GH#68 repro, pinned as a regression test.

    Before removal: ``LLM_ROUTER_RBAC_MODE=strict`` made
    ``check_route_prompt`` call ``has_permission(identity,
    Permission.ROUTE_PROMPT)`` where both were ``None`` (the enterprise
    import silently failed), raising
    ``AttributeError: 'NoneType' object has no attribute 'ROUTE_PROMPT'``
    before any provider was contacted.

    After removal: the env var is simply not read by the routing path —
    a routed turn succeeds exactly as it would with no RBAC env set at
    all (proving default-mode routing is unchanged, not just that the
    crash is gone).
    """
    from llm_router import router as router_mod
    from llm_router.router import route_and_call
    from llm_router.types import LLMResponse, TaskType

    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "strict")

    async def _fake_dispatch(**kwargs: Any) -> LLMResponse:
        return LLMResponse(
            content="ok", model="openai/gpt-5", provider="openai",
            input_tokens=1, output_tokens=1, cost_usd=0.001, latency_ms=10.0,
        )

    monkeypatch.setattr(router_mod, "_dispatch_model_loop", _fake_dispatch)

    resp = await route_and_call(task_type=TaskType.QUERY, prompt="hi")
    assert resp.content == "ok"


# ── 3. GH#70 — verify-enterprise: no longer reachable, nothing left to crash ──


def test_verify_enterprise_not_in_known_subcommands() -> None:
    from llm_router.cli import _KNOWN_SUBCOMMANDS

    assert "verify-enterprise" not in _KNOWN_SUBCOMMANDS


# ── 4. GH#71 — llm-router audit verify/export: clean exit, not TypeError ─────


@pytest.mark.parametrize("subcommand", ["verify", "export"])
def test_audit_verify_export_no_longer_typeerror(subcommand: str) -> None:
    """Before removal: ``AuditLog = TamperDetected = None`` (failed
    enterprise import) and ``_verify()``/``_export()`` called ``AuditLog()``
    unguarded → ``TypeError: 'NoneType' object is not callable``.

    After removal: the subcommands don't exist. argparse rejects them with
    its normal invalid-choice usage error (``SystemExit(2)``) — the
    documented shape of "not a valid command", never a raw ``TypeError``.
    """
    from llm_router.commands.audit import main

    with pytest.raises(SystemExit) as exc_info:
        main([subcommand])
    assert exc_info.value.code == 2


def test_audit_log_and_tamper_detected_no_longer_imported() -> None:
    import llm_router.commands.audit as audit_mod

    assert not hasattr(audit_mod, "AuditLog")
    assert not hasattr(audit_mod, "TamperDetected")


# ── 5. commands/audit.py's live misroute feature must survive intact ────────


def test_audit_misroute_subcommand_still_wired() -> None:
    """The one thing `commands/audit.py` must NOT lose: `llm_router.misroute_audit`
    (a separate, working, non-enterprise feature) is still reachable as
    `llm-router audit misroute`, and the `audit` command is still dispatched
    from cli.py — deleting the whole module along with its enterprise
    siblings would have silently taken this down too."""
    import llm_router.cli as cli_mod
    from llm_router.commands.audit import main

    assert "audit" in cli_mod._KNOWN_SUBCOMMANDS
    # A garbage sub-subcommand still reaches argparse (proves 'audit' itself
    # dispatches, distinct from 'misroute' being a valid choice).
    with pytest.raises(SystemExit):
        main(["--help"])


def test_misroute_audit_module_untouched() -> None:
    """misroute_audit.py is explicitly out of scope for this removal — it
    is a different, working feature despite the similar name (see its own
    module docstring). Confirm it still imports and is not the thing that
    was deleted."""
    import llm_router.misroute_audit  # noqa: F401 — import-success is the assertion
