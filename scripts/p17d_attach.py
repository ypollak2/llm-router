#!/usr/bin/env python3
"""P1.7-d synthetic, step 1: ``attach`` (docs/prereg/PREREG-P17D-synthetic-DRAFT.md sections 2, 2a, 3, 4).

    python3 scripts/p17d_attach.py            # one shot; refuses once its outputs exist or are pinned

The only P1.7-d step that reads the owner's transcripts (top-level ``~/.claude/projects/*/*.jsonl``), the
proxy ledger, ``~/.llm-router`` settings and the live install. Every source is opened read-only: each
transcript is read once into memory (a byte snapshot) and parsed from that copy; the ledger is streamed and
only ``msg_id -> requested/served model`` for the needed ids is kept. Nothing is ever written outside
``--run-dir`` (which must sit under ``--pp-root``), and every file written is 0600 in a 0700 directory.

Writes ``items.jsonl`` (prompt text: 0600, never printed), ``attach_counts.json``, ``tier_policy.yaml``
(a copy of the live tier policy) and ``tier_policy.json``. Stdout is one JSON object of counts. No model
is called, no label or verdict is read, and no rules tier is computed here.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import glob
import hashlib
import importlib.util
import json
import os
import plistlib
import pwd
import re
import shlex
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)   # the real home, never $HOME (a scratch HOME must not change kinds)
PP = HOME / ".rsi" / "research" / "primary-plan"

LABEL = "synthetic (D-99)"
WINDOW_END_UTC = "2026-10-08T20:46:00Z"        # first live classifier_shadow row (AMEND4)
PROXY_ERA_UTC = "2026-09-28T14:41:00Z"         # proxy ledger starts (PREREG-v2-amend5)
PROMPT_STORE_CAP = 20000                       # AMEND1-shadowlab B
ASSISTANT_KEEP = 501                           # > cls_input.ASSISTANT_CHARS, so assemble re-truncates identically
RULES_HAND_EDIT_COMMIT = "c0b0e5ca"
RULES_HAND_EDIT_TEST = "tests/test_first_forty_task14.py"
FIRST_MARK = "(This is the FIRST prompt of the session: no prior context.)"
TIER_POLICY_KEY = "LLM_ROUTER_PROXY_TIER_POLICY"

#: Frozen inputs (sha256). A difference refuses the run (exit 2).
PINS = {
    "corpus": "3f5d96eee34ed72b90f62ab462b8f452b6ca2d465c0beb6158dcb94b4a85b7f4",
    "gt500": "9cde774dfe93387d6ee3dfcbd1b5481a537f265e5961d82f85a87d29d6328959",
    "supply": "12cd3e1eed7fc15658b4b44246d83422e2b61adc4ccb270dd9f5e729608fb0e8",
    "ge3_items": "c087c5b4a388a971daefa448b1d6a74534e0811fdff22f795618d78c8f1e977e",
    "ge3_split": "8921a1549e458c8e648b8070ce84a8b97a1b9f0b055c99d6e932bcbc3ca5405b",
    "p_eval_sample": "0e0318e87ad7538ea097973d8fb3b0d4521f1bea1a13ade80326d91d34fdec2c",
    "e1_tasks": "128ed7687d13d47aec9492c7fb22bbbbe9b20cb5d444bd9c4c4e1da048e523cd",
    "exec_sessions": "b47e88f9fbe60a63788569b38db7727b7d01ad896077b569105c1902c83ec184",
}


class Refuse(SystemExit):
    def __init__(self, msg: str) -> None:
        super().__init__(2)
        self.msg = msg


@dataclass
class Cfg:
    run_dir: Path
    pp_root: Path
    corpus: Path
    claude_root: Path
    ledger: Path
    gt_dir: Path
    p_eval_sample: Path
    e1_tasks: Path
    exec_sessions: Path
    overrides: Path
    denylist: Path
    prereg_frozen: Path
    pins: dict = field(default_factory=lambda: dict(PINS))
    window_end: str = WINDOW_END_UTC
    kind_home: str | None = None          # session_kind home (tests); None = the real home (pwd)
    rules_hand_edit_texts: list | None = None   # None = read from git (c0b0e5ca) + the test file
    # tier-policy resolution inputs (None = discover on this machine)
    ps_text: str | None = None
    plists: list | None = None
    dotenv_paths: list | None = None
    live_pkg_dir: Path | None = None


# ----------------------------------------------------------------------------- io
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def write_private(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    """0600 write via a temp file in the same dir; ``exclusive`` never replaces an existing file."""
    fd = os.open(path.with_name(path.name + ".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        if exclusive:
            os.link(tmp, path)       # fails if path exists
            os.unlink(tmp)
        else:
            os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def load_pinned(name: str, path: Path, want: str):
    got = sha256_file(path)
    if got != want:
        raise Refuse(f"refusing: {path.name} sha256 {got} != pinned {want}")
    sys.dont_write_bytecode = True
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(f"p17d_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def check_pin(name: str, path: Path, pins: dict) -> None:
    got = sha256_file(path)
    if got != pins[name]:
        raise Refuse(f"refusing: {name} sha256 {got} != pinned {pins[name]}")


def read_jsonl(path: Path) -> list:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ----------------------------------------------------------------------------- tier policy (prereg §4)
def _proxy_argvs(ps_text: str) -> list[list[str]]:
    out = []
    for line in ps_text.splitlines():
        if "llm_router.proxy" in line or ("llm-router" in line and "proxy" in line):
            try:
                out.append(shlex.split(line.strip()))
            except ValueError:
                continue
    return out


def _flag(argv: list[str], name: str) -> str | None:
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def _is_proxy_plist(d: dict) -> bool:
    blob = " ".join([str(d.get("Label", ""))] + [str(x) for x in d.get("ProgramArguments") or []])
    return ("llm_router" in blob or "llm-router" in blob) and "proxy" in blob


def _interpreter(argv: list[str]) -> str | None:
    if not argv:
        return None
    exe = argv[0]
    if "python" in os.path.basename(exe):
        return exe
    try:
        with open(exe, "rb") as f:
            first = f.readline().decode("utf-8", "replace").strip()
    except OSError:
        return None
    if first.startswith("#!"):
        parts = first[2:].split()
        if parts and os.path.basename(parts[0]) == "env" and len(parts) > 1:
            return parts[1]
        return parts[0] if parts else None
    return None


def _live_pkg_dir(argv: list[str]) -> Path | None:
    """The live install's ``llm_router`` package dir, found with ``find_spec`` (no package code runs)."""
    py = _interpreter(argv)
    if not py:
        return None
    code = "import importlib.util,os;s=importlib.util.find_spec('llm_router');print(os.path.dirname(s.origin))"
    try:
        out = subprocess.run([py, "-I", "-c", code], capture_output=True, text=True, timeout=30,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    p = out.stdout.strip()
    return Path(p) if out.returncode == 0 and p else None


def resolve_tier_policy(cfg: Cfg) -> dict:
    """Proxy order: ``--tier-policy`` (process, then launchd plist), else ``LLM_ROUTER_PROXY_TIER_POLICY``
    (plist env, then the .env files in ``env_loader`` order), else the bundled yaml of the live install."""
    from llm_router.env_loader import _parse_env_file, project_env_may_set

    ps_text = cfg.ps_text
    if ps_text is None:
        try:
            ps_text = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True, text=True,
                                     timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            ps_text = ""
    plists = cfg.plists
    if plists is None:
        plists = []
        for p in sorted(glob.glob(str(HOME / "Library" / "LaunchAgents" / "*.plist"))):
            try:
                with open(p, "rb") as f:
                    d = plistlib.load(f)
            except Exception:  # noqa: BLE001 - an unreadable plist is not the proxy's
                continue
            if isinstance(d, dict) and _is_proxy_plist(d):
                plists.append(d)
    argvs = _proxy_argvs(ps_text)
    for argv in argvs:
        v = _flag(argv, "--tier-policy")
        if v:
            return {"path": v, "basis": "1:process_argv"}
    for d in plists:
        v = _flag([str(x) for x in d.get("ProgramArguments") or []], "--tier-policy")
        if v:
            return {"path": v, "basis": "1:plist_argv"}
    for d in plists:
        v = (d.get("EnvironmentVariables") or {}).get(TIER_POLICY_KEY)
        if v:
            return {"path": v, "basis": "2:plist_env"}
    dotenvs = cfg.dotenv_paths
    if dotenvs is None:
        wd = next((d.get("WorkingDirectory") for d in plists if d.get("WorkingDirectory")), None)
        dotenvs = ([("project", Path(wd) / ".env")] if wd else []) + [
            ("state", HOME / ".llm-router" / ".env"), ("home", HOME / ".env")]
    for kind, path in dotenvs:
        v = _parse_env_file(Path(path)).get(TIER_POLICY_KEY)
        if v and (kind != "project" or project_env_may_set(TIER_POLICY_KEY)):
            return {"path": v, "basis": f"2:dotenv_{kind}"}
    pkg = cfg.live_pkg_dir
    if pkg is None:
        argv = argvs[0] if argvs else ([str(x) for x in plists[0].get("ProgramArguments") or []] if plists else [])
        pkg = _live_pkg_dir(argv)
    if pkg is None:
        raise Refuse("refusing: cannot locate the live install (no proxy process, plist or package dir)")
    return {"path": str(Path(pkg) / "proxy" / "claude_tiers.yaml"), "basis": "3:bundled_live"}


# ----------------------------------------------------------------------------- leakage (prereg §3)
def rules_hand_edit_texts(repo: Path) -> list[str]:
    """Prompts the rules were hand-tuned on (c0b0e5ca): quoted strings of its message and the string
    literals of its test, 4 words or more."""
    out = []
    msg = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%B", RULES_HAND_EDIT_COMMIT],
                         capture_output=True, text=True, timeout=30).stdout
    out += [q for q in re.findall(r'"([^"\n]+)"', msg) if len(q.split()) >= 4]
    src = subprocess.run(["git", "-C", str(repo), "show", f"{RULES_HAND_EDIT_COMMIT}:{RULES_HAND_EDIT_TEST}"],
                         capture_output=True, text=True, timeout=30).stdout
    for node in ast.walk(ast.parse(src or "")):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value.split()) >= 4:
            out.append(node.value)
    if not out:
        raise Refuse("refusing: no rules_hand_edit texts found (git history missing?)")
    return out


