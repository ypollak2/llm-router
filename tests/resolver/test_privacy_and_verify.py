"""Blocker + should-fix cases from the independent review of #259.

* Ollama models hosted in the cloud are never local, whatever the base URL says.
* The base URL is parsed, not split: its host decides privacy, nothing else.
* Nothing a model or a provider said is persisted in the capability profile.
* An inconclusive (transport-failure) calibration never overwrites a good one.
* `inventory --verify`: a zero-cost key check that sends each key only to its own
  provider, caches briefly, and never prints or stores a key.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from llm_router.resolver import auth_ping
from llm_router.resolver import calibrate as cal
from llm_router.resolver import inventory as inv_mod
from llm_router.resolver import profile as profile_mod
from llm_router.resolver.calibrate import ERR_NO_RESPONSE, Reply
from llm_router.resolver.types import inventory_to_dict

from .fakes import make_probes
from .test_calibrate import FakeModel

KEY = "sk-live-AAAABBBBCCCCDDDD1234"
OTHER_KEY = "sk-ant-ZZZZYYYYXXXXWWWW9876"
CAPS = ["completion", "tools"]


# ------------------------------------------------------- cloud-backed Ollama models

CLOUD_FIXTURES = {
    "remote_host in /api/tags": {"evil-cloud:120b": {"caps": CAPS, "tags_extra": {
        "remote_host": "https://ollama.com:443", "remote_model": "evil:120b"}}},
    "remote_model in /api/tags": {"evil-cloud:120b": {"caps": CAPS, "tags_extra": {"remote_model": "evil:120b"}}},
    "remote_host in /api/show": {"plain:7b": {"caps": CAPS, "show_extra": {"remote_host": "https://ollama.com:443"}}},
    "remote_model in /api/show": {"plain:7b": {"caps": CAPS, "show_extra": {"remote_model": "x"}}},
    "-cloud tag": {"evil:120b-cloud": {"caps": CAPS}},
    ":cloud tag": {"gpt-oss:cloud": {"caps": CAPS}},
}


@pytest.mark.parametrize("label", list(CLOUD_FIXTURES))
def test_a_cloud_model_listed_by_a_loopback_ollama_is_never_local(label):
    inv = inv_mod.collect_inventory(make_probes(ollama=CLOUD_FIXTURES[label]), profiles={})
    (m,) = inv.models
    assert m.privacy == "cloud", label
    assert m.route_kind == "subscription" and m.route_kind != "local", label
    assert any("cloud-hosted model" in n and "leave this machine" in n for n in m.notes)


def test_local_models_beside_a_cloud_one_stay_local_and_a_cloud_word_inside_a_name_is_not_a_marker():
    inv = inv_mod.collect_inventory(make_probes(ollama={
        "cloudy-llama:8b": {"caps": CAPS}, "qwen3:8b": {"caps": CAPS},
        "big:120b-cloud": {"caps": CAPS}}), profiles={})
    by = {m.id: m for m in inv.models}
    assert by["ollama/cloudy-llama:8b"].privacy == "local" and by["ollama/qwen3:8b"].privacy == "local"
    assert by["ollama/big:120b-cloud"].privacy == "cloud"


@pytest.mark.parametrize("label", list(CLOUD_FIXTURES))
def test_calibrate_never_probes_a_cloud_ollama_model_without_allow_paid(label):
    inv = inv_mod.collect_inventory(
        make_probes(ollama=CLOUD_FIXTURES[label], ollama_signin="signed_in"), profiles={})
    chosen, skipped = cal.select_models(inv, None, allow_paid=False)
    assert chosen == [] and "--allow-paid" in skipped[0][1]

    def completer_for(m):
        raise AssertionError(f"{m.id} was about to be probed without --allow-paid")

    rep = cal.run_calibration(inv, completer_for=completer_for, clock=lambda: 0.0)
    assert rep.profiles == {}


def test_with_allow_paid_a_cloud_ollama_model_is_estimated_as_spend_and_reached_through_ollama():
    inv = inv_mod.collect_inventory(make_probes(ollama=CLOUD_FIXTURES["-cloud tag"], ollama_signin="signed_in"), profiles={})
    chosen, _ = cal.select_models(inv, None, allow_paid=True)
    assert [m.id for m in chosen] == ["ollama/evil:120b-cloud"]
    assert "quota" in cal.estimate_cost(chosen[0], 8000).note                    # not "local, no cost"
    comp = cal.default_completer(chosen[0], lambda *a: None, "http://127.0.0.1:11434")
    assert isinstance(comp, cal.OllamaCompleter)                                 # never the gemini CLI path


# ------------------------------------------------------------------ URL parsing

@pytest.mark.parametrize("base,privacy,exec_path", [
    ("http://localhost:11434", "local", "http://localhost:11434"),
    ("http://127.0.0.1:11434", "local", "http://127.0.0.1:11434"),
    ("http://127.0.0.2:11434", "local", "http://127.0.0.2:11434"),
    ("http://[::1]:11434", "local", "http://[::1]:11434"),
    ("http://user:pw@localhost:11434", "local", "http://localhost:11434"),
    ("http://evil.com/@localhost", "cloud", "http://evil.com"),
    ("http://evil.com:80/x@127.0.0.1", "cloud", "http://evil.com:80"),
    ("http://localhost.evil.com:11434", "cloud", "http://localhost.evil.com:11434"),
    ("http://gpu-box.lan:11434", "cloud", "http://gpu-box.lan:11434"),
    ("http://10.0.0.5:11434", "cloud", "http://10.0.0.5:11434"),
    ("http://0.0.0.0:11434", "cloud", "http://0.0.0.0:11434"),
    ("https://user:pw@gpu.example.com", "cloud", "https://gpu.example.com"),
])
def test_privacy_comes_from_the_parsed_host_only(base, privacy, exec_path):
    inv = inv_mod.collect_inventory(
        make_probes(ollama={"m:7b": {"caps": CAPS}}, ollama_base=base), profiles={})
    (m,) = inv.models
    assert (m.privacy, m.exec_path) == (privacy, exec_path)
    blob = json.dumps(inventory_to_dict(inv)) + inv_mod.render_table(inv)
    assert "pw" not in blob.replace("path", "") and "user:" not in blob


def test_unreachable_message_names_only_the_host():
    p = make_probes(ollama=None, ollama_base="http://user:pw@evil.com/@localhost:11434")
    inv = inv_mod.collect_inventory(p, profiles={})
    assert inv.sources["ollama"].detail == "Ollama not reachable at evil.com"


# --------------------------------------------- nothing a model said is persisted

HOSTILE = "sk-HOSTILE-1234567890abcdef password=hunter2 https://tok:secret@evil.example"


class Hostile(FakeModel):
    """Echoes secrets in every reply, including as an error."""

    def chat(self, model, messages, *, tools=None, num_ctx=None, timeout=90):
        if tools and not any(m.get("role") == "tool" for m in messages):
            return Reply(text=HOSTILE, tool_calls=[{"name": "lookup", "arguments": {"key": HOSTILE}}])
        if any(m.get("images") for m in messages):
            return Reply(text=HOSTILE)
        return Reply(text=HOSTILE, error=None)


class Erroring(FakeModel):
    def chat(self, *a, **k):
        return Reply(error=HOSTILE)


class Raising(FakeModel):
    def chat(self, *a, **k):
        raise RuntimeError(HOSTILE)


@pytest.mark.parametrize("model_cls", [Hostile, Erroring, Raising])
def test_no_model_text_or_exception_text_reaches_the_saved_profile(model_cls, tmp_path):
    inv = inv_mod.collect_inventory(make_probes(
        ollama={"v:7b": {"caps": ["completion", "tools", "vision"], "ctx": 32768}}), profiles={})
    entry = inv.get("ollama/v:7b")
    prof = cal.calibrate_model(entry, model_cls(), tokens=8000, probe_timeout=30,
                               deadline=1e9, clock=lambda: 0.0)
    assert set(prof.probes) == {"json", "edit", "tool_call", "vision", "long_context"}   # every probe ran
    path = profile_mod.save_profiles({entry.id: prof}, tmp_path / "capability_profile.json")
    raw = path.read_text()
    for needle in ("HOSTILE", "hunter2", "secret", "evil.example", "sk-"):
        assert needle not in raw, (model_cls.__name__, needle)
    fixed = {"wrong value", "no JSON edit in the reply", "no lookup tool call", "wrong arguments",
             "misread the code", "provider error", "no response"}
    for name, p in prof.probes.items():
        assert p.detail in fixed or p.detail.startswith("missed the needle at ~"), (name, p.detail)


def test_cloud_completer_drops_the_exception_message(monkeypatch):
    from llm_router import providers

    class Boom:
        def completion(self, **kw):
            raise RuntimeError(f"401 for key {HOSTILE}")

    monkeypatch.setattr(providers, "litellm", Boom())
    inv = inv_mod.collect_inventory(make_probes(env={"OPENAI_API_KEY": KEY}), profiles={})
    c = cal.CloudCompleter(inv.get("openai/gpt-5.5"))
    r = c.chat("openai/gpt-5.5", [{"role": "user", "content": "x"}])
    assert r.error == cal.ERR_PROVIDER and HOSTILE not in repr(r)


def test_probe_details_are_fixed_strings_even_when_the_model_answers_wrongly():
    r = cal.probe_json(FakeModel({"edit"}), "m", 5)
    assert (r.ok, r.detail) == (False, "wrong value")
    assert cal.probe_edit(Hostile(), "m", 5).detail == "no JSON edit in the reply"


# ---------------------------------------- inconclusive runs never erase a measurement

def _good_profile(model_id):
    return profile_mod.build_profile(model_id, {
        "json": profile_mod.ProbeResult(True), "edit": profile_mod.ProbeResult(True),
        "tool_call": profile_mod.ProbeResult(True),
        "long_context": profile_mod.ProbeResult(True, size_tokens=8000)}, reachable=True, now=1.0)


class Down(FakeModel):
    def chat(self, *a, **k):
        return Reply(error=ERR_NO_RESPONSE)


def test_all_transport_errors_keep_the_previous_measurement(tmp_path):
    path = tmp_path / "capability_profile.json"
    profile_mod.save_profiles({"ollama/m:7b": _good_profile("ollama/m:7b")}, path)
    inv = inv_mod.collect_inventory(make_probes(
        ollama={"m:7b": {"caps": CAPS, "ctx": 32768}}), profiles={})
    rep = cal.run_calibration(inv, completer_for=lambda m: Down(), clock=lambda: 0.0)
    assert rep.profiles == {} and rep.unreachable == ["ollama/m:7b"]
    profile_mod.save_profiles(rep.profiles, path)                 # what the CLI does with nothing new
    assert profile_mod.load_profiles(path)["ollama/m:7b"].tier_ceiling == "MEDIUM"


def test_a_transport_error_on_a_qualifying_probe_is_inconclusive_not_a_failure():
    class JsonDown(FakeModel):
        def chat(self, model, messages, **k):
            if '"a" set to the integer 17' in " ".join(str(m.get("content", "")) for m in messages):
                return Reply(error=ERR_NO_RESPONSE)
            return super().chat(model, messages, **k)

    inv = inv_mod.collect_inventory(make_probes(ollama={"m:7b": {"caps": CAPS, "ctx": 32768}}), profiles={})
    rep = cal.run_calibration(inv, completer_for=lambda m: JsonDown(), clock=lambda: 0.0)
    assert rep.unreachable == ["ollama/m:7b"] and rep.profiles == {}


def test_a_late_transport_error_is_not_run_rather_than_failed():
    class RecallDown(FakeModel):
        def chat(self, model, messages, **k):
            if "ZEBRA-" in " ".join(str(m.get("content", "")) for m in messages):
                return Reply(error=ERR_NO_RESPONSE)
            return super().chat(model, messages, **k)

    inv = inv_mod.collect_inventory(make_probes(ollama={"m:7b": {"caps": CAPS, "ctx": 32768}}), profiles={})
    rep = cal.run_calibration(inv, completer_for=lambda m: RecallDown(), clock=lambda: 0.0)
    prof = rep.profiles["ollama/m:7b"]
    assert prof.probes["long_context"].ok is None                 # not run, not failed
    assert prof.tier_ceiling == "EASY"


def test_cli_reports_unreachable_and_keeps_the_stored_profile(monkeypatch, tmp_path, capsys):
    from llm_router.commands.calibrate import cmd_calibrate

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    profile_mod.save_profiles({"ollama/m:7b": _good_profile("ollama/m:7b")})
    probes = make_probes(ollama={"m:7b": {"caps": CAPS, "ctx": 32768}})
    monkeypatch.setattr(inv_mod, "Probes", lambda: probes)
    monkeypatch.setattr(cal, "default_completer", lambda e, post, base: Down())
    assert cmd_calibrate([]) == 1
    assert "unreachable, not recorded" in capsys.readouterr().out
    assert profile_mod.load_profiles()["ollama/m:7b"].tier_ceiling == "MEDIUM"


# ----------------------------------------------------------- paid-run guards (nits)

def _many_paid(n):
    return make_probes(env={"OPENAI_API_KEY": KEY, "ANTHROPIC_API_KEY": OTHER_KEY},
                       claude="/bin/claude", usage=("ok", 0.1))


def test_allow_paid_without_models_needs_yes_when_more_than_three_paid(monkeypatch, tmp_path, capsys):
    from llm_router.commands.calibrate import cmd_calibrate

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    probes = _many_paid(0)
    monkeypatch.setattr(inv_mod, "Probes", lambda: probes)
    sent = []

    class Spy(FakeModel):
        def chat(self, *a, **k):
            sent.append(1)
            return super().chat(*a, **k)

    monkeypatch.setattr(cal, "default_completer", lambda e, post, base: Spy())
    assert cmd_calibrate(["--allow-paid"]) == 2
    cap = capsys.readouterr()
    assert "Total estimate" in cap.out and "pass --yes" in cap.err and sent == []
    assert cmd_calibrate(["--allow-paid", "--models", "claude_subscription/haiku"]) == 0   # named: no --yes needed
    assert sent


def test_max_usd_refuses_an_over_budget_run_before_sending(monkeypatch, tmp_path, capsys):
    from llm_router.commands.calibrate import cmd_calibrate

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    probes = _many_paid(0)
    monkeypatch.setattr(inv_mod, "Probes", lambda: probes)
    monkeypatch.setattr(cal, "default_completer",
                        lambda e, post, base: (_ for _ in ()).throw(AssertionError("sent")))
    assert cmd_calibrate(["--allow-paid", "--models", "gpt-5.5", "--max-usd", "0.000001"]) == 2
    assert "exceeds --max-usd" in capsys.readouterr().err


# ----------------------------------------------------------------- inventory --verify

def fake_ping(statuses, calls):
    def ping(provider, key):
        calls.append((provider, key))
        s = statuses.get(provider, 200)
        if isinstance(s, Exception):
            raise s
        return s
    return ping


def verified_inv(statuses, env, tmp_path, calls=None, **kw):
    calls = [] if calls is None else calls
    p = make_probes(env=env, ping=fake_ping(statuses, calls), ping_cache=tmp_path / "c.json")
    return inv_mod.collect_inventory(p, profiles={}, verify=True, **kw), calls


def test_verify_200_marks_the_path_verified_and_says_no_tokens_were_used(tmp_path):
    inv, calls = verified_inv({}, {"OPENAI_API_KEY": KEY}, tmp_path)
    m = inv.get("openai/gpt-5.5")
    assert m.path_verified and m.authorized
    assert "HTTP 200" in m.path_detail and "no tokens used" in m.path_detail
    assert calls == [("openai", KEY)]


@pytest.mark.parametrize("status", [401, 403])
def test_verify_rejection_marks_path_unverified_and_unauthorized(status, tmp_path):
    inv, _ = verified_inv({"openai": status}, {"OPENAI_API_KEY": KEY}, tmp_path)
    m = inv.get("openai/gpt-5.5")
    assert not m.path_verified and not m.authorized
    assert f"rejected by openai (HTTP {status})" in m.path_detail
    assert f"HTTP {status}" in m.auth_detail
    chosen, skipped = cal.select_models(inv, None, allow_paid=True)
    assert "openai/gpt-5.5" not in [c.id for c in chosen]         # a rejected key is never probed


def test_verify_network_failure_leaves_it_unverified_and_labelled_unreachable(tmp_path):
    inv, _ = verified_inv({"openai": None}, {"OPENAI_API_KEY": KEY}, tmp_path)
    m = inv.get("openai/gpt-5.5")
    assert not m.path_verified and m.authorized                    # not the key's fault
    assert m.path_detail.startswith("unreachable")
    assert not (tmp_path / "c.json").exists() or "openai" not in json.loads((tmp_path / "c.json").read_text())


def test_verify_unexpected_status_is_not_a_verdict(tmp_path):
    inv, _ = verified_inv({"openai": 429}, {"OPENAI_API_KEY": KEY}, tmp_path)
    m = inv.get("openai/gpt-5.5")
    assert not m.path_verified and m.authorized and "unexpected HTTP 429" in m.path_detail


def test_provider_without_a_zero_cost_endpoint_is_not_pinged(tmp_path):
    inv, calls = verified_inv({}, {"PERPLEXITYAI_API_KEY": KEY}, tmp_path)
    assert calls == []
    assert inv.api_key_names == ["PERPLEXITYAI_API_KEY"]


def test_each_key_goes_only_to_its_own_provider(tmp_path):
    inv, calls = verified_inv({}, {"OPENAI_API_KEY": KEY, "ANTHROPIC_API_KEY": OTHER_KEY}, tmp_path)
    assert sorted(calls) == [("anthropic", OTHER_KEY), ("openai", KEY)]


def test_no_ping_unless_verify_is_asked_for():
    p = make_probes(env={"OPENAI_API_KEY": KEY})          # default fake ping raises if called
    inv = inv_mod.collect_inventory(p, profiles={})
    assert not inv.get("openai/gpt-5.5").path_verified


def test_no_key_shaped_string_appears_in_any_output_or_cache(tmp_path):
    inv, _ = verified_inv({"anthropic": 401}, {"OPENAI_API_KEY": KEY, "ANTHROPIC_API_KEY": OTHER_KEY}, tmp_path)
    blob = json.dumps(inventory_to_dict(inv)) + inv_mod.render_table(inv) + (tmp_path / "c.json").read_text()
    assert KEY not in blob and OTHER_KEY not in blob and "sk-" not in blob
    cache = json.loads((tmp_path / "c.json").read_text())
    assert set(cache) == {"openai", "anthropic"} and all(set(v) == {"fp", "ts", "verdict", "status"}
                                                         for v in cache.values())


def test_cache_hits_within_the_ttl_and_expires_after_it_and_notices_a_new_key(tmp_path):
    cache = tmp_path / "c.json"
    calls = []
    ping = fake_ping({}, calls)
    args = dict(ping=ping, cache=cache)
    assert auth_ping.verify_provider("openai", KEY, now=1000.0, **args) == ("verified", 200)
    assert auth_ping.verify_provider("openai", KEY, now=1000.0 + 599, **args) == ("verified", 200)
    assert len(calls) == 1                                          # served from cache
    auth_ping.verify_provider("openai", KEY, now=1000.0 + 601, **args)
    assert len(calls) == 2                                          # TTL expired
    auth_ping.verify_provider("openai", OTHER_KEY, now=1000.0 + 602, **args)
    assert len(calls) == 3                                          # a different key is never served from cache


def test_unreachable_results_are_never_cached(tmp_path):
    cache = tmp_path / "c.json"
    calls = []
    ping = fake_ping({"openai": None}, calls)
    auth_ping.verify_provider("openai", KEY, ping=ping, now=1.0, cache=cache)
    auth_ping.verify_provider("openai", KEY, ping=ping, now=2.0, cache=cache)
    assert len(calls) == 2


class _FakeOpener:
    """Stands in for the no-redirect, no-proxy opener; records every request."""

    def __init__(self, behaviour, seen):
        self._behaviour, self._seen = behaviour, seen

    def open(self, req, timeout):
        self._seen.append(req)
        return self._behaviour(req)


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_ping_sends_the_key_only_in_a_header_to_the_providers_own_https_host(monkeypatch):
    seen = []
    monkeypatch.setattr(auth_ping, "_opener", lambda: _FakeOpener(lambda r: _Resp(), seen))
    for provider, (url, _style) in auth_ping.ENDPOINTS.items():
        assert auth_ping.http_ping(provider, KEY) == 200
        req = seen[-1]
        assert req.full_url == url and KEY not in req.full_url
        assert KEY in "".join(req.headers.values())
    assert auth_ping.http_ping("perplexity", KEY) is None and len(seen) == len(auth_ping.ENDPOINTS)


def test_http_ping_maps_errors_to_status_or_none_without_echoing_the_key(monkeypatch):
    def raise_http(req):
        raise urllib.error.HTTPError(req.full_url, 401, f"bad key {KEY}", {}, None)

    monkeypatch.setattr(auth_ping, "_opener", lambda: _FakeOpener(raise_http, []))
    assert auth_ping.http_ping("openai", KEY) == 401

    def raise_net(req):
        raise OSError(f"timed out talking to {KEY}")

    monkeypatch.setattr(auth_ping, "_opener", lambda: _FakeOpener(raise_net, []))
    assert auth_ping.http_ping("openai", KEY) is None


def test_cli_verify_flag_runs_the_ping_and_is_off_by_default(monkeypatch, tmp_path, capsys):
    from llm_router.commands.inventory import cmd_inventory

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    calls = []
    probes = make_probes(env={"OPENAI_API_KEY": KEY}, ping=fake_ping({}, calls), ping_cache=tmp_path / "c.json")
    monkeypatch.setattr(inv_mod, "Probes", lambda: probes)
    assert cmd_inventory(["--json"]) == 0 and calls == []
    assert cmd_inventory(["--json", "--verify"]) == 0
    out = capsys.readouterr().out
    assert calls == [("openai", KEY)] and KEY not in out


# ------------------------------------- cloud-backed Ollama: sign-in is checked, not assumed
#
# #260's review: cloud-backed Ollama models got authorized=True / path_verified=True
# unconditionally ("Ollama sign-in state is not inspected"), so
# hard_failures(entry, Needs(tools=True), now) == [] and resolve("EASY", ...) routed to
# the cloud model with zero rejections. The daemon's own `POST /api/me` (no model call,
# no quota; only the HTTP status is read) is now the evidence.

from llm_router.resolver import profile as _profile  # noqa: E402
from llm_router.resolver.resolve import (  # noqa: E402
    STATUS_NO_ELIGIBLE, Needs, Setup, hard_failures, resolve)

from .fakes import NOW  # noqa: E402

CLOUD = "ollama/evil:120b-cloud"
_EASY = {"json": _profile.ProbeResult(True), "edit": _profile.ProbeResult(True),
         "tool_call": _profile.ProbeResult(True),
         "long_context": _profile.ProbeResult(True, size_tokens=8000)}


def _cloud_inv(signin, local=False, profiles=None):
    models = {"evil:120b-cloud": {"caps": CAPS}}
    if local:
        models["qwen3:8b"] = {"caps": CAPS}
    return inv_mod.collect_inventory(make_probes(ollama=models, ollama_signin=signin),
                                     profiles=profiles or {})


def test_cloud_ollama_confirmed_signed_in_is_eligible():
    m = _cloud_inv("signed_in").get(CLOUD)
    assert m.authorized and m.path_verified and "signed in" in m.auth_detail
    assert hard_failures(m, Needs(tools=True), NOW) == []


def test_cloud_ollama_signed_out_is_excluded_with_the_reason():
    m = _cloud_inv("signed_out").get(CLOUD)
    assert not m.authorized and not m.path_verified
    assert m.auth_detail == "Ollama cloud model; not signed in (POST /api/me returned 401)"
    fails = hard_failures(m, Needs(tools=True), NOW)
    assert any(f.startswith("not authorized") and "not signed in" in f for f in fails)
    assert any(f.startswith("execution path not verified") for f in fails)


@pytest.mark.parametrize("signin", ["unknown", "garbage", RuntimeError("boom " + KEY), TimeoutError()])
def test_cloud_ollama_with_no_signal_error_or_timeout_is_excluded(signin):
    m = _cloud_inv(signin).get(CLOUD)
    assert not m.authorized and not m.path_verified
    assert m.auth_detail == "Ollama cloud model; sign-in not confirmed"
    assert m.path_detail == "Ollama cloud model; sign-in not confirmed"
    assert KEY not in json.dumps(inventory_to_dict(_cloud_inv(signin)))     # exception text dropped
    assert hard_failures(m, Needs(tools=True), NOW)


def test_an_unverified_cloud_ollama_model_that_wins_every_axis_is_not_selected():
    # Measured FRONTIER-capable cloud model vs a weaker configured local model: with
    # sign-in unconfirmed the cloud model must be rejected and the configured model kept.
    big = {**_EASY, "reasoning": _profile.ProbeResult(True)}
    for signin in ("unknown", "signed_out"):
        inv = _cloud_inv(signin, local=True, profiles={
            CLOUD: _profile.build_profile(CLOUD, big, reachable=True, now=NOW)})
        res = resolve("EASY", Needs(tools=True), Setup(inv, "ollama/qwen3:8b"), now=NOW)
        assert res.model != CLOUD, signin
        assert all(r.model != CLOUD for r in res.fallbacks), signin
        rej = {r.model: r.reasons for r in res.rejected}
        assert any("not authorized" in x for x in rej[CLOUD]), signin
        assert res.keep_configured == "ollama/qwen3:8b"
        assert any(r.kind == "configured" and r.model == "ollama/qwen3:8b" for r in res.fallbacks) \
            or res.model == "ollama/qwen3:8b"
    only = _cloud_inv("unknown", profiles={
        CLOUD: _profile.build_profile(CLOUD, _EASY, reachable=True, now=NOW)})
    res = resolve("EASY", Needs(tools=True), Setup(only, "mine"), now=NOW)
    assert res.status == STATUS_NO_ELIGIBLE and res.model is None and res.keep_configured == "mine"
    ok = _cloud_inv("signed_in", profiles={
        CLOUD: _profile.build_profile(CLOUD, _EASY, reachable=True, now=NOW)})
    assert resolve("EASY", Needs(tools=True), Setup(ok, "mine"), now=NOW).model == CLOUD


def test_plain_local_ollama_models_are_unaffected_and_never_trigger_the_signin_probe():
    calls = []
    p = make_probes(ollama={"qwen3:8b": {"caps": CAPS}})
    p.ollama_signin_status = lambda base: calls.append(base) or "signed_out"
    m = inv_mod.collect_inventory(p, profiles={}).get("ollama/qwen3:8b")
    assert (m.route_kind, m.privacy, m.authorized, m.path_verified) == ("local", "local", True, True)
    assert m.auth_detail == "local server, no credential" and calls == []
    # a signed-out account does not touch local models listed beside a cloud one
    inv = _cloud_inv("signed_out", local=True)
    q = inv.get("ollama/qwen3:8b")
    assert q.authorized and q.path_verified and q.privacy == "local"


def test_signin_is_probed_once_per_inventory_even_with_several_cloud_models():
    calls = []
    p = make_probes(ollama={"a:1b-cloud": {"caps": CAPS}, "b:1b-cloud": {"caps": CAPS}})
    p.ollama_signin_status = lambda base: calls.append(base) or "signed_in"
    inv = inv_mod.collect_inventory(p, profiles={})
    assert calls == ["http://127.0.0.1:11434"] and all(m.authorized for m in inv.models)


class _MeResp:
    def __init__(self, status):
        self.status = status

    def read(self, *a):
        raise AssertionError("the /api/me body must never be read")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("status,expected", [
    (200, "signed_in"), (401, "signed_out"), (403, "unknown"), (500, "unknown"),
    (503, "unknown"), (302, "unknown"), (None, "unknown")])
def test_ollama_signin_status_reads_only_the_http_status(monkeypatch, status, expected):
    seen = []

    def behaviour(req):
        if status is None:
            raise OSError(f"timed out {KEY}")
        if status == 200:
            return _MeResp(200)
        raise urllib.error.HTTPError(req.full_url, status, f"body {KEY}", {}, None)

    monkeypatch.setattr(auth_ping, "_opener", lambda: _FakeOpener(behaviour, seen))
    assert inv_mod._ollama_signin_status("http://127.0.0.1:11434/") == expected
    (req,) = seen
    assert req.full_url == "http://127.0.0.1:11434/api/me" and req.get_method() == "POST"


def test_ollama_signin_status_uses_the_shared_no_redirect_opener_and_a_5s_timeout(monkeypatch):
    got = {}

    class Op:
        def open(self, req, timeout):
            got["timeout"] = timeout
            return _MeResp(200)

    # the opener it is given is auth_ping's: redirects surface as an error status, not a re-send
    assert auth_ping._NoRedirect().redirect_request(None, None, 302, "", {}, "http://evil/") is None
    monkeypatch.setattr(auth_ping, "_opener", lambda: Op())
    assert inv_mod._ollama_signin_status("http://127.0.0.1:11434") == "signed_in"
    assert got["timeout"] == 5.0 == inv_mod.OLLAMA_ME_TIMEOUT_S
