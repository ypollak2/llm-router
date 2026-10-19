from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
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
    # F113: the real grader needs a pytest-capable python that cannot import llm_router;
    # the guard itself is tested in test_p20_requal. Here the run's python stands in.
    monkeypatch.setattr(h, "grader_python", lambda env: sys.executable)
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
        "import os\nfrom pathlib import Path\n"
        "mode = 'pass'\ncalls = 0\nseen = []\nenvs = []\n"
        "def run_pi(prompt, cwd, timeout_s):\n"
        "    global calls\n    calls += 1\n"
        "    seen.append((PI_MODEL, globals().get('PI_CONFIG_DIR')))\n"
        "    envs.append(dict(os.environ))\n"
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
    support.freeze_amend1(cfg, c)
    support.freeze_amend2(cfg, c)
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
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):  # F115: the scrub needs a token to scan for
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "FAKE-OAUTH-TOKEN-default-0123456789")

    def call(argv, **kwargs):
        assert "--safe-mode" in argv
        assert "--no-session-persistence" in argv
        settings = Path(argv[argv.index("--settings") + 1])
        assert h.read_json(settings) == {"disableAllHooks": True}
        assert "--permission-mode" in argv and "--allowedTools" in argv
        assert "ANTHROPIC_BASE_URL" not in kwargs["env"]
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        env = kwargs["env"]
        assert env["HOME"] != os.environ["HOME"] and Path(env["HOME"]).is_dir()
        assert Path(env["HOME"]).is_relative_to(cfg.run_dir / "agent_home")
        assert set(env) <= h.AGENT_ENV_ALLOWED
        assert not Path(kwargs["cwd"]).is_relative_to(env["HOME"])
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
    assert h.arms(setup[1], True) == [h.QWEN38, "sonnet"]
    assert len(result) == 6
    assert len({(x["task_id"], x["arm"]) for x in result}) == 6
    ledger = h.jsonl(cfg.run_dir / "ledger.jsonl")
    assert len(ledger) == 3
    assert all(x["stage"] == "code" and x["set"] == "code" for x in ledger)
    assert "PRIVATE" not in json.dumps(ledger)
    assert "PRIVATE" not in capsys.readouterr().out
    assert h.ingest_code(cfg, setup[1], h.frozen(cfg),
                         cfg.run_dir / "code_results.raw.jsonl", Path(r.__file__)) == {"rows": 6}


def test_sonnet_mismatch(setup, monkeypatch):
    cfg = fake_claude(setup, monkeypatch, served="wrong")
    assert invoke(setup, "sonnet") == 0
    assert all(x["hidden_pass"] is None and x["attempt"] == 2 for x in rows(setup))
    assert all(x["infra_class"] == "served_model_mismatch" for x in rows(setup))
    assert len(h.jsonl(cfg.run_dir / "ledger.jsonl")) == 6


def test_quota_and_spend(setup, monkeypatch):
    cfg = fake_claude(setup, monkeypatch)
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
    cfg.machine.load = 4
    assert invoke(setup) == 3
    assert capsys.readouterr().err.strip() == "machine not quiet"


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


def test_rx_manifest_row_missing_refuses(setup):
    cfg, rx = setup[0], setup[6]
    manifest = rx / "fixtures/manifest.json"
    manifest.write_text("[]")
    h.write_json(cfg.run_dir / "pools/build_manifest.json",
                 {"sources": {"rx_fixtures_manifest": h.sha256_file(manifest)}})
    assert invoke(setup) == 2
    ids = {row["task_id"] for row in h.jsonl(cfg.run_dir / "code_results.raw.jsonl")}
    assert "task-1" not in ids and "task-2" not in ids


