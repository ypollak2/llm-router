"""Self-calibrating registry of which local Ollama models can drive the agent loop.

The `tools` capability flag Ollama reports is NOT trustworthy — some models
advertise it but cannot actually use the structured tool-calling protocol
(observed: qwen2.5-coder:7b emits the call as text). The only reliable signal is
to *run* a tiny tool-calling task and check the ground-truth result.

This module probes each installed model once, caches the verdict (with a TTL and
a hash of the installed-model set so a newly pulled model is re-probed
automatically), and exposes a `rank()` so callers order verified tool-callers
first. That means any FUTURE model that drifts is caught the next time the model
set changes — no code edit required.

Draft generated via llm_router routing (codex/gpt-5.5), then verified against the
real run_agent_loop signature and ground-truth tested locally.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from llm_router.hooks.agent_loop import _get_ollama_url, run_agent_loop


CACHE_PATH = Path.home() / ".llm-router" / "agentic_models.json"
PROBE_PROMPT = (
    "Create a file fib.py with fib(n) returning the nth Fibonacci number "
    "(fib(0)=0, fib(1)=1) and print(fib(10)); then run 'python3 fib.py' "
    "and report output."
)


def list_installed_models() -> list[str]:
    """Return locally installed Ollama model names, or [] on failure."""
    try:
        url = f"{_get_ollama_url().rstrip('/')}/api/tags"
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))

        models = payload.get("models", [])
        names: list[str] = []
        for item in models:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(item["name"])
        return names
    except Exception:
        return []


def probe_model(model: str, timeout: int = 90) -> bool:
    """Return True if model can complete a tool-calling Fibonacci probe."""
    temp_dir = tempfile.mkdtemp(prefix="llm_router-agentic-probe-")
    project_root = Path(temp_dir)

    try:
        run_agent_loop(
            PROBE_PROMPT,
            model=model,
            project_root=project_root,
            timeout_per_call=timeout,
        )

        fib_path = project_root / "fib.py"
        if not fib_path.is_file():
            return False

        result = subprocess.run(
            ["python3", "fib.py"],
            cwd=project_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return result.stdout.strip() == "55"
    except Exception:
        return False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def probe_capable(model: str, attempts: int = 3, timeout: int = 90) -> bool:
    """Best-of-N capability probe: True if the model passes ANY of `attempts`.

    A single probe is flaky — a genuinely tool-capable model (e.g. hermes3:8b)
    occasionally misses one run, and marking it FAIL on that one sample would
    wrongly bench it for the whole cache TTL (drift inside the registry itself).
    Capability is "can it do this at all"; per-call flakiness is handled at
    runtime by the fallback ladder. Early-exits on the first success, so a
    reliable model still costs exactly one probe.
    """
    for _ in range(max(1, attempts)):
        if probe_model(model, timeout=timeout):
            return True
    return False


def _models_hash(models: list[str]) -> str:
    """Return a stable hash for the sorted installed model names."""
    encoded = json.dumps(sorted(models), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_cache() -> dict[str, Any] | None:
    """Read the registry cache, returning None if unavailable or invalid."""
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

        if not isinstance(payload, dict):
            return None
        if not isinstance(payload.get("probed_at"), (int, float)):
            return None
        if not isinstance(payload.get("models_hash"), str):
            return None
        if not isinstance(payload.get("verdicts"), dict):
            return None

        verdicts = payload["verdicts"]
        if not all(isinstance(k, str) and isinstance(v, bool) for k, v in verdicts.items()):
            return None

        return payload
    except Exception:
        return None


def _write_cache(payload: dict[str, Any]) -> None:
    """Atomically write the registry cache."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{CACHE_PATH.name}.",
        suffix=".tmp",
        dir=str(CACHE_PATH.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, CACHE_PATH)
    except Exception:
        try:
            os.unlink(temp_name)
        except Exception:
            pass
        raise


