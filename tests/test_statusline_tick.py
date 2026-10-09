"""The status line tick (debug mode, LLM_ROUTER_STATUSLINE=fast): one short honest
line, fast, never blocked by its refresh. The default is the full layout
(tests/test_statusline_default_full.py).

Pre-registered bars (PR "ux: status line and receipt band"):
  * output <= 200 visible chars in every state (26 state combinations below);
  * Claude quota (5h / weekly / Sonnet) from usage.json: unknown is n/a, never
    0%; past LLM_ROUTER_USAGE_TTL_SEC it is shown with a "(stale <age>)" marker;
  * an unknown value prints "n/a", never 0 / 0.0%; no prompt text is printed;
  * a stuck refresh never delays a tick (a hung fake KPI source);
  * the tick computes no KPI: it is stdlib-only and imports nothing from llm_router.
The wall-time bar (p95 <= 100 ms over n=200, cold and warm cache) is measured by
``scripts/bench_statusline.py``; the guard here is looser so a loaded CI runner
cannot flake it, and still fails if the tick starts doing real work.
"""

from __future__ import annotations

import ast
import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_router import statusline_tick as tick

REPO = Path(__file__).resolve().parents[1]
TICK = REPO / "src" / "llm_router" / "statusline_tick.py"
SCRIPT = REPO / "src" / "llm_router" / "hooks" / "statusline-command.sh"
NOW = 1_791_234_000.0
PROMPT = "PLEASE-NEVER-PRINT-THIS-PROMPT"
_ANSI = tick._ANSI


def _visible(line: str) -> str:
    return _ANSI.sub("", line)


# ── 26 state combinations ────────────────────────────────────────────────────

_FULL = {
    "v": 1, "written_at": NOW - 5, "mode": "smart",
    "ns": {"pct": 0.0, "n": 3599},
    "claude_weekly_pct": 41.0,
    "codex": {"used": 7, "budget": 15, "resets_at": NOW + 3600},
    "hooks_slow": {"hook": "auto-route", "p95_ms": 3002.3},
}
#: usage.json as the hooks write it (the source `llm-router status` reads).
_USAGE = {"session_pct": 12.4, "weekly_pct": 41.0, "sonnet_pct": 3.0, "updated_at": NOW - 30}


def _states() -> list[tuple[str, dict | None, str | None, dict | None]]:
    long = "x" * 500
    U = _USAGE
    states: list[tuple[str, dict | None, str | None, dict | None]] = [
        ("no cache", None, None, None),
        ("empty cache", {}, None, U),
        ("cache not a dict", ["nope"], None, U),  # type: ignore[list-item]
        ("stale cache", {**_FULL, "written_at": NOW - 3600}, None, U),
        ("cache from the future", {**_FULL, "written_at": NOW + 3600}, None, U),
        ("all fresh", dict(_FULL), None, U),
        ("mode off", {**_FULL, "mode": "off"}, None, U),
        ("mode shadow via env", dict(_FULL), "shadow", U),
        ("env mode wins", dict(_FULL), "hard", U),
        ("quota unknown", dict(_FULL), None, None),
        ("quota bool", dict(_FULL), None, {**U, "session_pct": True, "weekly_pct": True, "sonnet_pct": True}),
        ("quota fallback", dict(_FULL), None, {**U, "session_pct": 50, "weekly_pct": 50, "sonnet_pct": 50, "is_fallback": True}),
        ("quota pending", dict(_FULL), None, {"pending": True}),
        ("quota one field missing", dict(_FULL), None, {**U, "sonnet_pct": None}),
        ("quota stale", dict(_FULL), None, {**U, "updated_at": NOW - 7200}),
        ("quota no updated_at", dict(_FULL), None, {k: v for k, v in U.items() if k != "updated_at"}),
        ("ns unknown", {**_FULL, "ns": None}, None, U),
        ("ns too few", {**_FULL, "ns": {"pct": None, "n": 12}}, None, U),
        ("ns zero n", {**_FULL, "ns": {"pct": 0.0, "n": 0}}, None, U),
        ("codex unknown", {**_FULL, "codex": None}, None, U),
        ("codex no reset", {**_FULL, "codex": {"used": 0, "budget": 15, "resets_at": None}}, None, U),
        ("hooks fine", {**_FULL, "hooks_slow": None}, None, U),
        ("long names", {**_FULL, "mode": long, "hooks_slow": {"hook": long, "p95_ms": 9e9}}, long,
         {**U, "session_pct": 1e9, "weekly_pct": 1e9, "sonnet_pct": 1e9, "updated_at": 1.0}),
        ("escape in names", {**_FULL, "mode": "\x1b[31mevil", "hooks_slow": {"hook": "\x1b]0;x\x07", "p95_ms": 1}}, None, U),
        ("everything missing but mode", {"written_at": NOW, "mode": "smart"}, None, None),
        ("usage not a dict", dict(_FULL), None, ["nope"]),  # type: ignore[list-item]
    ]
    assert len(states) == 26
    return states