def test_code_gate_before_agent(setup, monkeypatch):
    cfg, _, _, _, pi, *_ = setup

    def refuse(constants):
        raise h.Refuse("refusing: test code gate")

    def forbidden(*args, **kwargs):
        pytest.fail("agent called before code gate")

    cfg.gate = refuse
    monkeypatch.setattr(r, "agent", forbidden)
    before = {p: p.read_bytes() for p in cfg.run_dir.rglob("*") if p.is_file()}
    assert invoke(setup) == 2
    assert invoke(setup, "sonnet") == 2
    assert pi.calls == 0 and not rows(setup)
    assert before == {p: p.read_bytes() for p in cfg.run_dir.rglob("*") if p.is_file()}


def test_arm_choices_are_amend2(setup):
    action = next(a for a in r.parser()._actions if a.dest == "arm")
    assert action.choices == [h.QWEN38, "sonnet"]
    for old in ("qwen3.6:35b-a3b-coding", "qwen3-coder:30b"):
        with pytest.raises(SystemExit):
            r.parser().parse_args(["--arm", old])


def test_pi_agent_thinking_off(setup):
    """AMEND2: the Pi agent runs from a run-dir config whose only thinking level is off,
    sent to Ollama's /v1 endpoint as reasoning_effort "none"."""
    cfg, _, _, arm, pi, *_ = setup
    assert arm == h.QWEN38
    assert invoke(setup) == 0
    config = cfg.run_dir / "pi_config"
    assert pi.seen and all(seen == (arm, str(config)) for seen in pi.seen)
    data = h.read_json(config / "models.json")
    provider = data["providers"]["ollama"]
    assert (provider["baseUrl"], provider["api"]) == (
        "http://localhost:11434/v1", "openai-completions")
    [model] = provider["models"]
    assert (model["id"], model["name"], model["reasoning"]) == (arm, arm, True)
    assert model["thinkingLevelMap"] == {"off": "none", "minimal": None, "low": None,
                                         "medium": None, "high": None, "xhigh": None,
                                         "max": None}
    assert model["contextWindow"] == h.local_models.num_ctx(arm) == 32768
    assert (config / "models.json").stat().st_mode & 0o777 == 0o600
    assert config.stat().st_mode & 0o777 == 0o700


def test_pi_agent_runs_with_scratch_home_and_minimal_env(setup, monkeypatch):
    """F104: the Pi child env is built from os.environ inside lib_pi, so the runner swaps
    os.environ for the call and restores it after."""
    cfg, _, _, arm, pi, *_ = setup
    caller = dict(os.environ)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(Path(caller["HOME"]) / "state"))
    assert invoke(setup) == 0
    assert pi.envs
    for env in pi.envs:
        assert env["HOME"] != caller["HOME"]
        assert Path(env["HOME"]).is_relative_to(cfg.run_dir / "agent_home")
        assert set(env) <= h.AGENT_ENV_ALLOWED
    assert os.environ["HOME"] == caller["HOME"]
    assert os.environ["ANTHROPIC_API_KEY"] == caller["ANTHROPIC_API_KEY"]


def test_grader_runs_on_agent_path_with_work_home(setup, monkeypatch):
    # Behavioural (F115): record what the grader is actually given for each rx_edit task.
    seen = []

    def recording(env):
        seen.append(dict(env))
        return sys.executable

    monkeypatch.setattr(h, "grader_python", recording)
    assert invoke(setup) == 0
    graded = [e for e in seen if e["HOME"] != str(setup[0].run_dir)]
    assert len(graded) == 2  # two rx_edit tasks
    for env in graded:
        assert env["PATH"] == h.agent_path(os.environ["PATH"])
        assert env["PYTHONNOUSERSITE"] == "1" and "VIRTUAL_ENV" not in env
        assert Path(env["HOME"]).name == "work"


def test_grader_preflight_refuses_before_any_agent_runs(setup, monkeypatch):
    pi = setup[4]

    def refuse(env):
        raise h.Refuse("refusing: no python on the agent PATH provides pytest for the grader")

    monkeypatch.setattr(h, "grader_python", refuse)
    assert invoke(setup) == 2
    assert pi.calls == 0 and not rows(setup)


