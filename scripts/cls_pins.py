#!/usr/bin/env python3
"""CLS-v16 pins (build B-L): PINS-STATIC (what exists at the freeze) and PINS-RUN (append-only hash chain).

  cls_pins.py pins --static --out PINS-STATIC.json --engine-commit SHA [--repo R] ...
  cls_pins.py pins --run --entry NAME --files F [F...] [--rules-tier FILE] [--pins-run pins_run.jsonl]
  cls_pins.py pins --verify PINS-STATIC.json [--pins-run pins_run.jsonl] [--freeze-record F]
  cls_pins.py pins --freeze-line PINS-STATIC.json

A STATIC pin that cannot be computed is an ERROR (exit 9) naming it. Nothing is omitted silently.
`--verify` checks STATIC always and RUN entries once any exist; the .env sha256 is never a hard pin
(it logs env_sha_at_use and warns env_changed_since_freeze). `--verify` also checks the sha256 of the
PINS-STATIC file itself against the `pins_sha256` line of the freeze record (PREREG section 15:
`docs/prereg/PREREG-CLS-DEV.sha256` under the pinned repo): fail closed once that record exists, a named
warning before the freeze. Output: counts and key names only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cls_send_lib as S  # noqa: E402

SCRIPT_NAMES = ["cls_eval.py", "cls_dev_run.py", "cls_dev_lib.py", "cls_label_run.py", "cls_gen_run.py",
                "cls_resemble.py", "cls_send_lib.py", "cls_pins.py"]
GENERATOR_V1_FILES = ["tmpl_gpt.v1.txt", "tmpl_opus.v1.txt", "task_tool_mask.v1.json", "projects.v1.json",
                      "intent_phrases.v1.json", "meta_patterns.v1.json"]
OLLAMA_MODELS = ["nimble:9b", "qwen3.5:latest", "qwen3-coder:30b", "nomic-embed-text"]
RUBRIC_MD5 = "1bcb3f441a0c9d056b3343a9d47c5565"
#: gt500.py (the GE3 labeller helpers: prompt_view, valid_label, extract_json, load_rubric), pinned by sha as
#: in PREREG-CLS-DEV section 2 and scripts/p17d_synthetic.py GT500_SHA256.
GT500_SHA256 = "9cde774dfe93387d6ee3dfcbd1b5481a537f265e5961d82f85a87d29d6328959"
#: Where p17d_synthetic.py finds gt500.py (GT_SCRIPTS): under the REAL home, never $HOME.
DEFAULT_GT500_PY = str(Path(pwd.getpwuid(os.getuid()).pw_dir) / ".rsi" / "research" / "primary-plan" / "v16" / "gt500"
                       / "scripts" / "gt500.py")
FREEZE_RECORD_REL = "docs/prereg/PREREG-CLS-DEV.sha256"
CLAUDE_FLAGS = ["--safe-mode", "--settings", "--tools", "--strict-mcp-config", "--no-session-persistence",
                "--setting-sources", "--output-format", "--model"]
CODEX_FLAGS = ["-s", "-C", "--skip-git-repo-check", "--ephemeral", "--json", "-m"]
SESSION_LOG_DIRS = {"claude": "~/.claude/projects", "codex": "~/.codex/sessions"}
RULES_TIER_SCHEMA = "rules_tier/1"
ZERO_SHA = "0" * 64


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"


def _default_run(argv, **kw) -> str:
    r = subprocess.run(argv, capture_output=True, timeout=60, shell=False, **kw)
    return (r.stdout + r.stderr).decode("utf-8", errors="replace")


def _default_http_get(url: str) -> dict:
    host = urllib.parse.urlparse(url).hostname
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("non-local host refused")
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 (127.0.0.1 only)
        return json.loads(r.read().decode("utf-8"))


def _git_show(repo, commit, path) -> bytes:
    r = subprocess.run(["git", "-C", str(repo), "show", "%s:%s" % (commit, path)], capture_output=True, timeout=60,
                       shell=False)
    if r.returncode != 0:
        raise ValueError("git show failed for %s" % path)
    return r.stdout


def _flag_pattern(fl: str):
    """A DEFINITION line of the flag only: `--flag` (optionally after `-x, `) or `-x` at the start of the
    line. A flag named inside another option's description does not count."""
    if fl.startswith("--"):
        return re.compile(r"^\s*(?:-\w,\s*)?" + re.escape(fl) + r"(?![\w-])")
    return re.compile(r"^\s*" + re.escape(fl) + r"(?=,|\s|$)")


