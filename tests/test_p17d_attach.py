"""scripts/p17d_attach.py on synthetic fixtures only (no real transcript, no network, no model).

The pinned GE3 helpers (gt500.py, supply.py) live outside the repo; the module tests skip without them.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
import pwd  # noqa: E402  (conftest points HOME at a temp dir; the pinned helpers live in the real home)

GT_SCRIPTS = Path(os.environ.get("P17D_GT_SCRIPTS") or
                  Path(pwd.getpwuid(os.getuid()).pw_dir) / ".rsi/research/primary-plan/v16/gt500/scripts")
HAVE_GT = (GT_SCRIPTS / "gt500.py").exists() and (GT_SCRIPTS / "supply.py").exists()

spec = importlib.util.spec_from_file_location("p17d_attach", ROOT / "scripts" / "p17d_attach.py")
pa = importlib.util.module_from_spec(spec)
sys.modules["p17d_attach"] = pa
spec.loader.exec_module(pa)

SENTINEL = "zebrafinch"           # appears in every fixture prompt; must never reach stdout/stderr
CWD = "/work/repo"
KIND_HOME = "/home/fixture"
S1, S2, S3, S4 = ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
                  "33333333-3333-3333-3333-333333333333", "44444444-4444-4444-4444-444444444444")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def user(sid, ts, text, **kw):
    rec = {"type": "user", "sessionId": sid, "timestamp": ts, "entrypoint": "cli", "promptSource": "typed",
           "cwd": kw.pop("cwd", CWD), "message": {"role": "user", "content": text}}
    rec.update(kw)
    return rec


def asst(sid, ts, mid, text=None, model="claude-opus-5-5", tool=False):
    block = {"type": "tool_use", "id": "t", "name": "Read", "input": {}} if tool else {"type": "text", "text": text}
    return {"type": "assistant", "sessionId": sid, "timestamp": ts,
            "message": {"id": mid, "model": model, "role": "assistant", "content": [block]}}


def tool_result(sid, ts):
    return {"type": "user", "sessionId": sid, "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}


P = {  # fixture prompts (all carry the sentinel)
    "a1": f"please refactor the {SENTINEL} parser so that errors carry line numbers and add tests",
    "a2": f"now explain why the {SENTINEL} cache invalidation fails after a restart of the daemon",
    "a3": f"ship it {SENTINEL}",
    "b1": f"design an evaluation plan for the {SENTINEL} router with stratified held out sets please",
    "sys": f"system injected {SENTINEL} text that a script wrote into the session for the user",
    "late": f"this prompt arrives after the shadow switch on for the {SENTINEL} proxy window cut",
    "exec": f"executor session prompt about the {SENTINEL} build that must never be counted here",
    "res": f"research session prompt about {SENTINEL} pricing that lives under the rsi tree today",
    "slash": f"/model opus {SENTINEL}",
    "haiku": f"a prompt that was served on haiku for the {SENTINEL} arm and must be excluded now",
    "ge3t": f"ge3 tune item text about the {SENTINEL} engine policy which was used for tuning",
    "pe": f"p eval calibration prompt about the {SENTINEL} classifier rubric and its tiers exactly",
    "pe_near": f"p eval calibration prompt about the {SENTINEL} classifier rubric and its tiers exactly!",
    "hand": f"investigate why the {SENTINEL} hook did not fire on the last run",
    "g1": f"ge3 held out prompt about debugging the {SENTINEL} websocket reconnect loop in prod",
    "gcal": f"ge3 held out prompt that is also an E2 calibration item about {SENTINEL} sessions",
    "unrelated": f"not in the corpus at all {SENTINEL} so attach never looks at it further",
    "deny": f"a prompt that names the quarantinedproject codename in the {SENTINEL} module today",
    "scrub": f"call the {SENTINEL} api with bearer abc123 and report what comes back from it",
    "edge_in": f"just before the window end the {SENTINEL} prompt still counts for this run",
    "edge_out": f"half a second after the window end the {SENTINEL} prompt must be excluded",
    "g_e2": f"p eval calibration prompt about the {SENTINEL} classifier rubric and its tiers exactly?",
}


@pytest.fixture
def world(tmp_path):
    if not HAVE_GT:
        pytest.skip("pinned GE3 helpers (gt500.py, supply.py) not on this machine")
    pp = tmp_path / "pp"
    pp.mkdir()
    gt = tmp_path / "gt"
    (gt / "scripts").mkdir(parents=True)
    for f in ("gt500.py", "supply.py"):
        shutil.copy(GT_SCRIPTS / f, gt / "scripts" / f)
    ge3 = [
        {"id": "gt-tune1", "session_id": S1, "timestamp": "2026-09-10T10:05:00Z", "prompt": P["ge3t"], "p_eval_cal": False},
        {"id": "gt-ho1", "session_id": S3, "timestamp": "2026-09-12T09:00:00Z", "prompt": P["g1"], "p_eval_cal": False},
        {"id": "gt-ho2", "session_id": S3, "timestamp": "2026-09-12T09:10:00Z", "prompt": P["gcal"], "p_eval_cal": True},
        {"id": "gt-ho3", "session_id": S3, "timestamp": "2026-09-12T09:20:00Z", "prompt": P["g_e2"], "p_eval_cal": False},
    ]
    (gt / "items.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ge3))
    (gt / "split.json").write_text(json.dumps({"tune_ids": ["gt-tune1"], "heldout_ids": ["gt-ho1", "gt-ho2", "gt-ho3"]}))
    pe = tmp_path / "sample.jsonl"
    pe.write_text(json.dumps({"session_id": "pe-s", "timestamp": "2026-08-01T00:00:00Z", "split": "calibration",
                              "text": P["pe"]}) + "\n")
    e1 = tmp_path / "tasks.json"
    e1.write_text(json.dumps([{"id": "e1", "split": "tune", "text": "rename a variable in one file", "truth": "haiku"}]))
    execs = tmp_path / "exec_sessions.txt"
    execs.write_text(S4 + "  executor\n")
    corpus_keys = ["a1", "a2", "a3", "b1", "sys", "late", "exec", "res", "slash", "haiku", "ge3t", "pe_near", "hand", "deny", "scrub", "edge_in", "edge_out"]
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text("".join(json.dumps({"i": i, "text": P[k]}) + "\n" for i, k in enumerate(corpus_keys)))
    proj = tmp_path / "projects" / "-work-repo"
    proj.mkdir(parents=True)
    s1 = [
        user(S1, "2026-09-10T10:00:00Z", P["a1"]),
        asst(S1, "2026-09-10T10:00:05Z", "msg_a", "I will look at the parser first."),
        asst(S1, "2026-09-10T10:00:06Z", "msg_a", tool=True),
        tool_result(S1, "2026-09-10T10:00:07Z"),
        asst(S1, "2026-09-10T10:00:09Z", "msg_b", "Done: errors now carry line numbers. " + "x" * 700),
        user(S1, "2026-09-10T10:05:00Z", P["ge3t"]),
        user(S1, "2026-09-10T10:06:00Z", P["a2"]),
        asst(S1, "2026-09-29T10:06:05Z", "msg_d", "Looking.", model="claude-sonnet-5-5"),
        user(S1, "2026-09-10T10:07:00Z", P["sys"], promptSource="system"),
        user(S1, "2026-09-10T10:08:00Z", P["slash"]),
        user(S1, "2026-09-10T10:09:00Z", P["a3"]),
        asst(S1, "2026-09-10T10:09:05Z", "msg_e", "Shipped."),
        user(S1, "2026-09-10T10:10:00Z", P["haiku"]),
        asst(S1, "2026-09-10T10:10:05Z", "msg_f", "Sure.", model="claude-haiku-4-5"),
        user(S1, "2026-09-10T10:11:00Z", P["pe_near"]),
        user(S1, "2026-09-10T10:12:00Z", P["hand"]),
        user(S1, "2026-10-09T10:00:00Z", P["late"]),
        user(S1, "2026-10-08T20:45:59.999Z", P["edge_in"]),
        user(S1, "2026-10-08T20:46:00.500Z", P["edge_out"]),
        user(S1, "2026-09-10T10:13:00Z", P["unrelated"]),
    ]
    (proj / f"{S1}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in s1))
    s2 = [user(S2, "2026-09-11T08:00:00Z", P["b1"]), asst(S2, "2026-09-11T08:00:05Z", "msg_g", "Plan drafted."),
          user(S2, "2026-09-11T08:14:00Z", P["scrub"]), user(S2, "2026-09-11T08:15:00Z", P["deny"])]
    (proj / f"{S2}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in s2))
    s3 = [user(S3, "2026-09-12T09:00:00Z", P["g1"]), asst(S3, "2026-09-12T09:00:05Z", "msg_h", "Checking."),
          user(S3, "2026-09-12T09:10:00Z", P["gcal"]), user(S3, "2026-09-12T09:20:00Z", P["g_e2"])]
    (proj / f"{S3}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in s3))
    s4 = [user(S4, "2026-09-13T09:00:00Z", P["exec"])]
    (proj / f"{S4}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in s4))
    rsi = tmp_path / "projects" / "-rsi"
    rsi.mkdir()
    s5 = [user("55555555-5555-5555-5555-555555555555", "2026-09-14T09:00:00Z", P["res"], cwd=KIND_HOME + "/.rsi/x")]
    (rsi / "55555555-5555-5555-5555-555555555555.jsonl").write_text("".join(json.dumps(r) + "\n" for r in s5))
    ledger = tmp_path / "proxy_calls.jsonl"
    ledger.write_text(json.dumps({"msg_id": "msg_d", "requested_model": "claude-opus-5-5",
                                  "served_model": "claude-sonnet-5-5"}) + "\n")
    pkg = tmp_path / "pkg"
    (pkg / "proxy").mkdir(parents=True)
    (pkg / "proxy" / "claude_tiers.yaml").write_text("route:\n  default:\n    simple: haiku\ncomplexity_knn: false\n")
    ov = tmp_path / "overrides.json"
    ov.write_text("{}")
    deny = tmp_path / "denylist.txt"
    deny.write_text("# comment\nquarantinedproject\n")
    pins = dict(pa.PINS)
    for name, path in (("corpus", corpus), ("ge3_items", gt / "items.jsonl"), ("ge3_split", gt / "split.json"),
                       ("p_eval_sample", pe), ("e1_tasks", e1), ("exec_sessions", execs)):
        pins[name] = sha(path)
    cfg = pa.Cfg(run_dir=pp / "v16" / "p17d", pp_root=pp, corpus=corpus, claude_root=tmp_path / "projects",
                 ledger=ledger, gt_dir=gt, p_eval_sample=pe, e1_tasks=e1, exec_sessions=execs,
                 overrides=ov, denylist=deny,
                 prereg_frozen=tmp_path / "PREREG-P17D-synthetic.md", pins=pins, kind_home=KIND_HOME,
                 rules_hand_edit_texts=[P["hand"]],
                 ps_text="", plists=[], dotenv_paths=[], live_pkg_dir=pkg)
    return cfg, tmp_path


def _items(cfg):
    return [json.loads(x) for x in (cfg.run_dir / "items.jsonl").read_text().splitlines()]


def test_attach_selects_frame_items_and_counts_every_exclusion(world, capsys):
    cfg, _ = world
    counts = pa.attach(cfg)
    items = _items(cfg)
    f = {i["prompt"] for i in items if i["group"] == "F"}
    assert f == {P["a1"], P["a2"], P["a3"], P["b1"], P["edge_in"]}
    ex = counts["exclusions"]
    assert ex["not_human_typed"] == 1 and ex["harness_or_slash"] == 1 and ex["after_window"] == 2
    assert ex["denylist"] == 1 and ex["secret_scrubber"] == 1
    assert counts["inputs"]["denylist_entries"] == 1 and counts["inputs"]["transcript_files"] == 5
    assert counts["llm_router"]["path"].startswith(str(ROOT / "src"))
    assert ex["exec_session"] == 1 and ex["kind_research"] == 1 and ex["served_haiku"] == 1
    assert ex["leakage"] == 3          # GE3 tune (session, ts), E2-cal near duplicate, rules_hand_edit
    assert counts["leak_hits_by_set"]["ge3_tune"] == 1 and counts["leak_hits_by_set"]["e2_cal"] == 1
    assert counts["leak_hits_by_set"]["rules_hand_edit"] == 1
    assert counts["F"]["n"] == 5 and counts["F"]["sessions"] == 2
    out = capsys.readouterr()
    assert SENTINEL not in out.out + out.err


def test_ge3_heldout_candidates_skip_p_eval_cal(world):
    cfg, _ = world
    counts = pa.attach(cfg)
    g = [i for i in _items(cfg) if i["group"] == "G"]
    assert [i["ge3_id"] for i in g] == ["gt-ho1"]
    assert counts["exclusions"]["g_p_eval_cal"] == 1
    assert counts["exclusions"]["g_leakage"] == 1 and counts["leak_hits_by_set"]["g:e2_cal"] == 1


def test_item_fields_ids_context_and_requested(world):
    cfg, _ = world
    pa.attach(cfg)
    from llm_router.prompt_key import key
    items = {i["prompt"]: i for i in _items(cfg)}
    a1, a2 = items[P["a1"]], items[P["a2"]]
    assert a1["item_id"] == hashlib.sha256(f"p17d:item:{S1}:2026-09-10T10:00:00Z".encode()).hexdigest()[:16]
    assert a1["text_sha"] == key(P["a1"])
    assert a1["first_in_session"] is True and a2["first_in_session"] is False
    assert all(i["assemble_ok"] for i in items.values())
    assert len(a2["assistant_tail"]) == pa.ASSISTANT_KEEP          # long assistant text kept with the 502 margin
    assert a2["requested_basis"] == "ledger" and a2["requested_tier"] == "opus"
    assert a2["served_tier"] == "sonnet"
    assert items[P["b1"]]["requested_basis"] == "transcript_served"


def test_files_are_private_and_sources_untouched(world):
    cfg, tmp = world
    src = [p for p in (tmp / "projects").rglob("*") if p.is_file()] + [cfg.ledger, cfg.corpus]
    before = {p: sha(p) for p in src}
    listing = sorted(str(p) for p in (tmp / "projects").rglob("*"))
    pa.attach(cfg)
    assert {p: sha(p) for p in src} == before
    assert sorted(str(p) for p in (tmp / "projects").rglob("*")) == listing
    assert stat.S_IMODE(os.stat(cfg.run_dir).st_mode) == 0o700
    for name in ("items.jsonl", "attach_counts.json", "tier_policy.yaml", "tier_policy.json"):
        assert stat.S_IMODE(os.stat(cfg.run_dir / name).st_mode) == 0o600
    assert SENTINEL not in (cfg.run_dir / "attach_counts.json").read_text()
    assert sorted(p.name for p in cfg.run_dir.iterdir()) == [
        "attach_counts.json", "items.jsonl", "tier_policy.json", "tier_policy.yaml"]


@pytest.mark.parametrize("make", ["items", "pinned", "frozen"])
def test_refuses_to_rerun(world, make):
    cfg, _ = world
    if make == "items":
        pa.attach(cfg)
    elif make == "pinned":
        cfg.run_dir.mkdir(parents=True)
        (cfg.run_dir / "attach.pinned").write_text("x")
    else:
        cfg.prereg_frozen.write_text("frozen")
    with pytest.raises(pa.Refuse):
        pa.attach(cfg)


def test_refuses_outside_pp_root_and_on_pin_mismatch(world, tmp_path):
    cfg, _ = world
    cfg.run_dir = tmp_path / "elsewhere"
    with pytest.raises(pa.Refuse):
        pa.attach(cfg)
    cfg.run_dir = cfg.pp_root / "v16" / "p17d"
    cfg.pins = {**cfg.pins, "corpus": "0" * 64}
    with pytest.raises(pa.Refuse):
        pa.attach(cfg)
    assert not cfg.run_dir.exists()


def test_tier_policy_precedence(world, tmp_path):
    cfg, _ = world
    env = tmp_path / "state.env"
    env.write_text("LLM_ROUTER_PROXY_TIER_POLICY=/from/dotenv.yaml\n")
    plist_argv = {"Label": "com.x.llm-router-proxy", "ProgramArguments": ["/v/bin/python", "-m", "llm_router.proxy",
                                                                           "--tier-policy", "/from/plist.yaml"]}
    plist_env = {"Label": "com.x.llm-router-proxy", "ProgramArguments": ["/v/bin/python", "-m", "llm_router.proxy"],
                 "EnvironmentVariables": {"LLM_ROUTER_PROXY_TIER_POLICY": "/from/plistenv.yaml"}}
    cfg.ps_text = "/v/bin/python -m llm_router.proxy --tier-policy=/from/ps.yaml\n"
    cfg.plists, cfg.dotenv_paths = [plist_argv], [("state", env)]
    assert pa.resolve_tier_policy(cfg) == {"path": "/from/ps.yaml", "basis": "1:process_argv"}
    cfg.ps_text = ""
    assert pa.resolve_tier_policy(cfg)["basis"] == "1:plist_argv"
    cfg.plists = [plist_env]
    assert pa.resolve_tier_policy(cfg) == {"path": "/from/plistenv.yaml", "basis": "2:plist_env"}
    cfg.plists = []
    assert pa.resolve_tier_policy(cfg) == {"path": "/from/dotenv.yaml", "basis": "2:dotenv_state"}
    cfg.dotenv_paths = []
    assert pa.resolve_tier_policy(cfg)["basis"] == "3:bundled_live"


def test_min_body_round_trips_assemble():
    from llm_router.proxy import cls_input
    msgs = []
    for i in range(5):
        msgs.append({"role": "user", "content": f"earlier prompt {i} " + "w" * 400})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"reply {i} " + "r" * 900}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]})
    prompt = "final ask " + "p" * 25000
    msgs.append({"role": "user", "content": prompt})
    full = {"messages": msgs, "system": "Primary working directory: /w"}
    want = cls_input.assemble(full)
    pieces = pa.assemble_pieces(msgs)
    got = cls_input.assemble(pa.min_body("/w", pieces["earlier"], pieces["assistant_tail"], pa.cap_prompt(prompt)))
    assert got.context == want.context and got.prompt == want.prompt
    assert len(pieces["earlier"]) == 3


def test_session_messages_merges_one_assistant_message():
    recs = [user(S1, "t1", "hello there"), asst(S1, "t2", "m1", "part one"), asst(S1, "t3", "m1", tool=True),
            tool_result(S1, "t4"), asst(S1, "t5", "m2", "part two"), user(S1, "t6", "next")]
    raw = "".join(json.dumps(r) + "\n" for r in recs).encode()
    seen = [(d["timestamp"], len(m), (n or {}).get("message", {}).get("id")) for d, m, n in pa.session_messages(raw)]
    assert seen[0] == ("t1", 1, "m1")
    assert seen[-1][0] == "t6" and seen[-1][1] == 5        # user, asst(m1 merged), tool_result, asst(m2), user


def test_exclusive_write_never_replaces(tmp_path):
    target = tmp_path / "items.jsonl"
    pa.write_private(target, b"first\n", exclusive=True)
    with pytest.raises(FileExistsError):
        pa.write_private(target, b"second\n", exclusive=True)
    assert target.read_bytes() == b"first\n" and stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["items.jsonl"]


def test_items_file_is_written_exclusively_and_last(world, monkeypatch):
    cfg, _ = world
    calls, real = [], pa.write_private
    monkeypatch.setattr(pa, "write_private", lambda path, data, exclusive=False: (
        calls.append((path.name, exclusive)), real(path, data, exclusive=exclusive))[1])
    pa.attach(cfg)
    assert calls[-1] == ("items.jsonl", True)
    assert all(not ex for name, ex in calls[:-1])


@pytest.mark.parametrize("break_it", ["no_denylist", "bad_overrides", "no_overrides", "no_transcripts", "no_frame"])
def test_refuses_instead_of_burning_the_single_run(world, break_it):
    cfg, tmp = world
    if break_it == "no_denylist":
        cfg.denylist.unlink()
    elif break_it == "bad_overrides":
        cfg.overrides.write_text("not json")
    elif break_it == "no_overrides":
        cfg.overrides.unlink()
    elif break_it == "no_transcripts":
        cfg.claude_root = tmp / "empty_projects"
        cfg.claude_root.mkdir()
    else:
        cfg.corpus.write_text(json.dumps({"i": 0, "text": "nothing matches this line"}) + "\n")
        cfg.pins["corpus"] = sha(cfg.corpus)
    with pytest.raises(pa.Refuse) as exc:
        pa.attach(cfg)
    want = {"no_denylist": "denylist missing", "bad_overrides": "overrides unreadable",
            "no_overrides": "overrides unreadable", "no_transcripts": "no transcript files",
            "no_frame": "matched the corpus"}[break_it]
    assert want in exc.value.msg
    assert not (cfg.run_dir / "items.jsonl").exists()


def test_refuses_a_foreign_llm_router(monkeypatch, tmp_path):
    import llm_router
    monkeypatch.setattr(llm_router, "__file__", str(tmp_path / "site-packages" / "llm_router" / "__init__.py"))
    with pytest.raises(pa.Refuse) as exc:
        pa.llm_router_provenance()
    assert "not" in exc.value.msg and "src" in exc.value.msg


def test_session_kind_uses_the_real_home_not_HOME(world, monkeypatch):
    cfg, tmp = world
    real = pwd.getpwuid(os.getuid()).pw_dir
    sid = "66666666-6666-6666-6666-666666666666"
    proj = tmp / "projects" / "-real-rsi"
    proj.mkdir()
    (proj / f"{sid}.jsonl").write_text(json.dumps(user(sid, "2026-09-15T09:00:00Z", P["res"], cwd=real + "/.rsi/y")) + "\n")
    monkeypatch.setenv("HOME", str(tmp / "scratch_home"))
    cfg.kind_home = None
    counts = pa.attach(cfg)
    assert counts["exclusions"]["kind_research"] == 1 and counts["inputs"]["kind_home_is_real_home"] is True


def test_timestamp_boundary_is_parsed_not_string_compared():
    end = pa.parse_ts(pa.WINDOW_END_UTC)
    assert pa.parse_ts("2026-10-08T20:46:00.500Z") >= end
    assert pa.parse_ts("2026-10-08T20:45:59.999Z") < end
    assert pa.parse_ts("2026-10-08T20:46:00Z") >= end
    assert pa.parse_ts("garbage") is None


def test_served_model_stops_at_the_next_human_prompt():
    recs = [user(S1, "t1", "first ask"), tool_result(S1, "t2"), user(S1, "t3", "second ask"),
            asst(S1, "t4", "m9", "answer to the second ask")]
    raw = "".join(json.dumps(r) + "\n" for r in recs).encode()
    nxt = {d["timestamp"]: n for d, _, n in pa.session_messages(raw)}
    assert nxt["t1"] is None and nxt["t3"]["message"]["id"] == "m9"


def _edge_history(earlier_space: bool, assistant_space: bool, prompt: str = "the final ask for this turn"):
    """Normalised texts that put a space exactly at assemble's cut (D-109 shapes)."""
    from llm_router.proxy import cls_input
    e = cls_input.EARLIER_CHARS
    a = cls_input.ASSISTANT_CHARS
    early = ("x" * (e - 1) + " tailword more words") if earlier_space else ("y" * (e + 40))
    assert (early[:e].endswith(" ")) is earlier_space
    reply = ("lead words " + "z" * 300 + " " + "q" * a) if assistant_space else ("r" * (a + 80))
    assert (reply[-(a + 1)] == " ") is assistant_space
    msgs = [{"role": "user", "content": early},
            {"role": "assistant", "content": [{"type": "text", "text": reply}]},
            {"role": "user", "content": prompt}]
    return msgs


