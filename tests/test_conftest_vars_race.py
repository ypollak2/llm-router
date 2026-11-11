"""VARS-RACE-1: the module-leak fingerprint must not crash when another thread imports into the package.

CI run 42980583688 (PR #904, test (3.13), 2026-11-11) failed with "dictionary changed size during iteration"
inside ``_module_state_fingerprint``: it iterated ``vars(llm_router)`` while a thread left by an earlier test
added a submodule attribute. This test adds and removes module attributes on ``llm_router`` from a thread while
the fingerprint runs, and removes every one of them before it returns, so the leak fixture sees no change.
"""
from __future__ import annotations

import sys
import threading
import types

import llm_router


def _fingerprint():
    # the tests/ conftest is already loaded by pytest; take its function rather than importing a second copy
    mod = next(m for name, m in list(sys.modules.items())
               if name.endswith("conftest") and hasattr(m, "_module_state_fingerprint"))
    return mod._module_state_fingerprint


def test_fingerprint_survives_a_concurrent_import_into_the_package():
    stop = threading.Event()
    names = [f"_vars_race_probe_{i}" for i in range(64)]

    def churn():
        while not stop.is_set():
            for n in names:
                setattr(llm_router, n, types.ModuleType(n))
            for n in names:
                delattr(llm_router, n)

    fingerprint = _fingerprint()
    t = threading.Thread(target=churn, daemon=True)
    sys_switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # switch threads as often as possible, so the race shows up if it exists
    errors = []
    try:
        t.start()
        for _ in range(3000):
            try:
                fingerprint()
            except RuntimeError as exc:
                errors.append(exc)
                break
    finally:
        stop.set()
        t.join()
        sys.setswitchinterval(sys_switch)
        for n in names:
            if hasattr(llm_router, n):
                delattr(llm_router, n)
    assert errors == []