def test_grader_refusal_mid_run_is_not_scored_as_agent_failure(setup, monkeypatch):
    calls = []

    def flaky(env):
        calls.append(1)
        if len(calls) > 1:  # preflight passes, the first real grading refuses
            raise h.Refuse("refusing: grader pytest probe timed out")
        return sys.executable

    monkeypatch.setattr(h, "grader_python", flaky)
    assert invoke(setup) == 2
    assert all(x["hidden_pass"] is not False for x in rows(setup) if x["task_id"] != "task-0")


def test_claude_token_in_agent_work_files_is_refused(setup, monkeypatch, capsys):
    token = "FAKE-OAUTH-TOKEN-abcdef0123456789"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    fake_claude(setup, monkeypatch)
    inner = r._claude_run

    def leaky(argv, **kwargs):
        done = inner(argv, **kwargs)
        (Path(kwargs["cwd"]) / "notes.txt").write_text(f"printenv: {token}\n")
        return done

    monkeypatch.setattr(r, "_claude_run", leaky)
    assert invoke(setup, "sonnet") == 2
    captured = capsys.readouterr()
    assert "token found in work files" in captured.err
    assert token not in captured.out + captured.err
    assert not rows(setup)


def test_claude_token_in_agent_output_is_refused(setup, monkeypatch, capsys):
    token = "FAKE-OAUTH-TOKEN-abcdef0123456789"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    fake_claude(setup, monkeypatch)
    inner = r._claude_run

    def leaky(argv, **kwargs):
        done = inner(argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, done.stdout, f"warn {token}")

    monkeypatch.setattr(r, "_claude_run", leaky)
    assert invoke(setup, "sonnet") == 2
    assert "token found in agent output" in capsys.readouterr().err
    assert not rows(setup)


def test_claude_run_without_token_leak_is_unaffected(setup, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "FAKE-OAUTH-TOKEN-abcdef0123456789")
    fake_claude(setup, monkeypatch)
    assert invoke(setup, "sonnet") == 0
    assert len(rows(setup)) == 3


def test_claude_timeout_still_scrubs_output_and_work(setup, monkeypatch, capsys):
    token = "FAKE-OAUTH-TOKEN-abcdef0123456789"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    fake_claude(setup, monkeypatch)

    def hang(argv, **kwargs):
        (Path(kwargs["cwd"]) / "notes.txt").write_text(f"printenv {token}\n")
        raise subprocess.TimeoutExpired(argv, 900, output=b"", stderr=b"partial")

    monkeypatch.setattr(r, "_claude_run", hang)
    assert invoke(setup, "sonnet") == 2
    captured = capsys.readouterr()
    assert "token found in work files" in captured.err
    assert token not in captured.out + captured.err
    assert "rotate the token" in captured.err and "claude setup-token" in captured.err
    assert not rows(setup)


def test_claude_timeout_output_is_scanned(setup, monkeypatch, capsys):
    token = "FAKE-OAUTH-TOKEN-abcdef0123456789"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    fake_claude(setup, monkeypatch)

    def hang(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 900, output=b"", stderr=f"x {token}".encode())

    monkeypatch.setattr(r, "_claude_run", hang)
    assert invoke(setup, "sonnet") == 2
    assert "token found in agent output" in capsys.readouterr().err


def test_scrub_hit_purges_work_and_home_inside_run_dir_only(setup, tmp_path):
    cfg = setup[0]
    token = "FAKE-OAUTH-TOKEN-abcdef0123456789"
    work = cfg.run_dir / "code_runs" / "w"
    home = cfg.run_dir / "agent_home" / "x"
    for d in (work, home):
        d.mkdir(parents=True)
    subprocess.run(["git", "-C", str(work), "init", "-q"], check=True)
    (home / "t.txt").write_text(token)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    with pytest.raises(h.Refuse, match="token found in agent scratch home"):
        r.scrub_claude_artifacts(token, work, home, "", cfg.run_dir)
    assert not home.exists() and not work.exists()
    r.purge_inside(cfg.run_dir, outside, cfg.run_dir)
    assert (outside / "keep.txt").exists() and cfg.run_dir.exists()


