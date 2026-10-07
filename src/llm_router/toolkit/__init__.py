"""Router-owned tool layer (phase 1): one executor behind every local model.

Seven tools (read, search, list, edit, write, bash, finish), one permission
function (`policy.Policy.decide`) enforced in code before every call, a throwaway
workspace, an OS sandbox for the model-driven shell that FAILS CLOSED, and a
verifier that alone decides `used`.

Phase 1 scope (docs: ~/.rsi/research/tool-layer/PLAN.md, section 10): local
Ollama only, propose-only (a patch is returned, the caller's tree is never
touched), no Claude fallback, no cloud adapter.
"""
from __future__ import annotations

__all__ = ["adapters", "loop", "policy", "result", "sandbox", "tools", "verify"]
