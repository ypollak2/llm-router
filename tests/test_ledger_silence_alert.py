"""P0.14-d: the 30 min ledger-silence alert (PLAN v16 R8 A.2).

Incident 2026-10-08: ``proxy_calls.jsonl`` wrote its last row at 16:55:02Z and nothing
until 19:02:24Z, while organic Claude Code sessions kept typing (the user-level
``ANTHROPIC_BASE_URL`` had been removed; ``proxy_default.json`` still said enabled). The
24 h ``proxy_rows_24h`` window still held rows, so ``kpi`` said ``warn=false``.

The replay below uses the real timestamps of that window (hook turns from
``hook_latency.jsonl``, the last proxy row before and the first after the gap), in the
row shape of the time: no ``host`` field, session tag files with ``entrypoint: cli``.
Session ids are shortened prefixes of the real ones. It is evaluated "now": every stamp
is shifted so the evaluation instant is the wall clock, which lets ``doctor`` and the
SessionStart hook (which read the clock themselves) see the same replay as ``kpi``.

Pinned (6 fixtures + 1 mutant, R8 MUST P0.14-d):

* outage replay  -> SILENT within 30 min + one poll on kpi, doctor and SessionStart;
                    ``ledger_gaps`` lists the replayed gap
* idle (no hook turns), rows present, Codex-only turns, project-level override,
  sentinel ``routing_opt_out``  -> no SILENT on any surface
* mutant: ``LIVENESS_WINDOW_MIN`` = 24 h  -> the outage fixture no longer fires

Also pinned: Codex running this repo's own plugin (``auto-route.py`` under
``~/.codex/plugins`` or ``CODEX_PLUGIN_ROOT``) does not fire; a session launched with its
own ``ANTHROPIC_BASE_URL`` does not fire; the tail reader's chunk growth and rotated-file
read; doctor's override findings survive a failing alert; the override rule agrees with
session-start's ``_effective_base_url``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from llm_router import hook_latency as hl
from llm_router import paths
from llm_router import proxy_liveness as plv
from llm_router import session_kind as sk
from llm_router.commands import doctor, kpi
from llm_router.proxy import ledger as pl

HOOK_PATH = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "session-start.py"
HOST_ENV = ("CLAUDE_PLUGIN_ROOT", "CODEX_PLUGIN_ROOT", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


def _utc(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


#: The last proxy row before the gap and the first after it (proxy_calls.jsonl).
LAST_ROW = _utc("2026-10-08T16:55:02.898924")
NEXT_ROW = _utc("2026-10-08T19:02:24.406930")
POLL_S = 60.0
#: R8: SILENT fires at <= 30 min + one poll interval after the last proxy row.
BOUNDARY = LAST_ROW + 30 * 60 + POLL_S
#: The instant the surfaces are checked in the replay: just past the boundary.
EVAL = BOUNDARY

RESEARCH, ORGANIC_A, ORGANIC_B = "572a0276-research", "c3e525dd-organic", "6cf3b1b8-organic"

#: Real UserPromptSubmit auto-route turns 16:40-17:56Z on 2026-10-08 (sid "" = no session id).
TURNS = [
    ("16:40:03.384", ""), ("16:40:37.429", ""), ("16:41:30.668", ""), ("16:51:11.236", ""),
    ("16:57:26.419", RESEARCH), ("17:01:31.750", RESEARCH), ("17:04:35.205", RESEARCH),
    ("17:06:37.118", RESEARCH), ("17:10:36.142", RESEARCH), ("17:14:11.988", ORGANIC_A),
    ("17:16:46.815", RESEARCH), ("17:17:16.362", RESEARCH), ("17:19:16.852", ORGANIC_B),
    ("17:19:35.617", ORGANIC_B), ("17:34:08.626", ORGANIC_B), ("17:47:53.180", ORGANIC_B),
    ("17:56:40.314", ORGANIC_B),
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    (tmp_path / "claude-projects").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "claude-projects"))
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    # The host a hook row records comes from these (hook_latency.detect_host); the suite
    # itself may run inside Claude Code or Codex, so none may leak in.
    for var in HOST_ENV:
        monkeypatch.delenv(var, raising=False)
    from llm_router import failopen

    failopen.reset_unpersisted()
    failopen.reset_cache()
    sk._FOUND.clear()
    yield
    sk._FOUND.clear()
    failopen.reset_unpersisted()
    failopen.reset_cache()


class Replay:
    """The 2026-10-08 window, every stamp shifted by ``shift`` so EVAL lands on ``now``."""

    def __init__(self, root: Path, *, now: float | None = None) -> None:
        self.now = time.time() if now is None else now
        self.shift = self.now - EVAL
        self.root = root
        self.projects = {RESEARCH: root / "rsi", ORGANIC_A: root / "proj_a", ORGANIC_B: root / "proj_b"}
        for p in self.projects.values():
            p.mkdir(parents=True, exist_ok=True)

    def t(self, real: float) -> float:
        return real + self.shift

    def sentinel(self, **extra) -> None:
        doc = {"enabled": True, "port": 8787, "upstream_port": 8797, **extra}
        p = paths.state_path("proxy_default.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc), encoding="utf-8")

    def tags(self, *, entrypoint: str | None = "cli") -> None:
        kinds = {RESEARCH: "research", ORGANIC_A: "organic", ORGANIC_B: "organic"}
        for sid, kind in kinds.items():
            p = sk._tag_path(sid)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"session_id": sid, "kind": kind, "cwd": str(self.projects[sid]),
                                     "entrypoint": entrypoint, "ts": self.t(LAST_ROW - 3600)}),
                         encoding="utf-8")

    def proxy(self, *, extra: list[float] = ()) -> None:
        stamps = [LAST_ROW - 20.0 * i for i in range(45, 0, -1)] + [LAST_ROW]
        stamps += [NEXT_ROW + 20.0 * i for i in range(30)] + list(extra)
        pl.ledger_path().parent.mkdir(parents=True, exist_ok=True)
        with pl.ledger_path().open("a", encoding="utf-8") as fh:
            for ts in stamps:
                sid = RESEARCH if ts <= LAST_ROW else ORGANIC_B
                fh.write(json.dumps({"ts": self.t(ts), "session_id": sid, "model": "m"}) + "\n")

    def turns(self, *, host: str | None = None) -> None:
        for hhmmss, sid in TURNS:
            ts = self.t(_utc(f"2026-10-08T{hhmmss}"))
            assert hl.record("auto-route", "UserPromptSubmit", 120.0, now=ts, session_id=sid or None, host=host)

    def outage(self) -> "Replay":
        self.sentinel()
        self.tags()
        self.proxy()
        self.turns()
        return self


def _load_session_start():
    spec = importlib.util.spec_from_file_location("session_start_hook_ledger_silence", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


def _session_start_output(monkeypatch) -> str:
    """The SessionStart hook's ``main()`` in-process: its JSON output, as text. The usage
    refresh, the #342 port probe and the detached child are stubbed (not under test)."""
    mod = _load_session_start()
    monkeypatch.setattr(mod, "_refresh_claude_usage_nonblocking", lambda: "")
    monkeypatch.setattr(mod, "_check_proxy_default_health", lambda: "")
    monkeypatch.setattr(mod, "_spawn_background_session_work", lambda *a, **k: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "new-session", "cwd": "/"})))
    prev, out = sys.stdout, io.StringIO()
    sys.stdout = out
    try:
        mod.main()
    finally:
        sys.stdout = prev
    return out.getvalue()


