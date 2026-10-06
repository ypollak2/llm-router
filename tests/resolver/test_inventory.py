"""Inventory against fake Ollama / CLI / environment outputs (never the real ones)."""

from __future__ import annotations

import json

from llm_router.resolver import inventory as inv_mod
from llm_router.resolver import profile as profile_mod
from llm_router.resolver.profile import ModelProfile, ProbeResult
from llm_router.resolver.types import (
    CAP_NO,
    CAP_UNKNOWN,
    CAP_YES,
    inventory_from_dict,
    inventory_to_dict,
)

from .fakes import NOW, make_probes

SECRET = "sk-THIS-MUST-NEVER-APPEAR-1234567890"


def collect(**kw):
    return inv_mod.collect_inventory(make_probes(**kw), profiles={})


# --------------------------------------------------------------------- Ollama

def test_ollama_models_sizes_context_loaded_and_declared_capabilities():
    inv = collect(ollama={
        "qwen3:30b": {"caps": ["completion", "tools"], "ctx": 262144, "loaded": True, "size": 18_000_000_000, "run_ctx": 8192},
        "llava:7b": {"caps": ["completion", "vision"], "ctx": 4096},
        "old:1b": {"ctx": 2048},                       # /api/show without capabilities
        "nomic-embed-text": {"caps": ["embedding"]},   # not a chat model
    })
    ids = [m.id for m in inv.models]
    assert ids == ["ollama/llava:7b", "ollama/old:1b", "ollama/qwen3:30b"]   # embedding model skipped
    q = inv.get("ollama/qwen3:30b")
    assert (q.route_kind, q.privacy, q.path_verified, q.authorized) == ("local", "local", True, True)
    assert q.loaded is True and q.size_bytes == 18_000_000_000
    assert q.context_window == 262144 and q.context_source == "ollama /api/show model_info"
    assert q.cap("tools").state == CAP_YES and q.cap("vision").state == CAP_NO
    assert any("8192-token window" in n for n in q.notes)        # running window is surfaced
    assert inv.get("ollama/llava:7b").cap("vision").state == CAP_YES
    assert inv.get("ollama/llava:7b").loaded is False
    old = inv.get("ollama/old:1b")
    assert old.cap("tools").state == CAP_UNKNOWN                 # unreported is unknown, not "no"
    assert inv.sources["ollama"].ok and "3 model(s)" in inv.sources["ollama"].detail


def test_unreachable_ollama_is_reported_not_raised():
    inv = collect(ollama=None)
    assert inv.models == []
    assert not inv.sources["ollama"].ok and "not reachable" in inv.sources["ollama"].detail


def test_remote_ollama_host_is_not_treated_as_local_for_privacy():
    p = make_probes(ollama={"m:1b": {"caps": ["completion"]}})
    p.ollama_base = lambda: "http://user:pw@gpu-box.lan:11434"
    inv = inv_mod.collect_inventory(p, profiles={})
    m = inv.models[0]
    assert m.privacy == "cloud"
    blob = json.dumps(inventory_to_dict(inv))
    assert "user:pw" not in blob and "@" not in blob        # userinfo is never stored or printed
    assert m.exec_path == "http://gpu-box.lan:11434"


# ------------------------------------------------------------------ Claude Code

def test_claude_subscription_with_fresh_usage_reading():
    inv = collect(claude="/bin/claude", usage=("ok", 0.92))
    opus = inv.get("claude_subscription/opus")
    assert opus.route_kind == "subscription" and opus.privacy == "cloud"
    assert opus.path_verified and opus.authorized
    assert opus.quota.state == "ok" and opus.quota.pressure == 0.92
    assert opus.tier_ceiling == "FRONTIER" and opus.tier_basis == "prior"
    assert inv.get("claude_subscription/haiku").tier_ceiling == "EASY"
    assert "credentials are never read" in opus.auth_detail


def test_stale_or_missing_usage_reading_is_unknown_pressure_not_zero():
    for usage in (("stale", 0.95), ("unknown", None), None):
        inv = collect(claude="/bin/claude", usage=usage)
        q = inv.get("claude_subscription/sonnet").quota
        assert q.state == "unknown" and q.pressure is None, usage


def test_claude_cli_absent_yields_no_models():
    inv = collect(claude=None)
    assert not [m for m in inv.models if m.provider == "anthropic"]
    assert not inv.sources["claude"].ok


