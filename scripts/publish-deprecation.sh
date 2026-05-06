#!/bin/bash
# Publish deprecation package for claude-code-llm-router to PyPI

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DEPRECATION_DIR="$PROJECT_ROOT/deprecation"

echo "📦 Building and publishing deprecation package..."
echo "   Package: claude-code-llm-router (redirects to llm-routing)"
echo "   Version: 1.0.0"
echo ""

# Check if deprecation directory exists
if [ ! -d "$DEPRECATION_DIR" ]; then
    echo "❌ Deprecation directory not found: $DEPRECATION_DIR"
    exit 1
fi

cd "$DEPRECATION_DIR"

# Verify pyproject.toml exists
if [ ! -f "pyproject.toml" ]; then
    echo "❌ pyproject.toml not found in $DEPRECATION_DIR"
    exit 1
fi

# Build
echo "🔨 Building distribution..."
uv build 2>&1 | tail -5

if [ ! -d "dist" ]; then
    echo "❌ Build failed: dist/ not found"
    exit 1
fi

echo "✅ Build successful"
echo ""

# Publish
echo "📤 Publishing to PyPI..."
echo "   Note: You need PyPI credentials in ~/.pypirc or PYPI_TOKEN env var"
echo ""

if [ -z "$PYPI_TOKEN" ]; then
    echo "⚠️  No PYPI_TOKEN found. Checking for ~/.pypirc..."
    if [ -f "$HOME/.pypirc" ]; then
        echo "   Using credentials from ~/.pypirc"
    else
        echo "❌ No PyPI credentials found."
        echo "   Set PYPI_TOKEN or configure ~/.pypirc"
        exit 1
    fi
fi

# Use twine or gh action
if command -v twine &> /dev/null; then
    echo "Using twine to publish..."
    if [ -n "$PYPI_TOKEN" ]; then
        twine upload dist/* --password "$PYPI_TOKEN" --skip-existing
    else
        twine upload dist/*
    fi
elif command -v uv &> /dev/null; then
    echo "Using uv to publish..."
    uv build --publish
else
    echo "❌ Neither twine nor uv found"
    echo "   Install with: pip install twine"
    exit 1
fi

echo ""
echo "✅ Deprecation package published!"
echo ""
echo "Verify on PyPI: https://pypi.org/project/claude-code-llm-router/"
echo ""
echo "Users with the old package will now see:"
echo "  1. A deprecation warning on import"
echo "  2. A dependency on llm-routing (auto-installed)"
echo "  3. Clear migration instructions"
