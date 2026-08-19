"""llm_router probe — verify which local Ollama models can drive the agentic loop.

Runs a live best-of-N ground-truth probe of every installed model, caches the
verdicts to ~/.llm-router/agentic_models.json, and reports the dynamically-selected
agentic model. `--cached` reads the existing cache without re-probing.
"""

from __future__ import annotations


def cmd_probe(args: list[str]) -> int:
    """Execute: llm_router probe [--cached]"""
    from llm_router.agentic_registry import best_agentic_model, get_registry

    use_cache = "--cached" in args
    verdicts = get_registry(force=not use_cache, allow_probe=not use_cache)

    if not verdicts:
        if use_cache:
            print("No cached verdicts yet. Run `llm_router probe` (no --cached) to build the registry.")
        else:
            print("No models probed — is Ollama running and are any models installed?")
        return 1

    width = max([len("MODEL"), *(len(m) for m in verdicts)])
    print(f"{'MODEL'.ljust(width)}  RESULT")
    print(f"{'-' * width}  ------")
    for model in sorted(verdicts):
        print(f"{model.ljust(width)}  {'PASS' if verdicts[model] else 'FAIL'}")

    passed = sum(1 for ok in verdicts.values() if ok)
    print(f"\n{passed}/{len(verdicts)} verified tool-callers"
          f"{' (from cache)' if use_cache else ''}")
    best = best_agentic_model()
    print(f"Dynamic agentic model → {best or '(none verified)'}")
    return 0
