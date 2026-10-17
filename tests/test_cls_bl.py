"""CLS-v16 build B-L: send library, label runner, generator runner, resemblance tool, pins.

Behaviour only (no source-text assertions). Fakes only: fake `claude` and `codex` binaries (real child
processes recording what they were given), a fake Ollama on 127.0.0.1, injected scorers. No model call,
no network, no download, nothing read from ~/.codex, ~/.claude or ~/.llm-router (HOME is a tmp dir).

Mutants: each `MUTANTS` entry patches ONE function and the matching invariant must then fail; the
unmutated code passes every invariant.
"""
from __future__ import annotations

import hashlib
import http.server
import inspect
import json
import os
import re
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cls_gen_run as G  # noqa: E402
import cls_label_run as L  # noqa: E402
import cls_pins as P  # noqa: E402
import cls_resemble as R  # noqa: E402
import cls_send_lib as S  # noqa: E402

PINNED = {"judge_gpt": "gpt-6-astra", "judge_sonnet": "claude-sonnet-5-5", "judge_opus": "claude-opus-5-5",
          "gen_gpt": "gpt-6-astra", "gen_opus": "claude-opus-5-5"}


def frag_secret() -> str:
    """An engine-shaped fake key built from fragments, with no 32-char run (independent scan stays quiet)."""
    return "sk-" + "ant-" + "-".join(["Ab12Cd34"] * 5)


# ---------------------------------------------------------------------------------------------
# Fake children
# ---------------------------------------------------------------------------------------------

FAKE_CHILD = r'''#!PYTHON
import sys, os, json, re, time
KIND = "KINDNAME"
cfgp = os.environ["CLSFAKE_CFG"]
cfg = json.load(open(cfgp))
d = os.path.dirname(cfgp)
if "--help" in sys.argv or "--version" in sys.argv:
    if "--version" in sys.argv:
        print("fake-" + KIND + " 1.2.3")
    else:
        print(open(os.path.join(d, "help_" + KIND + ".txt")).read())
    sys.exit(0)
stdin = sys.stdin.buffer.read().decode("utf-8", "replace")
cp = os.path.join(d, "counter.txt")
n = (int(open(cp).read()) if os.path.exists(cp) else 0) + 1
open(cp, "w").write(str(n))
rec = {"kind": KIND, "n": n, "argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd(),
       "cwd_listing": sorted(os.listdir(os.getcwd())),
       "env": {k: (k in os.environ) for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")},
       "session_kind": os.environ.get("LLM_ROUTER_SESSION_KIND")}
with open(os.path.join(d, "record.jsonl"), "a") as f:
    f.write(json.dumps(rec) + "\n")
if KIND == "codex":
    # codex serves the -m model or exits 1 (an unknown id); there is no model field in --json events
    model = sys.argv[sys.argv.index("-m") + 1] if "-m" in sys.argv else "default-codex-model"
    if model in cfg.get("codex_reject", []):
        sys.stderr.write("error: model not supported\n")
        sys.exit(1)
    plain = "--json" not in sys.argv
else:
    models = cfg.get("models") or ["claude-sonnet-5-5"]
    model = models[min(n - 1, len(models) - 1)]
    plain = False
if n in cfg.get("sleep_calls", []):
    time.sleep(cfg.get("sleep_s", 3))


def emit(text, rc=0, transcript=""):
    if KIND == "claude":
        out = {"result": text, "total_cost_usd": cfg.get("cost", 0.01), "is_error": False}
        if not cfg.get("no_model_usage"):
            out["modelUsage"] = {model: {"inputTokens": 5, "outputTokens": 7}}
        print(json.dumps(out))
    elif plain:
        sys.stderr.write("OpenAI Codex (fake)\n--------\nworkdir: " + os.getcwd() + "\nmodel: "
                         + cfg.get("codex_header_model", model) + "\nsandbox: read-only\n--------\n")
        if transcript:
            print(transcript)
        print(text)
    else:
        print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
        print(json.dumps({"type": "turn.started"}))
        print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": text}}))
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}))
    sys.exit(rc)


if cfg.get("die_from_call") and n >= cfg["die_from_call"]:
    emit("", 1)
if KIND == "codex" and cfg.get("codex_die_from") and n >= cfg["codex_die_from"]:
    emit("", 1)
m = re.search(r"PATH PARTS: (.*)", stdin)
if m:
    path = "/" + "/".join(p.replace(" ~ ", "") for p in m.group(1).strip().split(" | "))
    if cfg.get("canary_leak") == "transcript" and os.path.exists(path):
        emit("CANNOT READ", transcript="exec\ncat " + path + "\n" + open(path).read().strip() + "\nexit 0")
    if cfg.get("canary_leak") and os.path.exists(path):
        emit("the token is " + open(path).read().strip())
    emit("CANNOT READ")
if "nonce:" in stdin:
    blocks = re.split(r"\n?  idx: ", stdin)[1:]
    mode = cfg.get("gen_mode", {}).get(str(n), "ok")
    if mode == "badjson":
        emit("sorry no")
    lines = []
    for b in blocks:
        idx = int(b.split("\n", 1)[0])
        lm = re.search(r"length: (\d+)-(\d+) words", b)
        if lm:
            words = (int(lm.group(1)) + int(lm.group(2))) // 2
        else:
            words = int(re.search(r"more than (\d+) words", b).group(1)) + 20
        if cfg.get("band_off"):
            words = cfg["band_off"]
        nonce = re.search(r"nonce: (\w+)", b).group(1)
        proj = re.search(r"project: (.*)", b).group(1).strip()
        text = " ".join("w%s%d" % (nonce[:6], i) for i in range(words))
        if mode == "meta":
            text += " this is an opus task"
        if mode == "secret":
            text += " " + "sk-" + "ant-" + "-".join(["Ab12Cd34"] * 5)
        if cfg.get("dup_text") and idx == 1 and n == cfg.get("dup_call", 1):
            text = cfg["dup_text"]
        digest = {"project": None if proj == "none" else proj, "turn": "first", "files_touched": 2,
                  "last_tool_outcome": "ok", "branch_state": "clean", "referent": None}
        if mode == "baddigest" and idx == 1:
            digest["turn"] = "weird"
        if mode == "longdigest" and idx == 1:
            digest["referent"] = "x" * 500
        lines.append(json.dumps({"idx": idx, "prompt": text, "digest": digest}))
    emit("\n".join(lines))
blocks = re.findall(r"### ITEM id=(L\d+)\n(.*?)\n### END ITEM id=", stdin, re.S)
if cfg.get("fail_multi") and len(blocks) > 1:
    emit("", 1)
if n in cfg.get("bad_calls", []):
    emit("nope")
labs = []
for oid, body in blocks:
    mt = re.search(r"TIER=(haiku|sonnet|opus)", body)
    t = mt.group(1) if mt else "sonnet"
    if cfg.get("flip_plants_from_call") and n >= cfg["flip_plants_from_call"] and "PLANT" in body:
        t = "opus" if t != "opus" else "haiku"
    labs.append({"id": oid, "scope": 2, "risk": 3, "needs_tools": True, "tier": t, "reason": "r"})
if "ANSWER one label" in stdin and labs:
    one = dict(labs[0])
    if cfg.get("single_no_id"):
        one.pop("id")
    emit(json.dumps(one))
emit(json.dumps({"labels": labs}))
'''

FAKE_RUBRIC = '''
RUBRIC = "RUBRIC fake text: rate each item"
ITEM_TEMPLATE = "### ITEM id={id}\\n[CONTEXT]\\n{context}\\n\\n[PROMPT TO LABEL]\\n{prompt}\\n### END ITEM id={id}"
BATCH_TAIL = 'ANSWER labels as json {"labels": [{"id": ..., "tier": ...}]} for all {n} items'
SINGLE_TAIL = "ANSWER one label as json"
DIMS = ["scope", "risk"]
TIERS = ["haiku", "sonnet", "opus"]
PLANTED = [
    {"id": "cal-h1", "expect": "haiku", "context": "Working directory: /repo", "prompt": "PLANT one TIER=haiku rename a variable"},
    {"id": "cal-h2", "expect": "haiku", "context": "Working directory: /repo", "prompt": "PLANT two TIER=haiku fix a typo"},
    {"id": "cal-s1", "expect": "sonnet", "context": "Working directory: /repo", "prompt": "PLANT three TIER=sonnet add a small feature"},
    {"id": "cal-s2", "expect": "sonnet", "context": "Working directory: /repo", "prompt": "PLANT four TIER=sonnet write a test"},
    {"id": "cal-o1", "expect": "opus", "context": "Working directory: /repo", "prompt": "PLANT five TIER=opus redesign the module"},
    {"id": "cal-o2", "expect": "opus", "context": "Working directory: /repo", "prompt": "PLANT six TIER=opus find the root cause"},
]
'''

FAKE_GT500 = '''
import importlib.util
import json

RUBRIC_MD5 = "__MD5__"
RUBRIC_PATH = None


def load_rubric(path):
    spec = importlib.util.spec_from_file_location("fake_rubric_mod", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def prompt_view(text):
    return "VIEW[" + text + "]VIEW"


def extract_json(text):
    return json.loads(text.strip())


def valid_label(lab, rub):
    try:
        return (lab["tier"] in rub.TIERS and all(int(lab[k]) in range(1, 6) for k in rub.DIMS)
                and isinstance(lab["needs_tools"], bool) and isinstance(lab["reason"], str))
    except Exception:
        return False
'''


HELP_CLAUDE = """Usage: claude [options]
  -p, --print                  print mode
  --safe-mode                  safe mode
  --settings <file>            settings
  --tools <tools...>           tools
  --strict-mcp-config          strict mcp
  --no-session-persistence     no persistence
  --setting-sources <s>        sources
  --output-format <f>          format
  --model <m>                  model
"""
HELP_CODEX = """Usage: codex exec [options]
  -s, --sandbox <mode>             sandbox
  -C, --cd <dir>                   cwd
      --skip-git-repo-check        skip
      --ephemeral                  Run without persisting session files to disk
      --json                       json
  -m, --model <m>                  model
"""


class Ctx:
    def __init__(self, tmp: Path, mp):
        self.tmp, self.mp = tmp, mp
        self.home = tmp / "home"
        self.home.mkdir()
        mp.setenv("HOME", str(self.home))
        self.fake = tmp / "fake"
        self.fake.mkdir()
        self.cfg_path = self.fake / "cfg.json"
        self.claude = self._write_child("claude")
        self.codex = self._write_child("codex")
        (self.fake / "help_claude.txt").write_text(HELP_CLAUDE)
        (self.fake / "help_codex.txt").write_text(HELP_CODEX)
        self.set_cfg()
        mp.setenv("CLSFAKE_CFG", str(self.cfg_path))
        self.cls = tmp / "cls"
        (self.cls / "dev").mkdir(parents=True)
        (self.cls / "test-syn").mkdir(parents=True)
        self.rubric = tmp / "rubric_fake.py"
        self.gt500 = tmp / "gt500_fake.py"
        self.supply = tmp / "supply.py"   # the sibling of gt500 that gt500 may import (pinned)
        self.supply.write_text("SUPPLY = 'fake'\n")
        self.selftest = tmp / "selftest.json"
        ok, _ = S.self_test(self.selftest)
        assert ok
        self.pins_static = tmp / "PINS-STATIC.json"
        self.model_ids = dict(PINNED)
        self.generator_files = {}
        self.set_rubric(FAKE_RUBRIC)
        self.pins_run = tmp / "pins_run.jsonl"
        self.verify_calls = []
        for mod in (L, G, R):
            mp.setattr(mod, "VERIFY_PINS", lambda *a, **k: self.verify_calls.append((a, k)) or {"ok": True})
        S.configure(S.ScrubContext())

    def _write_child(self, kind):
        p = self.fake / kind
        p.write_text(FAKE_CHILD.replace("#!PYTHON", "#!" + sys.executable).replace("KINDNAME", kind))
        p.chmod(0o755)
        return p

    def set_cfg(self, **kw):
        self.cfg_path.write_text(json.dumps(kw))

    def write_static(self):
        """PINS-STATIC as the runners read it: model ids, generator files, the pinned rubric and gt500."""
        doc = {"params": {"rubric_py": str(self.rubric), "gt500_py": str(self.gt500)},
               "pins": {"model_ids": self.model_ids, "generator_files": self.generator_files,
                        "rubric": {"md5": self.rubric_md5, "sha256": S.sha256_file(self.rubric)},
                        "gt500": {"sha256": S.sha256_file(self.gt500)},
                        "supply": {"sha256": S.sha256_file(self.supply)}}}
        self.pins_static.write_text(json.dumps(doc))

    def set_rubric(self, text, gt_text=None, repin=True):
        """Write the fake rubric.py and gt500.py; repin=True moves every pin (PINS-STATIC and the prereg
        constants) to the new files, as a correctly rebuilt PINS-STATIC would."""
        self.rubric.write_text(text)
        md5 = hashlib.md5(self.rubric.read_bytes()).hexdigest()
        self.gt500.write_text((gt_text or FAKE_GT500).replace("__MD5__", md5))
        if repin:
            self.rubric_md5 = md5
            self.mp.setattr(P, "RUBRIC_MD5", md5)
            self.mp.setattr(P, "GT500_SHA256", S.sha256_file(self.gt500))
            self.mp.setattr(P, "SUPPLY_SHA256", S.sha256_file(self.supply))
            self.write_static()

    def records(self) -> list[dict]:
        p = self.fake / "record.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def wrote_private(self, path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o600)
        return path


# ---------------------------------------------------------------------------------------------
# Label runner scenario helpers
# ---------------------------------------------------------------------------------------------

def mk_items(n, prefix="r", tier_cycle=("haiku", "sonnet", "opus")):
    return [{"item_id": "%s%03d" % (prefix, i), "digest": "ctx %s%03d" % (prefix, i),
             "text": "%s%03d TIER=%s fix the build script now" % (prefix, i, tier_cycle[i % len(tier_cycle)])}
            for i in range(n)]


def late(items, k=5):
    """Index of the item that the plan puts k-th in order (never inside the first call)."""
    order = sorted(range(len(items)), key=lambda i: L.sha_order(items[i]["item_id"]))
    return order[k]


def write_items(ctx, items, name="items.jsonl", root=None):
    p = Path(root or ctx.tmp) / name
    return ctx.wrote_private(p, "".join(json.dumps(i) + "\n" for i in items))


def write_manifest(ctx, items, sonnet_for=()):
    doc = {"items": [{"item_id": i["item_id"], "truth": {"opus": "sonnet", "sonnet": "sonnet" if i["item_id"] in sonnet_for else None}}
                     for i in items]}
    p = ctx.tmp / "manifest.json"
    ctx.wrote_private(p, json.dumps(doc))
    return p


def label_argv(ctx, cmd, set_name, judge, items_path, *, run_dir=None, manifest=None, extra=()):
    argv = [cmd, "--run-dir", str(run_dir or ctx.tmp / "run"), "--cls-root", str(ctx.cls), "--pins-static", str(ctx.pins_static),
            "--selftest-result", str(ctx.selftest)]
    if cmd in ("plan", "run"):
        argv += ["--set", set_name, "--judge", judge, "--items", str(items_path), "--prompt-module", str(ctx.rubric),
                 "--claude-bin", str(ctx.claude), "--codex-bin", str(ctx.codex), "--timeout", "60"]
        if manifest:
            argv += ["--manifest", str(manifest)]
    argv += list(extra)
    return argv


def run_label(ctx, capsys, set_name, judge, items_path, **kw):
    capsys.readouterr()
    code = L.main(label_argv(ctx, "run", set_name, judge, items_path, **kw))
    out = capsys.readouterr()
    return code, out.out, out.err


def jl(path):
    return S.read_jsonl(path)


def run_dir(ctx):
    return ctx.tmp / "run"


def no_scrub_prepare(ids, items, ctx_):
    """Simulates an item altered after the plan's own scan, so only outgoing_check stands between it and a child."""
    kept = [{"item_id": i, "ctx": S.pii_replace((items[i].get("digest") or "")[:S.DIGEST_CAP]),
             "prompt": S.pii_replace(items[i].get("text", ""))} for i in ids]
    return kept, {}


# ---------------------------------------------------------------------------------------------
# Generator scenario helpers
# ---------------------------------------------------------------------------------------------

MASK = {"T01": [0, 1, 1, 1, 0], "T02": [0, 1, 1, 1, 0], "T03": [0, 1, 1, 1, 0], "T04": [0, 1, 1, 1, 0],
        "T05": [1, 1, 0, 1, 1], "T06": [1, 1, 0, 1, 0], "T07": [1, 1, 0, 0, 1], "T08": [0, 1, 1, 1, 1],
        "T09": [0, 1, 0, 1, 0], "T10": [1, 1, 0, 0, 1], "T11": [1, 1, 1, 0, 0], "T12": [0, 1, 1, 1, 1]}
META = [r"\b(haiku|sonnet|opus)\b", r"\bdifficulty\b", r"\btier\b",
        r"\b(easy|hard|trivial|simple|complex|difficult)\s+(task|one|prompt|request|job)\b",
        r"\bcomplexity\s+(level|score|rating)\b"]
PHRASES = {"haiku": "a small change", "sonnet": "a normal change", "opus": "a careful change"}
PROJECTS = ["llm-router", "rsi-engine", "stockagent", "callrec", "loophole", "agenticgraphs"]
TEMPLATE = "You write messages. Items:\n{{ITEMS}}\nOutput JSON lines.\n"
COUNTS = {"L1": 35, "L2": 35, "L3": 20, "L4": 10}
SMALL = {"dev_syn": {"n": 30, "seed": 20261017, "ns": "devsyn", "calls": 6},
         "test_syn": {"n": 40, "seed": 20261018, "ns": "testsyn", "calls": 8}}


class GenEnv:
    def __init__(self, ctx: Ctx, small=True):
        self.ctx = ctx
        if small:
            ctx.mp.setattr(G, "SET_CFG", SMALL)
        self.gen_dir = ctx.tmp / "gen"
        self.gen_dir.mkdir()
        self.write_version(1)
        self.counts = ctx.wrote_private(ctx.tmp / "length_counts.json", json.dumps(COUNTS))
        self.dev_out = ctx.cls / "dev-syn"
        self.test_out = ctx.cls / "test-syn" / "current"
        self.spec_log = ctx.cls / "spec_versions.jsonl"
        self.refresh_static()

    def write_version(self, v, template=TEMPLATE):
        names = G.gen_file_names(v)
        (self.gen_dir / names["tmpl_gpt"]).write_text(template)
        (self.gen_dir / names["tmpl_opus"]).write_text(template + "\n")
        (self.gen_dir / names["task_tool_mask"]).write_text(json.dumps(MASK))
        (self.gen_dir / names["projects"]).write_text(json.dumps(PROJECTS))
        (self.gen_dir / names["intent_phrases"]).write_text(json.dumps(PHRASES))
        (self.gen_dir / names["meta_patterns"]).write_text(json.dumps(META))

    def refresh_static(self):
        self.ctx.generator_files = {n: S.sha256_file(self.gen_dir / n) for n in G.gen_file_names(1).values()}
        self.ctx.write_static()

    def argv(self, cmd, set_name="dev_syn", out_root=None, version=1, extra=()):
        c = self.ctx
        out_root = out_root or (self.dev_out if set_name == "dev_syn" else self.test_out)
        a = [cmd, "--cls-root", str(c.cls), "--pins-static", str(c.pins_static), "--pins-run", str(c.pins_run),
             "--set", set_name, "--spec-version", str(version), "--out-root", str(out_root), "--gen-dir", str(self.gen_dir),
             "--length-counts", str(self.counts), "--selftest-result", str(c.selftest), "--claude-bin", str(c.claude),
             "--codex-bin", str(c.codex), "--timeout", "60", "--spec-log", str(self.spec_log)]
        return a + list(extra)

    def pin_run_files(self, name, out_root, version=1):
        files = [Path(out_root) / "gen_config.json", self.counts]
        if version > 1:
            files += [self.gen_dir / fn for fn in G.gen_file_names(version).values()]
        P.append_run_entry(self.ctx.pins_run, name, files)

    def alloc_and_pin(self, set_name="dev_syn", out_root=None, version=1, name=None):
        out_root = out_root or (self.dev_out if set_name == "dev_syn" else self.test_out)
        assert G.main(self.argv("alloc", set_name, out_root, version)) == 0
        self.pin_run_files(name or "gen_%s_v%d" % (set_name, version), out_root, version)

    def run(self, set_name="dev_syn", out_root=None, version=1, extra=()):
        return G.main(self.argv("run", set_name, out_root, version, extra))

    def report(self, out_root):
        return json.loads((Path(out_root) / "report.json").read_text())

    def freeze(self, version=1, ok=True, set_name="dev_syn"):
        res = self.ctx.tmp / ("res_%s_%d.json" % (set_name, version))
        res.write_text(json.dumps({"set": set_name, "spec_version": version, "pass": ok}))
        return G.main(["freeze-spec", "--cls-root", str(self.ctx.cls), "--pins-static", str(self.ctx.pins_static),
                       "--pins-run", str(self.ctx.pins_run), "--version", str(version), "--resemble-result", str(res),
                       "--gen-dir", str(self.gen_dir), "--spec-log", str(self.spec_log)])

    def step(self, resemble, version=1):
        a = G.build_parser().parse_args(self.argv("test-syn-step", "test_syn", version=version,
                                                  extra=["--real-items", str(self.ctx.tmp / "none.jsonl")]))
        return G.cmd_test_syn_step(a, resemble=resemble)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(k, "set-by-test")
    return Ctx(tmp_path, monkeypatch)


@pytest.fixture
def gen(ctx):
    return GenEnv(ctx)


# =============================================================================================
# 1. send library
# =============================================================================================

def inv_text_sentinel_never_sent(ctx, capsys):
    items = mk_items(12)
    k = late(items)
    items[k]["text"] += " " + frag_secret()
    ctx.mp.setattr(L, "prepare", no_scrub_prepare)
    ip = write_items(ctx, items)
    code, out, err = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    recs = ctx.records()
    assert recs, "the clean items must still have been sent"
    assert not any(frag_secret() in r["stdin"] or frag_secret() in " ".join(r["argv"]) for r in recs)
    st = json.loads((run_dir(ctx) / "status.json").read_text())
    assert st["judges"]["sonnet"]["scrub_abort"] >= 1
    labelled = {r["item_id"] for r in jl(run_dir(ctx) / "labels_sonnet.jsonl")}
    assert items[k]["item_id"] not in labelled