class LeakIndex:
    def __init__(self, supply, members: list[tuple[str, str, str | None, str | None]]):
        """``members``: (set name, text, session_id, timestamp)."""
        self.s = supply
        self.norm: dict[str, set] = {}
        self.long: list[tuple[str, set]] = []
        self.sid_ts: dict[tuple, set] = {}
        for name, text, sid, ts in members:
            n = supply.norm(text or "")
            if n:
                self.norm.setdefault(n, set()).add(name)
                if len(n.split()) > 5:
                    self.long.append((name, supply.shingles(text)))
            if sid and ts:
                self.sid_ts.setdefault((sid, ts), set()).add(name)

    def hits(self, text: str, sid: str, ts: str, skip: frozenset = frozenset()) -> set:
        found = set(self.sid_ts.get((sid, ts), set()))
        n = self.s.norm(text)
        found |= self.norm.get(n, set())
        if len(n.split()) > 5:
            sh = self.s.shingles(text)
            found |= {name for name, ls in self.long if len(sh & ls) / max(1, len(sh | ls)) >= 0.8}
        return found - skip


# ----------------------------------------------------------------------------- transcripts
def _text_blocks_raw(content) -> str:
    """The text ``groundtruth_sources`` stored in the corpus (joined text blocks, stripped)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


def parse_ts(ts) -> dt.datetime | None:
    """A transcript timestamp as an aware UTC datetime (fractional seconds kept); None if unparseable."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        v = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)