def _surfaces(now: float, monkeypatch) -> dict[str, bool]:
    """SILENT on each of the three surfaces, evaluated at the wall clock ``now``."""
    card = kpi.compute_scorecard(days=1, now=now)
    kpi_text = kpi.render_scorecard(card)
    _code, issues = doctor._run_doctor()
    start = _session_start_output(monkeypatch)
    return {
        "kpi": "SILENT proxy ledger" in kpi_text and card["proxy_liveness"]["ledger_silence"]["silent"] is True,
        "doctor": any(i.startswith("proxy bypass:") and "SILENT proxy ledger" in i for i in issues),
        "session_start": "SILENT proxy ledger" in start,
    }


def _first_fire(replay: Replay) -> float | None:
    """Poll every POLL_S from the last proxy row; the first poll at which SILENT fires."""
    for k in range(0, 200):
        at = LAST_ROW + k * POLL_S
        if plv.ledger_silence(now=replay.t(at))["silent"]:
            return at
    return None


# -- fixture 1: the outage replay -----------------------------------------------------


def test_outage_replay_fires_within_30_min_plus_one_poll(tmp_path):
    replay = Replay(tmp_path).outage()
    fired = _first_fire(replay)
    assert fired is not None and fired <= BOUNDARY
    # ... and not before the window has passed: the last row is still inside it at 29 min.
    early = plv.ledger_silence(now=replay.t(LAST_ROW + 29 * 60))
    assert early["state"] == "ok" and early["silent"] is False
    s = plv.ledger_silence(now=replay.t(EVAL))
    assert s["state"] == "SILENT" and s["proxy_rows"] == 0
    # 17:14 (c3e525dd) and 17:19 x2 (6cf3b1b8): the research session's turns do not count.
    assert s["organic_cc_turns"] == 3 and s["sessions"] == 2
    assert s["excluded"]["not_organic"] >= 1


