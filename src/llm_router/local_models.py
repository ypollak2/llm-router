"""One context window (``num_ctx``) per local Ollama model.

Ollama treats every distinct ``num_ctx`` as a separate runner, so two callers
that ask the same model for different windows evict each other (a 3-6 s reload
each time) and, for the 30B coder, cost another ~31 GB. Every caller that sizes
a local request reads the window here, through :func:`num_ctx`.

Measured basis (resident size, 52 GB Mac):
* qwen3.5 / qwen3.8 hold 131072 on the GPU (I2b, 2026-09-24).
* qwen3-coder:30b at 32768 already uses 31.4-31.7 GB; 64K spilled 11% and
  128K 45% of it to CPU.
* The two ``llmr-*`` aliases are qwen3.5 Modelfiles with their window fixed.
Unlisted models get ``DEFAULT_NUM_CTX``, the window known to be safe.

Operator overrides (``LLM_ROUTER_LOCAL_NUM_CTX`` and friends) are applied by the
callers, not here: this module is the table, not the policy.
"""
from __future__ import annotations

DEFAULT_NUM_CTX = 32768

NUM_CTX: dict[str, int] = {
    "qwen3-coder:30b": 32768,
    "qwen3.6:35b-a3b-coding": 32768,
    "qwen3.5:latest": 131072,
    "qwen3.8:latest": 131072,
    "llmr-classifier": 4096,
    "llmr-edit": 16384,
}


def _name(model: str | None) -> str:
    name = (model or "").strip().lower()
    return name.split("/", 1)[1] if name.startswith("ollama/") else name


def num_ctx(model: str | None = None) -> int:
    """The context window to request for *model* (``ollama/`` prefix optional).

    An exact table entry wins. Otherwise another tag of a family whose
    ``:latest`` is listed (``qwen3.5:9b``) takes that family's window, which is
    what the earlier family match did; anything else gets ``DEFAULT_NUM_CTX``.
    """
    name = _name(model)
    if name in NUM_CTX:
        return NUM_CTX[name]
    base = name.split(":", 1)[0]
    if base:
        return NUM_CTX.get(f"{base}:latest", DEFAULT_NUM_CTX)
    return DEFAULT_NUM_CTX
