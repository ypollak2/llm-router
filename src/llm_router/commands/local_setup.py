"""`llm-router local setup`: explicit, local repair readiness and qualification."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import tempfile
import time
from pathlib import Path

from llm_router import apply_tool, local_setup as setup, zero_claude_edit


def _show(steps, out):
    for name, ok, fix in steps:
        out(f"{'ok' if ok else 'fail'}: {name}; {fix}")


def cmd_local(argv: list[str], *, out=print) -> int:
    parser = argparse.ArgumentParser(prog="llm-router local")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("setup", help="check this machine for local repair; never pulls models")
    p.add_argument("--model")
    p.add_argument("--status", action="store_true", help="read the stored steps; no probes or model calls")
    args = parser.parse_args(argv)
    if args.status:
        records = setup.records()
        record = records.get(args.model) if args.model is not None else max(
            records.values(), key=lambda r: r["ts"], default=None)
        if record is None:
            out("fail: no stored setup; run llm-router local setup")
            return 1
        out(f"Stored setup: model={record['model']} ts={record['ts']} limit_s={record['limit_s']}")
        _show(record.get("steps", []), out)
        return 0 if record["passed_n"] >= 2 else 1

    # Preflight for an early diagnostic; the strict writer protects the locked merge.
    def readable_store():
        try:
            data = json.loads(setup.store.eligibility_path().read_text())
            if not isinstance(data, dict):
                raise ValueError("eligibility store must be a JSON object")
        except FileNotFoundError:
            return True
        except (OSError, ValueError) as exc:
            out(f"fail: cannot read existing eligibility store; setup aborted: {exc}")
            return False
        return True

    if not readable_store():
        return 1
    model = args.model or zero_claude_edit.edit_model(allow_probe=False) or ""
    limit = apply_tool._repair_seconds()
    record = dict(model=model, digest=None, endpoint=setup._get_ollama_url(), limit_s=limit, n=3, passed_n=0,
                  max_s=0, ts=time.time(), sandbox=False, platform=platform.platform())
    steps = []
    try:
        setup.repair_endpoint()
        rows = setup.inventory()
        steps.append(("Ollama reachable", True, "Ollama answered /api/tags"))
    except (setup.runtimes.RuntimeError_, OSError, ValueError) as exc:
        rows = None
        steps.append(("Ollama reachable", False, f"start Ollama (ollama serve): {exc}"))
    if not model and rows:
        model = next((r["name"] for r in rows if isinstance(r.get("name"), str)
                      and setup.model_discovery._is_completion_model(r["name"])), "")
        record["model"] = model
    row = setup.model_row(rows or [], model)
    digest = setup.digest_for(row)
    present = bool(row and digest and not (row.get("remote_host") or row.get("remote_model")))
    steps.append((f"model present ({model or 'none selected'})", present,
                  f"digest {digest}" if present else
                  f"install a local model: ollama pull {shlex.quote(model) if model else '<model>'}; then use --model <model>"))
    sandbox = False
    try:
        with tempfile.TemporaryDirectory(prefix="llm-setup-") as tmp:
            setup.sandbox_probe(Path(tmp).resolve())
        sandbox = True
        steps.append(("sandbox require", True, "contained no-op passed"))
    except (apply_tool.check_sandbox.SandboxUnavailable, OSError) as exc:
        steps.append(("sandbox require", False, f"enable sandbox-exec (macOS) or bubblewrap (Linux): {exc}"))
    if rows is not None and present and sandbox:
        record = setup.machine_check(model, digest, limit, out)
    steps.append(("machine check", record["passed_n"] >= 2,
                  f"{record['passed_n']}/3 inside {limit:g}s; "
                  "re-run llm-router local setup after changing the model or limit"))
    enabled = os.environ.get("LLM_ROUTER_APPLY", "").strip().lower() in {"on", "1", "true", "yes"}
    steps.append(("LLM_ROUTER_APPLY=" + ("on" if enabled else "off"), enabled,
                  "enable with: export LLM_ROUTER_APPLY=on"))
    record["steps"] = steps
    if not readable_store():
        return 1
    try:
        setup.store.record_competence(model, "apply_repair", {"machine": record}, strict=True)
    except (OSError, ValueError) as exc:
        out(f"fail: cannot write eligibility store; setup aborted: {exc}")
        return 1
    _show(steps, out)
    return 0 if record["passed_n"] >= 2 else 1