def test_outage_replay_is_silent_on_kpi_doctor_and_session_start(tmp_path, monkeypatch):
    replay = Replay(tmp_path).outage()
    assert _surfaces(replay.now, monkeypatch) == {"kpi": True, "doctor": True, "session_start": True}


def test_outage_replay_rows_carrying_the_new_host_field_fire_too(tmp_path):
    replay = Replay(tmp_path)
    replay.sentinel()
    replay.tags(entrypoint=None)          # host comes from the row, not the tag
    replay.proxy()
    replay.turns(host="claude_code")
    assert plv.ledger_silence(now=replay.t(EVAL))["state"] == "SILENT"


def test_ledger_gaps_in_the_gate_window_lists_the_replayed_gap(tmp_path):
    """A gate file is written from ``kpi --json --since --until``: its ``ledger_gaps``."""
    replay = Replay(tmp_path).outage()
    card = kpi.compute_scorecard(since=replay.t(_utc("2026-10-08T16:00:00")),
                                 until=replay.t(_utc("2026-10-08T19:30:00")))
    gaps = json.loads(json.dumps(card, default=str))["proxy_liveness"]["ledger_gaps"]
    assert len(gaps) == 1
    g = gaps[0]
    assert g["start_ts"] == pytest.approx(replay.t(LAST_ROW), abs=1e-3)
    assert g["end_ts"] == pytest.approx(replay.t(NEXT_ROW), abs=1e-3)
    assert g["organic_cc_turns"] == 6 and g["open_start"] is False and g["open_end"] is False
    assert "ledger_gaps: 1 (" in kpi.render_scorecard(card)


# -- fixtures 2-6: must not fire --------------------------------------------------------


def _assert_quiet_everywhere(replay: Replay, state: str, monkeypatch) -> None:
    s = plv.ledger_silence(now=replay.t(EVAL))
    assert s["state"] == state and s["silent"] is False and s["message"] is None
    assert _first_fire(replay) is None
    assert _surfaces(replay.now, monkeypatch) == {"kpi": False, "doctor": False, "session_start": False}


def test_idle_no_hook_turns_does_not_fire(tmp_path, monkeypatch):
    replay = Replay(tmp_path)
    replay.sentinel()
    replay.tags()
    replay.proxy()
    _assert_quiet_everywhere(replay, "quiet", monkeypatch)


def test_rows_present_does_not_fire(tmp_path, monkeypatch):
    replay = Replay(tmp_path)
    # The proxy kept writing through the outage window: one row every 10 min.
    replay.outage().proxy(extra=[LAST_ROW + 600.0 * i for i in range(1, 13)])
    s = plv.ledger_silence(now=replay.t(EVAL))
    assert s["state"] == "ok" and s["proxy_rows"] >= 1
    _assert_quiet_everywhere(replay, "ok", monkeypatch)