@pytest.mark.parametrize("earlier_space,assistant_space", [(True, False), (False, True), (True, True), (False, False)])
def test_whitespace_at_a_cut_is_not_a_mismatch(earlier_space, assistant_space):
    from llm_router.proxy import cls_input
    from llm_router.proxy import steps
    msgs = _edge_history(earlier_space, assistant_space)
    full = cls_input.assemble({"messages": msgs, "system": "Primary working directory: /w"})
    pieces = pa.assemble_pieces(msgs)
    prompt = pa.cap_prompt(steps.newest_human_text({"messages": msgs}))
    assert pa.rebuild_matches(full, "/w", pieces, prompt)


def test_a_genuine_difference_is_still_a_mismatch():
    from llm_router.proxy import cls_input
    from llm_router.proxy import steps
    msgs = _edge_history(True, True)
    full = cls_input.assemble({"messages": msgs, "system": "Primary working directory: /w"})
    pieces = pa.assemble_pieces(msgs)
    prompt = pa.cap_prompt(steps.newest_human_text({"messages": msgs}))
    bad = {**pieces, "earlier": ["w" + pieces["earlier"][0][1:]]}
    assert bad["earlier"] != pieces["earlier"]
    assert not pa.rebuild_matches(full, "/w", bad, prompt)
    assert not pa.rebuild_matches(full, "/elsewhere", pieces, prompt)
    assert not pa.rebuild_matches(full, "/w", pieces, prompt + " extra")
    harness = [*msgs[:-1], {"role": "user", "content": "<task-notification>job done</task-notification> the ask"}]
    full_h = cls_input.assemble({"messages": harness, "system": "Primary working directory: /w"})
    stored = pa.cap_prompt(steps.newest_human_text({"messages": harness}))
    assert stored != cls_input._human(harness[-1]["content"])       # the views differ: a real mismatch
    assert not pa.rebuild_matches(full_h, "/w", pa.assemble_pieces(harness), stored)


def test_attach_refuses_any_leftover_and_never_reads_the_archive(world):
    cfg, _ = world
    archive = cfg.run_dir.parent / "p17d-run1-void"
    archive.mkdir(parents=True)
    (archive / "items.jsonl").write_text("not json at all\n")
    os.chmod(archive, 0o000)            # any read of the archive would raise
    try:
        cfg.run_dir.mkdir()
        (cfg.run_dir / "stray.txt").write_text("x")
        with pytest.raises(pa.Refuse) as exc:
            pa.attach(cfg)
        assert "not empty" in exc.value.msg
        (cfg.run_dir / "stray.txt").unlink()
        counts = pa.attach(cfg)           # an empty run dir is fine; the archive beside it is untouched
        assert counts["F"]["n"] > 0
    finally:
        os.chmod(archive, 0o700)
    assert (archive / "items.jsonl").read_text() == "not json at all\n"