def _is_tool_result_only(content) -> bool:
    return isinstance(content, list) and bool(content) and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _next_answer(recs: list, i: int):
    """The first main-thread assistant record after ``recs[i]``, stopping at the next user prompt
    (a user record that is not only tool results): a turn with no answer has no served model."""
    for r in recs[i + 1:]:
        if r["type"] == "assistant":
            return r
        m = r.get("message") if isinstance(r.get("message"), dict) else {}
        if m.get("role") == "user" and not _is_tool_result_only(m.get("content")):
            return None
    return None


def llm_router_provenance() -> dict:
    """Which llm_router this run imports. Refuses unless it is this worktree's ``src``."""
    import llm_router

    pkg = Path(llm_router.__file__).resolve().parent
    if not _inside(pkg, ROOT / "src"):
        raise Refuse(f"refusing: llm_router imported from {pkg}, not {ROOT / 'src'}")
    mods = ("proxy/cls_input.py", "proxy/steps.py", "prompt_key.py", "session_kind.py", "secret_scrubber.py",
            "shadow_eval.py", "env_loader.py", "local_classifier.py")
    return {"path": str(pkg), "sha256": {m: sha256_file(pkg / m) for m in mods}}


def session_messages(raw: bytes):
    """Parse one transcript snapshot. Yields (record dict, messages-so-far, next main-thread assistant
    record) for every main-thread user record; consecutive assistant records of one message id merge."""
    recs = []
    for line in raw.decode("utf-8", "ignore").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and not d.get("isSidechain") and d.get("type") in ("user", "assistant"):
            recs.append(d)
    msgs: list[dict] = []
    last_asst_id = None
    for i, d in enumerate(recs):
        m = d.get("message") if isinstance(d.get("message"), dict) else {}
        if d["type"] == "assistant":
            content = m.get("content")
            blocks = content if isinstance(content, list) else ([{"type": "text", "text": content}]
                                                                if isinstance(content, str) else [])
            mid = m.get("id")
            if msgs and msgs[-1]["role"] == "assistant" and mid and mid == last_asst_id:
                msgs[-1]["content"].extend(blocks)
            else:
                msgs.append({"role": "assistant", "content": list(blocks)})
            last_asst_id = mid
            continue
        if m.get("role") != "user":
            continue
        msgs.append({"role": "user", "content": m.get("content")})
        last_asst_id = None
        nxt = _next_answer(recs, i)
        yield d, msgs, nxt


