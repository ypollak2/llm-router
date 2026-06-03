"""Bundled benchmark runners.

Each module in this package imports :mod:`llm_router.benchmark` and calls
``register_runner(...)`` at import time. The parent package imports them
all so that ``from llm_router.benchmark import get_runner`` finds them
without callers having to know module paths.
"""
