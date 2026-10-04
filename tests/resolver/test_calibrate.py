"""Calibration probes graded against scripted fake models. No real model, no spend."""

from __future__ import annotations

import json
import re

import pytest

from llm_router.resolver import calibrate as cal
from llm_router.resolver import inventory as inv_mod
from llm_router.resolver import profile as profile_mod
from llm_router.resolver.calibrate import Reply
from llm_router.resolver.profile import ProbeResult, tier_from_probes

from .fakes import make_probes


class FakeModel:
    """A scripted chat backend. ``skill`` names what it gets right."""

    supports_tools = True
    supports_images = True

    def __init__(self, skill=("json", "edit", "tool", "vision", "recall")):
        self.skill, self.calls = set(skill), []

    def chat(self, model, messages, *, tools=None, num_ctx=None, timeout=90):
        self.calls.append({"model": model, "tools": bool(tools), "num_ctx": num_ctx})
        text = " ".join(str(m.get("content", "")) for m in messages)
        if tools:
            if any(m.get("role") == "tool" for m in messages):
                result = next(m["content"] for m in messages if m.get("role") == "tool")
                return Reply(text=f"The value is {result}." if "tool" in self.skill else "unsure")
            if "tool" in self.skill:
                return Reply(tool_calls=[{"name": "lookup", "arguments": {"key": "alpha"}}])
            return Reply(text="I would guess 42.")
        if any(m.get("images") for m in messages):
            return Reply(text="0000")     # filled in by subclasses that can see
        if "ZEBRA-" in text:
            code = re.search(r"ZEBRA-\d{4}", text).group(0)
            return Reply(text=code if "recall" in self.skill else "I don't know")
        if '"a" set to the integer 17' in text:
            return Reply(text='```json\n{"a": 17, "b": "x"}\n```' if "json" in self.skill else "seventeen")
        if "old_string" in text:
            if "edit" in self.skill:
                return Reply(text='{"old_string": "return a - b", "new_string": "return a + b"}')
            return Reply(text='{"old_string": "return a - b", "new_string": "return a * b"}')
        return Reply(error="unexpected prompt")


def run_one(model: FakeModel, entry=None, **kw):
    inv = inv_mod.collect_inventory(
        make_probes(ollama={"m:7b": {"caps": ["completion", "tools"], "ctx": 32768}}), profiles={})
    entry = entry or inv.get("ollama/m:7b")
    return cal.calibrate_model(entry, model, tokens=kw.get("tokens", 8000), probe_timeout=30,
                               deadline=1e9, clock=lambda: 0.0)


def test_a_capable_model_reaches_medium_not_frontier():
    prof = run_one(FakeModel())
    assert {n: p.ok for n, p in prof.probes.items()} == {
        "json": True, "edit": True, "tool_call": True, "long_context": True}
    assert prof.tier_ceiling == "MEDIUM"
    assert "cannot establish FRONTIER" in prof.tier_detail
    assert prof.recall_tokens == 8000 and prof.reachable


@pytest.mark.parametrize("missing,expect_ceiling,why", [
    ("json", None, "valid-JSON"),
    ("edit", None, "verified-edit"),
    ("tool", "EASY", "tool round-trip"),
    ("recall", "EASY", "recall"),
])
def test_each_failed_probe_caps_the_tier_for_its_own_reason(missing, expect_ceiling, why):
    skills = {"json", "edit", "tool", "vision", "recall"} - {missing}
    prof = run_one(FakeModel(skills))
    assert prof.tier_ceiling == expect_ceiling
    assert why in prof.tier_detail


def test_edit_probe_applies_and_compares_it_never_executes_model_text():
    # The wrong edit applies cleanly (so it is judged), and is judged wrong.
    r = cal.probe_edit(FakeModel({"json"}), "m", 5)
    assert r.ok is False and "not the expected program" in r.detail

    class Hostile(FakeModel):
        def chat(self, *a, **k):
            return Reply(text='{"old_string": "return a - b", "new_string": "return __import__(\'os\').system(\'x\')"}')

    r = cal.probe_edit(Hostile(), "m", 5)
    assert r.ok is False                                    # judged by AST equality, nothing runs


