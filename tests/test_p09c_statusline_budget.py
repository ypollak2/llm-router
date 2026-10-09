"""PLAN v16 P0.9-c: the full status line's hot path is a cache read.

Live statusline p95 was 2,435 ms (2026-10-09 gap matrix; own re-measurement: p50
~540 ms, 30 runs, 11 MB transcript) against the PRD bar of 100 ms. The script ran
sixteen sequential subprocesses per render: twelve ``python3 -c`` one-liners, a
transcript scan that grows with the session, ``import llm_router`` for the money
figure, ``sqlite3``, an interpreter probe loop. The segments are now computed by
ONE detached process (``statusline_segments``) into a per-session ``key=value``
file the script reads with ``read``.

Wall-clock assertions are flakes under load (PLAN v16 L22), so the budget is held
as a PHASE budget a loaded machine cannot fake: the processes the hot path starts.
The wall clock itself is ``scripts/statusline_wall.py``.
"""

from __future__ import annotations

import ast
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_router import statusline_segments as seg

REPO = Path(__file__).resolve().parent.parent
STATUSLINE = REPO / "src" / "llm_router" / "hooks" / "statusline-command.sh"
SEGMENTS = REPO / "src" / "llm_router" / "statusline_segments.py"
SID = "p09c-test-session"
#: Commands the script may name on its warm path. ``date`` is the clock on bash 3.2
#: (macOS /bin/bash); nothing else.
HOT_PATH_ALLOWED = {"date"}
SHIMMED = ("cat", "date", "perl", "python3", "python", "sqlite3", "sed", "awk", "xargs", "head",
           "basename", "dirname", "grep", "stat", "tr", "cut", "ls", "find", "sort", "mkdir", "touch", "sleep")


def _shims(tmp_path: Path) -> tuple[Path, Path]:
    """A PATH holding only logging wrappers: each appends its name to SPAWNLOG, then runs
    the real binary. A command the script starts by name shows up in the log."""
    d, log = tmp_path / "shims", tmp_path / "spawn.log"
    if not d.exists():
        d.mkdir()
        for name in SHIMMED:
            real = shutil.which(name, path="/usr/bin:/bin")
            if real:
                f = d / name
                f.write_text(f'#!/bin/bash\necho {name} >> "$SPAWNLOG"\nexec {real} "$@"\n')
                f.chmod(0o755)
    return d, log


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".llm-router").mkdir(parents=True)
    # Pin the interpreter the refresher uses, as a first render would remember it.
    (h / ".llm-router" / ".statusline_python").write_text(sys.executable + "\n")
    return h


def _stdin(tmp_path: Path, **extra) -> str:
    return json.dumps({"session_id": SID, "cwd": str(tmp_path / "proj"),
                       "model": {"id": "claude-opus-5"}, **extra})


def _run(home: Path, tmp_path: Path, stdin: str, *, shims: bool = True, **env_extra):
    bindir, log = _shims(tmp_path)
    env = {"HOME": str(home), "PATH": str(bindir) if shims else "/usr/bin:/bin", "SPAWNLOG": str(log),
           "LANG": "en_US.UTF-8", "NO_COLOR": "1", "LLM_ROUTER_ENFORCE": "smart",
           "OLLAMA_URL": "http://127.0.0.1:9", **env_extra}
    log.write_text("")
    r = subprocess.run(["/bin/bash", str(STATUSLINE)], input=stdin, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    spawned = sorted(set(log.read_text().split()))
    return r.stdout, spawned


def _cache(home: Path) -> Path:
    return home / ".llm-router" / f"statusline_seg_{SID}.kv"


def _read_kv(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def _write_cache(home: Path, age_s: float, **fields) -> None:
    kv = {"v": "1", "written": str(int(time.time() - age_s)), "health": "ok", **fields}
    _cache(home).write_text("".join(f"{k}={v}\n" for k, v in kv.items()))


def _wait(predicate, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.1)
    return False


# ── the phase budget: what the hot path starts ───────────────────────────────


def test_a_fresh_cache_render_starts_no_process_but_the_clock(home, tmp_path):
    out0, _ = _run(home, tmp_path, _stdin(tmp_path))  # first render: fills the cache
    assert _cache(home).exists()
    out, spawned = _run(home, tmp_path, _stdin(tmp_path))
    assert out.strip(), "the warm render printed nothing"
    extra = set(spawned) - HOT_PATH_ALLOWED
    assert not extra, (
        f"the warm path started {sorted(extra)}: each python3 -c is ~20 ms, an import of "
        f"llm_router ~170 ms, and the bar for the whole line is 100 ms"
    )
    assert out == out0, "a cached render differs from the synchronous one"


def test_the_first_render_computes_synchronously_and_writes_the_cache(home, tmp_path):
    assert not _cache(home).exists()
    out, _ = _run(home, tmp_path, _stdin(tmp_path), shims=False)
    assert "proj" in out and "smart" in out
    kv = _read_kv(_cache(home))
    assert kv["v"] == "1" and kv["written"].isdigit() and kv["health"] in {"ok", "degraded", "idle", "down"}


def test_context_is_read_from_the_transcript_on_the_first_render(home, tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"message": {"usage": {"input_tokens": 1000, "cache_read_input_tokens": 49000}}}) + "\n")
    out, _ = _run(home, tmp_path, _stdin(tmp_path, transcript_path=str(t)), shims=False)
    assert "🧠 50.0k" in out and "25%" in out, out


