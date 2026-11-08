"""The context bar is used / the compaction point, not used / a guessed 200k."""
import json

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