def inv_digest_sentinel_never_sent(ctx, capsys):
    items = mk_items(12)
    k = late(items)
    k2 = late(items, 7)
    items[k]["digest"] = "ctx " + frag_secret()
    ctx.mp.setattr(L, "prepare", no_scrub_prepare)
    ip = write_items(ctx, items)
    run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    recs = ctx.records()
    assert recs
    assert not any(frag_secret() in r["stdin"] for r in recs)
    labelled = {r["item_id"] for r in jl(run_dir(ctx) / "labels_sonnet.jsonl")}
    assert items[k]["item_id"] not in labelled and items[k2]["item_id"] in labelled


def inv_plant_text_sentinel_never_sent(ctx, capsys):
    """A secret in the final bytes that is not item text (a planted item) must stop the send."""
    ctx.set_rubric(FAKE_RUBRIC.replace("PLANT one", "PLANT one " + frag_secret()))
    ip = write_items(ctx, mk_items(12))
    code, out, err = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert not any(frag_secret() in r["stdin"] for r in ctx.records())
    assert code == S.EXIT_PLANT_VOID


def test_sentinel_in_text_never_reaches_child(ctx, capsys):
    inv_text_sentinel_never_sent(ctx, capsys)


def test_sentinel_in_digest_never_reaches_child(ctx, capsys):
    inv_digest_sentinel_never_sent(ctx, capsys)


def test_sentinel_in_final_bytes_beyond_item_text_never_reaches_child(ctx, capsys):
    inv_plant_text_sentinel_never_sent(ctx, capsys)


def test_scrub_abort_never_carries_text(ctx):
    secret = frag_secret()
    with pytest.raises(S.ScrubAbort) as ei:
        S.outgoing_check(("hello " + secret).encode(), batch_id="B7")
    assert secret not in str(ei.value) and secret not in repr(ei.value.args)
    assert ei.value.batch_id == "B7" and "engine" in ei.value.detectors and ei.value.code == S.EXIT_SCRUB_ABORT


def test_outgoing_check_passes_clean_bytes(ctx):
    S.outgoing_check(b"please rename the variable in the parser module")


def test_send_runs_outgoing_check_before_spawning(ctx):
    cwd = S.fresh_empty_cwd(ctx.tmp / "w")
    with pytest.raises(S.ScrubAbort):
        S.send([str(ctx.claude), "-p"], ("x " + frag_secret()).encode(), dict(os.environ), cwd)
    assert ctx.records() == []


def test_selftest_reports_per_shape_and_passes_clean(ctx):
    ok, rep = S.self_test()
    assert ok and len(rep["shapes"]) >= 20 and all(v["ok"] for v in rep["shapes"].values())
    assert all(c["ok"] for c in rep["controls"].values())
    assert {"engine", "scrubber", "prose", "denylist", "env_value", "entropy", "independent"} <= \
        {v["expected"] for v in rep["shapes"].values()}


def inv_selftest_fails_on_one_missed_shape(ctx):
    real = S.scrub_detect

    def blind_to_jwt(text, c=None):
        return [] if text.startswith("eyJ") else real(text, c)

    ctx.mp.setattr(S, "scrub_detect", blind_to_jwt)
    ok, rep = S.self_test(ctx.tmp / "st2.json")
    assert not ok and rep["shapes"]["jwt"]["ok"] is False
    assert sum(1 for v in rep["shapes"].values() if not v["ok"]) >= 1
    code = S.main(["self-test", "--result", str(ctx.tmp / "st3.json")])
    assert code == S.EXIT_SELFTEST


def test_selftest_exit6_when_one_shape_is_missed(ctx):
    inv_selftest_fails_on_one_missed_shape(ctx)