@pytest.mark.parametrize("shape", ["host_field", "legacy_no_entrypoint"])
def test_codex_only_hook_turns_do_not_fire(tmp_path, monkeypatch, shape):
    replay = Replay(tmp_path)
    replay.sentinel()
    replay.proxy()
    if shape == "host_field":             # rows written by codex-auto-route.py from now on
        replay.tags()
        replay.turns(host="codex")
    else:                                 # older rows: no host, and Codex exports no CLAUDE_CODE_ENTRYPOINT
        replay.tags(entrypoint=None)
        replay.turns()
    s = plv.ledger_silence(now=replay.t(EVAL))
    assert s["excluded"]["other_host"] >= 3
    _assert_quiet_everywhere(replay, "quiet", monkeypatch)


def test_project_level_override_does_not_fire(tmp_path, monkeypatch):
    replay = Replay(tmp_path).outage()
    for sid in (ORGANIC_A, ORGANIC_B):
        f = replay.projects[sid] / ".claude" / "settings.local.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}}), encoding="utf-8")
    s = plv.ledger_silence(now=replay.t(EVAL))
    assert s["excluded"]["project_override"] == 3
    _assert_quiet_everywhere(replay, "quiet", monkeypatch)


def test_project_settings_pointing_at_the_proxy_are_not_an_override(tmp_path):
    replay = Replay(tmp_path).outage()
    for sid in (ORGANIC_A, ORGANIC_B):
        f = replay.projects[sid] / ".claude" / "settings.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://localhost:8787"}}), encoding="utf-8")
    assert plv.ledger_silence(now=replay.t(EVAL))["state"] == "SILENT"


@pytest.mark.parametrize("sentinel", [{"routing_opt_out": True}, {"enabled": False}])
def test_sentinel_opt_out_does_not_fire(tmp_path, monkeypatch, sentinel):
    replay = Replay(tmp_path).outage()
    replay.sentinel(**sentinel)
    _assert_quiet_everywhere(replay, "off", monkeypatch)


def test_no_sentinel_does_not_fire(tmp_path):
    replay = Replay(tmp_path).outage()
    paths.state_path("proxy_default.json").unlink()
    assert plv.ledger_silence(now=replay.t(EVAL))["state"] == "off"


# -- the mutant: the test must see the window --------------------------------------------


def test_mutant_24h_window_makes_the_outage_fixture_fail(tmp_path, monkeypatch):
    """R8: mutating ``liveness_window_min`` to 24 h makes the outage fixture fail."""
    replay = Replay(tmp_path).outage()
    assert _first_fire(replay) is not None                    # the real window fires ...
    monkeypatch.setattr(plv, "LIVENESS_WINDOW_MIN", 24 * 60.0)
    assert _first_fire(replay) is None                        # ... the 24 h one does not
    assert plv.ledger_silence(now=replay.t(EVAL))["state"] == "ok"


# -- host on the hook row ------------------------------------------------------------------


def _run_hook(monkeypatch, script: str, *, ts: float | None = None, session_id: str | None = None) -> dict:
    """The real write path a hook process takes: begin(), set_session(), _finish()."""
    monkeypatch.setattr(hl, "_pending", None)
    monkeypatch.setattr(hl, "_session_id", None)
    monkeypatch.setattr(hl, "_registered", True)          # no atexit handler in the test process
    monkeypatch.setattr(hl.sys, "argv", [script])
    if ts is not None:
        monkeypatch.setattr(hl, "_wall", lambda: ts)
    hl.begin("auto-route", "UserPromptSubmit")
    hl.set_session(session_id)
    hl._finish()
    return hl.read_rows()[-1]


CODEX_CACHE_SCRIPT = "/Users/x/.codex/plugins/cache/llm-router/hooks/auto-route.py"


