"""Moved to ``llm_router.groundtruth_sources`` (Phase 0.2d).

northstar needs these exclusion rules at runtime, and the wheel does not ship
``scripts/``, so an installed northstar used to find no rules and silently
count benchmark sandboxes, synthetic sessions and harness artefacts. The
single copy now lives in the package; this module aliases it, so
``groundtruth.sources`` and ``llm_router.groundtruth_sources`` are the SAME
module object (a monkeypatch on either is seen by both).
"""
import sys

from llm_router import groundtruth_sources as _impl

sys.modules[__name__] = _impl
