#!/usr/bin/env python3
"""Post-release verification: PyPI, GitHub, and test suite."""

import json
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
    """Run full test suite."""
    print("🔍 Running test suite...")

    try:
        result = subprocess.run(
            ["uv", "run", "pytest", "tests/", "-q", "--ignore=tests/test_agno_integration.py"],
            timeout=120
        )
        if result.returncode == 0:
            print("✅ Tests: All tests passed")
            return True
        else:
            print(f"❌ Tests: Some tests failed (exit code: {result.returncode})")
            return False
    except subprocess.TimeoutExpired:
        print("❌ Tests: Test suite timed out (120s)")
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
        "PyPI": check_pypi("claude-code-llm-router", version),
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