@pytest.mark.parametrize("script, env, host", [
    ("/Users/x/.claude/hooks/llm_router-auto-route.py", {}, "claude_code"),
    ("/plugin/hooks/auto-route.py", {"CLAUDE_PLUGIN_ROOT": "/plugin"}, "claude_code"),
    ("/opt/hooks/auto-route.py", {"CLAUDE_CODE_ENTRYPOINT": "cli"}, "claude_code"),
    ("/Users/x/.llm-router/hooks/codex-auto-route.py", {}, "codex"),
    ("/Users/x/.llm-router/hooks/gemini-cli-auto-route.py", {}, "gemini"),
    # .codex-plugin/hooks.json runs ${CODEX_PLUGIN_ROOT}/hooks/auto-route.py: same file name.
    (CODEX_CACHE_SCRIPT, {}, "codex"),
    (CODEX_CACHE_SCRIPT, {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}, "codex"),
    ("/dev/llm-router/hooks/auto-route.py", {"CODEX_PLUGIN_ROOT": "/dev/llm-router", "CLAUDECODE": "1"}, "codex"),
    # Nothing says which CLI: no host on the row (the reader falls back to the session tag).
    ("/plugin/hooks/auto-route.py", {}, None),
])
def test_hook_row_records_the_host_only_on_positive_evidence(monkeypatch, script, env, host):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert _run_hook(monkeypatch, script).get("host") == host


@pytest.mark.parametrize("variant", ["codex_plugin_cache", "codex_plugin_root_from_a_claude_shell"])
def test_codex_running_the_repos_own_plugin_does_not_fire(tmp_path, monkeypatch, variant):
    """Review of 72ccc62f: Codex runs this repo's plugin as ``auto-route.py`` (no ``codex-``
    prefix). Its turns were labelled ``claude_code`` and fired SILENT on a Codex-only half hour.
    The tag is the one auto-route writes for a Codex session: cwd only, no entrypoint, organic."""
    replay = Replay(tmp_path)
    replay.sentinel()
    replay.tags(entrypoint=None)
    replay.proxy()
    if variant == "codex_plugin_cache":
        script = CODEX_CACHE_SCRIPT
    else:
        root = tmp_path / "llm-router-checkout"
        (root / "hooks").mkdir(parents=True)
        script = str(root / "hooks" / "auto-route.py")
        monkeypatch.setenv("CODEX_PLUGIN_ROOT", str(root))
        monkeypatch.setenv("CLAUDECODE", "1")           # Codex started from a Claude Code shell
    turns = [(hhmmss, sid) for hhmmss, sid in TURNS if sid]
    for hhmmss, sid in turns:
        row = _run_hook(monkeypatch, script, ts=replay.t(_utc(f"2026-10-08T{hhmmss}")), session_id=sid)
        assert row.get("host") == "codex"
    s = plv.ledger_silence(now=replay.t(EVAL))
    assert s["state"] == "quiet" and s["organic_cc_turns"] == 0
    assert s["excluded"]["other_host"] >= 3
    _assert_quiet_everywhere(replay, "quiet", monkeypatch)


# -- a session launched with its own ANTHROPIC_BASE_URL -------------------------------------


@pytest.mark.parametrize("value, cls", [
    ("https://api.anthropic.com", "other"),
    ("http://localhost:8787", "loopback:8787"),
    ("http://127.0.0.1:9999/v1", "loopback:9999"),
    ("sk-ant-not-a-url", "other"),
])
def test_hook_row_records_the_inherited_base_url_class_never_the_value(monkeypatch, value, cls):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", value)
    row = _run_hook(monkeypatch, "/Users/x/.claude/hooks/llm_router-auto-route.py")
    assert row["base_url"] == cls
    assert value not in hl.store_path().read_text(encoding="utf-8")


@pytest.mark.parametrize("base_url, state", [("other", "quiet"), ("loopback:9999", "quiet"),
                                             ("loopback:8787", "SILENT"), (None, "SILENT")])
def test_a_session_launched_with_its_own_base_url_does_not_fire(tmp_path, base_url, state):
    """Same class as the project override: a session started with ``ANTHROPIC_BASE_URL`` in its
    shell bypasses the proxy on purpose. A missing value (the 2026-10-08 fault) still fires."""
    replay = Replay(tmp_path)
    replay.sentinel()
    replay.tags()
    replay.proxy()
    for hhmmss, sid in TURNS:
        hl.record("auto-route", "UserPromptSubmit", 120.0, now=replay.t(_utc(f"2026-10-08T{hhmmss}")),
                  session_id=sid or None, base_url=base_url)
    s = plv.ledger_silence(now=replay.t(EVAL))
    assert s["state"] == state
    if state == "quiet":
        assert s["excluded"]["env_override"] == 3


# -- reading the hook ledger's tail -----------------------------------------------------------


