"""Loop-5 follow-up — critical-module startup check.

The G-034 / OP-1 failure mode broke routing repeatedly during the
audit + Loop-5 sessions: a stale-installed runtime is missing a
module that exists in source (most often
``llm_router.classification_allowlist``), and the MCP server boots
into a state where every routed call fails mid-flight with a
confusing transport-layer error.

The smoke gate (``scripts/ci_install_smoke_test.sh``) prevents a
broken sdist from reaching PyPI. The check exercised here is the
boot-time second layer — it catches a runtime that drifted AFTER
install.

Tests cover:

* Happy path — every critical module imports cleanly → returns
  silently.
* Failure path — a missing module → SystemExit(1) with the
  remediation message on stderr.
* Failure path lists the failing module(s) so the operator knows
  exactly what's missing.
* The bypass env (``LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK=on``) skips
  the check and logs a loud warning.
* The check runs in BOTH developer and enterprise profile (unlike
  ``_startup_verify_or_die`` which is enterprise-only).
* The check runs BEFORE ``_startup_verify_or_die`` in both ``main``
  and ``main_sse_secured`` entry points (so a stale install gets a
  clean remediation message rather than a confusing one from
  inside the enterprise verifier).
"""
from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest

import llm_router.server as server


# ── 1. Happy path ────────────────────────────────────────────────────────


def test_critical_modules_or_die_silent_on_healthy_install(
    capsys, monkeypatch,
) -> None:
    """When every critical module imports, the check returns without
    writing anything to stderr and without raising. Pin so a future
    refactor doesn't accidentally add a chatty success log."""
    monkeypatch.delenv("LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK", raising=False)
    server._critical_modules_or_die()  # no raise
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


# ── 2. Failure path ──────────────────────────────────────────────────────


def test_critical_modules_or_die_exits_when_module_missing(
    capsys, monkeypatch,
) -> None:
    """A missing module → SystemExit(1). The exit code matters for
    process managers (systemd, supervisord) that retry on non-zero."""
    monkeypatch.delenv("LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK", raising=False)
    original = importlib.import_module

    def stub(name: str, *args, **kwargs):
        if name == "llm_router.classification_allowlist":
            raise ModuleNotFoundError(
                "No module named 'llm_router.classification_allowlist'"
            )
        return original(name, *args, **kwargs)

    with patch.object(importlib, "import_module", side_effect=stub):
        with pytest.raises(SystemExit) as exc_info:
            server._critical_modules_or_die()
    assert exc_info.value.code == 1


def test_failure_message_names_the_failing_module(
    capsys, monkeypatch,
) -> None:
    """The stderr block must list the exact module that's missing so
    operators can grep for it. Pinning so a future "log a generic
    failed-startup line" refactor can't drop the actionable detail."""
    monkeypatch.delenv("LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK", raising=False)
    original = importlib.import_module

    def stub(name: str, *args, **kwargs):
        if name == "llm_router.classification_allowlist":
            raise ModuleNotFoundError(
                "No module named 'llm_router.classification_allowlist'"
            )
        return original(name, *args, **kwargs)

    with patch.object(importlib, "import_module", side_effect=stub):
        with pytest.raises(SystemExit):
            server._critical_modules_or_die()
    err = capsys.readouterr().err
    assert "llm_router.classification_allowlist" in err
    assert "ModuleNotFoundError" in err


def test_failure_message_includes_remediation_steps(
    capsys, monkeypatch,
) -> None:
    """The stderr block must point the operator at the fix
    (``uv tool install --reinstall --editable``). Pinning the
    specific command so a future copy-edit doesn't water it down
    into a generic "check your install" message."""
    monkeypatch.delenv("LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK", raising=False)
    original = importlib.import_module

    def stub(name: str, *args, **kwargs):
        if name == "llm_router.classification_allowlist":
            raise ModuleNotFoundError("missing")
        return original(name, *args, **kwargs)

    with patch.object(importlib, "import_module", side_effect=stub):
        with pytest.raises(SystemExit):
            server._critical_modules_or_die()
    err = capsys.readouterr().err
    assert "uv tool install --reinstall --editable" in err
    assert "llm_router doctor" in err  # the OP-4 transport probe
    assert "Restart the MCP server" in err


def test_failure_path_accumulates_multiple_missing_modules(
    capsys, monkeypatch,
) -> None:
    """If TWO modules are missing, the report lists BOTH — don't
    short-circuit on the first failure. Operators fixing a broken
    install want the full picture in one pass."""
    monkeypatch.delenv("LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK", raising=False)
    original = importlib.import_module
    missing = {
        "llm_router.classification_allowlist",
        "llm_router.invoice_reconciliation",
    }

    def stub(name: str, *args, **kwargs):
        if name in missing:
            raise ModuleNotFoundError(f"missing: {name}")
        return original(name, *args, **kwargs)

    with patch.object(importlib, "import_module", side_effect=stub):
        with pytest.raises(SystemExit):
            server._critical_modules_or_die()
    err = capsys.readouterr().err
    for name in missing:
        assert name in err, f"{name} should appear in stderr report"


def test_failure_path_handles_non_importerror_exceptions(
    capsys, monkeypatch,
) -> None:
    """If a module raises ``SyntaxError`` at import (corrupt file),
    we still exit with the same remediation message. The
    classification gap (ImportError vs SyntaxError vs RuntimeError)
    doesn't matter — the fix is the same."""
    monkeypatch.delenv("LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK", raising=False)
    original = importlib.import_module

    def stub(name: str, *args, **kwargs):
        if name == "llm_router.admin_api":
            raise SyntaxError("invalid syntax in installed admin_api.py")
        return original(name, *args, **kwargs)

    with patch.object(importlib, "import_module", side_effect=stub):
        with pytest.raises(SystemExit):
            server._critical_modules_or_die()
    err = capsys.readouterr().err
    assert "SyntaxError" in err
    assert "llm_router.admin_api" in err


