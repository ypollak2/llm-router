from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


r = load("p20_code_runner_test", ROOT / "scripts/p20_code_runner.py")
support = load("p20_code_support", ROOT / "tests/test_p20_requal.py")
h = r.h
factory = support.factory


@pytest.fixture
def setup(factory, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / "state"))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://forbidden")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "forbidden")
    cfg, c, _ = factory()
    nc, rx = tmp_path / "nc", tmp_path / "rx"
    nc.mkdir()
    rx.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "fix.py").write_text("value = 0\n")
    r.git(repo, "init", "-q")
    r.git(repo, "add", ".")
    r.git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
          "-c", "commit.gpgsign=false", "commit", "-qm", "base")
    parent = r.git(repo, "rev-parse", "HEAD")
    h.append_row(nc / "tasks_meta.jsonl", {"task_id": "nc", "parent_sha": parent})
    (nc / "run_codex.py").write_text(
        "from pathlib import Path\nimport subprocess\n"
        f"REPO = Path({str(repo)!r})\n"
        "grades = [True, True, False]\n"
        "def ensure_clone():\n    return REPO\n"
        "def build_prompt(meta):\n    return 'PRIVATE PROMPT'\n"
        "def make_wt(task_id, tag, parent):\n"
        "    path = REPO.parent / ('wt_' + tag)\n"
        "    subprocess.run(['git', '-C', str(REPO), 'worktree', 'add', '--detach', "
        "str(path), parent], check=True, capture_output=True)\n"
        "    return path\n"
        "def grade_all(*args):\n    return grades, None\n"
    )
    (nc / "lib_pi.py").write_text(
        "from pathlib import Path\n"
        "mode = 'pass'\ncalls = 0\n"
        "def run_pi(prompt, cwd, timeout_s):\n"
        "    global calls\n    calls += 1\n"
        "    print('PRIVATE MODEL TEXT')\n"
        "    work = Path(cwd)\n"
        "    assert not (work / 'tests').exists()\n"
        "    if mode != 'empty':\n"
        "        (work / 'fix.py').write_text('value = 1\\n' "
        "if mode == 'pass' else 'value = 0  # edited\\n')\n"
        "    if mode == 'cheat':\n"
        "        (work / 'tests').mkdir()\n"
        "        (work / 'tests/test_fix.py').write_text('def test_fake(): assert True\\n')\n"
        "    return {'timed_out': mode == 'timeout', "
        "'returncode': int(mode == 'infra'), 'n_events': 1}\n"
    )
    for name in ("run_codex", "lib_pi"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(nc))
    nc_module = __import__("run_codex")
    pi = __import__("lib_pi")
    refs = [{"task_id": "nc"}]
    rows = []
    for number in range(2):
        name = f"fx{number}"
        directory = f"edit-{number:02d}-{name}"
        fixture = rx / "fixtures" / directory
        (fixture / "tests").mkdir(parents=True)
        (fixture / "fix.py").write_text("value = 0\n")
        (fixture / "tests/test_fix.py").write_text(
            "import runpy\n"
            "def test_hidden():\n    assert runpy.run_path('fix.py')['value'] == 1\n"
        )
        # Real manifest shape: name, idx, absolute path of edit-<idx>-<name>, module, func,
        # summary, and test with its tests/ prefix; the pool's task_ref carries no summary.
        rows.append(dict(name=name, idx=number, path=f"/elsewhere/fixtures/{directory}",
                         module="fix.py", func="value", summary="PRIVATE SUMMARY",
                         test="tests/test_fix.py"))
        refs.append(dict(stratum="rx_edit", dir=directory, name=name, idx=number,
                         module="fix.py", func="value", test="tests/test_fix.py"))
    manifest = rx / "fixtures/manifest.json"
    manifest.write_text(json.dumps(rows))
    h.write_json(cfg.run_dir / "pools/build_manifest.json", {
        "sources": {"rx_fixtures_manifest": h.sha256_file(manifest)},
    })
    items = [
        dict(id=f"task-{i}", stratum="noncloud" if i == 0 else "rx_edit", task_ref=ref)
        for i, ref in enumerate(refs)
    ]
    h.atomic(cfg.run_dir / "items/code.jsonl",
             "".join(json.dumps(item) + "\n" for item in items))
    h.write_json(cfg.run_dir / "selection.json", {
        "sets": {"code": {"ids": [item["id"] for item in items], "n": 3}},
    })
    h.append_use(cfg, "selected",
                 f"selection_sha256={h.sha256_file(cfg.run_dir / 'selection.json')}")
    c["CODE_RUNNER_SHA256"] = h.sha256_file(Path(r.__file__))
    support.freeze_file(cfg, c)
    tc = tmp_path / "contract.py"
    tc.write_text("raise RuntimeError('never import')\nTEST_CONTRACT = 'PRIVATE CONTRACT'\n")
    arm = c["CODE_LOCAL_MODELS"].split(",")[0]
    argv = ["--noncloud-dir", str(nc), "--rx-dir", str(rx), "--tc-source", str(tc)]
    return cfg, c, argv, arm, pi, nc_module, rx


