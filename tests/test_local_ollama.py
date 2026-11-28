"""`llm-router setup-local` and its parts (local_ollama.py, commands/setup_local.py).

Every launchctl, ollama and HTTP call here is injected. An autouse guard makes the REAL ones raise,
and ``test_the_guard_trips_on_a_real_call`` proves the guard itself can fail, so "no test reached the
machine" is a checked fact and not a hope. Nothing touches :11434, :11435, :8787, :8797, a real
LaunchAgents directory or the real ~/.llm-router (LLM_ROUTER_HOME is a tmp dir per test).
"""

from __future__ import annotations

import os
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

from llm_router import local_models, local_ollama as lo, proxy_default as pd
from llm_router.commands import proxy_default as cmd
from llm_router.commands import setup_local as sl
from llm_router.proxy import ledger

REAL_CALLS: list[str] = []
_EXPECT_TRIPS = [False]


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch, tmp_path):
    import llm_router.install_hooks as ih

    REAL_CALLS.clear()
    _EXPECT_TRIPS[0] = False
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(ih, "_SETTINGS_PATH", tmp_path / ".claude" / "settings.json")
    for k in lo.ENV_KEYS:
        monkeypatch.delenv(k, raising=False)

    def _trip(name):
        def _raise(*a, **k):
            REAL_CALLS.append(name)
            raise AssertionError(f"a test reached the real {name}")
        return _raise

    monkeypatch.setattr(subprocess, "run", _trip("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", _trip("subprocess.Popen"))
    monkeypatch.setattr(os, "system", _trip("os.system"))
    monkeypatch.setattr(urllib.request, "urlopen", _trip("urllib.request.urlopen"))
    monkeypatch.setattr(socket, "create_connection", _trip("socket.create_connection"))
    # The default arguments were bound to the real subprocess.run at import: patch them too.
    monkeypatch.setitem(pd.activate_service.__kwdefaults__, "runner", _trip("activate_service default runner"))
    monkeypatch.setitem(pd.deactivate_service.__kwdefaults__, "runner", _trip("deactivate_service default runner"))
    yield tmp_path
    # activate/deactivate_service swallow exceptions, so a raise alone proves nothing: check the record.
    if not _EXPECT_TRIPS[0]:
        assert REAL_CALLS == [], f"a test reached the real machine: {REAL_CALLS}"
    REAL_CALLS.clear()


class Box:
    """A fake machine: a launchd that records commands, an Ollama that comes up after activation."""

    def __init__(self, *, total_gb=48.0, tags=(), peer_models=(), ollama="/opt/homebrew/bin/ollama",
                 activate_rc=0, comes_up=True, pull_ok=True):
        self.total_gb, self.ollama = total_gb, ollama
        self.tags, self.peer_models = list(tags), list(peer_models)
        self.activate_rc, self.comes_up, self.pull_ok = activate_rc, comes_up, pull_ok
        self.up = False
        self.resident = False
        self.commands: list = []
        self.posts: list = []
        self.fetched: list[str] = []
        self.pulled = False
        self.answers: list[str] = []

    def probe(self):
        return {"total_gb": self.total_gb}

    def which(self, name):
        return self.ollama if name == "ollama" else None

    def fetch(self, url):
        self.fetched.append(url)
        if url.startswith(lo.URL):
            if not self.up:
                return None
            if url.endswith("/api/version"):
                return {"version": "0.0"}
            if url.endswith("/api/ps"):
                return {"models": [{"model": lo.MODEL, "size": 22.4e9}] if self.resident else []}
            if url.endswith("/api/tags"):
                return {"models": [{"model": m} for m in self.tags]}
        if url.endswith("/api/ps"):  # a peer server
            return {"models": self.peer_models}
        return None

    def post(self, url, body):
        self.posts.append((url, body))
        self.resident = True
        return {"done": True}

    def runner(self, cmd_, **kw):
        self.commands.append(cmd_)
        if self.activate_rc == 0 and self.comes_up:
            self.up = True
        return subprocess.CompletedProcess(cmd_, self.activate_rc, "", "boom" if self.activate_rc else "")

    def stream(self, url, body):
        self.pulled = True
        if not self.pull_ok:
            yield {"error": "nope"}
            return
        self.tags.append(lo.MODEL)
        yield {"status": "success"}

    def ask(self, prompt):
        return self.answers.pop(0)

    def run(self, home, *, yes=True, tty=True, system="Darwin", environ=None, peers=("http://127.0.0.1:11434",)):
        return sl.run_setup_local(
            yes=yes, stdin_tty=tty, system=system, home=home, environ=environ if environ is not None else {},
            probe_fn=self.probe, which=self.which, fetch=self.fetch, post=self.post, runner=self.runner,
            stream=self.stream, ask=self.ask, sleep=lambda s: None, peer_urls=list(peers),
            ready_retries=3, ready_interval_s=0.0, python="/py")


def _state(tmp_path) -> Path:
    return tmp_path / "state"


def _nothing_written(tmp_path, box):
    home = tmp_path / "home"
    assert not (home / "Library").exists()
    assert not lo.sentinel_path().exists()
    assert not (_state(tmp_path) / ".env").exists()
    assert box.commands == [] and box.posts == [] and not box.pulled


# ── RAM gate ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gb", [8.0, 16.0, 32.0, 47.9])
def test_ram_gate_refuses_below_48_and_changes_nothing(tmp_path, gb):
    box = Box(total_gb=gb)
    r = box.run(tmp_path / "home")
    assert r["ok"] is False
    assert "48 GB" in r["error"] and f"{gb:.0f} GB" in r["error"]
    _nothing_written(tmp_path, box)


def test_ram_gate_unknown_ram_is_a_refusal(tmp_path):
    box = Box(total_gb=None)
    r = box.run(tmp_path / "home")
    assert r["ok"] is False and "could not read" in r["error"]
    _nothing_written(tmp_path, box)


@pytest.mark.parametrize("gb", [48.0, 51.5, 64.0])
def test_ram_gate_passes_at_48_and_above(tmp_path, gb):
    assert Box(total_gb=gb).run(tmp_path / "home")["ok"] is True


def test_ram_gate_constant_is_48():
    assert lo.MIN_RAM_GB == 48 and lo.ram_gate(48.0) is None and lo.ram_gate(47.99) is not None


# ── peer GPU memory ─────────────────────────────────────────────────────────

def test_peer_gpu_conflict_refuses_and_changes_nothing(tmp_path):
    # 20 GB resident on another Ollama + 22.4 GB = 42.4 GB > 75% of 48 GB (36 GB): the 2026-11-27 OOM shape.
    box = Box(total_gb=48.0, peer_models=[{"model": "qwen3-coder:30b", "size": 20e9}])
    r = box.run(tmp_path / "home")
    assert r["ok"] is False
    assert "qwen3-coder:30b" in r["error"] and "75%" in r["error"] and "ollama stop" in r["error"]
    _nothing_written(tmp_path, box)


def test_peer_that_fits_is_allowed(tmp_path):
    # 8 GB + 22.4 GB = 30.4 GB <= 36 GB
    box = Box(total_gb=48.0, peer_models=[{"model": "small", "size": 8e9}])
    assert box.run(tmp_path / "home")["ok"] is True


def test_peer_check_skipped_when_our_model_is_already_resident(tmp_path):
    box = Box(total_gb=48.0, peer_models=[{"model": "big", "size": 30e9}])
    box.up = box.resident = True
    assert box.run(tmp_path / "home")["ok"] is True


def test_peer_boundary_exactly_75_percent_passes():
    # need = 13.6 + 22.4 = 36.0 == 0.75 * 48
    fetch = lambda u: {"models": [{"model": "x", "size": 13.6e9}]}  # noqa: E731
    assert lo.peer_conflict(["http://p"], fetch, 48.0) is None
    assert lo.peer_conflict(["http://p"], lambda u: {"models": [{"model": "x", "size": 13.7e9}]}, 48.0)


# ── binary and consent ──────────────────────────────────────────────────────

def test_missing_ollama_binary_refuses_before_writing(tmp_path):
    box = Box(ollama=None)
    r = box.run(tmp_path / "home")
    assert r["ok"] is False and "ollama" in r["error"] and "ollama.com/download" in r["error"]
    _nothing_written(tmp_path, box)


def test_headless_without_yes_refuses_and_names_the_download(tmp_path):
    box = Box()
    r = box.run(tmp_path / "home", yes=False, tty=False)
    assert r["ok"] is False and "--yes" in r["error"] and "22 GB" in r["error"]
    _nothing_written(tmp_path, box)


def test_interactive_no_declines(tmp_path, capsys):
    box = Box()
    box.answers = ["n"]
    r = box.run(tmp_path / "home", yes=False, tty=True)
    assert r["ok"] is False and "declined" in r["error"]
    assert "22 GB" in capsys.readouterr().out  # the consent screen shows the size
    _nothing_written(tmp_path, box)


def test_interactive_yes_proceeds(tmp_path):
    box = Box()
    box.answers = ["y"]
    assert box.run(tmp_path / "home", yes=False, tty=True)["ok"] is True


def test_already_downloaded_model_says_so_and_skips_the_pull(tmp_path, capsys):
    box = Box(tags=[lo.MODEL])
    box.up = True  # a server is already answering, e.g. a re-run
    r = box.run(tmp_path / "home")
    assert r["ok"] is True and box.pulled is False and any("already downloaded" in a for a in r["actions"])


# ── happy path ──────────────────────────────────────────────────────────────

def test_happy_path_darwin(tmp_path):
    home = tmp_path / "home"
    box = Box()
    r = box.run(home)
    assert r["ok"] is True, r
    plist = home / "Library" / "LaunchAgents" / f"{lo.LABEL}.plist"
    text = plist.read_text()
    assert lo.LABEL == "com.llm_router.local-ollama" and lo.LABEL != pd.LABEL
    for needle in (f"<string>{lo.LABEL}</string>", "<string>serve</string>", "/opt/homebrew/bin/ollama",
                   "OLLAMA_HOST</key><string>127.0.0.1:11435", "OLLAMA_KEEP_ALIVE</key><string>-1",
                   "OLLAMA_MAX_LOADED_MODELS</key><string>1", "<key>RunAtLoad</key><true/>",
                   "<key>KeepAlive</key><true/>"):
        assert needle in text, needle
    assert box.commands and lo.LABEL in box.commands[0]
    assert "bootout" not in box.commands[0]  # never bootout + bootstrap (docs/BUGS.md P010-2)
    assert box.pulled is True
    url, body = box.posts[0]
    assert url == f"{lo.URL}/api/generate"
    assert body["keep_alive"] == -1 and body["options"]["num_ctx"] == 131072 and body["model"] == lo.MODEL
    assert body["stream"] is False
    env = (_state(tmp_path) / ".env").read_text()
    for k, v in lo.ENV_KEYS.items():
        assert f"{k}={v}\n" in env
    s = lo.read_sentinel()
    assert s["model"] == lo.MODEL and s["port"] == 11435 and s["env_written"] == lo.ENV_KEYS
    # only GET probes of our own server plus the generate POST were sent to :11435
    assert all(u.startswith(lo.URL) or u.endswith("/api/ps") for u in box.fetched)


def test_happy_path_linux_writes_a_systemd_unit(tmp_path):
    home = tmp_path / "home"
    box = Box()
    r = box.run(home, system="Linux")
    assert r["ok"] is True, r
    unit = (home / ".config" / "systemd" / "user" / "llm_router-local-ollama.service").read_text()
    assert "Environment=OLLAMA_HOST=127.0.0.1:11435" in unit and "ExecStart=/opt/homebrew/bin/ollama serve" in unit
    assert "Restart=on-failure" in unit
    assert "systemctl --user" in box.commands[0] and "llm_router-local-ollama" in box.commands[0]


def test_unsupported_platform_refuses_before_writing(tmp_path):
    box = Box()
    r = box.run(tmp_path / "home", system="Windows")
    assert r["ok"] is False and "not supported" in r["error"]
    _nothing_written(tmp_path, box)


def test_service_that_fails_to_start_is_reported_and_leaves_a_sentinel_for_uninstall(tmp_path):
    box = Box(activate_rc=1)
    r = box.run(tmp_path / "home")
    assert r["ok"] is False and "could not start" in r["error"] and "boom" in r["error"]
    assert box.posts == [] and not box.pulled
    assert lo.read_sentinel() is not None  # so `llm-router uninstall` can still remove the written plist
    assert not (_state(tmp_path) / ".env").exists()


def test_service_that_never_answers_is_reported(tmp_path):
    box = Box(comes_up=False)
    r = box.run(tmp_path / "home")
    assert r["ok"] is False and "did not answer" in r["error"]
    assert box.posts == [] and not box.pulled and not (_state(tmp_path) / ".env").exists()


def test_failed_pull_writes_no_env(tmp_path):
    box = Box(pull_ok=False)
    r = box.run(tmp_path / "home")
    assert r["ok"] is False and "pulling" in r["error"]
    assert box.posts == [] and not (_state(tmp_path) / ".env").exists()


def test_a_rerun_is_idempotent(tmp_path):
    home = tmp_path / "home"
    box = Box()
    assert box.run(home)["ok"]
    first = (_state(tmp_path) / ".env").read_text()
    assert box.run(home)["ok"]
    assert (_state(tmp_path) / ".env").read_text() == first  # keys not appended twice
    assert lo.read_sentinel()["env_written"] == lo.ENV_KEYS  # the re-run wrote nothing but must not forget run 1


# ── window: the num_ctx question ────────────────────────────────────────────

def test_preload_window_equals_what_the_proxy_requests(monkeypatch):
    """The proxy takes ``local_models.num_ctx(model)``; under the keys we write that is WINDOW, and
    without them it is the table's 32768 (the number that would load a second runner)."""
    assert local_models.num_ctx(lo.MODEL) == 32768
    for k, v in lo.ENV_KEYS.items():
        monkeypatch.setenv(k, v)
    assert local_models.num_ctx(lo.MODEL) == lo.WINDOW == 131072
    from llm_router.proxy.server import ProxyConfig
    assert ProxyConfig(model=f"ollama/{lo.MODEL}").num_ctx == lo.WINDOW


# ── .env: explicit set wins, uninstall removes only what we wrote ───────────

def test_explicit_env_value_is_never_overridden_and_survives_uninstall(tmp_path):
    state = _state(tmp_path)
    state.mkdir(parents=True)
    (state / ".env").write_text("LLM_ROUTER_LOCAL_MODEL_URL=http://192.168.1.5:11434\nOTHER=1\n")
    home = tmp_path / "home"
    box = Box()
    r = box.run(home)
    assert r["ok"], r
    written = lo.read_sentinel()["env_written"]
    assert "LLM_ROUTER_LOCAL_MODEL_URL" not in written and set(written) == {"LLM_ROUTER_LOCAL_MODEL", "LLM_ROUTER_WINDOW_STEP"}
    assert any("Kept your existing LLM_ROUTER_LOCAL_MODEL_URL" in a for a in r["actions"])
    sl.uninstall_local_ollama(home=home, system="Darwin", runner=box.runner)
    left = (state / ".env").read_text()
    assert "LLM_ROUTER_LOCAL_MODEL_URL=http://192.168.1.5:11434" in left and "OTHER=1" in left
    assert "WINDOW_STEP" not in left and "LLM_ROUTER_LOCAL_MODEL=" not in left


def test_a_value_set_in_the_process_environment_wins_too(tmp_path):
    box = Box()
    r = box.run(tmp_path / "home", environ={"LLM_ROUTER_WINDOW_STEP": "65536"})
    assert r["ok"]
    assert "WINDOW_STEP" not in (_state(tmp_path) / ".env").read_text()


def test_uninstall_after_a_rerun_removes_all_three_keys(tmp_path):
    home = tmp_path / "home"
    box = Box()
    assert box.run(home)["ok"]
    assert box.run(home)["ok"]  # re-run: finds the keys set, writes none
    sl.uninstall_local_ollama(home=home, system="Darwin", runner=box.runner)
    left = (_state(tmp_path) / ".env").read_text()
    assert not any(k in left for k in lo.ENV_KEYS), left


def test_peer_urls_include_the_quality_gate_list(monkeypatch):
    monkeypatch.setenv("QUALITY_GATE_PEER_OLLAMA_URLS", "http://127.0.0.1:11500, http://127.0.0.1:11501/,"
                       f"{lo.URL}")
    got = lo.peer_urls()
    assert "http://127.0.0.1:11500" in got and "http://127.0.0.1:11501" in got and lo.URL not in got
    monkeypatch.delenv("QUALITY_GATE_PEER_OLLAMA_URLS")
    assert "http://127.0.0.1:11500" not in lo.peer_urls()


# ── the proxy plist gets the local-agent args only when the sentinel exists ──

def _proxy_plist_after_install(tmp_path, home, monkeypatch):
    # Nothing answers on this port and the runner is a fake, so the install refuses AFTER writing the
    # plist; the written file is what is under test.
    monkeypatch.setattr(pd, "proxy_health", lambda host="127.0.0.1", port=0, timeout=1.0: False)
    cmd.install_proxy_default(port=18791, shim=False, home=home, system="Darwin",
                              runner=lambda c, **k: subprocess.CompletedProcess(c, 0, "", ""),
                              health_retries=1, health_interval_s=0.0)
    return (home / "Library" / "LaunchAgents" / f"{pd.LABEL}.plist").read_text()


def test_proxy_plist_without_the_sentinel_is_unchanged(tmp_path, monkeypatch):
    text = _proxy_plist_after_install(tmp_path, tmp_path / "svc", monkeypatch)
    assert "--serve" not in text and "--local-with-tiers" not in text and "--ollama-url" not in text
    assert text == pd.render_launchd_plist(pd.sys.executable, tmp_path / "svc", port=18791,
                                           steps=pd.DEFAULT_STEPS, tiers=pd.DEFAULT_TIERS)


def test_proxy_plist_with_the_sentinel_has_the_owners_local_agent_args(tmp_path, monkeypatch):
    lo.write_sentinel(system="Darwin", ollama_bin="/x/ollama", env_written={})
    home = tmp_path / "svc"
    text = _proxy_plist_after_install(tmp_path, home, monkeypatch)
    for a in ("--serve", "local-agent", "--local-with-tiers", "--model", f"ollama/{lo.MODEL}",
              "--ollama-url", lo.URL, "--trim", "none", "--keep-alive", "-1", "--tiers", "conversation"):
        assert f"<string>{a}</string>" in text, a
    assert "--tier-policy" not in text  # no claude_tiers.yaml on this machine: the bundled policy applies
    # ordering the live job proves: --steps off ... --tiers X then the extras
    assert text.index("--tiers") < text.index("--serve")


def test_proxy_args_name_the_tier_policy_only_when_the_file_exists(tmp_path):
    lo.write_sentinel(system="Darwin", ollama_bin="/x/ollama", env_written={})
    home = tmp_path / "h"
    pol = home / ".llm-router" / "claude_tiers.yaml"
    pol.parent.mkdir(parents=True)
    assert "--tier-policy" not in lo.proxy_args(home)
    pol.write_text("x: 1\n")
    args = lo.proxy_args(home)
    assert args[args.index("--tier-policy") + 1] == str(pol)


def test_proxy_args_empty_without_sentinel(tmp_path):
    assert lo.proxy_args(tmp_path) == []


def test_systemd_proxy_unit_carries_the_args_too():
    unit = pd.render_systemd_user_unit("/py", port=8797, steps="off", tiers="conversation",
                                       extra_args=["--serve", "local-agent", "--model", "ollama/x:y"])
    assert "--tiers conversation --serve local-agent --model ollama/x:y\n" in unit
    assert "--serve" not in pd.render_systemd_user_unit("/py", port=8797, steps="off", tiers="conversation")


def test_existing_proxy_install_gets_the_args_written_but_not_restarted(tmp_path, monkeypatch):
    home = tmp_path / "svc"
    plist = home / "Library" / "LaunchAgents" / f"{pd.LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text(pd.render_launchd_plist("/py", home, port=18793, steps="off", tiers="conversation"))
    lo.write_sentinel(system="Darwin", ollama_bin="/x/ollama", env_written={})
    pd.write_sentinel(port=18792, steps="off", tiers="conversation", label=pd.LABEL, system="Darwin",
                      upstream_port=18793, shim_label=pd.SHIM_LABEL)
    monkeypatch.setattr(pd, "proxy_health", lambda host="127.0.0.1", port=0, timeout=1.0: True)
    r = cmd.install_proxy_default(port=18792, upstream_port=18793, home=home, system="Darwin",
                                  runner=lambda c, **k: pytest.fail("must not restart a healthy proxy"))
    assert "--serve" in plist.read_text()
    assert any("unload" in a for a in r["actions"])  # the owner step is named; nothing is booted out


# ── uninstall ───────────────────────────────────────────────────────────────

def test_uninstall_stops_removes_and_keeps_the_model(tmp_path):
    home = tmp_path / "home"
    box = Box()
    assert box.run(home)["ok"]
    plist = home / "Library" / "LaunchAgents" / f"{lo.LABEL}.plist"
    assert plist.exists()
    box.commands.clear()
    acts = sl.uninstall_local_ollama(home=home, system="Darwin", runner=box.runner)
    pre = home / "Library" / "LaunchAgents" / f"{lo.PRELOAD_LABEL}.plist"
    assert box.commands == [f"launchctl unload {pre}", f"launchctl unload {plist}"]  # stop BEFORE delete
    assert not plist.exists() and not pre.exists() and not lo.sentinel_path().exists()
    assert (_state(tmp_path) / ".env").read_text().strip() == ""
    assert any("Kept the downloaded model" in a for a in acts)
    assert not any("ollama" in str(c) and "rm" in str(c) for c in box.commands)


def test_uninstall_purge_models_runs_ollama_rm_against_our_server_first(tmp_path):
    home = tmp_path / "home"
    box = Box()
    assert box.run(home)["ok"]
    seen = []

    def runner(c, **kw):
        seen.append((c, kw.get("env", {}).get("OLLAMA_HOST")))
        return subprocess.CompletedProcess(c, 0, "", "")

    acts = sl.uninstall_local_ollama(home=home, system="Darwin", runner=runner, purge_models=True)
    assert seen[0][0] == ["/opt/homebrew/bin/ollama", "rm", lo.MODEL] and seen[0][1] == "127.0.0.1:11435"
    assert "launchctl unload" in seen[1][0]  # rm needs the server up, so the service stops after
    assert any(f"Removed model {lo.MODEL}" in a for a in acts)


def test_uninstall_without_sentinel_does_nothing(tmp_path):
    box = Box()
    assert sl.uninstall_local_ollama(home=tmp_path, system="Darwin", runner=box.runner) == []
    assert box.commands == []


def test_run_uninstall_calls_local_cleanup_and_never_the_real_runner_without_a_sentinel(tmp_path, monkeypatch):
    import llm_router.commands.uninstall as un
    called = []
    monkeypatch.setattr(sl, "uninstall_local_ollama", lambda **kw: called.append(kw) or [])
    monkeypatch.setattr("llm_router.install_hooks.uninstall", lambda: [])
    monkeypatch.setattr("llm_router.install_hooks.uninstall_claw_code", lambda: [])
    monkeypatch.setattr("llm_router.install_hooks.uninstall_ide_configs", lambda: [])
    monkeypatch.setattr("llm_router.install_manifest.apply_uninstall", lambda: [])
    monkeypatch.setattr("llm_router.commands.install.uninstall_host_integrations", lambda: [])
    monkeypatch.setattr(un, "_remove_derived_state", lambda: [])
    monkeypatch.setattr("llm_router.commands.proxy_default.uninstall_proxy_default", lambda: [])
    monkeypatch.setattr("llm_router.daily_schedule.uninstall", lambda **k: {"status": "none"})
    un._run_uninstall(["--purge-models"])
    assert len(called) == 1 and called[0]["purge_models"] is True
    called.clear()
    un._run_uninstall([])
    assert called[0]["purge_models"] is False


# ── doctor ──────────────────────────────────────────────────────────────────

NOW = 1_800_000_000.0


def _rows(**over):
    base = dict(
        total_gb=48.0, fetch=lambda u: ({"models": [{"model": lo.MODEL}]} if u.endswith("/api/ps") else {"v": 1}),
        port_up=lambda p: True, settings_base_url="http://127.0.0.1:8787",
        environ={"LLM_ROUTER_LOCAL_MODEL_URL": lo.URL},
        ledger_rows=[{"ts": NOW - 60, "decision": ledger.DECISION_SERVED}], now=NOW)
    base.update(over)
    return lo.routing_rows(**base)


def test_doctor_rows_all_ok():
    rows = _rows()
    assert len(rows) == 7 and all(ok for _n, ok, _d in rows), rows


@pytest.mark.parametrize("over,failing", [
    ({"total_gb": 32.0}, "RAM"),
    ({"fetch": lambda u: None}, "answers on :11435"),  # server down: not resident either (2 rows)
    ({"fetch": lambda u: {"models": []} if u.endswith("/api/ps") else {"v": 1}}, "resident"),
    ({"port_up": lambda p: False}, "shim"),
    ({"settings_base_url": None}, "ANTHROPIC_BASE_URL"),
    ({"settings_base_url": "https://corp.example.com"}, "ANTHROPIC_BASE_URL"),
    ({"environ": {}}, "LLM_ROUTER_LOCAL_MODEL_URL"),
    ({"ledger_rows": []}, "served locally"),
    ({"ledger_rows": [{"ts": NOW - 90000, "decision": ledger.DECISION_SERVED}]}, "served locally"),
    ({"ledger_rows": [{"ts": NOW - 60, "decision": "forwarded"}]}, "served locally"),
])
def test_doctor_each_requirement_can_fail(over, failing):
    bad = [n for n, ok, _d in _rows(**over) if not ok]
    assert failing in bad[0], bad
    assert len(bad) == (2 if failing.startswith("answers") else 1), bad


def test_doctor_section_hint_when_never_set_up(capsys):
    from llm_router.commands import doctor
    issues: list[str] = []
    doctor._local_ollama_section(issues)
    out = capsys.readouterr().out
    assert "setup-local" in out and "NONE" not in out and issues == []


def test_doctor_section_prints_none_and_the_fix_when_a_row_fails(capsys):
    from llm_router.commands import doctor
    lo.write_sentinel(system="Darwin", ollama_bin="/x", env_written={})
    issues: list[str] = []
    doctor._local_ollama_section(
        issues, fetch=lambda u: None, probe_fn=lambda: {"total_gb": 48.0}, port_up=lambda p: False,
        settings_base_url=None, environ={}, ledger_rows=[], now=NOW)
    out = capsys.readouterr().out
    assert "local routing: NONE" in out and "llm-router setup-local" in out
    assert len(issues) == 1 and "not live" in issues[0]


def test_doctor_section_all_green_has_no_none(capsys):
    from llm_router.commands import doctor
    lo.write_sentinel(system="Darwin", ollama_bin="/x", env_written={})
    issues: list[str] = []
    doctor._local_ollama_section(
        issues, fetch=lambda u: ({"models": [{"model": lo.MODEL}]} if u.endswith("/api/ps") else {"v": 1}),
        probe_fn=lambda: {"total_gb": 64.0}, port_up=lambda p: True,
        settings_base_url="http://127.0.0.1:8787", environ={"LLM_ROUTER_LOCAL_MODEL_URL": lo.URL},
        ledger_rows=[{"ts": NOW - 5, "decision": ledger.DECISION_SERVED}], now=NOW)
    assert "NONE" not in capsys.readouterr().out and issues == []


# ── cli ─────────────────────────────────────────────────────────────────────

def test_cli_help_is_inert_and_unknown_args_are_refused(capsys):
    assert sl.cmd_setup_local(["--help"]) == 0
    assert "48 GB" in capsys.readouterr().out
    assert sl.cmd_setup_local(["--bogus"]) == 2
    assert lo.read_sentinel() is None


def test_cli_dispatches_setup_local(monkeypatch):
    import llm_router.cli as cli
    seen = []
    monkeypatch.setattr(sl, "cmd_setup_local", lambda a: seen.append(a) or 0)
    monkeypatch.setattr("sys.argv", ["llm-router", "setup-local", "--yes"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0 and seen == [["--yes"]]
    assert "setup-local" in cli._KNOWN_SUBCOMMANDS


# ── reboot safety: `setup-local --preload` and the login job ─────────────────

def _fresh_sentinel():
    lo.write_sentinel(system="Darwin", ollama_bin="/x/ollama", env_written={}, preload_label=lo.PRELOAD_LABEL)


def _preload(box, home, *, retries=3, peers=("http://127.0.0.1:11434",), probe=None):
    return sl.run_preload(system="Darwin", home=home, probe_fn=probe or box.probe, fetch=box.fetch,
                          post=box.post, runner=box.runner, sleep=lambda s: None, peer_urls=list(peers),
                          retries=retries, interval_s=0.0)


def _log_text():
    return (_state_dir() / "logs" / "local-ollama-preload.log").read_text()


def _state_dir():
    from llm_router import paths
    return paths.llm_router_home()


def test_preload_plist_renders_run_once_at_login():
    t = lo.render_preload_plist("/py", Path("/h"))
    assert lo.PRELOAD_LABEL == "com.llm_router.local-ollama-preload"
    for needle in ("<string>/py</string>", "<string>llm_router.cli</string>", "<string>setup-local</string>",
                   "<string>--preload</string>", "<key>RunAtLoad</key><true/>", "<key>KeepAlive</key><false/>"):
        assert needle in t, needle
    assert "StartInterval" not in t
    assert "--preload" in lo.render_preload_systemd_unit("/py") and "oneshot" in lo.render_preload_systemd_unit("/py")


def test_setup_writes_the_preload_job_records_it_and_does_not_start_it(tmp_path):
    home = tmp_path / "home"
    box = Box()
    assert box.run(home)["ok"]
    pre = home / "Library" / "LaunchAgents" / f"{lo.PRELOAD_LABEL}.plist"
    assert "--preload" in pre.read_text() and "<string>/py</string>" in pre.read_text()
    assert lo.read_sentinel()["preload_label"] == lo.PRELOAD_LABEL
    assert not any(lo.PRELOAD_LABEL in str(c) for c in box.commands)  # written, not loaded


def test_preload_success_loads_pinned_at_the_proxy_window_and_logs_one_line(tmp_path):
    _fresh_sentinel()
    box = Box()
    box.up = True
    assert _preload(box, tmp_path) == 0
    _url, body = box.posts[0]
    assert body["keep_alive"] == -1 and body["options"]["num_ctx"] == lo.WINDOW
    assert box.commands == []  # no proxy sentinel: nothing restarted
    log = _log_text().splitlines()
    assert len(log) == 1 and "rc=0" in log[0]


def test_preload_failure_is_non_zero_when_the_server_never_answers(tmp_path, capsys):
    _fresh_sentinel()
    box = Box()  # never comes up
    assert _preload(box, tmp_path) == 1
    assert box.posts == [] and "rc=1" in _log_text() and "did not answer" in capsys.readouterr().err


def test_preload_failure_is_non_zero_when_the_model_does_not_load(tmp_path):
    _fresh_sentinel()
    box = Box()
    box.up = True
    box.post = lambda url, body: None
    assert _preload(box, tmp_path) == 1 and "did not load" in _log_text()


def test_preload_without_setup_is_non_zero(tmp_path):
    box = Box()
    box.up = True
    assert _preload(box, tmp_path) == 1 and box.posts == []


def test_preload_peer_refusal_skips_the_load_and_the_restart(tmp_path):
    _fresh_sentinel()
    pd.write_sentinel(port=8787, steps="off", tiers="conversation", label=pd.LABEL, system="Darwin")
    box = Box(peer_models=[{"model": "qwen3-coder:30b", "size": 20e9}])
    box.up = True
    assert _preload(box, tmp_path) == 1
    assert box.posts == [] and box.commands == []
    assert "not loading" in _log_text()


def test_preload_kickstarts_the_proxy_only_when_its_sentinel_exists(tmp_path):
    _fresh_sentinel()
    box = Box()
    box.up = True
    assert _preload(box, tmp_path) == 0 and box.commands == []
    pd.write_sentinel(port=8787, steps="off", tiers="conversation", label=pd.LABEL, system="Darwin")
    assert _preload(box, tmp_path) == 0
    assert len(box.commands) == 1
    assert f"kickstart -k gui/$(id -u)/{pd.LABEL}" in box.commands[0] and "bootout" not in box.commands[0]


def test_preload_failed_proxy_restart_is_non_zero(tmp_path):
    _fresh_sentinel()
    pd.write_sentinel(port=8787, steps="off", tiers="conversation", label=pd.LABEL, system="Darwin")
    box = Box(activate_rc=1)
    box.up = True
    assert _preload(box, tmp_path) == 1 and "could not restart the proxy" in _log_text()


def test_cli_preload_flag_dispatches_to_run_preload(monkeypatch):
    seen = []
    monkeypatch.setattr(sl, "run_preload", lambda **kw: seen.append(kw) or 7)
    assert sl.cmd_setup_local(["--preload"]) == 7 and len(seen) == 1


# ── the guard ───────────────────────────────────────────────────────────────

def test_no_test_in_this_file_reached_a_real_runner(tmp_path):
    box = Box()
    assert box.run(tmp_path / "home")["ok"]
    sl.uninstall_local_ollama(home=tmp_path / "home", system="Darwin", runner=box.runner)
    assert REAL_CALLS == []


def test_the_guard_trips_on_a_real_call():
    _EXPECT_TRIPS[0] = True  # this test calls the real things on purpose; the teardown stands down
    with pytest.raises(AssertionError, match="real subprocess.run"):
        subprocess.run(["true"])
    ok, detail = pd.activate_service(Path("/nonexistent"), "true")  # swallows the error by design...
    assert ok is False and "real activate_service default runner" in detail  # ...but it was recorded
    with pytest.raises(AssertionError, match="real socket.create_connection"):
        pd.proxy_health("127.0.0.1", 1)
    assert REAL_CALLS == ["subprocess.run", "activate_service default runner", "socket.create_connection"]
