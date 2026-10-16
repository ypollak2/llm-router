#!/usr/bin/env python3
"""Run the frozen P20 code selection, one arm at a time."""
from __future__ import annotations

import argparse
import ast
import contextlib
import importlib
import importlib.util
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "p20_code_requal", Path(__file__).with_name("p20_requal.py"),
)
h = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = h
SPEC.loader.exec_module(h)
_claude_run = subprocess.run


def contract(path):
    tree = ast.parse(path.read_text())
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        if any(isinstance(t, ast.Name) and t.id == "TEST_CONTRACT" for t in targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise h.Refuse("refusing: TEST_CONTRACT literal missing")


def git(work, *args):
    return subprocess.run(
        ["git", "-C", str(work), *args], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=120, check=True,
    ).stdout.strip()


def empty_patch(work, parent):
    changed = subprocess.run(
        ["git", "-C", str(work), "diff", "--quiet", parent],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=False,
    ).returncode
    if changed not in (0, 1):
        raise RuntimeError("git diff failed")
    return changed == 0 and not git(work, "ls-files", "--others", "--exclude-standard")


def gate(cfg, c, arm):
    if arm != "sonnet":
        h.quiet(cfg, c, arm)
    elif not 0 <= cfg.local_hour() < 7:
        raise h.Park("outside night window")


def agent(cfg, c, arm, ident, attempt, prompt, work, pi):
    if arm != "sonnet":
        pi.PI_MODEL = arm
        result = pi.run_pi(prompt, cwd=work, timeout_s=900)
        timed_out = bool(result["timed_out"])
        infra = not timed_out and (
            result["returncode"] != 0 or result["n_events"] == 0
        )
        return timed_out, "agent_error" if infra else None
    h.quota_guard(cfg, c)
    start = cfg.now()
    cost, served, error, timed_out = 0, "", None, False
    try:
        result = _claude_run(
            h.claude_argv(c["CLAUDE_SONNET_ID"], cfg.run_dir, tools=None)
            + ["--permission-mode", "acceptEdits", "--allowedTools",
               "Read,Edit,Write,Glob,Grep,Bash"],
            input=prompt, text=True, capture_output=True, timeout=900, cwd=work,
            env=h.claude_env(), check=False,
        )
        value = json.loads(result.stdout)
        cost = value.get("total_cost_usd", 0)
        if (isinstance(cost, bool) or not isinstance(cost, (int, float))
                or not math.isfinite(cost) or cost < 0):
            raise h.Refuse("refusing: invalid call cost")
        usage = value.get("modelUsage", {})
        served = next(iter(usage)) if isinstance(usage, dict) and len(usage) == 1 else ""
        error = "claude_error" if result.returncode or value.get("is_error") else None
        if served != c["CLAUDE_SONNET_ID"]:
            error = "served_model_mismatch"
    except subprocess.TimeoutExpired:
        timed_out = True
    except (OSError, ValueError, TypeError, AttributeError):
        error = "claude_error"
    h.append_row(cfg.run_dir / "ledger.jsonl", {
        "ts": h.utc(cfg), "stage": "code", "set": "code", "arm": arm,
        "item_id": ident, "attempt": attempt, "ok": error is None and not timed_out,
        "cost_usd": cost, "wall_s": cfg.now() - start, "served_model": served,
    })
    return timed_out, error


def safe(value):
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", value)


def child_path(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise h.Refuse("refusing: unsafe task path")
    return path


def test_path(value):
    if not re.fullmatch(r"tests/[A-Za-z0-9_./-]+\.py", value) or ".." in value:
        raise h.Refuse("refusing: unsafe test path")
    return value


def remove_tests(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def attempt_task(cfg, c, args, item, attempt, tc, tc_hash, runner_hash, nc, pi):
    start = cfg.now()
    row = {
        "task_id": item["id"], "arm": args.arm, "hidden_pass": None, "wall_s": 0,
        "timed_out": False, "patch_empty": False, "infra_class": None,
        "attempt": attempt, "tc_sha256": tc_hash, "runner_sha256": runner_hash,
        "ts": start,
    }
    work, hidden, meta = None, None, None
    tag = "p20_" + safe(args.arm)
    try:
        ref = item["task_ref"]
        if item["stratum"] == "noncloud":
            metas = {m["task_id"]: m for m in h.jsonl(args.noncloud_dir / "tasks_meta.jsonl")}
            task_id = ref["task_id"]
            meta = metas[task_id]
            nc.ensure_clone()
            work = Path(nc.make_wt(task_id, tag, meta["parent_sha"]))
            parent = meta["parent_sha"]
            prompt = nc.build_prompt(meta)
        elif item["stratum"] == "rx_edit":
            base = cfg.run_dir / "code_runs" / safe(args.arm) / safe(item["id"])
            if base.exists():
                shutil.rmtree(base)
            h.private_dir(base)
            work = base / "work"
            shutil.copytree(
                child_path(args.rx_dir / "fixtures", ref["dir"]), work,
                ignore=shutil.ignore_patterns(".git", "__pycache__", ".ruff_cache", "*.pyc"),
            )
            hidden = base / "hidden_tests"
            shutil.move(str(work / "tests"), hidden)
            git(work, "init", "-q")
            git(work, "add", ".")
            git(work, "-c", "user.name=P20", "-c", "user.email=p20@localhost",
                "-c", "commit.gpgsign=false", "commit", "-qm", "base")
            parent = git(work, "rev-parse", "HEAD")
            # Task text comes from the pinned fixtures manifest (checked before the first
            # rx_edit task), matched by idx and name; the pool carries no task text.
            fixture = next(
                (f for f in json.loads((args.rx_dir / "fixtures/manifest.json").read_text())
                 if f.get("idx") == ref["idx"] and f.get("name") == ref["name"]), None
            )
            if fixture is None:
                raise h.Refuse("refusing: rx fixture missing from manifest")
            prompt = (
                f"In {fixture['module']}, change `{fixture['func']}` so that it does this: "
                f"{fixture['summary']}\nEdit the file directly. "
                "Do not create new top-level files."
            )
        else:
            raise h.Refuse("refusing: unknown code stratum")
        timed_out, infra = agent(
            cfg, c, args.arm, item["id"], attempt, prompt + "\n\n" + tc, work, pi,
        )
        row["timed_out"], row["infra_class"] = timed_out, infra
        row["patch_empty"] = empty_patch(work, parent)
        if infra:
            return row
        if timed_out or row["patch_empty"]:
            row["hidden_pass"] = False
        elif meta is not None:
            grades, _ = nc.grade_all(work, meta, ref["task_id"], tag)
            row["hidden_pass"] = sum(grades) * 2 > len(grades)
        else:
            remove_tests(work / "tests")
            shutil.move(str(hidden), work / "tests")
            hidden = None
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                     test_path(ref["test"])],
                    cwd=work, stdin=subprocess.DEVNULL, capture_output=True, timeout=120,
                    env={"PATH": os.environ.get("PATH", ""), "HOME": str(work),
                         "PYTHONDONTWRITEBYTECODE": "1"}, check=False,
                )
                row["hidden_pass"] = result.returncode == 0
            except subprocess.TimeoutExpired:
                row["hidden_pass"] = False
    except (h.Refuse, h.Park):
        raise
    except Exception:
        row["infra_class"], row["hidden_pass"] = "runner_error", None
    finally:
        if hidden is not None and hidden.exists() and work is not None:
            remove_tests(work / "tests")
            shutil.move(str(hidden), work / "tests")
        if meta is not None and work is not None:
            try:
                git(work, "worktree", "remove", "--force", str(work))
            except (OSError, subprocess.SubprocessError):
                row["infra_class"], row["hidden_pass"] = "cleanup_error", None
        row["wall_s"] = cfg.now() - start
    return row


def run(cfg, args):
    c = h.frozen(cfg)["constants"]
    h.run_gate(cfg, c)
    selected = h.selection(cfg)
    if not (cfg.run_dir / "items/code.jsonl").is_file():
        raise h.Refuse("refusing: code items missing")
    h.require_unused(cfg)
    h.check_set(cfg, "code")
    if args.limit_tasks is not None and (not args.smoke or args.limit_tasks < 1):
        raise h.Refuse("refusing: positive --limit-tasks requires --smoke")
    if args.arm not in h.arms(c, True):
        raise h.Refuse("refusing: unknown code arm")
    items = h.items_for(cfg, "code", selected)
    if args.limit_tasks is not None:
        items = items[:args.limit_tasks]
    target = cfg.run_dir / (
        "code_results.smoke.jsonl" if args.smoke else "code_results.raw.jsonl"
    )
    attempts_path = cfg.run_dir / (
        "code_attempts.smoke.jsonl" if args.smoke else "code_attempts.jsonl"
    )
    existing = h.jsonl(target)
    pairs = [(r["task_id"], r["arm"]) for r in existing]
    if len(pairs) != len(set(pairs)):
        raise h.Refuse("refusing: duplicate final pair")
    done = set(pairs)
    pending = [i for i in items if (i["id"], args.arm) not in done]
    counts = dict(arm=args.arm, tasks=len(items), done=0, **{
        "pass": 0, "fail": 0, "null": 0, "timed_out": 0, "patch_empty": 0,
        "skipped_existing": len(items) - len(pending),
    })
    if not pending:
        return counts
    gate(cfg, c, args.arm)
    if args.arm != "sonnet":
        if cfg.ollama.list_ids().get(args.arm) != set(c[f"ID_{args.arm}"].split(",")):
            raise h.Refuse("refusing: model identity differs")
    tc_hash, runner_hash = h.sha256_file(args.tc_source), h.sha256_file(Path(__file__))
    tc = contract(args.tc_source)
    sys.path[:0] = [str(args.noncloud_dir), str(args.rx_dir / "local_agent/scripts")]
    nc = importlib.import_module("run_codex") if any(
        i["stratum"] == "noncloud" for i in pending
    ) else None
    pi = importlib.import_module("lib_pi") if args.arm != "sonnet" else None
    checked_rx = False
    attempts = h.jsonl(attempts_path)
    try:
        for item in pending:
            gate(cfg, c, args.arm)
            if item["stratum"] == "rx_edit" and not checked_rx:
                manifest = h.read_json(cfg.run_dir / "pools/build_manifest.json")
                if h.sha256_file(args.rx_dir / "fixtures/manifest.json") != (
                    manifest["sources"]["rx_fixtures_manifest"]
                ):
                    raise h.Refuse("refusing: rx fixtures manifest hash")
                checked_rx = True
            previous = [r for r in attempts
                        if (r["task_id"], r["arm"]) == (item["id"], args.arm)]
            row = previous[-1] if previous else None
            if row is None or (row["infra_class"] and row["attempt"] < 2):
                first = row["attempt"] + 1 if row else 1
                for number in range(first, 3):
                    gate(cfg, c, args.arm)
                    row = attempt_task(
                        cfg, c, args, item, number, tc, tc_hash, runner_hash, nc, pi,
                    )
                    h.append_row(attempts_path, row)
                    if not row["infra_class"]:
                        break
            h.append_row(target, row)
            counts["done"] += 1
            outcome = "null" if row["hidden_pass"] is None else (
                "pass" if row["hidden_pass"] else "fail"
            )
            counts[outcome] += 1
            for key in ("timed_out", "patch_empty"):
                counts[key] += int(row[key])
    finally:
        if args.arm != "sonnet":
            cfg.ollama.unload(args.arm)
    return counts


def parser():
    ap = argparse.ArgumentParser()
    default = h.parser().get_default("run_dir")
    ap.add_argument("--run-dir", type=Path, default=default)
    ap.add_argument("--arm", required=True, choices=h.arms(h.CONSTANTS, True))
    root = Path.home() / ".rsi/research"
    ap.add_argument("--noncloud-dir", type=Path,
                    default=root / "classifier-v2-release/ab/work/noncloud")
    ap.add_argument("--rx-dir", type=Path, default=root / "routing-experiment-2026-10-01")
    ap.add_argument("--tc-source", type=Path,
                    default=root / "tool-layer/phase_d/amend3/run_codex_tc_v2.py")
    ap.add_argument("--limit-tasks", type=int)
    ap.add_argument("--smoke", action="store_true")
    return ap


def main(argv=None, *, cfg=None):
    args = parser().parse_args(argv)
    cfg = cfg or h.real_cfg(args.run_dir)
    try:
        # Imported research helpers may print model text; never forward their streams.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = run(cfg, args)
    except h.Park as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except h.Refuse as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        # Class name only: these may carry paths or values read from task files.
        print(f"refusing: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