# ---------------------------------------------- Claude Code: login (CLAUDE-AUTH-01)
#
# #259 hard-coded authorized=True/path_verified=True whenever the `claude`
# binary was merely found on PATH, with "login state is not inspected" as the
# comment -- unlike Codex (`codex login status`) and Gemini CLI (oauth file
# presence), which both really check. Reproduced: a fake Probes where
# find_claude() returns a path but usage_reading() reports state="unknown"
# (never logged in) still resolved to claude_subscription/opus with no hard
# failures. These tests pin the real check: `_claude_authorization` must never
# default to authorized.

def test_claude_logged_in_via_auth_status_is_eligible():
    inv = collect(claude="/bin/claude", claude_login="logged_in", usage=("unknown", None))
    opus = inv.get("claude_subscription/opus")
    assert opus.authorized and opus.path_verified
    assert "loggedIn=true" in opus.auth_detail
    assert inv.sources["claude"].ok


def test_claude_logged_out_via_auth_status_is_excluded_with_reason():
    # usage.json reads "ok" (the weakest fallback signal would say yes), but an
    # explicit logged-out verdict from `claude auth status` must still win.
    inv = collect(claude="/bin/claude", claude_login="logged_out", usage=("ok", 0.1))
    opus = inv.get("claude_subscription/opus")
    assert not opus.authorized and not opus.path_verified
    assert "loggedIn=false" in opus.auth_detail
    assert not inv.sources["claude"].ok


def test_claude_oauth_presence_is_eligible_when_auth_status_is_unknown():
    inv = collect(claude="/bin/claude", claude_login="unknown", claude_oauth=True,
                   usage=("unknown", None))
    opus = inv.get("claude_subscription/opus")
    assert opus.authorized and opus.path_verified
    assert "oauthAccount" in opus.auth_detail


def test_claude_fresh_usage_reading_is_eligible_when_the_other_two_signals_are_unknown():
    inv = collect(claude="/bin/claude", claude_login="unknown", claude_oauth=False,
                   usage=("ok", 0.92))
    opus = inv.get("claude_subscription/opus")
    assert opus.authorized and opus.path_verified
    assert "credentials are never read" in opus.auth_detail


def test_claude_no_signal_confirms_login_is_excluded_with_an_explicit_reason():
    """The reviewer's exact reproduction: a claude binary with no other
    provider, usage.json unknown, no oauth file, no auth-status confirmation."""
    inv = collect(claude="/bin/claude", claude_login="unknown", claude_oauth=False,
                   usage=("unknown", None))
    opus = inv.get("claude_subscription/opus")
    assert not opus.authorized and not opus.path_verified
    assert opus.auth_detail == "claude CLI found but login not confirmed"
    assert not inv.sources["claude"].ok


def test_claude_auth_status_binary_missing_is_unknown_not_a_login(monkeypatch):
    """The subcommand hanging, erroring or simply not existing on an older CLI
    must never read as logged in."""
    def boom(*args, **kwargs):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(inv_mod.subprocess, "run", boom)
    assert inv_mod._claude_auth_status("/bin/claude") == "unknown"


def test_claude_auth_status_timeout_is_unknown_not_a_login(monkeypatch):
    import subprocess as _subprocess

    def boom(*args, **kwargs):
        raise _subprocess.TimeoutExpired(cmd=["claude", "auth", "status"], timeout=5.0)

    monkeypatch.setattr(inv_mod.subprocess, "run", boom)
    assert inv_mod._claude_auth_status("/bin/claude") == "unknown"