@pytest.mark.parametrize("name,cache,env_mode,usage", _states(), ids=[s[0] for s in _states()])
@pytest.mark.parametrize("color", [False, True])
def test_every_state_is_one_short_honest_line(name, cache, env_mode, usage, color):
    line = tick.render(cache, now=NOW, env_mode=env_mode, color=color, usage=usage)
    vis = _visible(line)
    assert "\n" not in line
    assert len(vis) <= tick.MAX_CHARS, (name, len(vis))
    assert vis.startswith("llm-router · ")
    assert "\x1b" not in vis and "\x07" not in vis, "no stray escape from data"
    assert PROMPT not in line


@pytest.mark.parametrize("name,cache,env_mode,usage", _states(), ids=[s[0] for s in _states()])
def test_unknown_is_na_never_zero(name, cache, env_mode, usage):
    vis = _visible(tick.render(cache, now=NOW, env_mode=env_mode, usage=usage))
    fresh = isinstance(cache, dict) and 0 <= NOW - cache.get("written_at", -1e18) <= tick.STALE_AFTER_S
    ns = cache.get("ns") if fresh else None
    if not (isinstance(ns, dict) and isinstance(ns.get("pct"), float) and ns.get("n")):
        assert "NS 0" not in vis and "NS n/a" in vis, vis
    measured = isinstance(usage, dict) and not usage.get("pending") and not usage.get("is_fallback")
    for key, label in (("session_pct", "5h"), ("weekly_pct", "wk"), ("sonnet_pct", "sonnet")):
        v = usage.get(key) if measured else None
        if not isinstance(v, float):
            assert f"{label} 0%" not in vis and (f"{label} n/a" in vis or "Claude n/a" in vis), vis
    if not (fresh and isinstance(cache.get("codex"), dict)):
        assert "Codex n/a" in vis, vis


def test_stale_cache_shows_no_cached_number():
    vis = tick.render({**_FULL, "written_at": NOW - tick.STALE_AFTER_S - 1}, now=NOW)
    assert vis == "llm-router · n/a · NS n/a · Claude n/a · Codex n/a"


def test_fresh_cache_line():
    vis = _visible(tick.render(dict(_FULL), now=NOW, color=True, usage=dict(_USAGE)))
    assert vis.startswith(
        "llm-router · smart · NS 0.0% n=3599 · Claude 5h 12% wk 41% sonnet 3% · Codex 7/15 ↻")
    assert vis.endswith("⚠ hooks p95 3.0s auto-route")


# ── quota: the numbers `llm-router status` prints, from the same usage.json ──

def test_quota_matches_the_status_panel_numbers():
    """Same keys and the same rounding (``{pct:.0f}``) as the status panel."""
    from llm_router.ui.status_premium import PremiumStatusCommand

    assert [k for k, _ in tick._QUOTA_FIELDS] == ["session_pct", "weekly_pct", "sonnet_pct"]
    assert hasattr(PremiumStatusCommand, "render_subscription_quotas")
    seg = tick._quota({"session_pct": 12.5, "weekly_pct": 40.6, "sonnet_pct": 0.0,
                       "updated_at": NOW}, NOW, 300.0)
    assert seg == f"Claude 5h {12.5:.0f}% wk {40.6:.0f}% sonnet {0.0:.0f}%"


@pytest.mark.parametrize("usage", [
    None, {}, {"pending": True},
    {"session_pct": 50, "weekly_pct": 50, "sonnet_pct": 50, "is_fallback": True, "updated_at": NOW},
    {"session_pct": None, "weekly_pct": "41", "sonnet_pct": True, "updated_at": NOW},
], ids=["no file", "empty", "pending", "fallback", "not numbers"])
def test_unknown_quota_is_na_never_zero_percent(usage):
    seg = tick._quota(usage, NOW, 300.0)
    assert seg == "Claude n/a", seg
    assert "0%" not in seg and "50%" not in seg