def test_edit_probe_rejects_an_old_string_that_is_not_unique_or_absent():
    assert cal.apply_edit("a a", "a", "b") is None          # twice
    assert cal.apply_edit("abc", "zzz", "b") is None        # absent
    assert cal.apply_edit("abc", "", "b") is None           # empty
    assert cal.apply_edit("abc", "b", "X") == "aXc"


def test_tool_probe_distinguishes_no_call_wrong_args_and_ignored_result():
    class NoCall(FakeModel):
        pass
    assert cal.probe_tool_call(NoCall({"json"}), "m", 5).detail == "no lookup tool call"

    class WrongArgs(FakeModel):
        def chat(self, model, messages, **k):
            return Reply(tool_calls=[{"name": "lookup", "arguments": {"key": "beta"}}])
    assert "wrong arguments" in cal.probe_tool_call(WrongArgs(), "m", 5).detail

    class Ignores(FakeModel):
        def chat(self, model, messages, **k):
            if any(m.get("role") == "tool" for m in messages):
                return Reply(text="no idea")
            return Reply(tool_calls=[{"name": "lookup", "arguments": {"key": "alpha"}}])
    r = cal.probe_tool_call(Ignores(), "m", 5)
    assert r.ok is False and "did not use its result" in r.detail


def test_tool_probe_is_not_run_on_a_route_without_tool_schemas():
    class NoTools(FakeModel):
        supports_tools = False
    r = cal.probe_tool_call(NoTools(), "m", 5)
    assert r.ok is None                                     # not a pass, not a fail


def test_vision_probe_is_only_run_where_vision_is_claimed_and_needs_exact_reads():
    sees = FakeModel()
    inv = inv_mod.collect_inventory(
        make_probes(ollama={"v:7b": {"caps": ["completion", "vision"], "ctx": 32768},
                            "t:7b": {"caps": ["completion"], "ctx": 32768}}), profiles={})
    assert "vision" in run_one(sees, inv.get("ollama/v:7b")).probes
    assert "vision" not in run_one(sees, inv.get("ollama/t:7b")).probes   # not claimed -> not probed

    class Reader(FakeModel):
        def chat(self, model, messages, **k):
            if any(m.get("images") for m in messages):
                return Reply(text=self.answer)
            return super().chat(model, messages, **k)
    blind = Reader()
    blind.answer = "1234"           # random 4-digit code: a fixed answer cannot pass 3 trials
    assert cal.probe_vision(blind, "m", 5).ok is False


def test_long_context_probe_states_its_size_and_skips_when_the_window_is_smaller():
    ok = cal.probe_long_context(FakeModel(), "m", 5, tokens=8000, window=32768)
    assert ok.ok is True and ok.size_tokens == 8000
    miss = cal.probe_long_context(FakeModel({"json"}), "m", 5, tokens=8000, window=32768)
    assert miss.ok is False and "missed the needle" in miss.detail
    small = cal.probe_long_context(FakeModel(), "m", 5, tokens=8000, window=4096)
    assert small.ok is None and small.size_tokens == 8000          # not run is not a pass


def test_long_context_prompt_really_is_the_stated_size_and_asks_for_a_big_window():
    m = FakeModel()
    cal.probe_long_context(m, "m", 5, tokens=8000, window=None)
    assert m.calls[0]["num_ctx"] == 9024


def test_transport_failure_is_recorded_as_unreachable_not_as_incapable():
    class Down(FakeModel):
        def chat(self, *a, **k):
            return Reply(error="no response (timeout, unreachable or model failed to load)")
    prof = run_one(Down())
    assert prof.tier_ceiling is None and prof.reachable is False


# ------------------------------------------------------------- tier derivation

