#!/usr/bin/env python3
"""Build the private, pinned P2.0-T tech_op pool (strata org/ho) without contacting model backends.

Separate from scripts/p20_build_pools.py (P2.0, unchanged in behaviour); reuses its helpers.
H1: no tier filter, strata org (census-exact organic) and ho (every other main-thread row).
H2: sidechain entries are accepted inside a sub-agent transcript file.
H3: organic_action, served_model, row_stamp, from_subagent_file; pack_over_window.
"""
import argparse
import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import p20_build_pools as pb  # noqa: E402
import p20t_requal as harness  # noqa: E402

WINDOW_MARGIN = 0.9  # capability_filter.WINDOW_MARGIN (main); same value, used when it is absent


def irreducible_tokens(messages):
    """The newest user turn, chars/4 (the gate's estimator is capability_filter's; its
    token_budget.estimate_tokens is the same chars/4)."""
    from llm_router.token_budget import estimate_tokens

    last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    return estimate_tokens(last)


def default_fits(messages, window):
    try:
        from llm_router import capability_filter  # present once the gate/P2.4 base is in

        # the gate's own estimate (needs_from_anthropic_body: system + newest user turn + tool
        # schemas). A rebuilt item holds the newest user turn only; the system prompt and the
        # tool schemas are not in a transcript, so this under-counts them (named in the prereg).
        last = next((m for m in reversed(messages) if m["role"] == "user"), None)
        needs = capability_filter.needs_from_anthropic_body({"messages": [last] if last else []})
        return not (needs.tokens and needs.tokens > capability_filter.WINDOW_MARGIN * window)
    except ImportError:
        return irreducible_tokens(messages) <= WINDOW_MARGIN * window


def subagent_evidence(tools):
    """Branch A: the gate's own sub-agent test over the tool names the history shows."""
    from llm_router.proxy import steps

    return steps.is_subagent_request({"tools": [{"name": n} for n in tools]})


def organic_action(data, msg_id, sidechain):
    """The first tool_use of the assistant message(s) carrying msg_id; {final} when none."""
    found = False
    for line in data.splitlines():
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(entry, dict) or (entry.get("isSidechain") and not sidechain):
            continue
        message = entry.get("message")
        if not (isinstance(message, dict) and entry.get("type") == "assistant"
                and message.get("id") == msg_id):
            continue
        found = True
        content = message.get("content")
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") == "tool_use":
                name, args = part.get("name"), part.get("input", {})
                try:
                    key = harness.action_key(json.dumps({"tool": name, "input": args}))
                except (ValueError, KeyError, TypeError):
                    key = None
                target = key[1] if key and len(key) == 2 else None
                return {"tool": name, "target": target}
    return {"final": True} if found else None


def output_check(out):
    absolute = Path(os.path.abspath(out))
    for path in (absolute, *absolute.parents):
        if path.is_symlink():
            raise ValueError("unsafe output")
    pool_dir = absolute / "pools"
    if pool_dir.is_symlink():
        raise ValueError("unsafe output")
    if any(os.path.lexists(p) for p in (pool_dir / "build_manifest.json",
                                       pool_dir / "tech_op.jsonl")):
        raise ValueError("existing output")
    return absolute