def _flag_lines(help_text: str, flags: list[str]) -> tuple[dict, list[str]]:
    lines, missing = {}, []
    for fl in flags:
        pat = _flag_pattern(fl)
        hit = next((ln.strip() for ln in help_text.splitlines() if pat.search(ln)), None)
        if hit is None:
            missing.append(fl)
        else:
            lines[fl] = hit
    return lines, missing


def compute_static(params: dict, *, runner=_default_run, http_get=_default_http_get) -> tuple[dict, list[str]]:
    """Compute every STATIC pin from `params`. Returns (pins, errors); errors are key names."""
    pins: dict = {}
    errors: list[str] = []
    repo = Path(params["repo"])

    def sec(key, fn):
        try:
            pins[key] = fn()
        except (OSError, ValueError, KeyError, subprocess.SubprocessError, S.CLSExit) as e:
            errors.append(key if not str(e).startswith("flag:") else str(e)[5:])

    sec("scripts", lambda: {n: S.sha256_file(Path(params["scripts_dir"]) / n) for n in SCRIPT_NAMES})

    def engine():
        c = params["engine_commit"]
        return {"commit": c,
                "engine_py_sha256": S.sha256_bytes(_git_show(repo, c, "src/llm_router/engine.py")),
                "shadow_eval_py_sha256": S.sha256_bytes(_git_show(repo, c, "src/llm_router/shadow_eval.py"))}
    sec("engine", engine)

    def rubric():
        b = Path(params["rubric_py"]).read_bytes()
        md5 = hashlib.md5(b).hexdigest()  # noqa: S324 (identification, matches the prereg's rubric md5)
        if md5 != RUBRIC_MD5:
            raise ValueError("rubric md5 differs from the expected value")
        return {"md5": md5, "sha256": S.sha256_bytes(b), "path": str(params["rubric_py"])}
    sec("rubric", rubric)

    def gt500():
        b = Path(params["gt500_py"]).read_bytes()
        sha = S.sha256_bytes(b)
        if sha != GT500_SHA256:
            raise ValueError("gt500.py sha256 differs from the expected value")
        return {"sha256": sha, "path": str(params["gt500_py"])}
    sec("gt500", gt500)

    def denylist():
        p = params["denylist"]
        return {"path": str(p), "sha256": S.sha256_file(p), "entries": len(S.load_denylist(p))}
    sec("denylist", denylist)

    def env():
        p = params["env_file"]
        return {"path": str(p), "sha256": S.sha256_file(p), "qualifying_values": S.load_env_values(p)[1]}
    sec("env", env)

    def ollama():
        base = params["ollama_url"].rstrip("/")
        ver = http_get(base + "/api/version")["version"]
        tags = {m["name"]: m["digest"] for m in http_get(base + "/api/tags")["models"]}
        digs = {}
        for m in params["ollama_models"]:
            if m not in tags:
                raise ValueError("model absent")
            digs[m] = tags[m]
        return {"version": ver, "digests": digs}
    sec("ollama", ollama)

    def bge():
        d = Path(params["bge_snapshot_dir"])
        if not d.is_dir():
            raise ValueError("snapshot dir absent")
        return {"snapshot_dir_name": d.name}
    sec("bge_snapshot", bge)

    def claude():
        help_text = runner([params["claude_bin"], "--help"])
        lines, missing = _flag_lines(help_text, CLAUDE_FLAGS)
        if missing:
            raise ValueError("flag:claude_flag" + missing[0])
        return {"version": runner([params["claude_bin"], "--version"]).strip(),
                "help_sha256": S.sha256_bytes(help_text.encode()), "flag_lines": lines,
                "argv_template": S.claude_argv("<MODEL>", "<SETTINGS>")}
    sec("claude_cli", claude)

    def codex():
        help_text = runner([params["codex_bin"], "exec", "--help"])
        lines, missing = _flag_lines(help_text, CODEX_FLAGS)
        if missing:
            raise ValueError("flag:codex_flag" + missing[0])
        return {"version": runner([params["codex_bin"], "--version"]).strip(),
                "help_sha256": S.sha256_bytes(help_text.encode()), "flag_lines": lines,
                "argv_template": S.codex_argv("<EMPTY_CWD>", "<MODEL>")}
    sec("codex_cli", codex)

    def models():
        m = params["model_ids"]
        for k in ("judge_gpt", "judge_sonnet", "judge_opus", "gen_gpt", "gen_opus"):
            if not m.get(k):
                raise ValueError("model id absent")
        return dict(m)
    sec("model_ids", models)

    sec("generator_files", lambda: {n: S.sha256_file(Path(params["gen_dir"]) / n) for n in GENERATOR_V1_FILES})
    sec("session_log_dirs", lambda: dict(SESSION_LOG_DIRS))
    return pins, errors


