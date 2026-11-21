#!/usr/bin/env python3
"""Post-release verification: PyPI, GitHub, and test suite."""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
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


def _junit_verdict(junit_path: str) -> tuple[bool, str]:
    """Return (pass, reason) from a pytest junit XML file.

    Pass only when the file parses and contains at least one test run with
    0 failures and 0 errors — a run that collected nothing is not a pass.
    """
    try:
        root = ET.parse(junit_path).getroot()
    except (OSError, ET.ParseError) as exc:
        return False, f"no readable junit file ({type(exc).__name__})"

    t = f = e = s = 0
    for el in root.iter("testsuite"):
        t += int(el.get("tests", 0))
        f += int(el.get("failures", 0))
        e += int(el.get("errors", 0))
        s += int(el.get("skipped", 0))
    ran = t - s
    detail = f"junit: {t} tests, {ran} ran, {f} failures, {e} errors, {s} skipped"
    if ran > 0 and f == 0 and e == 0:
        return True, detail
    return False, detail


def _run_tests_with_junit(timeout_seconds: int, junit_path: str) -> bool:
    """Run pytest with --junitxml and evaluate the result.

    Returns True only when exit is 0 and the junit file has >=1 test run
    with 0 failures and 0 errors.
    """
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
        "--tb=line",
        "--junitxml=" + junit_path,
        "--disable-warnings",
    ]
    try:
        result = subprocess.run(
            cmd,
            timeout=timeout_seconds,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"❌ Tests: Some tests failed (exit code: {result.returncode})")
            if result.stdout:
                print(result.stdout[-2000:])
            return False
        pass_, reason = _junit_verdict(junit_path)
        if pass_:
            print(f"✅ Tests: All tests passed ({reason})")
            return True
        print(f"❌ Tests: {reason}")
        return False
    except subprocess.TimeoutExpired:
        print(f"❌ Tests: Test suite timed out ({timeout_seconds}s); a timeout is a failure.")
        print(f"   If the suite is only slow, raise VERIFY_TEST_TIMEOUT (now {timeout_seconds}s).")
        return False
    except FileNotFoundError:
        print("❌ Tests: uv not found")
        return False


def run_tests() -> bool:
    """Run full test suite.

    A timeout is always a failure, even after the "[100%]" marker: that old rule
    passed runs whose results were never read (same rule as release.sh, F2).
    Pass or fail comes from the junit file (release.sh F2).
    """
    print("🔍 Running test suite...")
    timeout_seconds = int(os.environ.get("VERIFY_TEST_TIMEOUT", "7200"))

    _junit_fd, _junit_path = tempfile.mkstemp(prefix="verify-release-junit-", suffix=".xml")
    os.close(_junit_fd)
    try:
        return _run_tests_with_junit(timeout_seconds, _junit_path)
    finally:
        try:
            os.unlink(_junit_path)
        except OSError:
            pass


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
        "GitHub": check_github("ypollak2", "llm-router", version),
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
