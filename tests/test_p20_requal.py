from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
from collections import Counter
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from llm_router.eligibility import grade
from llm_router.paths import state_path
from llm_router.resolver.eligibility_store import record_competence

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("p20_requal", ROOT / "scripts/p20_requal.py")
h = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = h
SPEC.loader.exec_module(h)
NOW = 1_900_000_000.0
PAST = "2026-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / "state"))
    monkeypatch.delenv("P20_REQUAL_DIR", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("real external call")

    monkeypatch.setattr(h.subprocess, "run", forbidden)
    monkeypatch.setattr(h, "urlopen", forbidden)


def pool_rows(name, n):
    rows = []
    for i in range(n):
        stratum = ("rx" if i < 10 else "gt") if name == "qa" else (
            "noncloud" if i < 55 else "rx_edit"
        ) if name == "code" else "organic"
        ident = f"{name}-{i:03}"
        row = {
            "id": ident, "set": name, "stratum": stratum,
            "source": "gen" if name == "qa" and i < 4 else "real",
            "session": ("rx-gen" if i < 4 else "crowded" if 10 <= i < 50
                        else f"session-{i}"),
            "tier": "simple" if i % 2 else "moderate",
            "task_type": "summary" if i < 20 else "explanation",
        }
        if name == "code":
            row["task_ref"] = ident
        else:
            row.update(request=f"ID:{ident} please explain this request", context="private context")
            if name == "tech_op":
                row.update(messages=[], tools=[{"name": "read", "description": "read a file"}])
        rows.append(row)
    if name == "qa":
        rows[4]["tier"] = "complex"
        rows[5]["request"] = "thanks"
        rows[6]["tier"] = "unlabelled"
    return rows


def freeze_file(cfg, constants, text=None, date=PAST):
    text = text if text is not None else h.PREREG.read_text()
    for key in h.FILL_KEYS:
        text = text.replace(f"{key}=FILL-AT-FREEZE", f"{key}={constants[key]}")
    cfg.prereg.write_text(text)
    cfg.prereg.with_suffix(".sha256").write_text(
        f"{h.sha256_file(cfg.prereg)}  {cfg.prereg.name}\nfrozen_utc {date}\n"
    )


class Ollama:
    def __init__(self):
        self.calls = []
        self.unloaded = []
        self.ids = {m: set(h.CONSTANTS[f"ID_{m}"].split(","))
                    for m in h.CONSTANTS["LOCAL_MODELS"].split(",")}
        self.resident = []
        self.fail = False

    def list_ids(self):
        return self.ids

    def ps(self):
        return self.resident

    def chat(self, model, messages, *, timeout, options):
        self.calls.append((model, messages, timeout, options))
        if messages[0]["content"] == "Reply OK.":
            return {"text": "OK", "error": None}
        if self.fail:
            raise TimeoutError("test timeout")
        ident = re.search(r"ID:(\S+)", h.flatten(messages))[1]
        return {"text": answer(model, ident), "error": None}

    def unload(self, model):
        self.unloaded.append(model)


def answer(arm, ident, good=True, long=True):
    label = h.digest(arm)[:8]
    return f"ANS:{label}:{ident}:" + ("x" * 500 if long else "x") + (
        ":good" if good else ":bad"
    )


class Claude:
    def __init__(self):
        self.calls = []
        self.mismatch = False
        self.invalid = False

    def __call__(self, model, prompt, *, timeout):
        self.calls.append((model, prompt))
        ident = re.search(r"ID:(\S+)", prompt)[1]
        if model == "test-opus":
            values = {}
            for letter, text in re.findall(
                r"Answer ([A-Z]):\n(.*?)(?=\n\nAnswer [A-Z]:\n|\Z)", prompt, re.S
            ):
                good = f":{ident}:" in text and text.endswith(":good")
                values[letter] = "acceptable" if good else "unacceptable"
            text = "invalid" if self.invalid else json.dumps(values)
        else:
            text = answer(model, ident)
        return {"text": text, "error": None, "cost_usd": 0.001,
                "served_model": "wrong" if self.mismatch else model}


class Machine:
    def __init__(self):
        self.free = [40.0, 40.0, 40.0]
        self.load = 3.99
        self.index = 0

    def free_mem_pct(self):
        value = self.free[self.index % len(self.free)]
        self.index += 1
        return value

    def load1(self):
        return self.load


@pytest.fixture
def factory(tmp_path):
    def make(label="run", small=False):
        run_dir = tmp_path / label
        run_dir.mkdir()
        cfg = h.Cfg(
            run_dir, prereg=run_dir / "frozen.md", now=lambda: NOW, local_hour=lambda: 0,
            sleep=lambda _: None, ollama=Ollama(), claude=Claude(), machine=Machine(),
            quota=lambda: {"session_pct": 0, "weekly_pct": 0, "age_s": 0},
            pack_fn=lambda item, name, window: [{"role": "user", "content": item["request"]}],
            git_sha=lambda: "deadbeef",
        )
        pools = {s: pool_rows(s, 30 if small and s == "qa" else 75 if s == "code" else 130)
                 for s in h.SETS}
        for s, rows in pools.items():
            h.atomic(run_dir / f"pools/{s}.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
        constants = dict(h.CONSTANTS)
        constants.update({key: "test" for key in h.FILL_KEYS})
        constants.update(REPO_SHA="deadbeef", CLAUDE_SONNET_ID="test-sonnet",
                         CLAUDE_HAIKU_ID="test-haiku", CLAUDE_OPUS_ID="test-opus")
        runner = run_dir / "runner.py"
        runner.write_text("print('runner')\n")
        constants["CODE_RUNNER_SHA256"] = h.sha256_file(runner)
        manifest = {key: constants[value] for key, value in h.SOURCE_PINS.items()}
        manifest["rx_membership"] = {"main_qa_packets": 50, "main_qa_graded": 48, "difference": 2}
        h.write_json(run_dir / "pools/build_manifest.json", manifest)
        constants["BUILD_MANIFEST_SHA256"] = h.sha256_file(run_dir / "pools/build_manifest.json")
        selected = h.select_sets(pools, constants)
        for s in h.SETS:
            constants[f"POOL_{s.upper()}_SHA256"] = h.sha256_file(run_dir / f"pools/{s}.jsonl")
            constants[f"N_REALISED_{s.upper()}"] = str(selected[s]["n"])
        constants.update(
            POOL_COUNT_QA_RX=str(sum(r["stratum"] == "rx" for r in pools["qa"])),
            POOL_COUNT_QA_GT=str(sum(r["stratum"] == "gt" for r in pools["qa"])),
            POOL_COUNT_TECH_OP=str(len(pools["tech_op"])),
            POOL_COUNT_CODE_NONCLOUD="55", POOL_COUNT_CODE_RX_EDIT="20",
        )
        freeze_file(cfg, constants)
        return cfg, constants, pools
    return make


def call(cfg, *argv):
    return h.main(list(argv), cfg=cfg)


def selected_run(factory, label="selected"):
    cfg, c, pools = factory(label)
    h.select(cfg, c)
    return cfg, c, pools


def prepare_score(factory, label, counts_by_set=None, n=60):
    cfg, c, pools = factory(label)
    chosen = {}
    for name in h.SETS:
        rows = pools[name][:n]
        chosen[name] = {"ids": [r["id"] for r in rows], "n": n}
        h.atomic(cfg.run_dir / f"items/{name}.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
    h.write_json(cfg.run_dir / "selection.json", {"sets": chosen})
    h.append_use(cfg, "selected",
                 f"selection_sha256={h.sha256_file(cfg.run_dir / 'selection.json')}")
    counts_by_set = counts_by_set or {}
    for name in ("qa", "tech_op"):
        counts_map = counts_by_set.get(name, {})
        for j, item in enumerate(pools[name][:n]):
            verdicts = {}
            for arm in h.arms(c):
                k = counts_map.get(arm, 57)
                verdicts[arm] = j < k
                h.append_row(cfg.run_dir / f"answers/{name}.jsonl", {
                    "item_id": item["id"], "arm": arm, "attempt": 1, "ok": True,
                    "final": True, "text": answer(arm, item["id"]), "truncated": False,
                    "error": None, "wall_s": 0, "ts": PAST,
                })
            plants = {"__swap__": False} if j < 6 else (
                {"__degraded__": False} if j < 12 else {}
            )
            h.append_row(cfg.run_dir / f"judgements/{name}.jsonl", {
                "item_id": item["id"], "letters": {}, "verdicts": verdicts,
                "rejudge_verdicts": None, "judge_error": False, "attempts": 1,
                "plants": plants, "ts": PAST,
            })
    for j, item in enumerate(pools["code"][:n]):
        for arm in h.arms(c, True):
            h.append_row(cfg.run_dir / "code_results.jsonl", {
                "task_id": item["id"], "arm": arm,
                "hidden_pass": j < counts_by_set.get("code", {}).get(arm, 57), "ts": NOW,
            })
    return cfg, c


def rewrite_rows(path, fn):
    rows = h.jsonl(path)
    fn(rows)
    h.atomic(path, "".join(json.dumps(r) + "\n" for r in rows))


def test_claude_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "private")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private")
    args = h.claude_argv("test-sonnet", tmp_path, tools="")
    assert args == [
        "claude", "-p", "--safe-mode", "--settings", str(tmp_path / "claude_settings.json"),
        "--model", "test-sonnet", "--output-format", "json", "--tools", "",
    ]
    path = Path(args[args.index("--settings") + 1])
    assert json.loads(path.read_text()) == {"disableAllHooks": True}
    assert path.stat().st_mode & 0o777 == 0o600
    assert "--tools" not in h.claude_argv("test-sonnet", tmp_path, tools=None)
    env = h.claude_env()
    assert "ANTHROPIC_BASE_URL" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["LLM_ROUTER_SESSION_KIND"] == "research"


def test_constants_and_real_freeze(factory, tmp_path):
    parsed = h.parse_constants(h.PREREG.read_text())
    assert {k: v for k, v in parsed.items() if v != "FILL-AT-FREEZE"} == h.CONSTANTS
    assert tuple(k for k, v in parsed.items() if v == "FILL-AT-FREEZE") == h.FILL_KEYS
    cfg, c, _ = factory()
    assert h.frozen(cfg)["constants"]["PLANTS_PASS_SHARE"] == "5/6"
    assert h.frozen(cfg)["constants"]["PLANTS_MIN_PLACED"] == "4"
    text = cfg.prereg.read_text().replace(
        "PLANTS_MIN_PLACED=4", "PLANTS_MIN_PLACED=4\nPLANTS_MIN_REJECTED=5"
    )
    freeze_file(cfg, c, text)
    with pytest.raises(h.Refuse, match="unknown constant PLANTS_MIN_REJECTED"):
        h.frozen(cfg)
    with pytest.raises(h.Refuse):
        h.parse_constants("no block")
    if not h.PREREG.with_suffix(".sha256").exists():
        pytest.skip("real prereg has no freeze sidecar")
    assert h.frozen(replace(cfg, prereg=h.PREREG, run_dir=tmp_path / "real"))


def test_freeze_gates_and_read_only_modes(factory, capsys):
    commands = [
        ("select",), ("run", "--set", "qa"), ("judge", "--set", "qa"),
        ("ingest-code", "--results", "/missing", "--runner", "/missing"), ("score",),
        ("install-eligibility", "--confirm"),
        ("tp2", "--s-tech", "30/100", "--s-tech-sessions", "3", "--s-tech-largest", ".5",
         "--f-fit", "1/2", "--s-cheap", "2/100"),
    ]
    for defect in ("missing", "sha", "future", "fill", "constant"):
        cfg, c, _ = factory(defect)
        if defect == "missing":
            cfg.prereg.with_suffix(".sha256").unlink()
        elif defect == "sha":
            cfg.prereg.write_text(cfg.prereg.read_text() + "\n")
        elif defect == "future":
            freeze_file(cfg, c, cfg.prereg.read_text(), "2099-01-01T00:00:00Z")
        elif defect == "fill":
            freeze_file(cfg, c, cfg.prereg.read_text().replace("REPO_SHA=deadbeef",
                                                             "REPO_SHA=FILL-AT-FREEZE"))
            cfg.prereg.write_text(cfg.prereg.read_text().replace("REPO_SHA=deadbeef",
                                                                "REPO_SHA=FILL-AT-FREEZE"))
            cfg.prereg.with_suffix(".sha256").write_text(
                f"{h.sha256_file(cfg.prereg)}  frozen.md\nfrozen_utc {PAST}\n"
            )
        else:
            freeze_file(cfg, c, cfg.prereg.read_text().replace("N_MIN=60", "N_MIN=61"))
        for command in commands:
            assert call(cfg, *command) == 2
        before = {p: p.read_bytes() for p in cfg.run_dir.rglob("*") if p.is_file()}
        assert call(cfg, "select", "--dry-run") == 0
        assert call(cfg, "status") == 0
        after = {p: p.read_bytes() for p in cfg.run_dir.rglob("*") if p.is_file()}
        assert before == after
    assert "private context" not in capsys.readouterr().out


def test_selection(factory, capsys):
    cfg, c, pools = factory()
    selected = h.select_sets(pools, c)
    assert selected == h.select_sets({s: list(reversed(r)) for s, r in pools.items()}, c)
    qa = selected["qa"]
    assert qa["excluded"]["tier_complex"] == 1
    assert qa["excluded"]["too_short"] == 1
    assert qa["excluded"]["no_tier"] == 1
    by_id = {r["id"]: r for r in pools["qa"]}
    assert Counter(by_id[i]["session"] for i in qa["ids"])["crowded"] == 24
    strata = [by_id[i]["stratum"] for i in qa["ids"]]
    assert strata == sorted(strata, key=lambda s: s != "rx")
    assert selected["code"]["n"] == 75
    more = dict(pools, qa=pool_rows("qa", 160))
    assert h.select_sets(more, c)["qa"]["n"] == 120
    duplicate = dict(pools, qa=pools["qa"] + [pools["qa"][0]])
    with pytest.raises(h.Refuse, match="duplicate"):
        h.select_sets(duplicate, c)
    assert call(cfg, "select") == 0
    assert call(cfg, "select") == 2
    for path in [cfg.run_dir / "selection.json", *list((cfg.run_dir / "items").glob("*"))]:
        assert path.stat().st_mode & 0o777 == 0o600
    assert "please explain" not in capsys.readouterr().out
    for defect, key in (
        ("pool", "POOL_QA_SHA256"), ("count", "POOL_COUNT_QA_RX"), ("n", "N_REALISED_QA"),
    ):
        bad, cc, _ = factory(defect)
        cc[key] = "0"
        freeze_file(bad, cc)
        assert call(bad, "select") == 2
    small, cc, _ = factory("small", small=True)
    assert call(small, "select") == 0
    assert any(r[0] == "terminal" and r[2] == "qa" for r in h.uses(small))
    assert h.read_json(small.run_dir / "selection.json")["sets"]["code"]["n"] == 75


def test_run_boundaries_and_resume(factory):
    arm = h.CONSTANTS["LOCAL_MODELS"].split(",")[0]
    cfg, c, _ = selected_run(factory)
    cfg.ollama.ids[arm] = {"wrong"}
    assert call(cfg, "run", "--set", "qa", "--arms", arm) == 2
    assert not cfg.ollama.calls
    cfg.ollama.ids[arm] = set(c[f"ID_{arm}"].split(","))
    assert call(replace(cfg, local_hour=lambda: 7), "run", "--set", "qa", "--arms", arm) == 3
    cfg.machine.free = [40, 39.9, 40]
    assert call(cfg, "run", "--set", "qa", "--arms", arm) == 3
    cfg.machine.free, cfg.machine.load = [40] * 3, 4.0
    assert call(cfg, "run", "--set", "qa", "--arms", arm) == 3
    cfg.machine.load = 3.99
    cfg.ollama.resident = ["foreign"]
    assert call(cfg, "run", "--set", "qa", "--arms", arm) == 3
    assert not cfg.ollama.unloaded and not cfg.ollama.calls
    assert not (cfg.run_dir / "answers/qa.jsonl").exists()
    cfg.ollama.resident = []
    cfg.ollama.fail = True
    assert call(cfg, "run", "--set", "qa", "--arms", arm) == 0
    rows = h.jsonl(cfg.run_dir / "answers/qa.jsonl")
    n = h.selection(cfg)["qa"]["n"]
    assert len(rows) == 2 * n and len(cfg.ollama.calls) == 1 + 2 * n
    assert all(not r["ok"] for r in rows)
    assert sum(r["final"] for r in rows) == n
    assert cfg.ollama.unloaded == [arm]
    before = len(cfg.ollama.calls)
    assert call(cfg, "run", "--set", "qa", "--arms", arm) == 0
    assert len(cfg.ollama.calls) == before
    assert all("text" not in r for r in h.jsonl(cfg.run_dir / "ledger.jsonl"))
    quota = replace(cfg, quota=lambda: {"session_pct": 0, "weekly_pct": 85, "age_s": 0})
    assert call(quota, "run", "--set", "qa", "--arms", "sonnet") == 3
    assert not cfg.claude.calls
    h.append_row(cfg.run_dir / "ledger.jsonl", {"cost_usd": 60})
    assert call(cfg, "run", "--set", "qa", "--arms", "sonnet") == 3
    assert not cfg.claude.calls
    good, cc, _ = selected_run(factory, "good")
    assert call(good, "run", "--set", "qa", "--arms", arm) == 0
    assert all(r["ok"] for r in h.jsonl(good.run_dir / "answers/qa.jsonl"))
    good.claude.mismatch = True
    assert call(good, "run", "--set", "qa", "--arms", "sonnet") == 0
    rows = [r for r in h.jsonl(good.run_dir / "answers/qa.jsonl") if r["arm"] == "sonnet"]
    assert all(r["error"] == "served_model_mismatch" for r in rows)


def test_judge_plants_and_retries(factory):
    cfg, c, pools = selected_run(factory)
    assert call(cfg, "judge", "--set", "qa") == 2
    for name in ("qa", "tech_op"):
        assert call(cfg, "run", "--set", name) == 0
    qa_items = h.items_for(cfg, "qa", h.selection(cfg))
    order = sorted((i["id"] for i in qa_items),
                   key=lambda i: h.digest(f"{c['SELECT_SEED']}:plant:{i}"))
    keep = set(order[6:10])
    path = cfg.run_dir / "answers/qa.jsonl"

    def shorten(rows):
        for r in rows:
            if r["arm"] == "sonnet" and r["item_id"] not in keep:
                r["text"] = answer("sonnet", r["item_id"], long=False)

    rewrite_rows(path, shorten)
    assert call(cfg, "judge", "--set", "qa") == 0
    rows = h.jsonl(cfg.run_dir / "judgements/qa.jsonl")
    assert sum("__swap__" in r["plants"] for r in rows) == 6
    assert sum("__degraded__" in r["plants"] for r in rows) == 4
    assert all(v is False for r in rows for v in r["plants"].values())
    assert sum(r["rejudge_verdicts"] is not None for r in rows) == math.ceil(.10 * len(rows))
    assert all(model not in p for _, p in cfg.claude.calls
               for model in c["LOCAL_MODELS"].split(","))
    before = len(cfg.claude.calls)
    assert call(cfg, "judge", "--set", "qa") == 0
    assert len(cfg.claude.calls) == before
    tech_items = h.items_for(cfg, "tech_op", h.selection(cfg))
    order = sorted((i["id"] for i in tech_items),
                   key=lambda i: h.digest(f"{c['SELECT_SEED']}:plant:{i}"))
    rewrite_rows(cfg.run_dir / "answers/tech_op.jsonl", shorten)
    assert call(cfg, "judge", "--set", "tech_op") == 0
    rows = h.jsonl(cfg.run_dir / "judgements/tech_op.jsonl")
    assert {r["item_id"] for r in rows if "__degraded__" in r["plants"]} == set(order[6:12])
    bad, cc, _ = selected_run(factory, "invalid")
    assert call(bad, "run", "--set", "qa") == 0
    bad.claude.invalid = True
    before = len(bad.claude.calls)
    assert call(bad, "judge", "--set", "qa") == 0
    rows = h.jsonl(bad.run_dir / "judgements/qa.jsonl")
    assert all(r["judge_error"] for r in rows)
    n = len(rows)
    assert len(bad.claude.calls) - before == 2 * (n + math.ceil(.1 * n))


def test_ingest_code(factory):
    cfg, c, _ = selected_run(factory)
    runner = cfg.run_dir / "runner.py"
    rows = [{"task_id": ident, "arm": arm, "hidden_pass": True, "ts": NOW}
            for ident in h.selection(cfg)["code"]["ids"] for arm in h.arms(c, True)]
    path = cfg.run_dir / "external.jsonl"
    h.atomic(path, "".join(json.dumps(r) + "\n" for r in rows))
    wrong = cfg.run_dir / "wrong.py"
    wrong.write_text("wrong")
    assert call(cfg, "ingest-code", "--results", str(path), "--runner", str(wrong)) == 2
    old = [dict(r, ts="2025-01-01T00:00:00Z") for r in rows]
    for invalid in (old, rows[:-1], [dict(rows[0], hidden_pass=1)] + rows[1:]):
        h.atomic(path, "".join(json.dumps(r) + "\n" for r in invalid))
        assert call(cfg, "ingest-code", "--results", str(path), "--runner", str(runner)) == 2
    h.atomic(path, "".join(json.dumps(r) + "\n" for r in rows))
    assert call(cfg, "ingest-code", "--results", str(path), "--runner", str(runner)) == 0
    assert len(h.jsonl(cfg.run_dir / "code_results.jsonl")) == 225


def test_score_statistics_and_single_use(factory, capsys):
    local = h.CONSTANTS["LOCAL_MODELS"].split(",")
    for k, kr, g, verdict, powered in (
        (57, 57, "eligible", "eligible", True),
        (54, 57, "failed", "failed", True),
        (56, 56, "failed", "inconclusive", False),
        (59, 60, "eligible", "eligible", True),
    ):
        cfg, c = prepare_score(factory, f"stats-{k}", {"qa": {local[0]: k, "sonnet": kr}})
        assert call(cfg, "score") == 0
        result = h.read_json(cfg.run_dir / "result.json")
        lane = result["sets"]["qa"]["lanes"][local[0]]
        assert (lane["k"], lane["n"], lane["grade"], lane["verdict"], lane["powered"]) == (
            k, 60, g, verdict, powered,
        )
        assert lane["equal_threshold_k"] == 57
        stored = h.read_json(cfg.run_dir / "local_eligibility.json")
        for tasks in stored.values():
            for tiers in tasks.values():
                for entry in tiers.values():
                    assert grade(entry) == entry["grade"]
        assert "code" not in stored["qwen3.5:latest"]
        assert call(cfg, "score") == 2
    cfg, c = prepare_score(factory, "corrupt")
    (cfg.run_dir / "judgements/qa.jsonl").write_text("{invalid")
    assert call(cfg, "score") == 2
    assert any(r[0] == "started" for r in h.uses(cfg))
    assert call(cfg, "score") == 2
    cfg, c = prepare_score(factory, "local-error")

    def fail_local(rows):
        for r in rows:
            if r["arm"] == local[0]:
                r["ok"], r["text"] = False, ""

    rewrite_rows(cfg.run_dir / "answers/qa.jsonl", fail_local)
    assert call(cfg, "score") == 0
    lane = h.read_json(cfg.run_dir / "result.json")["sets"]["qa"]["lanes"][local[0]]
    assert lane["k"] == 0 and lane["n"] == 60
    assert "ANS:" not in capsys.readouterr().out


def test_score_calibration_and_reference_missing(factory):
    for label, m, rejected, informative in (
        ("five-of-six", 6, 5, True), ("four-of-six", 6, 4, False),
        ("four-of-four", 4, 4, True), ("three-of-four", 4, 3, False),
        ("three-of-three", 3, 3, False),
    ):
        cfg, c = prepare_score(factory, label)
        def change(rows):
            for j, row in enumerate(rows):
                row["plants"].pop("__degraded__", None)
                if 6 <= j < 6 + m:
                    row["plants"]["__degraded__"] = j >= 6 + rejected
        rewrite_rows(cfg.run_dir / "judgements/qa.jsonl", change)
        assert call(cfg, "score") == 0
        result = h.read_json(cfg.run_dir / "result.json")
        assert ("qa" not in result["not_informative"]) is informative
        if not informative:
            assert result["not_informative"]["qa"] == "calibration"
            assert any(r[0] == "terminal" and r[2] == "qa" for r in h.uses(cfg))
    cfg, c = prepare_score(factory, "ref-missing")
    def missing(rows):
        for row in rows[:7]:
            row["judge_error"] = True
    rewrite_rows(cfg.run_dir / "judgements/qa.jsonl", missing)
    assert call(cfg, "score") == 0
    assert h.read_json(cfg.run_dir / "result.json")["not_informative"]["qa"] == "ref_missing"


def test_score_decisions_d21_and_code_pairs(factory):
    locals_ = h.CONSTANTS["LOCAL_MODELS"].split(",")
    fail = {m: 54 for m in locals_}
    fail["sonnet"] = 57
    for label, values, expected in (
        ("tech-fail", {"tech_op": fail}, (True, False, False)),
        ("qa-fail", {"qa": fail}, (False, True, False)),
        ("code-fail", {"code": fail}, (False, False, False)),
        ("all-fail", {s: fail for s in h.SETS}, (True, True, True)),
        ("code-saves", {"qa": fail, "tech_op": fail}, (True, True, False)),
    ):
        cfg, c = prepare_score(factory, label, values)
        assert call(cfg, "score") == 0
        result = h.read_json(cfg.run_dir / "result.json")
        assert tuple(result[k] for k in ("cancel_p21", "cancel_p210", "kill_all")) == expected
    limited = {m: 56 for m in locals_ + ["sonnet"]}
    cfg, c = prepare_score(factory, "inconclusive", {"qa": limited})
    assert call(cfg, "score") == 0
    result = h.read_json(cfg.run_dir / "result.json")
    assert "qa" in result["no_decision"] and not result["cancel_p210"]
    cfg, c = prepare_score(
        factory, "d21", {"qa": {m: 72 for m in locals_ + ["sonnet", "haiku"]}}, n=75,
    )
    def types(rows):
        for j, row in enumerate(rows):
            row["task_type"] = "twenty" if j < 20 else "nineteen" if j < 39 else "other"
    rewrite_rows(cfg.run_dir / "items/qa.jsonl", types)
    arm = locals_[0]
    def nulls(rows):
        for row in rows:
            if row["arm"] == arm and row["task_id"].endswith("000"):
                row["hidden_pass"] = None
    rewrite_rows(cfg.run_dir / "code_results.jsonl", nulls)
    assert call(cfg, "score") == 0
    result = h.read_json(cfg.run_dir / "result.json")
    types = result["d21"][arm]["task_types"]
    assert types["twenty"]["n"] == 20 and types["twenty"]["action"].startswith("re-enable")
    assert types["nineteen"] == {"n": 19, "action": "stays stripped"}
    lanes = result["sets"]["code"]["lanes"]
    assert lanes[arm]["n"] == 74 and lanes[arm]["removed"] == 1
    assert lanes[locals_[1]]["n"] == 75


def test_record_competence(tmp_path):
    path = tmp_path / "eligibility.json"
    original = {"m": {"fitness": {"json": {"n": 1}}, "fitness_at": 12},
                "other": {"qa": "kept"}}
    path.write_text(json.dumps(original))
    lane = {"n": 60, "gap_pts": 0, "lb_pts": -9, "grade": "eligible",
            "verdict": "eligible", "eligible": True}
    assert record_competence("m", "qa", {"simple": lane}, path=path)
    data = json.loads(path.read_text())
    assert data["m"]["fitness"] == original["m"]["fitness"]
    assert data["m"]["fitness_at"] == 12 and data["other"] == original["other"]
    assert path.stat().st_mode & 0o777 == 0o600
    before = path.read_bytes()
    for task, tier, bad in (
        ("bad", "simple", lane), ("qa", "complex", lane),
        ("qa", "simple", dict(lane, grade="failed")),
        ("qa", "simple", dict(lane, verdict="bad")),
        ("qa", "simple", dict(lane, eligible=False)),
        ("qa", "simple", dict(lane, n=0, grade="unknown")),
    ):
        with pytest.raises(ValueError):
            record_competence("m", task, {tier: bad}, path=path)
        assert path.read_bytes() == before
    assert record_competence("m", "qa", {}, path=path) is False
    assert path.read_bytes() == before
    path.write_text("[]")
    assert record_competence("m", "qa", {"moderate": lane}, path=path)
    assert json.loads(path.read_text())["m"]["qa"]["moderate"] == lane


def test_install(factory):
    cfg, c = prepare_score(factory, "install")
    assert call(cfg, "install-eligibility", "--confirm") == 2
    assert call(cfg, "score") == 0
    assert call(cfg, "install-eligibility") == 2
    path = state_path("local_eligibility.json")
    h.write_json(path, {"qwen3.5:latest": {"fitness": {"json": {"n": 1}}, "fitness_at": 7}})
    assert call(cfg, "install-eligibility", "--confirm") == 0
    assert h.read_json(path)["qwen3.5:latest"]["fitness_at"] == 7
    assert h.read_json(path)["qwen3.5:latest"]["fitness"]["json"]["n"] == 1


def test_tp2_exact_fallback_and_flags(factory, capsys):
    cfg, c = prepare_score(factory, "tp2")
    args = ("tp2", "--s-tech", "30/100", "--s-tech-sessions", "3", "--s-tech-largest", ".5",
            "--f-fit", "1/2", "--s-cheap", "2/100")
    assert call(cfg, *args) == 2
    assert call(cfg, "score") == 0
    result = h.read_json(cfg.run_dir / "result.json")
    result["e_tech"] = {"model": c["LOCAL_MODELS"].split(",")[0], "k": 54, "n": 60}
    h.write_json(cfg.run_dir / "result.json", result)
    h.append_use(cfg, "scored", f"result_sha256={h.sha256_file(cfg.run_dir / 'result.json')}")
    assert call(cfg, *args) == 0
    out = h.read_json(cfg.run_dir / "tp2.json")
    assert Fraction(out["fractions"]["T_P2"]) == Fraction(31, 250)
    assert out["gating_s_tech_basis"] == "census"
    expected = Fraction(4, 5) * (Fraction(27, 200) + Fraction(7, 10) * Fraction(1, 50))
    assert Fraction(out["fractions"]["T_P2_alt"]) == expected
    assert "report_to_owner" in out["flags"]
    fallback = list(args)
    fallback[fallback.index("30/100")] = "1/45"
    assert call(cfg, *fallback) == 0
    out = h.read_json(cfg.run_dir / "tp2.json")
    assert out["gating_s_tech_basis"] == "fallback_1/45"
    assert Fraction(out["fractions"]["gating_s_tech"]) == Fraction(1, 45)
    high = Fraction(4, 5) * (Fraction(24, 45) * Fraction(1, 2) * Fraction(9, 10) + Fraction(1, 50))
    assert Fraction(out["fractions"]["tp2_high"]) == high
    assert "ceiling_below_10pct" in out["flags"]
    result["e_tech"] = None
    h.write_json(cfg.run_dir / "result.json", result)
    h.append_use(cfg, "scored", f"result_sha256={h.sha256_file(cfg.run_dir / 'result.json')}")
    assert call(cfg, *args) == 0
    assert "no_eligible_tech_op" in h.read_json(cfg.run_dir / "tp2.json")["flags"]
    zero = list(args)
    zero[zero.index("2/100")] = "0/0"
    assert call(cfg, *zero) == 0
    assert "s_cheap_n0" in h.read_json(cfg.run_dir / "tp2.json")["flags"]
    missing = list(args)
    start = missing.index("--f-fit")
    del missing[start:start + 2]
    with pytest.raises(SystemExit) as error:
        call(cfg, *missing)
    assert error.value.code == 2
    assert '"T_P2_alt"' in capsys.readouterr().out


def test_protocol_helpers(factory):
    cfg, c, _ = factory()
    assert h.agreement('{"tool":"read","input":{"file_path":"a/../b"}}',
                       '{"tool":"read","input":{"path":"b"}}')
    assert h.agreement('{"tool":"find","input":{"pattern":" x "}}',
                       '{"tool":"find","input":{"pattern":"x"}}')
    assert h.agreement('{"tool":"shell","input":{"command":"a   b"}}',
                       '{"tool":"shell","input":{"command":"a b"}}')
    assert h.agreement('{"final":"one"}', '{"final":"two"}')
    assert not h.agreement("broken", "broken")
    path = cfg.run_dir / "jsonl"
    path.write_text('\n{"n":1}\n\n')
    assert h.jsonl(path) == [{"n": 1}]
    path.write_text("broken")
    with pytest.raises(h.Refuse):
        h.jsonl(path)


def test_frozen_utc_must_be_strictly_in_the_past(factory):
    cfg, c, _ = factory()
    at_now = "2030-03-17T17:46:40Z"  # == NOW (1_900_000_000)
    freeze_file(cfg, c, cfg.prereg.read_text(), at_now)
    with pytest.raises(h.Refuse, match="not frozen"):
        h.frozen(cfg)
    freeze_file(cfg, c, cfg.prereg.read_text(), "2030-03-17T17:46:39Z")
    assert h.frozen(cfg)["frozen_utc"] == "2030-03-17T17:46:39Z"
