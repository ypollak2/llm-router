"""Refinement #9 — ``llm_router verify-enterprise`` startup verifier.

The audit's repeating pattern was *implementation lands, default
doesn't change* — RBAC strict was wired but defaulted off,
redaction was wired but defaulted off, audit was tamper-evident but
could be disabled by an env. Slice 3 + slice 12 closed those
defaults under ``LLM_ROUTER_PROFILE=enterprise``, but a deployment can
still misconfigure (typo in env name, missing token, deleted
identity DB) and silently boot in a degraded state.

This command runs ALL the checks an enterprise deployment relies on
at boot and either prints a green checklist (exit 0) or a red one
with remediation hints (exit 1). It's safe to wire into the llm_router
MCP server's startup, a Kubernetes readiness probe, or a CI gate.

Usage::

    llm_router verify-enterprise         # full enterprise check
    llm_router verify-enterprise --json  # machine-readable output
    llm_router verify-enterprise --developer
                                     # developer-profile self-check
    llm_router verify-enterprise --help

Each check returns a ``CheckResult`` with a name, a pass flag, a
short status line, and (on failure) a remediation hint. The verifier
collects all results so a partial failure still prints the green
items — operators see exactly what's working and what isn't.

Distinct from the older ``llm_router verify`` command (live provider /
hook health) — this one is about **enterprise-profile invariants**
and runs in tens of milliseconds, suitable for liveness probes.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Callable


_USAGE = (
    "llm_router verify-enterprise — pre-flight enterprise verification\n"
    "\n"
    "Options:\n"
    "  --developer    run the developer-profile self-check instead\n"
    "                 of the enterprise one\n"
    "  --json         machine-readable output for tooling\n"
    "  --help, -h     show this message and exit\n"
    "\n"
    "Exit codes:\n"
    "  0   all checks passed\n"
    "  1   at least one check failed\n"
    "  2   invalid CLI arguments\n"
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    status: str
    remediation: str = ""


@dataclass
class VerifyReport:
    profile: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "all_passed": self.all_passed,
            "checks": [
                {
                    "name": r.name, "passed": r.passed,
                    "status": r.status, "remediation": r.remediation,
                }
                for r in self.results
            ],
        }


# ────────────────────────────────────────────────────────────────────────
# Individual checks. Pure functions of os.environ + filesystem state
# so they can be exercised individually from unit tests.
# ────────────────────────────────────────────────────────────────────────


def _check_profile_set_to_enterprise() -> CheckResult:
    from llm_router.profile import is_enterprise
    if is_enterprise():
        return CheckResult(
            name="profile_enterprise", passed=True,
            status="LLM_ROUTER_PROFILE=enterprise (or alias)",
        )
    return CheckResult(
        name="profile_enterprise", passed=False,
        status=f"LLM_ROUTER_PROFILE={os.environ.get('LLM_ROUTER_PROFILE', '<unset>')!r}",
        remediation="export LLM_ROUTER_PROFILE=enterprise",
    )


def _check_token_present() -> CheckResult:
    """G-002 requires ``LLM_ROUTER_TOKEN`` under enterprise profile."""
    token = (os.environ.get("LLM_ROUTER_TOKEN") or "").strip()
    if token:
        return CheckResult(
            name="token_present", passed=True,
            status=f"LLM_ROUTER_TOKEN set ({len(token)} chars)",
        )
    return CheckResult(
        name="token_present", passed=False,
        status="LLM_ROUTER_TOKEN unset or empty",
        remediation=(
            "Issue a token via POST /v1/admin/users/{user_id}/tokens "
            "and export LLM_ROUTER_TOKEN=<plaintext>"
        ),
    )


def _check_token_authenticates() -> CheckResult:
    """Slice 12 wires Tier-3 token auth as the runtime identity
    path; the token must authenticate AND carry ROUTE_PROMPT."""
    from llm_router.identity import (
        EnterpriseIdentityRequired,
        _enterprise_identity,
    )
    try:
        identity = _enterprise_identity()
    except EnterpriseIdentityRequired as exc:
        return CheckResult(
            name="token_authenticates", passed=False,
            status=str(exc).splitlines()[0],
            remediation=(
                "Confirm the token is valid and the role grants "
                "Permission.ROUTE_PROMPT"
            ),
        )
    return CheckResult(
        name="token_authenticates", passed=True,
        status=f"identity {identity.user_email!r} authenticated",
    )


def _check_rbac_strict() -> CheckResult:
    """G-001 — under enterprise the RBAC default is strict. If the
    operator explicitly opted into warn or off, surface it."""
    from llm_router.rbac_routing import _resolve_mode
    mode = _resolve_mode()
    if mode == "strict":
        return CheckResult(
            name="rbac_strict", passed=True,
            status="RBAC mode = strict",
        )
    return CheckResult(
        name="rbac_strict", passed=False,
        status=f"RBAC mode = {mode!r} (expected strict)",
        remediation=(
            "unset LLM_ROUTER_RBAC_MODE (let enterprise default apply) "
            "or set LLM_ROUTER_RBAC_MODE=strict"
        ),
    )


def _check_audit_not_disabled() -> CheckResult:
    """G-003 — enterprise refuses LLM_ROUTER_AUDIT_DISABLED. This check
    surfaces the env value to the operator even though the runtime
    silently ignores it."""
    from llm_router.audit_routing import _audit_disabled
    if not _audit_disabled():
        env = os.environ.get("LLM_ROUTER_AUDIT_DISABLED", "")
        suffix = (
            f" (LLM_ROUTER_AUDIT_DISABLED={env!r} ignored under enterprise)"
            if env else ""
        )
        return CheckResult(
            name="audit_active", passed=True,
            status=f"audit active{suffix}",
        )
    # _audit_disabled() returns False under enterprise regardless of
    # env, so reaching this branch means we're NOT under enterprise.
    return CheckResult(
        name="audit_active", passed=False,
        status="audit disabled via LLM_ROUTER_AUDIT_DISABLED",
        remediation="unset LLM_ROUTER_AUDIT_DISABLED",
    )


def _check_redaction_on() -> CheckResult:
    """G-012 — enterprise defaults redaction on."""
    from llm_router.redaction_routing import _redaction_enabled
    if _redaction_enabled():
        return CheckResult(
            name="redaction_on", passed=True,
            status="redaction enabled",
        )
    return CheckResult(
        name="redaction_on", passed=False,
        status="redaction disabled",
        remediation=(
            "unset LLM_ROUTER_REDACTION (let enterprise default apply) "
            "or set LLM_ROUTER_REDACTION=on"
        ),
    )


def _writable_db_check(
    name: str,
    *,
    open_fn: Callable[[], object],
    remediation: str,
) -> CheckResult:
    """Generic helper: try to open the DB; flag a clear error if it
    can't even be created. The probe then runs a no-op ``SELECT 1``
    to confirm we can actually round-trip a query."""
    try:
        store = open_fn()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name=name, passed=False,
            status=f"open failed: {exc}",
            remediation=remediation,
        )
    conn = getattr(store, "_conn", None)
    try:
        if conn is not None:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name=name, passed=False,
            status=f"probe failed: {exc}",
            remediation=remediation,
        )
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    return CheckResult(
        name=name, passed=True, status="reachable",
    )


def _check_identity_db_reachable() -> CheckResult:
    from llm_router.enterprise.identity import IdentityStore
    return _writable_db_check(
        "identity_db",
        open_fn=lambda: IdentityStore(check_same_thread=False),
        remediation=(
            "Ensure ~/.llm-router/identity.db (or LLM_ROUTER_IDENTITY_PATH) "
            "is writable"
        ),
    )


def _check_audit_db_reachable() -> CheckResult:
    from llm_router.enterprise.audit import AuditLog
    return _writable_db_check(
        "audit_db",
        open_fn=lambda: AuditLog(check_same_thread=False),
        remediation=(
            "Ensure ~/.llm-router/audit.db (or LLM_ROUTER_AUDIT_PATH) is "
            "writable"
        ),
    )


def _check_audit_chain_intact() -> CheckResult:
    """Walk the audit hash chain and confirm no row was tampered with. This is
    the integrity counterpart to _check_audit_not_disabled (which only checks
    the log is switched on, not that its history is unbroken)."""
    from llm_router.enterprise.audit import AuditLog, TamperDetected
    try:
        log = AuditLog(check_same_thread=False)
        rows = log.count()
        log.verify_chain()
        return CheckResult(
            name="audit_chain_intact", passed=True,
            status=f"{rows} events, hash chain verified",
        )
    except TamperDetected as exc:
        return CheckResult(
            name="audit_chain_intact", passed=False, status=str(exc),
            remediation=(
                "Audit log tampered outside the API — investigate the breach "
                "and restore audit.db from a trusted backup"
            ),
        )
    except Exception as exc:  # DB unreadable / corrupt
        return CheckResult(
            name="audit_chain_intact", passed=False,
            status=f"could not verify chain: {exc}",
            remediation="Ensure ~/.llm-router/audit.db (or LLM_ROUTER_AUDIT_PATH) is readable",
        )


def _check_admin_actions_db_reachable() -> CheckResult:
    from llm_router.admin_actions import AdminActionLog
    return _writable_db_check(
        "admin_actions_db",
        open_fn=lambda: AdminActionLog(check_same_thread=False),
        remediation=(
            "Ensure ~/.llm-router/admin_actions.db (or "
            "LLM_ROUTER_ADMIN_ACTIONS_PATH) is writable"
        ),
    )


def _check_policy_store_reachable() -> CheckResult:
    from llm_router.policy_versions import PolicyVersionStore
    return _writable_db_check(
        "policy_versions_db",
        open_fn=lambda: PolicyVersionStore(check_same_thread=False),
        remediation=(
            "Ensure ~/.llm-router/policy_versions.db (or "
            "LLM_ROUTER_POLICY_STORE_PATH) is writable"
        ),
    )


# ────────────────────────────────────────────────────────────────────────
# Verifier orchestration
# ────────────────────────────────────────────────────────────────────────


ENTERPRISE_CHECKS: tuple[Callable[[], CheckResult], ...] = (
    _check_profile_set_to_enterprise,
    _check_token_present,
    _check_token_authenticates,
    _check_rbac_strict,
    _check_audit_not_disabled,
    _check_redaction_on,
    _check_identity_db_reachable,
    _check_audit_db_reachable,
    _check_audit_chain_intact,
    _check_admin_actions_db_reachable,
    _check_policy_store_reachable,
)


# Developer profile: only the DB-reachability checks make sense.
# RBAC + audit-disable + redaction defaults are intentionally
# permissive under developer.
DEVELOPER_CHECKS: tuple[Callable[[], CheckResult], ...] = (
    _check_identity_db_reachable,
    _check_audit_db_reachable,
    _check_admin_actions_db_reachable,
    _check_policy_store_reachable,
)


def run_verifier(*, enterprise: bool) -> VerifyReport:
    """Run the appropriate check list and return a structured report.
    Pure function — does not print or exit."""
    profile_label = "enterprise" if enterprise else "developer"
    checks = ENTERPRISE_CHECKS if enterprise else DEVELOPER_CHECKS
    results = [check() for check in checks]
    return VerifyReport(profile=profile_label, results=results)


def _print_report(report: VerifyReport) -> None:
    print(f"llm_router verify-enterprise — profile: {report.profile}")
    print()
    for r in report.results:
        mark = "✓" if r.passed else "✗"
        print(f"  {mark} {r.name:<24} {r.status}")
        if not r.passed and r.remediation:
            print(f"      → {r.remediation}")
    print()
    if report.all_passed:
        print("All checks passed.")
    else:
        failed = sum(1 for r in report.results if not r.passed)
        total = len(report.results)
        print(f"{failed} of {total} checks failed.")


def cmd_verify_enterprise(args: list[str]) -> int:
    """Execute: ``llm_router verify-enterprise [--developer] [--json]``.

    Returns ``0`` if all checks pass, ``1`` if any fail, ``2`` for
    invalid CLI input. Default is the enterprise check list; pass
    ``--developer`` to run the lighter dev-profile self-check.
    """
    enterprise = True
    as_json = False
    for flag in args:
        if flag in ("--help", "-h"):
            print(_USAGE)
            return 0
        if flag == "--developer":
            enterprise = False
        elif flag == "--enterprise":
            enterprise = True   # explicit even though it's the default
        elif flag == "--json":
            as_json = True
        else:
            print(f"Unknown flag: {flag!r}", file=sys.stderr)
            print(_USAGE, file=sys.stderr)
            return 2

    report = run_verifier(enterprise=enterprise)
    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_report(report)
    return 0 if report.all_passed else 1
