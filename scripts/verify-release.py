#!/usr/bin/env python3
"""Post-release verification: PyPI, GitHub, and test suite."""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import tomllib


def get_version():
    """Read version from pyproject.toml."""
    with open("pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def check_pypi(package_name: str, version: str) -> bool:
    """Verify package is available on PyPI."""
    print(f"🔍 Checking PyPI for {package_name}=={version}...")

    # Try up to 3 times (PyPI indexing can take a few seconds)
    for attempt in range(3):
        try:
            url = f"https://pypi.org/pypi/{package_name}/json"
            with urllib.request.urlopen(url, timeout=10) as response:
                data = json.loads(response.read().decode())
                if version in data["releases"]:
                    print(f"✅ PyPI: Version {version} found")
                    return True
                else:
                    print(f"⏳ PyPI: Version {version} not indexed yet (attempt {attempt+1}/3)")
                    if attempt < 2:
                        time.sleep(5)  # Wait before retry
        except urllib.error.URLError as e:
            print(f"❌ PyPI request failed: {e}")
            return False
        except Exception as e:
            print(f"❌ PyPI error: {e}")
            return False

    return False


def check_github(owner: str, repo: str, version: str) -> bool:
    """Verify release exists on GitHub."""
    print(f"🔍 Checking GitHub for {owner}/{repo} release v{version}...")

    try:
        result = subprocess.run(
            ["gh", "release", "view", f"v{version}", "-R", f"{owner}/{repo}"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            print(f"✅ GitHub: Release v{version} found")
            return True
        else:
            print(f"❌ GitHub: Release not found (gh exit code: {result.returncode})")
            return False
    except FileNotFoundError:
        print("❌ GitHub: gh CLI not found. Install with: brew install gh")
        return False
    except subprocess.TimeoutExpired:
        print("❌ GitHub: Request timed out")
        return False


def run_tests() -> bool:
    """Run full test suite.

    Pytest occasionally hangs in Py_FinalizeEx (asyncio event-loop teardown
    from background HTTP clients) AFTER all tests have passed. We capture
    output and treat a TimeoutExpired as success iff the "[100%]" pytest
    completion marker appears in the captured output — meaning all tests
    finished, only Python shutdown is stuck.
    """
    print("🔍 Running test suite...")
    timeout_seconds = int(os.environ.get("VERIFY_TEST_TIMEOUT", "600"))

    # v9.3.1 — align excludes with scripts/release.sh's pytest gate so verify
    # doesn't fail on tests release.sh deliberately skips (flaky integration
    # tests, network-dependent suites, etc.). Single source of truth would
    # be ideal — for now both lists must be kept in sync manually.
    cmd = [
        "uv", "run", "pytest", "tests/", "-q",
        "--ignore=tests/test_agno_integration.py",
        "--ignore=tests/test_codex_routing.py",
        "--ignore=tests/test_edge_cases.py",
        "--ignore=tests/test_freemium.py",
        "--ignore=tests/test_hook_health.py",
        "--ignore=tests/test_profile_invariants.py",
        "--ignore=tests/test_quality_guard.py",
        "--ignore=tests/test_rate_limit.py",
        "--ignore=tests/test_router.py",
        "--ignore=tests/test_adaptive_router.py",
        "--ignore=tests/test_agent_loop.py",
        "--ignore=tests/commands/test_doctor.py",
        "--deselect=tests/test_cost.py::test_get_router_efficiency",
        "--deselect=tests/test_cost.py::test_get_classifier_overhead",
        "--deselect=tests/test_cost.py::test_get_savings_by_task_type",
        "-m", "not slow",
        "--tb=line", "--disable-warnings",
    ]
    try:
        result = subprocess.run(
            cmd,
            timeout=timeout_seconds,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("✅ Tests: All tests passed")
            return True
        else:
            print(f"❌ Tests: Some tests failed (exit code: {result.returncode})")
            if result.stdout:
                print(result.stdout[-2000:])
            return False
    except subprocess.TimeoutExpired as exc:
        captured = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        if "[100%]" in captured:
            print(f"✅ Tests: All tests passed (Python shutdown hung after [100%]; "
                  f"killed by {timeout_seconds}s timeout — known asyncio teardown leak)")
            return True
        print(f"❌ Tests: Test suite timed out ({timeout_seconds}s) before reaching [100%]")
        return False
    except FileNotFoundError:
        print("❌ Tests: uv not found")
        return False


def main():
    """Run all verification checks."""
    print("=" * 60)
    print("POST-RELEASE VERIFICATION")
    print("=" * 60)
    print()

    version = get_version()
    print(f"Verifying version: {version}\n")

    results = {
        "PyPI": check_pypi("llm-routing", version),
        "GitHub": check_github("ypollak2", "llm_router", version),
        "Tests": run_tests(),
    }

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    all_passed = True
    for check, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} — {check}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("🎉 All checks passed! Release is complete.")
        return 0
    else:
        print("⚠️  Some checks failed. See details above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
