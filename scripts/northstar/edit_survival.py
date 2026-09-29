#!/usr/bin/env python3
"""Moved to ``llm_router.edit_survival`` (Phase 0.2d); this is the CLI entry.

northstar needs ``judge_row`` at runtime and the wheel does not ship
``scripts/``, so the single copy now lives in the package. Usage is unchanged:
see ``llm_router.edit_survival``'s docstring.
"""
import sys

from llm_router import edit_survival as _impl

if __name__ == "__main__":
    sys.exit(_impl.main())
sys.modules[__name__] = _impl