def test_scrub_without_token_fails_closed(setup):
    cfg = setup[0]
    with pytest.raises(h.Refuse, match="no token to scan for"):
        r.scrub_claude_artifacts(None, cfg.run_dir, cfg.run_dir, "", cfg.run_dir)


def test_scrub_git_failure_still_purges_and_refuses(setup):
    """F117: a broken .git (or non-UTF-8 output, timeout) is a Refuse, and the purge runs."""
    cfg = setup[0]
    token = "FAKE-OAUTH-TOKEN-abcdef0123456789"
    work = cfg.run_dir / "code_runs" / "w"
    home = cfg.run_dir / "agent_home" / "x"
    for d in (work, home):
        d.mkdir(parents=True)
    r.git(work, "init", "-q")
    (work / ".git" / "HEAD").write_text("garbage\n")
    with pytest.raises(h.Refuse, match="refusing"):
        r.scrub_claude_artifacts(token, work, home, "", cfg.run_dir)
    assert not home.exists() and not work.exists()


def test_scrub_non_utf8_history_is_scanned_not_a_crash(setup):
    cfg = setup[0]
    token = "FAKE-OAUTH-TOKEN-abcdef0123456789"
    work = cfg.run_dir / "code_runs" / "w"
    home = cfg.run_dir / "agent_home" / "x"
    for d in (work, home):
        d.mkdir(parents=True)
    r.git(work, "init", "-q")
    (work / "f.txt").write_bytes(b"\xff\xfe caf\xe9\n")
    r.git(work, "add", ".")
    r.git(work, "-c", "user.name=T", "-c", "user.email=t@l", "-c", "commit.gpgsign=false",
          "commit", "-qm", "base")
    (work / "f.txt").write_bytes(b"\xff\xfe caf\xe9 " + token.encode() + b"\n")
    with pytest.raises(h.Refuse, match="token found in (patch|work files)"):
        r.scrub_claude_artifacts(token, work, home, "", cfg.run_dir)
    # clean non-UTF-8 repo: no exception
    work2 = cfg.run_dir / "code_runs" / "w2"
    work2.mkdir(parents=True)
    r.git(work2, "init", "-q")
    (work2 / "f.txt").write_bytes(b"\xff\xfe\n")
    r.git(work2, "add", ".")
    r.git(work2, "-c", "user.name=T", "-c", "user.email=t@l", "-c", "commit.gpgsign=false",
          "commit", "-qm", "base")
    (work2 / "f.txt").write_bytes(b"\xe9\n")
    r.scrub_claude_artifacts(token, work2, home, "", cfg.run_dir)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores chmod 000")
def test_scrub_unreadable_dir_refuses_and_purge_removes_it(setup):
    cfg = setup[0]
    token = "FAKE-OAUTH-TOKEN-abcdef0123456789"
    work = cfg.run_dir / "code_runs" / "w"
    home = cfg.run_dir / "agent_home" / "x"
    (home / "locked").mkdir(parents=True)
    work.mkdir(parents=True)
    r.git(work, "init", "-q")
    (home / "locked" / "t.txt").write_text(token)
    (home / "locked").chmod(0)
    with pytest.raises(h.Refuse, match="unreadable directory in agent scratch home"):
        r.scrub_claude_artifacts(token, work, home, "", cfg.run_dir)
    assert not home.exists() and not work.exists()


def test_purge_inside_symlinked_parent_and_relative_path(setup, tmp_path, monkeypatch):
    cfg = setup[0]
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "keep.txt").write_text("keep")
    (cfg.run_dir / "link").symlink_to(outside)
    r.purge_inside(cfg.run_dir, cfg.run_dir / "link" / "sub", cfg.run_dir / "link")
    assert (outside / "sub" / "keep.txt").exists()
    inner = cfg.run_dir / "inner"
    inner.mkdir()
    (inner / "x.txt").write_text("x")
    monkeypatch.chdir(cfg.run_dir)
    r.purge_inside(cfg.run_dir, "inner")
    assert not inner.exists() and cfg.run_dir.exists()


