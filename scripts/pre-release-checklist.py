#!/usr/bin/env python3
"""Pre-release checklist: Validate everything locally before GitHub CI runs.

This script prevents wasted GitHub Actions minutes by catching common release
issues in advance:
- Code linting (ruff) — auto-fixes what it can
- Version mismatches across plugin manifests
- Uncommitted changes
- Missing changelog entries
- Common code issues (debug code, secrets)
- Version-critical tests

Run this BEFORE `python scripts/release.py <version>`

Usage:
  python scripts/pre-release-checklist.py        # Run all checks
  python scripts/pre-release-checklist.py --skip-tests  # Skip expensive tests
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = ROOT / "pyproject.toml"
CHANGELOG_PATH = ROOT / "CHANGELOG.md"
PLUGIN_DIRS = (".claude-plugin", ".codex-plugin", ".factory-plugin")


class CheckResult:
    """Track results of a single check."""

    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.messages: list[str] = []

    def pass_check(self, msg: str = ""):
        """Mark check as passed."""
        self.passed = True
        if msg:
            self.messages.append(msg)

    def fail_check(self, msg: str):
        """Mark check as failed."""
        self.passed = False
        self.messages.append(msg)

    def warn(self, msg: str):
        """Add a warning (non-blocking)."""
        self.messages.append(f"⚠️  {msg}")

    def info(self, msg: str):
        """Add informational message."""
        self.messages.append(f"ℹ️  {msg}")

    def print_result(self):
        """Print result summary."""
        status = "✅" if self.passed else "❌"
        print(f"{status} {self.name}")
        for msg in self.messages:
            print(f"   {msg}")


def check_git_status() -> CheckResult:
    """Verify clean git state and correct branch."""
    result = CheckResult("Git Status")

    try:
        # Check for uncommitted changes
        status_output = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status_output.stdout.strip():
            result.fail_check("Uncommitted changes detected:")
            for line in status_output.stdout.strip().split("\n")[:5]:
                result.info(line)
            result.info("Commit or stash changes before release")
            return result

        # Check current branch
        branch_output = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        current_branch = branch_output.stdout.strip()
        if current_branch not in ("main", "master", "release"):
            result.warn(f"Not on main/master/release branch (on {current_branch})")

        # Check if ahead of origin
        ahead_output = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..origin/main"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        behind = int(ahead_output.stdout.strip())
        if behind > 0:
            result.warn(f"Branch is {behind} commits behind origin/main")

        result.pass_check(f"Clean working tree on {current_branch}")
    except subprocess.TimeoutExpired:
        result.fail_check("Git command timed out")
    except Exception as e:
        result.fail_check(f"Git check failed: {e}")

    return result


def check_version_sync() -> CheckResult:
    """Verify version consistency across all project files."""
    result = CheckResult("Version Sync")

    try:
        # Read pyproject version
        with open(PYPROJECT_PATH, "rb") as f:
            pyproject_data = tomllib.load(f)
        pyproject_version = pyproject_data["project"]["version"]
        result.info(f"pyproject.toml: {pyproject_version}")

        # Check all plugin versions
        versions_ok = True
        for plugin_dir in PLUGIN_DIRS:
            plugin_path = ROOT / plugin_dir / "plugin.json"
            marketplace_path = ROOT / plugin_dir / "marketplace.json"

            # Check plugin.json
            if plugin_path.exists():
                with open(plugin_path) as f:
                    plugin_data = json.load(f)
                plugin_version = plugin_data.get("version", "MISSING")
                if plugin_version != pyproject_version:
                    result.fail_check(f"{plugin_dir}/plugin.json: {plugin_version}")
                    versions_ok = False
                else:
                    result.info(f"{plugin_dir}/plugin.json: ✓")

            # Check marketplace.json
            if marketplace_path.exists():
                with open(marketplace_path) as f:
                    marketplace_data = json.load(f)
                marketplace_version = marketplace_data.get("version", "MISSING")
                if marketplace_version != pyproject_version:
                    result.fail_check(f"{plugin_dir}/marketplace.json (root): {marketplace_version}")
                    versions_ok = False

                # Check plugin entry in marketplace
                for plugin in marketplace_data.get("plugins", []):
                    plugin_version = plugin.get("version", "MISSING")
                    if plugin_version != pyproject_version:
                        result.fail_check(f"{plugin_dir}/marketplace.json (plugin): {plugin_version}")
                        versions_ok = False

        if versions_ok:
            result.pass_check(f"All versions match: {pyproject_version}")
        else:
            result.fail_check("Run: python scripts/sync-versions.py")

    except Exception as e:
        result.fail_check(f"Version check failed: {e}")

    return result


def check_changelog() -> CheckResult:
    """Verify changelog entry exists for current version."""
    result = CheckResult("Changelog Entry")

    try:
        with open(PYPROJECT_PATH, "rb") as f:
            version = tomllib.load(f)["project"]["version"]

        with open(CHANGELOG_PATH) as f:
            changelog = f.read()

        # Check for version entry
        pattern = rf"^## v{re.escape(version)}\b"
        if re.search(pattern, changelog, re.MULTILINE):
            result.pass_check(f"Changelog entry found for v{version}")
        else:
            result.fail_check(
                f"No changelog entry for v{version}\n"
                "   Add a section: ## v{version}\n"
                "   (Must be above any newer versions)"
            )

    except Exception as e:
        result.fail_check(f"Changelog check failed: {e}")

    return result


def check_no_debug_code() -> CheckResult:
    """Check for common debug/TODO patterns in source code."""
    result = CheckResult("Debug Code Check")

    patterns = [
        (r"\bprint\s*\(", "print() statement", "use logging instead"),
        (r"TODO[:;]", "TODO comment", "resolve before release"),
        (r"FIXME[:;]", "FIXME comment", "resolve before release"),
        (r"breakpoint\s*\(", "breakpoint()", "remove debug breakpoints"),
        (r"import pdb", "pdb import", "remove debug imports"),
        (r"\.pdb", ".pdb file", "remove debug files"),
    ]

    issues = []
    for pattern, name, action in patterns:
        try:
            result_obj = subprocess.run(
                ["grep", "-r", "-n", "-E", pattern, "src/llm_router", "--include=*.py"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result_obj.stdout:
                for line in result_obj.stdout.strip().split("\n")[:3]:
                    issues.append(f"{name} ({action}): {line}")
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass

    if issues:
        result.passed = False
        result.fail_check("Found debug code in source:")
        for issue in issues[:5]:
            result.info(issue)
        if len(issues) > 5:
            result.info(f"... and {len(issues) - 5} more")
    else:
        result.pass_check("No common debug patterns found")

    return result


def check_no_secrets() -> CheckResult:
    """Check for hardcoded secrets/API keys."""
    result = CheckResult("Secrets Check")

    patterns = [
        (r"(API_KEY|api_key)\s*=\s*['\"]([^'\"]+)['\"]", "Hardcoded API key"),
        (r"(PASSWORD|password)\s*=\s*['\"]([^'\"]+)['\"]", "Hardcoded password"),
        (r"(TOKEN|token)\s*=\s*['\"]([a-zA-Z0-9]{20,})['\"]", "Hardcoded token"),
        (r"sk_[a-z]{2,}_[a-zA-Z0-9]{24,}", "Stripe key"),
        (r"pk_[a-z]{2,}_[a-zA-Z0-9]{24,}", "Public key"),
    ]

    issues = []
    for pattern, name in patterns:
        try:
            result_obj = subprocess.run(
                ["grep", "-r", "-E", pattern, "src/llm_router", "--include=*.py"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result_obj.stdout:
                for line in result_obj.stdout.strip().split("\n")[:2]:
                    issues.append(f"{name}: {line.split(':')[0]}")
        except (subprocess.TimeoutExpired, Exception):
            pass

    if issues:
        result.fail_check("Found potential secrets in source code:")
        for issue in issues:
            result.info(issue)
        result.info("Use environment variables instead")
    else:
        result.pass_check("No obvious hardcoded secrets detected")

    return result


def check_linting() -> CheckResult:
    """Run ruff linting and report issues."""
    result = CheckResult("Code Linting (ruff)")

    try:
        # First, auto-fix what we can
        fix_result = subprocess.run(
            ["uv", "run", "ruff", "check", "src/", "tests/", "--fix"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Then check if there are remaining issues
        check_result = subprocess.run(
            ["uv", "run", "ruff", "check", "src/", "tests/"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if check_result.returncode == 0:
            result.pass_check("All linting checks passed")
            if fix_result.stdout:
                result.info("Auto-fixed issues with --fix")
        else:
            # Parse ruff output to count errors
            errors = check_result.stderr + check_result.stdout
            error_count = len([line for line in errors.split("\n") if "error" in line.lower()])

            result.fail_check(f"Linting failed with {error_count} errors")
            result.info("Run: uv run ruff check src/ tests/ --fix")
            result.info("Then fix remaining issues manually")

            # Show first few errors
            for line in errors.split("\n")[:5]:
                if line.strip() and ("error" in line.lower() or "F401" in line or "E402" in line):
                    result.info(f"  {line[:100]}")

    except FileNotFoundError:
        result.fail_check("ruff not found — install with: uv pip install ruff")
    except subprocess.TimeoutExpired:
        result.fail_check("Linting check timed out (>60s)")
    except Exception as e:
        result.fail_check(f"Linting check failed: {e}")

    return result


def check_tests_pass() -> CheckResult:
    """Run version-critical tests locally."""
    result = CheckResult("Version Tests (local)")

    try:
        # Run only the version-related tests
        test_commands = [
            (
                ["uv", "run", "python", "scripts/verify-version-sync.py"],
                "Version sync verification",
            ),
            (
                [
                    "uv",
                    "run",
                    "pytest",
                    "tests/integration/test_host_integrations.py::test_plugin_manifest_version_matches_pyproject",
                    "-v",
                ],
                "Plugin manifest version tests",
            ),
            (
                [
                    "uv",
                    "run",
                    "pytest",
                    "tests/qa/test_plugin_packaging.py::test_marketplace_plugin_versions_match_pyproject",
                    "-v",
                ],
                "Marketplace version tests",
            ),
        ]

        all_passed = True
        for cmd, description in test_commands:
            result.info(f"Running: {description}...")
            test_result = subprocess.run(
                cmd,
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if test_result.returncode == 0:
                result.info(f"  ✓ {description}")
            else:
                result.info(f"  ✗ {description}")
                all_passed = False
                if "FAILED" in test_result.stdout:
                    for line in test_result.stdout.split("\n"):
                        if "FAILED" in line or "AssertionError" in line:
                            result.info(f"    {line}")

        if all_passed:
            result.pass_check("All version tests passed")
        else:
            result.fail_check("Some tests failed (see details above)")

    except subprocess.TimeoutExpired:
        result.fail_check("Tests timed out (>120s)")
    except FileNotFoundError:
        result.fail_check("pytest or uv not found — install with: uv pip install pytest")
    except Exception as e:
        result.fail_check(f"Test check failed: {e}")

    return result


def auto_fix_versions() -> bool:
    """Automatically run sync-versions.py if version mismatches detected."""
    print("\n🔧 Attempting to auto-fix version mismatches...\n")

    try:
        sync_result = subprocess.run(
            ["uv", "run", "python", "scripts/sync-versions.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if sync_result.returncode == 0:
            print(sync_result.stdout)
            print("✅ Version sync fixed!")
            return True
        else:
            print(f"❌ Version sync failed:\n{sync_result.stderr}")
            return False

    except Exception as e:
        print(f"❌ Could not run sync-versions.py: {e}")
        return False


def main(argv: list[str] | None = None) -> int:
    """Run all pre-release checks."""
    args = argv or sys.argv[1:]
    auto_fix = "--auto-fix" in args
    skip_tests = "--skip-tests" in args

    print("\n" + "=" * 70)
    print("PRE-RELEASE CHECKLIST")
    print("=" * 70)
    print()

    checks: list[CheckResult] = [
        check_git_status(),
        check_version_sync(),
        check_changelog(),
        check_linting(),
        check_no_debug_code(),
        check_no_secrets(),
    ]

    if not skip_tests:
        checks.append(check_tests_pass())

    # Print all results
    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    print()

    all_passed = True
    for check in checks:
        check.print_result()
        print()
        if not check.passed:
            all_passed = False

    # Summary
    print("=" * 70)
    if all_passed:
        print("✅ ALL CHECKS PASSED")
        print("=" * 70)
        print()
        print("You're ready to release! Run:")
        with open(PYPROJECT_PATH, "rb") as f:
            version = tomllib.load(f)["project"]["version"]
        print(f"  python scripts/release.py {version}")
        print()
        return 0
    else:
        print("❌ SOME CHECKS FAILED")
        print("=" * 70)
        print()

        # Check if version sync is the only issue
        version_check = checks[1]  # check_version_sync
        if not version_check.passed and auto_fix:
            print("Running auto-fix for version mismatches...")
            if auto_fix_versions():
                print("\n✅ Version sync fixed! Re-run checklist to verify:")
                print("  python scripts/pre-release-checklist.py")
                return 0
        elif not version_check.passed:
            print("💡 Tip: Run with --auto-fix to automatically fix version mismatches")
            print("  python scripts/pre-release-checklist.py --auto-fix")
            print()

        return 1


if __name__ == "__main__":
    sys.exit(main())
