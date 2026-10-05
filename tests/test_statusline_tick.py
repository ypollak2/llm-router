"""The status line tick: one short honest line, fast, never blocked by its refresh.

Pre-registered bars (PR "ux: status line and receipt band"):
  * output <= 200 visible chars in every state (20 state combinations below);
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


# ── 20 state combinations ────────────────────────────────────────────────────

_FULL = {
    "v": 1, "written_at": NOW - 5, "mode": "smart",
    "ns": {"pct": 0.0, "n": 3599},
    "claude_weekly_pct": 41.0,
    "codex": {"used": 7, "budget": 15, "resets_at": NOW + 3600},
    "hooks_slow": {"hook": "auto-route", "p95_ms": 3002.3},
}


def _states() -> list[tuple[str, dict | None, str | None]]:
    long = "x" * 500
    states: list[tuple[str, dict | None, str | None]] = [
        ("no cache", None, None),
        ("empty cache", {}, None),
        ("cache not a dict", ["nope"], None),  # type: ignore[list-item]
        ("stale cache", {**_FULL, "written_at": NOW - 3600}, None),
        ("cache from the future", {**_FULL, "written_at": NOW + 3600}, None),
        ("all fresh", dict(_FULL), None),
        ("mode off", {**_FULL, "mode": "off"}, None),
        ("mode shadow via env", dict(_FULL), "shadow"),
        ("env mode wins", dict(_FULL), "hard"),
        ("quota unknown", {**_FULL, "claude_weekly_pct": None}, None),
        ("quota bool", {**_FULL, "claude_weekly_pct": True}, None),
        ("ns unknown", {**_FULL, "ns": None}, None),
        ("ns too few", {**_FULL, "ns": {"pct": None, "n": 12}}, None),
        ("ns zero n", {**_FULL, "ns": {"pct": 0.0, "n": 0}}, None),
        ("codex unknown", {**_FULL, "codex": None}, None),
        ("codex no reset", {**_FULL, "codex": {"used": 0, "budget": 15, "resets_at": None}}, None),
        ("hooks fine", {**_FULL, "hooks_slow": None}, None),
        ("long names", {**_FULL, "mode": long, "hooks_slow": {"hook": long, "p95_ms": 9e9}}, long),
        ("escape in names", {**_FULL, "mode": "\x1b[31mevil", "hooks_slow": {"hook": "\x1b]0;x\x07", "p95_ms": 1}}, None),
        ("everything missing but mode", {"written_at": NOW, "mode": "smart"}, None),
    ]
    assert len(states) == 20
    return states


@pytest.mark.parametrize("name,cache,env_mode", _states(), ids=[s[0] for s in _states()])
@pytest.mark.parametrize("color", [False, True])
def test_every_state_is_one_short_honest_line(name, cache, env_mode, color):
    line = tick.render(cache, now=NOW, env_mode=env_mode, color=color)
    vis = _visible(line)
    assert "\n" not in line
    assert len(vis) <= tick.MAX_CHARS, (name, len(vis))
    assert vis.startswith("llm-router · ")
    assert "\x1b" not in vis and "\x07" not in vis, "no stray escape from data"
    assert PROMPT not in line


@pytest.mark.parametrize("name,cache,env_mode", _states(), ids=[s[0] for s in _states()])
def test_unknown_is_na_never_zero(name, cache, env_mode):
    vis = _visible(tick.render(cache, now=NOW, env_mode=env_mode))
    fresh = isinstance(cache, dict) and 0 <= NOW - cache.get("written_at", -1e18) <= tick.STALE_AFTER_S
    ns = cache.get("ns") if fresh else None
    if not (isinstance(ns, dict) and isinstance(ns.get("pct"), float) and ns.get("n")):
        assert "NS 0" not in vis and "NS n/a" in vis, vis
    wk = cache.get("claude_weekly_pct") if fresh else None
    if not isinstance(wk, float):
        assert "Claude wk n/a" in vis, vis
    if not (fresh and isinstance(cache.get("codex"), dict)):
        assert "Codex n/a" in vis, vis


def test_stale_cache_shows_no_cached_number():
    vis = tick.render({**_FULL, "written_at": NOW - tick.STALE_AFTER_S - 1}, now=NOW)
    assert vis == "llm-router · n/a · NS n/a · Claude wk n/a · Codex n/a"


def test_fresh_cache_line():
    vis = _visible(tick.render(dict(_FULL), now=NOW, color=True))
    assert vis.startswith("llm-router · smart · NS 0.0% n=3599 · Claude wk 41% · Codex 7/15 ↻")
    assert vis.endswith("⚠ hooks p95 3.0s auto-route")


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

def _env(home: Path, refresh_cmd: str) -> dict:
    return {
        "HOME": str(home), "LLM_ROUTER_HOME": str(home / ".llm-router"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "NO_COLOR": "1",
        "LLM_ROUTER_STATUSLINE_REFRESH_CMD": refresh_cmd,
    }


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
    deadline = time.time() + 5
    while not marker.exists() and time.time() < deadline:
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
    out, _ = _run(env)
    assert out.startswith("llm-router · smart · NS 0.0% n=3599 · Claude wk 41% · Codex 7/15"), out


def test_classic_layout_still_reachable(tmp_path):
    env = {**_env(tmp_path, "true"), "LLM_ROUTER_STATUSLINE": "full"}
    out, _ = _run(env)
    assert not out.startswith("llm-router · "), "full = the classic layout"


def test_combinations_cover_the_named_dimensions():
    """The 20 states span missing data, stale, mode off, quota unknown and long names."""
    names = {s[0] for s in _states()}
    for needed in ("no cache", "stale cache", "mode off", "quota unknown", "long names"):
        assert needed in names
    assert len(list(itertools.product(_states(), [False, True]))) == 40


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
    """A user whose installed script is new but whose tick has not been copied yet
    keeps a working status line (the classic one), never an empty one."""
    import shutil

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy2(SCRIPT, hooks / "llm_router-statusline.sh")
    r = subprocess.run(["bash", str(hooks / "llm_router-statusline.sh")], input="{}",
                       env=_env(tmp_path, "true"), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() != ""
    assert not r.stdout.startswith("llm-router · ")