def build(args, fits=default_fits):
    from llm_router import session_kind
    from llm_router.proxy import tool_classes
    from llm_router.secret_scrubber import SECRET_PATTERNS

    out = output_check(args.out)
    until, since = pb.instant(args.until), pb.instant(harness.CONSTANTS_T["WINDOW_SINCE"])
    if until <= since:
        raise ValueError("invalid interval")
    paths = {"census_py": args.census_py, "proxy_calls_copy": args.proxy_calls,
             "exec_sessions": args.exec_sessions}
    data = {name: path.read_bytes() for name, path in paths.items()}
    sources = {name: pb.digest(value) for name, value in data.items()}
    sources["secret_patterns"] = pb.digest(json.dumps(sorted(
        (k, p.pattern if hasattr(p, "pattern") else str(p)) for k, p in SECRET_PATTERNS.items()
    )).encode())
    if sources["census_py"] != harness.CONSTANTS_T["CENSUS_PY_SHA256"]:
        raise ValueError("pin mismatch")
    census = pb.load_module("_p20t_pool_census", args.census_py)
    exec_sessions = set(data["exec_sessions"].decode().split())
    rows = pb.jsonl(data["proxy_calls_copy"])
    in_window = [r for r in rows if _in_window(r, since, until)]
    kinds = session_kind.KindIndex(in_window)
    pool, transcripts, seen = [], {}, set()
    counts = {"rows": 0, "by_stratum": {}, "excluded": {}, "from_subagent_file": {},
              "rowstamp_organic": {}, "subagent_branch": args.subagent_branch}

    def exclude(reason):
        counts["excluded"][reason] = counts["excluded"].get(reason, 0) + 1

    for row in rows:
        try:
            stamp = pb.instant(row.get("ts"))
        except (ValueError, OverflowError, OSError):
            exclude("bad_ts")
            continue
        if not since <= stamp < until:
            continue
        session = row.get("session_id")
        reason = None
        kind = kinds.resolve(session, row.get("session_kind")).kind
        stratum = "org" if kind == "organic" else "ho"  # N2: census-exact; untagged is ho
        if session in exec_sessions:
            reason = "exec_session"
        else:
            step, recorded = census.step_kind_of(row)
            category = row.get("prev_tool_class") if recorded else None
            if step != "continuation":
                reason = "not_continuation"
            else:
                if not recorded:
                    category = census.offline_tool_class(row.get("prev_tools"))
                if category == census.BASH_UNRESOLVED:
                    reason = "bash_unresolved"
                elif category != tool_classes.TECHNICAL_OP:
                    reason = "not_technical_op"
                elif row.get("is_main_thread") is False:
                    reason = "not_main_thread"
        if reason:
            exclude(reason)
            continue
        msg_id = row.get("msg_id")
        if not isinstance(session, str) or not session or not isinstance(msg_id, str):
            exclude("no_msg_id")
            continue
        matches = sorted(
            project / (session + ".jsonl") for project in args.transcripts_root.glob("*")
            if project.is_dir() and "/" not in session and "\\" not in session
            and (project / (session + ".jsonl")).is_file())
        if not matches:
            exclude("transcript_missing")
            continue
        main = matches[0]
        candidates = [main] + sorted(p for p in (main.parent / session).glob("**/*.jsonl")
                                     if p.is_file())
        reason, raw, side = "msg_not_found", b"", False
        for path in candidates:
            raw = path.read_bytes()
            transcripts[path.relative_to(args.transcripts_root).as_posix()] = pb.digest(raw)
            side = path != main
            messages, tools, request, reason = pb.transcript(raw, msg_id, sidechain=side)
            if reason != "msg_not_found":
                break
        if reason:
            exclude(reason)
            continue
        if args.subagent_branch == "A" and subagent_evidence([t["name"] for t in tools]):
            exclude("subagent_step")
            continue
        if not fits(messages, harness.local_models.num_ctx(harness.LOCAL)):  # H3
            exclude("pack_over_window")
            continue
        action = organic_action(raw.decode(errors="replace"), msg_id, side)
        identity = "tech-" + pb.digest(f"{session}:{msg_id}".encode())[:16]
        if identity in seen:
            raise ValueError("duplicate")
        seen.add(identity)
        item = dict(
            id=identity, set="tech_op", stratum=stratum, source="real", session=session,
            tier=pb.tier(row.get("tier_complexity")) or "unlabelled", task_type="tech_op",
            request=request, messages=messages, tools=tools, organic_action=action or {"final": True},
            served_model=str(row.get("served_model") or ""), row_stamp=str(row.get("session_kind")),
            from_subagent_file=side)
        texts = [request, json.dumps(item["organic_action"])] + [m["content"] for m in messages]
        if any(re.search(pattern, text) for pattern in SECRET_PATTERNS.values() for text in texts):
            exclude("secret_pattern")
            continue
        pool.append(item)
        counts["rows"] += 1
        counts["by_stratum"][stratum] = counts["by_stratum"].get(stratum, 0) + 1
        if side:
            counts["from_subagent_file"][stratum] = counts["from_subagent_file"].get(stratum, 0) + 1
        if item["row_stamp"] == "organic":
            counts["rowstamp_organic"][stratum] = counts["rowstamp_organic"].get(stratum, 0) + 1
    return finish(args, out, pool, counts, sources, transcripts)


def _in_window(row, since, until):
    try:
        return since <= pb.instant(row.get("ts")) < until
    except (ValueError, OverflowError, OSError):
        return False


def finish(args, out, pool, counts, sources, transcripts):
    encoded = b"".join((json.dumps(row, sort_keys=True) + "\n").encode() for row in pool)
    git = pb.subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                            stdin=pb.subprocess.DEVNULL, capture_output=True, timeout=30, check=True)
    manifest = dict(built_utc=args.until, since=harness.CONSTANTS_T["WINDOW_SINCE"],
                    until=args.until, repo_git_sha=git.stdout.decode().strip(), sources=sources,
                    transcripts=transcripts, counts=counts,
                    pools={"tech_op": pb.digest(encoded)})
    payload = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    output_check(out)
    if not out.parent.is_dir():
        raise ValueError("output parent missing")
    out.mkdir(mode=0o700, exist_ok=True)
    (out / "pools").mkdir(mode=0o700, exist_ok=True)
    for directory in (out, out / "pools"):
        directory.chmod(0o700)
    for path, value in ((out / "pools/tech_op.jsonl", encoded),
                        (out / "pools/build_manifest.json", payload)):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
    return dict(counts=counts, pools=manifest["pools"], manifest_sha256=pb.digest(payload))


def main(argv=None):
    parser = argparse.ArgumentParser()
    base = "~/.rsi/research/"
    for name, default in {
        "out": base + "primary-plan/v16/requal/t1",
        "census-py": base + "primary-plan/v16/scripts/census.py",
        "exec-sessions": base + "primary-plan/exec_sessions.txt",
    }.items():
        parser.add_argument("--" + name, type=lambda p: Path(p).expanduser(), default=default)
    parser.add_argument("--proxy-calls", type=Path, required=True)
    parser.add_argument("--transcripts-root", type=Path, required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--subagent-branch", choices=("A", "B"), default="B")
    args = parser.parse_args(argv)
    engine_mode = os.environ.get("LLM_ROUTER_ENGINE_MODE")
    os.environ["LLM_ROUTER_ENGINE_MODE"] = "off"
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = build(args)
    except Exception as exc:
        # Privacy: only the exception class, a fixed ValueError reason and the line number.
        tb, line = exc.__traceback__, 0
        while tb is not None:
            if tb.tb_frame.f_code.co_filename == __file__:
                line = tb.tb_lineno
            tb = tb.tb_next
        fixed = {"pin mismatch", "existing output", "unsafe output", "invalid interval",
                 "duplicate", "invalid timestamp", "output parent missing"}
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) in fixed else ""
        print(f"pool build refused: {type(exc).__name__} {reason} line {line}".strip(),
              file=sys.stderr)
        return 2
    finally:
        sys.dont_write_bytecode = previous
        if engine_mode is None:
            os.environ.pop("LLM_ROUTER_ENGINE_MODE", None)
        else:
            os.environ["LLM_ROUTER_ENGINE_MODE"] = engine_mode
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