def _append_hook_rows(path: Path, stamps: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for ts in stamps:
            fh.write(json.dumps({"hook": "enforce-route", "event": "PreToolUse", "elapsed_ms": 40.0,
                                 "timed_out": False, "ts": round(ts, 3), "session_id": ORGANIC_B,
                                 "host": "claude_code", "pad": "x" * 60}) + "\n")


def test_turns_behind_600kb_of_tool_rows_are_still_read(tmp_path):
    """``_tail_dicts`` grows its chunk until it passes the window start: one 64 KB read
    would see only the tool-hook rows after the last turn and report ``quiet``."""
    replay = Replay(tmp_path).outage()
    a, b = replay.t(_utc("2026-10-08T17:20:00")), replay.t(EVAL - 1)
    n = 6000
    _append_hook_rows(hl.store_path(), [a + (b - a) * i / n for i in range(n)])
    assert hl.store_path().stat().st_size > 600_000
    s = plv.ledger_silence(now=replay.t(EVAL))
    assert s["state"] == "SILENT" and s["organic_cc_turns"] == 3


def test_turns_in_the_just_rotated_file_are_still_read(tmp_path):
    """The hook ledger rotated inside the window: the turns are in ``.1``, the live file
    holds only later tool rows. ``_recent_hook_rows`` must read both."""
    replay = Replay(tmp_path).outage()
    live = hl.store_path()
    live.rename(live.with_name(live.name + ".1"))
    _append_hook_rows(live, [replay.t(_utc("2026-10-08T17:25:00"))])
    s = plv.ledger_silence(now=replay.t(EVAL))
    assert s["state"] == "SILENT" and s["organic_cc_turns"] == 3


# -- doctor keeps its override findings when the alert cannot be computed ----------------------


def test_doctor_keeps_override_findings_when_ledger_silence_raises(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://localhost:8787"}}), encoding="utf-8")
    proj = tmp_path / "proj" / ".claude"
    proj.mkdir(parents=True)
    (proj / "settings.local.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}}), encoding="utf-8")

    def boom(**_kw):
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(plv, "ledger_silence", boom)
    found = plv.doctor_findings(cwd=tmp_path / "proj", home=home)
    assert any("overrides ANTHROPIC_BASE_URL" in f and "api.anthropic.com" in f for f in found)


# -- one override rule: session-start (A.1, #342) and this alert agree --------------------------


@pytest.mark.parametrize("local, project", [
    ("https://api.anthropic.com", None),
    (None, "https://api.anthropic.com"),
    ("http://localhost:8787", "https://api.anthropic.com"),     # local wins, and it routes
    ("https://api.anthropic.com", "http://localhost:8787"),     # local wins, and it bypasses
    ("", "https://api.anthropic.com"),                          # empty does not win
    (None, "http://127.0.0.1:8797"),
    (None, None),
])
def test_project_override_matches_session_start_effective_base_url(tmp_path, monkeypatch, local, project):
    """R8: the override rule is shared with A.1. session-start's ``_effective_base_url`` is
    stdlib-only (it runs without ``llm_router``), so the two are pinned to agree instead."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))       # no user-level key
    cwd = tmp_path / "proj"
    (cwd / ".claude").mkdir(parents=True)
    for name, value in (("settings.local.json", local), ("settings.json", project)):
        if value is not None:
            (cwd / ".claude" / name).write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": value}}),
                                                encoding="utf-8")
    ports = [8787, 8797]
    mod = _load_session_start()
    value, where = mod._effective_base_url(str(cwd))
    start_says = where if (value and not mod._routes_to_local_port(value, ports)) else None
    assert plv.project_override(str(cwd), ports) == start_says


def test_statusline_does_not_show_the_alert():
    """R8 cuts the statusline surface (sync path): nothing there may call it."""
    root = Path(__file__).parent.parent
    files = [*root.glob("src/llm_router/**/statusline*.py"), *root.glob("src/llm_router/hooks/status*.py"),
             *root.glob("src/llm_router/**/statusline-command.sh")]
    assert files                                         # an empty set would pass anything
    for f in files:
        assert "ledger_silence" not in f.read_text(encoding="utf-8", errors="replace"), f