DEFAULT_PARAMS = {"ollama_url": "http://127.0.0.1:11434", "ollama_models": OLLAMA_MODELS,
                  "claude_bin": "claude", "codex_bin": "codex", "gt500_py": DEFAULT_GT500_PY}


def build_static(params: dict, **kw) -> dict:
    """The PINS-STATIC document: params (paths, not secrets) plus computed pins. Raises CLSExit(9)."""
    pins, errors = compute_static(params, **kw)
    if errors:
        raise S.CLSExit(S.EXIT_PINS, "static pins not computable: " + ",".join(sorted(errors)))
    return {"params": params, "pins": pins}


# ---------------------------------------------------------------------------------------------
# PINS-RUN append-only chain
# ---------------------------------------------------------------------------------------------

def _entry_sha(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "entry_sha"}
    return S.sha256_bytes(canonical(body).encode())


def read_run(path) -> list[dict]:
    return S.read_jsonl(path) if path and Path(path).exists() else []


def append_run_entry(run_path, name: str, files: list, *, extra: dict | None = None) -> dict:
    """Append ONE hashed entry. Entries are never overwritten: a duplicate name is refused and the file is
    only ever opened for append."""
    entries = read_run(run_path)
    if any(e["name"] == name for e in entries):
        raise S.CLSExit(S.EXIT_REFUSE, "PINS-RUN entry already exists: %s" % name)
    hashes = {}
    for f in files:
        p = Path(f).resolve()
        hashes[str(p)] = S.sha256_file(p)
    entry = {"name": name, "utc": S.utc_now(), "files": hashes,
             "prev_entry_sha": entries[-1]["entry_sha"] if entries else ZERO_SHA}
    if extra:
        entry["extra"] = extra
    entry["entry_sha"] = _entry_sha(entry)
    S.append_jsonl(run_path, entry)
    return entry


def rules_tier_header(path) -> dict:
    first = Path(path).read_text(encoding="utf-8").split("\n", 1)[0]
    try:
        h = json.loads(first)
    except ValueError:
        raise S.CLSExit(S.EXIT_PINS, "rules_tier.jsonl has no header") from None
    if h.get("schema") != RULES_TIER_SCHEMA or not h.get("resolver_commit") or not h.get("produced_utc"):
        raise S.CLSExit(S.EXIT_PINS, "rules_tier.jsonl header lacks schema/resolver_commit/produced_utc")
    return {k: h[k] for k in ("schema", "resolver_commit", "produced_utc")}


def verify_run(run_path) -> list[str]:
    bad = []
    prev = ZERO_SHA
    for i, e in enumerate(read_run(run_path)):
        if e.get("prev_entry_sha") != prev or e.get("entry_sha") != _entry_sha(e):
            bad.append("run_chain:%d" % i)
        prev = e.get("entry_sha", "")
        for f, h in e.get("files", {}).items():
            try:
                ok = S.sha256_file(f) == h
            except OSError:
                ok = False
            if not ok:
                bad.append("run:%s:%s" % (e.get("name"), Path(f).name))
    return bad


def run_entry_names(run_path) -> set:
    return {e["name"] for e in read_run(run_path)}