def invoke(env, arm=None, extra=(), cfg=None):
    original, _, argv, local, *_ = env
    return r.main([*argv, "--arm", arm or local, *extra], cfg=cfg or original)


def rows(env, smoke=False):
    return h.jsonl(env[0].run_dir / (
        "code_results.smoke.jsonl" if smoke else "code_results.raw.jsonl"
    ))


def test_freeze_selection_and_score_gates(setup):
    cfg = setup[0]
    sidecar = cfg.prereg.with_suffix(".sha256")
    saved = sidecar.read_bytes()
    sidecar.unlink()
    assert invoke(setup) == 2
    sidecar.write_bytes(saved)
    selection = cfg.run_dir / "selection.json"
    saved = selection.read_bytes()
    selection.write_bytes(saved + b" ")
    assert invoke(setup) == 2
    selection.write_bytes(saved)
    h.append_use(cfg, "started", "score")
    assert invoke(setup) == 2
    assert not rows(setup)


def test_parking(setup):
    cfg, _, _, arm, pi, *_ = setup
    assert invoke(setup, cfg=replace(cfg, local_hour=lambda: 12)) == 3
    cfg.ollama.resident = ["foreign"]
    assert invoke(setup) == 3
    assert not cfg.ollama.unloaded
    assert pi.calls == 0
    assert not rows(setup)
    cfg.ollama.resident = []
    cfg.machine.load = 4
    assert invoke(setup) == 3
    assert arm not in cfg.ollama.unloaded


@pytest.mark.parametrize("mode,grades,expected,attempts", [
    ("pass", [True, True, False], True, 1),
    ("pass", [True, False, False], False, 1),
    ("pass", [True, True, False, False], False, 1),
    ("timeout", [True], False, 1),
    ("empty", [True], False, 1),
    ("infra", [True], None, 2),
])
def test_noncloud_outcomes(setup, mode, grades, expected, attempts):
    cfg, _, _, _, pi, nc, _ = setup
    pi.mode, nc.grades = mode, grades
    assert invoke(setup, extra=("--smoke", "--limit-tasks", "1")) == 0
    result = rows(setup, True)
    assert len(result) == 1 and result[0]["hidden_pass"] is expected
    assert result[0]["attempt"] == attempts
    assert pi.calls == attempts
    assert result[0]["timed_out"] is (mode == "timeout")
    assert result[0]["patch_empty"] is (mode == "empty")
    assert not (cfg.run_dir / "code_results.raw.jsonl").exists()
    assert (cfg.run_dir / "code_results.smoke.jsonl").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mode,expected", [("pass", True), ("cheat", False)])
def test_hidden_tests_and_resume(setup, mode, expected, capsys):
    cfg, _, _, arm, pi, *_ = setup
    pi.mode = mode
    assert invoke(setup) == 0
    result = rows(setup)
    assert len(result) == 3
    assert all(row["hidden_pass"] is expected for row in result[1:])
    for number in range(2):
        work = cfg.run_dir / "code_runs" / r.safe(arm) / f"task-{number + 1}" / "work"
        assert (work / "tests/test_fix.py").exists()
    calls = pi.calls
    assert invoke(setup) == 0
    assert pi.calls == calls and rows(setup) == result
    output = capsys.readouterr().out.splitlines()
    assert len(output) == 2
    assert json.loads(output[-1])["skipped_existing"] == 3
    assert "PRIVATE" not in "".join(output)
    assert (cfg.run_dir / "code_results.raw.jsonl").stat().st_mode & 0o777 == 0o600


