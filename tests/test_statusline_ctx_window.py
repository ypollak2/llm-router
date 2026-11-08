"""The context bar is used / the compaction point, not used / a guessed 200k."""
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

from llm_router import statusline_segments as seg


def _transcript(tmp_path, tokens):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 0, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": tokens, "output_tokens": 0}}}) + "\n")
    return str(p)


def _home(tmp_path, settings=None, raw=None):
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    if raw is not None:
        (h / ".claude" / "settings.json").write_text(raw)
    elif settings is not None:
        (h / ".claude" / "settings.json").write_text(json.dumps(settings))
    return {"HOME": str(h)}


def test_payload_window_is_used(tmp_path):
    env = _home(tmp_path)
    out = seg.context_segment(_transcript(tmp_path, 250_000), "opus", env, window=1_000_000)
    assert out["ctx_pct"] == "25"


def test_payload_window_capped_by_autocompact(tmp_path):
    env = _home(tmp_path, {"autoCompactWindow": 300000})
    out = seg.context_segment(_transcript(tmp_path, 250_000), "opus", env, window=1_000_000)
    assert out["ctx_pct"] == "83"


def test_no_payload_autocompact_300k_shows_83_not_100(tmp_path):
    env = _home(tmp_path, {"model": "opus", "autoCompactWindow": 300000})
    out = seg.context_segment(_transcript(tmp_path, 250_000), "opus", env)
    assert out["ctx_pct"] == "83"
    assert out["ctx_human"] == "250.0k"


def test_1m_model_without_payload(tmp_path):
    env = _home(tmp_path)
    out = seg.context_segment(_transcript(tmp_path, 250_000), "claude-opus-4-7[1m]", env)
    assert out["ctx_pct"] == "25"


@pytest.mark.parametrize("raw", ["{not json", "[]", '{"autoCompactWindow": "x"}',
                                 '{"autoCompactWindow": true}', '{"autoCompactWindow": -5}'])
def test_malformed_settings_fall_back_to_200k(tmp_path, raw):
    env = _home(tmp_path, raw=raw)
    out = seg.context_segment(_transcript(tmp_path, 150_000), "opus", env)
    assert out["ctx_pct"] == "75"


def test_no_settings_file_and_env_limit(tmp_path):
    env = _home(tmp_path)
    assert seg.effective_context_limit("opus", env) == 200000
    assert seg.effective_context_limit("opus", {**env, "CC_CONTEXT_LIMIT": "400000"}) == 400000
    assert seg.effective_context_limit("opus", {**env, "CC_CONTEXT_LIMIT": "junk"}) == 200000


def test_autocompact_never_raises_a_smaller_window(tmp_path):
    env = _home(tmp_path, {"autoCompactWindow": 300000})
    assert seg.effective_context_limit("opus", env, window=200000) == 200000


# ── rv43 fold ────────────────────────────────────────────────────────────────

def test_env_beats_model_settings_beats_top_level(tmp_path):
    env = _home(tmp_path, {"autoCompactWindow": 300000, "modelSettings": {
        "claude-opus-5-5": {"autoCompactWindow": 400000}}})
    assert seg._auto_compact_window(env, "claude-opus-5-5[1m]") == 400000
    assert seg._auto_compact_window(env, "claude-opus-5-5-20261001") == 400000
    assert seg._auto_compact_window(env, "claude-sonnet-5-5") == 300000
    assert seg._auto_compact_window(env, "opus") == 300000  # alias is not resolved
    assert seg._auto_compact_window({**env, "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500000"}, "claude-opus-5-5") == 500000
    assert seg._auto_compact_window({**env, "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "x"}, "claude-opus-5-5") == 400000


def test_payload_200k_with_more_used_does_not_pin(tmp_path):
    env = _home(tmp_path, {"autoCompactWindow": 300000})
    out = seg.context_segment(_transcript(tmp_path, 250_000), "opus", env, window=200000)
    assert out["ctx_pct"] == "83"  # larger of window and compact, not min -> 100


def test_used_above_every_known_value_still_caps_at_100(tmp_path):
    env = _home(tmp_path, {"autoCompactWindow": 300000})
    out = seg.context_segment(_transcript(tmp_path, 350_000), "opus", env, window=200000)
    assert out["ctx_pct"] == "100"


def test_cli_ctx_window_arg(tmp_path):
    state = tmp_path / "state"
    tr = _transcript(tmp_path, 250_000)
    h = _home(tmp_path)["HOME"]
    old = dict(os.environ)
    os.environ["HOME"] = h
    try:
        rc = seg.main(["--state", str(state), "--session", "s", "--transcript", tr,
                       "--model", "opus", "--ctx-window", "1000000"])
    finally:
        os.environ.clear()
        os.environ.update(old)
    assert rc == 0
    assert "ctx_pct=25" in (state / "statusline_seg_s.kv").read_text()


_SH = pathlib.Path(seg.__file__).parent / "hooks" / "statusline-command.sh"


def _shell_argv(tmp_path, stdin_text):
    """Run the real statusline script beside a stub segments script that records its argv."""
    d = tmp_path / "hooks"
    d.mkdir()
    (d / "statusline-command.sh").write_text(_SH.read_text())
    out = tmp_path / "argv.txt"
    (d / "llm_router_statusline_segments.py").write_text(
        "import sys\nopen(%r, 'w').write('\\n'.join(sys.argv[1:]))\n" % str(out))
    home = tmp_path / "shome"
    (home / ".llm-router").mkdir(parents=True)
    (home / ".llm-router" / ".statusline_python").write_text(sys.executable + "\n")
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    subprocess.run(["bash", str(d / "statusline-command.sh")], input=stdin_text, text=True,
                   env=env, capture_output=True, timeout=30)
    for _ in range(100):
        if out.exists() and out.read_text():
            break
        time.sleep(0.1)
    assert out.exists(), "stub segments script never ran"
    a = out.read_text().split("\n")
    return a[a.index("--ctx-window") + 1]


@pytest.mark.parametrize("payload,want", [
    ({"session_id": "s1", "model": {"id": "claude-opus-5-5"},
      "context_window": {"context_window_size": 1000000}}, "1000000"),
    ({"session_id": "s1", "model": {"id": "claude-opus-5-5"}}, "0"),
    ({"session_id": "s1", "context_window": None}, "0"),
    ({"session_id": "s1", "cwd": "a\\b", "context_window": {"context_window_size": 200000}}, "200000"),
    ({"session_id": "s1", "context_window": {"context_window_size": "junk"}}, "0"),
])
def test_shell_passes_ctx_window(tmp_path, payload, want):
    assert _shell_argv(tmp_path, json.dumps(payload)) == want
