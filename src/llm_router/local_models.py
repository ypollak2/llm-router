"""One context window (``num_ctx``) per local Ollama model.

Ollama treats every distinct ``num_ctx`` as a separate runner, so two callers
that ask the same model for different windows evict each other (a 3-6 s reload
each time) and, for the 30B coder, cost another ~31 GB. Every caller that sizes
a local request reads the window here, through :func:`num_ctx`.

Measured basis (resident size, 52 GB Mac):
* qwen3.5 / qwen3.8 hold 131072 on the GPU (I2b, 2026-09-24) but the owner runs the
  server at OLLAMA_CONTEXT_LENGTH=32768 (2026-10-10); an explicit 131072 here overrode
  it and cost memory (N20). They now take the default; raise one with
  ``LLM_ROUTER_LOCAL_NUM_CTX`` / ``LLM_ROUTER_PROXY_NUM_CTX`` or a table entry.
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
    "llmr-classifier": 4096,
    "llmr-edit": 16384,
}


def _name(model: str | None) -> str:
    name = (model or "").strip().lower()
    return name.split("/", 1)[1] if name.startswith("ollama/") else name


def num_ctx(model: str | None = None) -> int:
    """The context window to request for *model* (``ollama/`` prefix optional).

    An exact table entry wins. Otherwise the bare base name is tried, because
    Ollama reports an alias with a tag (``llmr-edit:latest`` in ``ollama list``,
    ``/api/ps`` and ``/api/tags``) while the table keys it bare. Then another
    tag of a family whose ``:latest`` is listed (``qwen3.5:9b``) takes that
    family's window; anything else gets ``DEFAULT_NUM_CTX``.
    """
    name = _name(model)
    if name in NUM_CTX:
        return NUM_CTX[name]
    base = name.split(":", 1)[0]
    if base:
        return NUM_CTX.get(base, NUM_CTX.get(f"{base}:latest", DEFAULT_NUM_CTX))
    return DEFAULT_NUM_CTX


# What a local model may be asked to do. Ollama's ``/api/show`` capabilities
# list says "completion" for a model that still 400s on a generate call when it
# was built for one narrow job, so the roles are declared here, next to the
# window. A model not listed here serves ``generate``. ``resource_map`` reads
# this table; nothing else should guess a model's role from its name.
# nimble:9b is the typed decision model (``decision_classifier.DEFAULT_MODEL``):
# a generate call to it returns HTTP 400 (bug N19).
ROLES: dict[str, frozenset[str]] = {
    "nimble:9b": frozenset({"decision"}),
}
DEFAULT_ROLES: frozenset[str] = frozenset({"generate"})


def roles(model: str | None = None) -> frozenset[str]:
    """The roles *model* serves (``ollama/`` prefix optional); see :data:`ROLES`."""
    name = _name(model)
    return ROLES.get(name, DEFAULT_ROLES)


#: Name prefixes of models that must never serve generation. ``llmr-classifier`` is a qwen3.5
#: Modelfile and ``llmr-classifier-38`` a qwen3.8 one: /api/show reports both as normal chat
#: models (no SYSTEM, template ``{{ .Prompt }}``, caps include ``completion``), so there is no
#: marker to read; the name is the only signal. An exact-name rule let ``llmr-classifier-38``
#: through (M0-3 rerun3, a code call burnt a 120 s timeout on it). ``llamacpp:<sha256>`` is
#: the tag Ollama's llamacpp runner gives an unnamed import of a blob: two exist on the
#: reference machine, byte-for-byte copies of ``qwen3.5`` and ``llmr-classifier``; an
#: unnamed import has no declared role, so it never enters a code chain.
#: Lives here, not in ``discover``, so the per-prompt hook can read it without importing
#: ``discover`` (~0.56 s) (HOOK-DRAFT-1).
NON_GENERATE_NAME_PREFIXES = ("llmr-classifier", "llamacpp:")


def _configured_classifier_bases() -> frozenset[str]:
    """Bare names of the models the classifier path is configured to use (explicit role)."""
    import os

    out = set()
    for v in (os.environ.get("LLM_ROUTER_CLASSIFIER_MODEL", ""), os.environ.get("LLM_ROUTER_DECISION_MODEL", "")):
        v = v.strip().lower()
        if v:
            out.add(v.split(":", 1)[0])
    return frozenset(out)


def is_named_non_generator(model: str | None) -> bool:
    """True for a classifier-only or unnamed-import model, by role then by name prefix.

    ``LLM_ROUTER_LOCAL_ALLOW_MODELS`` (comma-separated exact names, tag optional) overrides
    the name rules for a model the user deliberately named, e.g. a coder pulled as
    ``llamacpp:...``.
    """
    import os

    low = _name(model)
    allow = {a.strip().lower() for a in os.environ.get("LLM_ROUTER_LOCAL_ALLOW_MODELS", "").split(",") if a.strip()}
    if low in allow or (":" not in low and f"{low}:latest" in allow) or low.removesuffix(":latest") in allow:
        return False
    return low.startswith(NON_GENERATE_NAME_PREFIXES) or low.split(":", 1)[0] in _configured_classifier_bases()


def serves_generate(model: str | None) -> bool:
    """False for a model whose declared role excludes ``generate`` (:data:`ROLES`) or that
    the name rule marks as a classifier / unnamed import (:func:`is_named_non_generator`)."""
    return "generate" in roles(model) and not is_named_non_generator(model)