def assemble_pieces(msgs: list[dict]) -> dict:
    """The parts ``cls_input.assemble`` reads, found by the same backward scan (cls_input.py:62-108)."""
    from llm_router.proxy import cls_input

    stop = max(len(msgs) - cls_input.MAX_SCAN_MESSAGES, 0)
    newest = -1
    for i in range(len(msgs) - 1, stop - 1, -1):
        if msgs[i].get("role") == "user" and cls_input._human(msgs[i].get("content")):
            newest = i
            break
    earlier, assistant = [], ""
    for i in range(newest - 1, max(newest - cls_input.MAX_SCAN_MESSAGES, 0) - 1, -1):
        role = msgs[i].get("role")
        if role == "user" and len(earlier) < cls_input.EARLIER_PROMPTS:
            t = cls_input._human(msgs[i].get("content"))
            if t:
                earlier.append(t[:cls_input.EARLIER_CHARS])
        elif role == "assistant" and not assistant:
            assistant = cls_input._human(msgs[i].get("content"))
        if assistant and len(earlier) == cls_input.EARLIER_PROMPTS:
            break
    earlier.reverse()
    return {"earlier": earlier, "assistant_tail": assistant[-ASSISTANT_KEEP:]}


def min_body(cwd: str, earlier: list[str], assistant_tail: str, prompt: str, model: str | None = None) -> dict:
    """The minimal request body the harness replays (prereg §4); also used here for the equality check."""
    msgs = [{"role": "user", "content": e} for e in earlier]
    if assistant_tail:
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": assistant_tail}]})
    msgs.append({"role": "user", "content": prompt})
    body = {"messages": msgs, "system": f"Primary working directory: {cwd}" if cwd else ""}
    if model:
        body["model"] = model
    return body


def cap_prompt(t: str) -> str:
    if len(t) <= PROMPT_STORE_CAP:
        return t
    half = PROMPT_STORE_CAP // 2
    return t[:half] + "\n[... middle omitted ...]\n" + t[-half:]


