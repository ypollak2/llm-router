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
import time
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "p20_code_requal", Path(__file__).with_name("p20_requal.py"),
)
h = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = h
SPEC.loader.exec_module(h)


def run_agent_process(argv, *, input=None, text=True, capture_output=True, timeout=900,
                      cwd=None, env=None, check=False):
    """F117: `subprocess.run` for the coding agent, in its own session (process group), with a
    unique env tag. On return, success or timeout, the group, every descendant seen while it
    ran and every tagged process are killed and gone, so the scrub and purge never race a
    surviving grandchild. Same result / TimeoutExpired shape as subprocess.run."""
    import uuid
    tag = uuid.uuid4().hex
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text,
        cwd=cwd, env={**(env or {}), h.AGENT_TAG_ENV: tag}, start_new_session=True,
    )
    known: set[int] = set()
    deadline = time.monotonic() + timeout
    try:
        pending = input
        while True:
            known |= h.descendant_pids(proc.pid)
            try:
                out, err = proc.communicate(pending, timeout=min(0.5, max(
                    0.01, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                pending = None
                # a grandchild may hold the pipes after the agent exited: stop the tree, then
                # collect what was written; only a still-running agent is a timeout
                late = proc.poll() is None and time.monotonic() >= deadline
                if late or proc.poll() is not None:
                    h.stop_agent_processes(proc.pid, tag, known)
                    out, err = proc.communicate()
                    if late:
                        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
                    break
    finally:
        h.stop_agent_processes(proc.pid, tag, known)
        proc.wait()
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


_claude_run = run_agent_process


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


def git(work, *args, sandbox=False):
    """F117: text of one hardened git command (see h.run_git). `sandbox=True` for any repo the
    agent has touched: its .git/config is then never read."""
    return h.run_git(work, *args, sandbox=sandbox).decode(errors="replace").strip()


def git_bytes(work, *args):
    """Raw stdout of a git command on an agent-touched repo (non-UTF-8 safe, capped)."""
    return h.run_git(work, *args, sandbox=True)


def empty_patch(work, parent):
    differs = git_bytes(work, "diff", "--name-only", parent)
    return not differs.strip() and not git(
        work, "ls-files", "--others", "--exclude-standard", sandbox=True)


# Every Pi thinking level but "off" is unsupported, and "off" is sent as reasoning_effort "none":
# Ollama's /v1 endpoint thinks by default and stops on "none" (AMEND2; production sends
# think:false).
PI_THINKING_LEVELS = {"off": "none", "minimal": None, "low": None, "medium": None,
                      "high": None, "xhigh": None, "max": None}


def pi_config(run_dir, arm):
    root = run_dir / "pi_config"
    h.write_json(root / "models.json", {"providers": {"ollama": {
        "baseUrl": "http://localhost:11434/v1", "api": "openai-completions", "apiKey": "ollama",
        "models": [{
            "id": arm, "name": arm, "input": ["text"], "output": ["text"],
            "contextWindow": h.local_models.num_ctx(arm), "maxOutput": 8192,
            "reasoning": True, "thinkingLevelMap": PI_THINKING_LEVELS,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        }],
    }}})
    return root


def grader_env(home):
    """F113/F115: agent-written code is graded on the agent PATH, HOME at `home`."""
    return {"PATH": h.agent_path(os.environ.get("PATH", "")), "HOME": str(home),
            "PYTHONDONTWRITEBYTECODE": "1", **h.AGENT_FIXED_ENV}


def _text(value):
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def purge_inside(run_dir, *paths):
    """Delete each path only if it lies strictly inside the run dir the harness created."""
    root = Path(run_dir).resolve()
    for path in paths:
        target = Path(path)
        if target.is_symlink() or not target.exists():
            continue
        real = target.resolve()
        if real != root and root in real.parents:
            _open_permissions(root, real)
            shutil.rmtree(real, ignore_errors=True)


def _chmod_nofollow(parent_fd, name):
    """chmod 0700 the directory `name` under `parent_fd`, never through a symlink."""
    try:
        os.chmod(name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except NotImplementedError:
        pass  # Linux: no lchmod; go through an O_PATH fd instead
    except OSError:
        return False
    o_path = getattr(os, "O_PATH", None)
    if o_path is None:
        return False
    try:
        pfd = os.open(name, o_path | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=parent_fd)
    except OSError:
        return False
    try:
        os.chmod(f"/proc/self/fd/{pfd}", 0o700)
        return True
    except OSError:
        return False
    finally:
        os.close(pfd)


def _open_dir(parent_fd, name):
    """fd of the real directory `name` under `parent_fd` (never a symlink), unlocking a
    chmod 000 one first; None when it is not a directory or cannot be opened."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except PermissionError:
        if not _chmod_nofollow(parent_fd, name):
            return None
    except OSError:
        return None
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError:
        return None


def _open_permissions(root, target):
    """F117: an agent's chmod 000 directory cannot be listed or emptied; rmtree would leave it
    (and a token inside) behind silently. Give each real directory from `root` down to and
    under `target` owner rwx first. Everything goes through directory fds with O_NOFOLLOW and
    fchmod / chmod-by-fd, never a path string, so a symlink swapped in by a live agent cannot
    redirect the chmod outside the run dir."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in Path(target).relative_to(root).parts:
            child = _open_dir(fd, part)
            os.close(fd)
            fd = child
            if fd is None:
                return
        _chmod_tree(fd)
    except (OSError, ValueError):
        pass
    finally:
        if fd is not None:
            os.close(fd)


def _chmod_tree(fd):
    try:
        os.fchmod(fd, 0o700)
        names = os.listdir(fd)
    except OSError:
        return
    for name in names:
        child = _open_dir(fd, name)
        if child is None:
            continue
        try:
            _chmod_tree(child)
        finally:
            os.close(child)


def scrub_claude_artifacts(token, work, home, output, run_dir, baseline=None):
    """F115: the Claude arm has Bash and the OAuth token in its env; refuse before anything
    it produced (output, work files, patch, scratch HOME) is stored if it carries the token.
    `output` is the call's stdout+stderr text (also on a timeout). No token to look for while
    the Claude arm is active is itself a refusal (fail closed). On a hit the work and scratch
    HOME copies are deleted (only inside run_dir) and the refusal tells the owner what to do."""
    if not token:
        raise h.Refuse("refusing: no token to scan for while the Claude arm is active")
    try:
        h.scan_for_token(token, "agent output", data=output)
        h.scan_for_token(token, "work files", root=work)
        h.scan_for_token(token, "agent scratch home", root=home)
        # F117: raw objects the agent added (baseline = object ids before it ran), not `log -p`
        # over the whole shared mirror: covers merges, tags, unreachable commits, replace refs
        h.scan_for_token(token, "patch", data=git_bytes(work, "diff", "HEAD"))
        h.scan_new_git_objects(token, work, baseline)
    except h.Refuse as exc:
        purge_inside(run_dir, work, home)
        raise h.Refuse(
            f"{exc}; work and agent_home copies were deleted if inside the run dir, "
            f"but the run dir {run_dir} may still hold the token (agent output, ledgers): "
            "purge it, and rotate the token (claude setup-token)"
        ) from None
    except Exception:
        # F117: anything unexpected from the scan must still purge, never skip it.
        purge_inside(run_dir, work, home)
        raise h.Refuse("refusing: artifact scan failed; work and agent_home copies were "
                       "deleted if inside the run dir; purge the run dir and rotate the token"
                       ) from None


def agent(cfg, c, arm, ident, attempt, prompt, work, pi):
    # F104: the agent's HOME is a fresh per-attempt scratch dir outside `work`, never the
    # owner's real HOME (it deleted the live install twice: F86, F86-2).
    home = h.scratch_home(
        cfg.run_dir / "agent_home" / safe(arm) / safe(ident) / f"a{attempt}"
    )
    if arm != "sonnet":
        pi.PI_MODEL = arm
        # AMEND3 (D-202): lock + quiet gate before every local call, any time of day.
        with h.local_call(cfg, c, arm, {"stage": "code", "set": "code", "item_id": ident,
                                        "attempt": attempt}):
            with h.scrubbed_environ(h.agent_env(home, run_dir=cfg.run_dir)):
                result = pi.run_pi(prompt, cwd=work, timeout_s=900)
        timed_out = bool(result["timed_out"])
        infra = not timed_out and (
            result["returncode"] != 0 or result["n_events"] == 0
        )
        return timed_out, "agent_error" if infra else None
    h.quota_guard(cfg, c)
    start = cfg.now()
    cost, served, error, timed_out = 0, "", None, False
    env = h.claude_env(home, cfg.run_dir)
    output = ""
    baseline = h.git_object_ids(work)  # F117: what the agent adds is what gets scanned
    try:
        result = _claude_run(
            h.claude_argv(c["CLAUDE_SONNET_ID"], cfg.run_dir, tools=None)
            + ["--permission-mode", "acceptEdits", "--allowedTools",
               "Read,Edit,Write,Glob,Grep,Bash"],
            input=prompt, text=True, capture_output=True, timeout=900, cwd=work,
            env=env, check=False,
        )
        output = _text(result.stdout) + _text(result.stderr)
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
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        output = _text(exc.stdout) + _text(exc.stderr)
    except (OSError, ValueError, TypeError, AttributeError):
        error = "claude_error"
    finally:
        # always scanned, also after a timeout or a parse failure, before grading or storing
        scrub_claude_artifacts(env.get(h.CLAUDE_AUTH_ENV[0]), work, home, output, cfg.run_dir,
                              baseline)
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
    work, hidden, meta, clone = None, None, None, None
    tag = "p20_" + safe(args.arm)
    try:
        ref = item["task_ref"]
        if item["stratum"] == "noncloud":
            metas = {m["task_id"]: m for m in h.jsonl(args.noncloud_dir / "tasks_meta.jsonl")}
            task_id = ref["task_id"]
            meta = metas[task_id]
            clone = Path(nc.ensure_clone())
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
                # F113: agent-written code runs on the agent PATH with an interpreter that
                # cannot import llm_router (grader_python enforces it), never the run venv.
                genv = grader_env(work)
                result = subprocess.run(
                    [h.grader_python(genv), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                     test_path(ref["test"])],
                    cwd=work, stdin=subprocess.DEVNULL, capture_output=True, timeout=120,
                    env=genv, check=False,
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
        if meta is not None and work is not None and clone is not None:
            try:
                # F117: from the trusted clone, never `-C work`: work/.git is a pointer file
                # the agent can rewrite to another repo's config
                git(clone, "worktree", "remove", "--force", str(work))
            except (OSError, subprocess.SubprocessError, h.Refuse):
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
    if any(i["stratum"] == "rx_edit" for i in pending):
        h._GRADER_PYTHON.clear()  # a run probes once, then caches per PATH
        h.grader_python(grader_env(cfg.run_dir))  # F115 preflight: refuse before any agent runs
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
    if pi is not None:
        pi.PI_CONFIG_DIR = str(pi_config(cfg.run_dir, args.arm))
    checked_rx = False
    attempts = h.jsonl(attempts_path)
    try:
        for item in pending:
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
            h.unload_local(cfg, args.arm)
    return counts


def parser():
    ap = argparse.ArgumentParser()
    default = h.parser().get_default("run_dir")
    ap.add_argument("--run-dir", type=Path, default=default)
    ap.add_argument("--arm", required=True, choices=h.arms(h.STATIC, True))
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
