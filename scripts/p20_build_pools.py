#!/usr/bin/env python3
"""Build private, pinned P2.0 pools without contacting model backends."""

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone


REPO = Path(__file__).resolve().parents[1]
SINCE = "2026-09-28T00:00:00Z"
SETS = ("qa", "tech_op", "code")
PINS = {
    "rx_packets": "RX_PACKETS_SHA256",
    "rx_results_p2b": "RX_RESULTS_P2B_SHA256",
    "rx_grades_grader1": "RX_GRADES_GRADER1_SHA256",
    "rx_corpus": "RX_CORPUS_SHA256",
    "rx_real_corpus": "RX_REAL_CORPUS_SHA256",
    "rx_edit_fixtures_tree": "RX_EDIT_FIXTURES_TREE_SHA256",
    "gt_split": "GT_SPLIT_SHA256",
    "gt_manifest": "GT_MANIFEST_SHA256",
    "noncloud_task_ids": "NONCLOUD_TASK_IDS_SHA256",
    "census_py": "CENSUS_PY_SHA256",
}
DESCRIPTIONS = {
    "Read": "Read a file.",
    "Grep": "Search file contents.",
    "Glob": "Find files by path pattern.",
    "Bash": "Run a shell command.",
    "Edit": "Edit an existing file.",
    "Write": "Write a file.",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_module(name, path):
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = previous


def tree_digest(root):
    entries = []
    for directory in sorted(root.glob("edit-*")):
        if not directory.is_dir():
            continue
        for current, dirs, files in os.walk(directory, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
            for name in files:
                path = Path(current) / name
                if name.endswith(".pyc") or not path.is_file():
                    continue
                relative = path.relative_to(root).as_posix()
                entries.append((relative, digest(path.read_bytes())))
    return digest(b"".join(
        name.encode() + b"\0" + value.encode() + b"\n" for name, value in sorted(entries)
    ))


def jsonl(data):
    return [json.loads(line) for line in data.splitlines() if line.strip()]


def rows(value):
    if isinstance(value, list):
        return value
    for key in ("rows", "items", "tasks", "fixtures"):
        if key in value:
            return value[key]
    raise ValueError("invalid rows")


def index(records, key):
    result = {}
    for record in records:
        identity = record[key]
        if identity in result:
            raise ValueError("duplicate")
        result[identity] = record
    return result


def plain(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(part.get("text", "")) for part in value
            if isinstance(part, dict) and part.get("type", "text") == "text"
        )
    return "" if value is None else str(value)


def prior_context(turns):
    return "\n".join(
        f"{turn.get('role', turn.get('type', ''))}: "
        f"{plain(turn.get('text', turn.get('content', '')))}"
        if isinstance(turn, dict) else str(turn)
        for turn in turns
    )


def tier(value, labelled=False):
    mapping = {"haiku": "simple", "sonnet": "moderate", "opus": "complex"} if labelled else {
        "simple": "simple", "moderate": "moderate",
        "complex": "complex", "deep_reasoning": "complex",
    }
    return mapping.get(value, "")


def instant(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("invalid timestamp")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def transcript(data, msg_id):
    entries = []
    for line in data.splitlines():
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        role = entry.get("type")
        if role not in ("user", "assistant"):
            continue
        if role == "assistant" and message.get("id") == msg_id:
            break
        entries.append((role, message.get("content", "")))
    else:
        return None, None, None, "msg_not_found"
    messages, names, request = [], set(DESCRIPTIONS), None
    for role, content in entries:
        if isinstance(content, str):
            rendered = content
            if role == "user":
                request = content
        else:
            parts = [part for part in content if isinstance(part, dict)]
            rendered_parts = []
            for part in parts:
                kind = part.get("type")
                if kind == "text":
                    rendered_parts.append(str(part.get("text", "")))
                elif kind == "tool_result" and role == "user":
                    rendered_parts.append("[tool_result] " + plain(part.get("content", "")))
                elif kind == "tool_use" and role == "assistant":
                    name = part["name"]
                    names.add(name)
                    rendered_parts.append(
                        f"[tool_use {name}] {json.dumps(part.get('input', {}))}"
                    )
            rendered = "\n".join(rendered_parts)
            if role == "user" and any(p.get("type") == "text" for p in parts):
                if not any(p.get("type") == "tool_result" for p in parts):
                    request = plain(parts)
        messages.append({"role": role, "content": rendered})
    tools = [
        {"name": name, "description": DESCRIPTIONS.get(
            name, "tool used earlier in this session"
        )} for name in sorted(names)
    ]
    return messages, tools, request, None if request is not None else "no_human_ask"


def output_check(out):
    absolute = Path(os.path.abspath(out))
    for path in (absolute, *absolute.parents):
        if path.is_symlink():
            raise ValueError("unsafe output")
    pool_dir = absolute / "pools"
    if pool_dir.is_symlink():
        raise ValueError("unsafe output")
    targets = [pool_dir / "build_manifest.json"]
    targets.extend(pool_dir / f"{name}.jsonl" for name in SETS)
    if any(os.path.lexists(path) for path in targets):
        raise ValueError("existing output")
    return absolute


def build(args):
    from llm_router.proxy import backends
    from llm_router.qa_policy import QA_TASK_TYPES
    from llm_router.secret_scrubber import SECRET_PATTERNS
    from llm_router.proxy import tool_classes

    out = output_check(args.out)
    until = instant(args.until)
    if until <= instant(SINCE):
        raise ValueError("invalid interval")
    rx, gt, nc = args.rx_dir, args.gt_dir, args.noncloud_dir
    paths = {
        "rx_packets": rx / "p2b_work/packets.jsonl",
        "rx_results_p2b": rx / "results_p2b_qwen38.md",
        "rx_grades_grader1": rx / "p2b_work/grades_grader1.jsonl",
        "rx_corpus": rx / "corpus.jsonl",
        "rx_real_corpus": rx / "real_corpus.jsonl",
        "gt_split": gt / "split.json", "gt_manifest": gt / "manifest.json",
        "noncloud_task_ids": nc / "task_ids.json", "census_py": args.census_py,
        "rx_fixtures_manifest": rx / "fixtures/manifest.json",
        "gt_items": gt / "items.jsonl", "noncloud_tasks_meta": nc / "tasks_meta.jsonl",
        "proxy_calls_copy": args.proxy_calls, "exec_sessions": args.exec_sessions,
        "prereg": args.prereg,
    }
    data = {name: path.read_bytes() for name, path in paths.items()}
    sources = {name: digest(value) for name, value in data.items() if name != "prereg"}
    sources["rx_edit_fixtures_tree"] = tree_digest(rx / "fixtures")
    sources["secret_patterns"] = digest(json.dumps(sorted(
        (k, p.pattern if hasattr(p, "pattern") else str(p)) for k, p in SECRET_PATTERNS.items()
    )).encode())
    harness = load_module("_p20_pool_harness", REPO / "scripts/p20_requal.py")
    constants = harness.parse_constants(data["prereg"].decode())
    if any(sources[name] != constants.get(pin) for name, pin in PINS.items()):
        raise ValueError("pin mismatch")
    census = load_module("_p20_pool_census", args.census_py)
    pools = {name: [] for name in SETS}
    counts = {name: {"rows": 0, "by_stratum": {}, "excluded": {}} for name in SETS}
    seen, transcripts = set(), {}

    def exclude(group, reason):
        excluded = counts[group]["excluded"]
        excluded[reason] = excluded.get(reason, 0) + 1

    def add(group, stratum, identity, source, session, complexity, task_type, **fields):
        if identity in seen:
            raise ValueError("duplicate")
        seen.add(identity)
        texts = [fields.get("request", ""), fields.get("context", "")]
        texts.extend(message["content"] for message in fields.get("messages", []))
        if group != "code" and any(
            re.search(pattern, text) for pattern in SECRET_PATTERNS.values() for text in texts
        ):
            exclude(group, "secret_pattern")
            return
        pools[group].append(dict(
            id=identity, set=group, stratum=stratum, source=source, session=session,
            tier=complexity, task_type=task_type, **fields,
        ))
        count = counts[group]
        count["rows"] += 1
        count["by_stratum"][stratum] = count["by_stratum"].get(stratum, 0) + 1

    packets = list(index(jsonl(data["rx_packets"]), "packet_id").values())
    graded = set(index(jsonl(data["rx_grades_grader1"]), "packet_id"))
    corpus = index(jsonl(data["rx_corpus"]), "id")
    real = index(jsonl(data["rx_real_corpus"]), "id")
    eligible = [p for p in packets if p["kind"] == "main" and p["task_type"] in QA_TASK_TYPES]
    membership = {"main_qa_packets": len(eligible),
                  "main_qa_graded": sum(p["packet_id"] in graded for p in eligible)}
    membership["difference"] = membership["main_qa_packets"] - membership["main_qa_graded"]
    for packet in eligible:
        if packet["packet_id"] not in graded:
            continue
        ref = packet["ref_id"]
        if ref not in corpus:
            exclude("qa", "ref_missing")
            continue
        item = corpus[ref]
        source = packet["source"]
        session = real.get(ref, {}).get("_orig_session_id") or "rx-real"
        add("qa", "rx", "rx-" + packet["packet_id"], source,
            session if source == "real" else "rx-gen", tier(packet.get("complexity")),
            packet["task_type"], request=item["text"],
            context=prior_context(item.get("prior_turns", [])))
    items = index(jsonl(data["gt_items"]), "id")
    for meta in rows(json.loads(data["gt_manifest"])):
        if meta.get("split") != "heldout" or meta.get("kind") != "organic":
            continue
        item = items[meta["id"]]
        task_type = backends._classify_class(item["prompt"], capabilities=False)[0]
        if task_type not in QA_TASK_TYPES:
            exclude("qa", "not_qa")
            continue
        add("qa", "gt", meta["id"], "real", item["session_id"],
            tier(meta.get("tier_opus"), True), task_type,
            request=item["prompt"], context=item.get("context") or "")
    exec_sessions = set(data["exec_sessions"].decode().split())
    for row in jsonl(data["proxy_calls_copy"]):
        try:
            stamp = instant(row.get("ts"))
        except (ValueError, OverflowError, OSError):
            exclude("tech_op", "bad_ts")
            continue
        if not instant(SINCE) <= stamp < until:
            continue
        session = row.get("session_id")
        reason = None
        if row.get("session_kind") != "organic":
            reason = "not_organic"
        elif session in exec_sessions:
            reason = "exec_session"
        else:
            kind, recorded = census.step_kind_of(row)
            category = row.get("prev_tool_class") if recorded else None
            if kind != "continuation":
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
            exclude("tech_op", reason)
            continue
        msg_id = row.get("msg_id")
        if not isinstance(session, str) or not session or not isinstance(msg_id, str):
            exclude("tech_op", "no_msg_id")
            continue
        matches = sorted(
            project / (session + ".jsonl") for project in args.transcripts_root.glob("*")
            if project.is_dir() and "/" not in session and "\\" not in session
            and (project / (session + ".jsonl")).is_file()
        )
        if not matches:
            exclude("tech_op", "transcript_missing")
            continue
        path = matches[0]
        raw = path.read_bytes()
        transcripts[path.relative_to(args.transcripts_root).as_posix()] = digest(raw)
        messages, tools, request, reason = transcript(raw, msg_id)
        if reason:
            exclude("tech_op", reason)
            continue
        identity = "tech-" + digest(f"{session}:{msg_id}".encode())[:16]
        add("tech_op", "ge1", identity, "real", session, tier(row.get("tier_complexity")),
            "tech_op", request=request, messages=messages, tools=tools)
    metadata = index(jsonl(data["noncloud_tasks_meta"]), "task_id")
    tasks = json.loads(data["noncloud_task_ids"])["tasks"]
    index(tasks, "task_id")
    for task in tasks:
        meta = metadata[task["task_id"]]
        parent = meta["parent_sha"]
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", parent):
            exclude("code", "checkout_unavailable")
            continue
        try:
            result = subprocess.run(
                ["git", "-C", str(nc / "mirror.git"), "cat-file", "-e", parent + "^{commit}"],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            exclude("code", "checkout_unavailable")
            continue
        if result.returncode:
            exclude("code", "checkout_unavailable")
            continue
        add("code", "noncloud", "nc-" + task["task_id"], "real", "noncloud",
            tier(task.get("claude_truth"), True) or "unlabelled", "code",
            task_ref=dict(stratum="noncloud", task_id=task["task_id"],
                          commit_sha=meta["commit_sha"], parent_sha=parent))
    fixtures = rows(json.loads(data["rx_fixtures_manifest"]))
    index(fixtures, "name")
    for fixture in fixtures:
        if not (rx / "fixtures" / fixture["name"]).is_dir():
            exclude("code", "fixture_missing")
            continue
        add("code", "rx_edit", "rx-edit-" + fixture["name"], "real", "rx_edit",
            "unlabelled", "code", task_ref=dict(
                stratum="rx_edit", **{k: fixture[k] for k in ("name", "path", "test", "idx")}
            ))
    return finish(args, out, pools, counts, sources, transcripts, membership)


def finish(args, out, pools, counts, sources, transcripts, membership):
    encoded = {name: b"".join(
        (json.dumps(row, sort_keys=True) + "\n").encode() for row in pools[name]
    ) for name in SETS}
    hashes = {name: digest(value) for name, value in encoded.items()}
    git = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                         stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=True)
    manifest = dict(built_utc=args.until, since=SINCE, until=args.until,
                    repo_git_sha=git.stdout.decode().strip(), sources=sources,
                    transcripts=transcripts, counts=counts, rx_membership=membership, pools=hashes)
    payload = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    output_check(out)
    if not out.parent.is_dir():
        raise ValueError("output parent missing")
    out.mkdir(mode=0o700, exist_ok=True)
    (out / "pools").mkdir(mode=0o700, exist_ok=True)
    for directory in (out, out / "pools"):
        directory.chmod(0o700)
    outputs = {out / "pools" / f"{name}.jsonl": value for name, value in encoded.items()}
    outputs[out / "pools/build_manifest.json"] = payload
    for path, value in outputs.items():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
    return dict(counts=counts, pools=hashes, manifest_sha256=digest(payload))


def main(argv=None):
    parser = argparse.ArgumentParser()
    base = "~/.rsi/research/"
    for name, default in {
        "out": base + "primary-plan/v16/requal",
        "rx-dir": base + "routing-experiment-2026-10-01",
        "gt-dir": base + "primary-plan/v16/gt500",
        "noncloud-dir": base + "classifier-v2-release/ab/work/noncloud",
        "census-py": base + "primary-plan/v16/scripts/census.py",
        "exec-sessions": base + "primary-plan/exec_sessions.txt",
        "prereg": str(REPO / "docs/prereg/PREREG-P20-requal.md"),
    }.items():
        parser.add_argument("--" + name, type=lambda p: Path(p).expanduser(), default=default)
    parser.add_argument("--proxy-calls", type=Path, required=True)
    parser.add_argument("--transcripts-root", type=Path, required=True)
    parser.add_argument("--until", required=True)
    args = parser.parse_args(argv)
    engine_mode = os.environ.get("LLM_ROUTER_ENGINE_MODE")
    os.environ["LLM_ROUTER_ENGINE_MODE"] = "off"
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = build(args)
    except Exception as exc:
        # Privacy: only the exception class, the fixed ValueError reasons raised by this module,
        # and the line number are printed; never a value read from a source file.
        line = exc.__traceback__.tb_lineno if exc.__traceback__ else 0
        tb = exc.__traceback__
        while tb is not None:
            if tb.tb_frame.f_code.co_filename == __file__:
                line = tb.tb_lineno
            tb = tb.tb_next
        fixed = {"pin mismatch", "existing output", "unsafe output", "invalid interval",
                 "duplicate", "invalid rows", "invalid timestamp", "output parent missing"}
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