# ── a stale cache is served, marked when old, and refreshed off the hot path ──


def test_a_stale_cache_is_served_at_once_and_refreshed_in_the_background(home, tmp_path):
    _write_cache(home, age_s=45, last="code>query")
    before = _read_kv(_cache(home))["written"]
    out, spawned = _run(home, tmp_path, _stdin(tmp_path))
    assert "code>query" in out, "the stale cache was not rendered"
    assert "cached" not in out, "a 45 s old cache is within the 90 s staleness marker"
    assert not ({"python3", "python", "sqlite3"} & set(spawned)), "the render waited on a python"
    assert _wait(lambda: _read_kv(_cache(home)).get("written", before) != before or
                 _read_kv(_cache(home)).get("last") != "code>query"), "no background refresh landed"


def test_a_cache_the_refresher_stopped_updating_says_so(home, tmp_path):
    marker = home / ".llm-router" / f".statusline_seg_spawn_{SID}"
    marker.write_text(f"{int(time.time())}\n")  # a launch was just made: no new refresh will mask the age
    _write_cache(home, age_s=200, last="code>query")
    out, _ = _run(home, tmp_path, _stdin(tmp_path))
    assert "code>query" in out and "cached 3m ago" in out, out


def test_a_young_cache_has_no_staleness_marker(home, tmp_path):
    _write_cache(home, age_s=10, last="code>query")
    out, _ = _run(home, tmp_path, _stdin(tmp_path))
    assert "cached" not in out


def test_a_newer_transcript_asks_for_a_refresh_even_inside_the_ttl(home, tmp_path):
    t = tmp_path / "t.jsonl"
    _write_cache(home, age_s=10, ctx_human="1.0k", ctx_pct="0")
    old = time.time() - 10  # bash 3.2's -nt compares whole seconds: age the file as the field says
    os.utime(_cache(home), (old, old))
    t.write_text(json.dumps({"message": {"usage": {"input_tokens": 100000}}}) + "\n")  # newer than the cache
    out, _ = _run(home, tmp_path, _stdin(tmp_path, transcript_path=str(t)))
    assert "🧠 1.0k" in out, "the render should show the cache, not wait for the transcript"
    assert _wait(lambda: _read_kv(_cache(home)).get("ctx_human") == "100.0k"), \
        "a transcript newer than the cache did not trigger a refresh"


# ── injection / parsing ──────────────────────────────────────────────────────


def test_escaped_json_falls_back_to_one_python_parse(home, tmp_path):
    weird = tmp_path / 'we"ird'
    weird.mkdir()
    out, _ = _run(home, tmp_path, json.dumps({"session_id": SID, "cwd": str(weird)}), shims=False)
    assert 'we"ird' in out


def test_cache_values_are_one_clean_line():
    assert seg._clean("a\nb\x1b[31mc") == "a b [31mc"
    cache = seg.compute("/nonexistent-state-dir", "", "", {}, now=1.0)
    assert all("\n" not in v and "\x1b" not in v for v in cache.values())


# ── the segments module ──────────────────────────────────────────────────────


def test_segments_module_is_stdlib_only_at_import():
    """It runs under whatever python3 the shell finds, often one without llm_router."""
    tree = ast.parse(SEGMENTS.read_text())
    stdlib = set(sys.stdlib_module_names)
    for node in tree.body:  # module level only: function bodies import llm_router lazily
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module] if isinstance(node, ast.ImportFrom) else [])
        for n in names:
            assert n.split(".")[0] in stdlib, f"{n} is imported at module level"


def _full_scan(path: Path) -> int:
    """The one-liner this replaced: last line with a usage block whose tokens are > 0."""
    total = 0
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            msg = d.get("message")
            u = msg.get("usage") if isinstance(msg, dict) else None
            if not isinstance(u, dict):
                continue
            n = (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                 + u.get("cache_read_input_tokens", 0))
            if n > 0:
                total = n
    return total


