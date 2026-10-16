"""scripts/p17d_synthetic.py (select / shadow / label / score / status) on synthetic fixtures only.

No real transcript, no network, no model: Ollama, the decision endpoint and ``claude -p`` are mocked. The run
dir is a tmp dir. The frozen prereg itself is read (pins and freeze record), never written.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import time
from collections import Counter
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    """Import the script by path. Its dataclasses need it in sys.modules while it executes; the entry is
    removed afterwards so nothing later in the process imports this copy (test_tests_do_not_break_their_
    neighbours)."""
    spec = importlib.util.spec_from_file_location("p17d_synthetic", ROOT / "scripts" / "p17d_synthetic.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


ps = _load_script()

from llm_router import shadow_eval as se  # noqa: E402
from llm_router.proxy import cls_input  # noqa: E402

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
SENTINEL = "zebrafinch"        # in every fixture prompt; must never reach stdout or stderr
HEAD = "c0ffee" + "0" * 34
TIERS = ("haiku", "sonnet", "opus")


# ============================================================ fixtures
def _pins_block(path: Path) -> dict:
    block = re.search(r"```pins\n(.*?)```", path.read_text(encoding="utf-8"), re.S).group(1)
    return dict(line.split("=", 1) for line in block.splitlines() if "=" in line)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_item(sid: str, n: int, *, group: str = "F", ge3_id: str | None = None, prompt: str | None = None,
              earlier: list | None = None, assistant: str | None = "Done: the change is in place.",
              kind: str = "organic", served: str = "claude-opus-4-1") -> dict:
    ts = f"2026-10-0{1 + n % 7}T10:{n // 60 % 60:02d}:{n % 60:02d}.000Z"
    cwd = "/tmp/proj"
    words = ("rename", "explain", "design", "fix", "list", "refactor", "investigate", "commit")
    prompt = prompt if prompt is not None else (
        f"{SENTINEL} please {words[n % len(words)]} the module number {n} in area {sid[:4]} now")
    earlier = [f"{SENTINEL} earlier prompt {j} for {n}" for j in range(2)] if earlier is None else earlier
    msgs = [{"role": "user", "content": e} for e in earlier]
    if assistant:
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": assistant}]})
    msgs.append({"role": "user", "content": prompt})
    pa = ps.pa
    pieces = pa.assemble_pieces(msgs)
    full = cls_input.assemble({"messages": msgs, "system": f"Primary working directory: {cwd}"})
    item_id = hashlib.sha256(f"p17d:item:{sid}:{ts}".encode()).hexdigest()[:16]
    words_n = len(prompt.split())
    return {"item_id": item_id, "group": group, "ge3_id": ge3_id, "session_id": sid, "ts": ts, "cwd": cwd,
            "prompt": prompt, "text_sha": "x", "earlier": pieces["earlier"],
            "assistant_tail": pieces["assistant_tail"], "capped": False, "assembled_context": full.context,
            "assemble_ok": pa.rebuild_matches(full, cwd, pieces, prompt), "context_withheld": False,
            "msg_id": None, "served_model": served, "served_tier": se.tier_name(served), "kind": kind,
            "kind_basis": "x", "words": words_n, "length_band": pa.length_band(words_n),
            "first_in_session": not earlier and not assistant, "era": "proxy", "month": ts[:7],
            "requested_model": served, "requested_basis": "transcript_served", "requested_tier": se.tier_name(served)}


def sessions(n_sessions: int) -> list[str]:
    return [hashlib.md5(f"s{i}".encode()).hexdigest()[:8] + "-0000-4000-8000-000000000000" for i in range(n_sessions)]


class World:
    def __init__(self, tmp_path: Path, items: list):
        self.tmp = tmp_path
        self.run = tmp_path / "run"
        self.run.mkdir(mode=0o700)
        self.items = items
        (self.run / "items.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items), encoding="utf-8")
        (self.run / "attach_counts.json").write_text("{}\n", encoding="utf-8")
        shutil.copy(ROOT / "src" / "llm_router" / "proxy" / "claude_tiers.yaml", self.run / "tier_policy.yaml")
        for f in self.run.iterdir():
            os.chmod(f, 0o600)
        self.usage = tmp_path / "usage.json"
        self.set_usage(10, 10)
        self.p1_gates = tmp_path / "P1.json"
        self.set_gate(json.dumps({"test_gates_2026_10_13": {"P1.7-c": {
            "verdict": "PASS", "value": "v",
            "gap": "classifier shadow switched on before the artifact (process clause not judged)"
        }}}).encode())
        amend1 = tmp_path / "AMEND1.md"
        shutil.copy(ps.AMEND1, amend1)
        amend1.with_suffix(".sha256").write_text(
            f"{sha(amend1)}  AMEND1.md\nfrozen_utc 2026-10-16T00:00:00Z\n", encoding="utf-8")
        self.cfg = ps.Cfg(run=self.run, amend1=amend1, measurement=tmp_path / "measure" / "p17d-synthetic.json",
                          usage=self.usage,
                          p1_gates=self.p1_gates,
                          ge3_labels=tmp_path / "ge3_labels.jsonl", head_sha=lambda: HEAD,
                          src_matches=lambda _sha: True, tree_clean=lambda: True, asleep=_no_sleep)
        self.cfg.pins.update(items=sha(self.run / "items.jsonl"), attach_counts=sha(self.run / "attach_counts.json"),
                             tier_policy=sha(self.run / "tier_policy.yaml"))

    def set_gate(self, raw: bytes):
        self.p1_gates.write_bytes(raw)

    def set_usage(self, session_pct, weekly_pct, age_s=0):
        self.usage.write_text(json.dumps({"session_pct": session_pct, "weekly_pct": weekly_pct,
                                          "updated_at": time.time() - age_s}), encoding="utf-8")

    def harness_line(self):
        ps.append_line(self.cfg.use_log, f"harness 2026-10-15T17:00:00Z sha={HEAD}")

    def use(self) -> list[str]:
        return ps.use_lines(self.cfg)


async def _no_sleep(_s):
    return None


def big_items(n_f=120, n_sessions=6, n_g=0, g_sessions=4) -> list:
    sids = sessions(n_sessions + g_sessions)
    items = [make_item(sids[i % n_sessions], i) for i in range(n_f)]
    items += [make_item(sids[n_sessions + i % g_sessions], 1000 + i, group="G", ge3_id=f"gt-{i:04d}")
              for i in range(n_g)]
    return items


@pytest.fixture
def world(tmp_path):
    return World(tmp_path, big_items())


# ---------------------------------------------------------------- shadow mocks (no network)
class Nimble:
    """Stands in for local_classifier.classify_async (the HTTP call). ``script`` maps text_sha -> sources per
    attempt; anything else answers ``llm`` with a tier derived from the text_sha."""

    def __init__(self, script: dict | None = None):
        self.script = script or {}
        self.calls = Counter()

    async def __call__(self, assembled, *, session_id, text_sha, **_kw):
        from llm_router import decision_classifier as dc
        from llm_router.local_classifier import Verdict

        self.calls[text_sha] += 1
        seq = self.script.get(text_sha, [])
        src = seq[self.calls[text_sha] - 1] if self.calls[text_sha] <= len(seq) else "llm"
        if src != "llm":
            return dc._fail(src, "nimble:9b", 5.0)
        tier = TIERS[int(text_sha[:2], 16) % 3]
        return Verdict(None, tier, None, None, None, None, None, "direct", "llm", "nimble:9b", "sys1", 120.0,
                       confidence=0.9)


@pytest.fixture
def ollama(monkeypatch):
    """Mock every Ollama touch point: artifact identity, quiet gate, /api/ps, warm-up and the verdict call."""
    from llm_router import local_classifier as lc

    nimble = Nimble()
    state = {"free": 60.0, "load1": 1.0, "resident": True,
             "msha": ps.NIMBLE_MANIFEST_SHA256, "ids": [ps.NIMBLE_LIST_ID],
             "details": dict(zip(("format", "quantization_level", "runner"), ps.NIMBLE_DETAILS))}
    monkeypatch.setattr(ps, "nimble_manifest_sha256", lambda: state["msha"])
    monkeypatch.setattr(ps, "ollama_list_ids", lambda: state["ids"])
    monkeypatch.setattr(ps, "ollama_show_details", lambda: state["details"])
    monkeypatch.setattr(ps, "free_mem_pct", lambda: state["free"])
    monkeypatch.setattr(ps, "load1", lambda: state["load1"])
    monkeypatch.setattr(ps, "ollama_ps", lambda: ["nimble:9b", "foreign:1b"])

    async def warm(_m):
        state["warmed"] = state.get("warmed", 0) + 1

    async def loaded(_m, _b):
        return state["resident"]

    monkeypatch.setattr(lc, "_warm", warm)
    monkeypatch.setattr(lc, "_is_loaded", loaded)
    monkeypatch.setattr(lc, "classify_async", nimble)
    from llm_router import engine_door
    monkeypatch.setattr(engine_door, "shadow", lambda *a, **k: None)   # background engine log: not under test
    lc._reset_state()
    state["nimble"] = nimble
    return state


# ---------------------------------------------------------------- label mocks (no model)
class FakePaused(Exception):
    pass


def fake_mods(n_planted=6):
    expects = ["haiku", "haiku", "sonnet", "sonnet", "opus", "opus"][:n_planted]
    planted = [{"id": f"cal-{t[0]}{i}", "expect": t, "context": "Working directory: /x",
                "prompt": f"PLANTED:{t} synthetic calibration item {i}"} for i, t in enumerate(expects)]

    def extract_json(text):
        return json.loads(text)

    def valid_label(lab, rub):
        try:
            return (lab["tier"] in rub.TIERS and all(int(lab[k]) in range(1, 6) for k in rub.DIMS)
                    and isinstance(lab["needs_tools"], bool) and isinstance(lab["reason"], str))
        except Exception:  # noqa: BLE001
            return False

    def quota_guard(gt, usage_path):
        try:
            u = json.loads(Path(usage_path).read_text())
        except (OSError, ValueError):
            raise FakePaused("usage.json missing") from None
        if time.time() - float(u["updated_at"]) > 1800:
            raise FakePaused("stale")
        if float(u["session_pct"]) >= 80 or float(u["weekly_pct"]) >= 85:
            raise FakePaused("quota")

    gt = SimpleNamespace(Paused=FakePaused, extract_json=extract_json, valid_label=valid_label,
                         prompt_view=lambda t: t, cohen_kappa=lambda a, b: 1.0 if a == b else 0.0)
    rub = SimpleNamespace(RUBRIC="SYSTEM RUBRIC", DIMS=["scope", "risk"], TIERS=list(TIERS), PLANTED=planted,
                          ITEM_TEMPLATE="### ITEM id={id}\n[CONTEXT]\n{context}\n\n[PROMPT TO LABEL]\n{prompt}\n"
                                        "### END ITEM id={id}",
                          BATCH_TAIL="Return labels.", SINGLE_TAIL="Return one label.")
    sl = SimpleNamespace(scrub=lambda t: t, WITHHELD="(withheld)", quota_guard=quota_guard)
    return gt, rub, sl


ITEM_RE = re.compile(r"### ITEM id=(L\d+)\n\[CONTEXT\]\n(.*?)\n\n\[PROMPT TO LABEL\]\n(.*?)\n### END ITEM", re.S)


def truth_of(prompt: str) -> str:
    m = re.search(r"PLANTED:(\w+)", prompt)
    if m:
        return m.group(1)
    return TIERS[int(hashlib.sha256(prompt.encode()).hexdigest()[:2], 16) % 3]


class Judge:
    """Stands in for ``claude -p``. Records every call; never prints."""

    def __init__(self, *, model="claude-opus-5-5[1m]", cost=0.1, wrong_planted=False, drop=(), rc=0):
        self.model, self.cost, self.wrong_planted, self.drop, self.rc = model, cost, wrong_planted, set(drop), rc
        self.calls: list[dict] = []

    def __call__(self, cmd, **kw):
        user = cmd[-1]
        found = ITEM_RE.findall(user)
        self.calls.append({"cmd": cmd, "ids": [f[0] for f in found], "kw": kw, "single": "one label" in user})
        if self.rc:
            return subprocess.CompletedProcess(cmd, self.rc, "", "")
        labs = []
        for oid, _ctx, prompt in found:
            if oid in self.drop:
                continue
            t = truth_of(prompt)
            if self.wrong_planted and "PLANTED:" in prompt:
                t = "opus" if t != "opus" else "haiku"
            labs.append({"id": oid, "scope": 2, "risk": 2, "needs_tools": True, "tier": t, "reason": "r"})
        res = labs[0] if (labs and "one label" in user) else {"labels": labs}
        out = {"result": json.dumps(res), "total_cost_usd": self.cost, "modelUsage": {self.model: {}},
               "usage": {"input_tokens": 100, "output_tokens": 10}}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")


def install_judge(monkeypatch, judge: Judge, mods=None):
    monkeypatch.setattr(ps, "load_label_mods", lambda cfg: mods or fake_mods())
    real = subprocess.run

    def run(cmd, *a, **kw):
        if isinstance(cmd, list) and "--system-prompt" in cmd:
            return judge(cmd, **kw)
        return real(cmd, *a, **kw)

    monkeypatch.setattr(ps.subprocess, "run", run)


def do_select(w):
    assert ps.cmd_select(w.cfg) == ps.EXIT_OK


def do_shadow(w):
    w.harness_line()
    assert ps.cmd_shadow(w.cfg) == ps.EXIT_OK


@contextlib.contextmanager
def refuses(pattern: str = ""):
    """A refusal (exit 2) whose message matches ``pattern``; the message is never prompt text."""
    with pytest.raises(ps.pa.Refuse) as ei:
        yield
    assert re.search(pattern, ei.value.msg), ei.value.msg


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ============================================================ 1. pins
NUMERIC_TYPES = (int, float)


def test_every_prereg_pin_equals_the_script_constant():
    pins = _pins_block(ps.PREREG)
    assert len(pins) >= 40
    for key, want in pins.items():
        got = getattr(ps, key)
        if isinstance(got, bool):
            assert str(got).lower() == want, key
        elif isinstance(got, dict):
            assert ",".join(f"{k}:{v}" for k, v in got.items()) == want, key
        elif isinstance(got, tuple):
            assert ",".join(got) == want, key
        elif isinstance(got, NUMERIC_TYPES):
            assert float(want) == float(got), (key, got, want)
        else:
            assert str(got) == want, (key, got, want)


def test_pins_agree_with_the_scorer_the_attach_script_and_the_files_on_disk():
    assert ps.COST == se.COST and float(ps.LAMBDA) == se.LAMBDA
    assert (ps.BOOTSTRAP_B, ps.BOOTSTRAP_SEED, ps.MIN_N, ps.MAX_SESSION_SHARE) == \
        (se.BOOT_B, se.BOOT_SEED, se.MIN_N, se.MAX_SESSION_SHARE)
    assert ps.pa.PINS["gt500"] == ps.GT500_SHA256 and ps.pa.PINS["supply"] == ps.SUPPLY_SHA256
    assert ps.pa.WINDOW_END_UTC == ps.WINDOW_END_UTC and ps.pa.LABEL == ps.LABEL
    assert sha(ps.ATTACH_SCRIPT) == ps.ATTACH_SCRIPT_SHA256
    assert sha(Path(se.__file__)) == ps.SHADOW_EVAL_SHA256
    assert ps.default_pins()["items"] == ps.ITEMS_SHA256
    from llm_router import local_classifier as lc
    assert ps.TIMEOUT_COOLDOWN_S == lc.COOLDOWN_S and ps.NIMBLE_TIMEOUT_MS == lc.DEFAULT_TIMEOUT_MS


def test_the_frozen_prereg_matches_its_freeze_record():
    res = ps.check_prereg.check(ps.PREREG)
    assert res["match"] and res["frozen_utc"] == "2026-10-15T16:32:58Z"


# ============================================================ common refusals
@pytest.mark.parametrize("cmd", ["select", "shadow", "label", "score"])
def test_every_command_but_status_refuses_on_a_prereg_mismatch(world, tmp_path, cmd, capsys):
    bad = tmp_path / "PREREG-copy.md"
    bad.write_bytes(ps.PREREG.read_bytes() + b"\n")
    shutil.copy(ps.PREREG.with_suffix(".sha256"), bad.with_suffix(".sha256"))
    world.cfg.prereg = bad
    fn = {"select": ps.cmd_select, "shadow": ps.cmd_shadow, "label": ps.cmd_label,
          "score": lambda c: ps.cmd_score(c)}[cmd]
    with refuses("differs from its freeze record"):
        fn(world.cfg)
    assert ps.cmd_status(world.cfg) == 0
    assert not world.cfg.selection.exists() and not world.cfg.use_log.exists()


def test_refuses_before_frozen_utc(world):
    world.cfg.now = lambda: ps.parse_utc("2026-10-15T16:32:57Z")
    with refuses("frozen_utc"):
        ps.cmd_select(world.cfg)


@pytest.mark.parametrize("cmd", ["select", "shadow", "label", "score"])
def test_every_command_refuses_once_use_log_is_terminal(world, cmd):
    ps.append_line(world.cfg.use_log, "terminal 2026-10-15T17:00:00Z test")
    fn = {"select": ps.cmd_select, "shadow": ps.cmd_shadow, "label": ps.cmd_label,
          "score": lambda c: ps.cmd_score(c)}[cmd]
    with refuses("terminal"):
        fn(world.cfg)


@pytest.mark.parametrize("pin", ["items", "attach_counts", "attach_script", "shadow_eval"])
def test_refuses_when_a_pin_differs(world, pin):
    world.cfg.pins[pin] = "0" * 64
    with refuses("sha256"):
        ps.cmd_select(world.cfg)


def test_refuses_when_router_src_differs_from_repo_sha(world):
    world.cfg.src_matches = lambda s: s != ps.REPO_SHA
    with refuses("REPO_SHA"):
        ps.cmd_select(world.cfg)


def test_main_refuses_with_exit_2_and_run_dir_is_overridable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ps.RUN_ENV, str(tmp_path / "nowhere"))
    assert ps.main(["select"]) == ps.EXIT_REFUSED
    assert ps.main(["status", "--run-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["selected"] == 0


# ============================================================ 3. min_body / assemble round trip
def _hist(earlier, assistant, prompt):
    msgs = [{"role": "user", "content": e} for e in earlier]
    if assistant:
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": assistant}]})
    return msgs + [{"role": "user", "content": prompt}]


@pytest.mark.parametrize("earlier,assistant,prompt", [
    ([], None, f"{SENTINEL} first prompt"),
    ([f"{SENTINEL} e{i} " + "w " * 20 for i in range(4)], "ok", f"{SENTINEL} three earlier"),
    (["a"], "x " * 600 + "end", f"{SENTINEL} long assistant"),
    (["y" * 299 + " tail" + "z" * 10], "ok", f"{SENTINEL} space at the earlier cut"),
    (["a"], "x" * 400 + " " + "y" * 500, f"{SENTINEL} space at the assistant cut"),
    (["a"], "ok", f"{SENTINEL} " + "long " * 600),
])
def test_replay_body_round_trips_assemble(earlier, assistant, prompt):
    it = make_item(sessions(1)[0], 1, earlier=earlier, assistant=assistant, prompt=prompt)
    assert it["assemble_ok"] is True
    assert ps.check_assemble(it)
    body = ps.build_body(it)
    from llm_router.proxy.steps import has_client_tools
    assert has_client_tools(body) and body["model"] == it["requested_model"]


def test_a_changed_context_is_an_assemble_mismatch():
    it = make_item(sessions(1)[0], 1)
    assert ps.check_assemble({**it, "assembled_context": it["assembled_context"] + " "}) is False
    assert ps.check_assemble({**it, "assemble_ok": False}) is False


# ============================================================ 4, 11. selection
def test_selection_cap_never_exceeds_half_at_n_ge_4_and_is_seeded():
    items = big_items(n_f=300, n_sessions=3)
    sel = ps.select(items)
    assert sel["n"] == ps.N_SELECT
    by = {i["item_id"]: i["session_id"] for i in items}
    counts = Counter()
    for n, i in enumerate(sel["f_ids"], 1):
        counts[by[i]] += 1
        if n >= 4:
            assert max(counts.values()) <= Fraction(1, 2) * n
    assert ps.select(list(reversed(items)))["f_ids"] == sel["f_ids"]


def test_feasibility_gate_fires_and_writes_terminal(tmp_path, capsys):
    w = World(tmp_path, big_items(n_f=40, n_sessions=4))
    assert ps.cmd_select(w.cfg) == ps.EXIT_TERMINAL
    assert w.use()[0].startswith("terminal ") and "insufficient supply" in w.use()[0]
    assert json.loads(w.cfg.result.read_text())["verdict"] == ps.NOT_INFORMATIVE
    assert mode(w.cfg.selection) == 0o444
    with refuses():
        ps.cmd_select(w.cfg)


def test_admission_g_follows_f_and_never_takes_non_organic_or_unflagged_items(tmp_path):
    items = big_items(n_f=60, n_sessions=4, n_g=120, g_sessions=12)
    sids = sessions(20)
    items.append(make_item(sids[19], 5000, group="G", ge3_id="gt-codex", kind="research"))
    items.append(make_item(sids[19], 5001, group="G", ge3_id=None))
    sel = ps.select(items)
    assert sel["admitted"] and sel["f_ids"] and sel["g_ids"]
    by = {i["item_id"]: i for i in items}
    assert all(by[i]["group"] == "F" for i in sel["f_ids"]) and all(by[i]["group"] == "G" for i in sel["g_ids"])
    assert not {by[i]["ge3_id"] for i in sel["g_ids"]} & {"gt-codex", None}
    assert sel["n"] <= ps.N_SELECT_ADMIT and sel["feasible"]
    order = sorted([i for i in items if i["group"] == "G" and i["ge3_id"] and i["kind"] == "organic"],
                   key=lambda i: hashlib.sha256(f"p17d:admit:20261015:{i['ge3_id']}".encode()).hexdigest())
    pos = [o["item_id"] for o in order]
    assert [pos.index(i) for i in sel["g_ids"]][:3] == sorted(pos.index(i) for i in sel["g_ids"])[:3]


def test_refused_f_items_never_re_enter_the_admission_walk():
    sids = sessions(30)
    items = [make_item(sids[0], i) for i in range(30)] + [make_item(sids[1], 100 + i) for i in range(5)]
    items += [make_item(sids[i % 2], 500 + i, group="G", ge3_id=f"gt-big-{i}") for i in range(20)]
    items += [make_item(sids[2 + i % 25], 1000 + i, group="G", ge3_id=f"gt-{i}") for i in range(150)]
    sel = ps.select(items)
    f_pool = [i for i in items if i["group"] == "F"]
    f_walk = ps.walk(f_pool, ps.seed_order(ps.SELECT_SEED_FORMAT, "item_id"), Counter(), 0, ps.N_SELECT)
    assert len(f_walk) < 35 and sel["f_ids"] == [i["item_id"] for i in f_walk]
    refused = {i["item_id"] for i in f_pool} - set(sel["f_ids"])
    assert refused and not refused & set(sel["g_ids"])
    assert sel["admitted"] and sel["g_ids"]


def test_admission_does_not_fire_at_100_f_items():
    sel = ps.select(big_items(n_f=100, n_sessions=10, n_g=50))
    assert len(sel["f_ids"]) == 100 and not sel["admitted"] and sel["g_ids"] == []
    sel = ps.select(big_items(n_f=99, n_sessions=10, n_g=50))
    assert sel["admitted"]


def test_select_early_error_gate_on_assemble_mismatch(tmp_path):
    items = big_items(n_f=120, n_sessions=6)
    for it in items[:20]:
        it["assemble_ok"] = False
    w = World(tmp_path, items)
    assert ps.cmd_select(w.cfg) == ps.EXIT_TERMINAL
    assert "early_error_gate" in w.use()[0]


def test_select_writes_once_and_ids_follow_item_id_format(world, capsys):
    do_select(world)
    doc = json.loads(world.cfg.selection.read_text())
    assert doc["ok"] and doc["items_sha256"] == world.cfg.pins["items"] and mode(world.cfg.selection) == 0o444
    with refuses("exists"):
        ps.cmd_select(world.cfg)


# ============================================================ shadow
def test_shadow_runs_every_item_through_the_production_seam(world, ollama, capsys):
    do_select(world)
    do_shadow(world)
    recs = ps.json_rows(world.cfg.shadow)
    sel = json.loads(world.cfg.selection.read_text())
    assert len(recs) == len(sel["ids"]) and all(r["kind"] == "classifier_shadow" for r in recs)
    r = recs[0]
    assert r["session_kind"] == "organic" and r["step_class"] == "turn_first" and r["rules"]["tier"] in TIERS
    assert r["tier_live"] == "opus" and r["requested"] == "opus" and r["backend"] == "systemone"
    assert isinstance(r["ts"], float) and r["ts"] > ps.parse_utc("2026-10-15T16:32:58Z")
    meta = json.loads(world.cfg.shadow_meta.read_text())
    assert meta["done"] and meta["preflight"][0]["quiet_gate"]["foreign_models"] == ["foreign:1b"]
    assert mode(world.cfg.shadow) == 0o600 and mode(world.cfg.shadow_meta) == 0o600
    assert mode(world.cfg.scratch) == 0o700 and ollama["warmed"] == 1
    assert os.environ.get("LLM_ROUTER_LOCAL_CLASSIFIER") is None   # env restored


def test_shadow_refuses_without_the_harness_line_or_with_another_sha(world, ollama):
    do_select(world)
    with refuses("harness"):
        ps.cmd_shadow(world.cfg)
    ps.append_line(world.cfg.use_log, "harness 2026-10-15T17:00:00Z sha=deadbeef")
    with refuses("harness"):
        ps.cmd_shadow(world.cfg)
    assert not world.cfg.shadow.exists()


def test_shadow_refuses_when_labels_exist_and_label_refuses_before_done(world, ollama, monkeypatch):
    do_select(world)
    install_judge(monkeypatch, Judge())
    world.harness_line()
    with refuses("not done"):
        ps.cmd_label(world.cfg)
    world.cfg.labels.write_text("")
    world.harness_line()
    with refuses("labels.jsonl exists"):
        ps.cmd_shadow(world.cfg)
    assert not world.cfg.shadow.exists()


def test_errata1_identity_is_accepted_and_recorded(world, ollama):
    do_select(world)
    do_shadow(world)
    rec = json.loads(world.cfg.shadow_meta.read_text())["preflight"][0]
    assert rec["digest"]["manifest_sha256"] == ps.NIMBLE_MANIFEST_SHA256
    assert rec["harness_line"] == ps.governing_harness_line(world.cfg)
    assert rec["selection_sha256"] == sha(world.cfg.selection)
    assert rec["amend1_sha256"] == sha(world.cfg.amend1)


@pytest.mark.parametrize("changes,error", [
    ({"msha": "9b953de7a5336756ece1cb1e8632e374b3dbdabe3d02d405cf8291da2d43a131",
      "ids": ["9b953de7a533"],
      "details": {"format": "gguf", "quantization_level": "Q8_0", "runner": "llamacpp"}}, "manifest"),
    ({"msha": "0" * 64}, "manifest"),
    ({"msha": None}, "manifest"),
    ({"ids": ["13ae0a506103"]}, "list"),
    ({"ids": [ps.NIMBLE_LIST_ID, ps.NIMBLE_LIST_ID]}, "list"),
    ({"ids": []}, "list"),
])
def test_nimble_identity_mismatch_writes_nothing(world, ollama, changes, error):
    do_select(world)
    world.harness_line()
    ollama.update(changes)
    with refuses(error):
        ps.cmd_shadow(world.cfg)
    assert not world.cfg.shadow.exists() and not world.cfg.shadow_meta.exists()


@pytest.mark.parametrize("field", ["format", "quantization_level", "runner"])
def test_nimble_wrong_detail_writes_nothing(world, ollama, field):
    do_select(world)
    world.harness_line()
    ollama["details"][field] = "wrong"
    with refuses("details"):
        ps.cmd_shadow(world.cfg)
    assert not world.cfg.shadow.exists() and not world.cfg.shadow_meta.exists()


@pytest.mark.parametrize("error", [OSError, ValueError])
def test_nimble_show_failure_writes_nothing(world, ollama, monkeypatch, error):
    do_select(world)
    world.harness_line()

    def fail():
        raise error("unreadable")

    monkeypatch.setattr(ps, "ollama_show_details", fail)
    with refuses("api/show"):
        ps.cmd_shadow(world.cfg)
    assert not world.cfg.shadow.exists() and not world.cfg.shadow_meta.exists()


def test_digest_check_seams(monkeypatch):
    details = {"format": "safetensors", "quantization_level": "mxfp8", "runner": "mlx"}
    monkeypatch.setattr(ps, "nimble_manifest_sha256", lambda: ps.NIMBLE_MANIFEST_SHA256)
    monkeypatch.setattr(ps, "ollama_list_ids", lambda: [ps.NIMBLE_LIST_ID])
    monkeypatch.setattr(ps, "ollama_show_details", lambda: details)
    assert ps.digest_check() == {
        "manifest_sha256": ps.NIMBLE_MANIFEST_SHA256, "list_id": ps.NIMBLE_LIST_ID,
        "details": details, "method": "AMEND1 s2 (amend2-errata1 s2)"}


def test_ollama_list_ids_round_three_parse_and_failure(monkeypatch):
    code = [0]

    def run(args, **kwargs):
        assert args == ["ollama", "list"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 60,
                          "stdin": subprocess.DEVNULL, "env": dict(os.environ, HOME=str(ps.REAL_HOME))}
        return subprocess.CompletedProcess(args, code[0],
            "NAME ID SIZE MODIFIED\nnimble:9b    13ae0a506102    9.3 GB    8 days ago\n"
            "qwen:1b abcdef123456 1 GB now\n", "")

    monkeypatch.setattr(ps.subprocess, "run", run)
    assert ps.ollama_list_ids() == [ps.NIMBLE_LIST_ID]
    code[0] = 1
    with refuses("ollama list"):
        ps.ollama_list_ids()


@pytest.mark.parametrize("free,load", [(39.9, 1.0), (60.0, 4.0)])
def test_quiet_gate_failure_parks_with_no_row(world, ollama, free, load, capsys):
    do_select(world)
    world.harness_line()
    ollama["free"], ollama["load1"] = free, load
    assert ps.cmd_shadow(world.cfg) == ps.EXIT_PAUSED
    assert not world.cfg.shadow.exists() and not any(x.startswith("terminal") for x in world.use())


def test_retry_once_and_the_successful_retry_is_the_scored_row(world, ollama):
    do_select(world)
    sel = json.loads(world.cfg.selection.read_text())
    items = {i["item_id"]: i for i in world.items}
    keys = [ps.item_key(items[i])[1] for i in sel["ids"][:3]]
    ollama["nimble"].script = {keys[0]: ["timeout", "llm"], keys[1]: ["cold", "parse_error"], keys[2]: ["abstain"]}
    do_shadow(world)
    recs = ps.json_rows(world.cfg.shadow)
    srcs = {k: [r["source"] for r in recs if r["text_sha"] == k] for k in keys}
    assert srcs[keys[0]] == ["timeout", "llm"] and srcs[keys[1]] == ["cold", "parse_error"]
    assert srcs[keys[2]] == ["abstain", "llm"]
    last = {r["text_sha"]: r for r in ps.last_per_key(recs)}
    assert last[keys[0]]["source"] == "llm" and last[keys[1]]["source"] == "parse_error"
    assert sum(ollama["nimble"].calls.values()) == len(sel["ids"]) + 3


def test_a_crash_after_the_record_and_before_the_meta_resumes_without_a_third_call(world, ollama):
    do_select(world)
    sel = json.loads(world.cfg.selection.read_text())
    items = {i["item_id"]: i for i in world.items}
    k0 = ps.item_key(items[sel["ids"][0]])[1]
    ollama["nimble"].script = {k0: ["timeout", "timeout", "llm"]}
    real_save = ps.save_meta

    def crashing_save(cfg, meta):
        if meta.get("attempts") and meta["attempts"][-1]["attempt"] == 2:
            raise KeyboardInterrupt("crash after the retry record, before its meta write")
        real_save(cfg, meta)

    world.harness_line()
    ps.save_meta = crashing_save
    try:
        with pytest.raises(KeyboardInterrupt):
            ps.cmd_shadow(world.cfg)
    finally:
        ps.save_meta = real_save
    assert ollama["nimble"].calls[k0] == 2
    assert ps.cmd_shadow(world.cfg) == ps.EXIT_OK
    assert ollama["nimble"].calls[k0] == 2
    assert [r["source"] for r in ps.json_rows(world.cfg.shadow) if r["text_sha"] == k0] == ["timeout", "timeout"]
    assert ollama["warmed"] == 2   # warm-up repeated on resume


def test_assemble_mismatch_item_gets_no_nimble_call_and_enters_as_fallback(tmp_path, ollama):
    items = big_items()
    items[0]["assemble_ok"] = False
    w = World(tmp_path, items)
    do_select(w)
    do_shadow(w)
    k = ps.item_key(items[0])[1]
    recs = [r for r in ps.json_rows(w.cfg.shadow) if r["text_sha"] == k]
    assert [r["source"] for r in recs] == [ps.MISMATCH] and recs[0]["rules"]["tier"] in TIERS
    assert ollama["nimble"].calls[k] == 0
    llm, fell_back = se.llm_pick(recs[0])
    assert fell_back and llm == se.rules_eff(recs[0])


def test_post_shadow_error_gate_writes_terminal_before_any_judge_call(world, ollama, monkeypatch):
    do_select(world)
    sel = json.loads(world.cfg.selection.read_text())
    items = {i["item_id"]: i for i in world.items}
    n_bad = len(sel["ids"]) // 10 + 1
    ollama["nimble"].script = {ps.item_key(items[i])[1]: ["timeout", "timeout"] for i in sel["ids"][:n_bad]}
    world.harness_line()
    assert ps.cmd_shadow(world.cfg) == ps.EXIT_TERMINAL
    assert world.use()[-1].startswith("terminal ") and "post_shadow_error_gate" in world.use()[-1]
    judge = Judge()
    install_judge(monkeypatch, judge)
    with refuses("terminal"):
        ps.cmd_label(world.cfg)
    assert judge.calls == []


def test_exactly_ten_percent_errors_still_passes_the_post_shadow_gate(world, ollama):
    do_select(world)
    sel = json.loads(world.cfg.selection.read_text())
    items = {i["item_id"]: i for i in world.items}
    n_bad = len(sel["ids"]) // 10
    assert n_bad * 10 == len(sel["ids"])
    ollama["nimble"].script = {ps.item_key(items[i])[1]: ["timeout", "timeout"] for i in sel["ids"][:n_bad]}
    do_shadow(world)


def test_engine_decide_in_the_environment_is_overridden(world, ollama, monkeypatch):
    import llm_router.engine as engine
    from llm_router.proxy import backends

    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "decide")
    seen = []

    def no_decision(*a, **k):
        raise AssertionError("the engine decided")

    monkeypatch.setattr(engine, "decide_for_proxy", no_decision)
    monkeypatch.setattr(engine, "decide", no_decision)
    real = backends._classify_class

    def spy(text, **kw):
        seen.append(os.environ.get("LLM_ROUTER_ENGINE_MODE"))
        return real(text, **kw)

    monkeypatch.setattr(backends, "_classify_class", spy)
    do_select(world)
    do_shadow(world)
    assert seen and set(seen) == {"shadow"}
    assert os.environ["LLM_ROUTER_ENGINE_MODE"] == "decide"


def test_rules_tier_falls_back_to_the_requested_tier_like_the_live_proxy(world):
    policy = ps.load_policy(world.cfg)
    body = ps.build_body(world.items[0])
    task, cx, tier = ps.rules_tier(body, policy, "claude-opus-4-1")
    assert tier == (policy.tier_for(task, cx) or policy.tier_of("claude-opus-4-1")).name

    class NoTable:
        def tier_for(self, *_a):
            return None

        def tier_of(self, m):
            return SimpleNamespace(name="sonnet") if m else None

    assert ps.rules_tier(body, NoTable(), "claude-sonnet-4-5")[2] == "sonnet"


def test_complexity_knn_true_refuses(world):
    p = world.run / "tier_policy.yaml"
    os.chmod(p, 0o600)
    p.write_text(p.read_text() + "\ncomplexity_knn: true\n")
    world.cfg.pins["tier_policy"] = sha(p)
    with refuses("complexity_knn"):
        ps.load_policy(world.cfg)


# ============================================================ label
@pytest.fixture
def shadowed(world, ollama):
    do_select(world)
    do_shadow(world)
    return world


def test_label_calibration_first_then_batches_of_8_and_files(shadowed, monkeypatch, capsys):
    w = shadowed
    judge = Judge()
    install_judge(monkeypatch, judge)
    assert ps.cmd_label(w.cfg) == ps.EXIT_OK
    assert any(x.startswith("label done ") for x in w.use())
    first = judge.calls[0]
    assert len(first["ids"]) == 8
    sel = json.loads(w.cfg.selection.read_text())
    n = len(sel["ids"])
    planted_ids = {f"L{n + j:03d}" for j in range(1, 7)}
    assert planted_ids <= set(first["ids"]) and {"L001", "L002"} <= set(first["ids"])
    assert first["ids"] != sorted(first["ids"])        # shuffled
    assert all(len(c["ids"]) <= 8 for c in judge.calls) and len(judge.calls) == 1 + -(-(n - 2) // 8)
    cmd, kw = first["cmd"], first["kw"]
    assert cmd[:2] == ["claude", "-p"] and cmd[cmd.index("--model") + 1] == "opus"
    for flag in ("--no-session-persistence", "--strict-mcp-config", "--max-turns", "--tools"):
        assert flag in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project,local" and cmd[cmd.index("--tools") + 1] == ""
    assert "ANTHROPIC_BASE_URL" not in kw["env"] and kw["cwd"] == str(w.cfg.claude_cwd)
    labels = ps.json_rows(w.cfg.labels)
    assert len(labels) == n and set(labels[0]) == {"text_sha", "session_id", "truth"}
    planted = ps.json_rows(w.cfg.labels_planted)
    assert len(planted) == 6 and all(p["ok"] for p in planted)
    assert all(not p["id"].startswith("cal-") for p in planted)
    for f in (w.cfg.labels, w.cfg.labels_planted, w.cfg.judgements, w.cfg.ledger):
        assert mode(f) == 0o600
    calls = [r for r in ps.json_rows(w.cfg.ledger) if r["kind"] == "call"]
    assert len(calls) == len(judge.calls) and all(r["model_ok"] for r in calls)


def test_label_order_is_f_then_g_by_seed():
    items = big_items(n_f=10, n_sessions=2, n_g=6)
    f_ids = {i["item_id"] for i in items if i["group"] == "F"}
    order = ps.label_order(items, f_ids)
    assert [i["group"] for i in order] == ["F"] * 10 + ["G"] * 6
    key = [hashlib.sha256(f"p17d:label:20261015:{i['item_id']}".encode()).hexdigest() for i in order[:10]]
    assert key == sorted(key)


def test_a_wrong_planted_label_is_terminal_and_never_resent(shadowed, monkeypatch, capsys):
    w = shadowed
    judge = Judge(wrong_planted=True)
    install_judge(monkeypatch, judge)
    assert ps.cmd_label(w.cfg) == ps.EXIT_TERMINAL
    assert w.use()[-1].startswith("terminal ") and "calibration" in w.use()[-1]
    assert len(judge.calls) == 1
    with refuses("terminal"):
        ps.cmd_label(w.cfg)


def test_calibration_needs_all_six_planted_items(shadowed, monkeypatch):
    install_judge(monkeypatch, Judge(), mods=fake_mods(n_planted=5))
    assert ps.cmd_label(shadowed.cfg) == ps.EXIT_TERMINAL
    assert "calibration 5/6" in shadowed.use()[-1]


def test_a_planted_item_never_parsed_is_calibration_failure(shadowed, monkeypatch):
    sel = json.loads(shadowed.cfg.selection.read_text())
    lost = f"L{len(sel['ids']) + 1:03d}"
    judge = Judge(drop={lost})
    install_judge(monkeypatch, judge)
    assert ps.cmd_label(shadowed.cfg) == ps.EXIT_TERMINAL
    assert sum(lost in c["ids"] for c in judge.calls) == 1 + ps.LABEL_BATCH_RETRIES + ps.LABEL_SINGLE_ATTEMPTS


def test_quota_pause_writes_nothing_terminal_and_resume_skips_labelled_and_planted(shadowed, monkeypatch, capsys):
    w = shadowed
    w.set_usage(10, 100)
    judge = Judge()
    install_judge(monkeypatch, judge)
    assert ps.cmd_label(w.cfg) == ps.EXIT_PAUSED
    assert judge.calls == [] and not any(x.startswith("terminal") for x in w.use())
    w.set_usage(10, 84.9)
    calls = {"n": 0}
    real = ps.judge_call

    def pause_after_three(cfg, mods, entries, md):
        calls["n"] += 1
        if calls["n"] == 4:
            w.set_usage(80, 10)
        return real(cfg, mods, entries, md)

    monkeypatch.setattr(ps, "judge_call", pause_after_three)
    assert ps.cmd_label(w.cfg) == ps.EXIT_PAUSED
    assert len(judge.calls) == 3 and not any(x.startswith("terminal") for x in w.use())
    sent_before = [i for c in judge.calls for i in c["ids"]]
    w.set_usage(10, 10)
    assert ps.cmd_label(w.cfg) == ps.EXIT_OK
    sent_after = [i for c in judge.calls[3:] for i in c["ids"]]
    assert not set(sent_before) & set(sent_after)
    sel = json.loads(w.cfg.selection.read_text())
    assert len(ps.json_rows(w.cfg.labels)) == len(sel["ids"])
    assert not any(i in sent_after for i in (f"L{len(sel['ids']) + j:03d}" for j in range(1, 7)))


def test_spend_cap_stops_before_the_call_that_could_cross_10_dollars(shadowed, monkeypatch, capsys):
    w = shadowed
    for _ in range(49):
        ps.append_json(w.cfg.ledger, {"kind": "call", "total_cost_usd": 0.2})
    judge = Judge(cost=0.2)
    install_judge(monkeypatch, judge)
    assert ps.cmd_label(w.cfg) == ps.EXIT_SPEND_CAP   # 9.80 spent + 1.25 x 0.20 > 10.00
    assert judge.calls == [] and not any(x.startswith("terminal") for x in w.use())


def test_spend_guard_first_estimate_and_boundary(tmp_path):
    cfg = ps.Cfg(run=tmp_path)
    assert ps.spend_guard(cfg, cap=0.30) == 0.30           # equality with the cap passes
    ps.append_json(cfg.ledger, {"kind": "call", "total_cost_usd": 9.70})
    with pytest.raises(ps.SpendCap):
        ps.spend_guard(cfg)                              # 9.70 + 12.125 > 10
    cfg2 = ps.Cfg(run=tmp_path / "b")
    cfg2.run.mkdir()
    for c in (0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.5):
        ps.append_json(cfg2.ledger, {"kind": "call", "total_cost_usd": c})
    ps.spend_guard(cfg2)                                 # 8.10 + 0.625 = 8.725 <= 10
    ps.append_json(cfg2.ledger, {"kind": "call", "total_cost_usd": 1.275})
    with pytest.raises(ps.SpendCap):
        ps.spend_guard(cfg2)                             # 9.375 + 1.59375 > 10


def test_non_opus_model_is_exit_4_and_terminal(shadowed, monkeypatch, capsys):
    install_judge(monkeypatch, Judge(model="claude-sonnet-4-5"))
    assert ps.cmd_label(shadowed.cfg) == ps.EXIT_NOT_OPUS
    assert "judge_not_opus" in shadowed.use()[-1]
    assert not shadowed.cfg.labels.exists()


def test_unlabelled_item_after_retries_and_single_is_a_label_error_never_reasked(shadowed, monkeypatch):
    judge = Judge(drop={"L005"})
    install_judge(monkeypatch, judge)
    assert ps.cmd_label(shadowed.cfg) == ps.EXIT_OK
    assert sum("L005" in c["ids"] for c in judge.calls) == 1 + ps.LABEL_BATCH_RETRIES + ps.LABEL_SINGLE_ATTEMPTS
    errs = [r for r in ps.json_rows(shadowed.cfg.ledger) if r["kind"] == "label_error"]
    assert [e["id"] for e in errs] == ["L005"]
    n_calls = len(judge.calls)
    assert ps.cmd_label(shadowed.cfg) == ps.EXIT_OK and len(judge.calls) == n_calls


def test_judge_input_is_blind(shadowed, monkeypatch):
    judge = Judge()
    install_judge(monkeypatch, judge)
    ps.cmd_label(shadowed.cfg)
    user = judge.calls[1]["cmd"][-1]
    for it in shadowed.items:
        assert it["session_id"] not in user and it["item_id"] not in user
    for field in ("tier_live", "rules", "requested", "served", "nimble", "session"):
        assert field not in user.lower()


def test_real_pinned_helpers_load_and_pause_on_the_weekly_quota(world, tmp_path):
    if not (ps.GT_SCRIPTS / "gt500.py").exists() or not ps.SHADOWLAB_PY.exists():
        pytest.skip("pinned GE3 helpers live outside the repo (owner machine only)")
    gt, rub, sl = ps.load_label_mods(world.cfg)
    assert len(rub.PLANTED) == ps.CALIBRATION_N and gt.RUBRIC_MD5 == ps.RUBRIC_MD5
    world.set_usage(10, 100)
    with pytest.raises(gt.Paused):
        sl.quota_guard(gt, str(world.usage))
    world.set_usage(80, 10)
    with pytest.raises(gt.Paused):
        sl.quota_guard(gt, str(world.usage))
    world.set_usage(79, 84)
    sl.quota_guard(gt, str(world.usage))
    world.cfg.pins["shadowlab"] = "0" * 64
    with refuses():
        ps.load_label_mods(world.cfg)


# ============================================================ 6, 7, 10, 16. score and verdict
def V(**kw):
    base = dict(p17c_passed=True, pre_freeze_rows=0, calibration_ok=True, feasibility_ok=True, pins_ok=True,
                errors=0, n_selected=150, n=120, top_session=60, c_llm=5.0, c_rules=5.5, under_llm=10,
                under_rules=12)
    base.update(kw)
    return ps.decide(**base)


@pytest.mark.parametrize("kw,verdict,why", [
    ({}, ps.PASS, ""),
    ({"c_llm": 5.5}, ps.PASS, ""),                                   # tie on cost passes (<=)
    ({"under_llm": 12}, ps.PASS, ""),                                # tie on under-route passes (<=)
    ({"c_llm": 5.5001}, ps.FAIL, ""),
    ({"under_llm": 13}, ps.FAIL, ""),
    ({"n": 99, "top_session": 50}, ps.NOT_INFORMATIVE, "n:"),
    ({"n": 100, "top_session": 50}, ps.PASS, ""),                    # n = 100 is enough
    ({"top_session": 73}, ps.NOT_INFORMATIVE, "share:"),             # 73/120 > 0.60
    ({"top_session": 72}, ps.PASS, ""),                              # 72/120 = 0.60 is allowed
    ({"errors": 16}, ps.NOT_INFORMATIVE, "errors:"),                 # 16/150 > 10%
    ({"errors": 15}, ps.PASS, ""),                                   # 15/150 = 10% is allowed
    ({"calibration_ok": False}, ps.NOT_INFORMATIVE, "calibration"),
    ({"pre_freeze_rows": 1}, ps.NOT_INFORMATIVE, "before frozen_utc"),
    ({"p17c_passed": False}, ps.NOT_INFORMATIVE, "P1.7-c"),
    ({"feasibility_ok": False}, ps.NOT_INFORMATIVE, "feasibility"),
    ({"pins_ok": False}, ps.NOT_INFORMATIVE, "pin"),
])
def test_verdict_each_outcome_and_each_boundary(kw, verdict, why):
    v, reason = V(**kw)
    assert v == verdict and why in reason


def test_verdict_precedence_procedural_and_errors_beat_pass_and_each_other():
    assert V(errors=40)[0] == ps.NOT_INFORMATIVE and V(errors=40)[1].startswith("errors")
    assert V(errors=40, calibration_ok=False)[1].startswith("procedural")
    assert V(errors=40, n=10, top_session=10)[1].startswith("errors")
    assert V(n=10, top_session=10)[1].startswith("n:")


def rec(sid, sha_, *, source="llm", llm="sonnet", rules="opus", requested="opus", live="opus", ts=None, ms=100.0):
    return {"kind": "classifier_shadow", "session_id": sid, "text_sha": sha_, "source": source,
            "llm": {"tier": llm} if source in ("llm", "cache") else None, "rules": {"tier": rules},
            "tier_live": live, "requested": requested, "ts": ts or time.time(), "ms": ms}


def score_fixture(n_rows=120, n_sess=4, llm="sonnet", truth="sonnet", err_rows=0):
    sids = sessions(n_sess)
    records, labels, items_by_key = [], {}, {}
    sel = {"ids": [], "f_ids": [], "g_ids": [], "ok": True}
    for i in range(n_rows):
        sid, k = sids[i % n_sess], f"{i:016x}"
        src = "timeout" if i < err_rows else "llm"
        records.append(rec(sid, k, source=src, llm=llm))
        labels[(sid, k)] = truth
        iid = f"item{i:012d}"
        items_by_key[(sid, k)] = [{"item_id": iid, "session_id": sid, "length_band": "w13-50",
                                   "first_in_session": False, "ts": "2026-10-05T10:00:00.000Z"}]
        sel["ids"].append(iid)
        sel["f_ids"].append(iid)
    return records, labels, items_by_key, sel


def run_score(records, labels, items_by_key, sel, **kw):
    args = dict(p17c_passed=True, pre_freeze_rows=0, calibration_ok=True, label_error_ids=set(), ge3_tiers={})
    args.update(kw)
    return ps.score(None, records, labels, items_by_key, sel, **args)


def test_score_pass_fixture_uses_shadow_eval_and_reports_everything():
    res = run_score(*score_fixture())
    assert res["verdict"] == ps.PASS and res["n"] == 120 and res["label"] == "synthetic (D-99)"
    assert res["arms"]["llm"]["c_lambda2_clamp"] == 2.7 and res["arms"]["rules_eff"]["c_lambda2_clamp"] == 6.15
    assert res["boot_c_lambda2_clamp"]["diff"] == round(2.7 - 6.15, 4)
    assert res["boot_under"]["clusters"] == 4 and res["boot_c_lambda2_clamp"]["ci95_normal"] is not None
    assert "ci95_wide" in res["boot_under"]
    assert res["reference_arms"]["always_opus"]["under"]["k"] == 0
    assert res["lead"][0].startswith("under-route cannot discriminate")
    assert set(res["strata"]) == {"length_band", "truth_tier", "first_in_session", "requested_tier", "era"}
    assert res["groups"]["F"]["n_labeled"] == 120 and res["groups"]["G"]["n_labeled"] == 0


def test_score_fail_fixture():
    records, labels, ibk, sel = score_fixture(llm="haiku", truth="opus")
    res = run_score(records, labels, ibk, sel)
    assert res["verdict"] == ps.FAIL


def test_errors_enter_the_means_as_fallback_but_not_n():
    records, labels, ibk, sel = score_fixture(n_rows=130, err_rows=12)
    res = run_score(records, labels, ibk, sel)
    assert res["n"] == 118 and res["n_fallback"] == 12 and res["n_labeled"] == 130
    assert res["errors"]["nimble"] == 12 and res["verdict"] == ps.PASS
    assert res["sensitivity_error_excluded"]["n_labeled"] == 118
    records, labels, ibk, sel = score_fixture(n_rows=130, err_rows=14)
    assert run_score(records, labels, ibk, sel)["verdict"] == ps.NOT_INFORMATIVE


def test_label_errors_count_toward_the_error_gate():
    records, labels, ibk, sel = score_fixture(n_rows=120)
    dropped = list(labels)[:13]
    for k in dropped:
        del labels[k]
    ids = {ibk[k][0]["item_id"] for k in dropped}
    res = run_score(records, labels, ibk, sel, label_error_ids=ids)
    assert res["errors"]["label"] == 13 and res["verdict"] == ps.NOT_INFORMATIVE


def test_share_is_computed_on_verdict_rows_only():
    a, b = sessions(2)
    records, labels, ibk, sel = [], {}, {}, {"ids": [], "f_ids": [], "g_ids": [], "ok": True}
    for i in range(150):
        sid, k = (a if i < 87 else b), f"{i:016x}"
        records.append(rec(sid, k, source="timeout" if i >= 135 else "llm"))
        labels[(sid, k)] = "sonnet"
        ibk[(sid, k)] = [{"item_id": f"it{i:014d}", "session_id": sid}]
        sel["ids"].append(f"it{i:014d}")
        sel["f_ids"].append(f"it{i:014d}")
    res = run_score(records, labels, ibk, sel)
    assert se.score(records, labels)["largest_session_share"] == 0.58      # all labelled rows: 87/150
    assert res["n"] == 135 and res["largest_session_share_verdict_rows"] == round(87 / 135, 4)
    assert res["verdict"] == ps.NOT_INFORMATIVE and res["verdict_reason"].startswith("share:")


def test_last_record_per_key_is_scored():
    a = rec("s", "k1", source="timeout")
    b = rec("s", "k1", source="llm", llm="haiku")
    assert ps.last_per_key([a, b]) == [b]
    assert se.score([a, b], {("s", "k1"): "haiku"})["n_fallback"] == 1          # shadow_eval keeps the first
    assert se.score(ps.last_per_key([a, b]), {("s", "k1"): "haiku"})["n_fallback"] == 0


def test_pre_freeze_rows_are_counted_from_the_record_file(tmp_path):
    log = tmp_path / "s.jsonl"
    log.write_text(json.dumps(rec("s", "k", ts=ps.parse_utc("2026-10-15T16:00:00Z"))) + "\n")
    assert ps.check_prereg.check(ps.PREREG, log)["before_freeze"] == 1


def full_run(w, monkeypatch, judge=None):
    do_select(w)
    do_shadow(w)
    install_judge(monkeypatch, judge or Judge())
    assert ps.cmd_label(w.cfg) == ps.EXIT_OK


def test_score_end_to_end_started_before_labels_are_opened_and_single_use(world, ollama, monkeypatch, capsys):
    full_run(world, monkeypatch)
    real = ps.labels_from_bytes
    seen = []

    def spy(path):
        seen.append([x for x in world.use() if x.startswith("started ")])
        return real(path)

    monkeypatch.setattr(ps, "labels_from_bytes", spy)
    assert ps.cmd_score(world.cfg) == ps.EXIT_OK
    assert seen and seen[0], "started must be logged before labels.jsonl is parsed"
    line = seen[0][0]
    assert f"labels_sha256={sha(world.cfg.labels)}" in line and f"shadow_sha256={sha(world.cfg.shadow)}" in line
    assert f"prereg_sha256={ps.check_prereg.check(ps.PREREG)['sha256']}" in line and f"git_sha={HEAD}" in line
    res = json.loads(world.cfg.result.read_text())
    assert res["label"] == "synthetic (D-99)" and res["n_selected"] == 120 and mode(world.cfg.result) == 0o600
    m = json.loads(world.cfg.measurement.read_text())
    assert m["label"] == "synthetic (D-99)" and len(m["items"]) == 120
    assert all(isinstance(r["session_index"], int) for r in m["items"])
    text = world.cfg.measurement.read_text()
    assert not any(it["session_id"] in text or SENTINEL in text for it in world.items)
    with refuses("started"):
        ps.cmd_score(world.cfg)


@pytest.mark.parametrize("verdict", ["FAIL", "pass", "NOT RUN"])
def test_score_with_p17c_not_passed_is_not_informative(world, ollama, monkeypatch, verdict):
    full_run(world, monkeypatch)
    world.set_gate(json.dumps({"test_gates_2026_10_13": {"P1.7-c": {"verdict": verdict}}}).encode())
    ps.cmd_score(world.cfg)
    result = json.loads(world.cfg.result.read_text())
    assert result["verdict_reason"].startswith("procedural: P1.7-c")
    assert result["p17c_passed"] is False


def test_score_after_a_spend_cap_stop_runs_on_existing_labels(world, ollama, monkeypatch, capsys):
    do_select(world)
    do_shadow(world)
    judge = Judge(cost=1.0)
    install_judge(monkeypatch, judge)
    assert ps.cmd_label(world.cfg) == ps.EXIT_SPEND_CAP
    assert any(x.startswith("label stopped spend_cap ") for x in world.use())
    assert ps.cmd_score(world.cfg) == ps.EXIT_OK
    res = json.loads(world.cfg.result.read_text())
    assert res["verdict"] == ps.NOT_INFORMATIVE and res["verdict_reason"].startswith("n:")
    assert res["spend_usd"] <= ps.SPEND_CAP_USD


def test_kappa_on_g_is_reported_never_gates(tmp_path, ollama, monkeypatch, capsys):
    w = World(tmp_path, big_items(n_f=60, n_sessions=4, n_g=80, g_sessions=10))
    full_run(w, monkeypatch)
    sel = json.loads(w.cfg.selection.read_text())
    assert sel["admitted"] and sel["g_ids"]
    items = {i["item_id"]: i for i in w.items}
    w.cfg.ge3_labels.write_text("".join(json.dumps({"id": items[i]["ge3_id"], "tier": "sonnet"}) + "\n"
                                        for i in sel["g_ids"]))
    monkeypatch.setattr(ps, "load_label_mods", lambda cfg: fake_mods())
    ps.cmd_score(w.cfg)
    res = json.loads(w.cfg.result.read_text())
    assert res["kappa_fresh_vs_ge3_on_G"]["n"] == len(sel["g_ids"])
    assert res["groups"]["G"]["n_labeled"] == len(sel["g_ids"])


# ============================================================ status, privacy, file access
def test_status_never_parses_labels_and_prints_counts_only(world, ollama, monkeypatch, capsys):
    full_run(world, monkeypatch)
    capsys.readouterr()
    real = json.loads

    def guard(s, *a, **k):
        if isinstance(s, (str, bytes)) and '"truth"' in (s if isinstance(s, str) else s.decode()):
            raise AssertionError("status parsed a label line")
        return real(s, *a, **k)

    monkeypatch.setattr(ps.json, "loads", guard)
    assert ps.cmd_status(world.cfg) == 0
    monkeypatch.setattr(ps.json, "loads", real)
    out = json.loads(capsys.readouterr().out)
    assert out["labels_lines"] == 120 and out["planted_lines"] == 6 and out["shadow_done"] is True
    assert "truth" not in json.dumps(out) and set(out["use_log"]) == {"harness_line", "started", "terminal"}


def test_no_command_prints_prompt_text(world, ollama, monkeypatch, capsys):
    full_run(world, monkeypatch)
    ps.cmd_score(world.cfg)
    ps.cmd_status(world.cfg)
    out = capsys.readouterr()
    assert SENTINEL not in out.out and SENTINEL not in out.err
    assert "earlier prompt" not in out.out
    for line in out.out.splitlines():
        json.loads(line)


_OPENED: list = []
_AUDIT_ON = {"on": False}


def _audit(event, args):
    if _AUDIT_ON["on"] and event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        _OPENED.append(os.fsdecode(args[0]))


sys.addaudithook(_audit)


def test_no_open_under_real_claude_or_llm_router(world, ollama, monkeypatch, capsys):
    _OPENED.clear()
    _AUDIT_ON["on"] = True
    try:
        full_run(world, monkeypatch)
        ps.cmd_score(world.cfg)
        ps.cmd_status(world.cfg)
    finally:
        _AUDIT_ON["on"] = False
    assert _OPENED, "the audit hook saw no open at all"
    bad = [p for p in _OPENED for root in (REAL_HOME / ".claude", REAL_HOME / ".llm-router")
           if os.path.realpath(p).startswith(str(root) + os.sep)]
    assert bad == []
    assert any(p.endswith("labels.jsonl") for p in _OPENED)


def test_default_run_dir_is_under_the_real_home_and_never_used_by_tests(world):
    assert ps.DEFAULT_RUN == REAL_HOME / ".rsi" / "research" / "primary-plan" / "v16" / "p17d"
    assert world.cfg.run != ps.DEFAULT_RUN


def test_scratch_env_is_the_section_4_environment(world):
    seen = {}
    with ps.harness_env(world.cfg):
        seen = {k: v for k, v in os.environ.items() if k.startswith("LLM_ROUTER_") or k == "HOME"}
    assert seen["HOME"] == str(world.cfg.scratch / "home") and seen["LLM_ROUTER_HOME"] == str(world.cfg.scratch / "lr")
    assert seen["LLM_ROUTER_ENGINE_MODE"] == "shadow" and seen["LLM_ROUTER_LOCAL_CLASSIFIER"] == "shadow"
    assert seen["LLM_ROUTER_CLASSIFIER_BACKEND"] == "systemone" and seen["LLM_ROUTER_DECISION_MODEL"] == "nimble:9b"
    assert seen["LLM_ROUTER_CLASSIFIER_KEEP_ALIVE"] == "10m"
    assert seen["LLM_ROUTER_LOCAL_CLASSIFIER_TIMEOUT_MS"] == "2000"
    assert seen["LLM_ROUTER_OLLAMA_URL"] == "http://localhost:11434"
    assert "LLM_ROUTER_DECISION_ABSTAIN_BELOW" not in seen and "LLM_ROUTER_SHADOW_TEXT_SAMPLE" not in seen


def test_wait_resident_is_bounded(monkeypatch, world):
    from llm_router import local_classifier as lc

    polls = {"n": 0}

    async def never(_m, _b):
        polls["n"] += 1
        return False

    monkeypatch.setattr(lc, "_is_loaded", never)
    assert asyncio.run(ps.wait_resident(world.cfg)) is False
    assert polls["n"] == int(ps.COLD_WAIT_S / ps.COLD_POLL_S) + 1


def test_selection_is_bound_to_the_pinned_items_file(world, ollama):
    do_select(world)
    doc = json.loads(world.cfg.selection.read_text())
    os.chmod(world.cfg.selection, 0o600)
    world.cfg.selection.write_text(json.dumps({**doc, "items_sha256": "0" * 64}))
    world.harness_line()
    with refuses("another items.jsonl"):
        ps.cmd_shadow(world.cfg)


def test_report_leads_with_a_group_on_the_opposite_side():
    sids = sessions(8)
    records, labels, ibk = [], {}, {}
    sel = {"ids": [], "f_ids": [], "g_ids": [], "ok": True}
    for i in range(140):
        g = "F" if i < 120 else "G"
        sid, k = sids[i % 4 if g == "F" else 4 + i % 4], f"{i:016x}"
        llm, rules = ("opus", "sonnet") if g == "F" else ("sonnet", "opus")
        records.append(rec(sid, k, llm=llm, rules=rules))
        labels[(sid, k)] = "opus"
        iid = f"it{i:014d}"
        ibk[(sid, k)] = [{"item_id": iid, "session_id": sid}]
        sel["ids"].append(iid)
        sel["f_ids" if g == "F" else "g_ids"].append(iid)
    res = run_score(records, labels, ibk, sel)
    assert res["verdict"] == ps.PASS
    assert any(x.startswith("group G takes the opposite side") for x in res["lead"])
    assert not any(x.startswith("group F") for x in res["lead"])


def test_cooldown_is_waited_out_before_every_attempt_with_the_real_classify_async(tmp_path, monkeypatch):
    from llm_router import engine_door, local_classifier as lc
    from llm_router.local_classifier import Verdict

    monkeypatch.setattr(ps, "nimble_manifest_sha256", lambda: ps.NIMBLE_MANIFEST_SHA256)
    monkeypatch.setattr(ps, "ollama_list_ids", lambda: [ps.NIMBLE_LIST_ID])
    monkeypatch.setattr(ps, "ollama_show_details",
                        lambda: {"format": "safetensors", "quantization_level": "mxfp8", "runner": "mlx"})
    monkeypatch.setattr(ps, "free_mem_pct", lambda: 60.0)
    monkeypatch.setattr(ps, "load1", lambda: 1.0)
    monkeypatch.setattr(ps, "ollama_ps", lambda: ["nimble:9b", "foreign:1b"])
    monkeypatch.setattr(lc, "_warm", _no_sleep)

    async def loaded(_m, _b):
        return True

    monkeypatch.setattr(lc, "_is_loaded", loaded)
    monkeypatch.setattr(engine_door, "shadow", lambda *a, **k: None)
    clock = [1000.0]
    monkeypatch.setattr(lc, "_now", lambda: clock[0])
    requests = Counter()
    failing = set()

    async def asleep(seconds):
        clock[0] += seconds

    async def classify(key, assembled, model, budget, derivation, t_h, t_s):
        requests[key[1]] += 1
        if key[1] in failing:
            lc._cool_until = lc._now() + lc.COOLDOWN_S
            return lc._fail("timeout", model, derivation, 2000.0)
        return Verdict(None, "sonnet", None, None, None, None, None, "direct", "llm", "nimble:9b", "sys1",
                       100.0, confidence=0.9)

    monkeypatch.setattr(lc, "_classify", classify)
    try:
        for part in ("a", "b"):
            lc._reset_state()
            requests.clear()
            root = tmp_path / part
            root.mkdir()
            w = World(root, big_items())
            w.cfg.asleep = asleep
            do_select(w)
            sel = json.loads(w.cfg.selection.read_text())
            items = {i["item_id"]: i for i in w.items}
            keys = [ps.item_key(items[i]) for i in sel["ids"]]
            failing = {k[1] for k in (keys[:1] if part == "a" else keys)}
            w.harness_line()
            assert ps.cmd_shadow(w.cfg) == (ps.EXIT_OK if part == "a" else ps.EXIT_TERMINAL)
            sources = ps.shadow_records(w.cfg)
            if part == "a":
                assert requests[keys[0][1]] == 2 and sources[keys[0]] == ["timeout", "timeout"]
                assert requests[keys[1][1]] == 1 and sources[keys[1]] == ["llm"]
            else:
                assert all(requests[k[1]] == 2 and sources[k] == ["timeout", "timeout"] for k in keys)
                assert len(ps.json_rows(w.cfg.shadow)) == 2 * len(keys)
    finally:
        lc._reset_state()


@pytest.mark.parametrize("rc,payload,error,prior,cost,basis", [
    (1, {"total_cost_usd": 0.12, "modelUsage": {"opus": {}}}, None, None, 0.12, "reported"),
    (1, "not json", None, None, 0.30, "estimate"),
    (1, None, "timeout", 0.2, 0.25, "estimate"),
    (1, None, "oserror", None, 0.0, "not_started"),
    (0, {"modelUsage": {"opus": {}}, "result": "{}"}, None, None, 0.30, "estimate"),
    (1, {}, None, None, 0.30, "estimate"),
    (1, [], None, None, 0.30, "estimate"),
    (1, {"total_cost_usd": True}, None, None, 0.30, "estimate"),
    (1, {"total_cost_usd": -1}, None, None, 0.30, "estimate"),
    (1, {"total_cost_usd": float("nan")}, None, None, 0.30, "estimate"),
    (1, {"total_cost_usd": float("inf")}, None, None, 0.30, "estimate"),
    (1, {"total_cost_usd": "0.12"}, None, None, 0.30, "estimate"),
])
def test_judge_failed_and_unpriced_calls_use_reported_cost_or_estimate(
        tmp_path, monkeypatch, rc, payload, error, prior, cost, basis):
    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({"session_pct": 10, "weekly_pct": 10, "updated_at": time.time()}))
    cfg = ps.Cfg(run=tmp_path, usage=usage)
    if prior is not None:
        ps.append_json(cfg.ledger, {"kind": "call", "total_cost_usd": prior})

    def run(cmd, **kw):
        if error == "timeout":
            raise subprocess.TimeoutExpired(cmd, 900)
        if error == "oserror":
            raise OSError("failed")
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.CompletedProcess(cmd, rc, stdout, "")

    monkeypatch.setattr(ps.subprocess, "run", run)
    assert ps.judge_call(cfg, fake_mods(), [{"oid": "L001", "context": "", "prompt": ""}], "batch") == {}
    rows = ps.json_rows(cfg.ledger)
    row = rows[-1]
    assert row["kind"] == "call" and row["total_cost_usd"] == cost and row["cost_basis"] == basis
    assert rows[-2]["kind"] == "call_pending" and rows[-2]["call_id"] == row["call_id"]
    assert ps.spent(cfg) == ((prior or 0) + cost, max(prior or 0, cost if basis == "reported" else 0))


@pytest.mark.parametrize("crash", [False, True])
def test_judge_pending_cost_is_reconciled_or_survives_a_final_write_crash(tmp_path, monkeypatch, capsys, crash):
    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({"session_pct": 10, "weekly_pct": 10, "updated_at": time.time()}))
    cfg = ps.Cfg(run=tmp_path, usage=usage)
    payload = {"total_cost_usd": 0.12, "modelUsage": {"opus": {}}, "result": "{}"}
    monkeypatch.setattr(ps.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, json.dumps(payload), ""))
    real = ps.append_json

    def append(path, row):
        if crash and row.get("kind") == "call":
            raise RuntimeError("ledger write failed")
        real(path, row)

    monkeypatch.setattr(ps, "append_json", append)
    entries = [{"oid": "L001", "context": "", "prompt": ""}]
    if crash:
        with pytest.raises(RuntimeError, match="ledger write failed"):
            ps.judge_call(cfg, fake_mods(), entries, "batch")
        assert ps.spent(cfg) == (0.30, 0.0)
    else:
        assert ps.judge_call(cfg, fake_mods(), entries, "batch") == {}
        assert ps.spent(cfg) == (0.12, 0.12)
        pending, final = ps.json_rows(cfg.ledger)
        assert pending["kind"] == "call_pending" and final["kind"] == "call"
        assert pending["call_id"] == final["call_id"]
    ps.cmd_status(cfg)
    assert json.loads(capsys.readouterr().out)["spend_usd"] == (0.30 if crash else 0.12)


def test_failed_judge_call_still_rejects_sonnet(tmp_path, monkeypatch):
    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({"session_pct": 10, "weekly_pct": 10, "updated_at": time.time()}))
    cfg = ps.Cfg(run=tmp_path, usage=usage)
    payload = {"total_cost_usd": 0.12, "modelUsage": {"sonnet": {}}}
    monkeypatch.setattr(ps.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, json.dumps(payload), ""))
    with pytest.raises(ps.NotOpus):
        ps.judge_call(cfg, fake_mods(), [{"oid": "L001", "context": "", "prompt": ""}], "batch")
    assert ps.json_rows(cfg.ledger)[-1]["model_ok"] is False


def test_score_refuses_after_quota_pause(shadowed, monkeypatch):
    w = shadowed
    w.set_usage(10, 100)
    install_judge(monkeypatch, Judge())
    assert ps.cmd_label(w.cfg) == ps.EXIT_PAUSED
    with refuses("label has not finished"):
        ps.cmd_score(w.cfg)
    assert not any(x.startswith("started ") for x in w.use())
    assert not ps.label_finished(w.cfg)
    assert not w.cfg.result.exists() and not w.cfg.measurement.exists()


@pytest.mark.parametrize("command", ["shadow", "label", "score"])
def test_code_identity_guards_refuse_before_work(world, ollama, monkeypatch, command):
    if command == "shadow":
        do_select(world)
        world.harness_line()
    else:
        full_run(world, monkeypatch)
    fn = {"shadow": ps.cmd_shadow, "label": ps.cmd_label, "score": lambda cfg: ps.cmd_score(cfg)}[command]
    world.cfg.tree_clean = lambda: False
    with refuses("uncommitted"):
        fn(world.cfg)
    world.cfg.tree_clean = lambda: True
    world.cfg.head_sha = lambda: "f" * 40
    with refuses("harness"):
        fn(world.cfg)
    assert not any(x.startswith("started ") for x in world.use())
    if command == "shadow":
        assert not world.cfg.shadow.exists()


def test_repo_tree_clean_checks_scripts_and_src(monkeypatch):
    seen = []
    output = [""]

    def run(args, **kw):
        seen.append((args, kw))
        return subprocess.CompletedProcess(args, 0, output[0], "")

    monkeypatch.setattr(ps.subprocess, "run", run)
    assert ps.repo_tree_clean()
    args, kw = seen[-1]
    assert args == ["git", "-C", str(ps.ROOT), "status", "--porcelain", "--", "scripts/", "src/"]
    assert kw == {"capture_output": True, "text": True, "timeout": 60, "stdin": subprocess.DEVNULL}
    output[0] = " M scripts/p17d_synthetic.py\n"
    assert not ps.repo_tree_clean()


@pytest.mark.parametrize("error", [
    ps.Refuse("refusing: shadowlab.py sha256 x != pinned y"),
    SystemExit("rubric md5 mismatch"),
])
def test_score_consumes_use_and_reports_label_pin_failure(world, ollama, monkeypatch, error):
    full_run(world, monkeypatch)

    def fail(_cfg):
        raise error

    monkeypatch.setattr(ps, "load_label_mods", fail)
    assert ps.cmd_score(world.cfg) == ps.EXIT_OK
    result = json.loads(world.cfg.result.read_text())
    assert result["verdict"] == ps.NOT_INFORMATIVE and "pin" in result["verdict_reason"]
    assert result["pin_failures"] == [
        error.msg if isinstance(error, ps.Refuse) else type(error).__name__ + ": " + str(error)
    ]
    assert any(x.startswith("started ") for x in world.use())


def test_attach_pin_is_checked_before_import_execution(tmp_path, capsys):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("p17d_synthetic.py", "check_prereg.py"):
        shutil.copy(ROOT / "scripts" / name, scripts / name)
    marker = tmp_path / "executed"
    raw = (ROOT / "scripts" / "p17d_attach.py").read_bytes()
    (scripts / "p17d_attach.py").write_bytes(raw + f'\nopen({str(marker)!r}, "w").write("ran")\n'.encode())
    name = "_p17d_tampered_import"
    spec = importlib.util.spec_from_file_location(name, scripts / "p17d_synthetic.py")
    mod = importlib.util.module_from_spec(spec)
    previous = sys.modules.get("_p17d_attach_helpers")
    sys.modules[name] = mod
    try:
        with pytest.raises(SystemExit) as ei:
            spec.loader.exec_module(mod)
        assert ei.value.code == 2
    finally:
        sys.modules.pop(name, None)
        if previous is None:
            sys.modules.pop("_p17d_attach_helpers", None)
        else:
            sys.modules["_p17d_attach_helpers"] = previous
    assert not marker.exists()
    assert "p17d_attach.py sha256" in capsys.readouterr().err


def test_score_uses_the_snapshots_hashed_before_started(world, ollama, monkeypatch):
    full_run(world, monkeypatch)
    labels_sha, shadow_sha = sha(world.cfg.labels), sha(world.cfg.shadow)
    real = ps.append_line

    def append(path, line):
        real(path, line)
        if path == world.cfg.use_log and line.startswith("started "):
            world.cfg.labels.write_bytes(b"")
            world.cfg.shadow.write_bytes(b"garbage\n")

    monkeypatch.setattr(ps, "append_line", append)
    assert ps.cmd_score(world.cfg) == ps.EXIT_OK
    result = json.loads(world.cfg.result.read_text())
    assert result["n_labeled"] == 120
    started = next(x for x in world.use() if x.startswith("started "))
    assert f"labels_sha256={labels_sha}" in started and f"shadow_sha256={shadow_sha}" in started
    measure = json.loads(world.cfg.measurement.read_text())
    assert sum(r["truth"] is not None for r in measure["items"]) == 120


def test_before_freeze_count_matches_prereg_checker(tmp_path):
    frozen = ps.check_prereg.check(ps.PREREG)["frozen_utc"]
    cutoff = ps.parse_utc(frozen)
    records = [{"kind": "classifier_shadow", "ts": ts} for ts in
               (cutoff - 1, cutoff, cutoff + 1, "2026-10-15T16:32:57Z", frozen, "bad", None, True)]
    records.append({"kind": "other", "ts": cutoff - 1})
    raw = ("\n".join(json.dumps(r) for r in records) + "\nnot json\n[]\n").encode()
    log = tmp_path / "shadow.jsonl"
    log.write_bytes(raw)
    assert ps.before_freeze_count(ps.rows_from_bytes(raw), frozen) == 2
    assert ps.before_freeze_count(ps.rows_from_bytes(raw), frozen) == ps.check_prereg.check(ps.PREREG, log)[
        "before_freeze"
    ]


def test_spend_guard_allows_exactly_ten_dollars(tmp_path):
    cfg = ps.Cfg(run=tmp_path)
    ps.append_json(cfg.ledger, {"kind": "call", "total_cost_usd": 0.5})
    for _ in range(71):
        ps.append_json(cfg.ledger, {"kind": "call", "total_cost_usd": 0.125})
    assert ps.spent(cfg) == (9.375, 0.5)
    assert ps.spend_guard(cfg) == 0.625
    ps.append_json(cfg.ledger, {"kind": "call", "total_cost_usd": 0.125})
    with pytest.raises(ps.SpendCap):
        ps.spend_guard(cfg)


@pytest.mark.parametrize("largest,feasible", [(55, True), (56, False)])
def test_feasibility_share_boundary_without_admission(monkeypatch, largest, feasible):
    items = [make_item("shared" if i < largest else f"session-{i}", i) for i in range(100)]
    calls = []

    def walk(*args):
        calls.append(args)
        return items if len(calls) == 1 else []

    monkeypatch.setattr(ps, "walk", walk)
    selected = ps.select(items)
    assert selected["feasible"] is feasible and selected["admitted"] is False
    assert len(calls) == 1


def test_select_requires_strictly_after_frozen_utc(world):
    world.cfg.now = lambda: ps.parse_utc("2026-10-15T16:32:58Z")
    with refuses("frozen_utc"):
        ps.cmd_select(world.cfg)
    world.cfg.now = lambda: ps.parse_utc("2026-10-16T00:00:00Z")
    with refuses("AMEND1 frozen_utc"):
        ps.cmd_select(world.cfg)
    world.cfg.now = lambda: ps.parse_utc("2026-10-16T00:00:01Z")
    do_select(world)


def test_score_and_measurement_disclose_d110(world, ollama, monkeypatch):
    full_run(world, monkeypatch)
    assert ps.cmd_score(world.cfg) == ps.EXIT_OK
    for path in (world.cfg.result, world.cfg.measurement):
        assert any("D-110" in s for s in json.loads(path.read_text())["disclosures"])


def test_score_records_p17c_gate(world, ollama, monkeypatch):
    full_run(world, monkeypatch)
    raw = world.cfg.p1_gates.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    gap = json.loads(raw)["test_gates_2026_10_13"]["P1.7-c"]["gap"]
    assert ps.cmd_score(world.cfg) == ps.EXIT_OK
    result = json.loads(world.cfg.result.read_text())
    assert result["p17c_passed"] is True
    assert not result["verdict_reason"].startswith("procedural: P1.7-c")
    expected = {"path": str(world.cfg.p1_gates), "sha256": digest, "verdict": "PASS", "gap": gap}
    for path in (world.cfg.result, world.cfg.measurement):
        doc = json.loads(path.read_text())
        assert doc["p17c_gate"] == expected
        assert any(s.startswith("D-111a") and digest in s and gap in s for s in doc["disclosures"])
        assert any("D-110" in s for s in doc["disclosures"])


@pytest.mark.parametrize("raw", [
    None, "directory", b"{not json", b'{"test_gates_2026_10_13": {}}', b"[]",
    b'{"test_gates_2026_10_13": {"P1.7-c": {"gap": "x"}}}',
])
def test_score_refuses_invalid_p17c_gate_before_started(world, ollama, monkeypatch, raw):
    full_run(world, monkeypatch)
    if raw is None or raw == "directory":
        world.cfg.p1_gates.unlink()
        if raw == "directory":
            world.cfg.p1_gates.mkdir()
    else:
        world.set_gate(raw)
    with refuses("D-111a"):
        ps.cmd_score(world.cfg)
    assert not any(line.startswith("started ") for line in world.use())
    assert not world.cfg.result.exists() and not world.cfg.measurement.exists()


def test_score_discloses_absent_p17c_gap(world, ollama, monkeypatch):
    full_run(world, monkeypatch)
    world.set_gate(b'{"test_gates_2026_10_13": {"P1.7-c": {"verdict": "PASS"}}}')
    assert ps.cmd_score(world.cfg) == ps.EXIT_OK
    result = json.loads(world.cfg.result.read_text())
    assert result["p17c_gate"]["gap"] is None
    assert any(s.startswith("D-111a") and "the record has no gap field" in s for s in result["disclosures"])


def test_score_rejects_removed_p17c_flag(world, capsys):
    assert ps.main(["score", "--run-dir", str(world.run), "--p17c", "passed"]) == ps.EXIT_REFUSED
    assert "D-111a" in capsys.readouterr().err
    assert not any(line.startswith("started ") for line in world.use())


def test_p17c_gate_defaults_to_plan_path(tmp_path):
    assert ps.P1_GATES == ps.PP / "gates" / "v16" / "P1.json"
    assert ps.Cfg(run=tmp_path).p1_gates == ps.P1_GATES
    assert ".llm-router" not in str(ps.P1_GATES)


def test_spend_estimates_do_not_compound(tmp_path):
    cfg = ps.Cfg(run=tmp_path)
    for row in [
        {"kind": "call", "total_cost_usd": 0.2, "cost_basis": "reported"},
        {"kind": "call", "total_cost_usd": 0.25, "cost_basis": "estimate"},
        {"kind": "call", "total_cost_usd": 0.0, "cost_basis": "not_started"},
        {"kind": "call_pending", "call_id": "x", "est_usd": 0.9},
    ]:
        ps.append_json(cfg.ledger, row)
    total, top = ps.spent(cfg)
    assert total == pytest.approx(1.35)
    assert top == 0.2
    assert ps.spend_guard(cfg) == pytest.approx(0.25)
    cfg.ledger.unlink()
    for _ in range(2):
        ps.append_json(cfg.ledger, {"kind": "call", "total_cost_usd": 0.30, "cost_basis": "estimate"})
    assert ps.spend_guard(cfg) == 0.30


@pytest.mark.parametrize("cool_until", [1010.0, 1000.0, 999.0])
def test_wait_cooldown_handles_early_wakeups(tmp_path, monkeypatch, cool_until):
    from llm_router import local_classifier as lc

    clock = [1000.0]
    recorded = []
    monkeypatch.setattr(lc, "_now", lambda: clock[0])
    monkeypatch.setattr(lc, "_cool_until", cool_until)

    async def asleep(seconds):
        recorded.append(seconds)
        clock[0] += max(seconds / 2, 0.001)

    cfg = ps.Cfg(run=tmp_path, asleep=asleep)
    asyncio.run(ps.wait_cooldown(cfg))
    if cool_until > 1000.0:
        assert len(recorded) > 1
        assert clock[0] >= 1010.0
    else:
        assert recorded == []


def test_terminal_records_d110_disclosure(tmp_path):
    cfg = ps.Cfg(run=tmp_path)
    ps.terminal(cfg, "select", "test reason")
    res = json.loads(cfg.result.read_text())
    assert res["verdict"] == ps.NOT_INFORMATIVE
    assert any("D-110" in s for s in res["disclosures"])
    assert any(line.startswith("terminal ") for line in ps.use_lines(cfg))


@pytest.mark.parametrize("command", ["select", "shadow", "label", "score"])
@pytest.mark.parametrize("damage,error", [
    ("bytes", "AMEND1 sha256 differs"),
    ("future", "AMEND1 frozen_utc"),
    ("missing", "AMEND1 freeze record unreadable"),
])
def test_amend1_pin_guards_all_commands(world, capsys, command, damage, error):
    record = world.cfg.amend1.with_suffix(".sha256")
    if damage == "bytes":
        world.cfg.amend1.write_bytes(world.cfg.amend1.read_bytes() + b"\n")
    elif damage == "future":
        record.write_text(f"{sha(world.cfg.amend1)}  AMEND1.md\nfrozen_utc 2099-01-01T00:00:00Z\n")
    else:
        record.unlink()
    with refuses(error):
        getattr(ps, "cmd_" + command)(world.cfg)
    assert ps.cmd_status(world.cfg) == ps.EXIT_OK
    capsys.readouterr()
    assert not world.cfg.selection.exists() and not world.cfg.shadow.exists()


def test_amend1_default_path(tmp_path):
    assert ps.AMEND1 == ROOT / "docs" / "prereg" / "PREREG-P17D-synthetic-AMEND1.md"
    assert ps.AMEND1.is_file()
    assert ps.Cfg(run=tmp_path).amend1 == ps.AMEND1


@pytest.mark.parametrize("order,accepted", [
    (["old", "new"], True), (["old"], False), (["new", "old"], False),
    (["started", "new"], False), (["label", "new"], False),
    (["malformed", "new"], False),
])
def test_governing_harness_line_order(world, ollama, order, accepted):
    do_select(world)
    new_head = "a" * 40
    world.cfg.head_sha = lambda: new_head
    lines = {"old": "harness 2026-10-15T17:00:00Z sha=7e20664d" + "0" * 32,
             "new": f"harness 2026-10-15T18:00:00Z sha={new_head}",
             "started": "started 2026-10-15T17:00:00Z",
             "label": "label done 2026-10-15T17:00:00Z",
             "malformed": "harness invalid"}
    for name in order:
        ps.append_line(world.cfg.use_log, lines[name])
    assert ps.harness_line_ok(world.cfg) is accepted
    assert ps.harness_lines(world.cfg) == [lines[name] for name in order
                                          if lines[name].startswith("harness")]
    if accepted:
        assert ps.governing_harness_line(world.cfg) == lines["new"]
        assert ps.cmd_shadow(world.cfg) == ps.EXIT_OK
    else:
        if any(name in order for name in ("started", "label", "malformed")):
            assert ps.governing_harness_line(world.cfg) is None
        with refuses("harness"):
            ps.cmd_shadow(world.cfg)
        assert not world.cfg.shadow.exists() and not world.cfg.shadow_meta.exists()


def test_harness_line_cannot_postdate_first_shadow(world, ollama):
    do_select(world)
    do_shadow(world)
    ps.append_line(world.cfg.use_log, f"harness 2099-01-01T00:00:00Z sha={HEAD}")
    with refuses("after the first shadow row"):
        ps.check_code_identity(world.cfg)


def test_governing_harness_line_cannot_change_after_preflight(world, ollama):
    do_select(world)
    do_shadow(world)
    new_head = "b" * 40
    ps.append_line(world.cfg.use_log, f"harness 2026-10-15T16:00:00Z sha={new_head}")
    world.cfg.head_sha = lambda: new_head
    with refuses("differs from the one shadow recorded"):
        ps.check_code_identity(world.cfg)


def test_first_shadow_ts_excludes_booleans_and_strings(world):
    assert ps.first_shadow_ts(world.cfg) is None
    for ts in (True, "1", None, 9, 4.5):
        ps.append_json(world.cfg.shadow, {"ts": ts})
    assert ps.first_shadow_ts(world.cfg) == 4.5


@pytest.mark.parametrize("raw", [None, "bad json", "{}", '{"preflight":[]}', "[]"])
def test_recorded_preflight_absent_or_invalid(world, raw):
    if raw is not None:
        world.cfg.shadow_meta.write_text(raw)
    assert ps.recorded_preflight(world.cfg) is None


def test_shadow_rederives_selection_before_writing(world, ollama):
    do_select(world)
    world.harness_line()
    sel = json.loads(world.cfg.selection.read_text())
    dropped = sel["f_ids"].pop()
    sel["ids"].remove(dropped)
    sel["n"] -= 1
    world.cfg.selection.chmod(0o600)
    world.cfg.selection.write_text(json.dumps(sel))
    with refuses("re-derived"):
        ps.cmd_shadow(world.cfg)
    assert not world.cfg.shadow.exists() and not world.cfg.shadow_meta.exists()


def test_score_checks_selection_bytes_before_started(world, ollama, monkeypatch):
    full_run(world, monkeypatch)
    world.cfg.selection.chmod(0o600)
    world.cfg.selection.write_bytes(world.cfg.selection.read_bytes() + b"\n \n")
    with refuses("preflight record"):
        ps.cmd_score(world.cfg)
    assert not any(line.startswith("started ") for line in world.use())
    assert not world.cfg.result.exists() and not world.cfg.measurement.exists()


def test_score_and_measurement_disclose_amend1(world, ollama, monkeypatch):
    full_run(world, monkeypatch)
    lines = ps.harness_lines(world.cfg)
    assert ps.cmd_score(world.cfg) == ps.EXIT_OK
    for path in (world.cfg.result, world.cfg.measurement):
        doc = json.loads(path.read_text())
        assert doc["amend1_sha256"] == sha(world.cfg.amend1)
        assert doc["amend1_frozen_utc"] == "2026-10-16T00:00:00Z"
        assert any(text.startswith("AMEND1 (D-117)") for text in doc["disclosures"])
        assert ps.AMEND1_DISCLOSURE in doc["disclosures"]
    result = json.loads(world.cfg.result.read_text())
    assert result["harness_lines"] == lines
    assert result["selection_sha256"] == sha(world.cfg.selection)
    assert json.loads(world.cfg.measurement.read_text())["amendments"] == [
        "docs/prereg/PREREG-P17D-synthetic-AMEND1.md"]


@pytest.mark.parametrize("missing", [False, True])
def test_terminal_discloses_amend1_best_effort(world, missing):
    digest = sha(world.cfg.amend1)
    if missing:
        world.cfg.amend1.with_suffix(".sha256").unlink()
    ps.terminal(world.cfg, "select", "feasibility")
    result = json.loads(world.cfg.result.read_text())
    assert ps.AMEND1_DISCLOSURE in result["disclosures"]
    assert result["amend1_sha256"] == (None if missing else digest)
