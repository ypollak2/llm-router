#!/usr/bin/env python3
"""Cleanup hook health registry — remove test artifacts and stale entries.

This script cleans up ~/.llm-router/hook_health.json by:
  1. Identifying hooks with only test-session errors (session_id: "abc123" pattern)
  2. Supporting --dry-run to preview changes
  3. Supporting --remove hook-name to force-remove specific hooks
  4. Writing cleaned hook_health.json and printing a summary

Usage:
    python3 cleanup-hook-health.py --dry-run
    python3 cleanup-hook-health.py
    python3 cleanup-hook-health.py --remove hook-a hook-b test-hook
    python3 cleanup-hook-health.py --dry-run --remove hook-a test-hook
"""

import json
import sys
from pathlib import Path
from typing import Optional


def load_hook_health() -> Optional[dict]:
    """Load hook_health.json from ~/.llm-router/. Return None if not found."""
    path = Path.home() / ".llm-router" / "hook_health.json"
    if not path.exists():
        print(f"❌ {path} not found", file=sys.stderr)
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"❌ Failed to parse {path}: {e}", file=sys.stderr)
        return None


def load_hook_errors() -> Optional[dict]:
    """Load hook_errors.log from ~/.llm-router/. Return empty dict if not found."""
    path = Path.home() / ".llm-router" / "hook_errors.log"
    if not path.exists():
        return {}
    try:
        # Parse line-by-line JSON entries
        errors = {}
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                hook_name = entry.get("hook_name", "unknown")
                if hook_name not in errors:
                    errors[hook_name] = []
                errors[hook_name].append(entry)
            except json.JSONDecodeError:
                continue
        return errors
    except Exception as e:
        print(f"⚠️  Warning: Could not read hook_errors.log: {e}", file=sys.stderr)
        return {}


def identify_test_hooks(health: dict, errors: dict) -> set[str]:
    """Identify hooks where ALL errors came from test sessions (session_id: 'abc123')."""
    test_session_id = "abc123"
    test_hooks = set()
    
    for hook_name in health.keys():
        hook_errors = errors.get(hook_name, [])
        
        # If no errors recorded, check health.json for error indication
        # (health.json might have error counts but no corresponding errors.log entries)
        if not hook_errors:
            # Hook has no recorded errors — check if it's in the known test list
            # These are the test hooks mentioned in the plan
            if hook_name in {"test-hook", "hook-a", "hook-b"}:
                # Likely test artifacts if they're in the known test list
                test_hooks.add(hook_name)
            continue
        
        # If ALL errors are from test sessions, mark for removal
        all_test_errors = all(
            entry.get("session_id") == test_session_id
            for entry in hook_errors
        )
        if all_test_errors:
            test_hooks.add(hook_name)
    
    return test_hooks


def cleanup_hook_health(
    health: dict,
    test_hooks: set[str],
    force_remove: Optional[set[str]] = None,
) -> dict:
    """Remove test hooks and forced-remove hooks from health dict."""
    result = dict(health)
    hooks_to_remove = test_hooks | (force_remove or set())
    
    for hook_name in hooks_to_remove:
        if hook_name in result:
            del result[hook_name]
    
    return result


def save_hook_health(health: dict) -> bool:
    """Write cleaned hook_health.json back to disk."""
    path = Path.home() / ".llm-router" / "hook_health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(health, indent=2))
        return True
    except Exception as e:
        print(f"❌ Failed to write {path}: {e}", file=sys.stderr)
        return False


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Cleanup hook health registry — remove test artifacts",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing",
    )
    parser.add_argument(
        "--remove",
        nargs="+",
        help="Force-remove specific hooks (in addition to auto-detected test hooks)",
    )
    
    args = parser.parse_args()
    
    # Load current state
    health = load_hook_health()
    if health is None:
        sys.exit(1)
    
    errors = load_hook_errors()
    
    # Identify test hooks
    test_hooks = identify_test_hooks(health, errors)
    force_remove = set(args.remove) if args.remove else None
    hooks_to_remove = test_hooks | (force_remove or set())
    
    # Calculate cleaning impact
    original_count = len(health)
    new_count = original_count - len(hooks_to_remove)
    
    print(f"\n📊 Hook Health Cleanup Report")
    print(f"{'─' * 50}")
    print(f"Current hooks:     {original_count}")
    print(f"To remove:         {len(hooks_to_remove)}")
    print(f"After cleanup:     {new_count}")
    print(f"Dry run:           {'Yes' if args.dry_run else 'No'}")
    
    if hooks_to_remove:
        print(f"\n🗑️  Hooks to remove:")
        for hook_name in sorted(hooks_to_remove):
            reason = []
            if hook_name in test_hooks:
                reason.append("test-session errors only")
            if force_remove and hook_name in force_remove:
                reason.append("force-removed")
            print(f"  • {hook_name:20} ({', '.join(reason)})")
    else:
        print(f"\n✅ No hooks to remove — hook_health.json is clean!")
    
    if hooks_to_remove and args.dry_run:
        print(f"\n✨ Dry run complete. Run without --dry-run to apply changes.")
        return
    
    if hooks_to_remove and not args.dry_run:
        cleaned = cleanup_hook_health(health, test_hooks, force_remove)
        if save_hook_health(cleaned):
            print(f"\n✅ Cleaned hook_health.json successfully!")
            print(f"   Removed {len(hooks_to_remove)} hooks, {new_count} remaining")
        else:
            sys.exit(1)


if __name__ == "__main__":
    main()