@pytest.mark.parametrize("probes,tools,expect", [
    ({"json": True, "edit": True, "tool_call": True, "long_context": True}, True, "MEDIUM"),
    ({"json": True, "edit": True, "tool_call": None, "long_context": True}, True, "EASY"),   # not run != pass
    ({"json": True, "edit": True, "long_context": True}, False, "MEDIUM"),                   # tool-less model
    ({"json": True, "edit": False}, True, None),
    ({"json": False, "edit": True}, True, None),
    ({}, True, None),
])
def test_tier_from_probes_table(probes, tools, expect):
    results = {k: ProbeResult(v, size_tokens=8000 if k == "long_context" else None)
               for k, v in probes.items()}
    assert tier_from_probes(results, claims_tools=tools)[0] == expect


def test_recall_below_the_medium_size_does_not_qualify_for_medium():
    results = {"json": ProbeResult(True), "edit": ProbeResult(True),
               "tool_call": ProbeResult(True), "long_context": ProbeResult(True, size_tokens=2000)}
    assert tier_from_probes(results)[0] == "EASY"


# ------------------------------------------------------- selection / budget / cost

def _inv():
    return inv_mod.collect_inventory(make_probes(
        ollama={"m:7b": {"caps": ["completion", "tools"], "ctx": 32768}},
        claude="/bin/claude", usage=("ok", 0.1),
        env={"OPENAI_API_KEY": "x"}), profiles={})


def test_cloud_models_are_skipped_without_allow_paid_and_never_called():
    spy = {"cloud": 0}

    class Spy(FakeModel):
        def chat(self, *a, **k):
            spy["cloud"] += 1
            return super().chat(*a, **k)

    def completer_for(m):
        assert m.route_kind == "local", f"{m.id} was about to be probed without --allow-paid"
        return FakeModel()

    rep = cal.run_calibration(_inv(), completer_for=completer_for, clock=lambda: 0.0)
    assert list(rep.profiles) == ["ollama/m:7b"]
    skipped = dict(rep.skipped)
    assert "claude_subscription/opus" in skipped and "--allow-paid" in skipped["claude_subscription/opus"]
    assert "openai/gpt-5.5" in skipped
    assert spy["cloud"] == 0


def test_allow_paid_includes_cloud_and_estimates_each():
    seen = []

    def completer_for(m):
        seen.append(m.id)
        c = FakeModel()
        c.supports_images = c.supports_tools = m.route_kind == "api"
        return c

    rep = cal.run_calibration(_inv(), allow_paid=True, completer_for=completer_for, clock=lambda: 0.0)
    assert "openai/gpt-5.5" in seen and "claude_subscription/opus" in seen
    est = {e.model_id: e for e in rep.estimates}
    assert est["ollama/m:7b"].usd == 0.0
    assert est["openai/gpt-5.5"].usd > 0                         # priced from the registry
    assert "subscription quota" in est["claude_subscription/opus"].note
    assert est["claude_subscription/opus"].usd == 0.0


def test_models_filter_matches_ids_substrings_and_globs():
    chosen, _ = cal.select_models(_inv(), ["ollama/*"], allow_paid=False)
    assert [m.id for m in chosen] == ["ollama/m:7b"]
    chosen, _ = cal.select_models(_inv(), ["opus"], allow_paid=True)
    assert [m.id for m in chosen] == ["claude_subscription/opus"]


def test_unauthorized_and_removed_models_are_never_probed():
    inv = inv_mod.collect_inventory(make_probes(
        codex=("/bin/codex", "Not logged in"),
        ollama={"a:1b": {"caps": ["completion"]}}), profiles={})
    old = inv_mod.collect_inventory(make_probes(
        ollama={"a:1b": {"caps": ["completion"]}, "gone:1b": {"caps": ["completion"]}}), profiles={})
    inv = inv_mod.reconcile(old, inv)
    chosen, skipped = cal.select_models(inv, None, allow_paid=True)
    assert [m.id for m in chosen] == ["ollama/a:1b"]
    reasons = dict(skipped)
    assert "not authorized" in reasons["codex/gpt-5.5"] and "removed" in reasons["ollama/gone:1b"]


