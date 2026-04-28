"""DEPRECATED: claude-code-llm-router

⚠️  This package is DEPRECATED and no longer maintained.

→ Please upgrade to 'llm-routing' instead:
    pip install --upgrade llm-routing

All functionality has been moved to the new package. Simply replace:
    from claude_code_llm_router import ...
With:
    from llm_routing import ...

The new package has the same API with improved performance and features.
"""

import sys
import warnings

# Issue deprecation warning on import
warnings.warn(
    "claude-code-llm-router is DEPRECATED and no longer maintained. "
    "Please upgrade to 'llm-routing' instead: pip install --upgrade llm-routing",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export everything from the new package
try:
    from llm_routing import *  # noqa: F401, F403
except ImportError as e:
    print(
        "Error: llm-routing package not found. Please install it:",
        file=sys.stderr,
    )
    print("  pip install llm-routing", file=sys.stderr)
    raise

__all__ = ["deprecation_warning"]

__version__ = "1.0.0"
__deprecated__ = True
__migration_target__ = "llm-routing>=7.6.2"


def deprecation_warning():
    """Display deprecation information."""
    msg = """
╔══════════════════════════════════════════════════════════════════════════════╗
║                           DEPRECATION NOTICE                                 ║
╚══════════════════════════════════════════════════════════════════════════════╝

The 'claude-code-llm-router' package is DEPRECATED and no longer maintained.

✅ To migrate, simply upgrade to 'llm-routing':

   pip install --upgrade llm-routing

All functionality is identical — the API remains the same. The new package name
reflects the project's scope beyond Claude and improved organization.

📚 Migration details: https://github.com/ypollak2/llm-router#deprecation-notice

Questions? Open an issue: https://github.com/ypollak2/llm-router/issues
    """
    print(msg)
