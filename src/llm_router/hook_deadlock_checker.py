"""CLI interface for hook deadlock detection.

Provides a command-line tool to check for deadlock issues in hooks.
Used by CI/CD and startup verification.
"""

from __future__ import annotations

import sys
from pathlib import Path

from llm_router.hook_deadlock_detector import HookDeadlockDetector


def check_all_hooks(verbose: bool = False) -> int:
    """Check all hooks for deadlock issues.
    
    Returns:
        0 if no critical issues, 1 if issues found
    """
    hooks_dir = Path.home() / ".claude" / "hooks"
    
    if not hooks_dir.exists():
        print("[llm-router] Hooks directory not found, skipping deadlock check")
        return 0
    
    detector = HookDeadlockDetector(hooks_dir)
    report = detector.analyze()
    
    if verbose or report.has_cycles or report.has_timeout_issues:
        print(detector.format_report(report))
        print()
    
    if report.has_cycles or report.has_timeout_issues:
        return 1
    
    return 0


def main() -> int:
    """Main entry point."""
    return check_all_hooks(verbose=True)


if __name__ == "__main__":
    sys.exit(main())