def test_claude_auth_status_nonzero_exit_is_unknown_not_a_login(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(inv_mod.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert inv_mod._claude_auth_status("/bin/claude") == "unknown"


def test_claude_auth_status_unparsable_output_is_unknown_not_a_login(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(inv_mod.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="not json"))
    assert inv_mod._claude_auth_status("/bin/claude") == "unknown"


def test_claude_auth_status_reads_only_the_loggedin_field_never_pii(monkeypatch):
    """The command's JSON also carries email/orgId/orgName; none of that may
    reach the return value."""
    from types import SimpleNamespace

    payload = json.dumps({"loggedIn": True, "email": "SECRET@example.com",
                          "orgId": "org_SECRET", "orgName": "Secret Org"})
    monkeypatch.setattr(inv_mod.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=payload))
    result = inv_mod._claude_auth_status("/bin/claude")
    assert result == "logged_in"
    assert "SECRET" not in result and "@" not in result


def test_claude_auth_status_logged_in_false_is_logged_out(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(inv_mod.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps({"loggedIn": False})))
    assert inv_mod._claude_auth_status("/bin/claude") == "logged_out"


def test_claude_oauth_present_checks_key_presence_only(monkeypatch, tmp_path):
    from llm_router import install_hooks

    cfg = tmp_path / ".claude.json"
    monkeypatch.setattr(install_hooks, "_CLAUDE_JSON_PATH", cfg)

    assert inv_mod._claude_oauth_present() is False      # no file at all

    cfg.write_text(json.dumps({"someOtherKey": 1}), encoding="utf-8")
    assert inv_mod._claude_oauth_present() is False      # file exists, key absent

    cfg.write_text(json.dumps({"oauthAccount": {"email": "SECRET@example.com"}}),
                   encoding="utf-8")
    assert inv_mod._claude_oauth_present() is True       # presence only; value never read


# ------------------------------------------------------------------------ Codex

def test_codex_chatgpt_login_is_subscription_and_verified():
    inv = collect(codex=("/bin/codex", "Logged in using ChatGPT"), codex_pressure=0.4)
    m = inv.get("codex/gpt-5.5")
    assert (m.route_kind, m.authorized, m.path_verified) == ("subscription", True, True)
    assert m.quota.state == "ok" and m.quota.pressure == 0.4
    assert "estimate" in m.quota.detail


def test_codex_api_key_login_is_metered_api_route():
    inv = collect(codex=("/bin/codex", "Logged in using an API key - sk-***abcd"))
    m = inv.get("codex/gpt-5.5")
    assert m.route_kind == "api" and m.authorized
    assert not m.path_verified                                  # same rule as API keys
    assert "calibrate --allow-paid" in m.path_detail
    assert "sk-" not in json.dumps(inventory_to_dict(inv))      # raw login output is never kept


def test_codex_not_logged_in_is_not_authorized_nor_verified():
    inv = collect(codex=("/bin/codex", "Not logged in"))
    m = inv.get("codex/gpt-5.5")
    assert not m.authorized and not m.path_verified


def test_codex_bench_from_provider_reset_marks_quota_benched():
    inv = collect(codex=("/bin/codex", "Logged in using ChatGPT"),
                  resets={"codex": NOW + 3600})
    q = inv.get("codex/gpt-5.5").quota
    assert q.state == "benched" and q.benched_until == NOW + 3600


def test_codex_without_a_request_counter_has_unknown_pressure():
    inv = collect(codex=("/bin/codex", "Logged in using ChatGPT"), codex_pressure=None)
    assert inv.get("codex/gpt-5.5").quota.pressure is None


# ----------------------------------------------------------------------- Gemini

GEMINI_ID = "gemini_cli/gemini-2.5-flash"
GEMINI_KEY = "AIzaSy-gemini-secret-0123456789"


def test_gemini_cli_login_file_only_is_authorized_but_unverified():
    m = collect(gemini="/bin/gemini", gemini_login=True).get(GEMINI_ID)
    assert m.authorized and not m.path_verified and "not read" in m.auth_detail
    assert "not verified" in m.path_detail
    assert not collect(gemini="/bin/gemini").get(GEMINI_ID).authorized


def test_gemini_cli_env_key_only_is_authorized_but_unverified_and_never_pinged():
    m = collect(gemini="/bin/gemini", env={"GEMINI_API_KEY": GEMINI_KEY}).get(GEMINI_ID)
    assert m.authorized and not m.path_verified
    assert GEMINI_KEY not in repr(m)


def _gemini_verified(tmp_path, status, *, login=False):
    calls = []
    p = make_probes(gemini="/bin/gemini", gemini_login=login, env={"GEMINI_API_KEY": GEMINI_KEY},
                    ping=lambda provider, key: calls.append(provider) or status(),
                    ping_cache=tmp_path / "c.json")
    inv = inv_mod.collect_inventory(p, profiles={}, verify=True)
    return inv.get(GEMINI_ID), calls


def test_gemini_cli_verified_key_marks_the_path_verified(tmp_path):
    m, calls = _gemini_verified(tmp_path, lambda: 200)
    assert m.authorized and m.path_verified and "gemini" in calls
    assert GEMINI_KEY not in repr(m)


def test_gemini_cli_rejected_key_without_login_is_not_authorized(tmp_path):
    m, _ = _gemini_verified(tmp_path, lambda: 401)
    assert not m.authorized and not m.path_verified


def test_gemini_cli_rejected_key_does_not_deauthorize_a_login_file(tmp_path):
    m, _ = _gemini_verified(tmp_path, lambda: 401, login=True)
    assert m.authorized and not m.path_verified


def test_gemini_cli_timeout_or_unknown_stays_unverified_and_never_crashes(tmp_path):
    # the real ping maps a timeout/transport error to None (test_auth_ping_network)
    for status in (lambda: None, lambda: 500):
        m, _ = _gemini_verified(tmp_path, status)
        assert m is not None and m.authorized and not m.path_verified


def test_gemini_cli_login_file_without_verify_never_calls_the_ping(tmp_path):
    # make_probes' default ping raises if called
    m = collect(gemini="/bin/gemini", gemini_login=True,
                env={"GEMINI_API_KEY": GEMINI_KEY}).get(GEMINI_ID)
    assert m.authorized and not m.path_verified


# -------------------------------------------------------------------- API keys

def test_api_keys_are_listed_by_name_and_values_never_appear():
    inv = collect(env={"OPENAI_API_KEY": SECRET, "ANTHROPIC_API_KEY": "", "UNRELATED": SECRET})
    assert inv.api_key_names == ["OPENAI_API_KEY"]
    blob = json.dumps(inventory_to_dict(inv)) + inv_mod.render_table(inv)
    assert SECRET not in blob
    m = inv.get("openai/gpt-5.5")
    assert m.route_kind == "api" and m.authorized
    assert not m.path_verified                       # a key is not a verified path
    assert "inventory --verify" in m.path_detail
    assert m.quota.state == "metered" and m.price_in_per_mtok == 5.0
    assert not [x for x in inv.models if x.provider == "anthropic"]   # no key, no models


def test_api_path_becomes_verified_only_through_a_calibration_roundtrip():
    p = make_probes(env={"OPENAI_API_KEY": SECRET})
    prof = profile_mod.build_profile("openai/gpt-5.5", {"json": ProbeResult(True)}, reachable=True)
    inv = inv_mod.collect_inventory(p, profiles={"openai/gpt-5.5": prof})
    assert inv.get("openai/gpt-5.5").path_verified
    assert not inv.get("openai/gpt-5.4-mini").path_verified


# ------------------------------------------------------------ snapshots / stale

def test_a_model_removed_since_the_last_snapshot_is_marked_not_dropped():
    old = collect(ollama={"a:1b": {"caps": ["completion"]}, "b:1b": {"caps": ["completion"]}})
    new = inv_mod.collect_inventory(make_probes(ollama={"a:1b": {"caps": ["completion"]}}),
                                    profiles={}, previous=old)
    gone = new.get("ollama/b:1b")
    assert gone is not None and gone.present is False
    assert new.removed == ["ollama/b:1b"]
    assert new.get("ollama/a:1b").present


def test_snapshot_round_trips_through_json(tmp_path):
    inv = collect(ollama={"a:1b": {"caps": ["completion", "tools"]}}, claude="/bin/claude", usage=("ok", 0.5))
    path = inv_mod.save_snapshot(inv, tmp_path / "inv.json")
    back = inv_mod.load_snapshot(path)
    assert back == inv
    assert inventory_from_dict(inventory_to_dict(inv)) == inv


def test_unreadable_snapshot_is_none(tmp_path):
    (tmp_path / "x.json").write_text("{not json")
    assert inv_mod.load_snapshot(tmp_path / "x.json") is None
    assert inv_mod.load_snapshot(tmp_path / "missing.json") is None


def test_one_broken_probe_does_not_blank_the_inventory():
    p = make_probes(ollama={"a:1b": {"caps": ["completion"]}}, claude="/bin/claude")
    p.find_codex = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    inv = inv_mod.collect_inventory(p, profiles={})
    assert inv.get("ollama/a:1b") is not None
    assert not inv.sources["codex"].ok and "probe failed" in inv.sources["codex"].detail


# -------------------------------------------------------------- profile overlay

def test_measured_profile_overrides_declared_capability_and_sets_tier():
    inv_before = collect(ollama={"a:1b": {"caps": ["completion", "tools"]}})
    assert inv_before.get("ollama/a:1b").tier_basis == "unmeasured"
    probes = {"json": ProbeResult(True), "edit": ProbeResult(True),
              "tool_call": ProbeResult(False, "no lookup tool call"),
              "long_context": ProbeResult(True, size_tokens=8000)}
    prof = profile_mod.build_profile("ollama/a:1b", probes, reachable=True)
    inv = inv_mod.collect_inventory(make_probes(ollama={"a:1b": {"caps": ["completion", "tools"]}}),
                                    profiles={"ollama/a:1b": prof})
    m = inv.get("ollama/a:1b")
    assert m.cap("tools").state == CAP_NO and m.cap("tools").source == "measured"   # declared yes, measured no
    assert (m.tier_ceiling, m.tier_basis) == ("EASY", "measured")
    assert m.measured_recall_tokens == 8000


def test_cloud_prior_frontier_needs_a_measured_medium_to_stay_frontier():
    prior = "FRONTIER"
    assert profile_mod.merge_tier(prior, None)[:2] == ("FRONTIER", "prior")
    failing = ModelProfile("m", NOW, tier_ceiling=None, tier_detail="failed")
    assert profile_mod.merge_tier(prior, failing)[:2] == (None, "measured")
    easy = ModelProfile("m", NOW, tier_ceiling="EASY")
    assert profile_mod.merge_tier(prior, easy)[:2] == ("EASY", "measured")
    medium = ModelProfile("m", NOW, tier_ceiling="MEDIUM")
    assert profile_mod.merge_tier(prior, medium)[:2] == ("FRONTIER", "prior")
    assert profile_mod.merge_tier(None, None)[:2] == (None, "unmeasured")


def test_table_marks_measured_capabilities_and_lists_sources():
    inv = collect(ollama={"a:1b": {"caps": ["completion", "tools"]}})
    out = inv_mod.render_table(inv)
    assert "ollama/a:1b" in out and "+tools" in out and "ok ollama" in out


# ------------------------------------------------------------------------- CLI

def _cli_probes(monkeypatch, tmp_path, **kw):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    probes = make_probes(**kw)
    monkeypatch.setattr(inv_mod, "Probes", lambda: probes)


def test_cli_json_output_parses_and_carries_no_secret(monkeypatch, tmp_path, capsys):
    from llm_router.commands.inventory import cmd_inventory

    _cli_probes(monkeypatch, tmp_path, env={"OPENAI_API_KEY": SECRET},
                ollama={"a:1b": {"caps": ["completion"]}})
    assert cmd_inventory(["--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert SECRET not in out
    assert data["api_key_names"] == ["OPENAI_API_KEY"]
    assert {m["id"] for m in data["models"]} >= {"ollama/a:1b", "openai/gpt-5.5"}
    assert not (tmp_path / "inventory.json").exists()          # read-only unless --save


def test_cli_save_then_removed_model_is_flagged_next_run(monkeypatch, tmp_path, capsys):
    from llm_router.commands.inventory import cmd_inventory

    _cli_probes(monkeypatch, tmp_path, ollama={"a:1b": {"caps": ["completion"]}, "b:1b": {"caps": ["completion"]}})
    assert cmd_inventory(["--save"]) == 0
    capsys.readouterr()
    _cli_probes(monkeypatch, tmp_path, ollama={"a:1b": {"caps": ["completion"]}})
    assert cmd_inventory([]) == 0
    out = capsys.readouterr().out
    assert "ollama/b:1b (removed)" in out and "Removed since the last inventory: ollama/b:1b" in out
    # A plain run does not rewrite the snapshot, so the removal stays visible;
    # --save then forgets it instead of resurrecting it.
    assert cmd_inventory(["--save"]) == 0
    capsys.readouterr()
    assert inv_mod.load_snapshot().get("ollama/b:1b") is None


def test_cli_unknown_argument_is_an_error(capsys):
    from llm_router.commands.inventory import cmd_inventory

    assert cmd_inventory(["--bogus"]) == 2
