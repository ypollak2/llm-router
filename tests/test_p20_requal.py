from __future__ import annotations

import fcntl
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
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
_REAL_RUN = subprocess.run  # the F113 interpreter probes are local, side-effect free


def _load_harness():
    # A unique name that shadows no real module; registered because @dataclass resolves
    # string annotations through sys.modules[cls.__module__].
    spec = importlib.util.spec_from_file_location(
        "_test_p20_requal_harness", ROOT / "scripts/p20_requal.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


h = _load_harness()
NOW = 1_900_000_000.0
PAST = "2026-01-01T00:00:00Z"
AMEND_PAST = "2027-01-01T00:00:00Z"
AMEND2_PAST = "2028-01-01T00:00:00Z"
AMEND3_PAST = "2029-01-01T00:00:00Z"
OLD_LOCAL = ("qwen3.6:35b-a3b-coding", "qwen3-coder:30b", "qwen3.5:latest")
GIT_RUN = subprocess.run


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / "state"))
    monkeypatch.delenv("P20_REQUAL_DIR", raising=False)
    monkeypatch.setattr(h, "OLLAMA_LOCK", tmp_path / "default-ollama.lock")

    def forbidden(*args, **kwargs):
        raise AssertionError("real external call")

    def probe_only(argv, *args, **kwargs):
        if list(argv[1:3]) == ["-I", "-c"]:  # python -I -c <probe>: the F113 import guard
            return _REAL_RUN(argv, *args, **kwargs)
        return forbidden()

    monkeypatch.setattr(h.subprocess, "run", probe_only)
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
        # Works on the draft (FILL-AT-FREEZE) and on the frozen prereg (real fill-ins).
        text, count = re.subn(rf"(?m)^{re.escape(key)}=.*$", f"{key}={constants[key]}", text)
        assert count == 1, key
    cfg.prereg.write_text(text)
    cfg.prereg.with_suffix(".sha256").write_text(
        f"{h.sha256_file(cfg.prereg)}  {cfg.prereg.name}\nfrozen_utc {date}\n"
    )


def freeze_amend1(cfg, constants, *, text=None, date=AMEND_PAST):
    if text is None:
        text = ("# AMEND1\n\n```text P20-AMEND1-CONSTANTS\n"
                f"REPO_SHA={'a' * 40}\n"
                f"CODE_RUNNER_SHA256={constants['CODE_RUNNER_SHA256']}\n```\n")
    cfg.amend1.write_text(text)
    cfg.amend1.with_suffix(".sha256").write_text(
        f"{h.sha256_file(cfg.amend1)}  {cfg.amend1.name}\nfrozen_utc {date}\n"
    )


def amend2_text(constants, **values):
    block = {**h.AMEND2_FIXED, "REPO_SHA": "c" * 40,
             "CODE_RUNNER_SHA256": constants["CODE_RUNNER_SHA256"], **values}
    return ("# AMEND2\n\n```text P20-AMEND2-CONSTANTS\n"
            + "".join(f"{k}={v}\n" for k, v in block.items()) + "```\n")


def freeze_amend2(cfg, constants, *, text=None, date=AMEND2_PAST):
    cfg.amend2.write_text(text if text is not None else amend2_text(constants))
    cfg.amend2.with_suffix(".sha256").write_text(
        f"{h.sha256_file(cfg.amend2)}  {cfg.amend2.name}\nfrozen_utc {date}\n"
    )


def amend3_text(constants, **values):
    block = {**h.AMEND3_FIXED, "REPO_SHA": "e" * 40,
             "CODE_RUNNER_SHA256": constants["CODE_RUNNER_SHA256"], **values}
    return ("# AMEND3\n\n```text P20-AMEND3-CONSTANTS\n"
            + "".join(f"{k}={v}\n" for k, v in block.items()) + "```\n")


def freeze_amend3(cfg, constants, *, text=None, date=AMEND3_PAST):
    cfg.amend3.write_text(text if text is not None else amend3_text(constants))
    cfg.amend3.with_suffix(".sha256").write_text(
        f"{h.sha256_file(cfg.amend3)}  {cfg.amend3.name}\nfrozen_utc {date}\n"
    )


AMEND4_PAST = "2030-01-01T00:00:00Z"


def amend4_text(**values):
    block = {"REPO_SHA": "f" * 40, **values}
    return ("# AMEND4\n\n```text P20-AMEND4-CONSTANTS\n"
            + "".join(f"{k}={v}\n" for k, v in block.items()) + "```\n")


def freeze_amend4(cfg, *, text=None, date=AMEND4_PAST):
    cfg.amend4.write_text(text if text is not None else amend4_text())
    cfg.amend4.with_suffix(".sha256").write_text(
        f"{h.sha256_file(cfg.amend4)}  {cfg.amend4.name}\nfrozen_utc {date}\n"
    )


def effective(constants):
    """The constants frozen() returns for the factory's prereg, AMEND1 and AMEND2."""
    return h.amended({**constants, "REPO_SHA": "a" * 40}, {
        **h.AMEND2_FIXED, "REPO_SHA": "c" * 40,
        "CODE_RUNNER_SHA256": constants["CODE_RUNNER_SHA256"],
    })


class Ollama:
    def __init__(self):
        self.calls = []
        self.thinks = []
        self.unloaded = []
        self.ids = {m: set(h.STATIC[f"ID_{m}"].split(","))
                    for m in h.STATIC["LOCAL_MODELS"].split(",")}
        self.resident = []
        self.fail = False

    def list_ids(self):
        return self.ids

    def ps(self):
        return self.resident

    def chat(self, model, messages, *, timeout, options, think):
        self.calls.append((model, messages, timeout, options))
        self.thinks.append(think)
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
            run_dir, prereg=run_dir / "frozen.md", now=lambda: NOW,
            lock_path=tmp_path / "ollama.lock",
            sleep=lambda _: None, ollama=Ollama(), claude=Claude(), machine=Machine(),
            quota=lambda: {"session_pct": 0, "weekly_pct": 0, "age_s": 0},
            pack_fn=lambda item, name, window: [{"role": "user", "content": item["request"]}],
            git_sha=lambda: "deadbeef", amend1=run_dir / "AMEND1.md",
            amend2=run_dir / "AMEND2.md", amend3=run_dir / "AMEND3.md",
            amend4=run_dir / "AMEND4.md",
            gate=lambda constants: None,
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
        freeze_amend1(cfg, constants)
        freeze_amend2(cfg, constants)
        return cfg, effective(constants), pools
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
        "claude", "-p", "--safe-mode", "--no-session-persistence",
        "--settings", str(tmp_path / "claude_settings.json"),
        "--model", "test-sonnet", "--output-format", "json", "--tools", "",
    ]
    path = Path(args[args.index("--settings") + 1])
    assert json.loads(path.read_text()) == {"disableAllHooks": True}
    assert path.stat().st_mode & 0o777 == 0o600
    assert "--tools" not in h.claude_argv("test-sonnet", tmp_path, tools=None)
    env = h.claude_env(h.scratch_home(tmp_path / "agent"))
    assert "ANTHROPIC_BASE_URL" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["LLM_ROUTER_SESSION_KIND"] == "research"