def length_band(words: int) -> str:
    return "w<=12" if words <= 12 else "w13-50" if words <= 50 else "w51-150" if words <= 150 else "w>150"


def item_id(sid: str, ts: str) -> str:
    return hashlib.sha256(f"p17d:item:{sid}:{ts}".encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------- attach
def attach(cfg: Cfg) -> dict:
    from llm_router import secret_scrubber, shadow_eval
    from llm_router.proxy import cls_input, steps
    from llm_router.prompt_key import key as text_key
    from llm_router.session_kind import classify_with_basis

    # ---- refusals: single shot, writes only under pp_root
    if not _inside(cfg.run_dir, cfg.pp_root):
        raise Refuse(f"refusing: run dir {cfg.run_dir} is not under {cfg.pp_root}")
    if cfg.prereg_frozen.exists():
        raise Refuse("refusing: the prereg is frozen; attach is single-shot")
    for name in ("items.jsonl", "attach.pinned"):
        if (cfg.run_dir / name).exists():
            raise Refuse(f"refusing: {name} exists; attach is single-shot")
    gts = cfg.gt_dir / "scripts"
    supply = load_pinned("supply", gts / "supply.py", cfg.pins["supply"])
    gt = load_pinned("gt500", gts / "gt500.py", cfg.pins["gt500"])
    for name, path in (("corpus", cfg.corpus), ("ge3_items", cfg.gt_dir / "items.jsonl"),
                       ("ge3_split", cfg.gt_dir / "split.json"), ("p_eval_sample", cfg.p_eval_sample),
                       ("e1_tasks", cfg.e1_tasks), ("exec_sessions", cfg.exec_sessions)):
        check_pin(name, path, cfg.pins)

    # ---- inputs
    corpus = {hashlib.sha256(json.loads(ln)["text"].encode("utf-8")).hexdigest()
              for ln in open(cfg.corpus, encoding="utf-8") if ln.strip()}
    ge3 = read_jsonl(cfg.gt_dir / "items.jsonl")
    split = json.load(open(cfg.gt_dir / "split.json", encoding="utf-8"))
    tune, held = set(split["tune_ids"]), set(split["heldout_ids"])
    ge3_by_sidts = {(r["session_id"], r["timestamp"]): r for r in ge3}
    pe = read_jsonl(cfg.p_eval_sample)
    e1 = json.load(open(cfg.e1_tasks, encoding="utf-8"))
    hand = cfg.rules_hand_edit_texts if cfg.rules_hand_edit_texts is not None else rules_hand_edit_texts(ROOT)
    members = [("ge3_tune" if r["id"] in tune else "ge3_heldout" if r["id"] in held else "ge3_other",
                r["prompt"], r["session_id"], r["timestamp"]) for r in ge3]
    members += [("e2_cal" if r["split"] == "calibration" else "e2_ho", r["text"], r["session_id"], r["timestamp"])
                for r in pe]
    members += [(f"e1_{r['split']}", r["text"], None, None) for r in e1]
    members += [("rules_hand_edit", t, None, None) for t in hand]
    leak = LeakIndex(supply, members)
    provenance = llm_router_provenance()
    window_end = parse_ts(cfg.window_end)
    proxy_era = parse_ts(PROXY_ERA_UTC)
    exec_ids = {m.group(0) for m in (re.match(r"^[0-9a-f-]{36}", x.strip()) for x in open(cfg.exec_sessions)) if m}
    try:
        with open(cfg.overrides, encoding="utf-8") as f:
            raw_ov = json.load(f)
        overrides = {k: (v or {}).get("kind") for k, v in raw_ov.items() if isinstance(v, dict)}
    except (OSError, ValueError, AttributeError) as exc:
        raise Refuse(f"refusing: session-kind overrides unreadable ({type(exc).__name__})") from None
    if not cfg.denylist.is_file():
        raise Refuse("refusing: denylist missing (an empty file is fine; a missing one is not)")
    deny = gt.load_denylist(str(cfg.denylist))
    transcripts = sorted(glob.glob(str(cfg.claude_root / "*" / "*.jsonl")))
    if not transcripts:
        raise Refuse(f"refusing: no transcript files under {cfg.claude_root}")
    deny_l = [x.lower() for x in deny]
    policy = resolve_tier_policy(cfg)
    policy_path = Path(policy["path"]).expanduser()
    try:
        policy_bytes = policy_path.read_bytes()
    except OSError as exc:
        raise Refuse(f"refusing: live tier policy unreadable ({type(exc).__name__})") from None

    # ---- transcripts (snapshot per file, read-only)
    c, cand, seen = Counter(), [], set()
    for path in transcripts:
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            c["unreadable_file"] += 1
            continue
        for d, msgs, nxt in session_messages(raw):
            msg = d.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list) and content and all(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
            origin = d.get("origin") if isinstance(d.get("origin"), dict) else {}
            r = {"entrypoint": d.get("entrypoint"), "prompt_source": d.get("promptSource"),
                 "is_meta": bool(d.get("isMeta")), "origin_human": origin.get("kind") == "human"}
            sid, ts = d.get("sessionId") or "", d.get("timestamp") or ""
            in_corpus = hashlib.sha256(_text_blocks_raw(content).encode("utf-8")).hexdigest() in corpus
            g3 = ge3_by_sidts.get((sid, ts))
            g_cand = g3 is not None and g3["id"] in held
            if not in_corpus and not g_cand:
                continue
            c["frame_records_seen"] += 1
            c["corpus_matched_records"] += int(in_corpus)
            text = supply.REMINDER.sub("", supply.text_of(content)).strip()
            cwd = d.get("cwd") or ""
            if not gt._is_human(r):
                c["not_human_typed"] += 1
                continue
            tsd = parse_ts(ts)
            if tsd is None:
                c["bad_timestamp"] += 1
                continue
            if tsd >= window_end:
                c["after_window"] += 1
                continue
            if sid in exec_ids:
                c["exec_session"] += 1
                continue
            kind, basis = classify_with_basis(cwd=cwd, entrypoint=None, override=overrides.get(sid),
                                              home=cfg.kind_home if cfg.kind_home is not None else str(HOME))
            if kind != "organic":
                c[f"kind_{kind}"] += 1
                continue
            if not text:
                c["empty"] += 1
                continue
            if supply.HARNESS.match(text) or text.startswith("/"):
                c["harness_or_slash"] += 1
                continue
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "image" for b in content):
                c["has_image"] += 1
                continue
            if (sid, ts) in seen:
                c["dup_record_same_session_ts"] += 1
                continue
            seen.add((sid, ts))
            if supply.secret_hit(text):
                c["secret_prompt"] += 1
                continue
            body = {"messages": [dict(m) for m in msgs], "system": f"Primary working directory: {cwd}" if cwd else ""}
            assembled = cls_input.assemble(body)
            ctx = assembled.context
            if sid in deny or any(x in (text + "\n" + ctx).lower() for x in deny_l):
                c["denylist"] += 1
                continue
            if any(p.search(text) or p.search(ctx) for p in secret_scrubber.SECRET_PATTERNS.values()):
                c["secret_scrubber"] += 1
                continue
            nmsg = (nxt or {}).get("message") or {}
            served_model = nmsg.get("model")
            if shadow_eval.tier_name(served_model) == "haiku":
                c["served_haiku"] += 1
                continue
            prompt_full = steps.newest_human_text(body)
            pieces = assemble_pieces(body["messages"])
            stored_prompt = cap_prompt(prompt_full)
            mb = min_body(cwd, pieces["earlier"], pieces["assistant_tail"], stored_prompt)
            re_asm = cls_input.assemble(mb)
            words = len(text.split())
            cand.append({
                "item_id": item_id(sid, ts), "group": "G" if g_cand else "F",
                "ge3_id": g3["id"] if g_cand else None, "session_id": sid, "ts": ts, "cwd": cwd,
                "prompt": stored_prompt, "text_sha": text_key(prompt_full),
                "earlier": pieces["earlier"], "assistant_tail": pieces["assistant_tail"],
                "capped": bool(getattr(assembled, "capped", False)),
                "assembled_context": ctx,
                "assemble_ok": (re_asm.context == ctx and str(re_asm.prompt) == str(assembled.prompt)),
                "context_withheld": bool(supply.secret_hit(ctx)),
                "msg_id": nmsg.get("id"), "served_model": served_model,
                "served_tier": shadow_eval.tier_name(served_model),
                "kind": kind, "kind_basis": basis, "words": words, "length_band": length_band(words),
                "first_in_session": FIRST_MARK in ctx, "era": "proxy" if tsd >= proxy_era else "pre_proxy", "_tsd": tsd,
                "month": ts[:7], "_text": text, "_p_eval_cal": bool(g3 and g3.get("p_eval_cal")),
            })

    # ---- leakage + dedupe (F in timestamp order, supply rules), then G
    if not c["corpus_matched_records"]:
        raise Refuse("refusing: no transcript record matched the corpus (wrong root or corpus?)")
    cand.sort(key=lambda x: x["_tsd"])
    f_items, g_items, leak_hits = [], [], Counter()
    seen_exact, short_counts, long_sh = set(), Counter(), []
    for it in cand:
        if it["group"] != "F":
            continue
        hits = leak.hits(it["_text"], it["session_id"], it["ts"])
        if hits:
            c["leakage"] += 1
            leak_hits.update(hits)
            continue
        n = supply.norm(it["_text"])
        if not n:
            c["empty_after_norm"] += 1
            continue
        if len(n.split()) <= 5:
            short_counts[n] += 1
            if short_counts[n] > 2:
                c["near_dup_short_gt2"] += 1
                continue
        else:
            if n in seen_exact:
                c["dup_exact"] += 1
                continue
            sh = supply.shingles(it["_text"])
            if any(len(sh & s) / max(1, len(sh | s)) >= 0.8 for s in long_sh):
                c["near_dup_jaccard_ge_0.8"] += 1
                continue
            seen_exact.add(n)
            long_sh.append(sh)
        f_items.append(it)
    f_index = LeakIndex(supply, [("f_item", x["_text"], None, None) for x in f_items])
    for it in cand:
        if it["group"] != "G":
            continue
        if it["_p_eval_cal"]:
            c["g_p_eval_cal"] += 1
            continue
        hits = leak.hits(it["_text"], it["session_id"], it["ts"], skip=frozenset({"ge3_heldout"}))
        if hits:
            c["g_leakage"] += 1
            leak_hits.update("g:" + h for h in hits)
            continue
        if f_index.hits(it["_text"], "", ""):
            c["g_near_dup_of_f"] += 1
            continue
        g_items.append(it)

    # ---- requested tier: proxy ledger join by msg_id (streamed; only the needed ids are kept)
    need = {it["msg_id"] for it in f_items + g_items if it["msg_id"]}
    joined = {}
    if need and cfg.ledger.exists():
        with open(cfg.ledger, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"msg_id"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                mid = row.get("msg_id") if isinstance(row, dict) else None
                if mid in need and mid not in joined and row.get("requested_model"):
                    joined[mid] = row["requested_model"]
    for it in f_items + g_items:
        if it["msg_id"] in joined:
            it["requested_model"], it["requested_basis"] = joined[it["msg_id"]], "ledger"
        elif it["served_model"]:
            it["requested_model"], it["requested_basis"] = it["served_model"], "transcript_served"
        else:
            it["requested_model"], it["requested_basis"] = None, "imputed_by_scorer"
        it["requested_tier"] = shadow_eval.tier_name(it["requested_model"])

    # ---- outputs (0700 dir, 0600 files; items.jsonl last and never replaced)
    def summary(rows):
        sess = Counter(x["session_id"] for x in rows)
        n = len(rows)
        return {"n": n, "sessions": len(sess),
                "largest_session_share": round(sess.most_common(1)[0][1] / n, 4) if n else None,
                "top3_session_counts": [v for _, v in sess.most_common(3)]}

    items = f_items + g_items
    counts = {
        "label": LABEL, "what": "P1.7-d synthetic attach (counts only; no text)",
        "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "script_sha256": sha256_file(Path(__file__)), "pins": cfg.pins, "window_end_utc": cfg.window_end,
        "exclusions": dict(sorted(c.items())), "leak_hits_by_set": dict(sorted(leak_hits.items())),
        "F": summary(f_items), "G_candidates": summary(g_items),
        "requested_basis": dict(Counter(x["requested_basis"] for x in items)),
        "assemble_mismatch": sum(not x["assemble_ok"] for x in items),
        "context_withheld": sum(x["context_withheld"] for x in items),
        "capped": sum(x["capped"] for x in items),
        "tier_policy": {"basis": policy["basis"], "sha256": hashlib.sha256(policy_bytes).hexdigest()},
        "inputs": {"transcript_files": len(transcripts), "denylist_entries": len(deny),
                   "overrides_entries": len(overrides), "ledger_present": cfg.ledger.exists(),
                   "ledger_joined": len(joined), "kind_home_is_real_home": cfg.kind_home is None},
        "llm_router": provenance,
    }
    os.makedirs(cfg.run_dir, mode=0o700, exist_ok=True)
    os.chmod(cfg.run_dir, 0o700)
    try:
        import yaml  # noqa: PLC0415
        data = yaml.safe_load(policy_bytes) or {}
    except Exception:  # noqa: BLE001 - recorded, not fatal: the sha is the pin
        data = {}
    write_private(cfg.run_dir / "tier_policy.yaml", policy_bytes)
    write_private(cfg.run_dir / "tier_policy.json", (json.dumps({
        **policy, "sha256": counts["tier_policy"]["sha256"], "route": data.get("route"),
        "complexity_knn": data.get("complexity_knn")}, indent=1, sort_keys=True) + "\n").encode())
    write_private(cfg.run_dir / "attach_counts.json", (json.dumps(counts, indent=1, sort_keys=True) + "\n").encode())
    lines = "".join(json.dumps({k: v for k, v in it.items() if not k.startswith("_")}, ensure_ascii=False) + "\n"
                    for it in items)
    write_private(cfg.run_dir / "items.jsonl", lines.encode("utf-8"), exclusive=True)
    return counts


def default_cfg(a) -> Cfg:
    return Cfg(run_dir=Path(a.run_dir), pp_root=Path(a.pp_root), corpus=Path(a.corpus),
               claude_root=Path(a.claude_root), ledger=Path(a.ledger), gt_dir=Path(a.gt_dir),
               p_eval_sample=Path(a.p_eval_sample), e1_tasks=Path(a.e1_tasks),
               exec_sessions=Path(a.exec_sessions), overrides=Path(a.overrides), denylist=Path(a.denylist),
               prereg_frozen=ROOT / "docs" / "prereg" / "PREREG-P17D-synthetic.md")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", default=str(PP / "v16" / "p17d"))
    ap.add_argument("--pp-root", default=str(PP))
    ap.add_argument("--corpus", default=str(PP / "v16" / "door_corpus.jsonl"))
    ap.add_argument("--claude-root", default=str(HOME / ".claude" / "projects"))
    ap.add_argument("--ledger", default=str(HOME / ".llm-router" / "proxy_calls.jsonl"))
    ap.add_argument("--gt-dir", default=str(PP / "v16" / "gt500"))
    ap.add_argument("--p-eval-sample", default=str(HOME / ".rsi/research/complexity-v2/p_eval/work/sample.jsonl"))
    ap.add_argument("--e1-tasks", default=str(HOME / ".rsi/research/local-classifier/tasks_local.json"))
    ap.add_argument("--exec-sessions", default=str(PP / "exec_sessions.txt"))
    ap.add_argument("--overrides", default=str(HOME / ".llm-router" / "session_kind_overrides.json"))
    ap.add_argument("--denylist", default=str(HOME / ".llm-router" / "denylist.txt"))
    a = ap.parse_args(argv)
    try:
        counts = attach(default_cfg(a))
    except Refuse as r:
        print(r.msg, file=sys.stderr)
        return 2
    print(json.dumps({k: counts[k] for k in ("exclusions", "leak_hits_by_set", "F", "G_candidates",
                                             "requested_basis", "assemble_mismatch", "tier_policy")},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