# ---- F117 repair 1 ------------------------------------------------------------------------

def test_scrub_unexpected_scan_error_still_purges(setup, monkeypatch):
    """The generic `except Exception` branch: purge, then a Refuse that carries no detail."""
    cfg = setup[0]
    work = cfg.run_dir / "code_runs" / "w"
    home = cfg.run_dir / "agent_home" / "x"
    for d in (work, home):
        d.mkdir(parents=True)
    r.git(work, "init", "-q")
    (work / "f.txt").write_text("one\n")
    r.git(work, "add", ".")
    r.git(work, "-c", "user.name=T", "-c", "user.email=t@l", "-c", "commit.gpgsign=false",
          "commit", "-qm", "base")

    def boom(*a, **k):
        raise PermissionError("secret path /x/y")

    monkeypatch.setattr(h, "scan_new_git_objects", boom)
    with pytest.raises(h.Refuse, match="artifact scan failed") as info:
        r.scrub_claude_artifacts("FAKE-OAUTH-TOKEN-abcdef0123456789", work, home, "", cfg.run_dir)
    assert "/x/y" not in str(info.value)
    assert not work.exists() and not home.exists()


def test_scrub_unreachable_agent_object_is_found(setup):
    cfg = setup[0]
    token = "FAKE-OAUTH-TOKEN-abcdef0123456789"
    work = cfg.run_dir / "code_runs" / "w"
    home = cfg.run_dir / "agent_home" / "x"
    for d in (work, home):
        d.mkdir(parents=True)
    r.git(work, "init", "-q")
    (work / "f.txt").write_text("one\n")
    r.git(work, "add", ".")
    r.git(work, "-c", "user.name=T", "-c", "user.email=t@l", "-c", "commit.gpgsign=false",
          "commit", "-qm", "base")
    baseline = h.git_object_ids(work)
    side = cfg.run_dir / "t.txt"
    side.write_text(token)
    r.git(work, "hash-object", "-w", str(side))
    with pytest.raises(h.Refuse, match="token found in git objects"):
        r.scrub_claude_artifacts(token, work, home, "", cfg.run_dir, baseline)
    assert not work.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores chmod 000")
