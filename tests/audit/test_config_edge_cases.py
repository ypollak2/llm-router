"""SECTION 7 — Configuration edge cases for llm_router.repo_config.

Covers three areas from the task brief:
  1. No ~/.llm-router/routing.yaml at all -> effective_config() must return sane
     defaults and never crash.
  2. Malformed YAML -> `_parse_yaml` swallows ANY exception and returns `{}`
     silently. Flagged in REPORT_A.md: this can hide a real user typo (e.g. a
     tab character, a bad indent) with zero feedback — `llm_router config lint`
     is the only place that would ever surface it, and only if the user runs
     it, since the loader used by the actual routing path never raises.
  3. Task-type string case/whitespace handling — `model_override()` /
     `provider_override()` do a bare `dict.get()` with no normalisation, and
     `_dict_to_config()` silently DROPS routing keys that aren't already
     lowercase-exact members of `VALID_TASK_TYPES` (no warning either).
"""

from __future__ import annotations



from llm_router.repo_config import (
    RepoConfig,
    _dict_to_config,
    _parse_yaml,
    effective_config,
    find_repo_config_path,
    load_repo_config,
    load_user_config,
)


# ── 1. No config files at all ───────────────────────────────────────────────


def test_no_user_config_and_no_repo_config_returns_sane_defaults(tmp_path, monkeypatch):
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: empty_home)

    empty_repo_dir = tmp_path / "empty_repo"
    empty_repo_dir.mkdir()

    user_cfg = load_user_config()
    assert user_cfg == RepoConfig()

    repo_path = find_repo_config_path(empty_repo_dir)
    assert repo_path is None

    repo_cfg = load_repo_config(empty_repo_dir)
    assert repo_cfg == RepoConfig()

    merged = effective_config(start=empty_repo_dir)
    assert merged.profile is None
    assert merged.enforce is None
    assert merged.block_providers == []
    assert merged.block_models == []
    assert merged.allow_models == []
    assert merged.routing == {}
    assert merged.agentic_model is None
    assert merged.daily_caps == {}
    # Accessor methods must degrade gracefully, not raise.
    assert merged.effective_profile() is None
    # F01/North Star: the built-in default is now 'smart' (enforce routing out of the box).
    assert merged.effective_enforce() == "smart"
    assert merged.model_override("code") is None
    assert merged.provider_override("code") is None
    assert merged.daily_cap_for("code") is None
    assert merged.total_daily_cap() is None


# ── 2. Malformed YAML is silently swallowed ─────────────────────────────────


def test_parse_yaml_malformed_syntax_returns_empty_dict_silently(tmp_path):
    bad_yaml = tmp_path / "routing.yaml"
    # Unbalanced flow-mapping brace -> yaml.safe_load raises yaml.YAMLError.
    bad_yaml.write_text("profile: balanced\nrouting: {code: {model: 'oops'\n")

    result = _parse_yaml(bad_yaml)

    # ACTUAL behaviour: no exception propagates, caller gets an empty dict —
    # indistinguishable from "file exists but is empty". A user who fat-
    # fingers their YAML gets silently reverted to defaults with no error,
    # no log line, nothing. See REPORT_A.md bugs section.
    assert result == {}


def test_load_user_config_with_malformed_yaml_falls_back_to_defaults(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".llm-router").mkdir(parents=True)
    (home / ".llm-router" / "routing.yaml").write_text("profile: [unterminated\n")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    cfg = load_user_config()
    # Falls all the way back to an unset default config — no crash, no
    # partial parse, no signal that the file was invalid.
    assert cfg.profile is None
    assert cfg.routing == {}


def test_parse_yaml_non_dict_top_level_also_returns_empty_dict(tmp_path):
    """A syntactically valid YAML file whose top level isn't a mapping (e.g.
    a bare list or scalar) is treated the same as "no config" — silently."""
    list_yaml = tmp_path / "routing.yaml"
    list_yaml.write_text("- just\n- a\n- list\n")
    assert _parse_yaml(list_yaml) == {}

    scalar_yaml = tmp_path / "routing2.yaml"
    scalar_yaml.write_text("just a scalar string\n")
    assert _parse_yaml(scalar_yaml) == {}


# ── 3. Task-type string case / whitespace handling ──────────────────────────


def test_model_override_lookup_is_not_case_or_whitespace_normalized():
    cfg = _dict_to_config({"routing": {"analyze": {"model": "openai/gpt-4o-mini"}}}, "test")

    # Exact lowercase match works.
    assert cfg.model_override("analyze") == "openai/gpt-4o-mini"

    # ACTUAL behaviour: no normalisation anywhere in model_override /
    # provider_override — both are a bare `dict.get(task_type, ...)`.
    assert cfg.model_override("ANALYZE") is None
    assert cfg.model_override(" analyze ") is None
    assert cfg.model_override("Analyze") is None


def test_dict_to_config_silently_drops_non_lowercase_routing_keys():
    """_dict_to_config's own VALID_TASK_TYPES gate is lowercase-exact, so an
    uppercase or padded key in the YAML `routing:` block isn't just
    unnormalized at lookup time — it's dropped during PARSING, with no
    warning that the entry was ignored.
    """
    cfg = _dict_to_config(
        {
            "routing": {
                "ANALYZE": {"model": "openai/should-be-dropped"},
                " code": {"model": "openai/should-also-be-dropped"},
                "code": {"model": "openai/kept"},
            }
        },
        "test",
    )
    assert "ANALYZE" not in cfg.routing
    assert " code" not in cfg.routing
    assert cfg.routing.keys() == {"code"}
    assert cfg.model_override("code") == "openai/kept"


def test_profile_and_enforce_fields_ARE_lowercased_unlike_task_type_keys():
    """Contrast case: unlike routing-dict task-type keys, the scalar
    `profile` / `enforce` fields ARE explicitly lowercased by
    `_dict_to_config` (`str(data["profile"]).lower()`), so 'BALANCED' and
    'balanced' behave identically for those two fields only."""
    cfg = _dict_to_config({"profile": "BALANCED", "enforce": "HARD"}, "test")
    assert cfg.profile == "balanced"
    assert cfg.enforce == "hard"
