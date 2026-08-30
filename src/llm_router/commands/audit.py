"""llm_router audit — post-hoc misroute audit.

Historically this module also exposed ``llm_router audit verify [--json]``
and ``llm_router audit export [--format cef|json|csv]``, thin CLI wrappers
around ``llm_router.enterprise.audit.AuditLog``'s tamper-evident hash chain.
That module is not shipped in this distribution (``llm_router.enterprise``
does not exist here), so both subcommands crashed unconditionally —
``verify`` with a raw ``TypeError: 'NoneType' object is not callable``
instead of the documented 0/1 exit contract (GH#71). They have been removed
rather than reimplemented or papered over with a friendlier error, per the
same decision that removed ``rbac_routing.py`` / ``audit_routing.py`` /
``commands/verify_enterprise.py`` / ``scim_api.py`` (GH#68/#70/#71).

``llm_router audit misroute`` is unrelated and untouched: it re-scores
already-recorded ``routing_decisions`` rows offline (see
``llm_router.misroute_audit``, a live, working, non-enterprise feature) and
has never depended on the enterprise audit log. It stays nested under
``audit`` rather than promoted to a top-level command so the CLI surface
for operators doesn't change shape.

🥷 Backslash-Security: using vibe-coding rules for Logging & Error Handling
"""
from __future__ import annotations

import argparse
import json
import sys

_USAGE = (
    "llm_router audit — post-hoc misroute audit\n"
    "\n"
    "Commands:\n"
    "  misroute [--json] [--limit N]   re-score past routing decisions offline\n"
)


def _misroute(args: argparse.Namespace) -> int:
    """Run the post-hoc misroute audit and render its report.

    A subcommand of ``audit`` rather than a command of its own: the downstream
    package made it a top-level ``audit`` command, which is precisely what
    collided with this module's (now-removed) enterprise ``verify``/``export``
    commands. Nesting it composes instead, and the shape is kept even though
    those siblings are gone, so the CLI surface doesn't change again.

    Read-only with respect to routing — it only fills in ``audit_verdict`` on
    rows that already exist. Always exits 0, including when the audit is
    disabled or the database is unreadable, because this reports on history and
    a reporting failure is not an operational failure.
    """
    import asyncio

    from llm_router.misroute_audit import run_audit

    report = asyncio.run(run_audit(limit=args.limit))

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    if report.get("disabled"):
        print("misroute audit is disabled (LLM_ROUTER_AUDIT_DISABLED)")
        return 0

    counts = report.get("verdict_counts", {})
    print(f"sampled {report['sampled']} decision(s), recorded {report['audited']} verdict(s)")
    for verdict in ("likely_misroute", "likely_correct", "insufficient_data"):
        print(f"  {verdict:20} {counts.get(verdict, 0)}")

    baseline = report.get("mis_route_rate_inferred_baseline")
    if baseline is None:
        # Distinguished from 0.0 on purpose: "no population baseline available"
        # and "the population baseline is zero" are different facts, and
        # printing 0.0 for the first would be a claim the data does not support.
        print("  population misroute-rate baseline: unavailable")
    else:
        print(f"  population misroute-rate baseline: {baseline:.1%}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="llm_router audit", description="Post-hoc misroute audit",
        usage=_USAGE,
    )
    sub = parser.add_subparsers(dest="command")

    p_misroute = sub.add_parser(
        "misroute", help="re-score past routing decisions for likely misroutes"
    )
    p_misroute.add_argument("--json", action="store_true", help="machine-readable output")
    p_misroute.add_argument(
        "--limit", type=int, default=100, help="max decisions to score (default: 100)"
    )

    args = parser.parse_args(argv)
    if args.command == "misroute":
        return _misroute(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
