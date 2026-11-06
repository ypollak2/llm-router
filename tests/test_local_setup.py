"""PR5: offline machine qualification, production repair path and installed data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import site
import sysconfig

import pytest

from llm_router import apply_tool as apply, local_setup as setup, zero_claude_edit as edit
from llm_router.commands.local_setup import cmd_local

REAL_INVENTORY = setup.inventory
REAL_GENERATE_EDITS = edit.generate_edits


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / ".llm-router"))
    monkeypatch.setenv("LLM_ROUTER_APPLY_REPAIR_S", "60")
    monkeypatch.setenv("LLM_ROUTER_APPLY", "off")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", "http://localhost:11434")
    monkeypatch.delenv("LLM_ROUTER_APPLY_REPAIR", raising=False)
    monkeypatch.setattr(setup, "inventory", lambda: [{"name": "test:latest", "digest": "abc"}])
    monkeypatch.setattr(edit, "edit_model", lambda **kw: "test:latest")
    monkeypatch.setattr(edit, "generate_edits", lambda *a, **kw: pytest.fail("unexpected model call"))


def record(**overrides):
    return dict(endpoint=setup.repair_endpoint(), model="test:latest", digest="sha256:abc", limit_s=60, n=3, passed_n=2,
                max_s=1, ts=1, sandbox=True, platform="test", **overrides)


def save(rec=None):
    rec = rec or record()
    setup.store.record_competence(rec["model"], "apply_repair", {"machine": rec})


def fake_model(monkeypatch, outcomes=(True, True, True)):
    calls = []
    def generate(task, contents, model, *, deadline_s, allow_model_swap):
        assert allow_model_swap is False
        assert model == "test:latest"
        assert "assert" in task
        name, old = next(iter(contents.items()))
        calls.append((name, old, deadline_s))
        if not outcomes[len(calls) - 1]:
            return None, [], "stub failure"
        new = old.replace("a - b", "a + b").replace("age > 18", "age >= 18").replace(".upper()", ".lower()")
        instruction = apply.EditInstruction(name, old, new)
        return {name: new}, [instruction], None
    monkeypatch.setattr(edit, "generate_edits", generate)
    return calls


@pytest.mark.parametrize("missing", ["ollama", "model", "sandbox"])
def test_prerequisite_failure_never_calls_model(monkeypatch, missing):
    monkeypatch.setattr(setup, "sandbox_probe", lambda root: None)
    if missing == "ollama":
        def down():
            raise setup.runtimes.ProbeFailed("down")
        monkeypatch.setattr(setup, "inventory", down)
    elif missing == "model":
        monkeypatch.setattr(setup, "inventory", lambda: [])
    else:
        def unavailable(root):
            raise apply.check_sandbox.SandboxUnavailable("missing")
        monkeypatch.setattr(setup, "sandbox_probe", unavailable)
    lines = []
    assert cmd_local(["setup", "--model", "test:latest"], out=lines.append) == 1
    assert len(setup.records()["test:latest"]["steps"]) == 5
    text = "\n".join(lines)
    assert "export LLM_ROUTER_APPLY=on" in text
    assert {"ollama": "ollama serve", "model": "ollama pull test:latest", "sandbox": "bubblewrap"}[missing] in text
    assert not setup.repair_qualified("test:latest", 60)


@pytest.mark.parametrize("outcomes,passed", [((True, True, True), 3), ((True, True, False), 2), ((True, False, False), 1), ((False, False, False), 0)])
def test_machine_check_through_shared_apply_path(monkeypatch, outcomes, passed):
    calls = fake_model(monkeypatch, outcomes)
    events = []
    def wrap(argv, root):
        events.append("wrap")
        return argv, lambda: events.append("cleanup")
    monkeypatch.setattr(apply.check_sandbox, "wrap", wrap)
    # Real pytest check and apply/rollback; only containment and generation are doubles.
    lines = []
    assert cmd_local(["setup"], out=lines.append) == (0 if passed >= 2 else 1)
    rec = setup.records()["test:latest"]
    assert len(calls) == 3 and rec["passed_n"] == passed
    assert rec["digest"] == "sha256:abc" and rec["n"] == 3
    assert rec["endpoint"] == setup.repair_endpoint()
    assert rec["limit_s"] == 60 and rec["sandbox"] is True
    assert events.count("wrap") == events.count("cleanup") == 4 + passed
    assert setup.repair_qualified("test:latest", 60) is (passed >= 2)
    assert "LLM_ROUTER_APPLY=off" in "\n".join(lines)
    assert os.environ["LLM_ROUTER_APPLY"] == "off"


@pytest.mark.parametrize("ci", ["true", "false"])
@pytest.mark.parametrize("mutation,allowed", [("endpoint", False), (None, True), ("larger limit", True), ("absent", False), ("digest", False), ("limit", False), ("failed", False), ("unknown digest", False), ("down", False)])
def test_live_gate_digest_limit_and_ci(tmp_path, monkeypatch, ci, mutation, allowed):
    monkeypatch.setenv("CI", ci)
    rec = record()
    if mutation == "larger limit":
        rec["limit_s"] = 120
    if mutation == "endpoint":
        rec["endpoint"] = "http://127.0.0.1:11435"
    if mutation == "digest":
        rec["digest"] = "sha256:old"
    if mutation == "limit":
        rec["limit_s"] = 59
    if mutation == "failed":
        rec["passed_n"] = 1
    if mutation != "absent":
        save(rec)
    if mutation == "unknown digest":
        monkeypatch.setattr(setup, "inventory", lambda: [{"name": "test:latest"}])
    if mutation == "down":
        def down():
            raise setup.runtimes.ProbeFailed("down")
        monkeypatch.setattr(setup, "inventory", down)
    called = []
    monkeypatch.setattr(apply, "_repair_once", lambda *args: called.append(args))
    repair = {"repair_passed": False}
    apply._local_repair(tmp_path, [sys.executable], [], "task", [], repair)
    assert bool(called) is allowed
    if not allowed:
        assert repair["repair_reason"] == "machine check not passed; run llm-router local setup"


def test_status_is_read_only_and_runs_no_probes(monkeypatch):
    rec = record()
    rec["steps"] = [["Ollama reachable", True, "stored"], ["machine check", True, "2/3"]]
    save(rec)
    before = setup.store.eligibility_path().read_bytes()
    monkeypatch.setattr(setup, "inventory", lambda: pytest.fail("status probed"))
    monkeypatch.setattr(edit, "edit_model", lambda **kw: pytest.fail("status discovery"))
    monkeypatch.setattr(setup, "sandbox_probe", lambda *a: pytest.fail("status sandbox"))
    lines = []
    assert cmd_local(["setup", "--status"], out=lines.append) == 0
    assert "ok: Ollama reachable; stored" in lines
    assert setup.store.eligibility_path().read_bytes() == before
    assert cmd_local(["setup", "--status", "--model", "absent"], out=lines.append) == 1


def test_writer_preserves_other_lanes_and_private_permissions():
    path = setup.store.eligibility_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"other": {"fitness": {}}, "test:latest": {"code": {"simple": {"old": True}}}}))
    save()
    data = json.loads(path.read_text())
    assert data["other"] == {"fitness": {}}
    assert data["test:latest"]["code"] == {"simple": {"old": True}}
    assert path.stat().st_mode & 0o777 == 0o600


def test_slow_passing_checks_do_not_qualify(monkeypatch):
    fake_model(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(setup.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(apply.check_sandbox, "wrap", lambda argv, root: (argv, lambda: None))
    def slow_check(*args, **kwargs):
        clock[0] += 61
        return 0, "late pass", True
    monkeypatch.setattr(apply, "_run_check", slow_check)
    rec = setup.machine_check("test:latest", "sha256:abc", 60, out=lambda s: None)
    assert rec["passed_n"] == 0
    assert rec["max_s"] == 61


def test_sandbox_required_even_when_off(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_APPLY_SANDBOX", "off")
    calls = []
    monkeypatch.setattr(apply.check_sandbox, "wrap", lambda argv, root: (argv, lambda: calls.append("cleanup")))
    setup.sandbox_probe(tmp_path)
    assert calls == ["cleanup"]


def test_machine_check_real_sandbox(monkeypatch):
    # CI's Linux containment lane requires this to execute, never silently skip.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        try:
            setup.sandbox_probe(Path(tmp).resolve())
        except apply.check_sandbox.SandboxUnavailable as exc:
            pytest.skip(str(exc))
    calls = fake_model(monkeypatch)
    rec = setup.machine_check("test:latest", "sha256:abc", 60, out=lambda s: None)
    assert len(calls) == 3 and rec["passed_n"] == 3


@pytest.mark.timeout(900)
def test_packaged_fixtures_and_installed_layout(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        if os.environ.get("CI"):
            pytest.fail("uv is required in CI to build the wheel; this test must run")
        pytest.skip("uv not available to build the wheel")

    def run(argv, **kwargs):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=600, **kwargs)
        assert result.returncode == 0, result.stderr[-3000:]
        return result

    root = Path(__file__).resolve().parents[1]
    dist = tmp_path / "dist"
    # As tests/test_agt_a0_agents_yaml_wheel.py: uv builds the wheel from the sdist.
    run([uv, "build", "--out-dir", str(dist)], cwd=root)
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 1
    venv = tmp_path / "venv"
    run([uv, "venv", "--python", sys.executable, str(venv)])
    vpy = venv / "bin" / "python"
    run([uv, "pip", "install", "--python", str(vpy), "--no-deps", str(wheels[0])])
    vsite = Path(run([str(vpy), "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"]).stdout.strip())
    dep_dirs = {sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"], *site.getsitepackages()}
    (vsite / "zz_test_deps.pth").write_text("\n".join(sorted(dep_dirs)) + "\n")
    probe = """