def plant_real_home(tmp_path, monkeypatch):
    """A fake 'real' HOME holding the files an uninstall-shaped deletion would remove."""
    real = tmp_path / "realhome"
    markers = [real / ".claude/settings.json", real / ".llm-router/proxy_default.json",
               real / "Library/LaunchAgents/com.llm_router.proxy.plist"]
    for m in markers:
        m.parent.mkdir(parents=True, exist_ok=True)
        m.write_text("MARKER")
    monkeypatch.setenv("HOME", str(real))
    monkeypatch.setattr(h, "real_home", lambda: real.resolve())
    return real, markers


def test_agent_env_is_minimal_and_scratch(tmp_path, monkeypatch):
    real, _ = plant_real_home(tmp_path, monkeypatch)
    for k, v in {"ANTHROPIC_BASE_URL": "x", "ANTHROPIC_API_KEY": "x", "LLM_ROUTER_HOME": "x",
                 "LLM_ROUTER_ENFORCE": "x", "OPENAI_API_KEY": "x", "PATH": "/bin", "LANG": "C",
                 "CLAUDE_CODE_OAUTH_TOKEN": "tok"}.items():
        monkeypatch.setenv(k, v)
    scratch = h.scratch_home(tmp_path / "scratch")
    pi_env = h.agent_env(scratch)
    claude_env = h.claude_env(scratch)
    for env in (pi_env, claude_env):
        assert Path(env["HOME"]) != real and env["HOME"] == str(scratch)
        assert set(env) <= h.AGENT_ENV_ALLOWED
        assert not [k for k in env if k.startswith("LLM_ROUTER_") and k != "LLM_ROUTER_SESSION_KIND"]
        assert "OPENAI_API_KEY" not in env and "ANTHROPIC_BASE_URL" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in pi_env and claude_env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert "LLM_ROUTER_SESSION_KIND" not in pi_env
    assert scratch.stat().st_mode & 0o777 == 0o700


def test_credentials_file_sets_token_in_claude_env_only(tmp_path, monkeypatch, capsys):
    plant_real_home(tmp_path, monkeypatch)
    source = tmp_path / "cred-source"
    source.write_text("SECRET-VALUE\n")
    source.chmod(0o600)
    monkeypatch.setenv(h.CLAUDE_CREDENTIALS_ENV, str(source))
    scratch = h.scratch_home(tmp_path / "scratch")
    env = h.claude_env(scratch)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "SECRET-VALUE"
    assert not (scratch / ".claude").exists()  # the value is never written anywhere
    pi_env = h.agent_env(h.scratch_home(tmp_path / "s2"))
    assert pi_env.keys() <= h.AGENT_ENV_ALLOWED and "CLAUDE_CODE_OAUTH_TOKEN" not in pi_env
    assert not (tmp_path / "s2/.claude").exists()
    monkeypatch.setenv(h.CLAUDE_CREDENTIALS_ENV, str(tmp_path / "missing-SECRET-PATH"))
    with pytest.raises(h.Refuse) as err:
        h.claude_env(h.scratch_home(tmp_path / "s3"))
    assert "SECRET" not in str(err.value) and "SECRET" not in capsys.readouterr().out


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_credentials_file_refused_when_group_or_world_accessible(tmp_path, monkeypatch, mode):
    plant_real_home(tmp_path, monkeypatch)
    source = tmp_path / "cred-source"
    source.write_text("SECRET-VALUE")
    source.chmod(mode)
    monkeypatch.setenv(h.CLAUDE_CREDENTIALS_ENV, str(source))
    with pytest.raises(h.Refuse, match="group/world") as err:
        h.claude_env(h.scratch_home(tmp_path / "scratch"))
    assert "SECRET" not in str(err.value) and str(source) not in str(err.value)


def test_credentials_file_empty_refused(tmp_path, monkeypatch):
    plant_real_home(tmp_path, monkeypatch)
    source = tmp_path / "cred-source"
    source.write_text("  \n")
    source.chmod(0o600)
    monkeypatch.setenv(h.CLAUDE_CREDENTIALS_ENV, str(source))
    with pytest.raises(h.Refuse, match="empty"):
        h.claude_env(h.scratch_home(tmp_path / "scratch"))


def python_stub(directory: Path, name: str, importable: bool) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / name
    exe.write_text(f"#!/bin/sh\nexit {1 if importable else 0}\n")
    exe.chmod(0o755)
    return directory


def test_import_guard_refuses_python_that_imports_llm_router(tmp_path, monkeypatch):
    plant_real_home(tmp_path, monkeypatch)
    bad = python_stub(tmp_path / "bad", "python3", importable=True)
    clean = python_stub(tmp_path / "clean", "python", importable=False)
    env = {"HOME": str(tmp_path / "s"), "PATH": f"{clean}{os.pathsep}{bad}"}
    with pytest.raises(h.Refuse, match="importable"):
        h.assert_agent_env(env)
    env["PATH"] = str(clean)
    h.assert_agent_env(env)  # only the clean python: passes
    # A real interpreter that can import llm_router (this test process's) is refused.
    real_py = tmp_path / "real"
    real_py.mkdir()
    (real_py / "python3").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (real_py / "python3").chmod(0o755)
    with pytest.raises(h.Refuse, match="importable"):
        h.assert_agent_env({"HOME": str(tmp_path / "s"), "PATH": str(real_py)})


def test_import_guard_probe_found_a_python_to_check(tmp_path):
    # An empty set passes everything: the stubs must actually be discovered.
    one = python_stub(tmp_path / "one", "python3", importable=False)
    two = python_stub(tmp_path / "two", "python", importable=False)
    assert len(h.agent_pythons(f"{one}{os.pathsep}{two}")) == 2


def test_agent_env_sets_pythonnousersite(tmp_path, monkeypatch):
    plant_real_home(tmp_path, monkeypatch)
    for env in (h.agent_env(h.scratch_home(tmp_path / "a")), h.claude_env(h.scratch_home(tmp_path / "b"))):
        assert env["PYTHONNOUSERSITE"] == "1"
    assert "PYTHONNOUSERSITE" in h.AGENT_ENV_ALLOWED


