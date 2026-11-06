"""Machine qualification for apply repair; no model downloads or routing defaults."""
from __future__ import annotations

import json
import math
import platform
import sys
import tempfile
import time
from importlib.resources import files
from pathlib import Path
from typing import Mapping

from llm_router import apply_tool, model_discovery
from llm_router.local_check import runtimes
from llm_router.hooks.direct_executor import _get_ollama_url
from llm_router.resolver import eligibility_store as store

FIXTURES = ("addition", "boundary", "normalization")
ENDPOINT_REASON = "local repair needs a loopback Ollama endpoint"
GATE_REASON = "machine check not passed; run llm-router local setup"


def repair_endpoint() -> str:
    endpoint = _get_ollama_url()
    if not runtimes.is_loopback(endpoint):
        raise runtimes.RuntimeError_(ENDPOINT_REASON)
    return endpoint


def inventory() -> list[dict]:
    """Read the same daemon used by edit generation; loopback only, no proxy."""
    data = runtimes._strict_json(repair_endpoint(), "GET", "/api/tags")
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise runtimes.ProbeFailed("Ollama returned no model inventory")
    return runtimes._entries(data, "models")


def model_row(rows, model):
    return next((r for r in rows if any(model_discovery._same_model(model, name)
                 for name in (r.get("name"), r.get("model")) if isinstance(name, str))), None)


def digest_for(row):
    value = row.get("digest") if row else None
    return (value if value.startswith("sha256:") else "sha256:" + value) if isinstance(value, str) and value else None


def validate_record(model: str, record: Mapping) -> None:
    if not isinstance(record, Mapping) or record.get("model") != model or record.get("n") != 3:
        raise ValueError("invalid apply_repair record")
    k = record.get("passed_n")
    if type(k) is not int or not 0 <= k <= 3:
        raise ValueError("invalid apply_repair count")
    for key in ("limit_s", "max_s", "ts"):
        v = record.get(key)
        if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0:
            raise ValueError("invalid apply_repair timing")
    if (record["limit_s"] <= 0 or not isinstance(record.get("platform"), str)
            or not isinstance(record.get("endpoint"), str)):
        raise ValueError("invalid apply_repair metadata")
    if k >= 2 and (not isinstance(record.get("digest"), str) or not record["digest"] or record.get("sandbox") is not True):
        raise ValueError("passed apply_repair needs digest and sandbox")


def records() -> dict:
    try:
        data = json.loads(store.eligibility_path().read_text())
    except (OSError, ValueError):
        return {}
    result = {}
    for model, entry in (data.items() if isinstance(data, dict) else []):
        task = entry.get("apply_repair") if isinstance(entry, dict) else None
        rec = task.get("machine") if isinstance(task, dict) else None
        try:
            validate_record(model, rec)
        except ValueError:
            continue
        result[model] = rec
    return result


def qualified(record, digest, limit_s, endpoint):
    return bool(record and digest and record.get("endpoint") == endpoint
                and record.get("digest") == digest
                and record["passed_n"] >= 2 and record["limit_s"] >= limit_s
                and record.get("sandbox") is True)


def repair_qualified(model: str, limit_s: float) -> bool:
    try:
        endpoint = repair_endpoint()
        record = records().get(model)
        if not record:
            return False
        row = model_row(inventory(), model)
        return qualified(record, digest_for(row), limit_s, endpoint) and not (
            row.get("remote_host") or row.get("remote_model"))
    except (runtimes.RuntimeError_, OSError, ValueError):
        return False


def sandbox_probe(root: Path) -> None:
    # _run_check always uses wrap + cleanup and requires containment here,
    # including when LLM_ROUTER_APPLY_SANDBOX=off.
    rc, tail, sandboxed = apply_tool._run_check(
        [sys.executable, "-c", "pass"], root, require_sandbox=True)
    if rc != 0 or not sandboxed:
        raise apply_tool.check_sandbox.SandboxUnavailable(tail)


def machine_check(model: str, digest: str, limit_s: float, out=print) -> dict:
    endpoint = repair_endpoint()
    passed_n, durations = 0, []
    for name in FIXTURES:
        with tempfile.TemporaryDirectory(prefix="llm-repair-") as tmp:
            root = Path(tmp).resolve()
            source = files("llm_router").joinpath("data", "apply_repair", name)
            for filename in ("module.py", "test_module.py"):
                (root / filename).write_bytes(source.joinpath(filename).read_bytes())
            repair = {"repair_passed": False}
            details = []
            started = time.monotonic()
            apply_tool._repair_once(
                root, [sys.executable, "-m", "pytest", "-q", "test_module.py"],
                [root / "module.py"],
                "make this check pass with minimal edits to module.py; preserve the test:\n"
                + (root / "test_module.py").read_text(), details, repair, model)
            elapsed = time.monotonic() - started
            durations.append(elapsed)
            passed = repair["repair_passed"] and elapsed <= limit_s
            passed_n += int(passed)
            out(f"{'ok' if passed else 'fail'}: machine fixture {name} ({elapsed:.2f}s); "
                + ("passed" if passed else "try a faster or more capable local model"))
    return dict(model=model, digest=digest, endpoint=endpoint, limit_s=limit_s, n=3, passed_n=passed_n,
                max_s=max(durations), ts=time.time(), sandbox=True, platform=platform.platform())