from pathlib import Path
import sys, llm_router
assert Path(llm_router.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
from importlib.resources import files
from llm_router.local_setup import FIXTURES
for name in FIXTURES:
    source = files('llm_router').joinpath('data', 'apply_repair', name)
    assert source.joinpath('module.py').is_file()
    assert source.joinpath('test_module.py').is_file()
from llm_router.commands.local_setup import cmd_local
assert cmd_local(['setup', '--status']) == 1
"""
    home, work = tmp_path / "isolated-home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV", "LLM_ROUTER_HOME")}
    env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    run([str(vpy), "-c", probe], cwd=work, env=env)


def test_cli_dispatch_and_help(monkeypatch):
    from llm_router import cli
    from llm_router.commands import local_setup as command
    assert "local" in cli._KNOWN_SUBCOMMANDS
    assert "local" in cli._OWN_HELP_ANYWHERE
    calls = []
    monkeypatch.setattr(command, "cmd_local", lambda args: calls.append(args) or 0)
    monkeypatch.setattr(sys, "argv", ["llm-router", "local", "setup", "--status"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0 and calls == [["setup", "--status"]]


def test_inventory_reads_tags_without_model_generation(monkeypatch):
    calls = []
    def tags(url, method, path):
        calls.append((url, method, path))
        return {"models": [{"name": "test:latest", "digest": "abc"}]}
    # Recover production function despite the autouse network stub.
    monkeypatch.setattr(setup.runtimes, "_strict_json", tags)
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11435")
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_URL")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11436")
    rows = REAL_INVENTORY()
    assert setup.digest_for(setup.model_row(rows, "test")) == "sha256:abc"
    assert calls == [("http://localhost:11436", "GET", "/api/tags")]


@pytest.mark.parametrize("name", setup.FIXTURES)
def test_bundled_fixture_starts_broken(tmp_path, name):
    from importlib.resources import files
    source = files("llm_router").joinpath("data", "apply_repair", name)
    for filename in ("module.py", "test_module.py"):
        (tmp_path / filename).write_bytes(source.joinpath(filename).read_bytes())
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "test_module.py"],
                       cwd=tmp_path, capture_output=True, text=True,
                       env=apply.check_sandbox.environment())
    assert p.returncode == 1 and "1 failed" in p.stdout, p.stdout + p.stderr


@pytest.mark.parametrize("endpoint", ["http://example.com:11434", "http://192.168.1.2:11434"])
def test_non_loopback_refused_by_gate_and_setup(tmp_path, monkeypatch, endpoint):
    save()
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", endpoint)
    monkeypatch.setattr(setup, "inventory", lambda: pytest.fail("remote inventory"))
    monkeypatch.setattr(apply, "_repair_once", lambda *a: pytest.fail("remote repair"))
    assert not setup.repair_qualified("test:latest", 60)
    repair = {"repair_passed": False}
    apply._local_repair(tmp_path, [sys.executable], [], "task", [], repair)
    assert repair["repair_reason"] == "local repair needs a loopback Ollama endpoint"
    monkeypatch.setattr(setup, "sandbox_probe", lambda root: None)
    lines = []
    assert cmd_local(["setup"], out=lines.append) == 1
    assert any(setup.ENDPOINT_REASON in line for line in lines)


@pytest.mark.parametrize("contents", [b"{broken json\n", b"[]", b"\xff"])
def test_setup_preserves_corrupt_store(monkeypatch, contents):
    path = setup.store.eligibility_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(contents)
    monkeypatch.setattr(setup, "inventory", lambda: pytest.fail("probe after corrupt store"))
    lines = []
    assert cmd_local(["setup"], out=lines.append) == 1
    assert path.read_bytes() == contents
    assert any("cannot read existing eligibility store" in line for line in lines)


def test_setup_preserves_unreadable_store(monkeypatch):
    save()
    path = setup.store.eligibility_path()
    before = path.read_bytes()
    read = Path.read_text
    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("injected read denial")
        return read(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", denied)
    lines = []
    assert cmd_local(["setup"], out=lines.append) == 1
    assert path.read_bytes() == before
    assert any("injected read denial" in line for line in lines)


@pytest.mark.parametrize("failure", ["permission", "json", "non_object", "encoding"])
def test_setup_preserves_store_on_locked_writer_read_failure(monkeypatch, failure):
    from contextlib import contextmanager

    path = setup.store.eligibility_path()
    path.parent.mkdir(parents=True)
    original = {"other": {"qa": {"simple": {"grade": "eligible"}}},
                "test:latest": {"fitness": {"keep": True}}}
    path.write_text(json.dumps(original))
    before = path.read_bytes()
    real_lock = setup.store.exclusive_lock
    real_read = Path.read_text
    locked = False
    reads = []

    @contextmanager
    def lock(target):
        nonlocal locked
        with real_lock(target) as acquired:
            locked = True
            try:
                yield acquired
            finally:
                locked = False

    def read(target, *args, **kwargs):
        if target == path:
            reads.append(locked)
            if locked:
                if failure == "permission":
                    raise PermissionError("writer-only denial")
                if failure == "encoding":
                    raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "writer-only decoding")
                return "{broken" if failure == "json" else "[]"
        return real_read(target, *args, **kwargs)

    monkeypatch.setattr(setup.store, "exclusive_lock", lock)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(setup, "sandbox_probe", lambda root: None)
    monkeypatch.setattr(setup, "machine_check", lambda *a: record())
    lines = []
    assert cmd_local(["setup"], out=lines.append) == 1
    assert reads == [False, False, True]
    assert path.read_bytes() == before
    assert json.loads(path.read_bytes()) == original
    assert any("cannot write eligibility store" in line for line in lines)
    assert not list(path.parent.glob(path.name + ".*.tmp"))


def test_strict_writer_initializes_missing_store_and_merges():
    path = setup.store.eligibility_path()
    assert not path.exists()
    assert setup.store.record_competence("other", "apply_repair",
                                         {"machine": {**record(), "model": "other"}}, strict=True)
    before = json.loads(path.read_bytes())
    assert setup.store.record_competence("test:latest", "apply_repair", {"machine": record()}, strict=True)
    assert json.loads(path.read_bytes())["other"] == before["other"]


@pytest.mark.parametrize("caller", ["machine", "live"])
def test_repair_pack_overflow_keeps_requested_model(tmp_path, monkeypatch, caller):
    from llm_router.hooks import direct_executor as executor
    from llm_router import local_context_guard as guard, pack_overflow
    monkeypatch.setenv("LLM_ROUTER_PACK_OVERFLOW", "1")
    monkeypatch.setattr(executor, "_local_num_ctx", lambda model: 512)
    monkeypatch.setattr(guard, "effective_window", lambda **kw: (512, "test"))
    # The separate context guard is off: exercise packing's model selection.
    monkeypatch.setattr(guard, "check_overflow", lambda *a, **kw: None)
    swaps = []
    monkeypatch.setattr(pack_overflow, "pick_larger_model", lambda *a: swaps.append(a) or "bigger:latest")
    called = []
    class Response:
        def __enter__(self):
            return iter([json.dumps({"message": {"content": "no edits"}, "done": True}).encode()])
        def __exit__(self, *args):
            pass
    def opened(req, timeout):
        payload = json.loads(req.data)
        assert guard.estimate_payload_tokens(payload) > 512
        called.append((req.full_url, payload["model"]))
        return Response()
    monkeypatch.setattr(executor, "_ollama_open", opened)
    monkeypatch.setattr(edit, "generate_edits", REAL_GENERATE_EDITS)
    monkeypatch.setattr(apply.check_sandbox, "wrap", lambda argv, root: (argv, lambda: None))
    monkeypatch.setattr(edit, "MAX_EDIT_ATTEMPTS", 1)
    if caller == "machine":
        setup.machine_check("test:latest", "sha256:abc", 60, out=lambda s: None)
        assert len(called) == 3
    else:
        save()
        path = tmp_path / "module.py"
        path.write_text("x = 1\n")
        apply._local_repair(tmp_path, [sys.executable], [path], "fix " * 2000, [], {"repair_passed": False})
        assert len(called) == 1
    assert not swaps
    assert all(model == "test:latest" and url == setup.repair_endpoint() + "/api/chat" for url, model in called)
    # The same overflowing payload still swaps for ordinary callers.
    executor.call_ollama("fix " * 2000, "test:latest")
    assert swaps and called[-1][1] == "bigger:latest"