@pytest.mark.parametrize("seed", range(12))
def test_tail_read_equals_the_full_scan(tmp_path, seed):
    rng = random.Random(seed)
    lines = []
    for i in range(rng.randint(0, 400)):
        kind = rng.random()
        if kind < 0.3:
            lines.append(json.dumps({"message": {"usage": {"input_tokens": rng.randint(0, 9000),
                                                           "cache_read_input_tokens": rng.randint(0, 9000)}}}))
        elif kind < 0.4:
            lines.append("not json {")
        else:
            lines.append(json.dumps({"type": "tool", "blob": "x" * rng.randint(0, 6000)}))
    # sometimes the only qualifying line is far from the end (window must grow)
    if seed % 3 == 0:
        lines = [json.dumps({"message": {"usage": {"input_tokens": 777}}})] + \
                [json.dumps({"blob": "y" * 5000}) for _ in range(150)]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(lines) + ("\n" if seed % 2 else ""))
    assert seg._last_usage_tokens(str(p)) == _full_scan(p)


def test_a_part_that_fails_is_absent_not_zero(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(seg, "mix_segment", boom)
    monkeypatch.setattr(seg, "money_segment", boom)
    out = seg.compute(str(tmp_path), "", "", {}, now=time.time())
    assert "mix_local" not in out and "money" not in out and out["v"] == "1"


@pytest.mark.timing
def test_refresher_exits_at_once_when_another_holds_the_lock(home, tmp_path):
    import fcntl

    lock = open(home / ".llm-router" / f".statusline-seg-{SID}.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        t0 = time.monotonic()
        rc = seg.main(["--state", str(home / ".llm-router"), "--session", SID])
        assert rc == 0 and time.monotonic() - t0 < 5
        assert not _cache(home).exists(), "a second refresher computed while the first held the lock"
    finally:
        lock.close()


def test_install_ships_the_segments_module_beside_the_hooks():
    from llm_router import install_hooks as ih

    assert ("statusline_segments.py", "llm_router_statusline_segments.py") in ih._HOOK_SUPPORT_FILES


def test_the_statusline_budget_is_still_100ms():
    from llm_router import hook_latency as hl

    assert hl.HOOK_BUDGETS_MS["statusline"] == 100


def test_harness_percentile_matches_hook_wall():
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import statusline_wall as sw
    finally:
        sys.path.pop(0)
    assert sw.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.95) == 5.0
    rows = [{"mode": "cold", "wall_ms": float(i), "in_script_ms": None, "rc": 0,
             "load1_before": 1.0, "load1_after": 1.0} for i in range(1, 11)]
    rows.append({"mode": "cold", "wall_ms": 999.0, "in_script_ms": None, "rc": 0,
                 "load1_before": 9.0, "load1_after": 9.0})
    s = sw.summarise(rows, "cold", 4.0)
    assert s["n"] == 10 and s["excluded_load"] == 1 and s["p95_ms"] < 100 and s["above_load_p95_ms"] == 999.0
    assert os.path.exists(sw.__file__)


# ── repair round 1: the cache file is data, not code ─────────────────────────


@pytest.mark.parametrize("key", ["ctx_pct", "session_pct", "weekly_pct", "mix_local", "mix_paid"])
def test_a_malicious_cache_value_is_never_evaluated(home, tmp_path, key):
    pwned = tmp_path / "PWNED"
    _write_cache(home, age_s=5, usage="ok", session_pct="5", weekly_pct="5", ctx_human="1k",
                 ctx_pct="5", mix_local="1", mix_paid="1")
    kv = _read_kv(_cache(home))
    kv[key] = f"a[$(touch {pwned})]"
    _cache(home).write_text("".join(f"{k}={v}\n" for k, v in kv.items()))
    out, _ = _run(home, tmp_path, _stdin(tmp_path))
    assert not pwned.exists(), f"{key} was evaluated by the shell"
    assert "smart" in out, "the render did not survive a bad cache value"


# ── repair round 1: the render path never probes for an interpreter ──────────


@pytest.mark.timing
def test_a_stale_cache_render_does_not_wait_for_an_interpreter_probe(home, tmp_path):
    (home / ".llm-router" / ".statusline_python").unlink()  # nothing remembered: a probe is needed
    bindir, _log = _shims(tmp_path)
    slow = bindir / "python3"  # every probe candidate that goes through PATH takes 3 s
    slow.write_text('#!/bin/bash\nsleep 3\nexit 1\n')
    slow.chmod(0o755)
    _write_cache(home, age_s=45, last="code>query")
    t0 = time.monotonic()
    out, _ = _run(home, tmp_path, _stdin(tmp_path))
    assert "code>query" in out
    assert time.monotonic() - t0 < 2.5, "the render waited on the interpreter search"


def test_with_no_usable_python_warm_renders_spawn_nothing_but_the_clock(home, tmp_path):
    (home / ".llm-router" / ".statusline_python").unlink()
    _write_cache(home, age_s=5, last="code>query")
    out, spawned = _run(home, tmp_path, _stdin(tmp_path))
    assert "code>query" in out and not (set(spawned) - HOT_PATH_ALLOWED), spawned


# ── repair round 1: the synchronous path is rate-limited and visible ─────────


@pytest.mark.timing
def test_a_broken_refresher_costs_one_sync_call_per_five_seconds_and_says_so(home, tmp_path):
    calls = tmp_path / "calls.log"
    fake = tmp_path / "fakepy"
    fake.write_text(f'#!/bin/bash\necho x >> {calls}\nexit 1\n')
    fake.chmod(0o755)
    (home / ".llm-router" / ".statusline_python").write_text(f"{fake}\n")
    outs = [_run(home, tmp_path, _stdin(tmp_path))[0] for _ in range(3)]
    assert calls.read_text().count("x") == 1, "every render paid the failing synchronous call"
    assert all("segments pending" in o and "smart" in o for o in outs), outs


def test_the_loser_of_a_first_render_race_renders_a_marker_not_a_bare_line(home, tmp_path):
    import fcntl

    lock = open(home / ".llm-router" / f".statusline-seg-{SID}.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        out, _ = _run(home, tmp_path, _stdin(tmp_path), shims=False)
    finally:
        lock.close()
    assert "segments pending" in out and "proj" in out


def test_a_plain_interpreter_answer_is_reprobed_after_its_ttl(home, tmp_path):
    fake = tmp_path / "probe-me"
    fake.write_text("#!/bin/bash\nexit 1\n")
    fake.chmod(0o755)
    stale = int(time.time()) - 3600
    (home / ".llm-router" / ".statusline_python").write_text(f"{fake}\n{stale}\nplain\n")
    _run(home, tmp_path, _stdin(tmp_path), shims=False)  # first render: sync, re-probes
    lines = (home / ".llm-router" / ".statusline_python").read_text().split("\n")
    assert lines[0] != str(fake) and lines[2] in {"full", "plain"}, lines


def test_prune_removes_old_locks_and_markers_too(tmp_path):
    names = ["statusline_seg_x.kv", ".statusline-seg-x.lock", ".statusline_seg_spawn_x",
             ".statusline_seg_sync_x"]
    old = time.time() - 3 * 86400
    for n in names:
        (tmp_path / n).write_text("")
        os.utime(tmp_path / n, (old, old))
    (tmp_path / ".statusline_seg_sync_fresh").write_text("")
    seg._prune(str(tmp_path))
    assert [n for n in names if (tmp_path / n).exists()] == []
    assert (tmp_path / ".statusline_seg_sync_fresh").exists()


# ── repair round 2: digits are not enough (length, octal) ────────────────────


def _render_with(home, tmp_path, **overrides):
    _write_cache(home, age_s=5, usage="ok", session_pct="5", weekly_pct="5", ctx_human="1.0k",
                 ctx_pct="5", mix_local="1", mix_paid="1")
    kv = _read_kv(_cache(home))
    kv.update(overrides)
    _cache(home).write_text("".join(f"{k}={v}\n" for k, v in kv.items()))
    bindir, log = _shims(tmp_path)
    env = {"HOME": str(home), "PATH": str(bindir), "SPAWNLOG": str(log), "LANG": "en_US.UTF-8",
           "NO_COLOR": "1", "LLM_ROUTER_ENFORCE": "smart"}
    return subprocess.run(["/bin/bash", str(STATUSLINE)], input=_stdin(tmp_path), env=env,
                          capture_output=True, text=True, timeout=4)  # a hang is a TimeoutExpired


@pytest.mark.timing
@pytest.mark.parametrize("digits", ["9" * 23, "9" * 19, "9" * 18])
@pytest.mark.parametrize("key", ["ctx_pct", "session_pct", "mix_local", "written"])
def test_an_overlong_number_neither_hangs_nor_prints_shell_errors(home, tmp_path, key, digits):
    r = _render_with(home, tmp_path, **{key: digits})
    assert r.returncode == 0 and "smart" in r.stdout
    assert r.stderr == "", r.stderr


@pytest.mark.parametrize("key", ["ctx_pct", "session_pct", "weekly_pct", "written"])
def test_a_leading_zero_is_decimal_not_octal(home, tmp_path, key):
    val = "08" if key != "written" else "0" + str(int(time.time()) - 5)
    r = _render_with(home, tmp_path, **{key: val})
    assert r.stderr == "", r.stderr
    if key == "ctx_pct":
        assert "🧠 1.0k" in r.stdout and "8%" in r.stdout
    if key == "session_pct":
        assert "8%/5h" in r.stdout
    if key == "written":
        assert "cached" not in r.stdout  # read as an age of ~5 s, not as an error


def test_a_percentage_above_100_is_clamped(home, tmp_path):
    r = _render_with(home, tmp_path, ctx_pct="999")
    assert "100%" in r.stdout and r.stderr == ""
