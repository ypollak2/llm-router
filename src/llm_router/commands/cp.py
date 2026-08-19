"""llm_router cp — control-plane operations.

    llm_router cp bootstrap-tenant --tenant-id T [--policy-path P] [--force] [--store-path DB]

Seeds a tenant's control-plane policy from the instance's current local policy
(day-one migration, #44/#59). Idempotent; changes nothing at runtime until a
sidecar is enabled.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def cmd_cp(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="llm_router cp", description="Control-plane operations")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("bootstrap-tenant", help="seed a tenant's policy from the local policy")
    p.add_argument("--tenant-id", required=True)
    p.add_argument("--policy-path", default="", help="local policy YAML (default: LLM_ROUTER_POLICY_PATH or ~/.llm-router/org-policy.yaml)")
    p.add_argument("--store-path", default="", help="control-plane store DB (default: LLM_ROUTER_CP_STORE_PATH or ~/.llm-router/cp_store.db)")
    p.add_argument("--force", action="store_true", help="append a new version if a different policy already exists")
    args = parser.parse_args(argv)

    if args.command != "bootstrap-tenant":
        parser.print_help()
        return 2

    from llm_router.control_plane.migration import bootstrap_tenant
    from llm_router.control_plane.store import SqliteControlPlaneStore

    store_path = args.store_path or os.environ.get("LLM_ROUTER_CP_STORE_PATH") or str(
        Path.home() / ".llm-router" / "cp_store.db"
    )
    store = SqliteControlPlaneStore(store_path)
    try:
        result = bootstrap_tenant(
            store, tenant_id=args.tenant_id,
            policy_path=args.policy_path or None, force=args.force,
        )
    except ValueError as exc:
        print(f"[llm_router cp] {exc}", file=sys.stderr)
        store.close()
        return 1
    finally:
        pass
    store.close()
    print(
        f"tenant {result['tenant_id']}: policy v{result['version']} "
        f"({result['action']}) digest={result['digest'][:12]}"
    )
    return 0
