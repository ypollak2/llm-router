"""P2.0-T harness (D-198..D-200). Synthetic fixtures only; no model, no real data."""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from llm_router.eligibility import grade
from llm_router.resolver.eligibility_store import record_competence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _load():
    spec = importlib.util.spec_from_file_location("_test_p20t_harness",
                                                  ROOT / "scripts/p20t_requal.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


h = _load()
base = h.base
NOW = 1_900_000_000.0
SIX = ["Read", "Grep", "Glob", "Bash", "Edit", "Write"]


ORIG_RESERVE = h.judge_reserve
REAL_RUN = h.subprocess.run  # captured before the autouse fixture forbids subprocess


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "judge_reserve", lambda cfg, c, items: h.Fraction(0))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / "state"))
    monkeypatch.delenv("P20T_REQUAL_DIR", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("real external call")

    monkeypatch.setattr(h.subprocess, "run", forbidden)
    monkeypatch.setattr(base, "urlopen", forbidden)


def reply(ident, path=None):
    return json.dumps({"tool": "Read", "input": {"file_path": path or f"/f/{ident}.py"}})


def make_item(i, stratum=None, session=None, tier="unlabelled", history=True):
    ident = f"tech-{i:03}"
    messages = [{"role": "user", "content": "ask"}]
    if history:
        messages += [{"role": "assistant",
                      "content": f'[tool_use Read] {{"file_path": "/h/{ident}.py"}}'},
                     {"role": "user", "content": "[tool_result] x"}]
    return {
        "id": ident, "set": "tech_op", "stratum": stratum or ("org" if i < 30 else "ho"),
        "source": "real", "session": session or f"s{i % 8}", "tier": tier,
        "task_type": "tech_op", "request": f"ID:{ident} please look", "messages": messages,
        "tools": [{"name": n, "description": "d"} for n in SIX],
        "organic_action": {"tool": "Read", "target": f"/f/{ident}.py"},
        "served_model": "m", "row_stamp": "organic" if i < 30 else "headless",
        "from_subagent_file": i % 10 == 0,
    }


class Ollama:
    def __init__(self, bad=()):
        self.calls, self.unloaded, self.resident, self.bad = [], [], [], set(bad)
        self.ids = {h.LOCAL: {"b3f4c17c6811"}}

    def list_ids(self):
        return self.ids

    def ps(self):
        return self.resident

    size = 0

    def resident_bytes(self, model):
        return self.size if model in self.resident else 0

    def chat(self, model, messages, *, timeout, options, think):
        assert think is False
        self.calls.append(messages)
        if messages[0]["content"] == "Reply OK.":
            return {"text": "OK", "error": None}
        ident = re.search(r"ID:(\S+)", base.flatten(messages))[1]
        return {"text": reply(ident, "/f/wrong.py" if ident in self.bad else None), "error": None}

    def unload(self, model):
        self.unloaded.append(model)


class Claude:
    def __init__(self, cost=0.001):
        self.calls, self.cost = [], cost

    def __call__(self, model, prompt, *, timeout):
        self.calls.append((model, prompt))
        ident = re.search(r"ID:(\S+)", prompt)[1]
        if model == "claude-opus-5-5":
            values = {}
            for letter, text in re.findall(
                    r"Answer ([A-Z]):\n(.*?)(?=\n\nAnswer [A-Z]:\n|\Z)", prompt, re.S):
                values[letter] = "acceptable" if f"/f/{ident}.py" in text else "unacceptable"
            return {"text": json.dumps(values), "error": None, "cost_usd": self.cost,
                    "served_model": model}
        return {"text": reply(ident), "error": None, "cost_usd": self.cost, "served_model": model}


class Machine:
    def __init__(self):
        self.free, self.load = 50.0, 1.0

    def free_mem_pct(self):
        return self.free

    def load1(self):
        return self.load


def constants(**fills):
    c = dict(h.CONSTANTS_T)
    c.update(REPO_SHA="a" * 40, SUBAGENT_BRANCH="B", N_REALISED_TECH_OP_ROWSTAMP_ORG="0",
             WINDOW_UNTIL="2026-10-17T00:00:00Z", PROXY_CALLS_COPY_SHA256="b" * 64,
             BUILD_MANIFEST_SHA256="c" * 64, POOL_TECH_OP_SHA256="d" * 64,
             POOL_COUNT_TECH_OP_ORG="0", POOL_COUNT_TECH_OP_HO="0", N_REALISED_TECH_OP="0",
             N_REALISED_TECH_OP_ORG="0", N_REALISED_SESSIONS="0")
    c.update(fills)
    return c


def prereg_text(c, name="PREREG-P20T-techop.md"):
    return ("# P20T\n\n```text P20T-CONSTANTS\n" + "".join(f"{k}={v}\n" for k, v in c.items())
            + "```\n")


def sidecar(path, date):
    path.with_suffix(".sha256").write_text(
        f"{base.sha256_file(path)}  {path.name}\nfrozen_utc {date}\n")


@pytest.fixture
def factory(tmp_path):
    def make(label="run", n=130, items=None, bad=(), cost=0.001, **extra):
        run_dir = tmp_path / label
        run_dir.mkdir()
        pool = items if items is not None else [make_item(i) for i in range(n)]
        base.atomic(run_dir / "pools/tech_op.jsonl", "".join(json.dumps(r) + "\n" for r in pool))
        base_c = {**h.STATIC}
        sel = h.select_tech_op(pool, base_c)
        manifest = {"sources": {"census_py": h.CONSTANTS_T["CENSUS_PY_SHA256"],
                                "proxy_calls_copy": "b" * 64},
                    "since": h.CONSTANTS_T["WINDOW_SINCE"], "until": "2026-10-17T00:00:00Z",
                    "counts": {"excluded": {"subagent_step": 3}}}
        base.write_json(run_dir / "pools/build_manifest.json", manifest)
        c = constants(**{**dict(
            POOL_TECH_OP_SHA256=base.sha256_file(run_dir / "pools/tech_op.jsonl"),
            BUILD_MANIFEST_SHA256=base.sha256_file(run_dir / "pools/build_manifest.json"),
            POOL_COUNT_TECH_OP_ORG=str(sum(r["stratum"] == "org" for r in pool)),
            POOL_COUNT_TECH_OP_HO=str(sum(r["stratum"] == "ho" for r in pool)),
            N_REALISED_TECH_OP=str(sel["n"]), N_REALISED_TECH_OP_ORG=str(sel["org_n"]),
            N_REALISED_TECH_OP_ROWSTAMP_ORG=str(sel["rowstamp_org"]),
            N_REALISED_SESSIONS=str(sel["sessions"])), **extra})
        prereg = run_dir / "PREREG-P20T-techop.md"
        prereg.write_text(prereg_text(c))
        sidecar(prereg, "2027-01-01T00:00:00Z")
        amend = run_dir / "AMEND2.md"
        amend.write_text("# AMEND2\n")
        sidecar(amend, "2026-12-01T00:00:00Z")
        cfg = h.CfgT(
            run_dir, prereg=prereg, p20_amend2=amend,
            lock_path=run_dir / "ollama.lock", now=lambda: NOW,
            ps_fn=lambda: ["python other.py"], sleep=lambda _: None, ollama=Ollama(bad),
            claude=Claude(cost), machine=Machine(),
            quota=lambda: {"session_pct": 0, "weekly_pct": 0, "age_s": 0},
            pack_fn=lambda item, name, window: [{"role": "user", "content": item["request"]}],
            git_sha=lambda: "deadbeef", gate=lambda constants: None,
            amend1_t=run_dir / "AMEND1.md", mem_total_fn=lambda: 48 * 2**30)
        return cfg, {**c, "LOCAL_MODELS": h.LOCAL}, pool
    return make


def call(cfg, *argv):
    return h.main(list(argv), cfg=cfg)


def full_run(factory, label="full", **kw):
    cfg, c, pool = factory(label, **kw)
    assert call(cfg, "select") == 0
    assert call(cfg, "run", "--set", "tech_op") == 0
    assert call(cfg, "judge", "--set", "tech_op") == 0
    return cfg, c, pool


# ---- H4: selection ---------------------------------------------------------------------

def test_no_tier_selection_and_org_first():
    pool = [make_item(i, tier=("complex" if i % 3 == 0 else "unlabelled" if i % 3 else "simple"))
            for i in range(130)]
    out = h.select_tech_op(pool, h.STATIC)
    assert out["n"] == 120 and out["excluded"].get("session_cap", 0) >= 0
    assert "no_tier" not in out["excluded"] and "tier_complex" not in out["excluded"]
    chosen = set(out["ids"])
    assert any(r["tier"] == "complex" and r["id"] in chosen for r in pool)
    assert any(r["tier"] == "unlabelled" and r["id"] in chosen for r in pool)
    assert out["selected_strata"]["org"] > 0 and out["selected_strata"]["ho"] > 0
    assert out["selected_strata"] == {"org": 30, "ho": 90}  # D-206: all org, ho fills the rest


def test_org_first_selects_every_org_row_before_any_ho():
    pool = [make_item(i, stratum="org" if i < 32 else "ho", session=f"s{i % 16}")
            for i in range(32 + 679)]
    ids = h.select_tech_op(pool, h.STATIC)["ids"]
    stratum = {r["id"]: r["stratum"] for r in pool}
    order = [stratum[i] for i in ids]
    assert order[:32] == ["org"] * 32 and set(order[32:]) == {"ho"} and len(ids) == 120
    out = h.select_tech_op(pool, h.STATIC)
    assert out["org_n"] == 32 and out["freeze"]["ready"] is True


def test_org_first_still_obeys_the_session_cap():
    pool = [make_item(i, stratum="org" if i < 40 else "ho",
                      session="big" if i < 30 else f"s{i % 8}") for i in range(300)]
    out = h.select_tech_op(pool, h.STATIC)
    chosen = {r["id"]: r for r in pool}
    in_big = sum(chosen[i]["session"] == "big" for i in out["ids"])
    assert in_big == 24 and out["excluded"]["session_cap"] >= 6  # floor(0.20 x 120)
    assert out["selected_strata"]["org"] == 10 + 24 and out["n"] == 120


def test_window_filter_boundary_is_strictly_greater_and_counted(monkeypatch):
    monkeypatch.setattr(h.local_models, "num_ctx", lambda model=None: 1000)  # limit 900.0
    def sized(i, tokens, stratum):
        row = make_item(i, stratum=stratum, history=False)
        row["messages"][0]["content"] = "x" * (4 * tokens)
        return row
    pool = [sized(0, 900, "org"), sized(1, 901, "org"), sized(2, 900, "ho"), sized(3, 901, "ho"),
            sized(4, 5000, "ho")]
    assert [h.prompt_tokens_est(r) for r in pool] == [900, 901, 900, 901, 5000]
    out = h.select_tech_op(pool, h.STATIC)
    assert set(out["ids"]) == {"tech-000", "tech-002"}
    assert out["excluded"]["over_gate_window"] == 3
    assert out["over_gate_window_by_stratum"] == {"org": 1, "ho": 2}
    assert out["gate_window_limit"] == 900.0


def test_window_limit_follows_local_model_num_ctx():
    out = h.select_tech_op([make_item(0)], h.STATIC)
    assert out["gate_window_limit"] == 0.9 * h.local_models.num_ctx(h.LOCAL)


def test_unknown_stratum_and_missing_field_dropped_and_duplicates_refuse():
    pool = [make_item(i) for i in range(70)]
    pool[0]["stratum"] = "rx"
    del pool[1]["organic_action"]
    out = h.select_tech_op(pool, h.STATIC)
    assert out["excluded"]["unknown_stratum"] == 1 and out["excluded"]["missing_field"] == 1
    with pytest.raises(h.Refuse):
        h.select_tech_op([make_item(1), make_item(1)], h.STATIC)


def test_sessions_and_n_floor_status_no_freeze():
    few = [make_item(i, session=f"s{i % 3}") for i in range(130)]
    out = h.select_tech_op(few, h.STATIC)
    assert "sessions" in out["freeze"]["reasons"] and not out["freeze"]["ready"]
    small = h.select_tech_op([make_item(i) for i in range(40)], h.STATIC)
    assert "n_min" in small["freeze"]["reasons"]
    ok = h.select_tech_op([make_item(i) for i in range(130)], h.STATIC)
    assert ok["freeze"] == {"ready": True, "reasons": []}
    assert ok["sessions"] >= 5 and ok["equal_rate_threshold_k"] == 97


def test_org_floor_blocks_freeze_ready():
    pool = [make_item(i, stratum="org" if i < 10 else "ho") for i in range(130)]
    out = h.select_tech_op(pool, h.STATIC)
    assert "org_n" in out["freeze"]["reasons"] and out["org_ok"] is False


def test_dry_run_prints_median_and_p90_prompt_tokens_est(factory, capsys):
    pool = [make_item(i) for i in range(100)]
    for i, row in enumerate(pool):
        row["messages"][0]["content"] = "x" * (400 * (i + 1))  # 100, 200, ... tokens (+ history)
    cfg, c, _ = factory("tok", items=pool)
    assert call(cfg, "select", "--dry-run") == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    est = out["sets"]["tech_op"]["prompt_tokens_est"]
    chosen = h.select_tech_op(pool, h.STATIC)["ids"]
    vals = sorted(h.prompt_tokens_est(r) for r in pool if r["id"] in set(chosen))
    assert est["median"] == h.median(vals) and est["p90"] == vals[-(-9 * len(vals) // 10) - 1]
    assert est["p90"] > est["median"] > 0


def test_dry_run_prints_counts_only_and_status_no_freeze(factory, capsys):
    cfg, c, pool = factory("dry", n=40)
    assert call(cfg, "select", "--dry-run") == 0
    out = json.loads(capsys.readouterr().out)
    sel = out["sets"]["tech_op"]
    assert sel["n"] == 40 and "ids" not in sel and sel["freeze"]["ready"] is False
    assert out["builder_counts"]["excluded"]["subagent_step"] == 3
    for key in ("stratum_counts", "selected_strata", "org_n", "rowstamp_org", "sessions",
                "equal_rate_threshold_k"):  # H12: strata, org both ways, sessions, threshold
        assert key in sel
    assert "ask" not in json.dumps(out) and "ID:" not in json.dumps(out)
    assert call(cfg, "status") == 0
    st = json.loads(capsys.readouterr().out)
    assert st["freeze_status"] == "no_freeze" and "n_min" in st["freeze_reasons"]


def test_select_refuses_when_realised_n_differs(factory):
    cfg, c, pool = factory("mismatch", N_REALISED_TECH_OP="119")
    assert call(cfg, "select") == 2
    assert not (cfg.run_dir / "selection.json").exists()


def test_select_writes_items_and_terminal_below_floor(factory):
    cfg, c, pool = factory("floor", n=40)
    assert call(cfg, "select") == 0
    assert (cfg.run_dir / "items/tech_op.jsonl").exists()
    assert any(r[0] == "terminal" and r[2] == "tech_op" for r in base.uses(cfg))


# ---- H9: freeze ------------------------------------------------------------------------

def test_frozen_accepts_p20t_prereg_name_and_constants(factory):
    cfg, c, _ = factory("frz")
    out = h.frozen(cfg)
    assert cfg.prereg.name == "PREREG-P20T-techop.md"
    assert out["constants"]["LOCAL_MODELS"] == h.LOCAL and out["constants"]["SPEND_CAP_USD"] == "40.00"


def test_frozen_refusals(factory):
    cfg, c, _ = factory("r1")
    text = cfg.prereg.read_text()
    for old, new in (("REPO_SHA=" + "a" * 40, "REPO_SHA=FILL-AT-FREEZE"),
                     ("SELECT_SEED=20261017", "SELECT_SEED=1"),
                     ("SUBAGENT_BRANCH=B", "SUBAGENT_BRANCH=C"),
                     ("NIGHT_WINDOW", "x")):
        cfg.prereg.write_text(text.replace(old, new) if old in text
                              else text.replace("```\n", "NIGHT_WINDOW=1\n```\n", 2))
        sidecar(cfg.prereg, "2027-01-01T00:00:00Z")
        with pytest.raises(h.Refuse):
            h.frozen(cfg)
    cfg.prereg.write_text(text.replace("```\n", "EXTRA=1\n```\n", 2))
    sidecar(cfg.prereg, "2027-01-01T00:00:00Z")
    with pytest.raises(h.Refuse):
        h.frozen(cfg)


def test_frozen_sidecar_and_amend2_order(factory):
    cfg, c, _ = factory("r2")
    cfg.prereg.write_text(cfg.prereg.read_text() + "\n")  # bytes differ from the sidecar
    with pytest.raises(h.Refuse):
        h.frozen(cfg)
    cfg, c, _ = factory("r3")
    sidecar(cfg.p20_amend2, "2028-01-01T00:00:00Z")  # AMEND2 frozen after this prereg
    with pytest.raises(h.Refuse):
        h.frozen(cfg)
    cfg, c, _ = factory("r4")
    (cfg.run_dir / "PREREG.md").write_text("different")
    with pytest.raises(h.Refuse):
        h.frozen(cfg)


def test_prefreeze_modes_and_every_other_command_refuses(factory, tmp_path):
    cfg, c, _ = factory("pre")
    cfg.prereg.write_text("# draft\n")
    (cfg.prereg.with_suffix(".sha256")).unlink()
    assert call(cfg, "status") == 0 and call(cfg, "select", "--dry-run") == 0
    for argv in (("select",), ("run", "--set", "tech_op"), ("judge", "--set", "tech_op"),
                 ("score",)):
        assert call(cfg, *argv) == 2
    assert not (cfg.run_dir / "items").exists() and not (cfg.run_dir / "ledger.jsonl").exists()


# ---- protocol: validity, redundant ------------------------------------------------------

ITEM = make_item(1)


@pytest.mark.parametrize("text,ok", [
    (reply("x"), True),
    ('```json\n{"tool":"Read","input":{"file_path":"/a"}}\n```', True),
    ('{"final": "done"}', True),
    ('{"final": 3}', False),
    ('no json', False),
    ('{"tool":"Nope","input":{"file_path":"/a"}}', False),
    ('{"tool":"Read","input":{"limit":3}}', False),
    ('{"tool":"Read","input":"x"}', False),
    ('see {x} then {"tool":"Read","input":{"file_path":"/a"}}', False),
])
def test_validity_rules(text, ok):
    assert h.valid_reply(text, ITEM) is ok


def test_key_rule_only_for_six_builder_tools():
    item = make_item(1)
    item["tools"].append({"name": "Custom", "description": "d"})
    assert h.valid_reply('{"tool":"Custom","input":{"x":1}}', item)
    assert h.reply_key('{"tool":"Custom","input":{"x":1}}') is None


def test_redundant_rule_and_mutation_reset():
    item = make_item(1)
    same = json.dumps({"tool": "Read", "input": {"file_path": "/h/tech-001.py"}})
    assert h.is_redundant(same, item) and not h.usable(same, item)
    assert not h.is_redundant(reply("tech-001"), item)
    item["messages"] += [
        {"role": "assistant", "content": '[tool_use Edit] {"file_path": "/h/tech-001.py"}'}]
    assert not h.is_redundant(same, item)  # an Edit intervened: the earlier result may be stale
    bash = make_item(2)
    bash["messages"][1]["content"] = '[tool_use Bash] {"command": "ls   -la"}'
    assert h.is_redundant('{"tool":"Bash","input":{"command":"ls -la"}}', bash)
    bash["messages"].append({"role": "assistant",
                             "content": '[tool_use Bash] {"command": "rm -rf build"}'})
    assert not h.is_redundant('{"tool":"Bash","input":{"command":"ls -la"}}', bash)
    assert h.mutating("Bash", {"command": "some-unknown-tool --x"})  # unresolvable = mutating
    assert not h.mutating("Bash", {"command": "git status"})
    assert h.edit_shaped('{"tool":"Write","input":{"file_path":"/a"}}', item)


def test_judge_never_sees_invalid_or_redundant_replies(factory):
    cfg, c, pool = factory("gate")
    cfg.ollama.bad = set()
    assert call(cfg, "select") == 0
    items = base.jsonl(cfg.run_dir / "items/tech_op.jsonl")
    first = items[0]["id"]
    orig = cfg.ollama.chat

    def chat(model, messages, **kw):
        if messages[0]["content"] != "Reply OK." and f"ID:{first}" in base.flatten(messages):
            return {"text": "not json at all", "error": None}
        return orig(model, messages, **kw)

    cfg.ollama.chat = chat
    assert call(cfg, "run", "--set", "tech_op") == 0
    assert call(cfg, "judge", "--set", "tech_op") == 0
    prompts = [p for m, p in cfg.claude.calls if m == "claude-opus-5-5" and f"ID:{first}" in p]
    assert prompts and all("not json at all" not in p for p in prompts)
    row = next(r for r in base.jsonl(cfg.run_dir / "judgements/tech_op.jsonl")
               if r["item_id"] == first)
    assert h.LOCAL in row["not_shown_invalid"] and h.LOCAL not in row["verdicts"]


# ---- plants ----------------------------------------------------------------------------

def test_plants_four_kinds_placed_and_shown_to_judge(factory):
    cfg, c, pool = full_run(factory)
    rows = base.jsonl(cfg.run_dir / "judgements/tech_op.jsonl")
    kinds = {k for r in rows for k in r["plants"]}
    assert kinds == set(h.KINDS)
    for kind in h.KINDS:
        values = [r["plants"][kind] for r in rows if kind in r["plants"]]
        assert len(values) == 6 and not any(values)  # all judged unacceptable
    items = base.jsonl(cfg.run_dir / "items/tech_op.jsonl")
    plants, stats = h.plant_candidates(items, base.finals(cfg, "tech_op"), c)
    assert stats["plants_skipped"]["repeat_plant_precheck_redundant"] == 6  # F1 record


def test_repeat_plant_skipped_without_history_and_no_repeat_means_no_calibration(factory):
    pool = [make_item(i, history=False) for i in range(130)]
    cfg, c, _ = factory("nohist", items=pool)
    items = pool
    answers = {(i["id"], "sonnet"): {"ok": True, "text": reply(i["id"])} for i in items}
    plants, stats = h.plant_candidates(items, answers, c)
    assert stats["plants_skipped"]["repeat"] == 6
    assert not any("__repeat__" in v for v in plants.values())


# ---- run, quiet gate, lock, run block ---------------------------------------------------

def test_run_records_quiet_readings_and_checks_before_every_call(factory):
    cfg, c, pool = full_run(factory, "quiet")
    local_rows = [r for r in base.jsonl(cfg.run_dir / "answers/tech_op.jsonl") if r["arm"] == h.LOCAL]
    assert local_rows and all(len(r["quiet"]) == 3 for r in local_rows)
    assert cfg.ollama.unloaded == [h.LOCAL]  # this process called: one unload, at the end


def test_quiet_failure_parks_before_any_row(factory):
    cfg, c, pool = factory("loud")
    assert call(cfg, "select") == 0
    cfg.machine.load = 4.0
    assert call(cfg, "run", "--set", "tech_op") == 3
    assert not (cfg.run_dir / "answers/tech_op.jsonl").exists()
    cfg.machine.load = 1.0
    cfg.ollama.resident = ["foreign:latest"]
    assert call(cfg, "run", "--set", "tech_op") == 3
    cfg.ollama.resident = []
    cfg.machine.free = 39.9
    assert call(cfg, "run", "--set", "tech_op") == 3


def test_quiet_rechecked_mid_run_parks_after_current_item(factory):
    cfg, c, pool = factory("mid")
    assert call(cfg, "select") == 0
    seen = {"n": 0}

    def load1():
        seen["n"] += 1
        return 1.0 if seen["n"] <= 3 * 5 + 3 else 9.0  # warm-up + five items pass

    cfg.machine.load1 = load1
    assert call(cfg, "run", "--set", "tech_op") == 3
    rows = base.jsonl(cfg.run_dir / "answers/tech_op.jsonl")
    assert 1 <= len(rows) < 120 and all(r["final"] for r in rows)


def hold_lock(path):
    import fcntl
    import os
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def lock_is_held(path):
    import fcntl
    import os
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def test_no_time_of_day_run_block(factory):
    # D-204: the sibling P2.0 harness is frozen under AMEND3 and shares the lock, so the
    # D-199 23:15-07:00 block is gone from code and constants.
    assert not any(k.startswith("RUN_BLOCK") for k in h.CONSTANTS_T)
    assert not hasattr(h, "in_run_block") and not hasattr(h, "p20_unfinished")
    assert not hasattr(h.CfgT, "local_minute") and "local_minute" not in h.CfgT.__dataclass_fields__
    cfg, c, pool = factory("noblock")
    assert call(cfg, "select") == 0
    assert call(cfg, "run", "--set", "tech_op") == 0


def test_p20_harness_process_parks_before_any_call_and_does_not_unload(factory):
    cfg, c, pool = factory("proc")
    assert call(cfg, "select") == 0
    cfg.ps_fn = lambda: ["python scripts/p20_requal.py run --set qa"]
    assert call(cfg, "run", "--set", "tech_op") == 3
    assert not (cfg.run_dir / "answers/tech_op.jsonl").exists()
    assert cfg.ollama.unloaded == []  # BLOCKER 1: the model is P2.0's; this process never used it
    assert cfg.ollama.calls == []


def test_lock_held_before_any_call_parks_and_does_not_unload(factory):
    cfg, c, pool = factory("lockfirst")
    assert call(cfg, "select") == 0
    fd = hold_lock(cfg.lock_path)
    try:
        assert call(cfg, "run", "--set", "tech_op") == 3
    finally:
        import os
        os.close(fd)
    assert not (cfg.run_dir / "answers/tech_op.jsonl").exists()
    assert cfg.ollama.unloaded == [] and cfg.ollama.calls == []
    assert call(cfg, "run", "--set", "tech_op") == 0
    assert cfg.ollama.unloaded == [h.LOCAL]


def test_lock_taken_after_warmup_parks_per_attempt_and_end_unload_skipped(factory):
    # FIX 2: the lock is taken around EVERY local call, not once. The sibling grabs it after
    # the warm-up and the first item; the next attempt must park, and the unload (this process
    # did call) must be skipped silently because the lock is held.
    import os
    cfg, c, pool = factory("lockmid")
    assert call(cfg, "select") == 0
    held, packed = [], []

    def pack(item, name, window):
        packed.append(item["id"])
        if len(packed) == 2:  # the second item is being packed: warm-up and item 1 are done
            held.append(hold_lock(cfg.lock_path))
        return [{"role": "user", "content": item["request"]}]

    cfg.pack_fn = pack
    try:
        assert call(cfg, "run", "--set", "tech_op") == 3
    finally:
        for fd in held:
            os.close(fd)
    rows = base.jsonl(cfg.run_dir / "answers/tech_op.jsonl")
    assert len(held) == 1 and len(rows) == 1 and rows[0]["final"]
    assert cfg.ollama.unloaded == []  # held lock at the end: no unload


def test_quiet_is_read_inside_the_lock(factory):
    cfg, c, pool = factory("order")
    assert call(cfg, "select") == 0
    seen = []
    load = cfg.machine.load1
    cfg.machine.load1 = lambda: (seen.append(lock_is_held(cfg.lock_path)), load())[1]
    assert call(cfg, "run", "--set", "tech_op") == 0
    assert seen and all(seen)  # AMEND3 order: lock, then quiet, then the call


def test_ps_fails_closed(factory):
    cfg, c, pool = factory("ps")
    assert call(cfg, "select") == 0
    cfg.ps_fn = lambda: []  # ps returned nothing: cannot rule P2.0 out
    assert call(cfg, "run", "--set", "tech_op") == 3

    def boom():
        raise OSError("ps")

    cfg.ps_fn = boom
    assert call(cfg, "run", "--set", "tech_op") == 3
    assert not (cfg.run_dir / "answers/tech_op.jsonl").exists() and cfg.ollama.unloaded == []
    cfg.ps_fn = lambda: ["python other.py"]
    assert call(cfg, "run", "--set", "tech_op") == 0


def test_resume_continues_at_next_attempt_number(factory):
    # weak spot (d): a parked item resumes at the next attempt; no item gets > RETRIES+1 rows
    cfg, c, pool = factory("resume")
    assert call(cfg, "select") == 0
    first = json.loads((cfg.run_dir / "items/tech_op.jsonl").read_text().splitlines()[0])["id"]
    seed = {"item_id": first, "arm": h.LOCAL, "attempt": 1, "ok": False, "final": False,
            "text": "", "error": "empty"}
    base.append_row(cfg.run_dir / "answers/tech_op.jsonl", seed)
    assert call(cfg, "run", "--set", "tech_op") == 0
    mine = [r for r in base.jsonl(cfg.run_dir / "answers/tech_op.jsonl")
            if r["item_id"] == first and r["arm"] == h.LOCAL]
    assert [r["attempt"] for r in mine] == [1, 2] and mine[-1]["final"]
    assert h.over_attempts(cfg, c) == 0


def test_over_attempt_detector_and_run_report_fields(factory, capsys):
    cfg, c, pool = factory("report")
    assert call(cfg, "select") == 0
    assert call(cfg, "run", "--set", "tech_op") == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["items_over_retries_plus_1_attempt_rows"] == 0
    assert out["items_run_after_failed_recheck"] == 0
    # the checks find something when it is there (an empty set would pass everything)
    path = cfg.run_dir / "answers/tech_op.jsonl"
    row = base.jsonl(path)[0]
    for attempt in (7, 8):
        base.append_row(path, {**row, "attempt": attempt, "final": False})
    assert h.over_attempts(cfg, c) == 1
    base.append_row(path, {**row, "item_id": "x", "quiet": [{"free_pct": 50.0, "load1": 9.0}] * 3})
    base.append_row(path, {**row, "item_id": "y", "quiet": []})
    assert h.unchecked_local_rows(cfg, c) == 2


def test_identity_mismatch_and_foreign_arm_refuse(factory):
    cfg, c, pool = factory("ident")
    assert call(cfg, "select") == 0
    cfg.ollama.ids = {h.LOCAL: {"other"}}
    assert call(cfg, "run", "--set", "tech_op") == 2
    cfg.ollama.ids = {h.LOCAL: {"b3f4c17c6811"}}
    base.append_row(cfg.run_dir / "answers/tech_op.jsonl",
                    {"item_id": "tech-000", "arm": "qwen3.6:x", "final": True, "ok": True})
    assert call(cfg, "run", "--set", "tech_op") == 2
    assert call(cfg, "judge", "--set", "tech_op") == 2
    assert call(cfg, "score") == 2
    assert not any(r[0] == "started" for r in base.uses(cfg))


# ---- spend ------------------------------------------------------------------------------

def test_reserve_parks_before_first_sonnet_call(factory, monkeypatch):
    monkeypatch.setattr(h, "judge_reserve", ORIG_RESERVE)
    cfg, c, pool = factory("res")
    assert call(cfg, "select") == 0
    # 1.25 x 0.30 x (120 + 12) = 49.5 > 40.00: the literal N9b reserve cannot be afforded
    assert call(cfg, "run", "--set", "tech_op", "--arms", "sonnet") == 3
    assert not any(m != h.LOCAL for m, _ in cfg.claude.calls)


def test_haiku_skipped_on_budget_and_judge_treats_arm_as_complete(factory, monkeypatch):
    cfg, c, pool = factory("hk")
    assert call(cfg, "select") == 0
    assert call(cfg, "run", "--set", "tech_op", "--arms", h.LOCAL) == 0
    assert call(cfg, "run", "--set", "tech_op", "--arms", "sonnet") == 0
    # now the reserve is unaffordable for haiku only
    monkeypatch.setattr(h, "judge_reserve", lambda cfg, c, items: h.Fraction(100))
    assert call(cfg, "run", "--set", "tech_op", "--arms", "haiku") == 0
    assert h.active_arms(cfg, c) == [h.LOCAL, "sonnet"]
    assert any(r[0] == "skipped" and r[2] == "haiku" for r in base.uses(cfg))
    assert call(cfg, "judge", "--set", "tech_op") == 0
    assert call(cfg, "score") == 0
    result = json.loads((cfg.run_dir / "result.json").read_text())
    assert "haiku" not in result["sets"]["tech_op"]["lanes"]


# ---- score: lane, org check, secondary --------------------------------------------------

def test_score_eligible_lane_written_through_record_competence(factory, capsys):
    cfg, c, pool = full_run(factory, "elig")
    assert call(cfg, "score", "--measurement", str(cfg.run_dir / "m.json")) == 0
    out = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert out["verdict"] == "eligible" and out["lanes"][h.LOCAL]["org_check"] == "pass"
    data = json.loads((cfg.run_dir / "local_eligibility.json").read_text())
    lane = data[h.LOCAL]["tech_op"]["simple"]
    assert set(data[h.LOCAL]["tech_op"]) == {"simple"} and h.LOCAL in data and "haiku" not in data
    assert lane["basis"] == "all_technical_op_continuations" and lane["source"] == "p2.0-t"
    assert grade(lane) == lane["grade"] == "eligible" and lane["eligible"] is True
    assert set(lane["strata"]) == {"org", "ho"} and lane["n"] == 120
    # the gate's own reads
    assert lane["verdict"] == "eligible"
    result = json.loads((cfg.run_dir / "result.json").read_text())
    assert result["e_tech"]["model"] == h.LOCAL and result["p21_allowed"] is True
    started = [r for r in base.uses(cfg) if r[0] == "started"]
    assert len(started) == 1 and any(x.startswith("answers_sha256=") for x in started[0])


def test_score_is_single_use(factory):
    cfg, c, pool = full_run(factory, "once")
    assert call(cfg, "score") == 0
    assert call(cfg, "score") == 2


def org_bad_ids(pool, org_bad, ho_bad=0):
    org = [r["id"] for r in pool if r["stratum"] == "org"][:org_bad]
    ho = [r["id"] for r in pool if r["stratum"] == "ho"][:ho_bad]
    return org + ho


def test_organic_check_downgrades_when_org_disagrees(factory):
    pool = [make_item(i, stratum="org" if i < 22 else "ho") for i in range(130)]
    sel = h.select_tech_op(pool, h.STATIC)
    chosen = set(sel["ids"])
    org = [r for r in pool if r["stratum"] == "org" and r["id"] in chosen]
    bad, seen = [], set()
    for r in org:  # org-first selects all 22 org rows: three misses in three sessions, org gap 3/22 = 13.6 pts
        if r["session"] not in seen and len(bad) < 3:
            seen.add(r["session"])
            bad.append(r["id"])
    cfg, c, _ = factory("orgbad", items=pool, bad=bad)
    assert call(cfg, "select") == 0
    assert call(cfg, "run", "--set", "tech_op") == 0
    assert call(cfg, "judge", "--set", "tech_op") == 0
    assert call(cfg, "score") == 0
    lane = json.loads((cfg.run_dir / "result.json").read_text())["sets"]["tech_op"]["lanes"][h.LOCAL]
    assert lane["grade"] == "eligible" and lane["strata"]["org"]["gap_pts"] > 10
    assert lane["verdict"] == "inconclusive" and lane["eligible"] is False
    assert lane["org_check"] == "org_disagrees"


def test_ho_alone_never_makes_a_lane_eligible(factory):
    # 130 rows, only 10 organic: org n < 20 so even a perfect local arm cannot be eligible
    pool = [make_item(i, stratum="org" if i < 10 else "ho") for i in range(130)]
    cfg, c, _ = full_run(factory, "honly", items=pool)
    assert call(cfg, "score") == 0
    lane = json.loads((cfg.run_dir / "result.json").read_text())["sets"]["tech_op"]["lanes"][h.LOCAL]
    assert lane["grade"] == "eligible" and lane["org_check"] == "org_too_small"
    assert lane["verdict"] == "inconclusive" and lane["eligible"] is False
    data = json.loads((cfg.run_dir / "local_eligibility.json").read_text())
    assert data[h.LOCAL]["tech_op"]["simple"]["eligible"] is False


def test_few_sessions_is_not_informative(factory):
    pool = [make_item(i, session=f"s{i % 3}") for i in range(130)]
    sel = h.select_tech_op(pool, h.STATIC)
    assert sel["sessions"] == 3
    cfg, c, _ = factory("fs", items=pool)
    assert call(cfg, "select") == 0
    assert any(r[0] == "terminal" and r[3] == "sessions" for r in base.uses(cfg))
    assert call(cfg, "run", "--set", "tech_op") == 4
    assert call(cfg, "score") == 0
    result = json.loads((cfg.run_dir / "result.json").read_text())
    assert "tech_op" in result["not_informative"] and result["verdict"] == "not_informative"
    lane = result["sets"]["tech_op"]["lanes"][h.LOCAL]
    assert lane["n"] == 0 and lane["grade"] == "unknown"


def test_empty_pool_is_not_informative(factory):
    cfg, c, _ = factory("empty", items=[])
    assert call(cfg, "select", "--dry-run") == 0
    assert call(cfg, "status") == 0
    out = h.select_tech_op([], h.STATIC)
    assert out["n"] == 0 and not out["freeze"]["ready"]


def test_secondary_report_fields(factory):
    cfg, c, pool = full_run(factory, "sec")
    assert call(cfg, "score") == 0
    result = json.loads((cfg.run_dir / "result.json").read_text())
    lane = result["sets"]["tech_op"]["lanes"][h.LOCAL]
    sec = lane["secondary"]
    for stratum in ("org", "ho", "all"):
        block = sec[stratum]
        for key in ("concordance_organic", "redundant_repeat", "validity", "timeout", "edit_shaped",
                    "anchor_acceptable_when_concordant", "latency_s", "by_organic_tool",
                    "judge_rejects_organic"):
            assert key in block, (stratum, key)
    assert sec["all"]["concordance_organic"]["k"] == 120
    assert sec["all"]["validity"]["rate"] == 1.0 and sec["all"]["redundant_repeat"]["k"] == 0
    assert sec["all"]["latency_s"]["p95"] is not None
    assert "secondary_reference" in result["sets"]["tech_op"]
    assert lane["session_sensitivity"]["sessions"] == 8
    picked = set(h.select_tech_op(pool, h.STATIC)["ids"])
    assert lane["without_subagent_file"]["removed"] == sum(
        r["from_subagent_file"] for r in pool if r["id"] in picked) == 12
    assert lane["agreement"] == {"k": 120, "n": 120}
    assert result["sets"]["tech_op"]["equal_window_sample"]["n"] == 12
    assert lane["strata"]["org"]["n"] + lane["strata"]["ho"]["n"] == 120


def test_loso_waives_n_floor_and_flags_flip():
    items = [make_item(i, session=f"s{i % 2}") for i in range(20)]
    outcomes = {i["id"]: True for i in items}
    refs = {i["id"]: True for i in items}
    assert h.loso(items, outcomes, refs)["session_sensitive"] is False
    # candidate fails every item of session s1 only: leaving s1 out fixes it, leaving s0 out
    # leaves a failing set; the full set already fails, so nothing flips
    outcomes = {i["id"]: i["session"] == "s0" for i in items}
    assert h.loso(items, outcomes, refs)["full_passes"] is False
    # full passes (one miss in 90 vs 0) but dropping one session exposes a failing remainder
    many = [make_item(i, session="big" if i < 40 else f"s{i}") for i in range(60)]
    o = {i["id"]: True for i in many}
    r = {i["id"]: True for i in many}
    for i in many[:40]:
        r[i["id"]] = True
    flipped = h.loso(many, {**o, many[45]["id"]: False, many[46]["id"]: False,
                            many[47]["id"]: False}, r)
    assert flipped["sessions"] == 21 and "gap_min" in flipped


def test_session_sensitive_lane_is_downgraded(factory, monkeypatch):
    cfg, c, pool = full_run(factory, "sens")
    monkeypatch.setattr(h, "loso", lambda *a: {"sessions": 8, "gap_min": 0, "gap_max": 9,
                                               "full_passes": True, "session_sensitive": True})
    assert call(cfg, "score") == 0
    lane = json.loads((cfg.run_dir / "result.json").read_text())["sets"]["tech_op"]["lanes"][h.LOCAL]
    assert lane["grade"] == "eligible" and lane["verdict"] == "inconclusive"
    assert lane["org_check"] == "session_sensitive"


def test_install_merges_lane_only_after_score(factory):
    cfg, c, pool = full_run(factory, "inst")
    assert call(cfg, "install-eligibility", "--confirm") == 2  # not scored
    assert call(cfg, "score") == 0
    assert call(cfg, "install-eligibility") == 2
    record_competence(h.LOCAL, "qa", {"simple": {"grade": "unknown", "verdict": "not_informative",
                                                 "eligible": False, "n": 0}})
    assert call(cfg, "install-eligibility", "--confirm") == 0
    from llm_router.paths import state_path
    data = json.loads(state_path("local_eligibility.json").read_text())
    assert data[h.LOCAL]["tech_op"]["simple"]["basis"] == "all_technical_op_continuations"
    assert "qa" in data[h.LOCAL]


def test_calibration_failure_is_not_informative(factory):
    cfg, c, pool = full_run(factory, "calib")
    # a lenient judge: every plant accepted
    rows = base.jsonl(cfg.run_dir / "judgements/tech_op.jsonl")
    for r in rows:
        r["plants"] = {k: True for k in r["plants"]}
    base.atomic(cfg.run_dir / "judgements/tech_op.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
    assert call(cfg, "score") == 0
    result = json.loads((cfg.run_dir / "result.json").read_text())
    assert result["not_informative"]["tech_op"] == "calibration"


def test_score_counts_invalid_reply_unacceptable_even_if_judge_says_acceptable(factory):
    cfg, c, pool = full_run(factory, "val")
    answers = base.jsonl(cfg.run_dir / "answers/tech_op.jsonl")
    victims = [i["id"] for i in base.jsonl(cfg.run_dir / "items/tech_op.jsonl")][:3]
    for r in answers:
        if r["arm"] == h.LOCAL and r["item_id"] in victims:
            r["text"] = '{"tool":"Read","input":{}}'  # invalid for a builder tool
    base.atomic(cfg.run_dir / "answers/tech_op.jsonl", "".join(json.dumps(r) + "\n" for r in answers))
    assert call(cfg, "score") == 0
    lane = json.loads((cfg.run_dir / "result.json").read_text())["sets"]["tech_op"]["lanes"][h.LOCAL]
    assert lane["k"] == lane["n"] - 3


def test_tp2_reuses_p20_math(factory, capsys):
    cfg, c, pool = full_run(factory, "tp")
    assert call(cfg, "score") == 0
    capsys.readouterr()
    assert call(cfg, "tp2", "--f-fit", "9/10", "--s-tech", "30/100", "--s-tech-sessions", "5",
                "--s-tech-largest", "1/4", "--s-cheap", "0/0") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["e_tech"] == 1.0


def test_real_prereg_constants_block_matches_when_present():
    path = ROOT / "docs/prereg/PREREG-P20T-techop.md"
    if not path.exists():
        pytest.skip("prereg not committed yet")
    block = base.parse_block(path.read_text(), h.BLOCK)
    assert block.keys() == h.CONSTANTS_T.keys() | set(h.FILL_KEYS_T)
    assert all(block[k] == v for k, v in h.CONSTANTS_T.items())


def test_repeat_plant_is_placed_even_when_the_previous_call_would_fail_validity(factory):
    pool = [make_item(i) for i in range(130)]
    for row in pool:  # the previous call names a tool outside the item's list: invalid reply
        row["messages"][1]["content"] = '[tool_use Gone] {"x": 1}'
    cfg, c, _ = factory("badprev", items=pool)
    answers = {(i["id"], "sonnet"): {"ok": True, "text": reply(i["id"])} for i in pool}
    plants, stats = h.plant_candidates(pool, answers, c)
    assert sum("__repeat__" in v for v in plants.values()) == 6
    assert stats["plants_skipped"]["repeat"] == 0


def test_h13_repo_sha_must_hold_the_gate_and_record_competence(tmp_path, monkeypatch):
    monkeypatch.setattr(h.subprocess, "run", REAL_RUN)
    repo = tmp_path / "r"
    (repo / "src/llm_router/proxy").mkdir(parents=True)
    (repo / "src/llm_router/resolver").mkdir(parents=True)

    def git(*a):
        return REAL_RUN(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=repo,
                        capture_output=True, text=True, check=True).stdout.strip()

    git("init", "-q")
    (repo / "x").write_text("x")
    git("add", "-A"), git("commit", "-qm", "base")
    bare = git("rev-parse", "HEAD")
    gate = repo / "src/llm_router/proxy/local_gate.py"
    store = repo / "src/llm_router/resolver/eligibility_store.py"
    gate.write_text("# gate\n"), store.write_text("def other(): pass\n")
    git("add", "-A"), git("commit", "-qm", "no record_competence")
    half = git("rev-parse", "HEAD")
    store.write_text("def record_competence(): pass\n")
    git("add", "-A"), git("commit", "-qm", "both")
    full = git("rev-parse", "HEAD")
    for sha in (bare, half):
        with pytest.raises(h.Refuse, match="H13"):
            h.h13_check(repo, sha)
    h.h13_check(repo, full)
    h.h13_check(h.REPO, git_head())  # this worktree itself carries both


def git_head():
    return REAL_RUN(["git", "rev-parse", "HEAD"], cwd=h.REPO, capture_output=True, text=True,
                    check=True).stdout.strip()


# ---- AMEND1-T: the free-memory floor counts the arm's own resident model -----------------

AMEND1_DATE = "2028-01-01T00:00:00Z"


def amend1_text(**values):
    block = {**h.AMEND1_FIXED, "REPO_SHA": "9" * 40, **values}
    return ("# AMEND1\n\n" + h.AMEND1_BLOCK + "\n"
            + "".join(f"{k}={v}\n" for k, v in block.items()) + "```\n")


def freeze_amend1(cfg, *, text=None, date=AMEND1_DATE):
    cfg.amend1_t.write_text(text if text is not None else amend1_text())
    sidecar(cfg.amend1_t, date)


def test_amend1_frozen_enforcement(factory):
    cfg, c, _ = factory("a1")
    base_out = h.frozen(cfg)
    assert "amend1_sha256" not in base_out
    assert "QUIET_FREE_MEM_BASIS" not in base_out["constants"]
    freeze_amend1(cfg)
    got = h.frozen(cfg)
    assert got["amend1_sha256"] == base.sha256_file(cfg.amend1_t)
    assert got["amend1_frozen_utc"] == AMEND1_DATE
    assert got["constants"]["QUIET_FREE_MEM_BASIS"] == "excl_own_arm"
    assert got["constants"]["REPO_SHA"] == "9" * 40
    assert got["constants"]["LOCAL_MODELS"] == h.LOCAL
    (cfg.run_dir / "PREREG-AMEND1.md").write_text("different")
    with pytest.raises(h.Refuse, match="PREREG-AMEND1.md differs"):
        h.frozen(cfg)
    (cfg.run_dir / "PREREG-AMEND1.md").unlink()
    cases = [
        (dict(date="2026-12-31T00:00:00Z"), "AMEND1 frozen_utc"),  # before the prereg
        (dict(date="2031-01-01T00:00:00Z"), "AMEND1 frozen_utc"),  # after now
        (dict(date="2028-01-01T00:00:00"), "AMEND1 frozen_utc"),
        (dict(text=amend1_text(QUIET_FREE_MEM_BASIS="raw")), "AMEND1 constants"),
        (dict(text=amend1_text(QUIET_FREE_MEM_MIN_PCT="10")), "AMEND1 constants"),
        (dict(text=amend1_text(REPO_SHA="zz")), "AMEND1 constants"),
        (dict(text="# AMEND1\n" + h.AMEND1_BLOCK + "\nREPO_SHA=" + "9" * 40 + "\n```\n"),
         "AMEND1 constants"),
    ]
    for kwargs, message in cases:
        freeze_amend1(cfg, **kwargs)
        with pytest.raises(h.Refuse, match=message):
            h.frozen(cfg)
    freeze_amend1(cfg)
    cfg.amend1_t.write_text(cfg.amend1_t.read_text() + " ")
    with pytest.raises(h.Refuse, match="AMEND1 is not frozen"):
        h.frozen(cfg)


def test_amend1_quiet_counts_only_the_arms_own_model(factory):
    cfg, c, _ = factory("a1q")
    cfg.ollama.size = 24 * 2**30  # 50% of the fake 48 GiB
    cfg.machine.free = 27.0
    cfg.ollama.resident = [h.LOCAL]
    with pytest.raises(h.Park, match="machine not quiet"):  # raw basis (prereg as frozen)
        h.quiet(cfg, c, h.LOCAL)
    c1 = {**c, **h.AMEND1_FIXED}
    got = h.quiet(cfg, c1, h.LOCAL)
    assert got == [{"free_pct": 27.0, "load1": 1.0, "own_pct": 50.0}] * 3
    cfg.ollama.resident = []
    with pytest.raises(h.Park, match="machine not quiet"):
        h.quiet(cfg, c1, h.LOCAL)
    cfg.ollama.resident = ["foreign:latest"]
    with pytest.raises(h.Park, match="machine not quiet"):
        h.quiet(cfg, c1, h.LOCAL)
    cfg.machine.free = 80.0
    with pytest.raises(h.Park, match="foreign model resident"):
        h.quiet(cfg, c1, h.LOCAL)
    cfg.ollama.resident = [h.LOCAL]
    cfg.machine.free = float("nan")
    with pytest.raises(h.Park, match="machine not quiet"):
        h.quiet(cfg, c1, h.LOCAL)
    # the integrity check accepts own-basis readings only under the basis, and needs own_pct
    ok = {"free_pct": 27.0, "load1": 1.0, "own_pct": 50.0}
    path = cfg.run_dir / "answers/tech_op.jsonl"
    base.append_row(path, {"item_id": "a", "arm": h.LOCAL, "quiet": [ok] * 3})
    base.append_row(path, {"item_id": "b", "arm": h.LOCAL,
                           "quiet": [{"free_pct": 27.0, "load1": 1.0}] * 3})
    base.append_row(path, {"item_id": "c", "arm": h.LOCAL,
                           "quiet": [{**ok, "own_pct": -5.0}] * 3})
    assert h.unchecked_local_rows(cfg, c1) == 2
    assert h.unchecked_local_rows(cfg, c) == 3


@pytest.mark.parametrize("amend1", [False, True])
def test_amend1_run_survives_own_model_residency(factory, amend1):
    """The bug AMEND1-T fixes: loading the arm drops raw free memory under the floor, so every
    call after the warm-up parked (rc 3) and no answer row was written."""
    cfg, c, _ = factory(f"a1r{int(amend1)}")
    if amend1:
        freeze_amend1(cfg)
    assert call(cfg, "select") == 0
    cfg.ollama.size = 24 * 2**30
    original = cfg.ollama.chat

    def chat(model, messages, **kw):
        cfg.ollama.resident = [model]
        return original(model, messages, **kw)

    cfg.ollama.chat = chat
    cfg.machine.free_mem_pct = lambda: 27.0 if cfg.ollama.resident else 80.0
    rc = call(cfg, "run", "--set", "tech_op")
    path = cfg.run_dir / "answers/tech_op.jsonl"
    local = [r for r in base.jsonl(path) if r["arm"] == h.LOCAL] if path.exists() else []
    if not amend1:
        assert rc == 3 and local == []
        return
    assert rc == 0 and local
    assert all(q["own_pct"] == 50.0 for r in local for q in r["quiet"])
    assert h.unchecked_local_rows(cfg, h.frozen(cfg)["constants"]) == 0