def fake_claude(setup, monkeypatch, served=None):
    cfg, c, *_ = setup

    def call(argv, **kwargs):
        assert "--safe-mode" in argv
        settings = Path(argv[argv.index("--settings") + 1])
        assert h.read_json(settings) == {"disableAllHooks": True}
        assert "--permission-mode" in argv and "--allowedTools" in argv
        assert "ANTHROPIC_BASE_URL" not in kwargs["env"]
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        assert not (Path(kwargs["cwd"]) / "tests").exists()
        assert "PRIVATE CONTRACT" in kwargs["input"]
        (Path(kwargs["cwd"]) / "fix.py").write_text("value = 1\n")
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            "total_cost_usd": .01, "is_error": False,
            "modelUsage": {served or c["CLAUDE_SONNET_ID"]: {}},
            "result": "PRIVATE MODEL TEXT",
        }), "")
    monkeypatch.setattr(r, "_claude_run", call)
    return cfg


def test_sonnet_and_ingest(setup, monkeypatch, capsys):
    cfg = fake_claude(setup, monkeypatch)
    for arm in h.arms(setup[1], True):
        assert invoke(setup, arm) == 0
    result = rows(setup)
    assert len(result) == 9
    assert len({(x["task_id"], x["arm"]) for x in result}) == 9
    ledger = h.jsonl(cfg.run_dir / "ledger.jsonl")
    assert len(ledger) == 3
    assert all(x["stage"] == "code" and x["set"] == "code" for x in ledger)
    assert "PRIVATE" not in json.dumps(ledger)
    assert "PRIVATE" not in capsys.readouterr().out
    assert h.ingest_code(cfg, setup[1], h.frozen(cfg),
                         cfg.run_dir / "code_results.raw.jsonl", Path(r.__file__)) == {"rows": 9}


def test_sonnet_mismatch(setup, monkeypatch):
    cfg = fake_claude(setup, monkeypatch, served="wrong")
    assert invoke(setup, "sonnet") == 0
    assert all(x["hidden_pass"] is None and x["attempt"] == 2 for x in rows(setup))
    assert all(x["infra_class"] == "served_model_mismatch" for x in rows(setup))
    assert len(h.jsonl(cfg.run_dir / "ledger.jsonl")) == 6


def test_quota_spend_and_night(setup, monkeypatch):
    cfg = fake_claude(setup, monkeypatch)
    assert invoke(setup, "sonnet", cfg=replace(cfg, local_hour=lambda: 7)) == 3
    assert invoke(setup, "sonnet", cfg=replace(cfg, quota=lambda: None)) == 3
    h.append_row(cfg.run_dir / "ledger.jsonl", {"cost_usd": 60})
    assert invoke(setup, "sonnet") == 3
    assert not rows(setup)
    assert len(h.jsonl(cfg.run_dir / "ledger.jsonl")) == 1


def test_manifest_limit_and_contract(setup):
    cfg, _, argv, _, _, _, rx = setup
    assert r.contract(Path(argv[-1])) == "PRIVATE CONTRACT"
    assert invoke(setup, extra=("--limit-tasks", "1")) == 2
    (rx / "fixtures/manifest.json").write_text("changed")
    assert invoke(setup) == 2
    assert len(rows(setup)) == 1


def test_refusal_and_park_reasons_reach_stderr(setup, capsys):
    cfg = setup[0]
    sidecar = cfg.prereg.with_suffix(".sha256")
    saved = sidecar.read_bytes()
    sidecar.unlink()
    assert invoke(setup) == 2
    assert capsys.readouterr().err.strip() == "refusing: prereg is not frozen"
    sidecar.write_bytes(saved)
    assert invoke(setup, cfg=replace(cfg, local_hour=lambda: 12)) == 3
    assert capsys.readouterr().err.strip() == "outside night window"


def test_contract_picks_the_named_assignment(tmp_path):
    src = tmp_path / "tc.py"
    src.write_text("OTHER = 'not this'\nTEST_CONTRACT = 'the contract'\n")
    assert r.contract(src) == "the contract"
    src.write_text("OTHER = 'only'\n")
    with pytest.raises(h.Refuse):
        r.contract(src)


def test_no_third_attempt_after_two_infra_attempts(setup):
    cfg, _, _, _, pi, nc, _ = setup
    pi.mode = "infra"
    assert invoke(setup, extra=("--smoke", "--limit-tasks", "1")) == 0
    assert pi.calls == 2
    smoke = cfg.run_dir / "code_results.smoke.jsonl"
    smoke.unlink()  # a crash after the attempts but before the final row
    pi.calls = 0
    assert invoke(setup, extra=("--smoke", "--limit-tasks", "1")) == 0
    assert pi.calls == 0
    result = rows(setup, True)
    assert len(result) == 1 and result[0]["attempt"] == 2 and result[0]["hidden_pass"] is None