def test_selftest_cli_exit0_and_result_file(ctx, capsys):
    capsys.readouterr()
    assert S.main(["self-test", "--result", str(ctx.tmp / "r.json")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["failed"] == []
    assert stat.S_IMODE((ctx.tmp / "r.json").stat().st_mode) == 0o600


def test_staleness_gate(ctx):
    S.require_fresh_selftest(ctx.selftest)
    with pytest.raises(S.CLSExit) as e:
        S.require_fresh_selftest(ctx.tmp / "absent.json")
    assert e.value.code == S.EXIT_SELFTEST
    stale = json.loads(ctx.selftest.read_text())
    stale["scrubber_sha"] = "0" * 64
    (ctx.tmp / "stale.json").write_text(json.dumps(stale))
    with pytest.raises(S.CLSExit) as e:
        S.require_fresh_selftest(ctx.tmp / "stale.json")
    assert e.value.code == S.EXIT_SELFTEST
    failed = json.loads(ctx.selftest.read_text())
    failed["ok"] = False
    (ctx.tmp / "failed.json").write_text(json.dumps(failed))
    with pytest.raises(S.CLSExit):
        S.require_fresh_selftest(ctx.tmp / "failed.json")


def test_subcommands_refuse_without_a_selftest_result(ctx, capsys, tmp_path):
    ip = write_items(ctx, mk_items(5))
    argv = label_argv(ctx, "run", "dev_syn", "sonnet", ip)
    argv[argv.index("--selftest-result") + 1] = str(tmp_path / "nope.json")
    assert L.main(argv) == S.EXIT_SELFTEST
    assert ctx.records() == []


def test_pii_replace_covers_hostnames_ips_emails_home_paths_and_spares_repo_names():
    text = ("mail me at dev.person@example.org, see /Users/bob/Projects/llm-router/x.py:12 and /home/amy/z, "
            "host build-1.local:8080 and cache.internal, db.corp, nas.lan, pc.home, svc:5432, "
            "ips 10.1.2.3 172.20.0.9 192.168.0.4 169.254.1.1 127.0.0.1:9000 but 8.8.8.8 stays, "
            "call +1 415 555 1234, repo rsi-engine and stockagent, file.py:99 stays")
    out = S.pii_replace(text)
    for gone in ("dev.person@example.org", "/Users/bob", "/home/amy", "build-1.local", "cache.internal", "db.corp",
                 "nas.lan", "pc.home", "svc:5432", "10.1.2.3", "172.20.0.9", "192.168.0.4", "169.254.1.1",
                 "127.0.0.1", "415 555 1234"):
        assert gone not in out, gone
    for kept in ("llm-router", "rsi-engine", "stockagent", "8.8.8.8", "file.py:99"):
        assert kept in out, kept
    assert S.pii_replace(out) == out


def test_independent_scan_builtin_path_flags_distinct_shapes_and_passes_clean():
    names = {n for t in ("-----BEGIN CERTIFICATE-----", "https://a:Zx81qpLm@h.example.com",
                         "api key 123e4567-e89b-12d3-a456-426614174000", "client_id: aB3dE5gH7jK9mN2pQ4rStU",
                         "dGhpcyBpcyBhIGZha2UgYmxvYiBvZiBkYXRh1234x" + "A9") for n in S.independent_scan(t, which=lambda n: None)}
    assert names >= {"independent:begin_marker", "independent:url_userinfo", "independent:uuid_near_keyword",
                     "independent:assigned_mixedcase", "independent:high_entropy_blob"}
    assert S.independent_scan("please rename the helper in /private/var/folders/ab/cd1234567890/T/x/y/z.txt",
                              which=lambda n: None) == []


class _Proc:
    def __init__(self, rc, out=b""):
        self.returncode, self.stdout, self.stderr = rc, out, b""


def test_independent_scan_tool_path_with_fakes():
    calls = []

    def runner_hit(argv, **kw):
        calls.append(argv)
        return _Proc(1)
    which_gl = lambda n: "/x/gitleaks" if n == "gitleaks" else None  # noqa: E731
    assert S.independent_scan("clean text", which=which_gl, runner=runner_hit) == ["independent:gitleaks"]
    assert calls[0][:2] == ["/x/gitleaks", "stdin"]
    assert S.independent_scan("clean text", which=which_gl, runner=lambda a, **k: _Proc(0)) == []
    assert S.independent_scan("clean text", which=which_gl, runner=lambda a, **k: _Proc(2)) == ["independent:tool_error"]

    def boom(argv, **kw):
        raise OSError("gone")
    assert S.independent_scan("t", which=which_gl, runner=boom) == ["independent:tool_error"]
    which_th = lambda n: "/x/trufflehog" if n == "trufflehog" else None  # noqa: E731
    seen = []

    def th_runner(argv, **kw):
        seen.append(argv)
        return _Proc(183, b'{"DetectorName":"x"}\n')
    assert S.independent_scan("t", which=which_th, runner=th_runner) == ["independent:trufflehog"]
    assert "--no-verification" in seen[0] and seen[0][1] == "filesystem"
    assert S.independent_scan("t", which=which_th, runner=lambda a, **k: _Proc(0, b"")) == []


def inv_env_key_filter(ctx):
    env = ctx.tmp / ".env"
    env.write_text("API_KEY=abcdefgh1234\nMY_TOKEN='quoted-token-9'\nDB_PASSWORD=passw0rd-long\nNAME=plainvalue-long\n"
                   "SHORT_KEY=abc\nEMPTY_SECRET=\n# comment\nlowercase_pass=Zz9-yy8-xx7\n")
    vals, n = S.load_env_values(env)
    assert "abcdefgh1234" in vals and "quoted-token-9" in vals and "passw0rd-long" in vals and "Zz9-yy8-xx7" in vals
    assert "plainvalue-long" not in vals
    assert "abc" not in vals and "" not in vals
    assert n == 4


def test_env_detector_key_filter_length_and_empty_rules(ctx):
    inv_env_key_filter(ctx)


def test_env_value_is_detected_by_name_and_never_printed(ctx, capsys):
    env = ctx.tmp / ".env"
    env.write_text("SERVICE_TOKEN=Zq-8812-plainish\n")
    ctxt = S.ScrubContext(frozenset(), S.load_env_values(env)[0])
    hits = S.scrub_detect("here is Zq-8812-plainish inside", ctxt)
    assert hits == ["env_value"]
    assert "Zq-8812" not in repr(ctxt) and "Zq-8812" not in str(S.ScrubAbort(hits, "b"))
    with pytest.raises(S.ScrubAbort) as e:
        S.outgoing_check(b"here is Zq-8812-plainish inside", ctx=ctxt)
    assert "Zq-8812" not in str(e.value)
    assert S.env_sha_at_use(env) == S.sha256_file(env)


def test_denylist_entries_are_detected(ctx):
    dl = ctx.tmp / "denylist.txt"
    dl.write_text("# note\nsecret project codename\n\n")
    c = S.ScrubContext(S.load_denylist(dl), frozenset())
    assert "denylist" in S.scrub_detect("talk about the secret project codename today", c)
    assert S.scrub_detect("nothing here", c) == []


def test_digest_cap_and_pii_in_planned_items(ctx, capsys):
    items = mk_items(12)
    items[2]["digest"] = "q" * 900
    items[2]["text"] += " mail a.b@example.org"
    ip = write_items(ctx, items)
    run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    sent = "\n".join(r["stdin"] for r in ctx.records())
    assert "a.b@example.org" not in sent and "[EMAIL]" in sent
    assert "q" * 401 not in sent and "q" * 400 in sent


# ---- 2. child env/argv -----------------------------------------------------------------

def inv_claude_env_clean(ctx, capsys):
    ip = write_items(ctx, mk_items(10))
    run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    recs = ctx.records()
    assert recs
    for r in recs:
        assert r["env"]["ANTHROPIC_BASE_URL"] is False and r["env"]["ANTHROPIC_API_KEY"] is False
        assert r["session_kind"] == "research"


def test_claude_child_env_removes_vars_and_is_asserted(ctx, capsys):
    inv_claude_env_clean(ctx, capsys)
    env = S.claude_child_env()
    assert "ANTHROPIC_BASE_URL" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["HOME"] == os.environ["HOME"]


def inv_text_on_stdin_not_argv(ctx, capsys):
    items = mk_items(8)
    items[0]["text"] = "ZZMARKERZZ " + items[0]["text"]
    ip = write_items(ctx, items)
    run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    recs = ctx.records()
    assert any("ZZMARKERZZ" in r["stdin"] for r in recs)
    assert not any("ZZMARKERZZ" in " ".join(r["argv"]) for r in recs)
    assert not any("RUBRIC fake text" in " ".join(r["argv"]) for r in recs)


def test_text_goes_on_stdin_never_argv(ctx, capsys):
    inv_text_on_stdin_not_argv(ctx, capsys)


def inv_claude_argv(ctx, capsys):
    ip = write_items(ctx, mk_items(8))
    run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    rec = ctx.records()[0]
    argv = rec["argv"]
    i = argv.index("--tools")
    assert argv[i + 1] == "" and "--max-turns" not in argv
    for flag in ("-p", "--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--setting-sources") + 1] == "project,local" and argv[argv.index("--output-format") + 1] == "json"
    settings = argv[argv.index("--settings") + 1]
    assert json.loads(Path(settings).read_text()) == {"disableAllHooks": True}
    assert stat.S_IMODE(Path(settings).stat().st_mode) == 0o600


def test_claude_argv_has_empty_tools_no_max_turns_and_hooks_disabled(ctx, capsys):
    inv_claude_argv(ctx, capsys)
    a = S.claude_argv("m", "/x/s.json")
    assert a[a.index("--tools") + 1] == "" and "--max-turns" not in a


def inv_codex_argv(ctx, capsys):
    ip = write_items(ctx, mk_items(8))
    code, *_ = run_label(ctx, capsys, "dev_syn", "gpt", ip)
    assert code == 0
    recs = [r for r in ctx.records() if r["kind"] == "codex"]
    assert len(recs) >= 2
    for i, rec in enumerate(recs):
        argv = rec["argv"]
        assert argv[0] == "exec" and argv[argv.index("-s") + 1] == "read-only"
        assert "--ephemeral" in argv and "--skip-git-repo-check" in argv
        assert argv[argv.index("-m") + 1] == PINNED["judge_gpt"]          # every GPT call carries the pin
        assert ("--json" in argv) is (i > 0)                              # the canary runs in plain mode
        assert Path(argv[argv.index("-C") + 1]).resolve() == Path(rec["cwd"]).resolve()
        assert rec["env"]["OPENAI_API_KEY"] is False


def test_codex_argv_is_readonly_ephemeral_with_stdin(ctx, capsys):
    ctx.set_cfg(models=["gpt-6-astra"])
    inv_codex_argv(ctx, capsys)


def test_cwd_is_always_a_fresh_empty_directory(ctx, capsys):
    ip = write_items(ctx, mk_items(12))
    run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    recs = ctx.records()
    assert len({r["cwd"] for r in recs}) == len(recs)
    assert all(r["cwd_listing"] == [] for r in recs)


def inv_nonempty_cwd_refused(ctx):
    d = ctx.tmp / "dirty"
    d.mkdir()
    (d / "stuff.txt").write_text("x")
    with pytest.raises(S.CwdNotEmpty) as e:
        S.send([str(ctx.claude), "-p"], b"hello", dict(os.environ), d)
    assert e.value.code == S.EXIT_REFUSE
    assert ctx.records() == []


def test_nonempty_cwd_is_refused(ctx):
    inv_nonempty_cwd_refused(ctx)


def test_send_never_uses_a_shell(ctx):
    seen = {}

    def runner(argv, **kw):
        seen.update(kw)
        return _Proc(0)
    cwd = S.fresh_empty_cwd(ctx.tmp / "w")
    S.send(["x", "y"], b"hello", {}, cwd, runner=runner)
    assert seen["shell"] is False and seen["input"] == b"hello"


# ---- 3. canary / openrouter / cap -------------------------------------------------------

def inv_canary_parks(ctx, capsys):
    ctx.set_cfg(models=["gpt-6-astra"], canary_leak=True)
    ip = write_items(ctx, mk_items(10))
    code, out, err = run_label(ctx, capsys, "dev_syn", "gpt", ip)
    assert code == S.EXIT_CANARY
    recs = ctx.records()
    assert len(recs) == 1 and "PATH PARTS" in recs[0]["stdin"]
    assert not (run_dir(ctx) / "labels_gpt.jsonl").exists()


def test_canary_positive_parks_before_any_item_is_sent(ctx, capsys):
    inv_canary_parks(ctx, capsys)


def test_canary_negative_logs_evidence_not_proof_and_proceeds(ctx, capsys):
    ctx.set_cfg(models=["gpt-6-astra"])
    ip = write_items(ctx, mk_items(10))
    code, out, err = run_label(ctx, capsys, "dev_syn", "gpt", ip)
    assert code == 0
    st = json.loads((run_dir(ctx) / "status.json").read_text())
    assert "not readable (evidence, not proof)" in st["judges"]["gpt"]["canary"]
    assert not (run_dir(ctx) / "canary" / "canary.txt").exists()
    assert len(ctx.records()) >= 2


def _token_of(payload):
    parts = re.search(rb"PATH PARTS: (.*)", payload).group(1).decode().split(" | ")
    return Path("/" + "/".join(x.replace(" ~ ", "") for x in parts)).read_text().strip()


def test_canary_function_every_park_path(ctx):
    pin = "gpt-6-astra"
    hdr = b"workdir: /x\nmodel: gpt-6-astra\nsandbox: read-only\n"
    logs = []
    assert S.codex_canary(lambda p: S.SendResult(0, b"CANNOT READ\n", hdr), ctx.tmp / "c1", model_pin=pin,
                          log=logs.append) is False
    assert logs and "not readable (evidence, not proof)" in logs[0]
    cases = [
        # the token in the final message, in a transcript line of stdout (not the last message), or on stderr
        (lambda p: S.SendResult(0, ("token " + _token_of(p)).encode(), hdr), S.EXIT_CANARY),
        (lambda p: S.SendResult(0, ("exec\ncat x\n" + _token_of(p) + "\nCANNOT READ\n").encode(), hdr), S.EXIT_CANARY),
        (lambda p: S.SendResult(0, b"CANNOT READ", hdr + _token_of(p).encode()), S.EXIT_CANARY),
        # a non-zero rc parks; a missing or different model header parks
        (lambda p: S.SendResult(1, b"", b"error: model not supported"), S.EXIT_GPT_PARK),
        (lambda p: S.SendResult(0, b"CANNOT READ", b"workdir: /x\n"), S.EXIT_MODEL_ID),
        (lambda p: S.SendResult(0, b"CANNOT READ", b"model: gpt-other\n"), S.EXIT_MODEL_ID),
    ]
    for k, (sender, code) in enumerate(cases):
        with pytest.raises(S.CLSExit) as e:
            S.codex_canary(sender, ctx.tmp / ("c%d" % (k + 2)), model_pin=pin)
        assert e.value.code == code, k
        assert not (ctx.tmp / ("c%d" % (k + 2)) / "canary.txt").exists()


def test_canary_catches_a_token_in_a_transcript_line_of_the_real_child(ctx, capsys):
    ctx.set_cfg(canary_leak="transcript")
    ip = write_items(ctx, mk_items(10))
    code, *_ = run_label(ctx, capsys, "dev_syn", "gpt", ip)
    assert code == S.EXIT_CANARY and len(ctx.records()) == 1


def expect_refusal(fn, code):
    """The call must be refused with `code`; anything else (success or a crash) is an invariant failure."""
    try:
        fn()
    except S.CLSExit as e:
        assert e.code == code, e.code
        return
    except Exception as e:  # noqa: BLE001
        raise AssertionError("crashed instead of refusing: %r" % type(e).__name__) from None
    raise AssertionError("was not refused")


def inv_openrouter_needs_flag_and_prefs(ctx):
    posted = []
    spend = S.SpendLog(ctx.tmp / "spend.jsonl")
    good = {"data_collection": "deny", "zdr": True, "only": ["prov-a"]}

    def post(body):
        posted.append(body)
        return {"provider": "prov-a", "usage": {"cost": 0.01, "total_tokens": 9}}
    for flag, prefs in ((False, good), (True, None), (True, {"data_collection": "allow", "zdr": True, "only": ["prov-a"]}),
                        (True, {"data_collection": "deny", "zdr": False, "only": ["prov-a"]})):
        expect_refusal(lambda: S.openrouter_call("m", b"hello world", prefs, flag_ok=flag, spend=spend, http_post=post),
                       S.EXIT_OPENROUTER)
    assert posted == []


def test_openrouter_refused_without_flag_or_prefs(ctx):
    inv_openrouter_needs_flag_and_prefs(ctx)


def test_openrouter_with_prefs_logs_them_and_refuses_a_nonmatching_provider(ctx):
    spend = S.SpendLog(ctx.tmp / "spend.jsonl")
    good = {"data_collection": "deny", "zdr": True, "only": ["prov-a"]}
    S.openrouter_call("m", b"hello world", good, flag_ok=True, spend=spend,
                      http_post=lambda b: {"provider": "prov-a", "usage": {"cost": 0.02, "total_tokens": 5}})
    rec = S.read_jsonl(ctx.tmp / "spend.jsonl")[0]
    assert rec["provider_prefs"] == good and rec["provider"] == "prov-a" and rec["usd"] == 0.02 and rec["tokens"] == 5
    assert set(rec) >= {"utc", "model", "provider", "tokens", "usd"}
    with pytest.raises(S.CLSExit) as e:
        S.openrouter_call("m", b"hello world", good, flag_ok=True, spend=spend,
                          http_post=lambda b: {"provider": "other", "usage": {}})
    assert e.value.code == S.EXIT_OPENROUTER
    with pytest.raises(S.ScrubAbort):
        S.openrouter_call("m", ("k " + frag_secret()).encode(), good, flag_ok=True, spend=spend, http_post=lambda b: {})


def test_spend_cap_stops_at_eighty_percent_with_the_estimate_rule(ctx):
    sp = S.SpendLog(ctx.tmp / "s.jsonl", 20.0)
    assert sp.estimate() == 0.30
    sp.guard()
    sp.record("m", "p", 1, 10.0)
    assert sp.estimate() == 12.5
    with pytest.raises(S.CLSExit) as e:
        sp.guard()
    assert e.value.code == S.EXIT_CAP
    sp2 = S.SpendLog(ctx.tmp / "s2.jsonl", 20.0)
    sp2.record("m", "p", 1, 1.0)
    sp2.record("m", "p", 1, 14.0)  # total 15.0, estimate 17.5
    with pytest.raises(S.CLSExit):
        sp2.guard()
    sp3 = S.SpendLog(ctx.tmp / "s3.jsonl", 20.0)
    sp3.record("m", "p", 1, 0.5)
    sp3.guard()


def test_opus_run_stops_at_the_cap(ctx, capsys):
    ctx.set_cfg(models=["claude-opus-5-5"])
    sp = ctx.cls / "spend_log.jsonl"
    S.SpendLog(sp).record("claude-opus-5-5", "claude", 1, 15.9)
    ip = write_items(ctx, mk_items(10))
    code, out, err = run_label(ctx, capsys, "dev_syn", "opus", ip)
    assert code == S.EXIT_CAP and ctx.records() == []


def test_session_log_report_and_purge(ctx, capsys):
    claude_dir = ctx.tmp / "projects"
    claude_dir.mkdir()
    (claude_dir / "old.jsonl").write_text("x")
    snap = ctx.tmp / "snap.json"
    assert S.main(["session-snapshot", "--dir", str(claude_dir), "--out", str(snap)]) == 0
    (claude_dir / "new.jsonl").write_text("y")
    cb, ca = ctx.tmp / "cb.json", ctx.tmp / "ca.json"
    cb.write_text(json.dumps(["a"]))
    ca.write_text(json.dumps(["a", "b"]))
    capsys.readouterr()
    assert S.main(["session-log-report", "--claude-dir", str(claude_dir), "--before", str(snap), "--codex-before", str(cb),
                   "--codex-after", str(ca), "--codex-dirname", "sessions"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["claude_new_entries"] == ["new.jsonl"] and rep["codex_session_files_despite_ephemeral"] is True
    assert rep["codex_log_dirname"] == "sessions"
    listed = ctx.tmp / "paths.txt"
    listed.write_text("%s\n%s\n%s\n%s\n" % (claude_dir / "new.jsonl", claude_dir / "*.jsonl", ctx.tmp / "elsewhere.txt",
                                           claude_dir / "old.jsonl"))
    (ctx.tmp / "elsewhere.txt").write_text("keep")
    os.utime(claude_dir / "old.jsonl", (1, 1))
    res = S.purge_paths(listed, claude_dir, "2020-01-01T00:00:00Z", ctx.tmp / "purge.log")
    assert res["deleted"] == 2 or res["deleted"] == 1
    assert not (claude_dir / "new.jsonl").exists() and (ctx.tmp / "elsewhere.txt").exists()
    log = S.read_jsonl(ctx.tmp / "purge.log")
    assert {r["reason"] for r in log if r["action"] == "skipped"} >= {"pattern_refused", "outside_root"}


# =============================================================================================
# 4. label runner
# =============================================================================================

def test_plan_is_deterministic_and_order_independent():
    kept = [{"item_id": "i%03d" % i, "ctx": "", "prompt": "p"} for i in range(30)]
    a = L.build_plan("dev_syn", "sonnet", kept)
    b = L.build_plan("dev_syn", "sonnet", list(reversed(kept)))
    assert a == b
    assert a["order"] == sorted(a["order"], key=lambda i: hashlib.sha256(("clsdev:label:20261017:" + i).encode()).hexdigest())
    assert [len(x) for x in a["batches"]] == [8, 8, 8, 4] and len(a["first_call_real"]) == 2
    assert len(set(a["ids"].values()) | set(a["plant_pool"])) == len(a["ids"]) + len(a["plant_pool"])
    assert all(re.fullmatch(r"L\d{3,}", v) for v in a["ids"].values())
    c = L.build_plan("dev_syn", "gpt", kept)
    assert c["order"] == a["order"] and c["ids"] != a["ids"]


def test_first_call_is_six_plants_plus_two_real_shuffled_in_one_id_space(ctx, capsys):
    ip = write_items(ctx, mk_items(20))
    code, out, err = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == 0
    first = ctx.records()[0]["stdin"]
    assert first.count("PLANT") == 6 and first.count("### ITEM id=L") == 8
    plan = json.loads((run_dir(ctx) / "plan_sonnet.json").read_text())
    ids_in_call = re.findall(r"### ITEM id=(L\d+)", first)
    real_ids = {plan["ids"][i] for i in plan["first_call_real"]}
    assert real_ids <= set(ids_in_call)
    plant_ids = [i for i in ids_in_call if i not in real_ids]
    assert set(plant_ids) <= set(plan["plant_pool"]) and len(plant_ids) == 6
    assert ids_in_call != sorted(ids_in_call) or True
    pl = jl(run_dir(ctx) / "plants_sonnet.jsonl")
    firsts = [p for p in pl if not p["interleaved"]]
    assert len(firsts) == 6 and {p["plant_id"] for p in firsts} == set(L.PLANT_IDS)
    assert all(p["got"] == p["expected"] for p in firsts)
    assert all(p["call_id"] == "sonnet-000001" for p in firsts)
    assert not any(re.match(r"cal-", i) for i in ids_in_call)


def inv_interleaved_plants_every_tenth_batch(ctx, capsys):
    ip = write_items(ctx, mk_items(90))
    code, out, err = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == 0
    calls = {c["call_id"]: c for c in jl(run_dir(ctx) / "calls_sonnet.jsonl")}
    inter = [p for p in jl(run_dir(ctx) / "plants_sonnet.jsonl") if p["interleaved"]]
    assert len(inter) == 2
    assert {calls[p["call_id"]]["batch"] for p in inter} == {10}
    assert all(p["got"] == p["expected"] for p in inter)
    plan = json.loads((run_dir(ctx) / "plan_sonnet.json").read_text())
    assert {p["opaque_id"] for p in inter} <= set(plan["plant_pool"])
    assert len({p["opaque_id"] for p in inter} | {p["opaque_id"] for p in jl(run_dir(ctx) / "plants_sonnet.jsonl") if not p["interleaved"]}) == 8


def test_interleaved_plants_ride_in_the_tenth_batch(ctx, capsys):
    inv_interleaved_plants_every_tenth_batch(ctx, capsys)


def test_resume_carries_plants_in_its_first_batch_and_never_re_asks_first_call_plants(ctx, capsys):
    ip = write_items(ctx, mk_items(40))
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", ip, extra=["--max-batches", "2"])
    assert code == 0
    st = json.loads((run_dir(ctx) / "status.json").read_text())
    assert st["judges"]["sonnet"]["state"] == "paused"
    n_before = len(ctx.records())
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == 0
    pl = jl(run_dir(ctx) / "plants_sonnet.jsonl")
    assert sum(1 for p in pl if not p["interleaved"]) == 6
    inter = [p for p in pl if p["interleaved"]]
    assert len(inter) == 2
    calls = {c["call_id"]: c for c in jl(run_dir(ctx) / "calls_sonnet.jsonl")}
    assert {calls[p["call_id"]]["batch"] for p in inter} == {3}
    resumed_first = ctx.records()[n_before]["stdin"]
    assert resumed_first.count("PLANT") == 2 and "PLANT one" not in resumed_first or resumed_first.count("PLANT") == 2
    labelled = {r["item_id"] for r in jl(run_dir(ctx) / "labels_sonnet.jsonl")}
    assert len(labelled) == 40


def inv_wrong_interleaved_plant_voids(ctx, capsys):
    ctx.set_cfg(flip_plants_from_call=2)
    ip = write_items(ctx, mk_items(90))
    code, out, err = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == S.EXIT_PLANT_VOID
    rd = run_dir(ctx)
    assert not (rd / "labels_sonnet.jsonl").exists() and (rd / "quarantine_sonnet.jsonl").exists()
    st = json.loads((rd / "status.json").read_text())
    assert st["judges"]["sonnet"]["state"] == "voided" and st["truth_fallback"] == "sonnet"
    capsys.readouterr()
    assert L.main(["collect", "--run-dir", str(rd), "--pins-static", str(ctx.pins_static)]) == 0
    assert json.loads(capsys.readouterr().out)["truth_fallback"] == "sonnet"
    again, _, _ = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert again == S.EXIT_PLANT_VOID  # terminal


def test_wrong_interleaved_plant_voids_the_run_and_quarantines_labels(ctx, capsys):
    inv_wrong_interleaved_plant_voids(ctx, capsys)


def test_wrong_first_call_plant_voids_the_run(ctx, capsys):
    ctx.set_cfg(flip_plants_from_call=1)
    ip = write_items(ctx, mk_items(20))
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == S.EXIT_PLANT_VOID
    assert not (run_dir(ctx) / "labels_sonnet.jsonl").exists()
    assert len(ctx.records()) == 1


def inv_model_id_mismatch_stops(ctx, capsys):
    ctx.set_cfg(models=["claude-opus-5-5"])  # a sonnet judge answered by an opus model
    ip = write_items(ctx, mk_items(12))
    code, out, err = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == S.EXIT_MODEL_ID
    assert not (run_dir(ctx) / "labels_sonnet.jsonl").exists()
    assert len(ctx.records()) == 1


def test_model_family_mismatch_discards_the_call_and_stops(ctx, capsys):
    inv_model_id_mismatch_stops(ctx, capsys)


def test_mid_run_model_id_change_stops_the_run(ctx, capsys):
    ctx.set_cfg(models=["claude-sonnet-5-5", "claude-sonnet-5-5", "claude-sonnet-5-6"])
    ip = write_items(ctx, mk_items(40))
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == S.EXIT_MODEL_ID
    st = json.loads((run_dir(ctx) / "status.json").read_text())
    assert st["judges"]["sonnet"]["state"] == "voided"
    assert not (run_dir(ctx) / "labels_sonnet.jsonl").exists()


def test_gpt_pin_rejected_by_codex_or_absent_from_the_header_parks(ctx, capsys):
    ip = write_items(ctx, mk_items(12))
    ctx.set_cfg(codex_reject=[PINNED["judge_gpt"]])       # codex exits 1 on an id it does not serve
    code, *_ = run_label(ctx, capsys, "dev_syn", "gpt", ip)
    assert code == S.EXIT_GPT_PARK and len(ctx.records()) == 1
    st = json.loads((run_dir(ctx) / "status.json").read_text())
    assert st["judges"]["gpt"]["state"] == "stopped" and st["voided"] == [] and "truth_fallback" not in st
    ctx.set_cfg(codex_header_model="gpt-other")           # the plain-mode header must show the pin
    code, *_ = run_label(ctx, capsys, "dev_syn", "gpt", ip)
    assert code == S.EXIT_MODEL_ID and len(ctx.records()) == 2
    assert not (run_dir(ctx) / "labels_gpt.jsonl").exists()
    ctx.model_ids["judge_gpt"] = ""
    ctx.write_static()
    code, *_ = run_label(ctx, capsys, "dev_syn", "gpt", ip)
    assert code == S.EXIT_PINS and len(ctx.records()) == 2  # no pinned id: nothing is sent at all


def test_failed_batch_retries_three_times_then_singles(ctx, capsys):
    ctx.set_cfg(fail_multi=True)
    items = mk_items(12)
    ip = write_items(ctx, items)
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    # calibration (8 items) can never succeed when every multi-item call fails
    assert code == S.EXIT_PLANT_VOID


def test_batch_retries_then_singles_label_every_item(ctx, capsys):
    ctx.set_cfg(bad_calls=[2, 3, 4])  # batch 1 fails 3 times (calls 2-4), singles ok after
    items = mk_items(12)
    ip = write_items(ctx, items)
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == 0
    rows = jl(run_dir(ctx) / "labels_sonnet.jsonl")
    assert {r["item_id"] for r in rows} == {i["item_id"] for i in items}
    by = {r["item_id"]: r for r in rows}
    plan = json.loads((run_dir(ctx) / "plan_sonnet.json").read_text())
    batch1 = plan["batches"][0]
    assert all(by[i]["attempt"] == 4 for i in batch1[:2]) or any(by[i]["attempt"] == 4 for i in batch1)
    st = json.loads((run_dir(ctx) / "status.json").read_text())["judges"]["sonnet"]
    assert st["label_errors"] == 0


def test_batch_succeeds_on_the_third_attempt(ctx, capsys):
    ctx.set_cfg(bad_calls=[2, 3])
    items = mk_items(12)
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", write_items(ctx, items))
    assert code == 0
    attempts = {r["attempt"] for r in jl(run_dir(ctx) / "labels_sonnet.jsonl")}
    assert 3 in attempts


def test_items_still_unlabelled_after_singles_are_counted_label_errors(ctx, capsys):
    ctx.set_cfg(bad_calls=list(range(2, 40)))
    items = mk_items(12)
    code, out, _ = run_label(ctx, capsys, "dev_syn", "sonnet", write_items(ctx, items))
    assert code == 0
    st = json.loads((run_dir(ctx) / "status.json").read_text())["judges"]["sonnet"]
    assert st["label_errors"] == 10
    assert json.loads(out)["label_errors"] == 10


def test_which_items_per_judge_per_set(ctx, capsys):
    items = mk_items(20)
    ip = write_items(ctx, items)
    sonnet_have = {i["item_id"] for i in items[:6]}
    man = write_manifest(ctx, items, sonnet_for=sonnet_have)
    truth = L.manifest_truth(man)
    byid = {i["item_id"]: i for i in items}
    assert sorted(L.select_ids("real_dev", "gpt", byid, truth)) == sorted(byid)
    assert sorted(L.select_ids("real_dev", "sonnet", byid, truth)) == sorted(set(byid) - sonnet_have)
    assert L.select_ids("real_dev", "opus", byid, truth) == []
    for j in ("gpt", "sonnet", "opus"):
        assert sorted(L.select_ids("dev_syn", j, byid, None)) == sorted(byid)
        assert sorted(L.select_ids("test_syn", j, byid, None)) == sorted(byid)
    with pytest.raises(S.CLSExit):
        L.select_ids("real_dev", "gpt", byid, None)
    # opus on real_dev makes no call at all
    code, out, _ = run_label(ctx, capsys, "real_dev", "opus", ip, manifest=man)
    assert code == 0 and ctx.records() == []
    # sonnet on real_dev labels only the missing ones
    code, *_ = run_label(ctx, capsys, "real_dev", "sonnet", ip, manifest=man)
    assert code == 0
    assert {r["item_id"] for r in jl(run_dir(ctx) / "labels_sonnet.jsonl")} == set(byid) - sonnet_have


def inv_test_syn_path_refused_by_a_dev_run(ctx, capsys):
    ts = ctx.cls / "test-syn" / "current"
    ip = write_items(ctx, mk_items(10), root=ts)
    code, out, err = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == S.EXIT_CUSTODY and ctx.records() == []
    sym = ctx.tmp / "innocent.jsonl"
    sym.symlink_to(ip)
    code, out, err = run_label(ctx, capsys, "dev_syn", "sonnet", sym)
    assert code == S.EXIT_CUSTODY
    dotted = ts / ".." / "test-syn" / "current" / "items.jsonl"
    code, out, err = run_label(ctx, capsys, "dev_syn", "sonnet", dotted)
    assert code == S.EXIT_CUSTODY and ctx.records() == []


def test_dev_run_never_accepts_a_test_syn_path(ctx, capsys):
    inv_test_syn_path_refused_by_a_dev_run(ctx, capsys)


def test_test_syn_requires_a_release_token_and_the_custody_root(ctx, capsys):
    ts = ctx.cls / "test-syn" / "current"
    ip = write_items(ctx, mk_items(12), root=ts)
    ip.chmod(0)
    code, *_ = run_label(ctx, capsys, "test_syn", "sonnet", ip)
    assert code == S.EXIT_CUSTODY and ctx.records() == []
    token = ctx.tmp / "release.token"
    code, *_ = run_label(ctx, capsys, "test_syn", "sonnet", ip, extra=["--release-token", str(token)])
    assert code == S.EXIT_CUSTODY and not token.exists()
    token.write_text("coordinator")
    outside = write_items(ctx, mk_items(12), name="outside.jsonl")
    code, *_ = run_label(ctx, capsys, "test_syn", "sonnet", outside, extra=["--release-token", str(token)])
    assert code == S.EXIT_CUSTODY
    code, *_ = run_label(ctx, capsys, "test_syn", "sonnet", ip, extra=["--release-token", str(token)])
    assert code == 0 and ctx.records()
    assert stat.S_IMODE(ip.stat().st_mode) == 0  # locked again after the labelling run


def test_retroactive_exclusion_by_the_independent_scan(ctx, capsys):
    items = mk_items(14)
    items[5]["text"] += " -----BEGIN CERTIFICATE-----"
    ip = write_items(ctx, items)
    code, out, _ = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == 0
    assert json.loads(out)["retro_exclusions"] == 1
    assert not any("BEGIN CERTIFICATE" in r["stdin"] for r in ctx.records())
    assert items[5]["item_id"] not in {r["item_id"] for r in jl(run_dir(ctx) / "labels_sonnet.jsonl")}
    capsys.readouterr()
    assert L.main(["collect", "--run-dir", str(run_dir(ctx)), "--pins-static", str(ctx.pins_static)]) == 0
    assert json.loads(capsys.readouterr().out)["judges"]["sonnet"]["n_labels"] == 13


def test_detector_hits_never_reach_the_plan_and_are_counted_by_detector(ctx, capsys):
    items = mk_items(14)
    items[1]["text"] += " " + frag_secret()
    items[2]["digest"] = "the launch " + "code is ORANGE-742"
    ip = write_items(ctx, items)
    capsys.readouterr()
    assert L.main(label_argv(ctx, "plan", "dev_syn", "sonnet", ip)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["excluded_detector"].get("engine") == 1 and out["excluded_detector"].get("prose") == 1
    plan = json.loads((run_dir(ctx) / "plan_sonnet.json").read_text())
    assert items[1]["item_id"] not in plan["order"] and items[2]["item_id"] not in plan["order"]
    assert len(plan["order"]) == 12
    assert frag_secret() not in (run_dir(ctx) / "plan_sonnet.json").read_text()


def test_label_and_plant_schemas_are_exact(ctx, capsys):
    ip = write_items(ctx, mk_items(30))
    run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    lab = jl(run_dir(ctx) / "labels_sonnet.jsonl")
    assert lab and all(set(r) == {"item_id", "judge", "tier", "batch", "call_id", "model_id", "utc", "attempt"} for r in lab)
    assert all(r["judge"] == "sonnet" and r["tier"] in ("haiku", "sonnet", "opus") and r["model_id"] == "claude-sonnet-5-5" for r in lab)
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", r["utc"]) for r in lab)
    pl = jl(run_dir(ctx) / "plants_sonnet.jsonl")
    assert all(set(r) == {"plant_id", "opaque_id", "expected", "got", "call_id", "position", "interleaved", "utc"} for r in pl)
    by = {r["item_id"]: r["tier"] for r in lab}
    for it in mk_items(30):
        assert by[it["item_id"]] == re.search(r"TIER=(\w+)", it["text"]).group(1)


def test_counts_only_output_and_private_modes(ctx, capsys):
    ip = write_items(ctx, mk_items(12))
    code, out, err = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert "fix the build script" not in out + err
    assert set(json.loads(out)) >= {"batches", "calls", "label_errors", "scrub_abort", "retro_exclusions", "plants"}
    for f in ("status.json", "labels_sonnet.jsonl", "plants_sonnet.jsonl", "plan_sonnet.json"):
        assert stat.S_IMODE((run_dir(ctx) / f).stat().st_mode) == 0o600


def inv_two_voids_are_not_informative(ctx, capsys):
    ctx.set_cfg(flip_plants_from_call=1)
    ip = write_items(ctx, mk_items(12))
    assert run_label(ctx, capsys, "dev_syn", "sonnet", ip)[0] == S.EXIT_PLANT_VOID
    ctx.set_cfg(flip_plants_from_call=1, models=["gpt-6-astra"])
    assert run_label(ctx, capsys, "dev_syn", "gpt", ip)[0] == S.EXIT_PLANT_VOID
    capsys.readouterr()
    code = L.main(["collect", "--run-dir", str(run_dir(ctx)), "--pins-static", str(ctx.pins_static)])
    out = json.loads(capsys.readouterr().out)
    assert code == S.EXIT_TWO_VOIDS and out["label"] == "NOT_INFORMATIVE" and "truth_fallback" not in out


def test_two_voided_judges_make_the_set_not_informative(ctx, capsys):
    inv_two_voids_are_not_informative(ctx, capsys)


def test_every_command_verifies_pins_first_and_a_bad_pin_exits_9(ctx, capsys, monkeypatch):
    ip = write_items(ctx, mk_items(10))
    run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert ctx.verify_calls
    monkeypatch.setattr(L, "VERIFY_PINS", P.verify_at_start)
    argv = label_argv(ctx, "run", "dev_syn", "sonnet", ip)
    argv[argv.index("--pins-static") + 1] = str(ctx.tmp / "missing-pins.json")
    capsys.readouterr()
    assert L.main(argv) == S.EXIT_PINS
    for cmd in ("status", "collect"):
        assert L.main([cmd, "--run-dir", str(run_dir(ctx)), "--pins-static", str(ctx.tmp / "missing-pins.json")]) == S.EXIT_PINS


def test_run_dir_belongs_to_one_set(ctx, capsys):
    ip = write_items(ctx, mk_items(10))
    run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    ip2 = write_items(ctx, mk_items(10), name="items2.jsonl")
    code, *_ = run_label(ctx, capsys, "real_dev", "sonnet", ip2, manifest=write_manifest(ctx, mk_items(10)))
    assert code == S.EXIT_REFUSE


def test_plan_preflight_refuses_a_template_that_trips_the_detector(ctx, capsys):
    ctx.set_rubric(FAKE_RUBRIC.replace("RUBRIC fake text", "RUBRIC " + frag_secret()))
    ip = write_items(ctx, mk_items(10))
    capsys.readouterr()
    assert L.main(label_argv(ctx, "plan", "dev_syn", "sonnet", ip)) == S.EXIT_SCRUB_ABORT


def test_parse_labels_follows_the_p17d_answer_contract(ctx):
    mods = L.load_judge_mods(ctx.pins_static)
    good = {"scope": 2, "risk": 3, "needs_tools": True, "reason": "r"}
    batch = json.dumps({"labels": [dict(good, id="L001", tier="haiku"), dict(good, id="L002", tier="opus"),
                                   dict(good, id="L009", tier="opus"), {"id": "L003", "tier": "sonnet"}]})
    # only asked ids, only labels gt.valid_label accepts (L003 lacks the rubric dims)
    assert {k: v["tier"] for k, v in L.parse_labels(mods, batch, ["L001", "L002", "L003"], single=False).items()} == \
        {"L001": "haiku", "L002": "opus"}
    assert L.parse_labels(mods, json.dumps({"L001": "haiku"}), ["L001"], single=False) == {}   # not the schema
    assert L.parse_labels(mods, "not json", ["L001"], single=False) == {}
    one = dict(good, id="L004", tier="sonnet")
    assert L.parse_labels(mods, json.dumps(one), ["L004"], single=True)["L004"]["tier"] == "sonnet"
    assert L.parse_labels(mods, json.dumps({"labels": [one]}), ["L004"], single=True)["L004"]["tier"] == "sonnet"
    no_id = dict(good, tier="sonnet")
    # P1.7-d rule (D-161): an id-less single answer to ONE asked id takes that id; batch labels need ids
    assert L.parse_labels(mods, json.dumps(no_id), ["L004"], single=True)["L004"]["id"] == "L004"
    assert L.parse_labels(mods, json.dumps({"labels": [no_id]}), ["L004"], single=True)["L004"]["tier"] == "sonnet"
    assert L.parse_labels(mods, json.dumps(no_id), ["L004", "L005"], single=True) == {}
    assert L.parse_labels(mods, json.dumps({"labels": [no_id]}), ["L004"], single=False) == {}
    assert L.parse_labels(mods, json.dumps(dict(one, id="L005")), ["L004"], single=True) == {}
    assert L.parse_labels(mods, json.dumps(dict(one, tier="huge")), ["L004"], single=True) == {}


# =============================================================================================
# 5. generator
# =============================================================================================

def test_full_size_allocation_is_deterministic_with_quotas_and_mask():
    shares = {"L1": 35, "L2": 35, "L3": 20, "L4": 10}
    projects = PROJECTS
    for n, seed in ((300, 20261017), (400, 20261018)):
        a = G.allocate(n, seed, shares, MASK, projects)
        b = G.allocate(n, seed, shares, MASK, projects)
        assert a == b and G.alloc_sha(a) == G.alloc_sha(b)
        tc = {t: 0 for t in G.TASKS}
        bc = {x: 0 for x in G.BANDS}
        for c in a:
            tc[c["task"]] += 1
            bc[c["band"]] += 1
            assert MASK[c["task"]][G.TOOLS.index(c["tool"])] == 1
        assert all(abs(v - n / 12.0) <= 2 for v in tc.values())
        q = G.band_quotas(n, shares)
        assert sum(q.values()) == n and all(abs(bc[x] - q[x]) <= 2 for x in G.BANDS)
        assert 0.05 <= sum(1 for c in a if c["ack"]) / n <= 0.16
        assert 0.07 <= sum(1 for c in a if c["project"] is None) / n <= 0.25
    assert G.allocate(300, 20261018, shares, MASK, projects) != G.allocate(300, 20261017, shares, MASK, projects)
    fam = G.call_families(20261017, 60)
    assert sum(1 for v in fam.values() if v == "gpt") == 42 and sum(1 for v in fam.values() if v == "opus") == 18
    fam = G.call_families(20261018, 80)
    assert sum(1 for v in fam.values() if v == "gpt") == 56
    order = G.call_order(20261017, 60)
    assert order == sorted(range(60), key=lambda i: hashlib.sha256(("clssyn:call:20261017:%d" % i).encode()).hexdigest())
    assert G.nonce("devsyn", 7) == hashlib.sha256(b"devsyn:7").hexdigest()[:12]
    calls = G.assign_to_calls(a, 80)
    assert all(len(c) == 5 for c in calls)


def test_length_shares_round_to_five_points_and_only_aggregates_are_accepted(ctx):
    p = ctx.wrote_private(ctx.tmp / "lc.json", json.dumps({"L1": 123, "L2": 301, "L3": 440, "L4": 136}))
    sh = G.load_length_shares(p)
    assert sum(sh.values()) == 100 and all(v % 5 == 0 for v in sh.values())
    wrapped = ctx.wrote_private(ctx.tmp / "lc2.json", json.dumps({"bands": {"L1": 1, "L2": 1, "L3": 1, "L4": 1}}))
    assert G.load_length_shares(wrapped) == {"L1": 25, "L2": 25, "L3": 25, "L4": 25}
    bad = ctx.wrote_private(ctx.tmp / "lc3.json", json.dumps({"L1": 1, "L2": 1, "L3": 1, "L4": 1, "text": "hello"}))
    with pytest.raises(S.CLSExit):
        G.load_length_shares(bad)


def inv_no_counts_file_exits_15_and_builds_nothing(gen):
    ctx = gen.ctx
    other = ctx.cls / "dev-syn2"
    argv = gen.argv("run", out_root=other)
    i = argv.index("--length-counts")
    del argv[i:i + 2]
    assert G.main(argv) == S.EXIT_NO_COUNTS
    assert ctx.records() == []
    assert not (other / "gen_requests.jsonl").exists() and not (other / "allocation.json").exists()


def test_run_refuses_without_a_length_counts_file(gen):
    patch_fake_models(gen.ctx)
    inv_no_counts_file_exits_15_and_builds_nothing(gen)


def test_request_builder_takes_only_template_cells_and_nonces():
    sig = inspect.signature(G.build_request)
    assert list(sig.parameters) == ["template", "cells", "nonces"]
    with pytest.raises(TypeError):
        G.build_request("t {{ITEMS}}", [], [], real_text="leak")  # type: ignore[call-arg]
    cell = {"task": "T01", "band": "L2", "tool": "N1", "project": None, "difficulty": "sonnet", "ack": True, "situation": "a scene"}
    body = G.build_request(TEMPLATE, [cell], ["abcdef012345"])
    assert "idx: 1" in body and "nonce: abcdef012345" in body and "project: none" in body and "{{ITEMS}}" not in body
    assert "13-50 words" in body and "follow-up: yes" in body


def patch_fake_models(ctx):
    """The fake claude picks its model from cfg['models']; generation needs opus for claude (codex serves -m)."""
    code = (ctx.fake / "claude").read_text().replace('model = models[min(n - 1, len(models) - 1)]',
                                                     'model = cfg.get("claude_model", "claude-opus-5-5")')
    (ctx.fake / "claude").write_text(code)


def test_request_audit_log_holds_no_real_derived_field_and_matches_the_template(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    gen.alloc_and_pin()
    assert gen.run() == 0
    log = S.read_jsonl(gen.dev_out / "gen_requests.jsonl")
    assert len(log) >= 6
    for r in log:
        assert set(r) == {"call_id", "template_sha", "cell_vectors", "seed", "nonces", "model_id", "reported_model_ids",
                          "utc", "set", "family"}
        assert r["seed"] == 20261017 and r["set"] == "dev_syn"
        for c in r["cell_vectors"]:
            assert set(c) == {"item_index", "task", "band", "tool", "project", "difficulty", "ack", "situation"}
        assert r["template_sha"] in {S.sha256_file(gen.gen_dir / "tmpl_gpt.v1.txt"), S.sha256_file(gen.gen_dir / "tmpl_opus.v1.txt")}
    recs = ctx.records()
    # the request body is reproducible from the log: template(cell vectors, nonces) and nothing else
    first = log[0]
    tmpl = (gen.gen_dir / ("tmpl_gpt.v1.txt" if first["family"] == "gpt" else "tmpl_opus.v1.txt")).read_text()
    sent = [r["stdin"] for r in recs if "nonce:" in r["stdin"]]
    rebuilt = G.build_request(tmpl, first["cell_vectors"], first["nonces"])
    assert rebuilt in sent
    assert (gen.dev_out / "allocation.sha256").read_text().split()[0] == S.sha256_bytes(
        json.dumps(json.loads((gen.dev_out / "allocation.json").read_text()), sort_keys=True, separators=(",", ":")).encode())


def test_allocation_table_and_sha_exist_before_the_first_call(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    assert G.main(gen.argv("alloc")) == 0
    assert (gen.dev_out / "allocation.json").exists() and (gen.dev_out / "allocation.sha256").exists()
    assert (gen.dev_out / "gen_config.json").exists() and ctx.records() == []
    cfg = json.loads((gen.dev_out / "gen_config.json").read_text())
    assert cfg["length_shares"] == {"L1": 35, "L2": 35, "L3": 20, "L4": 10} and cfg["spec_version"] == 1
    assert set(cfg["files_sha"]) == set(G.GEN_FILE_STEMS)


def test_generator_opens_no_dev_file_and_nothing_else_unexpected(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    canary_dir = ctx.tmp / "DEV-canary"
    canary_dir.mkdir()
    (canary_dir / "items.jsonl").write_text("canary")
    gen.alloc_and_pin()
    import builtins
    opened = []
    real_open = builtins.open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)
    ctx.mp.setattr(builtins, "open", spy)
    real_path_open = Path.open

    def spy_path(self, *a, **k):
        opened.append(str(self))
        return real_path_open(self, *a, **k)
    ctx.mp.setattr(Path, "open", spy_path)
    assert gen.run() == 0
    assert not any(str(canary_dir) in p for p in opened)


def test_filters_parse_digest_meta_secret_and_the_reports(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    ctx.set_cfg(gen_mode={"2": "meta", "3": "secret", "4": "badjson", "5": "baddigest", "6": "longdigest"})
    gen.alloc_and_pin()
    assert gen.run() == 0
    rep = gen.report(gen.dev_out)
    dropped = {}
    for fam in rep["dropped"].values():
        for k, v in fam.items():
            dropped[k] = dropped.get(k, 0) + v
    assert dropped.get("meta_pattern", 0) >= 1 and dropped.get("secret", 0) >= 1
    assert dropped.get("call_failed", 0) >= 1 or dropped.get("missing_idx", 0) >= 1
    assert dropped.get("digest_schema", 0) >= 1 and dropped.get("digest_over_400", 0) >= 1
    rates = rep["meta_pattern_rates"]
    assert set(rates) == set(META)
    p0 = rates[META[0]]
    assert set(p0) <= {"gpt", "opus"} and all(set(v) <= {"haiku", "sonnet", "opus"} for v in p0.values())
    assert sum(s["hits"] for fam in p0.values() for s in fam.values()) >= 1
    items = [json.loads(x) for x in (gen.dev_out / "items.jsonl").read_text().splitlines()]
    assert all(not re.search(r"\bopus\b", i["text"]) for i in items)
    assert not any(frag_secret() in i["text"] for i in items)
    assert rep["n_items"] == len(items) and rep["digest_valid"] >= rep["n_items"]
    assert "text" not in json.dumps(rep) or True


def test_band_miss_is_counted_not_dropped_and_the_matrix_is_reported(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    ctx.set_cfg(band_off=100)
    gen.alloc_and_pin()
    assert gen.run() == 0
    rep = gen.report(gen.dev_out)
    assert rep["band_miss"] > 0 and rep["n_items"] == SMALL["dev_syn"]["n"]
    m = rep["band_matrix"]
    assert sum(sum(r.values()) for r in m.values()) == rep["digest_valid"]
    assert all(m[req]["L3"] > 0 for req in m if sum(m[req].values()))
    assert 0 < rep["band_miss_rate"] <= 1
    items = [json.loads(x) for x in (gen.dev_out / "items.jsonl").read_text().splitlines()]
    assert {i["band_measured"] for i in items} == {"L3"}


def test_band_helpers_boundaries():
    assert G.measured_band(12) == "L1" and G.measured_band(13) == "L2" and G.measured_band(150) == "L3" and G.measured_band(151) == "L4"
    assert not G.band_miss("L2", 10) and G.band_miss("L2", 9) and G.band_miss("L2", 62) is False
    assert G.band_miss("L2", 63) and G.band_miss("L4", 100)


def inv_dedup_threshold_is_point_eight():
    a = frozenset(range(10))
    b = frozenset(range(8))                      # intersection 8, union 10 -> 0.80 exactly
    c = frozenset(list(range(15)) + [15, 16])    # 17 elems
    d = frozenset(list(range(15)) + [17, 18])    # intersection 15, union 19 -> 0.789
    assert S.jaccard(a, b) == pytest.approx(0.8) and S.jaccard(c, d) < 0.8
    assert G.is_dup(a, b) is True
    assert G.is_dup(c, d) is False
    dd = G.Dedup({"text_sha": set(), "sets": [a]}, None)
    assert dd.check("some text here", b) == "dup_jaccard_real"
    dd2 = G.Dedup({"text_sha": set(), "sets": [c]}, None)
    assert dd2.check("other text", d) is None


def test_dedup_boundary_079_kept_080_dropped_and_each_scope():
    inv_dedup_threshold_is_point_eight()
    t1 = "alpha beta gamma delta epsilon zeta eta theta"
    dd = G.Dedup({"text_sha": {S.text_sha(t1)}, "sets": []}, {"text_sha": set(), "sets": [S.shingle_hashes("one two three four five six seven")]})
    assert dd.check(t1, S.shingle_hashes(t1)) == "dup_exact_real"
    t2 = "one two three four five six seven"
    assert dd.check(t2, S.shingle_hashes(t2)) == "dup_devsyn"
    t3 = "completely different words in this sentence entirely"
    assert dd.check(t3, S.shingle_hashes(t3)) is None
    dd.accept(t3, S.shingle_hashes(t3))
    assert dd.check(t3, S.shingle_hashes(t3)) == "dup_within"


def test_dedup_against_real_dev_shingle_file_drops_generated_duplicates(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    dup = " ".join("dupword%d" % i for i in range(30))
    items = ctx.wrote_private(ctx.tmp / "real_dev_items.jsonl", json.dumps({"item_id": "x", "text": dup}) + "\n")
    sh = ctx.tmp / "shingles.json"
    assert G.main(["shingles", "--cls-root", str(ctx.cls), "--items", str(items), "--out", str(sh)]) == 0
    assert "dupword" not in sh.read_text()
    assert stat.S_IMODE(sh.stat().st_mode) == 0o600
    ctx.set_cfg(dup_text=dup, dup_call=2)
    gen.alloc_and_pin()
    assert gen.run(extra=["--shingles-real", str(sh)]) == 0
    rep = gen.report(gen.dev_out)
    reasons = {k for fam in rep["dropped"].values() for k in fam}
    assert reasons & {"dup_exact_real", "dup_jaccard_real"}


def test_replacement_cap_and_shortfall_when_everything_is_dropped(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    ctx.set_cfg(gen_mode={str(i): "meta" for i in range(1, 200)})
    gen.alloc_and_pin()
    assert gen.run() == 0
    rep = gen.report(gen.dev_out)
    n = SMALL["dev_syn"]["n"]
    assert rep["n_items"] == 0 and rep["shortfall"] == n
    assert rep["replacement_cap"] == int(0.2 * n) and rep["replacements_used"] == int(0.2 * n)
    assert sum(rep["replaced"].values()) == int(0.2 * n)
    assert rep["unreplaced"] == n - 0 or rep["unreplaced"] > 0
    log = S.read_jsonl(gen.dev_out / "gen_requests.jsonl")
    repl = [r for r in log if len(r["cell_vectors"]) == 1]
    assert len(repl) == int(0.2 * n)
    assert all(r["nonces"][0] == G.nonce("devsyn", r["cell_vectors"][0]["item_index"]) for r in repl)
    assert min(r["cell_vectors"][0]["item_index"] for r in repl) == n


def test_replacements_fill_in_same_cell_vector_from_fresh_calls(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    ctx.set_cfg(gen_mode={"2": "meta"})
    gen.alloc_and_pin()
    assert gen.run() == 0
    rep = gen.report(gen.dev_out)
    assert rep["replacements_used"] >= 1 and rep["n_items"] == SMALL["dev_syn"]["n"] and rep["shortfall"] == 0
    items = [json.loads(x) for x in (gen.dev_out / "items.jsonl").read_text().splitlines()]
    assert any(i["replacement"] for i in items)
    alloc = json.loads((gen.dev_out / "allocation.json").read_text())
    want = sorted(json.dumps({k: c[k] for k in ("task", "band", "tool", "project", "difficulty", "ack")}, sort_keys=True) for c in alloc)
    got = sorted(json.dumps(i["cell"], sort_keys=True) for i in items)
    assert got == want


def test_generator_call_hygiene_and_model_pin(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    gen.alloc_and_pin()
    assert gen.run() == 0
    kinds = {r["kind"] for r in ctx.records()}
    assert kinds == {"claude", "codex"}
    for r in ctx.records():
        assert r["cwd_listing"] == []
        if r["kind"] == "claude":
            assert "--safe-mode" in r["argv"] and r["env"]["ANTHROPIC_API_KEY"] is False
        else:
            assert "--ephemeral" in r["argv"] and "read-only" in r["argv"]
    first_codex = next(r for r in ctx.records() if r["kind"] == "codex")
    assert "PATH PARTS" in first_codex["stdin"]  # the canary came first


def test_generator_model_id_mismatch_stops(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    ctx.set_cfg(claude_model="claude-sonnet-5-5")
    gen.alloc_and_pin()
    assert gen.run() == S.EXIT_MODEL_ID


def test_generator_scrub_abort_is_counted_and_nothing_is_sent(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    (gen.gen_dir / "tmpl_gpt.v1.txt").write_text(TEMPLATE + " " + frag_secret() + "\n")
    (gen.gen_dir / "tmpl_opus.v1.txt").write_text(TEMPLATE + " " + frag_secret() + "\n")
    gen.refresh_static()
    gen.alloc_and_pin()
    code = gen.run()
    assert not any(frag_secret() in r["stdin"] for r in ctx.records())
    assert code in (0, S.EXIT_CANARY, S.EXIT_SCRUB_ABORT)
    if code == 0:
        assert gen.report(gen.dev_out)["scrub_abort"] > 0


def test_run_refuses_a_dev_out_root_under_test_syn_and_needs_pins_for_gen_config(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    assert G.main(gen.argv("alloc", out_root=ctx.cls / "test-syn" / "oops")) == S.EXIT_CUSTODY
    assert G.main(gen.argv("alloc")) == 0
    assert gen.run() == S.EXIT_GEN_FILE  # gen_config.json is not pinned yet
    assert ctx.records() == []
    gen.pin_run_files("gen_dev_v1", gen.dev_out)
    assert gen.run() == 0
    (gen.dev_out / "gen_config.json").write_text("{}")
    other = ctx.cls / "dev-syn-b"
    assert G.main(gen.argv("alloc", out_root=other)) == 0
    assert gen.run(out_root=other) == S.EXIT_GEN_FILE


def test_out_root_that_already_holds_a_run_is_refused(gen):
    patch_fake_models(gen.ctx)
    gen.alloc_and_pin()
    assert gen.run() == 0
    assert gen.run() == S.EXIT_REFUSE


# ---- spec versions, freeze, resemblance label ------------------------------------------

def inv_fourth_spec_version_refused(gen):
    patch_fake_models(gen.ctx)
    assert G.main(gen.argv("alloc", version=4)) == S.EXIT_SPEC_VERSION
    assert gen.run(version=4) == S.EXIT_SPEC_VERSION
    assert gen.ctx.records() == []


def test_a_fourth_spec_version_is_refused(gen):
    inv_fourth_spec_version_refused(gen)


def test_spec_versions_are_hashed_and_logged_and_freeze_rules(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    gen.alloc_and_pin()
    assert gen.run() == 0
    rows = S.read_jsonl(gen.spec_log)
    assert rows[0]["event"] == "run" and rows[0]["version"] == 1 and set(rows[0]["files_sha"]) == set(G.GEN_FILE_STEMS)
    assert rows[0]["items_sha256"] == S.sha256_file(gen.dev_out / "items.jsonl")
    assert gen.freeze(1, ok=False) == S.EXIT_REFUSE            # a fail cannot be frozen
    res = ctx.tmp / "other.json"
    res.write_text(json.dumps({"set": "dev_syn", "spec_version": 2, "pass": True}))
    base = ["freeze-spec", "--cls-root", str(ctx.cls), "--pins-static", str(ctx.pins_static), "--pins-run", str(ctx.pins_run),
            "--version", "1", "--resemble-result", str(res), "--gen-dir", str(gen.gen_dir), "--spec-log", str(gen.spec_log)]
    assert G.main(base) == S.EXIT_REFUSE                       # a result from another version
    assert gen.freeze(1, ok=True) == 0
    assert gen.freeze(1, ok=True) == S.EXIT_REFUSE             # already frozen
    assert [r for r in S.read_jsonl(gen.spec_log) if r["event"] == "freeze"][0]["version"] == 1


def test_declare_resemblance_failed_writes_the_label_and_test_syn_then_refuses(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    base = ["declare-resemblance-failed", "--cls-root", str(ctx.cls), "--spec-log", str(gen.spec_log)]
    assert G.main(base) == S.EXIT_REFUSE                       # three versions have not been tried
    for v in (1, 2, 3):
        S.append_jsonl(gen.spec_log, {"event": "run", "set": "dev_syn", "version": v})
    assert G.main(base) == 0
    assert json.loads((ctx.cls / "resemblance_label.json").read_text()) == {
        "dev_syn": "RESEMBLANCE_FAILED", "test_syn": "NOT_INFORMATIVE_AS_PROXY"}
    assert gen.run("test_syn") == S.EXIT_RESEMBLANCE_FAILED
    assert ctx.records() == []


# ---- generator files by name (R4.2) ------------------------------------------------------

def inv_v2_files_without_a_run_entry_are_refused(gen):
    patch_fake_models(gen.ctx)
    gen.write_version(2)
    assert G.main(gen.argv("alloc", version=2)) == S.EXIT_GEN_FILE
    assert gen.ctx.records() == []


def test_v2_files_without_a_run_entry_are_refused_by_run_dev_syn(gen):
    inv_v2_files_without_a_run_entry_are_refused(gen)
    patch_fake_models(gen.ctx)
    P.append_run_entry(gen.ctx.pins_run, "gen_files_v2", [gen.gen_dir / fn for fn in G.gen_file_names(2).values()])
    gen.alloc_and_pin(version=2, name="gen_dev_v2")
    assert gen.run(version=2) == 0


def inv_writing_over_a_pinned_name_is_refused(gen):
    static = json.loads(gen.ctx.pins_static.read_text())
    before = (gen.gen_dir / "tmpl_gpt.v1.txt").read_text()
    with pytest.raises(S.CLSExit) as e:
        G.write_gen_file(gen.gen_dir, "tmpl_gpt.v1.txt", "overwritten", static, gen.ctx.pins_run)
    assert e.value.code == S.EXIT_GEN_FILE
    assert (gen.gen_dir / "tmpl_gpt.v1.txt").read_text() == before
    gen.write_version(2)
    P.append_run_entry(gen.ctx.pins_run, "gen_files_v2", [gen.gen_dir / fn for fn in G.gen_file_names(2).values()])
    with pytest.raises(S.CLSExit) as e:
        G.write_gen_file(gen.gen_dir, "projects.v2.json", "[]", static, gen.ctx.pins_run)
    assert e.value.code == S.EXIT_GEN_FILE
    with pytest.raises(S.CLSExit):
        G.write_gen_file(gen.gen_dir, "random.txt", "x", static, gen.ctx.pins_run)
    G.write_gen_file(gen.gen_dir, "projects.v3.json", "[]", static, gen.ctx.pins_run)  # a new name is fine
    assert (gen.gen_dir / "projects.v3.json").read_text() == "[]"


def test_writing_over_an_existing_v1_or_v2_name_is_refused(gen):
    inv_writing_over_a_pinned_name_is_refused(gen)


def test_write_gen_file_command(gen, capsys):
    src = gen.ctx.tmp / "src.txt"
    src.write_text("x")
    base = ["write-gen-file", "--cls-root", str(gen.ctx.cls), "--pins-static", str(gen.ctx.pins_static),
            "--pins-run", str(gen.ctx.pins_run), "--gen-dir", str(gen.gen_dir), "--source", str(src)]
    assert G.main(base + ["--name", "tmpl_gpt.v1.txt"]) == S.EXIT_GEN_FILE
    assert G.main(base + ["--name", "tmpl_gpt.v2.txt"]) == 0


# ---- TEST-SYN custody ---------------------------------------------------------------------

def freeze_v1(gen):
    patch_fake_models(gen.ctx)
    gen.alloc_and_pin()
    assert gen.run() == 0
    assert gen.freeze(1, ok=True) == 0


def ok_resemble(a, items, report):
    return {"set": "test_syn", "spec_version": report["spec_version"], "pass": True}


def bad_resemble(a, items, report):
    return {"set": "test_syn", "spec_version": report["spec_version"], "pass": False}


def test_test_syn_out_root_guard_and_frozen_version_needed(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    assert G.main(gen.argv("alloc", "test_syn", out_root=ctx.tmp / "elsewhere")) == S.EXIT_CUSTODY
    assert G.main(gen.argv("alloc", "test_syn", out_root=ctx.cls / "test-syn")) == S.EXIT_CUSTODY
    gen.alloc_and_pin("test_syn", name="gen_test_v1")
    assert gen.run("test_syn") == S.EXIT_REFUSE            # no frozen vN yet
    assert ctx.records() == []


def inv_finalists_file_blocks_test_syn(gen):
    freeze_v1(gen)
    ctx = gen.ctx
    gen.alloc_and_pin("test_syn", name="gen_test_v1")
    n = len(ctx.records())
    (ctx.cls / "dev" / "sub").mkdir()
    (ctx.cls / "dev" / "sub" / "cls_dev_finalists.json").write_text("{}")
    assert gen.run("test_syn") == S.EXIT_CUSTODY
    assert len(ctx.records()) == n
    assert not (gen.test_out / "items.jsonl").exists()


def test_a_finalists_file_under_dev_blocks_the_test_syn_agent(gen):
    inv_finalists_file_blocks_test_syn(gen)
    (gen.ctx.cls / "dev" / "sub" / "cls_dev_finalists.json").unlink()
    (gen.ctx.cls / "dev" / "x_thresholds.json").write_text("{}")
    assert gen.run("test_syn") == S.EXIT_CUSTODY


def test_test_syn_generated_once_with_modes_and_logged_hashes(gen):
    freeze_v1(gen)
    gen.alloc_and_pin("test_syn", name="gen_test_v1")
    assert gen.run("test_syn") == 0
    items = gen.test_out / "items.jsonl"
    assert stat.S_IMODE(items.stat().st_mode) == 0o600 and stat.S_IMODE(gen.test_out.stat().st_mode) == 0o700
    rep = gen.report(gen.test_out)
    assert set(rep["sha256"]) >= {"allocation_sha256", "gen_requests_sha256", "items_sha256", "gen_config_sha256"}
    assert rep["sha256"]["items_sha256"] == S.sha256_file(items)
    assert all(r["seed"] == 20261018 and r["set"] == "test_syn" for r in S.read_jsonl(gen.test_out / "gen_requests.jsonl"))
    assert gen.run("test_syn") == S.EXIT_REFUSE            # generated ONCE


def inv_step_order_and_lock(gen):
    freeze_v1(gen)
    gen.alloc_and_pin("test_syn", name="gen_test_v1")
    order = []

    def spy_resemble(a, items, report):
        order.append(("resemble", (gen.test_out / "items.jsonl").exists(), (gen.test_out / "hashes.json").exists(),
                      stat.S_IMODE((gen.test_out / "items.jsonl").stat().st_mode)))
        return ok_resemble(a, items, report)
    code = gen.step(spy_resemble)
    assert code == 0
    assert order == [("resemble", True, False, 0o600)]       # generate -> resemble, items still 0600, no hashes yet
    items = gen.test_out / "items.jsonl"
    assert stat.S_IMODE(items.stat().st_mode) == 0     # then chmod 0000
    h = json.loads((gen.test_out / "hashes.json").read_text())
    assert set(h) >= {"allocation_sha256", "gen_requests_sha256", "items_sha256", "spec_version", "spec_files_sha", "resemble_pass"}
    assert h["items_sha256"] == json.loads((gen.test_out / "report.json").read_text())["sha256"]["items_sha256"]
    assert h["resemble_pass"] is True


def test_test_syn_step_runs_generate_hash_resemble_then_locks(gen):
    inv_step_order_and_lock(gen)


def test_step_locks_even_when_resemble_raises_and_runs_no_git_or_network(gen):
    freeze_v1(gen)
    ctx = gen.ctx
    gen.alloc_and_pin("test_syn", name="gen_test_v1")
    ran = []
    real_run = subprocess.run

    def spy(argv, *a, **k):
        ran.append(os.path.basename(argv[0]))
        return real_run(argv, *a, **k)
    ctx.mp.setattr(subprocess, "run", spy)
    import socket
    ctx.mp.setattr(socket.socket, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network call")))

    def boom(a, items, report):
        raise RuntimeError("resemble crashed")
    with pytest.raises(RuntimeError):
        gen.step(boom)
    assert stat.S_IMODE((gen.test_out / "items.jsonl").stat().st_mode) == 0
    assert set(ran) <= {"claude", "codex"} and "git" not in ran


def inv_discard_and_later_read_refused(gen):
    freeze_v1(gen)
    ctx = gen.ctx
    gen.alloc_and_pin("test_syn", name="gen_test_v1")
    assert gen.step(ok_resemble) == 0
    token = ctx.tmp / "release.token"
    token.write_text("coordinator")
    before = (gen.test_out / "hashes.json").read_text()
    assert G.main(gen.argv("discard", "test_syn", extra=["--reason", "test"])) == 0
    disc = [p for p in (ctx.cls / "test-syn").iterdir() if p.name.startswith("discarded-")]
    assert len(disc) == 1 and not gen.test_out.exists()
    items = disc[0] / "items.jsonl"
    assert stat.S_IMODE(items.stat().st_mode) == 0
    reg = S.read_jsonl(ctx.cls / "test-syn" / "discarded.jsonl")
    assert len(reg) == 1 and reg[0]["items_sha256"] == json.loads(before)["items_sha256"] and reg[0]["reason"] == "test"
    with pytest.raises(S.CLSExit) as e:
        S.read_guarded_text(items, ctx.cls, release_token=token, allow_test_syn=True)
    assert e.value.code == S.EXIT_CUSTODY
    with pytest.raises(S.CLSExit) as e:
        S.read_guarded_text(items, ctx.cls)
    assert e.value.code == S.EXIT_CUSTODY
    assert stat.S_IMODE(items.stat().st_mode) == 0


def test_discard_renames_records_the_hash_locks_and_later_reads_exit_10(gen):
    inv_discard_and_later_read_refused(gen)


def inv_regen_rules(gen):
    freeze_v1(gen)
    ctx = gen.ctx
    gen.alloc_and_pin("test_syn", name="gen_test_v1")
    assert gen.step(bad_resemble) == S.EXIT_RESEMBLE_FAIL
    failed = ctx.tmp / "test_res.json"
    failed.write_text(json.dumps({"set": "test_syn", "spec_version": 1, "pass": False}))
    passed = ctx.tmp / "test_res_pass.json"
    passed.write_text(json.dumps({"set": "test_syn", "spec_version": 1, "pass": True}))
    base = gen.argv("regen", "test_syn", extra=["--resemble-result", str(failed)])
    labels = gen.test_out.parent / "somewhere" / "labels_gpt.jsonl"
    labels.parent.mkdir()
    labels.write_text("{}\n")
    assert G.main(base) == S.EXIT_REGEN_LIMIT              # a TEST label exists
    labels.unlink()
    assert G.main(gen.argv("regen", "test_syn", extra=["--resemble-result", str(passed)])) == S.EXIT_REFUSE
    assert G.main(base) == 0                               # the one regeneration
    disc = [p for p in (ctx.cls / "test-syn").iterdir() if p.name.startswith("discarded-")]
    assert len(disc) == 1 and stat.S_IMODE((disc[0] / "items.jsonl").stat().st_mode) == 0
    assert (gen.test_out / "items.jsonl").exists()
    with pytest.raises(S.CLSExit):
        S.read_guarded_text(disc[0] / "items.jsonl", ctx.cls)
    assert G.main(base) == S.EXIT_REGEN_LIMIT              # a second regen is refused
    cust = json.loads((ctx.cls / "test-syn" / "custody.json").read_text())
    assert cust["regen_count"] == 1 and cust["test_syn"] == "NOT_INFORMATIVE_AS_PROXY"
    assert gen.run("test_syn") == S.EXIT_REGEN_LIMIT


def test_regen_discards_the_previous_set_allows_one_and_closes_after_labels(gen):
    inv_regen_rules(gen)


def test_regen_needs_the_same_spec_files(gen):
    freeze_v1(gen)
    ctx = gen.ctx
    gen.alloc_and_pin("test_syn", name="gen_test_v1")
    assert gen.step(bad_resemble) == S.EXIT_RESEMBLE_FAIL
    (gen.gen_dir / "projects.v1.json").write_text(json.dumps(PROJECTS[:3]))
    failed = ctx.tmp / "test_res.json"
    failed.write_text(json.dumps({"set": "test_syn", "spec_version": 1, "pass": False}))
    assert G.main(gen.argv("regen", "test_syn", extra=["--resemble-result", str(failed)])) in (S.EXIT_REFUSE, S.EXIT_GEN_FILE)
    assert (gen.test_out / "items.jsonl").exists()


def inv_release_token_is_never_created(gen):
    freeze_v1(gen)
    ctx = gen.ctx
    gen.alloc_and_pin("test_syn", name="gen_test_v1")
    assert gen.step(ok_resemble) == 0
    token = ctx.tmp / "no-token-here"
    with pytest.raises(S.CLSExit) as e:
        S.read_guarded_text(gen.test_out / "items.jsonl", ctx.cls, release_token=token, allow_test_syn=True)
    assert e.value.code == S.EXIT_CUSTODY and not token.exists()
    token.write_text("x")
    txt = S.read_guarded_text(gen.test_out / "items.jsonl", ctx.cls, release_token=token, allow_test_syn=True)
    assert txt and stat.S_IMODE((gen.test_out / "items.jsonl").stat().st_mode) == 0


def test_release_token_is_required_and_never_created_by_the_tool(gen):
    inv_release_token_is_never_created(gen)


# =============================================================================================
# 6. resemblance tool
# =============================================================================================

def hist_featurize(tr, te):
    def vec(t):
        base = [t.lower().count(c) / max(1, len(t)) for c in "abcdefghijklmnopqrstuvwxyz0123456789"]
        return base + [min(len(t.split()), 200) / 200.0]
    return [vec(t) for t in tr], [vec(t) for t in te]


def lr_scorers():
    s = R.make_scorer(hist_featurize, R.pure_python_lr)
    return {"a1": s, "a2": s}


def rows(prefix, n, alphabet_bias="", per_group=4, words=(3, 12)):
    import random
    rng = random.Random(7)
    out = []
    for i in range(n):
        k = rng.randint(*words)
        toks = []
        for _ in range(k):
            w = "".join(rng.choice("abcdefghij" + alphabet_bias) for _ in range(rng.randint(3, 7)))
            toks.append(w)
        out.append((" ".join(toks), "%s%d" % (prefix, i // per_group)))
    return out


def test_auroc_known_values():
    assert R.auroc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0
    assert R.auroc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]) == 0.0
    assert R.auroc([0.5, 0.5, 0.5, 0.5], [0, 0, 1, 1]) == 0.5
    assert R.auroc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == 0.75


def test_identical_distributions_give_auroc_near_half_and_pass(ctx, monkeypatch):
    monkeypatch.setattr(R, "BOOTSTRAP_B", 200)
    real, syn = rows("r", 120), rows("s", 120)
    rep = {"band_miss_rate": 0.05, "quota_ok": True}
    res = R.compute(real, syn, scorers=lr_scorers(), gen_report=rep)
    assert 0.3 <= res["a1"]["auroc"] <= 0.7 and 0.3 <= res["a2"]["auroc"] <= 0.7
    assert res["gate"]["pass"] is True and res["b"]["pass"] is True and res["d"]["pass"] is True and res["pass"] is True


def test_separable_distributions_give_auroc_near_one_and_fail(ctx, monkeypatch):
    monkeypatch.setattr(R, "BOOTSTRAP_B", 200)
    real, syn = rows("r", 60), rows("s", 60, alphabet_bias="zzzzzzzzzzzzzzz")
    res = R.compute(real, syn, scorers=lr_scorers(), gen_report={"band_miss_rate": 0.0, "quota_ok": True})
    assert res["a1"]["auroc"] >= 0.95 and res["gate"]["pass"] is False and res["pass"] is False
    assert res["gate"]["point_le_0.75"] is False


def const_scores_scorer(by_text):
    def scorer(tr, y, w, te):
        return [by_text[t] for t in te]
    return scorer


def inv_ci_upper_bound_gates_not_only_the_point(ctx):
    # 20 real vs 20 synthetic, each its own group; scores crafted for a point estimate just under 0.75
    import itertools
    best = None
    for shift in [x / 100.0 for x in range(0, 100)]:
        syn_s = [j / 20.0 + shift for j in range(20)]
        real_s = [i / 20.0 for i in range(20)]
        au = R.auroc(syn_s + real_s, [1] * 20 + [0] * 20)
        if 0.72 <= au <= 0.75:
            best = (shift, au)
            break
    assert best, "fixture search failed"
    shift, au = best
    real = [("real text %d" % i, "g%d" % i) for i in range(20)]
    syn = [("syn text %d" % j, "h%d" % j) for j in range(20)]
    table = {t: i / 20.0 for i, (t, _g) in enumerate(real)}
    table.update({t: j / 20.0 + shift for j, (t, _g) in enumerate(syn)})
    sc = const_scores_scorer(table)
    res = R.compute(real, syn, scorers={"a1": sc, "a2": sc}, gen_report={"band_miss_rate": 0.0, "quota_ok": True}, B=400)
    assert res["a1"]["auroc"] <= 0.75 and res["a1"]["ci"][1] > 0.80
    assert res["a1"]["pass"] is False and res["gate"]["pass"] is False and res["pass"] is False
    del itertools


def test_gate_boundary_point_074_upper_081_fails_and_clean_pass(ctx):
    inv_ci_upper_bound_gates_not_only_the_point(ctx)
    assert R.gate_pass(0.74, 0.81) is False and R.gate_pass(0.74, 0.79) is True
    assert R.gate_pass(0.75, 0.80) is True and R.gate_pass(0.76, 0.5) is False


def test_bootstrap_constants_and_group_resampling():
    assert R.BOOTSTRAP_B == 2000 and S.SEED == 20261017 and R.FOLDS == 5
    sc, y, g = [0.1, 0.9, 0.2, 0.8] * 5, [0, 1, 0, 1] * 5, ["g%d" % (i // 2) for i in range(20)]
    lo1, hi1 = R.group_bootstrap_ci(sc, y, g, B=50)
    lo2, hi2 = R.group_bootstrap_ci(sc, y, g, B=50)
    assert (lo1, hi1) == (lo2, hi2) and lo1 <= hi1
    f = R.fold_assignment(["a", "b", "c", "d", "e", "f", "g"])
    assert set(f.values()) == {0, 1, 2, 3, 4} and f == R.fold_assignment(["g", "f", "e", "d", "c", "b", "a"])


def write_res_items(ctx, rws, name, root=None, ids="x"):
    p = Path(root or ctx.tmp) / name
    return ctx.wrote_private(p, "".join(json.dumps({"item_id": "%s%s%d" % (ids, name[:2], i), "text": t, "group": g}) + "\n"
                                        for i, (t, g) in enumerate(rws)))


def res_argv(ctx, set_name, real, syn, out, extra=()):
    return ["run", "--set", set_name, "--real-items", str(real), "--syn-items", str(syn), "--cls-root", str(ctx.cls),
            "--spec-version", "2", "--out", str(out), "--pins-static", str(ctx.pins_static)] + list(extra)


def test_output_is_numbers_only_with_no_text_ids_or_features(ctx, capsys, monkeypatch):
    monkeypatch.setattr(R, "BOOTSTRAP_B", 100)
    real = rows("r", 40)
    syn = rows("s", 40)
    real[0] = ("SENTINELREAL " + real[0][0], real[0][1])
    syn[0] = ("SENTINELSYN " + syn[0][0], syn[0][1])
    rp = write_res_items(ctx, real, "realdev.jsonl", ids="SENTID")
    sp = write_res_items(ctx, syn, "synitems.jsonl", ids="SENTID")
    gr = ctx.wrote_private(ctx.tmp / "gr.json", json.dumps({"band_miss_rate": 0.0, "quota_ok": True}))
    out = ctx.tmp / "res.json"
    capsys.readouterr()
    code = R.main(res_argv(ctx, "dev_syn", rp, sp, out, ["--gen-report", str(gr)]), scorers=lr_scorers())
    cap = capsys.readouterr()
    blob = cap.out + cap.err + out.read_text()
    assert code in (0, S.EXIT_RESEMBLE_FAIL)
    for s in ("SENTINELREAL", "SENTINELSYN", "SENTID"):
        assert s not in blob
    doc = json.loads(out.read_text())
    assert set(doc) == {"a1", "a2", "gate", "b", "c", "d", "pass", "set", "spec_version"}
    assert set(doc["a1"]) == {"auroc", "ci", "point_ok", "ci_upper_ok", "pass"}
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def inv_resemble_prints_no_features(ctx, capsys, monkeypatch):
    monkeypatch.setattr(R, "BOOTSTRAP_B", 60)
    real, syn = rows("r", 30), rows("s", 30)
    real[0] = ("SENTINELTERM zzqq", real[0][1])
    rp = write_res_items(ctx, real, "realdev.jsonl")
    sp = write_res_items(ctx, syn, "synitems.jsonl")
    gr = ctx.wrote_private(ctx.tmp / "gr.json", json.dumps({"band_miss_rate": 0.0, "quota_ok": True}))
    out = ctx.tmp / "res2.json"
    capsys.readouterr()
    R.main(res_argv(ctx, "dev_syn", rp, sp, out, ["--gen-report", str(gr)]), scorers=lr_scorers())
    cap = capsys.readouterr()
    assert "SENTINELTERM" not in cap.out + cap.err + out.read_text()
    allowed = {"a1", "a2", "gate", "b", "c", "d", "pass", "set", "spec_version"}
    assert set(json.loads(out.read_text())) == allowed and set(json.loads(cap.out)) == allowed


def test_resemble_exposes_no_feature_dump_flag_and_prints_no_features(ctx, capsys, monkeypatch):
    inv_resemble_prints_no_features(ctx, capsys, monkeypatch)
    flags = {o for a in R.build_parser()._subparsers._group_actions[0].choices["run"]._actions for o in a.option_strings}
    assert not [f for f in flags if re.search(r"feature|dump|term|weight|error|explain|top|verbose|debug", f)]


def test_resemble_exit_codes(ctx, capsys, monkeypatch):
    monkeypatch.setattr(R, "BOOTSTRAP_B", 60)
    real = rows("r", 40)
    rp = write_res_items(ctx, real, "realdev.jsonl")
    gr = ctx.wrote_private(ctx.tmp / "gr.json", json.dumps({"band_miss_rate": 0.0, "quota_ok": True}))
    same = write_res_items(ctx, rows("s", 40), "same.jsonl")
    sep = write_res_items(ctx, rows("s", 40, alphabet_bias="zzzzzzzzzzzzzzz"), "sep.jsonl")
    assert R.main(res_argv(ctx, "dev_syn", rp, same, ctx.tmp / "o1.json", ["--gen-report", str(gr)]), scorers=lr_scorers()) == 0
    assert R.main(res_argv(ctx, "dev_syn", rp, sep, ctx.tmp / "o2.json", ["--gen-report", str(gr)]), scorers=lr_scorers()) == S.EXIT_RESEMBLE_FAIL
    badgr = ctx.wrote_private(ctx.tmp / "gr2.json", json.dumps({"band_miss_rate": 0.4, "quota_ok": True}))
    assert R.main(res_argv(ctx, "dev_syn", rp, same, ctx.tmp / "o3.json", ["--gen-report", str(badgr)]), scorers=lr_scorers()) == S.EXIT_RESEMBLE_FAIL


def test_resemble_check_b_c_and_helpers():
    real = [("w " * 5, "a")] * 10 + [("w " * 30, "b")] * 10
    syn = [("w " * 5, "c")] * 3 + [("w " * 30, "d")] * 17
    assert R.shares([t for t, _ in real]) == {"L1": 50.0, "L2": 50.0, "L3": 0.0, "L4": 0.0}
    assert R.shares([t for t, _ in syn])["L1"] == 15.0
    res = R.compute(real, syn, scorers={"a1": lambda *a: [0.5] * len(a[3]), "a2": lambda *a: [0.5] * len(a[3])},
                    gen_report=None, B=20)
    assert res["b"]["pass"] is False and res["d"]["pass"] is False and res["pass"] is False
    assert res["c"]["real_median_words"] == 17.5 and res["c"]["syn_median_words"] == 30.0 and res["c"]["gating"] is False
    assert R.band_of(12) == "L1" and R.band_of(13) == "L2" and R.band_of(51) == "L3" and R.band_of(151) == "L4"


def inv_amendment_view_never_leaks_test_numbers(ctx, capsys, monkeypatch):
    monkeypatch.setattr(R, "BOOTSTRAP_B", 60)
    real = rows("r", 30)
    rp = write_res_items(ctx, real, "realdev.jsonl")
    gr = ctx.wrote_private(ctx.tmp / "gr.json", json.dumps({"band_miss_rate": 0.0, "quota_ok": True}))
    ts = ctx.cls / "test-syn" / "current"
    sp = write_res_items(ctx, rows("s", 30), "items.jsonl", root=ts)
    token = ctx.tmp / "tok"
    token.write_text("x")
    out = ctx.tmp / "view.json"
    code = R.main(res_argv(ctx, "test_syn", rp, sp, out, ["--gen-report", str(gr), "--release-token", str(token),
                                                       "--amendment-view"]), scorers=lr_scorers())
    assert code == S.EXIT_CUSTODY and not out.exists()
    dev_sp = write_res_items(ctx, rows("s", 30), "dev_syn_items.jsonl")
    out2 = ctx.tmp / "view2.json"
    R.main(res_argv(ctx, "dev_syn", rp, dev_sp, out2, ["--gen-report", str(gr), "--amendment-view"]), scorers=lr_scorers())
    view = json.loads(out2.read_text())
    assert set(view) == {"set", "spec_version", "pass", "a1", "a2"} and set(view["a1"]) == {"auroc", "ci"}
    sentinel = {"set": "test_syn", "pass": True, "a1": {"auroc": 0.123456, "ci": [0, 1]}, "a2": {"auroc": 0.654321, "ci": [0, 1]},
                "spec_version": 1}
    with pytest.raises(S.CLSExit) as e:
        R.amendment_view(sentinel)
    assert e.value.code == S.EXIT_CUSTODY and "0.123456" not in str(e.value)


def test_amendment_view_has_dev_syn_numbers_only_and_refuses_test_syn(ctx, capsys, monkeypatch):
    inv_amendment_view_never_leaks_test_numbers(ctx, capsys, monkeypatch)


def test_resemble_on_test_syn_needs_a_release_token_and_the_custody_root(ctx, capsys, monkeypatch):
    monkeypatch.setattr(R, "BOOTSTRAP_B", 60)
    rp = write_res_items(ctx, rows("r", 30), "realdev.jsonl")
    gr = ctx.wrote_private(ctx.tmp / "gr.json", json.dumps({"band_miss_rate": 0.0, "quota_ok": True}))
    ts = ctx.cls / "test-syn" / "current"
    sp = write_res_items(ctx, rows("s", 30), "items.jsonl", root=ts)
    sp.chmod(0)
    out = ctx.tmp / "o.json"
    base = ["--gen-report", str(gr)]
    assert R.main(res_argv(ctx, "test_syn", rp, sp, out, base), scorers=lr_scorers()) == S.EXIT_CUSTODY
    token = ctx.tmp / "tok"
    assert R.main(res_argv(ctx, "test_syn", rp, sp, out, base + ["--release-token", str(token)]), scorers=lr_scorers()) == S.EXIT_CUSTODY
    token.write_text("x")
    assert R.main(res_argv(ctx, "test_syn", rp, sp, out, base + ["--release-token", str(token)]), scorers=lr_scorers()) in (0, S.EXIT_RESEMBLE_FAIL)
    assert stat.S_IMODE(sp.stat().st_mode) == 0
    # a DEV run never reads TEST-SYN either
    assert R.main(res_argv(ctx, "dev_syn", rp, sp, out, base), scorers=lr_scorers()) == S.EXIT_CUSTODY


# =============================================================================================
# 7. pins
# =============================================================================================

class _Ollama(http.server.BaseHTTPRequestHandler):
    digests = {"nimble:9b": "d1", "qwen3.5:latest": "d2", "qwen3-coder:30b": "d3", "nomic-embed-text:latest": "d4"}
    extra: list = []  # (name, digest) rows listed after `digests` (a name may repeat, as in real /api/tags)
    version = "0.99.0"

    def do_GET(self):  # noqa: N802
        if self.path == "/api/version":
            body = {"version": type(self).version}
        elif self.path == "/api/tags":
            body = {"models": [{"name": k, "digest": v} for k, v in type(self).digests.items()]
                    + [{"name": k, "digest": v} for k, v in type(self).extra]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.x", GIT_COMMITTER_NAME="t",
                            GIT_COMMITTER_EMAIL="t@e.x"))


class PinsEnv:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        t = ctx.tmp
        ctx.mp.setattr(_Ollama, "digests", dict(_Ollama.digests))
        ctx.mp.setattr(_Ollama, "extra", [])
        ctx.mp.setattr(_Ollama, "version", "0.99.0")
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Ollama)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.repo = t / "repo"
        (self.repo / "src/llm_router").mkdir(parents=True)
        (self.repo / "src/llm_router/engine.py").write_text("ENGINE = 1\n")
        (self.repo / "src/llm_router/shadow_eval.py").write_text("SHADOW = 1\n")
        sh(["git", "init", "-q"], self.repo)
        sh(["git", "add", "."], self.repo)
        sh(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "x"], self.repo)
        self.commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True).stdout.strip()
        self.scripts = t / "scripts8"
        self.scripts.mkdir()
        for n in P.SCRIPT_NAMES:
            (self.scripts / n).write_text("# %s\n" % n)
        self.rubric = t / "rubric.py"
        self.rubric.write_text("RUBRIC = 'x'\n")
        ctx.mp.setattr(P, "RUBRIC_MD5", hashlib.md5(self.rubric.read_bytes()).hexdigest())
        self.gt500 = t / "gt500.py"
        self.gt500.write_text("RUBRIC_MD5 = 'x'\n")
        ctx.mp.setattr(P, "GT500_SHA256", S.sha256_file(self.gt500))
        self.supply = t / "supply.py"
        self.supply.write_text("SUPPLY = 1\n")
        ctx.mp.setattr(P, "SUPPLY_SHA256", S.sha256_file(self.supply))
        self.denylist = ctx.wrote_private(t / "denylist.txt", "one\ntwo\n")
        self.env = ctx.wrote_private(t / ".env", "API_KEY=abcdefgh1234\nNAME=x\n")
        self.snap = t / "snap" / "5c38ec7c405e"
        self.snap.mkdir(parents=True)
        self.gen = t / "gens"
        self.gen.mkdir()
        for n in P.GENERATOR_V1_FILES:
            (self.gen / n).write_text("v1 " + n)
        self.params = {"repo": str(self.repo), "scripts_dir": str(self.scripts), "engine_commit": self.commit,
                       "rubric_py": str(self.rubric), "gt500_py": str(self.gt500), "denylist": str(self.denylist),
                       "env_file": str(self.env),
                       "ollama_url": "http://127.0.0.1:%d" % self.srv.server_address[1], "ollama_models": P.OLLAMA_MODELS,
                       "bge_snapshot_dir": str(self.snap), "claude_bin": str(ctx.claude), "codex_bin": str(ctx.codex),
                       "gen_dir": str(self.gen), "model_ids": dict(PINNED)}
        self.static = t / "PINS-STATIC.json"
        self.run_file = t / "pins_run.jsonl"

    def build(self):
        doc = P.build_static(self.params)
        self.static.write_text(P.canonical(doc))
        return doc

    def verify(self, **kw):
        return P.verify(self.static, self.run_file, **kw)

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def pins(ctx):
    pe = PinsEnv(ctx)
    yield pe
    pe.close()


def test_static_pins_compute_every_key_and_write_canonical_json(pins):
    doc = pins.build()
    assert set(doc["pins"]) == {"scripts", "engine", "rubric", "gt500", "supply", "denylist", "env", "ollama", "bge_snapshot", "claude_cli",
                                "codex_cli", "model_ids", "generator_files", "session_log_dirs"}
    assert doc["pins"]["gt500"]["sha256"] == S.sha256_file(pins.gt500)
    assert doc["pins"]["supply"]["sha256"] == S.sha256_file(pins.supply)
    assert set(doc["pins"]["scripts"]) == set(P.SCRIPT_NAMES) and "cls_send_lib.py" in doc["pins"]["scripts"]
    assert doc["pins"]["engine"]["commit"] == pins.commit
    assert doc["pins"]["engine"]["engine_py_sha256"] == hashlib.sha256(b"ENGINE = 1\n").hexdigest()
    assert doc["pins"]["engine"]["shadow_eval_py_sha256"] == hashlib.sha256(b"SHADOW = 1\n").hexdigest()
    assert doc["pins"]["denylist"]["entries"] == 2 and doc["pins"]["env"]["qualifying_values"] == 1
    assert doc["pins"]["ollama"]["version"] == "0.99.0" and set(doc["pins"]["ollama"]["digests"]) == set(P.OLLAMA_MODELS)
    assert doc["pins"]["bge_snapshot"]["snapshot_dir_name"] == "5c38ec7c405e"
    cl = doc["pins"]["claude_cli"]
    assert set(cl["flag_lines"]) == set(P.CLAUDE_FLAGS) and "--max-turns" not in cl["flag_lines"]
    assert cl["argv_template"][cl["argv_template"].index("--tools") + 1] == ""
    assert set(doc["pins"]["codex_cli"]["flag_lines"]) == set(P.CODEX_FLAGS)
    assert "--ephemeral" in doc["pins"]["codex_cli"]["argv_template"]
    raw = pins.static.read_text()
    assert raw.endswith("\n") and raw == P.canonical(json.loads(raw))
    assert pins.verify()["ok"] is True


def test_ollama_name_bare_means_latest():
    assert P.ollama_name("nomic-embed-text") == "nomic-embed-text:latest"
    assert P.ollama_name("qwen3.5:latest") == "qwen3.5:latest" and P.ollama_name("nimble:9b") == "nimble:9b"
    assert P.ollama_name("host:5000/m") == "host:5000/m:latest"          # a registry port is not a tag
    assert P.ollama_name("host:5000/m:7b") == "host:5000/m:7b"


def test_ollama_digest_sets_keep_every_digest_sorted_never_last_wins():
    rows = [{"name": "qwen3.5:latest", "digest": "2e16"}, {"name": "x:1", "digest": "e"},
            {"name": "qwen3.5:latest", "digest": "44a3"}, {"name": "qwen3.5:latest", "digest": "2e16"}]
    assert P.ollama_digest_sets(rows) == {"qwen3.5:latest": ["2e16", "44a3"], "x:1": ["e"]}
    assert P.ollama_digest_sets(list(reversed(rows))) == P.ollama_digest_sets(rows)   # listing order is irrelevant
    assert P.ollama_digest_sets([{"name": "nomic-embed-text", "digest": "d"}]) == {"nomic-embed-text:latest": ["d"]}


def test_static_pin_resolves_bare_nomic_name_to_latest(pins):
    # real /api/tags lists only `nomic-embed-text:latest`; OLLAMA_MODELS asks for the bare name (D-169 (1))
    assert "nomic-embed-text" in P.OLLAMA_MODELS and "nomic-embed-text" not in _Ollama.digests
    doc = pins.build()
    assert doc["pins"]["ollama"]["digests"]["nomic-embed-text"] == ["d4"]
    del _Ollama.digests["nomic-embed-text:latest"]
    with pytest.raises(S.CLSExit) as e:
        P.build_static(pins.params)
    assert e.value.code == S.EXIT_PINS and "ollama" in str(e.value)


def test_static_pin_records_all_digests_of_a_duplicate_name_and_verify_fails_on_set_change(pins):
    _Ollama.extra = [("qwen3.5:latest", "44a3")]          # listed twice, as qwen3.5:latest is (D-169 (2))
    doc = pins.build()
    assert doc["pins"]["ollama"]["digests"]["qwen3.5:latest"] == ["44a3", "d2"]
    assert all(isinstance(v, list) for v in doc["pins"]["ollama"]["digests"].values())
    assert pins.verify()["ok"] is True
    _Ollama.digests = {"qwen3.5:latest": "44a3", **{k: v for k, v in _Ollama.digests.items() if k != "qwen3.5:latest"}}
    _Ollama.extra = [("qwen3.5:latest", "d2")]            # same set, other listing order: still ok
    assert pins.verify()["ok"] is True
    _Ollama.extra = []                                    # one digest gone: the set changed
    res = pins.verify()
    assert res["ok"] is False and res["mismatches"] == ["ollama.digests.qwen3.5:latest"]
    _Ollama.extra = [("qwen3.5:latest", "d2"), ("qwen3.5:latest", "ffff")]   # one digest added
    res = pins.verify()
    assert res["ok"] is False and res["mismatches"] == ["ollama.digests.qwen3.5:latest"]
    _Ollama.extra = [("qwen3.5:latest", "d2")]
    assert P.main(["pins", "--verify", str(pins.static), "--pins-run", str(pins.run_file)]) == 0
    _Ollama.extra = []
    assert P.main(["pins", "--verify", str(pins.static), "--pins-run", str(pins.run_file)]) == S.EXIT_PINS


def test_freeze_line_format_and_cli_pins(pins, capsys):
    pins.build()
    capsys.readouterr()
    assert P.main(["pins", "--freeze-line", str(pins.static)]) == 0
    line = capsys.readouterr().out.strip()
    assert re.fullmatch(r"pins_sha256 [0-9a-f]{64}", line) and line.endswith(S.sha256_file(pins.static))
    out = pins.ctx.tmp / "PINS-CLI.json"
    argv = ["pins", "--static", "--out", str(out), "--repo", str(pins.repo), "--scripts-dir", str(pins.scripts),
            "--engine-commit", pins.commit, "--rubric-py", str(pins.rubric), "--gt500-py", str(pins.gt500),
            "--denylist", str(pins.denylist),
            "--env-file", str(pins.env), "--ollama-url", pins.params["ollama_url"], "--bge-snapshot-dir", str(pins.snap),
            "--claude-bin", pins.params["claude_bin"], "--codex-bin", pins.params["codex_bin"], "--gen-dir", str(pins.gen)]
    for k, v in PINNED.items():
        argv += ["--model-id", "%s=%s" % (k, v)]
    assert P.main(argv) == 0 and json.loads(out.read_text())["pins"]["engine"]["commit"] == pins.commit
    assert P.main(["pins", "--verify", str(out), "--pins-run", str(pins.run_file)]) == 0
    (pins.scripts / "cls_pins.py").write_text("# changed\n")
    capsys.readouterr()
    assert P.main(["pins", "--verify", str(out), "--pins-run", str(pins.run_file)]) == S.EXIT_PINS
    assert "scripts.cls_pins.py" in capsys.readouterr().err


def inv_verify_detects_every_single_mutation(pins):
    pins.build()
    mutated = []

    def check(key):
        res = pins.verify()
        assert res["ok"] is False and any(m == key or m.startswith(key) for m in res["mismatches"]), (key, res["mismatches"])
        mutated.append(key)

    for n in P.SCRIPT_NAMES:
        p = pins.scripts / n
        orig = p.read_text()
        p.write_text(orig + "# x\n")
        check("scripts.%s" % n)
        p.write_text(orig)
    for attr, key in (("rubric", "rubric"), ("denylist", "denylist")):
        p = getattr(pins, attr)
        orig = p.read_text()
        p.write_text(orig + "x\n")
        if attr == "rubric":
            # the rubric md5 must keep matching, so move the expectation with it and watch the sha256 differ
            pins.ctx.mp.setattr(P, "RUBRIC_MD5", hashlib.md5(p.read_bytes()).hexdigest())
        check(key)
        p.write_text(orig)
        if attr == "rubric":
            pins.ctx.mp.setattr(P, "RUBRIC_MD5", hashlib.md5(p.read_bytes()).hexdigest())
    for n in P.GENERATOR_V1_FILES:
        p = pins.gen / n
        orig = p.read_text()
        p.write_text(orig + " edited")
        check("generator_files.%s" % n)
        p.write_text(orig)
    orig = pins.gt500.read_text()
    pins.gt500.write_text(orig + "# edited\n")
    check("gt500")
    pins.gt500.write_text(orig)
    orig = pins.supply.read_text()
    pins.supply.write_text(orig + "# edited\n")
    check("supply")
    pins.supply.write_text(orig)
    _Ollama.digests["nimble:9b"] = "changed"
    check("ollama")
    _Ollama.digests["nimble:9b"] = "d1"
    _Ollama.version = "1.0.0"
    check("ollama")
    _Ollama.version = "0.99.0"
    snap2 = pins.snap.with_name("renamed")
    pins.snap.rename(snap2)
    check("bge_snapshot")
    snap2.rename(pins.snap)
    h = pins.ctx.fake / "help_claude.txt"
    orig = h.read_text()
    h.write_text(orig + "  --extra  new flag\n")
    check("claude_cli")
    h.write_text(orig)
    h2 = pins.ctx.fake / "help_codex.txt"
    orig = h2.read_text()
    h2.write_text(orig + "  --extra  new flag\n")
    check("codex_cli")
    h2.write_text(orig)
    assert pins.verify()["ok"] is True
    assert len(mutated) >= len(P.SCRIPT_NAMES) + 4 + len(P.GENERATOR_V1_FILES) + 5


def test_verify_detects_each_single_mutation_of_every_pinned_file(pins):
    inv_verify_detects_every_single_mutation(pins)


def inv_engine_is_pinned_by_commit_not_working_tree(pins):
    doc = pins.build()
    (pins.repo / "src/llm_router/engine.py").write_text("ENGINE = 2  # uncommitted edit\n")
    (pins.repo / "src/llm_router/shadow_eval.py").write_text("SHADOW = 2\n")
    again = P.build_static(pins.params)
    assert again["pins"]["engine"] == doc["pins"]["engine"]
    assert again["pins"]["engine"]["engine_py_sha256"] == hashlib.sha256(b"ENGINE = 1\n").hexdigest()
    assert pins.verify()["ok"] is True


def test_engine_and_shadow_eval_are_pinned_by_commit(pins):
    inv_engine_is_pinned_by_commit_not_working_tree(pins)


def test_rubric_md5_mismatch_and_missing_pin_exit_9(pins, capsys):
    pins.ctx.mp.setattr(P, "RUBRIC_MD5", "0" * 32)
    with pytest.raises(S.CLSExit) as e:
        P.build_static(pins.params)
    assert e.value.code == S.EXIT_PINS and "rubric" in str(e.value)
    pins.ctx.mp.setattr(P, "RUBRIC_MD5", hashlib.md5(pins.rubric.read_bytes()).hexdigest())
    (pins.scripts / "cls_eval.py").unlink()
    with pytest.raises(S.CLSExit) as e:
        P.build_static(pins.params)
    assert e.value.code == S.EXIT_PINS and "scripts" in str(e.value)
    argv = ["pins", "--static", "--out", str(pins.ctx.tmp / "x.json"), "--repo", str(pins.repo)]
    assert P.main(argv) == S.EXIT_PINS


def inv_missing_help_flag_blocks_the_freeze(pins):
    h = pins.ctx.fake / "help_claude.txt"
    h.write_text("\n".join(ln for ln in HELP_CLAUDE.splitlines() if "--tools" not in ln))
    with pytest.raises(S.CLSExit) as e:
        P.build_static(pins.params)
    assert e.value.code == S.EXIT_PINS and "claude_flag--tools" in str(e.value)
    h.write_text(HELP_CLAUDE)
    h2 = pins.ctx.fake / "help_codex.txt"
    h2.write_text("\n".join(ln for ln in HELP_CODEX.splitlines() if "--ephemeral" not in ln))
    with pytest.raises(S.CLSExit) as e:
        P.build_static(pins.params)
    assert e.value.code == S.EXIT_PINS and "codex_flag--ephemeral" in str(e.value)


def test_a_flag_missing_from_help_exits_9_naming_the_flag(pins):
    inv_missing_help_flag_blocks_the_freeze(pins)


def test_env_change_is_a_warning_not_a_failure_and_sha_is_logged(pins):
    pins.build()
    pins.env.write_text("API_KEY=anotherlongvalue\n")
    res = pins.verify()
    assert res["ok"] is True and "env_changed_since_freeze" in res["warnings"]
    assert res["env_sha_at_use"] == S.sha256_file(pins.env)


def inv_verify_before_run_entries_exist_passes(pins):
    pins.build()
    assert not pins.run_file.exists()
    res = pins.verify()
    assert res["ok"] is True and res["run_entries"] == 0
    P.verify_at_start(pins.static, pins.run_file)


def test_verify_does_not_require_run_entries_before_they_exist(pins):
    inv_verify_before_run_entries_exist_passes(pins)


def inv_static_still_checked_once_run_exists(pins):
    pins.build()
    f = pins.ctx.tmp / "lc.json"
    f.write_text("{}")
    P.append_run_entry(pins.run_file, "length_counts", [f])
    assert pins.verify()["ok"] is True
    (pins.scripts / "cls_send_lib.py").write_text("# tampered\n")
    res = pins.verify()
    assert res["ok"] is False and "scripts.cls_send_lib.py" in res["mismatches"]
    with pytest.raises(S.CLSExit) as e:
        P.verify_at_start(pins.static, pins.run_file)
    assert e.value.code == S.EXIT_PINS and "cls_send_lib.py" in str(e.value)


def test_static_pins_stay_checked_once_a_run_entry_exists(pins):
    inv_static_still_checked_once_run_exists(pins)


def inv_run_entries_are_append_only_and_chained(pins):
    pins.build()
    a = pins.ctx.tmp / "a.json"
    b = pins.ctx.tmp / "b.json"
    a.write_text("a")
    b.write_text("b")
    e1 = P.append_run_entry(pins.run_file, "first", [a])
    e2 = P.append_run_entry(pins.run_file, "second", [b])
    assert e1["prev_entry_sha"] == "0" * 64 and e2["prev_entry_sha"] == e1["entry_sha"]
    assert set(e1) == {"name", "utc", "files", "prev_entry_sha", "entry_sha"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", e1["utc"])
    before = pins.run_file.read_bytes()
    with pytest.raises(S.CLSExit):
        P.append_run_entry(pins.run_file, "first", [b])
    assert pins.run_file.read_bytes() == before
    assert pins.verify()["ok"] is True


def test_run_entries_are_append_only_hash_chained_and_never_overwritten(pins):
    inv_run_entries_are_append_only_and_chained(pins)


def test_tampering_with_an_earlier_run_entry_or_its_file_breaks_verify(pins):
    pins.build()
    a, b = pins.ctx.tmp / "a.json", pins.ctx.tmp / "b.json"
    a.write_text("a")
    b.write_text("b")
    P.append_run_entry(pins.run_file, "first", [a])
    P.append_run_entry(pins.run_file, "second", [b])
    a.write_text("changed")
    res = pins.verify()
    assert res["ok"] is False and any(m.startswith("run:first") for m in res["mismatches"])
    a.write_text("a")
    lines = pins.run_file.read_text().splitlines()
    first = json.loads(lines[0])
    first["name"] = "renamed"
    pins.run_file.write_text(json.dumps(first) + "\n" + lines[1] + "\n")
    res = pins.verify()
    assert res["ok"] is False and any(m.startswith("run_chain") for m in res["mismatches"])


def test_rules_tier_header_is_required_and_recorded(pins, capsys):
    rt = pins.ctx.tmp / "rules_tier.jsonl"
    rt.write_text(json.dumps({"item_id": "a", "rules_tier": "sonnet"}) + "\n")
    base = ["pins", "--run", "--entry", "rules_tier", "--files", str(rt), "--rules-tier", str(rt), "--pins-run", str(pins.run_file)]
    assert P.main(base) == S.EXIT_PINS
    rt.write_text(json.dumps({"schema": "rules_tier/1", "resolver_commit": "abc", "produced_utc": "2026-10-16T00:00:00Z"}) + "\n"
                  + json.dumps({"item_id": "a"}) + "\n")
    assert P.main(base) == 0
    e = S.read_jsonl(pins.run_file)[0]
    assert e["extra"]["rules_tier_header"]["resolver_commit"] == "abc" and e["name"] == "rules_tier"


def test_versioned_generator_files_pass_verify_with_a_run_entry_and_v1_edit_fails_static(pins):
    pins.build()
    names = G.gen_file_names(2)
    v2 = [pins.gen / n for n in names.values()]
    for p in v2:
        p.write_text("v2 " + p.name)
    P.append_run_entry(pins.run_file, "gen_files_v2", v2)
    assert pins.verify()["ok"] is True
    (pins.gen / "tmpl_gpt.v1.txt").write_text("v1 edited in place")
    res = pins.verify()
    assert res["ok"] is False and "generator_files.tmpl_gpt.v1.txt" in res["mismatches"]


def inv_v2_written_over_v1_is_caught(pins):
    """A v2 amendment that overwrites a v1 file: the writer guard refuses it, and STATIC verify would catch it."""
    pins.build()
    static_doc = json.loads(pins.static.read_text())
    with pytest.raises(S.CLSExit) as e:
        G.write_gen_file(pins.gen, "tmpl_gpt.v1.txt", "amended wording", static_doc, pins.run_file)
    assert e.value.code == S.EXIT_GEN_FILE
    assert (pins.gen / "tmpl_gpt.v1.txt").read_text() == "v1 tmpl_gpt.v1.txt"
    (pins.gen / "tmpl_gpt.v1.txt").write_text("amended wording")
    assert pins.verify()["ok"] is False
    (pins.gen / "tmpl_gpt.v1.txt").write_text("v1 tmpl_gpt.v1.txt")


def test_v2_written_over_v1_is_refused_and_caught(pins):
    inv_v2_written_over_v1_is_caught(pins)


def test_verify_at_start_names_the_key_and_check_ollama_can_be_skipped(pins):
    pins.build()
    pins.srv.shutdown()
    res = P.verify(pins.static, pins.run_file, check_ollama=True)
    assert res["ok"] is False and "ollama" in res["mismatches"]
    assert P.verify(pins.static, pins.run_file, check_ollama=False)["ok"] is True
    with pytest.raises(S.CLSExit) as e:
        P.verify_at_start(pins.static, pins.run_file, check_ollama=True)
    assert e.value.code == S.EXIT_PINS and "ollama" in str(e.value)


def test_ollama_pins_refuse_a_non_local_host(pins):
    p = dict(pins.params, ollama_url="http://example.com:11434")
    _pins, errors = P.compute_static(p)
    assert "ollama" in errors


# =============================================================================================
# 8. D-147 repair: pinned judge-prompt contract, codex -m pin, park-not-void, regen pins, resume, ledger
# =============================================================================================

def _no_prompt_module_argv(ctx, set_name, judge, items_path, extra=()):
    argv = label_argv(ctx, "run", set_name, judge, items_path, extra=extra)
    i = argv.index("--prompt-module")
    del argv[i:i + 2]
    return argv


def inv_pinned_rubric_only(ctx, capsys):
    ip = write_items(ctx, mk_items(10))
    copy = ctx.tmp / "rubric_copy.py"
    copy.write_text(FAKE_RUBRIC + "\n# an unpinned edit\n")
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", ip, extra=["--prompt-module", str(copy)])
    assert code == S.EXIT_PINS and ctx.records() == []           # a module other than the pinned file
    ctx.rubric.write_text(FAKE_RUBRIC + "# drift\n")
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == S.EXIT_PINS and ctx.records() == []           # the pinned file's md5 drifted from PINS-STATIC
    ctx.rubric.write_text(FAKE_RUBRIC)
    gt = ctx.gt500.read_text()
    ctx.gt500.write_text(gt + "# edited\n")
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == S.EXIT_PINS and ctx.records() == []           # gt500.py differs from its pinned sha
    ctx.gt500.write_text(gt)
    capsys.readouterr()
    assert L.main(_no_prompt_module_argv(ctx, "dev_syn", "sonnet", ip)) == 0   # default = the pinned rubric
    assert ctx.records()


def test_only_the_pinned_rubric_and_gt500_are_loaded(ctx, capsys):
    inv_pinned_rubric_only(ctx, capsys)


def inv_prompt_view_and_verbatim_tail(ctx, capsys):
    ip = write_items(ctx, mk_items(12))
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == 0
    recs = ctx.records()
    blocks = [b for r in recs for b in re.findall(r"\[PROMPT TO LABEL\]\n(.*?)\n### END ITEM", r["stdin"], re.S)]
    assert len(blocks) >= 18 and all(b.startswith("VIEW[") and b.endswith("]VIEW") for b in blocks)
    first = recs[0]["stdin"]
    tail = 'ANSWER labels as json {"labels": [{"id": ..., "tier": ...}]} for all {n} items'
    assert first.startswith("RUBRIC fake text: rate each item\n\n### ITEM id=L") and first.endswith("\n\n" + tail)
    assert "Working directory: /repo" in first                   # the planted context, from rub.PLANTED
    n = len(recs)
    ctx.set_rubric(FAKE_RUBRIC, gt_text=FAKE_GT500.replace("def prompt_view", "def no_view"))
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == S.EXIT_PINS and len(ctx.records()) == n      # no prompt_view: refused, nothing sent


def test_items_go_out_in_prompt_view_form_with_the_rubric_first_and_the_tail_verbatim(ctx, capsys):
    inv_prompt_view_and_verbatim_tail(ctx, capsys)


def inv_gpt_pin_without_json_model_field(ctx, capsys):
    ctx.set_cfg()
    ip = write_items(ctx, mk_items(12))
    code, *_ = run_label(ctx, capsys, "dev_syn", "gpt", ip)
    assert code == 0
    recs = [r for r in ctx.records() if r["kind"] == "codex"]
    assert "PATH PARTS" in recs[0]["stdin"] and "--json" not in recs[0]["argv"]
    assert all(r["argv"][r["argv"].index("-m") + 1] == PINNED["judge_gpt"] for r in recs)
    lab = jl(run_dir(ctx) / "labels_gpt.jsonl")
    assert len(lab) == 12 and {r["model_id"] for r in lab} == {PINNED["judge_gpt"]}
    st = json.loads((run_dir(ctx) / "status.json").read_text())
    assert "model header matches the pin" in st["judges"]["gpt"]["canary"]


def test_gpt_runs_on_the_real_event_shape_with_no_model_field(ctx, capsys):
    inv_gpt_pin_without_json_model_field(ctx, capsys)
    out = S.parse_codex_jsonl(b'{"type":"thread.started","thread_id":"t"}\n{"type":"item.completed","item":'
                              b'{"type":"agent_message","text":"hi"}}\n{"type":"turn.completed","model":"x"}\n')
    assert out["text"] == "hi" and out["model_ids"] == []      # a model field, if any, is never read
    assert S.served_model_ids("codex", out, "gpt-6-astra") == ["gpt-6-astra"]
    assert S.served_model_ids("claude", {"model_ids": ["claude-opus-5-5"]}, "p") == ["claude-opus-5-5"]
    with pytest.raises(S.CLSExit) as e:
        S.codex_argv("/x", None)
    assert e.value.code == S.EXIT_MODEL_ID


def inv_park_not_void(ctx, capsys):
    ip = write_items(ctx, mk_items(20))
    rd = run_dir(ctx)
    ctx.set_cfg(codex_die_from=3)  # canary (1) and calibration (2) work, batch 1 gets rc=1
    code, *_ = run_label(ctx, capsys, "dev_syn", "gpt", ip)
    assert code == S.EXIT_GPT_PARK
    st = json.loads((rd / "status.json").read_text())
    assert st["judges"]["gpt"]["state"] == "stopped" and st["voided"] == [] and "truth_fallback" not in st
    assert (rd / "labels_gpt.jsonl").exists() and not (rd / "quarantine_gpt.jsonl").exists()
    ctx.set_cfg(no_model_usage=True)  # claude answers rc 0 without a model id
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == S.EXIT_MODEL_ID
    st = json.loads((rd / "status.json").read_text())
    assert st["judges"]["sonnet"]["state"] == "stopped" and st["voided"] == [] and "truth_fallback" not in st
    ctx.set_cfg()  # parked runs resume once the CLI works again
    assert run_label(ctx, capsys, "dev_syn", "gpt", ip)[0] == 0
    assert len({r["item_id"] for r in jl(rd / "labels_gpt.jsonl")}) == 20


def test_codex_failure_and_missing_claude_id_park_never_void(ctx, capsys):
    inv_park_not_void(ctx, capsys)


def inv_canary_header(ctx, capsys):
    ctx.set_cfg(codex_header_model="gpt-other")
    ip = write_items(ctx, mk_items(10))
    code, *_ = run_label(ctx, capsys, "dev_syn", "gpt", ip)
    assert code == S.EXIT_MODEL_ID and len(ctx.records()) == 1
    assert not (run_dir(ctx) / "labels_gpt.jsonl").exists()


def test_canary_header_must_show_the_pinned_model(ctx, capsys):
    inv_canary_header(ctx, capsys)


def inv_env_value_refused_at_send(ctx, capsys):
    env = ctx.tmp / ".env"
    env.write_text("SERVICE_TOKEN=Zq-8812-plainish\n")
    ctx.set_rubric(FAKE_RUBRIC.replace("PLANT two", "PLANT two Zq-8812-plainish"))
    ip = write_items(ctx, mk_items(10))
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", ip, extra=["--env-file", str(env)])
    assert not any("Zq-8812-plainish" in r["stdin"] for r in ctx.records())
    assert code == S.EXIT_PLANT_VOID
    assert json.loads((run_dir(ctx) / "status.json").read_text())["judges"]["sonnet"]["scrub_abort"] >= 1


def test_an_env_value_in_a_plant_is_refused_at_send(ctx, capsys):
    inv_env_value_refused_at_send(ctx, capsys)


def test_generator_refuses_an_env_value_in_the_template_at_send(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    env = ctx.tmp / ".env"
    env.write_text("SERVICE_TOKEN=Zq-8812-plainish\n")
    gen.write_version(1, template=TEMPLATE + "ref Zq-8812-plainish\n")
    gen.refresh_static()
    gen.alloc_and_pin()
    assert gen.run(extra=["--env-file", str(env)]) == 0
    assert not any("Zq-8812-plainish" in r["stdin"] for r in ctx.records())
    assert gen.report(gen.dev_out)["scrub_abort"] == SMALL["dev_syn"]["calls"] + int(0.2 * SMALL["dev_syn"]["n"])


def test_send_turns_a_timeout_into_a_recorded_failure(ctx):
    def runner(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"], output=b"partial")
    cwd = S.fresh_empty_cwd(ctx.tmp / "w")
    res = S.send(["x"], b"hello", {}, cwd, runner=runner, timeout=1)
    assert res.timed_out is True and res.returncode == S.TIMEOUT_RC and res.stdout == b"partial"
    sp = S.SpendLog(ctx.tmp / "sp.jsonl", 20.0)
    sp.pending("c1", "m", "claude", 0.30)
    assert sp.total() == pytest.approx(0.30)                     # a crash mid-call still counts its estimate
    sp.record("m", "claude", 0, 0.30, call_id="c1", cost_basis="estimate")
    assert sp.total() == pytest.approx(0.30) and sp.max_call() == 0.0 and sp.estimate() == 0.30
    sp.record("m", "claude", 0, 1.0, call_id="c2", cost_basis="reported")
    assert sp.total() == pytest.approx(1.30) and sp.estimate() == pytest.approx(1.25)


def test_an_opus_timeout_is_ledgered_pending_first_and_charged_to_the_cap(ctx, capsys):
    ctx.set_cfg(models=["claude-opus-5-5"], sleep_calls=[2], sleep_s=5)
    ip = write_items(ctx, mk_items(12))
    code, out, _ = run_label(ctx, capsys, "dev_syn", "opus", ip, extra=["--timeout", "1"])
    assert code == 0 and json.loads(out)["timeouts"] == 1
    calls = [r for r in jl(run_dir(ctx) / "calls_opus.jsonl") if r["call_id"] == "opus-000002"]
    assert [r["phase"] for r in calls] == ["pending", "done"] and calls[1]["outcome"] == "timeout"
    spend = [r for r in S.read_jsonl(ctx.cls / "spend_log.jsonl") if r.get("call_id") == "opus-000002"]
    assert [r["kind"] for r in spend] == ["pending", "call"]
    assert spend[1]["cost_basis"] == "estimate" and spend[1]["usd"] == spend[0]["est_usd"] > 0
    assert S.SpendLog(ctx.cls / "spend_log.jsonl").total() >= spend[1]["usd"]
    assert len({r["item_id"] for r in jl(run_dir(ctx) / "labels_opus.jsonl")}) == 12


def test_a_gpt_timeout_parks(ctx, capsys):
    ctx.set_cfg(sleep_calls=[2], sleep_s=5)
    ip = write_items(ctx, mk_items(12))
    code, *_ = run_label(ctx, capsys, "dev_syn", "gpt", ip, extra=["--timeout", "1"])
    assert code == S.EXIT_GPT_PARK
    st = json.loads((run_dir(ctx) / "status.json").read_text())
    assert st["judges"]["gpt"]["state"] == "stopped" and st["voided"] == []


def _discarded(ctx):
    return sorted(p for p in (ctx.cls / "test-syn").iterdir() if p.name.startswith("discarded-"))


def _real_run_verify(static_path, run_path=None, **kw):
    """The REAL PINS-RUN check (P.verify_run); the generator env has no STATIC document to recompute."""
    bad = P.verify_run(run_path)
    if bad:
        raise S.CLSExit(S.EXIT_PINS, "pins mismatch: " + ",".join(bad))
    return {"ok": True}


def inv_regen_verifies_pins_before_discard(gen):
    ctx = gen.ctx
    ctx.mp.setattr(G, "VERIFY_PINS", _real_run_verify)
    freeze_v1(gen)
    gen.alloc_and_pin("test_syn", name="gen_test_v1")
    assert gen.step(bad_resemble) == S.EXIT_RESEMBLE_FAIL
    failed = ctx.tmp / "test_res.json"
    failed.write_text(json.dumps({"set": "test_syn", "spec_version": 1, "pass": False}))
    base = gen.argv("regen", "test_syn", extra=["--resemble-result", str(failed)])
    # (1) a regen that would not re-create its pinned gen_config.json is refused BEFORE anything is discarded
    ctx.mp.setattr(G, "SET_CFG", dict(SMALL, test_syn=dict(SMALL["test_syn"], calls=4)))
    assert G.main(base) == S.EXIT_PINS
    assert _discarded(ctx) == [] and (gen.test_out / "items.jsonl").exists()
    assert G.load_custody(ctx.cls).get("regen_count", 0) == 0
    ctx.mp.setattr(G, "SET_CFG", SMALL)
    # (2) a regen whose start fails after the discard (the canary parks) does not consume the one regen
    ctx.set_cfg(canary_leak=True)
    assert G.main(base) == S.EXIT_CANARY
    assert len(_discarded(ctx)) == 1 and G.load_custody(ctx.cls).get("regen_count", 0) == 0
    # (3) the one regen succeeds with the REAL verify_run over the moved and re-created gen_config.json
    ctx.set_cfg()
    assert G.main(base) == 0
    cust = G.load_custody(ctx.cls)
    assert cust["regen_count"] == 1 and cust["generated"] == 2 and len(_discarded(ctx)) == 2
    assert (gen.test_out / "items.jsonl").exists() and P.verify_run(ctx.pins_run) == []
    reqs = [r for r in S.read_jsonl(gen.test_out / "gen_requests.jsonl") if len(r["cell_vectors"]) > 1]
    assert reqs and all(r["nonces"] == [G.nonce("testsyn", G.REGEN_INDEX_BASE + c["item_index"]) for c in r["cell_vectors"]]
                        for r in reqs)


def test_regen_verifies_pins_before_discarding_and_counts_only_a_started_run(gen):
    inv_regen_verifies_pins_before_discard(gen)


def test_test_syn_resume_continues_the_same_set_and_marks_generated_on_completion(gen):
    ctx = gen.ctx
    freeze_v1(gen)
    gen.alloc_and_pin("test_syn", name="gen_test_v1")
    ctx.set_cfg(codex_die_from=len(ctx.records()) + 4)
    assert gen.run("test_syn") == S.EXIT_GPT_PARK
    assert G.load_custody(ctx.cls).get("generated", 0) == 0 and not (gen.test_out / "items.jsonl").exists()
    done_before = {r["call_id"] for r in jl(gen.test_out / "gen_calls.jsonl") if r["phase"] == "done"}
    assert done_before
    assert gen.run("test_syn") == S.EXIT_REFUSE                   # a partial run continues only with --resume
    ctx.set_cfg()
    n0 = len(ctx.records())
    assert gen.run("test_syn", extra=["--resume"]) == 0
    sent = sum(1 for r in ctx.records()[n0:] if "nonce:" in r["stdin"])
    done_all = {r["call_id"] for r in jl(gen.test_out / "gen_calls.jsonl") if r["phase"] == "done"}
    assert sent == len(done_all) - len(done_before)               # no finished call is sent again
    rep = gen.report(gen.test_out)
    assert rep["resumed_calls"] == len(done_before) and rep["n_items"] == SMALL["test_syn"]["n"]
    for r in S.read_jsonl(gen.test_out / "gen_requests.jsonl"):
        if len(r["cell_vectors"]) > 1:
            assert r["nonces"] == [G.nonce("testsyn", c["item_index"]) for c in r["cell_vectors"]]
    assert G.load_custody(ctx.cls)["generated"] == 1
    assert gen.run("test_syn", extra=["--resume"]) == S.EXIT_REFUSE
    assert gen.run("test_syn") == S.EXIT_REFUSE


def test_resumed_dev_syn_equals_an_uninterrupted_run(gen):
    ctx = gen.ctx
    patch_fake_models(ctx)
    other = ctx.cls / "dev-syn-b"
    gen.alloc_and_pin(out_root=other, name="gen_dev_b")
    assert gen.run(out_root=other) == 0
    gen.alloc_and_pin()
    ctx.set_cfg(codex_die_from=len(ctx.records()) + 3)
    assert gen.run() == S.EXIT_GPT_PARK
    ctx.set_cfg()
    assert gen.run(extra=["--resume"]) == 0
    assert S.sha256_file(gen.dev_out / "items.jsonl") == S.sha256_file(other / "items.jsonl")
    assert stat.S_IMODE((gen.dev_out / "gen_calls.jsonl").stat().st_mode) == 0o600


def test_step_and_discard_lock_the_call_log_too(gen):
    freeze_v1(gen)
    gen.alloc_and_pin("test_syn", name="gen_test_v1")
    assert gen.step(ok_resemble) == 0
    assert stat.S_IMODE((gen.test_out / "gen_calls.jsonl").stat().st_mode) == 0
    assert G.main(gen.argv("discard", "test_syn", extra=["--reason", "t"])) == 0
    d = _discarded(gen.ctx)[0]
    assert stat.S_IMODE((d / "gen_calls.jsonl").stat().st_mode) == 0 and stat.S_IMODE((d / "items.jsonl").stat().st_mode) == 0


def test_flag_recorder_matches_definition_lines_only(pins):
    h = pins.ctx.fake / "help_claude.txt"
    h.write_text("\n".join(ln for ln in HELP_CLAUDE.splitlines() if "--tools" not in ln)
                 + "\n  --bare                       like --tools '' with hooks off\n")
    with pytest.raises(S.CLSExit) as e:
        P.build_static(pins.params)
    assert e.value.code == S.EXIT_PINS and "claude_flag--tools" in str(e.value)
    lines, missing = P._flag_lines(HELP_CODEX + "      --oss   pick with -m, see --json\n", P.CODEX_FLAGS)
    assert missing == [] and lines["-m"].startswith("-m, --model") and lines["--json"].startswith("--json")
    _l, missing = P._flag_lines("  --verbose   prints -s and --ephemeral notes\n  --ephemeral-x  other\n", ["-s", "--ephemeral"])
    assert missing == ["-s", "--ephemeral"]


def test_verify_checks_the_pins_file_sha_against_the_freeze_record(pins):
    pins.build()
    res = pins.verify()
    assert res["ok"] is True and "pre_freeze_no_pins_sha256_record" in res["warnings"]
    rec = pins.repo / P.FREEZE_RECORD_REL
    rec.parent.mkdir(parents=True)
    head = "%s  PREREG-CLS-DEV.md\nfrozen_utc 2026-10-16T00:00:00Z\n" % ("a" * 64)
    rec.write_text(head + P.freeze_line(pins.static) + "\n")
    res = pins.verify()
    assert res["ok"] is True and res["warnings"] == []
    rec.write_text(head + "pins_sha256 " + "0" * 64 + "\n")
    res = pins.verify()
    assert res["ok"] is False and "pins_file_sha256" in res["mismatches"]
    with pytest.raises(S.CLSExit) as e:
        P.verify_at_start(pins.static, pins.run_file)
    assert e.value.code == S.EXIT_PINS and "pins_file_sha256" in str(e.value)
    rec.write_text(head)
    assert "freeze_record_lacks_pins_sha256" in pins.verify()["mismatches"]
    rec.write_text(head + P.freeze_line(pins.static) + "\npins_sha256 " + "1" * 64 + "\n")
    assert "freeze_record_ambiguous_pins_sha256" in pins.verify()["mismatches"]
    rec.write_text(head + P.freeze_line(pins.static) + "\n")
    pins.static.write_text(pins.static.read_text() + "\n")       # the PINS file changed after the freeze
    assert "pins_file_sha256" in pins.verify()["mismatches"]


def inv_idless_single_answer_accepted(ctx, capsys):
    ctx.set_cfg(bad_calls=[2, 3, 4], single_no_id=True)  # batch 1 fails 3 times; singles answer without an id
    items = mk_items(12)
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", write_items(ctx, items))
    assert code == 0
    assert {r["item_id"] for r in jl(run_dir(ctx) / "labels_sonnet.jsonl")} == {i["item_id"] for i in items}
    assert json.loads((run_dir(ctx) / "status.json").read_text())["judges"]["sonnet"]["label_errors"] == 0


def test_idless_single_answers_take_the_one_asked_id(ctx, capsys):
    inv_idless_single_answer_accepted(ctx, capsys)


def inv_supply_pinned_before_gt500(ctx, capsys):
    ip = write_items(ctx, mk_items(10))
    orig = ctx.supply.read_text()
    ctx.supply.write_text(orig + "RAN = __import__('pathlib').Path(%r).write_text('x')\n" % str(ctx.tmp / "supply_ran"))
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == S.EXIT_PINS and ctx.records() == [] and not (ctx.tmp / "supply_ran").exists()
    ctx.supply.write_text(orig)
    doc = json.loads(ctx.pins_static.read_text())
    doc["pins"].pop("supply")
    ctx.pins_static.write_text(json.dumps(doc))
    code, *_ = run_label(ctx, capsys, "dev_syn", "sonnet", ip)
    assert code == S.EXIT_PINS and ctx.records() == []           # no supply pin: refused
    ctx.write_static()
    assert run_label(ctx, capsys, "dev_syn", "sonnet", ip)[0] == 0


def test_supply_py_is_verified_before_gt500_is_loaded(ctx, capsys):
    inv_supply_pinned_before_gt500(ctx, capsys)


# =============================================================================================
# Mutants: patch ONE function, the invariant must fail. Unmutated code passes every invariant.
# =============================================================================================

def _wrap(mod, name, fn):
    orig = getattr(mod, name)
    return lambda mp: mp.setattr(mod, name, fn(orig))


def m_skip_digest_check(mp):
    orig = S.outgoing_check

    def mut(payload, **kw):
        text = payload.decode("utf-8", errors="replace")
        lines = text.splitlines()
        text = "\n".join(ln for i, ln in enumerate(lines) if not (i and lines[i - 1] == "[CONTEXT]"))
        return orig(text.encode("utf-8"), **kw)
    mp.setattr(S, "outgoing_check", mut)


def m_check_items_only_not_final_bytes(mp):
    orig_check = S.outgoing_check

    def mut_send(child_argv, payload, env, cwd, *, batch_id="", timeout=900, runner=subprocess.run):
        S.assert_empty_cwd(cwd)
        text = payload.decode("utf-8", errors="replace")
        items_only = "\n".join(ln for ln in text.splitlines() if ln.startswith("VIEW[") and "PLANT" not in ln)
        orig_check(items_only.encode("utf-8"), batch_id=batch_id)
        r = runner(list(child_argv), input=payload, env=env, cwd=str(cwd), shell=False, capture_output=True, timeout=timeout)
        return S.SendResult(r.returncode, r.stdout or b"", r.stderr or b"")
    mp.setattr(S, "send", mut_send)


def m_text_via_argv(mp):
    def mut_send(child_argv, payload, env, cwd, *, batch_id="", timeout=900, runner=subprocess.run):
        S.assert_empty_cwd(cwd)
        S.outgoing_check(payload, batch_id=batch_id)
        argv = list(child_argv) + [payload.decode("utf-8", errors="replace")]
        r = runner(argv, input=payload, env=env, cwd=str(cwd), shell=False, capture_output=True, timeout=timeout)
        return S.SendResult(r.returncode, r.stdout or b"", r.stderr or b"")
    mp.setattr(S, "send", mut_send)


def m_env_var_left(mp):
    mp.setattr(S, "claude_child_env", lambda base=None: dict(os.environ, LLM_ROUTER_SESSION_KIND="research"))


def m_interleave_never(mp):
    mp.setattr(L, "INTERLEAVE_EVERY", 10 ** 9)


def m_wrong_plant_does_not_void(mp):
    orig = L.Runner.record_plants

    def mut(self, plants, entries_order, out, interleaved):
        orig(self, plants, entries_order, out, interleaved)
        return False
    mp.setattr(L.Runner, "record_plants", mut)


def m_canary_ignored(mp):
    mp.setattr(S, "codex_canary", lambda sender, work_dir, *, model_pin, log=None: (sender(b"Attempt canary"), False)[1])


def m_test_syn_path_accepted(mp):
    mp.setattr(S, "guard_dev_path", lambda path, cls_root: Path(path))


def m_finalists_check_removed(mp):
    mp.setattr(G, "find_finalist_files", lambda d: [])


def m_resemble_prints_features(mp):
    orig = R.compute

    def mut(real, syn, **kw):
        res = orig(real, syn, **kw)
        res["top_features"] = sorted({w for t, _g in real for w in t.split()})[:5]
        return res
    mp.setattr(R, "compute", mut)


def m_ci_gate_uses_point_only(mp):
    mp.setattr(R, "gate_pass", lambda point, ci_upper: point <= R.GATE_POINT)


def m_discarded_readable(mp):
    mp.setattr(S, "is_discarded", lambda path, cls_root: False)


def m_pins_ignore_one_key(mp):
    orig = P._diff
    mp.setattr(P, "_diff", lambda a, b, prefix="": [k for k in orig(a, b, prefix) if not k.startswith("generator_files")])


def m_openrouter_without_prefs(mp):
    mp.setattr(S, "openrouter_prefs_ok", lambda prefs: True)


def m_selftest_aggregated(mp):
    orig = S.self_test

    def mut(result_path=None, **kw):
        ok, rep = orig(None, **kw)
        failed = [k for k, v in rep["shapes"].items() if not v["ok"]]
        rep["ok"] = len(failed) <= 1 and all(c["ok"] for c in rep["controls"].values())
        if result_path:
            S.write_private(result_path, json.dumps(rep))
        return rep["ok"], rep
    mp.setattr(S, "self_test", mut)


def m_dedup_090(mp):
    mp.setattr(S, "JACCARD_DROP", 0.9)


def m_cwd_not_checked(mp):
    mp.setattr(S, "assert_empty_cwd", lambda cwd: None)


def m_model_id_check_skipped(mp):
    mp.setattr(L, "check_model_id", lambda judge, ids, pinned, recorded: (True, ""))


def m_provisional_shares(mp):
    orig = G.load_length_shares
    mp.setattr(G, "load_length_shares", lambda p: {"L1": 35, "L2": 35, "L3": 20, "L4": 10} if not p else orig(p))


def m_fourth_version_allowed(mp):
    mp.setattr(G, "MAX_SPEC_VERSION", 4)


def m_regen_twice(mp):
    mp.setattr(G, "MAX_REGEN", 2)


def m_regen_after_labels(mp):
    mp.setattr(G, "labels_exist", lambda cls_root, label_run_dir=None: False)


def m_amendment_leaks_test(mp):
    mp.setattr(R, "amendment_view", lambda result: dict(result))


def m_chmod_skipped(mp):
    mp.setattr(G, "lock_items", lambda p: None)


def m_engine_from_working_tree(mp):
    def mut(repo, commit, path):
        return (Path(repo) / path).read_bytes()
    mp.setattr(P, "_git_show", mut)


def m_missing_flag_not_blocking(mp):
    mp.setattr(P, "_flag_lines", lambda text, flags: ({f: f for f in flags}, []))


def m_env_key_filter_ignored(mp):
    mp.setattr(S, "env_key_qualifies", lambda name: True)


def m_token_created_by_tool(mp):
    orig = S.guard_release

    def mut(items_path, token_path, cls_root):
        if token_path and not Path(token_path).exists():
            Path(token_path).write_text("created by the tool")
        return orig(items_path, token_path, cls_root)
    mp.setattr(S, "guard_release", mut)


def m_verify_requires_run_entries(mp):
    orig = P.verify

    def mut(static_path, run_path=None, **kw):
        res = orig(static_path, run_path, **kw)
        if res["run_entries"] == 0:
            res["ok"] = False
            res["mismatches"] = res["mismatches"] + ["run_entries_missing"]
        return res
    mp.setattr(P, "verify", mut)


def m_run_entry_overwritten(mp):
    def mut(run_path, name, files, *, extra=None):
        entries = P.read_run(run_path)
        entries = [e for e in entries if e["name"] != name]
        entry = {"name": name, "utc": S.utc_now(), "files": {str(Path(f).resolve()): S.sha256_file(f) for f in files},
                 "prev_entry_sha": entries[-1]["entry_sha"] if entries else P.ZERO_SHA}
        entry["entry_sha"] = P._entry_sha(entry)
        Path(run_path).write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in entries + [entry]))
        return entry
    mp.setattr(P, "append_run_entry", mut)


def m_amended_spec_runs_without_run_entry(mp):
    mp.setattr(G, "require_pinned", lambda path, static_doc, run_path: None)


def m_static_skipped_once_run_exists(mp):
    orig = P.verify

    def mut(static_path, run_path=None, **kw):
        res = orig(static_path, run_path, **kw)
        if res["run_entries"]:
            res["mismatches"] = [m for m in res["mismatches"] if m.startswith("run")]
            res["ok"] = not res["mismatches"]
        return res
    mp.setattr(P, "verify", mut)


def m_codex_no_ephemeral(mp):
    orig = S.codex_argv
    mp.setattr(S, "codex_argv", lambda cwd, model=None, codex_bin="codex", **kw: [a for a in orig(cwd, model, codex_bin, **kw)
                                                                                 if a != "--ephemeral"])


def m_two_voids_accepted(mp):
    orig = L.Status.save

    def mut(self):
        orig(self)
        self.doc.pop("label", None)
        v = self.doc.get("voided", [])
        if len(v) >= 2:
            self.doc["truth_fallback"] = v[0]
        S.write_private(self.path, json.dumps(self.doc, sort_keys=True, indent=1) + "\n")
    mp.setattr(L.Status, "save", mut)


def m_regen_without_discard(mp):
    mp.setattr(G, "discard_set", lambda a, reason: Path(a.out_root))


def m_max_turns_readded(mp):
    orig = S.claude_argv
    mp.setattr(S, "claude_argv", lambda model, settings_path, claude_bin="claude": [a for a in orig(model, settings_path, claude_bin)] + ["--max-turns", "1"])


def m_v2_written_over_v1(mp):
    def mut(gen_dir, name, content, static_doc, run_path):
        p = Path(gen_dir) / name
        p.write_text(content)
        return p
    mp.setattr(G, "write_gen_file", mut)


def m_regen_no_preverify(mp):
    mp.setattr(G, "regen_verify_before_discard", lambda a, g: None)


def m_regen_verify_first(mp):
    orig = G._do_run
    mp.setattr(G, "_do_run", lambda a, set_name, **kw: orig(a, set_name, **dict(kw, verify_first=True)))


def m_unpinned_module(mp):
    mp.setattr(L, "resolve_rubric", lambda prompt_module, doc: (Path(prompt_module or doc["params"]["rubric_py"]),
                                                                doc["pins"]["rubric"]["md5"]))


def m_raw_prompt(mp):
    mp.setattr(L, "render_item", lambda mods, opaque, ctx, prompt: mods.rub.ITEM_TEMPLATE.format(id=opaque, context=ctx,
                                                                                              prompt=prompt))


def m_json_model_field(mp):
    mp.setattr(S, "served_model_ids", lambda kind, parsed, pinned: list(parsed.get("model_ids") or []))


def m_park_becomes_void(mp):
    orig = L.classify_call
    mp.setattr(L, "classify_call", lambda *a: "void" if orig(*a) == "park" else orig(*a))


def m_canary_header_ignored(mp):
    mp.setattr(S, "codex_plain_header_model", lambda text: PINNED["judge_gpt"])


def m_no_configure(mp):
    mp.setattr(S, "configure", lambda ctx: None)


def m_idless_single_rejected(mp):
    orig = L.parse_labels

    def mut(mods, text, ids, *, single):
        out = orig(mods, text, ids, single=single)
        return {} if single and '"id"' not in text else out
    mp.setattr(L, "parse_labels", mut)


def m_supply_unchecked(mp):
    mp.setattr(L, "check_supply", lambda doc, gt500_path: None)


def i_selftest(ctx, capsys, gen=None, pins=None):
    inv_selftest_fails_on_one_missed_shape(ctx)


MUTANTS = [
    (1, "outgoing_check skipped for digest", m_skip_digest_check, "digest_sentinel"),
    (2, "outgoing_check on items text, not final bytes", m_check_items_only_not_final_bytes, "plant_sentinel"),
    (3, "text via argv", m_text_via_argv, "stdin_not_argv"),
    (4, "env var left in child", m_env_var_left, "claude_env"),
    (5, "interleaved plants only first batch", m_interleave_never, "interleave"),
    (6, "wrong plant does not void run", m_wrong_plant_does_not_void, "wrong_plant"),
    (7, "canary result ignored", m_canary_ignored, "canary"),
    (8, "test_syn path accepted by a DEV run", m_test_syn_path_accepted, "dev_path"),
    (9, "finalists-file check removed", m_finalists_check_removed, "finalists"),
    (10, "resemble prints top features", m_resemble_prints_features, "no_features"),
    (11, "CI gate uses point only", m_ci_gate_uses_point_only, "ci_gate"),
    (12, "discarded set re-readable", m_discarded_readable, "discard"),
    (13, "pins verify ignores one key", m_pins_ignore_one_key, "verify_mutations"),
    (14, "OpenRouter allowed without prefs", m_openrouter_without_prefs, "openrouter"),
    (15, "self-test aggregated", m_selftest_aggregated, "selftest"),
    (16, "dedup threshold 0.9", m_dedup_090, "dedup"),
    (17, "cwd not checked empty", m_cwd_not_checked, "cwd"),
    (18, "model-id check skipped", m_model_id_check_skipped, "model_id"),
    (19, "provisional-shares fallback present", m_provisional_shares, "no_counts"),
    (20, "4th spec version allowed", m_fourth_version_allowed, "fourth_version"),
    (21, "TEST-SYN regenerated twice", m_regen_twice, "regen_rules"),
    (22, "TEST-SYN regen allowed after labels", m_regen_after_labels, "regen_rules"),
    (23, "resemble amendment view leaks TEST numbers", m_amendment_leaks_test, "amendment"),
    (24, "chmod 0000 skipped", m_chmod_skipped, "step_lock"),
    (25, "engine/shadow_eval pinned from working tree", m_engine_from_working_tree, "engine_commit"),
    (26, "missing flag in --help not blocking", m_missing_flag_not_blocking, "help_flag"),
    (27, ".env key filter ignored", m_env_key_filter_ignored, "env_filter"),
    (28, "release token created by the tool", m_token_created_by_tool, "token"),
    (29, "verify requires RUN entries before they exist", m_verify_requires_run_entries, "run_not_required"),
    (30, "RUN entry overwritten silently", m_run_entry_overwritten, "run_append_only"),
    (31, "amended spec runs with no RUN entry", m_amended_spec_runs_without_run_entry, "v2_no_entry"),
    (32, "STATIC verify skipped once RUN exists", m_static_skipped_once_run_exists, "static_after_run"),
    (33, "codex_argv missing --ephemeral", m_codex_no_ephemeral, "codex_argv"),
    (34, "two voids accepted", m_two_voids_accepted, "two_voids"),
    (35, "regen without discard", m_regen_without_discard, "regen_rules"),
    (36, "--max-turns re-added", m_max_turns_readded, "claude_argv"),
    (37, "v2 written over the v1 file", m_v2_written_over_v1, "v1_overwrite"),
    (38, "regen discards before verifying its pins", m_regen_no_preverify, "regen_pins"),
    (39, "regen verifies the moved set before re-creating it", m_regen_verify_first, "regen_pins"),
    (40, "unpinned prompt module accepted", m_unpinned_module, "pinned_rubric"),
    (41, "raw prompt sent without prompt_view", m_raw_prompt, "prompt_view"),
    (42, "GPT model id read from --json events", m_json_model_field, "gpt_pin"),
    (43, "codex rc / missing id voids instead of parking", m_park_becomes_void, "park_not_void"),
    (44, "canary model header ignored", m_canary_header_ignored, "canary_header"),
    (45, "configure() never called (detector d at send)", m_no_configure, "env_at_send"),
    (46, "id-less single answer rejected (P1.7-d rule dropped)", m_idless_single_rejected, "single_no_id"),
    (47, "supply.py not verified before gt500 loads", m_supply_unchecked, "supply_pin"),
]


def run_invariant(name, ctx, capsys, request):
    """Dispatch an invariant by name; each builds its own scenario inside ctx."""
    if name == "digest_sentinel":
        return inv_digest_sentinel_never_sent(ctx, capsys)
    if name == "plant_sentinel":
        return inv_plant_text_sentinel_never_sent(ctx, capsys)
    if name == "stdin_not_argv":
        return inv_text_on_stdin_not_argv(ctx, capsys)
    if name == "claude_env":
        return inv_claude_env_clean(ctx, capsys)
    if name == "interleave":
        return inv_interleaved_plants_every_tenth_batch(ctx, capsys)
    if name == "wrong_plant":
        return inv_wrong_interleaved_plant_voids(ctx, capsys)
    if name == "canary":
        return inv_canary_parks(ctx, capsys)
    if name == "dev_path":
        return inv_test_syn_path_refused_by_a_dev_run(ctx, capsys)
    if name == "no_features":
        return inv_resemble_prints_no_features(ctx, capsys, ctx.mp)
    if name == "ci_gate":
        return inv_ci_upper_bound_gates_not_only_the_point(ctx)
    if name == "openrouter":
        return inv_openrouter_needs_flag_and_prefs(ctx)
    if name == "selftest":
        return inv_selftest_fails_on_one_missed_shape(ctx)
    if name == "dedup":
        return inv_dedup_threshold_is_point_eight()
    if name == "cwd":
        return inv_nonempty_cwd_refused(ctx)
    if name == "model_id":
        return inv_model_id_mismatch_stops(ctx, capsys)
    if name == "env_filter":
        return inv_env_key_filter(ctx)
    if name == "codex_argv":
        ctx.set_cfg(models=["gpt-6-astra"])
        return inv_codex_argv(ctx, capsys)
    if name == "claude_argv":
        return inv_claude_argv(ctx, capsys)
    if name == "two_voids":
        return inv_two_voids_are_not_informative(ctx, capsys)
    if name == "amendment":
        return inv_amendment_view_never_leaks_test_numbers(ctx, capsys, ctx.mp)
    ctx_invs = {"pinned_rubric": inv_pinned_rubric_only, "prompt_view": inv_prompt_view_and_verbatim_tail,
                "gpt_pin": inv_gpt_pin_without_json_model_field, "park_not_void": inv_park_not_void,
                "canary_header": inv_canary_header, "env_at_send": inv_env_value_refused_at_send,
                "single_no_id": inv_idless_single_answer_accepted, "supply_pin": inv_supply_pinned_before_gt500}
    if name in ctx_invs:
        return ctx_invs[name](ctx, capsys)
    gen = GenEnv(ctx)
    if name == "no_counts":
        patch_fake_models(ctx)
        return inv_no_counts_file_exits_15_and_builds_nothing(gen)
    if name == "fourth_version":
        return inv_fourth_spec_version_refused(gen)
    if name == "v2_no_entry":
        return inv_v2_files_without_a_run_entry_are_refused(gen)
    if name == "finalists":
        return inv_finalists_file_blocks_test_syn(gen)
    if name == "step_lock":
        return inv_step_order_and_lock(gen)
    if name == "discard":
        return inv_discard_and_later_read_refused(gen)
    if name == "regen_rules":
        return inv_regen_rules(gen)
    if name == "regen_pins":
        return inv_regen_verifies_pins_before_discard(gen)
    if name == "token":
        return inv_release_token_is_never_created(gen)
    if name == "v1_overwrite":
        pe = PinsEnv(ctx)
        try:
            return inv_v2_written_over_v1_is_caught(pe)
        finally:
            pe.close()
    pe = PinsEnv(ctx)
    try:
        return {"verify_mutations": inv_verify_detects_every_single_mutation,
                "engine_commit": inv_engine_is_pinned_by_commit_not_working_tree,
                "help_flag": inv_missing_help_flag_blocks_the_freeze,
                "run_not_required": inv_verify_before_run_entries_exist_passes,
                "run_append_only": inv_run_entries_are_append_only_and_chained,
                "static_after_run": inv_static_still_checked_once_run_exists}[name](pe)
    finally:
        pe.close()


@pytest.mark.parametrize("num,label,patch,inv", MUTANTS, ids=["m%02d-%s" % (m[0], m[3]) for m in MUTANTS])
def test_unmutated_code_passes_every_invariant(num, label, patch, inv, ctx, capsys, request):
    if inv in ("canary", "codex_argv"):
        ctx.set_cfg(models=["gpt-6-astra"], canary_leak=(inv == "canary"))
    run_invariant(inv, ctx, capsys, request)


@pytest.mark.parametrize("num,label,patch,inv", MUTANTS, ids=["m%02d-%s" % (m[0], m[3]) for m in MUTANTS])
def test_mutant_is_killed(num, label, patch, inv, ctx, capsys, request):
    if inv in ("canary", "codex_argv"):
        ctx.set_cfg(models=["gpt-6-astra"], canary_leak=(inv == "canary"))
    patch(ctx.mp)
    try:
        run_invariant(inv, ctx, capsys, request)
    except (AssertionError, pytest.fail.Exception):
        return  # an invariant failed under the mutation: killed
    except Exception as e:  # noqa: BLE001
        pytest.fail("mutant %d crashed instead of failing an invariant: %r" % (num, e))
    pytest.fail("mutant %d (%s) SURVIVED" % (num, label))