def pinned_basenames(static_doc: dict | None, run_path) -> set:
    """Basenames of every file pinned in STATIC (generator files, scripts) or in any RUN entry."""
    names = set()
    if static_doc:
        names |= set(static_doc.get("pins", {}).get("generator_files", {}))
        names |= set(static_doc.get("pins", {}).get("scripts", {}))
    for e in read_run(run_path):
        names |= {Path(f).name for f in e.get("files", {})}
    return names


# ---------------------------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------------------------

def _diff(a, b, prefix=""):
    """Key paths where a and b differ (names only, never values)."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(prefix + k)
            else:
                out += _diff(a[k], b[k], prefix + k + ".")
    elif a != b:
        out.append(prefix.rstrip("."))
    return out


_FREEZE_PINS_RE = re.compile(r"^pins_sha256\s+([0-9a-f]{64})\s*$", re.MULTILINE)


def freeze_record_path(params: dict, freeze_record=None) -> Path | None:
    if freeze_record:
        return Path(freeze_record)
    if params.get("freeze_record"):
        return Path(params["freeze_record"])
    if params.get("repo"):
        return Path(params["repo"]) / FREEZE_RECORD_REL
    return None


def check_freeze_record(static_path, record) -> tuple[list[str], list[str]]:
    """(mismatches, warnings). Record absent: warning `pre_freeze_no_pins_sha256_record` (not frozen yet).
    Record present: it must hold exactly one `pins_sha256 <sha>` value equal to the PINS-STATIC file's sha256,
    else a mismatch (fail closed)."""
    if record is None or not Path(record).is_file():
        return [], ["pre_freeze_no_pins_sha256_record"]
    shas = set(_FREEZE_PINS_RE.findall(Path(record).read_text(encoding="utf-8", errors="replace")))
    if not shas:
        return ["freeze_record_lacks_pins_sha256"], []
    if len(shas) > 1:
        return ["freeze_record_ambiguous_pins_sha256"], []
    if shas.pop() != S.sha256_file(static_path):
        return ["pins_file_sha256"], []
    return [], []


def verify(static_path, run_path=None, *, check_ollama: bool = True, freeze_record=None, **kw) -> dict:
    """Recompute STATIC (always) and RUN entries (once any exist), and check the PINS-STATIC file sha256
    against the freeze record. Returns {"ok", "mismatches", "warnings", "env_sha_at_use", "run_entries"};
    never raises on a mismatch."""
    mism, warns = [], []
    doc = json.loads(Path(static_path).read_text(encoding="utf-8"))
    params = doc["params"]
    fm, fw = check_freeze_record(static_path, freeze_record_path(params, freeze_record))
    mism += fm
    warns += fw
    now, errors = compute_static(params, **kw)
    frozen = doc["pins"]
    for k in errors:
        if k == "ollama" and not check_ollama:
            continue
        mism.append(k)
    env_now = now.pop("env", None)
    env_then = frozen.get("env")
    env_sha = env_now["sha256"] if env_now else None
    if env_then and env_now and env_then.get("sha256") != env_now.get("sha256"):
        warns.append("env_changed_since_freeze")
    elif env_now is None and "env" not in errors:
        warns.append("env_unreadable")
    frozen = {k: v for k, v in frozen.items() if k != "env"}
    if not check_ollama:
        now.pop("ollama", None)
        frozen.pop("ollama", None)
    for k in errors:
        now.pop(k, None)
        frozen.pop(k, None)
    mism += _diff(frozen, now)
    run = read_run(run_path)
    if run:
        mism += verify_run(run_path)
    return {"ok": not mism, "mismatches": sorted(set(mism)), "warnings": warns, "env_sha_at_use": env_sha,
            "run_entries": len(run)}


def verify_at_start(static_path, run_path=None, *, check_ollama: bool = False, **kw) -> dict:
    """Every runner calls this first: exit 9 on a mismatch, naming the key."""
    if not static_path or not Path(static_path).exists():
        raise S.CLSExit(S.EXIT_PINS, "PINS-STATIC absent")
    res = verify(static_path, run_path, check_ollama=check_ollama, **kw)
    if not res["ok"]:
        raise S.CLSExit(S.EXIT_PINS, "pins mismatch: " + ",".join(res["mismatches"]))
    return res


def freeze_line(static_path) -> str:
    return "pins_sha256 " + S.sha256_file(static_path)


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cls_pins")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pins")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--static", action="store_true")
    g.add_argument("--run", action="store_true")
    g.add_argument("--verify", metavar="PINS_STATIC")
    g.add_argument("--freeze-line", metavar="PINS_STATIC")
    p.add_argument("--out")
    p.add_argument("--repo", default=str(S.REPO))
    p.add_argument("--scripts-dir")
    p.add_argument("--engine-commit")
    p.add_argument("--rubric-py")
    p.add_argument("--gt500-py", default=DEFAULT_GT500_PY, help="pinned by sha (GT500_SHA256); default as p17d")
    p.add_argument("--freeze-record", help="verify only: the freeze record (default <repo>/%s)" % FREEZE_RECORD_REL)
    p.add_argument("--denylist")
    p.add_argument("--env-file")
    p.add_argument("--ollama-url", default=DEFAULT_PARAMS["ollama_url"])
    p.add_argument("--bge-snapshot-dir")
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--codex-bin", default="codex")
    p.add_argument("--gen-dir")
    p.add_argument("--model-id", action="append", default=[], metavar="KEY=ID",
                   help="judge_gpt, judge_sonnet, judge_opus, gen_gpt, gen_opus")
    p.add_argument("--entry")
    p.add_argument("--files", nargs="*", default=[])
    p.add_argument("--rules-tier")
    p.add_argument("--pins-run", default="pins_run.jsonl")
    p.add_argument("--no-ollama", action="store_true", help="verify only: skip the Ollama pins")
    args = ap.parse_args(argv)
    try:
        if args.static:
            need = ["out", "engine_commit", "rubric_py", "denylist", "env_file", "bge_snapshot_dir", "gen_dir"]
            miss = [n for n in need if not getattr(args, n)]
            if miss:
                raise S.CLSExit(S.EXIT_PINS, "static pins need: " + ",".join(miss))
            mids = dict(kv.split("=", 1) for kv in args.model_id if "=" in kv)
            params = {"repo": str(Path(args.repo).resolve()),
                      "scripts_dir": str(Path(args.scripts_dir or Path(args.repo) / "scripts").resolve()),
                      "engine_commit": args.engine_commit, "rubric_py": args.rubric_py, "gt500_py": args.gt500_py,
                      "denylist": args.denylist,
                      "env_file": args.env_file, "ollama_url": args.ollama_url, "ollama_models": OLLAMA_MODELS,
                      "bge_snapshot_dir": args.bge_snapshot_dir, "claude_bin": args.claude_bin,
                      "codex_bin": args.codex_bin, "gen_dir": args.gen_dir, "model_ids": mids}
            doc = build_static(params)
            S.write_private(args.out, canonical(doc))
            print(json.dumps({"pins_keys": sorted(doc["pins"]), "scripts": len(doc["pins"]["scripts"]),
                              "pins_sha256": S.sha256_file(args.out)}, sort_keys=True))
            return S.EXIT_OK
        if args.run:
            if not args.entry or not args.files:
                raise S.CLSExit(S.EXIT_REFUSE, "pins --run needs --entry and --files")
            extra = {"rules_tier_header": rules_tier_header(args.rules_tier)} if args.rules_tier else None
            e = append_run_entry(args.pins_run, args.entry, args.files, extra=extra)
            print(json.dumps({"entry": e["name"], "files": len(e["files"]), "entry_sha": e["entry_sha"],
                              "utc": e["utc"]}, sort_keys=True))
            return S.EXIT_OK
        if args.verify:
            res = verify(args.verify, args.pins_run, check_ollama=not args.no_ollama, freeze_record=args.freeze_record)
            print(json.dumps(res, sort_keys=True))
            if not res["ok"]:
                print("pins mismatch: " + ",".join(res["mismatches"]), file=sys.stderr)
                return S.EXIT_PINS
            return S.EXIT_OK
        print(freeze_line(args.freeze_line))
        return S.EXIT_OK
    except S.CLSExit as e:
        print("refused: %s" % e, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