def get_registry(
    force: bool = False, ttl_seconds: int = 604800, allow_probe: bool = True
) -> dict[str, bool]:
    """Return cached model verdicts, refreshing when stale or model set changed.

    `allow_probe=False` is the hot-path mode: never run a (blocking, ~seconds-
    per-model) probe. It returns the cache as a soft hint — even if stale or the
    model set changed — else {}. Use it from latency-sensitive callers like the
    router; use the default (or force=True) from the CLI / install hook where a
    real probe is wanted.
    """
    try:
        models = sorted(list_installed_models())
        models_hash = _models_hash(models)
        cache = _read_cache()

        cache_valid = (
            cache is not None
            and cache.get("models_hash") == models_hash
            and time.time() - float(cache.get("probed_at", 0)) <= ttl_seconds
        )

        if not force and cache_valid:
            return dict(cache["verdicts"])

        if not allow_probe:
            # Hot path: never block on a probe. Whatever cache exists is a good
            # enough soft hint for ordering; empty means "no preference".
            return dict(cache["verdicts"]) if cache else {}

        verdicts = {model: probe_capable(model) for model in models}
        payload: dict[str, Any] = {
            "probed_at": time.time(),
            "models_hash": models_hash,
            "verdicts": verdicts,
        }
        _write_cache(payload)
        return verdicts
    except Exception:
        return {}


def rank(model_name: str, verdicts: dict[str, bool]) -> int:
    """Rank model preference: 0 passing (try first), 1 unknown, 2 failing (last)."""
    verdict = verdicts.get(model_name)
    if verdict is True:
        return 0
    if verdict is False:
        return 2
    return 1


def best_agentic_model(
    prefer: tuple[str, ...] = ("qwen3-coder", "devstral", "hermes", "qwen3"),
    provider_prefix: str = "ollama/",
) -> str:
    """Best VERIFIED agentic model as a provider-prefixed string, chosen
    DYNAMICALLY from this machine's registry — never hardcoded, so it adapts to
    each user's installed set. Cache-only (never probes). "" if none verified.

    Selection among verified models: `prefer`-substring priority first (earlier
    = stronger), then larger parameter size (parsed from a ':NNb' suffix), then
    name. A verified model matching no `prefer` token is still eligible, ranked
    after matched ones.
    """
    try:
        verified = [name for name, ok in get_registry(allow_probe=False).items() if ok]
        if not verified:
            return ""

        def pref_rank(name: str) -> int:
            for i, token in enumerate(prefer):
                if token in name:
                    return i
            return len(prefer)

        def size(name: str) -> float:
            tail = name.rsplit(":", 1)[-1].lower()
            if tail.endswith("b"):
                try:
                    return float(tail[:-1])
                except ValueError:
                    return -1.0
            return -1.0

        chosen = sorted(verified, key=lambda name: (pref_rank(name), -size(name), name))[0]
        return provider_prefix + chosen
    except Exception:
        return ""


def populate_in_background() -> bool:
    """Force-probe the registry in a detached subprocess so callers (e.g. the
    install hook) never block ~1-2 min. Best-effort: True if spawned, False if
    skipped (no Ollama / no models) or on error."""
    try:
        if not list_installed_models():
            return False
        import subprocess
        import sys as _sys

        subprocess.Popen(
            [_sys.executable, "-m", "llm_router.agentic_registry"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


if __name__ == "__main__":
    registry = get_registry(force=True)
    passed = sum(1 for verdict in registry.values() if verdict)
    failed = sum(1 for verdict in registry.values() if not verdict)

    width = max([len("MODEL"), *(len(model) for model in registry)] or [len("MODEL")])
    print(f"{'MODEL'.ljust(width)}  RESULT")
    print(f"{'-' * width}  ------")
    for model in sorted(registry):
        print(f"{model.ljust(width)}  {'PASS' if registry[model] else 'FAIL'}")
    print(f"\nSummary: {passed} PASS, {failed} FAIL, {len(registry)} total")