def test_budget_exhaustion_records_the_model_as_unfinished_not_as_failed():
    t = {"now": 0.0}

    class Slow(FakeModel):
        def chat(self, *a, **k):
            t["now"] += 40.0
            return super().chat(*a, **k)

    rep = cal.run_calibration(_inv(), budget_s=50, completer_for=lambda m: Slow(),
                              clock=lambda: t["now"])
    assert rep.profiles == {} and rep.out_of_budget == ["ollama/m:7b"]


# --------------------------------------------------------------------- storage

def test_profile_file_is_versioned_merged_and_survives_garbage(tmp_path):
    p = tmp_path / "capability_profile.json"
    a = profile_mod.build_profile("ollama/a", {"json": ProbeResult(True)}, reachable=True, now=1.0)
    b = profile_mod.build_profile("ollama/b", {"json": ProbeResult(False, "x")}, reachable=True, now=2.0)
    profile_mod.save_profiles({"ollama/a": a}, p)
    profile_mod.save_profiles({"ollama/b": b}, p)
    raw = json.loads(p.read_text())
    assert raw["version"] == profile_mod.PROFILE_VERSION and raw["suite_version"] == profile_mod.PROBE_SUITE_VERSION
    assert "not a global ranking" in raw["note"].lower()
    back = profile_mod.load_profiles(p)
    assert set(back) == {"ollama/a", "ollama/b"} and back["ollama/a"].probes["json"].ok is True
    raw["version"] = 99
    p.write_text(json.dumps(raw))
    assert profile_mod.load_profiles(p) == {}                 # unknown version: nothing measured
    p.write_text("garbage")
    assert profile_mod.load_profiles(p) == {}
    assert profile_mod.load_profiles(tmp_path / "nope.json") == {}


# ------------------------------------------------------------------------- CLI

def _patch_cli(monkeypatch, tmp_path, fake=None):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    probes = make_probes(ollama={"m:7b": {"caps": ["completion", "tools"], "ctx": 32768}},
                         claude="/bin/claude", usage=("ok", 0.1), env={"OPENAI_API_KEY": "x"})
    monkeypatch.setattr(inv_mod, "Probes", lambda: probes)
    model = fake or FakeModel()
    monkeypatch.setattr(cal, "default_completer", lambda entry, post, base: model)
    return model


def test_cli_prints_the_estimate_before_the_first_paid_call(monkeypatch, tmp_path, capsys):
    from llm_router.commands.calibrate import cmd_calibrate

    order = []

    class Marking(FakeModel):
        def chat(self, *a, **k):
            order.append("call")
            return super().chat(*a, **k)

    _patch_cli(monkeypatch, tmp_path, Marking())
    real_print = print

    def spying_print(*a, **k):
        if a and "Estimated spend" in str(a[0]):
            order.append("estimate")
        real_print(*a, **k)

    monkeypatch.setattr("builtins.print", spying_print)
    rc = cmd_calibrate(["--allow-paid", "--models", "gpt-5.5,ollama/*", "--budget-s", "100"])
    out = capsys.readouterr().out
    assert rc == 0 and order[0] == "estimate" and "call" in order
    assert "est. $" in out and "metered API" in out


def test_cli_dry_run_sends_nothing_and_default_run_is_local_only(monkeypatch, tmp_path, capsys):
    from llm_router.commands.calibrate import cmd_calibrate

    model = _patch_cli(monkeypatch, tmp_path)
    assert cmd_calibrate(["--allow-paid", "--dry-run"]) == 0
    assert model.calls == [] and "nothing sent" in capsys.readouterr().out
    assert cmd_calibrate([]) == 0
    out = capsys.readouterr().out
    assert "ollama/m:7b" in out and "--allow-paid" in out          # cloud models listed as skipped
    assert "not a global ranking" in out
    assert {c["model"] for c in model.calls} == {"ollama/m:7b"}
    assert "ollama/m:7b" in profile_mod.load_profiles()


def test_cli_rejects_bad_arguments(monkeypatch, tmp_path, capsys):
    from llm_router.commands.calibrate import cmd_calibrate

    assert cmd_calibrate(["--budget-s", "abc"]) == 2
    assert cmd_calibrate(["--wat"]) == 2