def test_open_permissions_never_chmods_through_a_swapped_symlink(setup, tmp_path, monkeypatch):
    cfg = setup[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    base = cfg.run_dir / "w"
    (base / "sub" / "deep").mkdir(parents=True)
    (base / "sub" / "deep").chmod(0)
    real_listdir = os.listdir
    swapped = []

    def listdir_then_swap(fd):
        names = real_listdir(fd)
        if "sub" in names and not swapped:  # the agent swaps `sub` after the harness listed it
            swapped.append(1)
            (base / "sub" / "deep").chmod(0o700)
            shutil.rmtree(base / "sub")
            (base / "sub").symlink_to(outside)
        return names

    monkeypatch.setattr(os, "listdir", listdir_then_swap)
    assert r._delete_tree(cfg.run_dir, base)
    monkeypatch.undo()
    assert swapped and (outside.stat().st_mode & 0o777) == 0o755 and outside.exists()
    assert not base.exists()  # the swapped-in link was removed, not followed
    # unswapped: a chmod 000 directory is opened up and removed with its content
    (cfg.run_dir / "w2" / "a" / "b").mkdir(parents=True)
    (cfg.run_dir / "w2" / "a" / "b" / "t.txt").write_text("x")
    (cfg.run_dir / "w2" / "a").chmod(0)
    assert r._delete_tree(cfg.run_dir, cfg.run_dir / "w2")
    assert not (cfg.run_dir / "w2").exists()


def _deep_chain(top, depth):
    """A directory chain `depth` levels deep (names of one letter), built through dir fds so the
    path is far longer than PATH_MAX. Returns the bottom directory's relative parts."""
    top.mkdir(parents=True)
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(depth):
            os.mkdir("a", 0o700, dir_fd=fd)
            nxt = os.open("a", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores chmod 000")
def test_deep_chain_is_purged_without_recursion_and_refuse_is_kept(setup):
    """F117 round 2: a 2000-deep chain with a chmod 000 dir made the recursive purge raise
    RecursionError and leave the token behind."""
    cfg = setup[0]
    token = "FAKE-OAUTH-TOKEN-abcdef0123456789"
    work = cfg.run_dir / "code_runs" / "w"
    home = cfg.run_dir / "agent_home" / "x"
    home.mkdir(parents=True)
    work.mkdir(parents=True)
    r.git(work, "init", "-q")
    bottom = _deep_chain(home / "deep", 2000)
    try:
        fd = os.open("t.txt", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=bottom)
        os.write(fd, token.encode())
        os.close(fd)
    finally:
        os.close(bottom)
    (home / "deep" / "a").chmod(0)
    with pytest.raises(h.Refuse) as info:
        r.scrub_claude_artifacts(token, work, home, "", cfg.run_dir)
    assert token not in str(info.value) and "rotate the token" in str(info.value)
    assert not home.exists() and not work.exists()  # token file gone with its tree


_AGENT = """
import os, sys, time
mode, pidfile, outfile = sys.argv[1:4]

def writer(path):
    open(path, "w").write(str(os.getpid()))
    while True:
        open(outfile, "a").write("x")
        time.sleep(0.05)

def wait_for(path):
    for _ in range(200):
        if os.path.exists(path) and open(path).read():
            return
        time.sleep(0.05)

if mode == "group":       # a child that stays in the agent's group
    if os.fork() == 0:
        writer(pidfile)
    wait_for(pidfile)
    time.sleep(1.0)
elif mode == "session":   # a setsid child whose parent (the agent) is still alive
    if os.fork() == 0:
        os.setsid()
        writer(pidfile)
    wait_for(pidfile)
    time.sleep(1.0)
elif mode == "orphan":    # double fork + setsid, the agent exits at once
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            if len(sys.argv) > 4:                 # stdio to /dev/null
                null = os.open(os.devnull, os.O_RDWR)
                for fd in (0, 1, 2):
                    os.dup2(null, fd)
            writer(pidfile)
        os._exit(0)
    wait_for(pidfile)
elif mode == "hang":
    if os.fork() == 0:
        writer(pidfile)
    wait_for(pidfile)
    time.sleep(60)
print("done")
"""


def _agent_argv(tmp_path, mode, *extra):
    return [sys.executable, "-c", _AGENT, mode, str(tmp_path / "pid"), str(tmp_path / "out"),
            *extra]


def _run(tmp_path, mode, timeout=30, *extra):
    (tmp_path / "home").mkdir(exist_ok=True)
    env = {**os.environ, "HOME": str(tmp_path / "home")}
    return r.run_agent_process(_agent_argv(tmp_path, mode, *extra), input="", timeout=timeout,
                               cwd=tmp_path, env=env)


@pytest.fixture
def bystander():
    """A process the test starts outside the agent's group and tree: it must never be killed."""
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    yield proc
    proc.kill()
    proc.wait()


@pytest.mark.parametrize("mode", ["group", "session", "hang"])
def test_agent_tree_is_dead_before_run_returns_and_bystander_lives(tmp_path, bystander, mode):
    """F117: killpg while the leader is unreaped, plus start-time-verified tracked pids."""
    import time
    if mode == "hang":
        with pytest.raises(subprocess.TimeoutExpired):
            _run(tmp_path, mode, timeout=3)
    else:
        done = _run(tmp_path, mode)
        assert done.returncode == 0 and "done" in done.stdout
    pid = int(Path(tmp_path / "pid").read_text())
    assert pid not in h.process_table() or h.process_table()[pid].zombie
    size = (tmp_path / "out").stat().st_size
    time.sleep(0.4)
    assert (tmp_path / "out").stat().st_size == size  # nothing writes after the run returned
    assert bystander.poll() is None  # an unrelated process is never signalled


@pytest.mark.parametrize("stdio", ["inherited", "devnull"])
def test_detached_orphan_makes_the_run_refuse_not_hang(tmp_path, monkeypatch, stdio):
    """F117 round 2: a double-forked setsid orphan is not provably the agent's, so it is not
    killed; because it still holds the work dir the run refuses (and never hangs on its pipes)."""
    import time
    monkeypatch.setattr(r, "DRAIN_SECONDS", 1.0)
    extra = ("null",) if stdio == "devnull" else ()
    started = time.monotonic()
    try:
        with pytest.raises(h.AgentSurvived, match="agent process survived"):
            _run(tmp_path, "orphan", 30, *extra)
        assert time.monotonic() - started < 20
    finally:  # the test started this process, the test ends it
        pidfile = tmp_path / "pid"
        if pidfile.exists() and pidfile.read_text():
            with contextlib.suppress(OSError):
                os.kill(int(pidfile.read_text()), 9)


def test_stop_never_signals_a_pid_it_cannot_reverify(bystander):
    """A recorded pid whose start time differs (the pid was reused) is left alone."""
    leader = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        known = {bystander.pid: "Thu Jan  1 00:00:00 1970"}
        h.stop_agent_processes(leader.pid, known)
        assert leader.wait(timeout=10) == -9  # the leader's group was killed, unreaped until now
        assert bystander.poll() is None
        # the same pid with its real start time IS the agent's, so it is killed
        known = {bystander.pid: h.process_table()[bystander.pid].start}
        second = subprocess.Popen(["sleep", "300"], start_new_session=True)
        h.stop_agent_processes(second.pid, known)
        second.wait(timeout=10)
        assert bystander.wait(timeout=10) == -9
    finally:
        leader.kill()
        leader.wait()


def test_stop_does_not_killpg_a_reaped_leader(bystander, monkeypatch):
    """With the leader gone from the table, no group signal is sent (its pgid may be reused)."""
    sent = []
    monkeypatch.setattr(os, "killpg", lambda *a: sent.append(a))
    gone = subprocess.Popen(["true"], start_new_session=True)
    gone.wait()
    h.stop_agent_processes(gone.pid, {})
    assert not sent and bystander.poll() is None


def test_unreadable_process_list_refuses(monkeypatch):
    monkeypatch.setattr(h, "_ps_lines", lambda *a: [])
    with pytest.raises(h.AgentSurvived):
        h.stop_agent_processes(1, {})


def test_unverifiable_holder_check_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "_open_paths_of_others", lambda dirs, seconds: None)
    with pytest.raises(h.AgentSurvived, match="cannot rule out"):
        _run(tmp_path, "group")


def test_agent_survivor_skips_the_scan(setup, monkeypatch, capsys):
    token = "FAKE-OAUTH-TOKEN-abcdef0123456789"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    fake_claude(setup, monkeypatch)
    scanned = []

    def survivor(argv, **kwargs):
        raise h.AgentSurvived("refusing: agent process survived")

    monkeypatch.setattr(r, "_claude_run", survivor)
    monkeypatch.setattr(r, "scrub_claude_artifacts", lambda *a, **k: scanned.append(1))
    assert invoke(setup, "sonnet") == 2
    assert "agent process survived" in capsys.readouterr().err and not scanned


def test_noncloud_worktree_is_removed_from_the_trusted_clone(setup, tmp_path, monkeypatch):
    calls = []
    real_git = r.git

    def spy(work, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            calls.append(Path(work))
        return real_git(work, *args, **kwargs)

    monkeypatch.setattr(r, "git", spy)
    invoke(setup)
    assert calls and all(c == tmp_path / "repo" for c in calls)
