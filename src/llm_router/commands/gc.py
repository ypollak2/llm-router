#!/usr/bin/env python3
"""llm_router gc — TTL-sweep stale per-session shards from ~/.llm-router.

Command: uv run llm_router gc [--ttl-days N] [--apply] [--root PATH]

LLM Router's hooks write small per-session shard files (`last_route_<id>.json`,
`tool_history_<id>.json`, `turn_blocks_<id>.json`, `violations_<id>.json`,
`agent_depth_<id>.json`, `last_classification_<id>.json`, `*.bak*`) that are
never cleaned up. This command removes shards whose mtime is older than the
TTL. It is **dry-run by default**: nothing is deleted unless `--apply` is
passed.

Never touched: `usage.db*`, `admin_actions.db`, `config.*`, `.env`, or any
path outside the shard patterns below.
"""

import argparse
import sys
import time
from pathlib import Path

# Shard filename prefixes that are safe to TTL-sweep. Anything not matching
# one of these (or the *.bak* rule) is left alone.
SHARD_PREFIXES = (
    "last_route_",
    "tool_history_",
    "turn_blocks_",
    "violations_",
    "agent_depth_",
    "last_classification_",
    "transcript_",
)

DEFAULT_TTL_DAYS = 7


def _is_shard(p: Path) -> bool:
    """True if the file matches a sweepable shard pattern."""
    if not p.is_file():
        return False
    name = p.name
    if name.startswith(SHARD_PREFIXES):
        return True
    # backup litter: foo.json.bak, foo.bak2, ...
    if ".bak" in name and not name.startswith((".", "usage", "config")):
        return True
    return False


def collect_stale(root: Path, ttl_days: float) -> list[Path]:
    """Return shard files under root older than ttl_days (non-recursive)."""
    if not root.is_dir():
        return []
    cutoff = time.time() - ttl_days * 86400
    stale = []
    for p in sorted(root.iterdir()):
        if _is_shard(p):
            try:
                if p.stat().st_mtime < cutoff:
                    stale.append(p)
            except OSError:
                continue
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="llm-router gc",
        description="Sweep stale per-session shard files from ~/.llm-router (dry-run by default).",
    )
    parser.add_argument(
        "--ttl-days",
        type=float,
        default=DEFAULT_TTL_DAYS,
        help=f"delete shards older than this many days (default: {DEFAULT_TTL_DAYS})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete; without this flag gc only reports what it would remove",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.home() / ".llm-router",
        help="directory to sweep (default: ~/.llm-router)",
    )
    args = parser.parse_args(argv)

    stale = collect_stale(args.root, args.ttl_days)
    total_bytes = 0
    for p in stale:
        try:
            total_bytes += p.stat().st_size
        except OSError:
            pass

    verb = "removed" if args.apply else "would remove"
    for p in stale:
        print(f"  {verb}: {p.name}")
        if args.apply:
            try:
                p.unlink()
            except OSError as e:
                print(f"  ! failed: {p.name}: {e}", file=sys.stderr)

    kb = total_bytes / 1024
    print(
        f"llm-router gc: {verb} {len(stale)} shard(s) older than "
        f"{args.ttl_days:g}d ({kb:.1f} KiB) in {args.root}"
    )
    if not args.apply and stale:
        print("  (dry run — pass --apply to delete)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
