"""`llm-router run` : one task through the router-owned tool layer (propose-only).

    llm-router run --model qwen3.6:35b-a3b-coding --verify "pytest -q tests/test_x.py" \\
        --workspace . "fix the off-by-one in pager.py"

The model works in a throwaway COPY of --workspace and the caller's tree is never
written. The output is a patch file and a verdict; applying it is the human's job.
Exit status: 0 verified (used=true), 1 not verified or not used, 2 usage error,
3 switched off (LLM_ROUTER_TOOLLAYER=off or the KILL file).
"""
from __future__ import annotations

import argparse
import sys

USAGE = __doc__


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="llm-router run", add_help=True,
                                description="Run one task through the router-owned tool layer (propose-only).")
    p.add_argument("task", nargs="?", help="the task text (or use --task-file)")
    p.add_argument("--task-file", help="read the task text from this file")
    p.add_argument("--model", required=True, help="local Ollama model, e.g. qwen3.6:35b-a3b-coding")
    p.add_argument("--workspace", default=".", help="source tree to copy (default: current directory)")
    p.add_argument("--verify", help="test command run before and after, e.g. 'pytest -q tests/test_x.py'")
    p.add_argument("--python-dir", help="bin directory whose python/pytest the sandboxed commands use")
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--max-seconds", type=float, default=600.0)
    p.add_argument("--max-tokens", type=int, default=400_000)
    p.add_argument("--constrained", action="store_true", help="use Ollama grammar-constrained tool calls")
    p.add_argument("--keep-workspace", action="store_true", help="keep the throwaway workspace for inspection")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    return p


def cmd_run(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    task = args.task
    if args.task_file:
        with open(args.task_file, encoding="utf-8") as fh:
            task = fh.read()
    if not task or not task.strip():
        print("llm-router run: a task is required (positional or --task-file)", file=sys.stderr)
        return 2
    model = args.model[len("ollama/"):] if args.model.startswith("ollama/") else args.model

    from llm_router.toolkit import sandbox
    from llm_router.toolkit.adapters.ollama import OllamaAdapter
    from llm_router.toolkit.loop import Budgets, run_task

    why = sandbox.kill_switch_reason()
    if why:
        print(f"llm-router run: tool layer is switched off ({why})", file=sys.stderr)
        return 3
    sandbox.install_signal_handlers()
    res = run_task(task, adapter=OllamaAdapter(model, constrained=args.constrained),
                   source=args.workspace, verify_cmd=args.verify, python_dir=args.python_dir,
                   budgets=Budgets(max_steps=args.max_steps, max_seconds=args.max_seconds,
                                   max_tokens=args.max_tokens),
                   keep_workspace=args.keep_workspace)
    if args.json:
        print(res.to_json())
    else:
        print(f"status:  {res.status} ({res.stop_reason})")
        print(f"verdict: {res.verdict_line()}")
        print(f"bash:    {'sandboxed' if res.bash_enabled else 'OFF: ' + res.bash_reason}")
        print(f"changed: {', '.join(res.changed_files) or '(nothing)'}")
        print(f"patch:   {res.patch_path}   (propose-only: your tree was not touched)")
        if res.source_modified:
            print("WARNING: the source tree changed during the run", file=sys.stderr)
    if res.status == "kill":
        return 3
    return 0 if res.used is True else 1


def main(argv: list[str] | None = None) -> int:
    return cmd_run(sys.argv[1:] if argv is None else argv)