def test_measured_zero_quota_is_shown():
    assert tick._quota({"session_pct": 0, "weekly_pct": 0.0, "sonnet_pct": 0, "updated_at": NOW},
                       NOW, 300.0) == "Claude 5h 0% wk 0% sonnet 0%"


def test_partly_known_quota_marks_only_the_unknown_field():
    seg = tick._quota({**_USAGE, "sonnet_pct": None}, NOW, 300.0)
    assert seg == "Claude 5h 12% wk 41% sonnet n/a"


@pytest.mark.parametrize("age,marker", [(299, ""), (301, " (stale 5m)"), (7200, " (stale 2h)"),
                                        (3 * 86400, " (stale 3d)")])
def test_stale_quota_is_shown_with_a_marker(age, marker):
    seg = tick._quota({**_USAGE, "updated_at": NOW - age}, NOW, 300.0)
    assert seg == "Claude 5h 12% wk 41% sonnet 3%" + marker


def test_quota_with_no_timestamp_is_marked_stale():
    usage = {k: v for k, v in _USAGE.items() if k != "updated_at"}
    assert tick._quota(usage, NOW, 300.0).endswith(" (stale)")


def test_quota_ttl_follows_the_env(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_USAGE_TTL_SEC", "60")
    assert tick.usage_ttl() == 60.0
    for bad in ("", "abc", "-5", "0"):
        monkeypatch.setenv("LLM_ROUTER_USAGE_TTL_SEC", bad)
        assert tick.usage_ttl() == tick.DEFAULT_USAGE_TTL_S


def test_measured_zero_is_shown_with_its_n():
    """0.0% WITH n is a measurement (3599 units, none used): it is shown."""
    assert "NS 0.0% n=3599" in tick.render(dict(_FULL), now=NOW)


# ── the tick never computes the KPI ──────────────────────────────────────────

def test_tick_is_stdlib_only():
    """Mutation guard: a tick that imports llm_router (e.g. to compute the KPI
    inline) fails here. The package import alone is over the tick's budget."""
    tree = ast.parse(TICK.read_text())
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for n in names:
            assert n.split(".")[0] in stdlib, f"tick imports {n}: it must stay stdlib-only"
    assert "compute_scorecard" not in TICK.read_text()


# ── subprocess: the real command ─────────────────────────────────────────────

def _env(home: Path, refresh_cmd: str, *, fast: bool = True) -> dict:
    env = {
        "HOME": str(home), "LLM_ROUTER_HOME": str(home / ".llm-router"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "NO_COLOR": "1",
        "LLM_ROUTER_STATUSLINE_REFRESH_CMD": refresh_cmd,
    }
    if fast:
        env["LLM_ROUTER_STATUSLINE"] = "fast"
    return env


def _run(env: dict) -> tuple[str, float]:
    t0 = time.perf_counter()
    r = subprocess.run(["bash", str(SCRIPT)], input=json.dumps({"prompt": PROMPT, "session_id": "s"}),
                       env=env, capture_output=True, text=True, timeout=30)
    return r.stdout, time.perf_counter() - t0


def test_hung_refresh_never_delays_a_tick_and_is_started_once(tmp_path):
    """A refresher that hangs for 20 s (a stuck KPI source) is started once and
    never waited on: every tick still returns at once."""
    marker = tmp_path / "started"
    hang = f"{sys.executable} -c \"open({str(marker)!r},'a').write('x');import time;time.sleep(20)\""
    env = _env(tmp_path, hang)
    times = []
    for _ in range(5):
        out, dt = _run(env)
        times.append(dt)
        assert out.startswith("llm-router · "), out
        assert PROMPT not in out
    # Wait for the refresher's write, not for the marker to exist: open() creates
    # it empty before write() lands, and a slow runner read '' in that window
    # (docs/bugs/SLT-1.md).
    deadline = time.time() + 15
    while not (marker.exists() and marker.read_text()) and time.time() < deadline:
        time.sleep(0.05)
    assert marker.read_text() == "x", "exactly one refresh started across 5 ticks"
    assert max(times) < 2.0, times  # 20 s if any tick waited on the hung refresher


def test_wall_time_guard(tmp_path):
    """Loose CI guard (the bar itself, p95 <= 100 ms at n=200, is measured by
    scripts/bench_statusline.py): a tick doing real work takes seconds."""
    env = _env(tmp_path, "true")
    cache = tmp_path / ".llm-router" / "statusline_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({**_FULL, "written_at": time.time()}))
    times = sorted(_run(env)[1] for _ in range(15))
    assert times[len(times) // 2] < 0.5, times


def test_command_output_with_cache(tmp_path):
    env = _env(tmp_path, "true")
    cache = tmp_path / ".llm-router" / "statusline_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({**_FULL, "written_at": time.time()}))
    (cache.parent / "usage.json").write_text(json.dumps({**_USAGE, "updated_at": time.time()}))
    out, _ = _run(env)
    assert out.startswith(
        "llm-router · smart · NS 0.0% n=3599 · Claude 5h 12% wk 41% sonnet 3% · Codex 7/15"), out


def test_command_shows_stale_quota_with_marker(tmp_path):
    env = _env(tmp_path, "true")
    state = tmp_path / ".llm-router"
    state.mkdir(parents=True)
    (state / "usage.json").write_text(json.dumps({**_USAGE, "updated_at": time.time() - 7200}))
    out, _ = _run(env)
    assert "Claude 5h 12% wk 41% sonnet 3% (stale 2h)" in out, out


def test_default_is_the_full_layout(tmp_path):
    """Owner decision: the full status line is the default for everyone."""
    out, _ = _run(_env(tmp_path, "true", fast=False))
    assert out.strip() and not out.startswith("llm-router · "), out


@pytest.mark.parametrize("value", ["full", "compact", "FAST", "1", ""])
def test_only_fast_selects_the_fast_line(tmp_path, value):
    out, _ = _run({**_env(tmp_path, "true", fast=False), "LLM_ROUTER_STATUSLINE": value})
    assert not out.startswith("llm-router · "), (value, out)


# ── the local switch: the router's own .env, no settings.json edit ───────────

@pytest.mark.parametrize("line", [
    "LLM_ROUTER_STATUSLINE=fast", 'LLM_ROUTER_STATUSLINE="fast"', "export LLM_ROUTER_STATUSLINE=fast",
    "LLM_ROUTER_STATUSLINE='fast'  # debug", "LLM_ROUTER_STATUSLINE=fast\r",
])
def test_router_env_file_turns_on_the_fast_line(tmp_path, line):
    state = tmp_path / ".llm-router"
    state.mkdir(parents=True)
    (state / ".env").write_text(f"OPENAI_API_KEY=sk-x\n{line}\nLLM_ROUTER_ENFORCE=smart\n")
    out, _ = _run(_env(tmp_path, "true", fast=False))
    assert out.startswith("llm-router · "), (line, out)
    assert "sk-x" not in out


def test_router_env_file_without_a_final_newline(tmp_path):
    state = tmp_path / ".llm-router"
    state.mkdir(parents=True)
    (state / ".env").write_text("LLM_ROUTER_STATUSLINE=fast")
    out, _ = _run(_env(tmp_path, "true", fast=False))
    assert out.startswith("llm-router · "), out


def test_real_environment_wins_over_the_env_file(tmp_path):
    state = tmp_path / ".llm-router"
    state.mkdir(parents=True)
    (state / ".env").write_text("LLM_ROUTER_STATUSLINE=fast\n")
    out, _ = _run({**_env(tmp_path, "true", fast=False), "LLM_ROUTER_STATUSLINE": "full"})
    assert not out.startswith("llm-router · "), out


def test_commented_out_switch_is_ignored(tmp_path):
    state = tmp_path / ".llm-router"
    state.mkdir(parents=True)
    (state / ".env").write_text("# LLM_ROUTER_STATUSLINE=fast\n")
    out, _ = _run(_env(tmp_path, "true", fast=False))
    assert not out.startswith("llm-router · "), out


def test_combinations_cover_the_named_dimensions():
    """The 26 states span missing data, stale, mode off, quota unknown/stale and long names."""
    names = {s[0] for s in _states()}
    for needed in ("no cache", "stale cache", "mode off", "quota unknown", "quota stale",
                   "quota fallback", "long names"):
        assert needed in names
    assert len(list(itertools.product(_states(), [False, True]))) == 52


def test_installed_layout_finds_the_tick_next_to_the_script(tmp_path):
    """install() copies the script as llm_router-statusline.sh and the tick as
    llm_router_statusline_tick.py into one folder: that layout must work, and the
    installer must be told to copy the tick."""
    import shutil

    from llm_router.install_hooks import _HOOK_SUPPORT_FILES

    assert ("statusline_tick.py", "llm_router_statusline_tick.py") in _HOOK_SUPPORT_FILES
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy2(SCRIPT, hooks / "llm_router-statusline.sh")
    shutil.copy2(TICK, hooks / "llm_router_statusline_tick.py")
    env = _env(tmp_path, "true")
    r = subprocess.run(["bash", str(hooks / "llm_router-statusline.sh")], input="{}", env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.stdout.startswith("llm-router · "), r.stdout


def test_script_without_the_tick_falls_back_to_the_classic_layout(tmp_path):
    """A user in debug mode whose tick has not been copied yet keeps a working
    status line (the classic one), never an empty one."""
    import shutil

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy2(SCRIPT, hooks / "llm_router-statusline.sh")
    r = subprocess.run(["bash", str(hooks / "llm_router-statusline.sh")], input="{}",
                       env=_env(tmp_path, "true"), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() != ""
    assert not r.stdout.startswith("llm-router · ")


# ── LLM_ROUTER_STATUSLINE=both: full line, then the fast line ────────────────

def _both(tmp_path, **extra):
    return _run({**_env(tmp_path, "true", fast=False), "LLM_ROUTER_STATUSLINE": "both", **extra})


def test_both_prints_full_line_then_fast_line(tmp_path):
    out, _ = _both(tmp_path)
    lines = out.splitlines()
    assert len(lines) == 2, out
    assert not lines[0].startswith("llm-router · "), lines[0]
    assert lines[1].startswith("llm-router · "), lines[1]
    full, _ = _run(_env(tmp_path, "true", fast=False))
    assert lines[0] == full.splitlines()[0]


def test_both_from_env_file(tmp_path):
    state = tmp_path / ".llm-router"
    state.mkdir(exist_ok=True)
    (state / ".env").write_text("LLM_ROUTER_STATUSLINE=both\n")
    out, _ = _run(_env(tmp_path, "true", fast=False))
    assert len(out.splitlines()) == 2 and out.splitlines()[1].startswith("llm-router · "), out


@pytest.mark.parametrize("value,fast_only", [("fast", True), ("full", False), ("bogus", False), ("", False)])
def test_other_values_stay_single_line(tmp_path, value, fast_only):
    out, _ = _run({**_env(tmp_path, "true", fast=False), "LLM_ROUTER_STATUSLINE": value})
    assert len(out.splitlines()) == 1, out
    assert out.startswith("llm-router · ") is fast_only


def test_both_fast_part_never_blocks_on_a_hung_refresher(tmp_path):
    hang = f"{sys.executable} -c 'import time; time.sleep(30)'"
    try:
        out, dt = _run({**_env(tmp_path, hang, fast=False), "LLM_ROUTER_STATUSLINE": "both"})
    finally:
        subprocess.run(["pkill", "-f", "time.sleep(30)"], check=False)
    assert len(out.splitlines()) == 2 and dt < 10, (dt, out)


def test_env_file_value_is_never_executed(tmp_path):
    """The .env is read with read/case, never sourced: $(...) and `...` stay text."""
    state = tmp_path / ".llm-router"
    state.mkdir(exist_ok=True)
    marker = tmp_path / "PWNED"
    for payload in (f"$(touch {marker})", f"`touch {marker}`", f"fast; touch {marker}"):
        (state / ".env").write_text(f"LLM_ROUTER_STATUSLINE={payload}\nX=$(touch {marker})\n")
        _run(_env(tmp_path, "true", fast=False))
        assert not marker.exists(), payload


def test_quota_without_updated_at_falls_back_to_file_mtime(tmp_path):
    usage = {k: v for k, v in _USAGE.items() if k != "updated_at"}
    f = tmp_path / tick.USAGE_NAME
    f.write_text(json.dumps(usage))
    fresh = tick.read_usage(str(tmp_path))
    assert tick._quota(fresh, fresh["updated_at"] + 10, 300.0) == "Claude 5h 12% wk 41% sonnet 3%"
    os.utime(f, (NOW - 7200, NOW - 7200))
    assert tick._quota(tick.read_usage(str(tmp_path)), NOW, 300.0).endswith(" (stale 2h)")