def test_grader_python_requires_clean_interpreter_with_pytest(tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    exe = clean / "python3"
    exe.write_text(
        "#!/bin/sh\ncase \"$*\" in *find_spec*) exit 0;; *'import pytest'*) exit 0;; esac\nexit 0\n"
    )
    exe.chmod(0o755)
    assert h.grader_python({"PATH": str(clean)}) == str(exe)
    exe.write_text("#!/bin/sh\ncase \"$*\" in *find_spec*) exit 0;; esac\nexit 1\n")
    with pytest.raises(h.Refuse, match="pytest"):
        h.grader_python({"PATH": str(clean)})
    bad = python_stub(tmp_path / "bad", "python3", importable=True)
    with pytest.raises(h.Refuse, match="importable"):
        h.grader_python({"PATH": str(bad)})


def test_agent_env_guard_refuses_real_home_and_strays(tmp_path, monkeypatch):
    real, _ = plant_real_home(tmp_path, monkeypatch)
    with pytest.raises(h.Refuse, match="real HOME"):
        h.scratch_home(real)
    with pytest.raises(h.Refuse, match="real HOME"):
        h.assert_agent_env({"HOME": str(real), "PATH": "/bin"})
    with pytest.raises(h.Refuse, match="real HOME"):  # caller's $HOME even if not the pw home
        monkeypatch.setattr(h, "real_home", lambda: tmp_path / "elsewhere")
        h.assert_agent_env({"HOME": str(real)})
    with pytest.raises(h.Refuse, match="unexpected key"):
        h.assert_agent_env({"HOME": str(tmp_path / "s"), "LLM_ROUTER_HOME": "x"})
    with pytest.raises(h.Refuse, match="no HOME"):
        h.assert_agent_env({"PATH": "/bin"})


UNINSTALL_STUB = (
    "import os, shutil, pathlib\n"
    "home = pathlib.Path(os.path.expanduser('~'))\n"
    "for rel in ('.claude/settings.json', '.llm-router/proxy_default.json',\n"
    "            'Library/LaunchAgents/com.llm_router.proxy.plist'):\n"
    "    (home / rel).parent.mkdir(parents=True, exist_ok=True)\n"
    "    (home / rel).write_text('x'); (home / rel).unlink()\n"
    "for d in ('.claude', '.llm-router', 'Library'):\n"
    "    shutil.rmtree(home / d, ignore_errors=True)\n"
    "(home / 'ran').write_text('1')\n"
)


def test_uninstall_shaped_agent_cannot_touch_real_home(tmp_path, monkeypatch):
    real, markers = plant_real_home(tmp_path, monkeypatch)
    scratch = h.scratch_home(tmp_path / "scratch")
    for env in (h.claude_env(scratch), h.agent_env(scratch)):
        out = GIT_RUN([sys.executable, "-c", UNINSTALL_STUB], env=env, check=False,
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert all(m.read_text() == "MARKER" for m in markers)
        assert (scratch / "ran").is_file()  # the stub ran, against scratch


def test_constants_and_real_freeze(factory, tmp_path):
    parsed = h.parse_constants(h.PREREG.read_text())
    assert {k: v for k, v in parsed.items() if k not in h.FILL_KEYS} == h.CONSTANTS
    assert tuple(k for k in parsed if k not in h.CONSTANTS) == h.FILL_KEYS
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
    if not h.AMEND1.with_suffix(".sha256").exists():
        pytest.skip("real AMEND1 has no freeze sidecar")
    real = h.frozen(replace(cfg, prereg=h.PREREG, amend1=h.AMEND1, run_dir=tmp_path / "real2"))
    assert real["constants"]["REPO_SHA"] != h.CONSTANTS.get("REPO_SHA")
    if not h.AMEND2.with_suffix(".sha256").exists():
        pytest.skip("real AMEND2 has no freeze sidecar")
    real = h.frozen(replace(cfg, prereg=h.PREREG, amend1=h.AMEND1, amend2=h.AMEND2,
                            run_dir=tmp_path / "real3"))
    assert h.arms(real["constants"], True) == [h.QWEN38, "sonnet"]


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
            # c is the amended view (REPO_SHA from AMEND2), so edit the prereg line directly.
            cfg.prereg.write_text(re.sub(r"(?m)^REPO_SHA=.*$", "REPO_SHA=FILL-AT-FREEZE",
                                         cfg.prereg.read_text()))
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
    arm = h.STATIC["LOCAL_MODELS"].split(",")[0]
    cfg, c, _ = selected_run(factory)
    cfg.ollama.ids[arm] = {"wrong"}
    assert call(cfg, "run", "--set", "qa", "--arms", arm) == 2
    assert not cfg.ollama.calls
    cfg.ollama.ids[arm] = set(c[f"ID_{arm}"].split(","))
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
    assert cfg.ollama.thinks == [False] * len(cfg.ollama.calls)
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


# CPU-bound, not a timing test: ~1,500 stubbed model calls, each re-reading the answer ledger
# in quota_guard (4.8 s of the 7.8 s profile). 6.5 s on an idle laptop, over the 30 s default on
# a loaded parallel CI runner (F52: #523, #571).
@pytest.mark.timeout(180)
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
    assert len(h.jsonl(cfg.run_dir / "code_results.jsonl")) == 75 * len(h.arms(c, True)) == 150


def test_score_statistics_and_single_use(factory, capsys):
    local = h.STATIC["LOCAL_MODELS"].split(",")
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
        assert set(stored) <= {h.QWEN38}
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
    locals_ = h.STATIC["LOCAL_MODELS"].split(",")
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
    assert set(lanes) == {h.QWEN38}


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
    assert call(cfg, *missing) == 0  # e_tech is None here: f_fit is not required (§11)
    out = h.read_json(cfg.run_dir / "tp2.json")
    assert "f_fit_not_required" in out["flags"]
    assert Fraction(out["fractions"]["T_P2"]) == Fraction(4, 5) * Fraction(2, 100)
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
    freeze_amend1(cfg, c, date="2030-03-17T17:46:39.500000Z")
    freeze_amend2(cfg, c, date="2030-03-17T17:46:39.750000Z")
    assert h.frozen(cfg)["frozen_utc"] == "2030-03-17T17:46:39Z"


def test_tp2_without_f_fit(factory):
    """Prereg §11: with e_tech = 0, T_P2 = 0.8 x s_cheap and --f-fit is optional; with
    e_tech > 0 a missing --f-fit refuses."""
    cfg, c = prepare_score(factory, "tp2b")
    assert call(cfg, "score") == 0
    args = ("tp2", "--s-tech", "30/100", "--s-tech-sessions", "3", "--s-tech-largest", ".5",
            "--s-cheap", "2/100")
    result = h.read_json(cfg.run_dir / "result.json")
    for e_tech, code in (({"model": c["LOCAL_MODELS"].split(",")[0], "k": 54, "n": 60}, 2),
                         (None, 0)):
        result["e_tech"] = e_tech
        h.write_json(cfg.run_dir / "result.json", result)
        h.append_use(cfg, "scored",
                     f"result_sha256={h.sha256_file(cfg.run_dir / 'result.json')}")
        assert call(cfg, *args) == code
    out = h.read_json(cfg.run_dir / "tp2.json")
    assert Fraction(out["fractions"]["T_P2"]) == Fraction(2, 125)
    assert "f_fit_not_required" in out["flags"]
    assert out["inputs"]["f_fit"] == "n/a"
    assert call(cfg, *args, "--f-fit", "0/0") == 2  # a given f_fit still needs n > 0


GATED_COMMANDS = [
    ("select",), ("run", "--set", "qa"), ("judge", "--set", "qa"),
    ("ingest-code", "--results", "/missing", "--runner", "/missing"), ("score",),
    ("install-eligibility", "--confirm"),
    ("tp2", "--s-tech", "30/100", "--s-tech-sessions", "3", "--s-tech-largest", ".5",
     "--s-cheap", "2/100"),
]


@pytest.mark.parametrize("command", GATED_COMMANDS)
def test_amend1_missing_gates_every_command(factory, command):
    cfg, _, _ = factory()
    cfg.amend1.unlink()
    assert call(cfg, *command) == 2


@pytest.mark.parametrize("defect,reason", [
    ("sidecar", "AMEND1 is not frozen"),
    ("sha", "AMEND1 is not frozen"),
    ("parse", "AMEND1 is not frozen"),
    ("same_date", "AMEND1 frozen_utc"),
    ("earlier_date", "AMEND1 frozen_utc"),
    ("future", "AMEND1 frozen_utc"),
    ("extra", "AMEND1 constants"),
    ("nonhex", "AMEND1 constants"),
    ("runner_nonhex", "AMEND1 constants"),
    ("missing_key", "AMEND1 constants"),
    ("duplicate", "AMEND1 constants"),
    ("copy", "PREREG-AMEND1.md differs"),
])
def test_amend1_validation(factory, defect, reason, capsys):
    cfg, c, _ = factory()
    text = cfg.amend1.read_text()
    if defect == "sidecar":
        cfg.amend1.with_suffix(".sha256").unlink()
    elif defect == "sha":
        cfg.amend1.write_text(text + "\n")
    elif defect == "parse":
        cfg.amend1.with_suffix(".sha256").write_text("broken")
    elif defect in ("same_date", "earlier_date", "future"):
        date = {"same_date": PAST, "earlier_date": "2025-01-01T00:00:00Z",
                "future": "2099-01-01T00:00:00Z"}[defect]
        freeze_amend1(cfg, c, date=date)
    elif defect == "copy":
        (cfg.run_dir / "PREREG-AMEND1.md").write_text(text + "changed")
    else:
        if defect == "extra":
            text = text.replace("REPO_SHA=", "EXTRA=value\nREPO_SHA=")
        elif defect == "nonhex":
            text = text.replace("a" * 40, "g" * 40)
        elif defect == "runner_nonhex":
            text = text.replace(c["CODE_RUNNER_SHA256"], "g" * 64)
        elif defect == "missing_key":
            text = text.replace(f"REPO_SHA={'a' * 40}\n", "")
        else:
            text = text.replace("REPO_SHA=", f"REPO_SHA={'a' * 40}\nREPO_SHA=")
        freeze_amend1(cfg, c, text=text)
    assert call(cfg, "select") == 2
    assert reason in capsys.readouterr().err
    assert not (cfg.run_dir / "selection.json").exists()


def test_amend_overrides_and_score_log(factory):
    """AMEND1 overrides the prereg, then AMEND2 overrides both and voids the old ID_ pins."""
    cfg, c, _ = factory()
    freeze_amend1(cfg, c, text=(
        f"```text P20-AMEND1-CONSTANTS\nREPO_SHA={'a' * 40}\n"
        f"CODE_RUNNER_SHA256={'b' * 64}\n```\n"
    ))
    freeze_amend2(cfg, c, text=amend2_text(c, CODE_RUNNER_SHA256="d" * 64))
    (cfg.run_dir / "PREREG-AMEND1.md").write_bytes(cfg.amend1.read_bytes())
    (cfg.run_dir / "PREREG-AMEND2.md").write_bytes(cfg.amend2.read_bytes())
    freeze = h.frozen(cfg)
    assert freeze["constants"] == {**c, "REPO_SHA": "c" * 40, "CODE_RUNNER_SHA256": "d" * 64}
    assert freeze["amend1_sha256"] == h.sha256_file(cfg.amend1)
    assert freeze["amend1_frozen_utc"] == AMEND_PAST
    assert freeze["amend2_sha256"] == h.sha256_file(cfg.amend2)
    assert freeze["amend2_frozen_utc"] == AMEND2_PAST
    assert call(cfg, "select") == 0
    assert call(cfg, "score") == 0
    started = next(row for row in h.uses(cfg) if row[0] == "started")
    assert f"amend1_sha256={freeze['amend1_sha256']}" in started
    assert f"amend2_sha256={freeze['amend2_sha256']}" in started


@pytest.mark.parametrize("command", GATED_COMMANDS)
def test_main_gate_before_writes(factory, command):
    cfg, _, _ = factory()
    seen = []

    def refuse(constants):
        seen.append(constants)
        raise h.Refuse("refusing: test code gate")

    cfg.gate = refuse
    before = {p: p.read_bytes() for p in cfg.run_dir.rglob("*") if p.is_file()}
    assert call(cfg, *command) == 2
    assert len(seen) == 1 and seen[0]["REPO_SHA"] == "c" * 40
    assert before == {p: p.read_bytes() for p in cfg.run_dir.rglob("*") if p.is_file()}
    assert not cfg.ollama.calls and not cfg.claude.calls


def test_gate_status_and_dry_run(factory, capsys):
    cfg, _, _ = factory()
    assert call(cfg, "status") == 0
    assert json.loads(capsys.readouterr().out)["code_gate"] == "ok"
    seen = []

    def refuse(constants):
        seen.append(constants)
        raise h.Refuse("refusing: test code gate")

    cfg.gate = refuse
    assert call(cfg, "status") == 0
    assert json.loads(capsys.readouterr().out)["code_gate"] == "refusing: test code gate"
    assert len(seen) == 1
    assert call(cfg, "select", "--dry-run") == 0
    assert len(seen) == 1
    cfg.amend1.unlink()
    assert h.status(cfg)["code_gate"] == "n/a"
    assert len(seen) == 1


@pytest.fixture
def gate_repo(tmp_path, monkeypatch):
    for key, value in {
        "GIT_AUTHOR_NAME": "Test", "GIT_COMMITTER_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@localhost", "GIT_COMMITTER_EMAIL": "test@localhost",
    }.items():
        monkeypatch.setenv(key, value)
    repo = tmp_path / "gate-repo"
    repo.mkdir()

    def git(*args):
        return GIT_RUN(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=60, check=True).stdout.strip()

    git("init", "-q")
    for directory in ("scripts", "src", "tests"):
        (repo / directory).mkdir()
        (repo / directory / "tracked.txt").write_text("base\n")
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "base")
    return repo, git("rev-parse", "HEAD"), str(repo / "src/tracked.txt"), git


def test_code_gate_clean_and_docs(gate_repo):
    repo, sha, module, _ = gate_repo
    h.code_gate(repo, sha, module, run=GIT_RUN)
    (repo / "docs").mkdir()
    (repo / "docs/x.md").write_text("documentation change\n")
    h.code_gate(repo, sha, module, run=GIT_RUN)


@pytest.mark.parametrize("defect,reason", [
    ("edit", "code differs from REPO_SHA"),
    ("untracked", "uncommitted code under scripts/src/tests"),
    ("outside", "llm_router imported from outside the run worktree"),
    ("bogus_sha", "refusing:"),
])
def test_code_gate_refusals(gate_repo, defect, reason):
    repo, sha, module, _ = gate_repo
    if defect == "edit":
        (repo / "scripts/tracked.txt").write_text("edited\n")
    elif defect == "untracked":
        (repo / "tests/untracked.txt").write_text("new\n")
    elif defect == "outside":
        module = str(repo.parent / "outside.py")
    else:
        sha = "not-a-revision"
    with pytest.raises(h.Refuse, match=reason):
        h.code_gate(repo, sha, module, run=GIT_RUN)


@pytest.mark.parametrize("error", [OSError("git unavailable"), subprocess.TimeoutExpired("git", 60)])
def test_code_gate_subprocess_errors(tmp_path, error):
    def run(argv, **kwargs):
        assert kwargs == {"stdin": subprocess.DEVNULL, "capture_output": True,
                          "text": True, "timeout": 60, "check": False}
        raise error

    with pytest.raises(h.Refuse, match="code gate git error"):
        h.code_gate(tmp_path, "a" * 40, str(tmp_path / "module.py"), run=run)


def test_run_gate_default(factory, monkeypatch):
    cfg, _, _ = factory()
    cfg.gate = None
    seen = []
    monkeypatch.setattr(h, "code_gate", lambda *args: seen.append(args))
    h.run_gate(cfg, h.frozen(cfg)["constants"])
    assert seen == [(h.REPO, "c" * 40, h.llm_router.__file__)]


# AMEND2 (D-166): qwen3.8-uncensored:latest is the only local arm, think:false, a foreign-arm
# guard, and frozen() enforces the amendment.


def test_amend2_arm_lists_and_old_pins_gone(factory):
    cfg, _, _ = factory()
    for c in (h.STATIC, h.frozen(cfg)["constants"]):
        assert c["LOCAL_MODELS"].split(",") == [h.QWEN38]
        assert c["CODE_LOCAL_MODELS"].split(",") == [h.QWEN38]
        assert c["SIZE_ORDER"].split(",") == [h.QWEN38]
        assert h.arms(c) == [h.QWEN38, "sonnet", "haiku"]
        assert h.arms(c, True) == [h.QWEN38, "sonnet"]
        assert {k for k in c if k.startswith("ID_")} == {f"ID_{h.QWEN38}"}
        assert c[f"ID_{h.QWEN38}"] == "b3f4c17c6811"
        assert not any(old in value for old in OLD_LOCAL for value in c.values())
        assert (c["REFERENCE"], c["REPORT_ARMS"], c["JUDGE"]) == ("sonnet", "haiku", "opus")
    # The prereg's own constants are frozen and stay as they were.
    assert h.CONSTANTS["LOCAL_MODELS"].split(",") == list(OLD_LOCAL)
    assert h.local_models.num_ctx(h.QWEN38) == h.local_models.DEFAULT_NUM_CTX == 32768


class RecordingOllama(h.OllamaHTTP):
    """The real HTTP client with the transport replaced: records every request body."""

    def __init__(self):
        self.bodies = []

    def request(self, route, body=None, timeout=60):
        self.bodies.append((route, body))
        if route != "/api/chat":
            return {}
        if body["messages"][0]["content"] == "Reply OK.":
            return {"message": {"content": "OK"}}
        ident = re.search(r"ID:(\S+)", h.flatten(body["messages"]))[1]
        return {"message": {"content": "<think>hidden</think>" + answer(body["model"], ident)}}

    def list_ids(self):
        return {h.QWEN38: {"b3f4c17c6811"}}

    def ps(self):
        return []


def test_think_false_in_every_local_payload(factory):
    cfg, _, _ = selected_run(factory)
    cfg.ollama = RecordingOllama()
    assert call(cfg, "run", "--set", "qa", "--arms", h.QWEN38) == 0
    n = h.selection(cfg)["qa"]["n"]
    chats = [body for route, body in cfg.ollama.bodies if route == "/api/chat"]
    assert len(chats) == 1 + n  # warm-up plus one answer call per item
    assert all(body["think"] is False for body in chats)
    assert all(body["options"] == {"num_ctx": 32768, "temperature": 0} for body in chats)
    assert cfg.ollama.bodies[-1] == ("/api/generate", {"model": h.QWEN38, "keep_alive": 0})
    rows = h.jsonl(cfg.run_dir / "answers/qa.jsonl")
    assert len(rows) == n and all(r["ok"] and "<think>" not in r["text"] for r in rows)


def foreign_row(ident):
    return {"item_id": ident, "arm": OLD_LOCAL[0], "attempt": 1, "ok": True, "final": True,
            "text": "x", "truncated": False, "error": None, "wall_s": 0, "ts": PAST}


@pytest.mark.parametrize("command", [("run", "--set", "qa"), ("judge", "--set", "qa")])
def test_foreign_arm_answers_refuse_run_and_judge(factory, command, capsys):
    cfg, _, _ = selected_run(factory)
    ident = h.selection(cfg)["qa"]["ids"][0]
    h.append_row(cfg.run_dir / "answers/qa.jsonl", foreign_row(ident))
    before = {p: p.read_bytes() for p in cfg.run_dir.rglob("*") if p.is_file()}
    assert call(cfg, *command) == 2
    assert "refusing: 1 foreign-arm rows in answers/qa.jsonl" in capsys.readouterr().err
    assert before == {p: p.read_bytes() for p in cfg.run_dir.rglob("*") if p.is_file()}
    assert not cfg.ollama.calls and not cfg.claude.calls
    # The other set is not blocked by a stray qa row.
    assert call(cfg, command[0], "--set", "tech_op") in (0, 2)
    assert "foreign-arm" not in capsys.readouterr().err


def test_foreign_arm_code_results_refuse_ingest(factory, capsys):
    cfg, c, _ = selected_run(factory)
    rows = [{"task_id": ident, "arm": arm, "hidden_pass": True, "ts": NOW}
            for ident in h.selection(cfg)["code"]["ids"] for arm in h.arms(c, True)]
    rows.append(dict(rows[0], arm=OLD_LOCAL[1]))
    path = cfg.run_dir / "external.jsonl"
    h.atomic(path, "".join(json.dumps(r) + "\n" for r in rows))
    runner = cfg.run_dir / "runner.py"
    assert call(cfg, "ingest-code", "--results", str(path), "--runner", str(runner)) == 2
    assert "refusing: 1 foreign-arm rows in" in capsys.readouterr().err
    assert not (cfg.run_dir / "code_results.jsonl").exists()
    assert not any(r[0] == "ingested" for r in h.uses(cfg))


@pytest.mark.parametrize("where", ["qa", "tech_op", "code"])
def test_foreign_arm_rows_refuse_score_before_start(factory, where, capsys):
    cfg, c = prepare_score(factory, f"foreign-{where}")
    if where == "code":
        h.append_row(cfg.run_dir / "code_results.jsonl",
                     {"task_id": "code-000", "arm": OLD_LOCAL[1], "hidden_pass": True, "ts": NOW})
    else:
        h.append_row(cfg.run_dir / f"answers/{where}.jsonl", foreign_row(f"{where}-000"))
    assert call(cfg, "score") == 2
    assert "foreign-arm rows" in capsys.readouterr().err
    assert not any(r[0] == "started" for r in h.uses(cfg))
    assert not (cfg.run_dir / "result.json").exists()


def test_status_counts_foreign_arm_rows(factory, capsys):
    cfg, _, _ = selected_run(factory)
    for i in range(16):
        h.append_row(cfg.run_dir / "answers/qa.jsonl", foreign_row(f"qa-{i:03}"))
    h.append_row(cfg.run_dir / "code_results.raw.jsonl",
                 {"task_id": "code-000", "arm": OLD_LOCAL[1], "hidden_pass": None, "ts": NOW})
    assert call(cfg, "status") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["foreign_arm_rows"] == {"qa": 16, "tech_op": 0, "code_raw": 1, "code": 0}
    cfg.amend2.unlink()  # not frozen: counted against the AMEND2 arm list all the same
    assert call(cfg, "status") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["frozen"] is False and out["code_gate"] == "n/a"
    assert out["foreign_arm_rows"]["qa"] == 16
    assert "x" * 5 not in json.dumps(out) and "ID:" not in json.dumps(out)


@pytest.mark.parametrize("command", GATED_COMMANDS)
def test_amend2_missing_gates_every_command(factory, command, capsys):
    cfg, _, _ = factory()
    cfg.amend2.unlink()
    before = {p: p.read_bytes() for p in cfg.run_dir.rglob("*") if p.is_file()}
    assert call(cfg, *command) == 2
    assert "AMEND2 is not frozen" in capsys.readouterr().err
    assert before == {p: p.read_bytes() for p in cfg.run_dir.rglob("*") if p.is_file()}


@pytest.mark.parametrize("defect,reason", [
    ("sidecar", "AMEND2 is not frozen"),
    ("sha", "AMEND2 is not frozen"),
    ("parse", "AMEND2 is not frozen"),
    ("same_as_amend1", "AMEND2 frozen_utc"),
    ("before_amend1", "AMEND2 frozen_utc"),
    ("before_prereg", "AMEND2 frozen_utc"),
    ("future", "AMEND2 frozen_utc"),
    ("no_block", "AMEND2 constants"),
    ("extra", "AMEND2 constants"),
    ("old_pin", "AMEND2 constants"),
    ("missing_size_order", "AMEND2 constants"),
    ("missing_runner", "AMEND2 constants"),
    ("old_arms", "AMEND2 constants"),
    ("wrong_id", "AMEND2 constants"),
    ("nonhex", "AMEND2 constants"),
    ("duplicate", "AMEND2 constants"),
    ("copy", "PREREG-AMEND2.md differs"),
])
def test_amend2_validation(factory, defect, reason, capsys):
    cfg, c, _ = factory()
    text = cfg.amend2.read_text()
    if defect == "sidecar":
        cfg.amend2.with_suffix(".sha256").unlink()
    elif defect == "sha":
        cfg.amend2.write_text(text + "\n")
    elif defect == "parse":
        cfg.amend2.with_suffix(".sha256").write_text("broken")
    elif defect in ("same_as_amend1", "before_amend1", "before_prereg", "future"):
        date = {"same_as_amend1": AMEND_PAST, "before_amend1": "2026-06-01T00:00:00Z",
                "before_prereg": "2025-01-01T00:00:00Z",
                "future": "2099-01-01T00:00:00Z"}[defect]
        freeze_amend2(cfg, c, date=date)
    elif defect == "copy":
        (cfg.run_dir / "PREREG-AMEND2.md").write_text(text + "changed")
    else:
        if defect == "no_block":
            text = "# AMEND2 without constants\n"
        elif defect == "extra":
            text = text.replace("REPO_SHA=", "N_MIN=61\nREPO_SHA=")
        elif defect == "old_pin":
            text = text.replace("REPO_SHA=", f"ID_{OLD_LOCAL[0]}=017eda0b7c34\nREPO_SHA=")
        elif defect == "missing_size_order":
            text = text.replace(f"SIZE_ORDER={h.QWEN38}\n", "")
        elif defect == "missing_runner":
            text = text.replace(f"CODE_RUNNER_SHA256={c['CODE_RUNNER_SHA256']}\n", "")
        elif defect == "old_arms":
            text = text.replace(f"LOCAL_MODELS={h.QWEN38}\nCODE",
                                f"LOCAL_MODELS={h.QWEN38},{OLD_LOCAL[2]}\nCODE")
        elif defect == "wrong_id":
            text = text.replace("b3f4c17c6811", "000000000000")
        elif defect == "nonhex":
            text = text.replace("c" * 40, "g" * 40)
        else:
            text = text.replace("REPO_SHA=", f"REPO_SHA={'c' * 40}\nREPO_SHA=")
        assert text != cfg.amend2.read_text()
        freeze_amend2(cfg, c, text=text)
    assert call(cfg, "select") == 2
    assert reason in capsys.readouterr().err
    assert not (cfg.run_dir / "selection.json").exists()


def test_amend2_byte_identical_copy_accepted(factory):
    cfg, _, _ = factory()
    copy = cfg.run_dir / "PREREG-AMEND2.md"
    copy.write_bytes(cfg.amend2.read_bytes())
    copy.chmod(0o444)
    assert h.frozen(cfg)["amend2_sha256"] == h.sha256_file(cfg.amend2)


def test_real_amend2_constants_block_matches_the_harness():
    if not h.AMEND2.exists():
        pytest.skip("AMEND2 not written yet")
    block = h.parse_block(h.AMEND2.read_text(), "```text P20-AMEND2-CONSTANTS")
    assert block.keys() == h.AMEND2_FIXED.keys() | h.AMEND2_SHA_KEYS.keys()
    assert {k: block[k] for k in h.AMEND2_FIXED} == h.AMEND2_FIXED
    assert all(re.fullmatch(p, block[k]) for k, p in h.AMEND2_SHA_KEYS.items())


# AMEND3 (D-202): any time of day, quiet gate + exclusive lock before every local call.
def test_amend3_daytime_run_allowed_when_quiet(factory):
    arm = h.STATIC["LOCAL_MODELS"].split(",")[0]
    cfg, c, _ = selected_run(factory)
    cfg.ollama.ids[arm] = set(c[f"ID_{arm}"].split(","))
    assert call(cfg, "run", "--set", "qa", "--arms", arm) == 0
    rows = h.jsonl(cfg.run_dir / "answers/qa.jsonl")
    quiet_rows = h.jsonl(cfg.run_dir / "quiet.jsonl")
    assert rows and len(quiet_rows) == len(rows) + 1  # + warm-up
    assert quiet_rows[-1]["load1"] == [3.99] * 3 and len(quiet_rows[-1]["free_mem_pct"]) == 3
    assert quiet_rows[-1]["item_id"] == rows[-1]["item_id"]
    assert not hasattr(cfg, "local_hour")


def test_amend3_quiet_gate_parks_mid_run_without_row(factory):
    arm = h.STATIC["LOCAL_MODELS"].split(",")[0]
    cfg, c, _ = selected_run(factory)
    cfg.ollama.ids[arm] = set(c[f"ID_{arm}"].split(","))
    original = cfg.ollama.chat

    def chat(model, messages, **kw):
        out = original(model, messages, **kw)
        if len(cfg.ollama.calls) == 4:  # warm-up + 3 answers, then the machine gets busy
            cfg.machine.load = 4.0
        return out

    cfg.ollama.chat = chat
    assert call(cfg, "run", "--set", "qa", "--arms", arm) == 3
    assert len(cfg.ollama.calls) == 4
    assert len(h.jsonl(cfg.run_dir / "answers/qa.jsonl")) == 3
    assert len(h.jsonl(cfg.run_dir / "quiet.jsonl")) == 4
    cfg.machine.load = 3.99
    cfg.ollama.resident = [arm]
    assert call(cfg, "run", "--set", "qa", "--arms", arm) == 0  # resumes
    cfg.ollama.resident = ["foreign"]
    cfg.machine.index = 0
    with pytest.raises(h.Park, match="foreign model resident"):
        with h.local_call(cfg, c, arm, {}):
            pass


def test_amend3_lock_held_parks_and_is_released_after_each_call(factory, tmp_path):
    arm = h.STATIC["LOCAL_MODELS"].split(",")[0]
    cfg, c, _ = selected_run(factory)
    cfg.ollama.ids[arm] = set(c[f"ID_{arm}"].split(","))
    other = os.open(cfg.lock_path, os.O_RDWR | os.O_CREAT)
    fcntl.flock(other, fcntl.LOCK_EX)
    try:
        assert call(cfg, "run", "--set", "qa", "--arms", arm) == 3
        assert not cfg.ollama.calls
        assert not (cfg.run_dir / "answers/qa.jsonl").exists()
    finally:
        os.close(other)
    seen = []
    original = cfg.ollama.chat

    def chat(model, messages, **kw):
        probe = os.open(cfg.lock_path, os.O_RDWR)
        try:
            with pytest.raises(OSError):  # held while the call is outstanding
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen.append(1)
        finally:
            os.close(probe)
        return original(model, messages, **kw)

    cfg.ollama.chat = chat
    assert call(cfg, "run", "--set", "qa", "--arms", arm) == 0
    assert len(seen) == len(cfg.ollama.calls) > 1
    free = os.open(cfg.lock_path, os.O_RDWR)
    fcntl.flock(free, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released at the end
    os.close(free)


def test_amend3_lock_released_on_exception_and_park(factory):
    arm = h.STATIC["LOCAL_MODELS"].split(",")[0]
    cfg, c, _ = factory()

    def can_lock():
        fd = os.open(cfg.lock_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    with pytest.raises(RuntimeError):
        with h.local_call(cfg, c, arm, {}):
            raise RuntimeError("boom")
    assert can_lock()
    cfg.machine.load = 4.0
    with pytest.raises(h.Park):
        with h.local_call(cfg, c, arm, {}):
            pass
    assert can_lock()
    cfg.machine.load = 3.99
    cfg.ollama.fail = True  # a timeout inside call_model becomes an error row; lock still freed
    row = h.call_model(cfg, c, "qa", arm, "x", 1, [{"role": "user", "content": "ID:x"}])
    assert row["error"] == "exception" and can_lock()


def test_amend3_frozen_enforcement(factory):
    cfg, c, _ = factory()
    base = h.frozen(cfg)
    assert "amend3_sha256" not in base and base["constants"]["NIGHT_WINDOW"] == "00:00-07:00"
    freeze_amend3(cfg, c)
    got = h.frozen(cfg)
    assert got["constants"]["REPO_SHA"] == "e" * 40
    assert got["constants"]["NIGHT_WINDOW"] == "none"
    assert got["constants"]["LOCAL_MODELS"] == h.QWEN38  # AMEND2 keys remain
    assert got["amend3_sha256"] == h.sha256_file(cfg.amend3)
    assert got["amend3_frozen_utc"] == AMEND3_PAST
    (cfg.run_dir / "PREREG-AMEND3.md").write_text("different")
    with pytest.raises(h.Refuse, match="PREREG-AMEND3.md differs"):
        h.frozen(cfg)
    (cfg.run_dir / "PREREG-AMEND3.md").unlink()
    cases = [
        (dict(date=AMEND2_PAST), "AMEND3 frozen_utc"),
        (dict(date="2031-01-01T00:00:00Z"), "AMEND3 frozen_utc"),
        (dict(text=amend3_text(c, NIGHT_WINDOW="00:00-07:00")), "AMEND3 constants"),
        (dict(text=amend3_text(c, REPO_SHA="zz")), "AMEND3 constants"),
        (dict(text="# AMEND3\n```text P20-AMEND3-CONSTANTS\nREPO_SHA=" + "e" * 40 + "\n```\n"),
         "AMEND3 constants"),
    ]
    for kwargs, message in cases:
        freeze_amend3(cfg, c, **kwargs)
        with pytest.raises(h.Refuse, match=message):
            h.frozen(cfg)
    freeze_amend3(cfg, c)
    cfg.amend3.write_text(cfg.amend3.read_text() + " ")  # sidecar no longer matches
    with pytest.raises(h.Refuse, match="AMEND3 is not frozen"):
        h.frozen(cfg)
    assert call(cfg, "select") == 2


def test_amend3_score_log_records_hash(factory):
    cfg, c, _ = factory()
    freeze_amend3(cfg, c)
    assert call(cfg, "select") == 0 and call(cfg, "score") == 0
    started = next(row for row in h.uses(cfg) if row[0] == "started")
    assert f"amend3_sha256={h.sha256_file(cfg.amend3)}" in started


# AMEND4: AMEND3 is a plain merge (it must not drop AMEND2's ID_ pin); optional REPO_SHA re-pin.
QWEN_ID = f"ID_{h.QWEN38}"


def test_amend3_and_amend4_keep_every_amend2_key(factory):
    cfg, c, _ = factory()
    freeze_amend3(cfg, c)
    third = h.frozen(cfg)["constants"]
    assert third[QWEN_ID] == "b3f4c17c6811"
    freeze_amend4(cfg)
    fourth = h.frozen(cfg)["constants"]
    assert fourth[QWEN_ID] == "b3f4c17c6811"
    for key, value in h.AMEND2_FIXED.items():
        assert third[key] == value and fourth[key] == value, key
    assert third["REPO_SHA"] == "e" * 40 and fourth["REPO_SHA"] == "f" * 40
    assert fourth["NIGHT_WINDOW"] == "none"
    assert not any(k.startswith("ID_") and k != QWEN_ID for k in fourth)


@pytest.mark.parametrize("amend4", [False, True])
def test_run_after_real_frozen_with_all_amendments_is_not_refused(factory, amend4):
    cfg, c, _ = selected_run(factory)
    freeze_amend3(cfg, c)
    if amend4:
        freeze_amend4(cfg)
    rc = call(cfg, "run", "--set", "qa")
    assert rc in (0, 3)  # never 2 ("invalid input", the KeyError on ID_<arm>)
    assert cfg.ollama.calls and cfg.ollama.calls[0][1][0]["content"] == "Reply OK."


def test_amend4_frozen_enforcement(factory):
    cfg, c, _ = factory()
    freeze_amend3(cfg, c)
    base = h.frozen(cfg)
    assert "amend4_sha256" not in base and base["constants"]["REPO_SHA"] == "e" * 40
    freeze_amend4(cfg)
    got = h.frozen(cfg)
    assert got["amend4_sha256"] == h.sha256_file(cfg.amend4)
    assert got["amend4_frozen_utc"] == AMEND4_PAST
    (cfg.run_dir / "PREREG-AMEND4.md").write_text("different")
    with pytest.raises(h.Refuse, match="PREREG-AMEND4.md differs"):
        h.frozen(cfg)
    (cfg.run_dir / "PREREG-AMEND4.md").write_bytes(cfg.amend4.read_bytes())
    assert h.frozen(cfg)["amend4_sha256"] == got["amend4_sha256"]
    (cfg.run_dir / "PREREG-AMEND4.md").unlink()
    cases = [
        (dict(date=AMEND3_PAST), "AMEND4 frozen_utc"),
        (dict(date="2031-01-01T00:00:00Z"), "AMEND4 frozen_utc"),
        (dict(date="2030-01-01T00:00:00"), "AMEND4 frozen_utc"),
        (dict(text=amend4_text(CODE_RUNNER_SHA256="d" * 64)), "AMEND4 constants"),
        (dict(text=amend4_text(REPO_SHA="zz")), "AMEND4 constants"),
        (dict(text=amend4_text(REPO_SHA="F" * 40)), "AMEND4 constants"),
        (dict(text="# AMEND4\n```text P20-AMEND4-CONSTANTS\n```\n"), "AMEND4 constants"),
    ]
    for kwargs, message in cases:
        freeze_amend4(cfg, **kwargs)
        with pytest.raises(h.Refuse, match=message):
            h.frozen(cfg)
    freeze_amend4(cfg)
    cfg.amend4.write_text(cfg.amend4.read_text() + " ")  # sidecar no longer matches
    with pytest.raises(h.Refuse, match="AMEND4 is not frozen"):
        h.frozen(cfg)
    assert call(cfg, "select") == 2


def test_amend4_sidecar_without_frozen_amend3_refuses(factory):
    cfg, c, _ = factory()
    freeze_amend4(cfg)
    with pytest.raises(h.Refuse, match="AMEND4 requires a frozen AMEND3"):
        h.frozen(cfg)


def test_amend4_score_log_records_hash(factory):
    cfg, c, _ = factory()
    freeze_amend3(cfg, c)
    freeze_amend4(cfg)
    assert call(cfg, "select") == 0 and call(cfg, "score") == 0
    started = next(row for row in h.uses(cfg) if row[0] == "started")
    assert f"amend4_sha256={h.sha256_file(cfg.amend4)}" in started


def test_without_amend4_sidecar_behaviour_is_unchanged(factory):
    cfg, c, _ = factory()
    freeze_amend3(cfg, c)
    cfg.amend4.write_text(amend4_text(REPO_SHA="0" * 40))  # a draft with no sidecar is inert
    got = h.frozen(cfg)
    assert not any(k.startswith("amend4") for k in got)
    assert got["constants"]["REPO_SHA"] == "e" * 40
    assert set(got) == {"sha256", "frozen_utc", "amend1_sha256", "amend1_frozen_utc",
                        "amend2_sha256", "amend2_frozen_utc", "amend3_sha256",
                        "amend3_frozen_utc", "constants"}


def fake_exe(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return directory


def test_agent_path_drops_llm_router_and_keeps_needed_tools(tmp_path, monkeypatch):
    real, _ = plant_real_home(tmp_path, monkeypatch)
    bad = fake_exe(tmp_path / "bad", "llm-router")
    fake_exe(bad, "node")  # node shares the dir with llm-router: dropped whole, fails closed
    chu = fake_exe(tmp_path / "chu", "llm_router")
    local = fake_exe(real / ".local/bin", "git")
    good = fake_exe(tmp_path / "good", "node")
    junk = fake_exe(tmp_path / "junk", "unrelated-tool")
    monkeypatch.setenv("PATH", os.pathsep.join(map(str, (bad, chu, local, good, junk, "/bin"))))
    env = h.agent_env(h.scratch_home(tmp_path / "scratch"))
    dirs = env["PATH"].split(os.pathsep)
    assert str(good) in dirs and "/bin" in dirs
    for gone in (bad, chu, local, junk):
        assert str(gone) not in dirs
    for name in h.DENIED_EXECUTABLES:
        assert shutil.which(name, path=env["PATH"]) is None


def test_agent_env_refuses_when_llm_router_resolves_on_path(tmp_path, monkeypatch):
    plant_real_home(tmp_path, monkeypatch)
    bad = fake_exe(tmp_path / "bad", "llm-router")
    with pytest.raises(h.Refuse, match="llm-router is reachable"):
        h.assert_agent_env({"HOME": str(tmp_path / "s"), "PATH": f"/bin{os.pathsep}{bad}"})


def test_agent_env_refuses_home_inside_real_home_unless_under_run_dir(tmp_path, monkeypatch):
    real, _ = plant_real_home(tmp_path, monkeypatch)
    inside = real / "stuff" / "scratch"
    with pytest.raises(h.Refuse, match="inside the real HOME"):
        h.assert_agent_env({"HOME": str(inside), "PATH": "/bin"})
    with pytest.raises(h.Refuse, match="inside the real HOME"):
        h.assert_agent_env({"HOME": str(inside), "PATH": "/bin"}, run_dir=real / "other")
    h.assert_agent_env({"HOME": str(inside), "PATH": "/bin"}, run_dir=real / "stuff")


@pytest.mark.parametrize("code", [2, 126, 127, 139])
def test_import_guard_fails_closed_on_any_nonzero_probe_exit(tmp_path, code):
    stub = tmp_path / "s"
    stub.mkdir()
    exe = stub / "python3"
    exe.write_text(f"#!/bin/sh\nexit {code}\n")
    exe.chmod(0o755)
    with pytest.raises(h.Refuse, match="no clean answer"):
        h.assert_no_llm_router_import({"PATH": str(stub)})


def test_import_guard_refuses_on_signal_oserror_and_timeout(tmp_path, monkeypatch):
    stub = python_stub(tmp_path / "s", "python3", importable=False)
    env = {"PATH": str(stub)}
    killed = tmp_path / "k"
    killed.mkdir()
    (killed / "python3").write_text("#!/bin/sh\nkill -9 $$\n")
    (killed / "python3").chmod(0o755)
    with pytest.raises(h.Refuse, match="no clean answer"):
        h.assert_no_llm_router_import({"PATH": str(killed)})
    for exc, text in ((OSError("/secret/path"), "could not run"),
                      (subprocess.TimeoutExpired("x", 1), "timed out")):
        def boom(*a, _e=exc, **k):
            raise _e
        monkeypatch.setattr(h.subprocess, "run", boom)
        with pytest.raises(h.Refuse, match=text) as err:
            h.assert_no_llm_router_import(env)
        assert err.value.__cause__ is None and "/secret/path" not in str(err.value)
    monkeypatch.undo()
    h.assert_no_llm_router_import(env)


def test_agent_pythons_finds_versioned_and_pypy_interpreters(tmp_path):
    d = tmp_path / "d"
    for name in ("python3.13", "pypy3", "pypy", "python", "python3-config", "pythonw.txt"):
        python_stub(d, name, importable=False)
    found = {Path(p).name for p in h.agent_pythons(str(d))}
    assert found == {"python3.13", "pypy3", "pypy", "python"}
    bad = python_stub(tmp_path / "bad", "python3.11", importable=True)
    with pytest.raises(h.Refuse, match="importable"):
        h.assert_no_llm_router_import({"PATH": f"{d}{os.pathsep}{bad}"})


def test_grader_python_hung_or_unrunnable_pytest_probe_refuses(tmp_path, monkeypatch):
    stub = python_stub(tmp_path / "s", "python3", importable=False)
    real_run = subprocess.run

    def hung(argv, *a, **k):
        if "import pytest" in argv:
            raise subprocess.TimeoutExpired(argv, 1)
        return real_run(argv, *a, **k)

    monkeypatch.setattr(h.subprocess, "run", hung)
    with pytest.raises(h.Refuse, match="timed out"):
        h.grader_python({"PATH": str(stub)})

    def gone(argv, *a, **k):
        if "import pytest" in argv:
            raise OSError("nope")
        return real_run(argv, *a, **k)

    monkeypatch.setattr(h.subprocess, "run", gone)
    with pytest.raises(h.Refuse, match="could not run"):
        h.grader_python({"PATH": str(stub)})


def test_credentials_file_error_drops_the_path_cause(tmp_path, monkeypatch):
    missing = tmp_path / "no-such-token-file"
    with pytest.raises(h.Refuse) as err:
        h.read_oauth_token(str(missing))
    assert err.value.__cause__ is None and err.value.__suppress_context__
    binary = tmp_path / "bin"
    binary.write_bytes(b"\xff\xfe\x00")
    binary.chmod(0o600)
    with pytest.raises(h.Refuse, match="unusable"):
        h.read_oauth_token(str(binary))


def test_scan_for_token_refuses_by_kind_without_the_value(tmp_path):
    token = "FAKE-OAUTH-TOKEN-0123456789"
    h.scan_for_token(token, "patch", data="clean diff")
    h.scan_for_token("", "patch", data=token)  # no token configured: nothing to find
    with pytest.raises(h.Refuse, match="token found in patch") as err:
        h.scan_for_token(token, "patch", data=f"+KEY={token}\n")
    assert token not in str(err.value)
    tree = tmp_path / "w"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "ok.txt").write_text("nothing here")
    h.scan_for_token(token, "work files", root=tree)
    (tree / "sub" / "notes.txt").write_bytes(b"x" * ((1 << 20) - 5) + token.encode() + b"y")
    with pytest.raises(h.Refuse, match="token found in work files") as err:
        h.scan_for_token(token, "work files", root=tree)
    assert token not in str(err.value) and "notes" not in str(err.value)