# ── 3. Bypass env ────────────────────────────────────────────────────────


def test_bypass_env_skips_check(capsys, monkeypatch) -> None:
    """``LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK=on`` returns without
    importing anything. Test by stubbing a failure that would
    normally exit — under the bypass it doesn't fire."""
    monkeypatch.setenv("LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK", "on")
    original = importlib.import_module

    def stub(name: str, *args, **kwargs):
        if name in server._CRITICAL_MODULES:
            raise ModuleNotFoundError("would have failed")
        return original(name, *args, **kwargs)

    with patch.object(importlib, "import_module", side_effect=stub):
        server._critical_modules_or_die()  # no raise
    err = capsys.readouterr().err
    # Loud warning so the bypass can never be silent.
    assert "LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK=on" in err
    assert "may boot" in err  # the warning body


@pytest.mark.parametrize("value", ["on", "1", "true", "yes", "ON", "TRUE"])
def test_bypass_env_accepts_canonical_truthy_values(
    monkeypatch, value: str,
) -> None:
    """Match the set used by ``_startup_verify_or_die``'s bypass for
    consistency — operators shouldn't have to remember a different
    truthy spelling per env knob."""
    monkeypatch.setenv("LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK", value)
    original = importlib.import_module

    def stub(name: str, *args, **kwargs):
        if name in server._CRITICAL_MODULES:
            raise ModuleNotFoundError("would have failed")
        return original(name, *args, **kwargs)

    with patch.object(importlib, "import_module", side_effect=stub):
        server._critical_modules_or_die()  # no raise


def test_bypass_env_rejects_falsy_and_garbage(monkeypatch) -> None:
    """Empty / ``off`` / typo values do NOT bypass — the check runs.
    Pin so a future "let's accept ``no`` and ``false`` as bypass too"
    refactor is visible (would invert the safety default)."""
    for value in ("", "off", "no", "false", "ye", "yess"):
        monkeypatch.setenv("LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK", value)
        original = importlib.import_module

        def stub(name: str, *args, **kwargs):
            if name == "llm_router.classification_allowlist":
                raise ModuleNotFoundError("nope")
            return original(name, *args, **kwargs)

        with patch.object(importlib, "import_module", side_effect=stub):
            with pytest.raises(SystemExit):
                server._critical_modules_or_die()


# ── 4. Profile independence (developer + enterprise) ────────────────────


def test_check_runs_under_developer_profile(monkeypatch) -> None:
    """Unlike ``_startup_verify_or_die`` (enterprise-only), this
    check fires under developer profile too. Pin the symmetry so a
    future "only enterprise needs this" refactor reverts the OP-1
    safety net for developer installs."""
    monkeypatch.delenv("LLM_ROUTER_DEPLOYMENT_PROFILE", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROFILE", raising=False)
    monkeypatch.delenv("LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK", raising=False)
    original = importlib.import_module

    def stub(name: str, *args, **kwargs):
        if name == "llm_router.classification_allowlist":
            raise ModuleNotFoundError("nope")
        return original(name, *args, **kwargs)

    with patch.object(importlib, "import_module", side_effect=stub):
        with pytest.raises(SystemExit):
            server._critical_modules_or_die()


def test_check_runs_under_enterprise_profile(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_DEPLOYMENT_PROFILE", "enterprise")
    monkeypatch.delenv("LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK", raising=False)
    original = importlib.import_module

    def stub(name: str, *args, **kwargs):
        if name == "llm_router.classification_allowlist":
            raise ModuleNotFoundError("nope")
        return original(name, *args, **kwargs)

    with patch.object(importlib, "import_module", side_effect=stub):
        with pytest.raises(SystemExit):
            server._critical_modules_or_die()


# ── 5. Wiring in entry points ────────────────────────────────────────────


def test_main_runs_critical_check_before_enterprise_verifier(
    monkeypatch,
) -> None:
    """Pin ordering: critical check FIRST. If the enterprise
    verifier ran first and itself failed to import a missing
    module, the operator would see a confusing inner-traceback
    rather than the clean remediation message this check provides."""
    calls: list[str] = []

    def fake_critical():
        calls.append("critical")

    def fake_verify():
        calls.append("verify")

    fake_mcp = type("M", (), {"run": lambda self: calls.append("run")})()

    monkeypatch.setattr(server, "_critical_modules_or_die", fake_critical)
    monkeypatch.setattr(server, "_startup_verify_or_die", fake_verify)
    monkeypatch.setattr(server, "mcp", fake_mcp)

    server.main()
    assert calls == ["critical", "verify", "run"]


def test_critical_modules_list_includes_known_canaries() -> None:
    """The list of critical modules is a contract — pin the
    canonical canaries so a future refactor can't silently drop
    them. Specifically: ``llm_router.classification_allowlist`` (the
    exact module that broke routing during the audit + Loop-5) and
    ``llm_router.agents.session`` (the budget-agent runtime that the
    ``agents/`` sdist exclude was silently stripping)."""
    assert "llm_router.classification_allowlist" in server._CRITICAL_MODULES
    assert "llm_router.agents.session" in server._CRITICAL_MODULES
    assert "llm_router.admin_api" in server._CRITICAL_MODULES
    assert "llm_router.invoice_reconciliation" in server._CRITICAL_MODULES
