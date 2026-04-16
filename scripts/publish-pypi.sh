#!/usr/bin/env bash
# Publish llm-router to PyPI with automatic credential extraction from ~/.pypirc
#
# Usage:
#   ./scripts/publish-pypi.sh              # Build and publish
#   ./scripts/publish-pypi.sh --dry-run    # Dry run (don't actually upload)
#
# Requirements:
#   - .pypirc configured in home directory with [pypi] section
#   - uv installed (used for building)
#   - git in clean state (no uncommitted changes)

set -e

DRY_RUN=false
if [[ "$1" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "🏃 DRY RUN MODE — will not publish to PyPI"
fi

# Verify git is clean
if [[ -n $(git status -s) ]]; then
    echo "❌ Error: git working directory is not clean"
    echo "   Commit or stash changes before publishing"
    git status -s
    exit 1
fi

# Verify .pypirc exists
PYPIRC="$HOME/.pypirc"
if [[ ! -f "$PYPIRC" ]]; then
    echo "❌ Error: $PYPIRC not found"
    echo "   Configure PyPI credentials in ~/.pypirc"
    exit 1
fi

# Extract PyPI token from .pypirc
PYPI_TOKEN=$(python3 << 'EOF'
import configparser
import sys

try:
    c = configparser.ConfigParser()
    c.read('${HOME}/.pypirc')
    print(c['pypi']['password'])
except Exception as e:
    print(f"Error reading .pypirc: {e}", file=sys.stderr)
    sys.exit(1)
EOF
)

if [[ -z "$PYPI_TOKEN" ]]; then
    echo "❌ Error: Could not extract token from .pypirc"
    exit 1
fi

echo "✅ Found PyPI credentials"

# Get version
VERSION=$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
echo "📦 Publishing version: $VERSION"

# Clean and build
echo "🏗️  Building distribution..."
rm -rf dist/
uv build > /dev/null 2>&1

if [[ ! -f "dist/claude_code_llm_router-${VERSION}-py3-none-any.whl" ]]; then
    echo "❌ Error: Build failed or wheel not found"
    exit 1
fi

echo "✅ Built successfully"
echo "   - dist/claude_code_llm_router-${VERSION}-py3-none-any.whl"
echo "   - dist/claude_code_llm_router-${VERSION}.tar.gz"

# Publish
if [[ "$DRY_RUN" == "true" ]]; then
    echo ""
    echo "🏃 [DRY RUN] Would now publish to PyPI"
    echo "   Command: uv publish --token <token>"
    exit 0
fi

echo ""
echo "📤 Publishing to PyPI..."
uv publish --token "$PYPI_TOKEN"

echo ""
echo "✅ Published successfully!"
echo "   PyPI: https://pypi.org/project/claude-code-llm-router/$VERSION/"
