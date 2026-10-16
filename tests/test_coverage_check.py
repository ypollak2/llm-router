"""P2.2-A: scripts/coverage_check.py on fixtures built in tmp.

Population, classes and bar are the ones PREREG-P22A will measure: one call = one assistant
message id
passed = a ledger row with tier_reason / reason set or decision served; every
other call must be explained
per-session cap 40; Wilson interval always printed.
No real transcript, ledger or HOME is read.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import coverage_check as cc  # noqa: E402

SWITCH = "2026-10-19T12:00:00Z"
SINCE = "2026-10-20T00:00:00Z"
UNTIL = "2026-10-21T00:00:00Z"
T0 = datetime(2026, 10, 20, 6, 0, 0, tzinfo=timezone.utc)
SECRET = "TOPSECRET-PROMPT-TEXT-9f31"


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class World:
    def __init__(self, root: Path):
        self.root = root
        self.projects = root / "projects"
        self.state = root / "state"
        self.state.mkdir()
        self.ledger_rows: list[dict] = [
            # one old row, so the ledger provably reaches back before the window opens
            {"ts": (T0 - timedelta(days=3)).timestamp(), "msg_id": "msg_old", "session_id": "old", "reason": "x"}]
        self.skip_rows: list[dict] = []
        self.failopen_rows: list[dict] = []
        self.counter = 0

    def call(self, session: str, *, offset_s: float = 0, entrypoint: str | None = "cli", sidechain: bool = False,
             synthetic: bool = False, ts: datetime | None = None, no_ts: bool = False, text: str = SECRET) -> str:
        self.counter += 1
        mid = f"msg_{session}_{self.counter:04d}"
        when = ts or (T0 + timedelta(seconds=offset_s))
        entry = {"type": "assistant", "sessionId": session, "isSidechain": False,
                 "message": {"id": mid, "model": "<synthetic>" if synthetic else "claude-opus-5-5",
                             "content": [{"type": "text", "text": text}]}}
        if not no_ts:
            entry["timestamp"] = iso(when)
        if entrypoint is not None:
            entry["entrypoint"] = entrypoint
        if sidechain:
            path = self.projects / "proj" / session / "subagents" / "agent-1.jsonl"
        else:
            path = self.projects / "proj" / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        self._last = (mid, when)
        return mid

    def row(self, mid: str, session: str, **fields) -> None:
        base = {"ts": self._last[1].timestamp(), "msg_id": mid, "session_id": session, "decision": "forwarded",
                "session_kind": "organic"}
        base.update(fields)
        self.ledger_rows.append(base)

    def passed(self, session: str, **kw) -> str:
        mid = self.call(session, **kw)
        self.row(mid, session, tier_reason="policy")
        return mid

    def flush(self) -> None:
        (self.state / "proxy_calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.ledger_rows))
        if self.skip_rows:
            (self.state / "skips.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.skip_rows))
        if self.failopen_rows:
            (self.state / "fail_open.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.failopen_rows))

    def argv(self, *extra: str, since: str = SINCE, until: str = UNTIL, switch: bool = True) -> list[str]:
        switch_args: list[str] = []
        if switch:
            switch_file = self.root / "default-switch.json"
            switch_file.write_text(json.dumps({"ts": SWITCH}))
            switch_args = ["--switch-file", str(switch_file)]
        return ["--since", since, "--until", until, "--projects-dir", str(self.projects),
                "--ledger", str(self.state / "proxy_calls.jsonl"), "--skips", str(self.state / "skips.jsonl"),
                "--failopen", str(self.state / "fail_open.jsonl"), *switch_args, *extra]

    def run(self, capsys, *extra: str, **kw):
        self.flush()
        capsys.readouterr()
        rc = cc.run(self.argv(*extra, **kw))
        return rc, capsys.readouterr()

    def result(self, tmp_path, capsys, *extra, **kw):
        out = tmp_path / "result.json"
        rc, cap = self.run(capsys, "--json", str(out), *extra, **kw)
        return rc, json.loads(out.read_text()), cap


@pytest.fixture
def w(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("CLAUDE_PROJECTS_DIR", raising=False)
    return World(tmp_path)


def big(w: World, sessions: int = 6, per: int = 40) -> None:
    for s in range(sessions):
        for i in range(per):
            w.passed(f"sess{s}", offset_s=i)


# ── exact counts ─────────────────────────────────────────────────────────────

def test_exact_class_counts_on_three_sessions(w, tmp_path, capsys):
    # s1: three passed (tier_reason, reason, served) + one no_decision row
    m = w.call("s1", offset_s=0)
    w.row(m, "s1", tier_reason="policy")
    m = w.call("s1", offset_s=10)
    w.row(m, "s1", reason="routing_off")
    m = w.call("s1", offset_s=20)
    w.row(m, "s1", decision="served")
    m = w.call("s1", offset_s=30)
    w.row(m, "s1")                       # no_decision
    # s2: sidechain passed; a call BEFORE the session-level skip (unexplained); two calls after it
    m = w.call("s2", offset_s=100, sidechain=True)
    w.row(m, "s2", tier_reason="policy")
    w.call("s2", offset_s=10)
    w.call("s2", offset_s=3000)
    w.call("s2", offset_s=20000)
    w.skip_rows.append({"ts": iso(T0 + timedelta(seconds=2950)), "session_id": "s2", "door": "agent-route",
                        "reason": "headless"})
    # s3: headless; one call near a proxy_down fail-open row, one far from everything
    w.call("s3", offset_s=6000, entrypoint="sdk-cli")
    w.failopen_rows.append({"c": "proxy_down", "ts": (T0 + timedelta(seconds=6050)).timestamp()})
    w.call("s3", offset_s=9000, entrypoint="sdk-cli")
    # an orphan: a ledger row in the window whose message is in no transcript
    w.ledger_rows.append({"ts": T0.timestamp() + 5, "msg_id": "msg_orphan", "session_id": "s1", "reason": "x"})
    rc, res, cap = w.result(tmp_path, capsys)
    assert res["classes"] == {"passed": 4, "no_decision": 1, "skip_explained": 2, "failopen_explained": 1,
                              "unexplained": 2, "no_decision_explained": 0}
    assert res["n"] == 10 and res["passed"] == 4 and res["coverage"] == 0.4
    assert res["wilson95"] == cc.wilson(4, 10)
    assert res["orphan_ledger_rows"] == 1
    assert res["strata"]["headless"]["n"] == 2 and res["strata"]["interactive"]["n"] == 8
    assert res["strata"]["sidechain"]["n"] == 1 and res["strata"]["main"]["n"] == 9
    assert res["unexplained_breakdown"] == {"interactive/main": 2, "headless/main": 1}  # no_decision + 2 unexplained
    assert res["verdict"] == "NOT INFORMATIVE" and rc == 1
    assert res["n_sessions"] == 3


def test_all_calls_passing_with_a_valid_bar_is_a_pass(w, tmp_path, capsys):
    big(w)
    rc, res, cap = w.result(tmp_path, capsys)
    assert (rc, res["verdict"]) == (0, "PASS")
    assert res["n"] == 240 and res["coverage"] == 1.0 and res["wilson95"][0] > 0.98
    assert res["clauses"]["A"] and res["clauses"]["B"] and res["clauses"]["C"]
    assert res["preregistered_params"] is True
    assert "verdict: PASS" in cap.out and "Wilson95" in cap.out


def test_two_explained_failures_in_200_still_pass_but_three_fail(w, tmp_path, capsys):
    for s in range(5):
        for i in range(40):
            if s == 0 and i < 2:
                w.call(f"sess{s}", offset_s=i)   # no row: explained below
            else:
                w.passed(f"sess{s}", offset_s=i)
    w.skip_rows.append({"ts": iso(T0), "session_id": "sess0", "door": "agent-route", "reason": "headless"})
    rc, res, _ = w.result(tmp_path, capsys)
    assert res["n"] == 200 and res["passed"] == 198 and res["classes"]["skip_explained"] == 2
    assert (rc, res["verdict"]) == (0, "PASS") and res["coverage"] == 0.99


def test_three_failures_in_200_fail_the_bar(w, tmp_path, capsys):
    for s in range(5):
        for i in range(40):
            if s == 0 and i < 3:
                w.call(f"sess{s}", offset_s=i)
            else:
                w.passed(f"sess{s}", offset_s=i)
    w.skip_rows.append({"ts": iso(T0), "session_id": "sess0", "door": "agent-route", "reason": "headless"})
    rc, res, _ = w.result(tmp_path, capsys)
    assert (rc, res["verdict"]) == (1, "FAIL") and res["clauses"]["A"] is False and res["clauses"]["B"] is True


def test_one_unexplained_call_fails_even_when_coverage_is_above_the_bar(w, tmp_path, capsys):
    big(w, sessions=6, per=40)
    w.call("sess0", offset_s=900)  # no row, no skip: 1 unexplained in 241 calls (--cap 0 keeps all)
    rc, res, _ = w.result(tmp_path, capsys, "--cap", "0")
    assert res["classes"]["unexplained"] == 1 and res["coverage"] >= 0.99
    assert (rc, res["verdict"]) == (1, "FAIL") and res["clauses"]["B"] is False


def test_a_no_decision_call_needs_an_explanation_too(w, tmp_path, capsys):
    big(w)
    m = w.call("sess1", offset_s=700)
    w.row(m, "sess1")  # row exists, nothing decided
    rc, res, _ = w.result(tmp_path, capsys, "--cap", "0")
    assert res["classes"]["no_decision"] == 1 and res["clauses"]["B"] is False and rc == 1
    w.skip_rows.append({"ts": iso(T0 + timedelta(seconds=600)), "session_id": "sess1", "door": "agent-route",
                        "reason": "headless"})
    rc, res, _ = w.result(tmp_path, capsys, "--cap", "0")
    assert res["classes"]["no_decision"] == 1 and res["classes"]["no_decision_explained"] == 1
    assert res["clauses"]["B"] is True and rc == 0


# ── negative controls ────────────────────────────────────────────────────────

def test_a_ledger_of_only_no_decision_rows_must_not_pass(w, tmp_path, capsys):
    for s in range(6):
        for i in range(40):
            m = w.call(f"sess{s}", offset_s=i)
            w.row(m, f"sess{s}")  # a proxy row, but no tier_reason, no reason, not served
    rc, res, _ = w.result(tmp_path, capsys)
    assert res["n"] == 240 and res["passed"] == 0 and res["coverage"] == 0.0
    assert res["classes"]["no_decision"] == 240
    assert (rc, res["verdict"]) == (1, "FAIL"), "calls happened and none passed: FAIL, not NOT INFORMATIVE"


def test_an_empty_transcript_set_is_not_informative_never_a_pass(w, tmp_path, capsys):
    rc, res, _ = w.result(tmp_path, capsys)
    assert (rc, res["verdict"]) == (1, "NOT INFORMATIVE") and "no_transcripts" in res["not_informative_reasons"]
    assert res["n"] == 0 and res["coverage"] is None and res["wilson95"] is None


def test_a_missing_projects_dir_is_not_informative_too(w, tmp_path, capsys):
    w.flush()
    rc = cc.run(["--since", SINCE, "--until", UNTIL, "--projects-dir", str(tmp_path / "nowhere"),
                 "--ledger", str(w.state / "proxy_calls.jsonl")])
    assert rc == 1


def test_one_dominant_session_is_not_informative(w, tmp_path, capsys):
    for i in range(150):
        w.passed("whale", offset_s=i)
    for s in range(5):
        for i in range(10):
            w.passed(f"small{s}", offset_s=i)
    rc, res, _ = w.result(tmp_path, capsys, "--cap", "0", "--min-n", "10")
    assert res["largest_session_share"] == 0.75
    assert res["verdict"] == "NOT INFORMATIVE" and "largest_session_share_above_0.60" in res["not_informative_reasons"]
    assert rc == 1 and res["preregistered_params"] is False


def test_the_cap_makes_a_dominant_session_informative_but_uncapped_share_still_counts(w, tmp_path, capsys):
    for i in range(150):
        w.passed("whale", offset_s=i)
    for s in range(5):
        for i in range(40):
            w.passed(f"small{s}", offset_s=i)
    rc, res, _ = w.result(tmp_path, capsys)
    assert res["n"] == 240 and res["uncapped"]["n"] == 350
    assert res["largest_session_share"] == round(40 / 240, 4)
    assert res["uncapped"]["largest_session_share"] == round(150 / 350, 4) and rc == 0


def test_below_n_and_below_five_sessions_are_not_informative(w, tmp_path, capsys):
    for s in range(4):
        for i in range(40):
            w.passed(f"sess{s}", offset_s=i)
    rc, res, _ = w.result(tmp_path, capsys)
    assert res["n"] == 160
    assert any(r.startswith("n_below_min_n") for r in res["not_informative_reasons"])
    assert any(r.startswith("sessions_below_5") for r in res["not_informative_reasons"])
    assert rc == 1


def test_cap_is_the_seeded_lowest_hashes_and_outcome_blind(w, tmp_path, capsys):
    mids = [w.passed("long", offset_s=i) for i in range(100)]
    rc, res, _ = w.result(tmp_path, capsys)
    assert res["uncapped"]["n"] == 100 and res["n"] == 40
    want = sorted(mids, key=lambda m: hashlib.sha256(("p22a-v1:" + m).encode()).hexdigest())[:40]
    kept = {c["msg_id"] for c in cc.cap_calls(
        [{"session": "long", "msg_id": m} for m in reversed(mids)], 40)}
    assert kept == set(want), "the pick does not depend on file order"
    late = {m for m in mids[60:]}
    assert kept & late, "a late-session call can be sampled, so a late bypass is not hidden"


def test_ledger_that_starts_after_the_window_is_a_rotation_not_informative(w, tmp_path, capsys):
    big(w)
    w.ledger_rows = [r for r in w.ledger_rows if r["msg_id"] != "msg_old"]
    rc, res, _ = w.result(tmp_path, capsys)
    assert "ledger_rotated_after_window_start" in res["not_informative_reasons"] and rc == 1


def test_missing_ledger_is_not_informative(w, tmp_path, capsys):
    big(w)
    w.flush()
    (w.state / "proxy_calls.jsonl").unlink()
    rc = cc.run(w.argv("--json", str(tmp_path / "r.json")))
    res = json.loads((tmp_path / "r.json").read_text())
    assert rc == 1 and "ledger_unreadable" in res["not_informative_reasons"]


def test_more_than_five_percent_missing_timestamps_is_not_informative(w, tmp_path, capsys):
    big(w)
    for _ in range(20):
        w.call("sess0", no_ts=True)
    rc, res, _ = w.result(tmp_path, capsys)
    assert "missing_timestamps_above_5pct" in res["not_informative_reasons"] and res["excluded"]["missing_timestamp"] == 20


def test_unknown_entrypoint_is_a_stratum_not_a_failure_until_it_is_20_percent(w, tmp_path, capsys):
    big(w)
    for i in range(10):
        w.passed("sess0", offset_s=500 + i, entrypoint=None)
    rc, res, _ = w.result(tmp_path, capsys, "--cap", "0")
    assert res["strata"]["unknown"]["n"] == 10 and res["verdict"] == "PASS"
    for s in range(6):
        for i in range(40):
            w.passed(f"unk{s}", offset_s=i, entrypoint=None)
    rc, res, _ = w.result(tmp_path, capsys, "--cap", "0")
    assert "unknown_entrypoint_above_20pct" in res["not_informative_reasons"]


# ── skips ledger ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("row", [
    {"session_id": "sess0", "door": "proxy", "reason": ""},
    {"session_id": "sess0", "door": "proxy", "reason": "made_up"},
    {"session_id": "sess0", "door": "elsewhere", "reason": "tiers_off"},
])
def test_an_invalid_skips_row_fails_clause_c(w, tmp_path, capsys, row):
    big(w)
    w.skip_rows.append({"ts": iso(T0 + timedelta(hours=1)), **row})
    rc, res, _ = w.result(tmp_path, capsys)
    assert res["skips"]["invalid"] == 1 and res["clauses"]["C"] is False
    assert (rc, res["verdict"]) == (1, "FAIL")


def test_reasonless_skips_are_counted_separately(w, tmp_path, capsys):
    big(w)
    w.skip_rows.append({"ts": iso(T0), "session_id": "sess0", "door": "proxy", "reason": " "})
    _, res, _ = w.result(tmp_path, capsys)
    assert res["skips"]["reasonless"] == 1 and res["skips"]["invalid"] == 1


def test_a_session_level_skip_explains_every_later_call_of_its_session_only(w, tmp_path, capsys):
    w.skip_rows.append({"ts": iso(T0), "session_id": "bo", "door": "session-start", "reason": "base_url_override"})
    w.call("bo", offset_s=5)
    w.call("bo", offset_s=50000)
    w.call("other", offset_s=50000)
    _, res, _ = w.result(tmp_path, capsys)
    assert res["classes"]["skip_explained"] == 2 and res["classes"]["unexplained"] == 1
    assert res["skips"]["reasons"] == {"base_url_override": 1}


@pytest.mark.parametrize("reason", ["tiers_off", "routing_off"])
def test_a_proxy_door_skip_row_explains_nothing_even_at_the_same_instant(w, tmp_path, capsys, reason):
    """D-150: a per-call proxy skip row would mark any call of its session within +-120 s as
    explained and hide a proxy BYPASS. Only session-level skips and fail_open rows explain."""
    w.call("s", offset_s=0)
    w.skip_rows.append({"ts": iso(T0), "session_id": "s", "door": "proxy", "reason": reason})
    _, res, _ = w.result(tmp_path, capsys)
    assert res["classes"]["skip_explained"] == 0 and res["classes"]["unexplained"] == 1
    assert res["skips"]["invalid"] == 0, "the proxy door stays in the closed set (structure only)"


def test_a_session_level_skip_does_not_explain_earlier_calls(w, tmp_path, capsys):
    w.call("s", offset_s=0)
    w.skip_rows.append({"ts": iso(T0 + timedelta(seconds=5)), "session_id": "s", "door": "agent-route",
                        "reason": "headless"})
    _, res, _ = w.result(tmp_path, capsys)
    assert res["classes"]["skip_explained"] == 0 and res["classes"]["unexplained"] == 1


def test_script_constants_match_the_writer():
    from llm_router import skips

    assert cc.REASONS == skips.REASONS and cc.DOORS == skips.DOORS


# ── population rules ─────────────────────────────────────────────────────────

def test_exclusion_file_kind_rule_synthetic_window_and_prior_sessions(w, tmp_path, capsys):
    big(w)
    for i in range(5):
        w.passed("listed", offset_s=i)
        w.passed("researchy", offset_s=i)
    m = w.call("researchy", offset_s=99)
    w.row(m, "researchy", tier_reason="p", session_kind="research")
    w.passed("hl", offset_s=1, entrypoint="sdk-cli")
    w.ledger_rows[-1]["session_kind"] = "headless"
    w.call("sess0", offset_s=1, synthetic=True)
    w.passed("early", ts=T0 - timedelta(days=1))          # before the window
    w.passed("late", ts=T0 + timedelta(days=2))           # after the window
    w.passed("spans", ts=datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc))
    w.passed("spans", offset_s=60)  # started before the switch: its environment was read before it
    w.passed("between", ts=datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc))
    w.passed("between", offset_s=60)  # started after the switch, before --since: counts
    excl = tmp_path / "exclude.txt"
    excl.write_text("# executors\nlisted\n")
    rc, res, _ = w.result(tmp_path, capsys, "--exclude-sessions", str(excl))
    assert res["n"] == 242, "6 x 40 + the headless session's call + the session that began after the switch"
    assert res["excluded"]["sessions_listed"] == 1 and res["excluded"]["sessions_by_kind"] == 1
    assert res["excluded"]["sessions_started_before_switch"] == 1
    assert res["excluded"]["synthetic_entries"] == 1
    assert res["strata"]["headless"]["n"] == 1, "a headless session is a stratum, not an exclusion"
    rc, res2, _ = w.result(tmp_path, capsys, "--exclude-sessions", str(excl), "--all-kinds")
    assert res2["n"] == res["n"] + 6 and res2["excluded"]["sessions_by_kind"] == 0


def test_a_session_with_no_evidence_either_way_is_included(w, tmp_path, capsys):
    w.call("ghost", offset_s=3)  # no ledger row at all, no kind
    _, res, _ = w.result(tmp_path, capsys)
    assert res["n"] == 1 and res["classes"]["unexplained"] == 1


def test_headless_stratum_is_reported_and_pooled(w, tmp_path, capsys):
    for i in range(30):
        w.passed("hs", offset_s=i, entrypoint="sdk-ts")
    for i in range(30):
        w.passed("is", offset_s=i, entrypoint="cli")
    _, res, _ = w.result(tmp_path, capsys)
    assert res["strata"]["headless"] == {"n": 30, "passed": 30, "coverage": 1.0, "wilson95": cc.wilson(30, 30)}
    assert res["n"] == 60 and res["passed"] == 60


def test_a_call_with_several_entries_counts_once_and_multi_row_ids_are_counted(w, tmp_path, capsys):
    mid = w.call("s", offset_s=0)
    w.row(mid, "s")  # a no_decision row, then a passing one for the same message
    w.row(mid, "s", tier_reason="policy")
    path = w.projects / "proj" / "s.jsonl"
    with path.open("a") as fh:   # same message id written again (streamed entries)
        fh.write(path.read_text().splitlines()[0] + "\n")
    _, res, _ = w.result(tmp_path, capsys)
    assert res["n"] == 1 and res["classes"]["passed"] == 1 and res["multi_row_msg_ids"] == 1


# ── refusals, privacy, outputs ───────────────────────────────────────────────

@pytest.mark.parametrize("argv", [
    ["--days", "7"], ["--days=7", "--since", SINCE, "--until", UNTIL],
    [], ["--since", SINCE], ["--since", "2026-10-20", "--until", UNTIL],
    ["--since", "2026-10-20T00:00:00", "--until", UNTIL],
    ["--since", UNTIL, "--until", SINCE], ["--since", SINCE, "--until", UNTIL, "--bogus"],
])
def test_refusals_exit_2_and_compute_nothing(w, capsys, argv):
    assert cc.run(argv) == 2
    assert capsys.readouterr().out == ""


def test_days_is_refused_even_alongside_a_valid_window(w, capsys):
    assert cc.run(["--days", "14", "--since", SINCE, "--until", UNTIL]) == 2
    assert "absolute" in capsys.readouterr().err


def test_missing_exclusion_file_is_refused(w, tmp_path, capsys):
    big(w)
    w.flush()
    assert cc.run(w.argv("--exclude-sessions", str(tmp_path / "nope.txt"))) == 2


def test_switch_file_after_since_is_refused_and_before_it_is_accepted(w, tmp_path, capsys):
    big(w)
    sw = tmp_path / "switch.json"
    sw.write_text(json.dumps({"ts": "2026-10-20T00:00:01Z"}))
    w.flush()
    assert cc.run(w.argv("--switch-file", str(sw))) == 2
    sw.write_text(json.dumps({"ts": "2026-10-19T23:00:00Z"}))
    assert cc.run(w.argv("--switch-file", str(sw))) == 0
    sw.write_text("{not json")
    assert cc.run(w.argv("--switch-file", str(sw))) == 2


def test_expect_script_sha_must_match(w, capsys):
    big(w)
    w.flush()
    assert cc.run(w.argv("--expect-script-sha", "0" * 64)) == 2
    sha = hashlib.sha256(Path(cc.__file__).read_bytes()).hexdigest()
    assert cc.run(w.argv("--expect-script-sha", sha)) == 0


def test_no_prompt_text_and_no_full_session_id_reach_the_output(w, tmp_path, capsys):
    long_sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    for s in range(5):
        for i in range(40):
            w.passed(f"{long_sid[:-1]}{s}", offset_s=i)
    rc, res, cap = w.result(tmp_path, capsys)
    blob = cap.out + cap.err + (tmp_path / "result.json").read_text()
    assert SECRET not in blob and long_sid[:-1] not in blob
    assert rc == 0


def test_write_creates_the_private_state_file_kpi_reads(w, tmp_path, capsys):
    big(w)
    rc, _ = w.run(capsys, "--write")
    path = w.state / "coverage_check.json"
    assert rc == 0 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["verdict"] == "PASS"


def test_nothing_is_written_without_write_or_json(w, capsys):
    big(w)
    w.flush()
    before = sorted(p.name for p in w.state.iterdir())
    w.run(capsys)
    assert sorted(p.name for p in w.state.iterdir()) == before


def test_wilson_matches_the_numbers_in_the_brief():
    assert abs(cc.wilson(198, 200)[0] - 0.964) < 0.001
    assert abs(cc.wilson(200, 200)[0] - 0.981) < 0.001
    assert abs(cc.wilson(197, 200)[0] - 0.957) < 0.001
    assert cc.wilson(0, 0) is None


def test_output_names_itself_decision_point_coverage(w, capsys):
    big(w)
    _, cap = w.run(capsys)
    assert "decision-point coverage, not routing coverage" in cap.out


# ── D-150 repairs ────────────────────────────────────────────────────────────

def test_without_a_switch_file_the_run_is_not_informative(w, tmp_path, capsys):
    big(w)
    w.flush()
    out = tmp_path / "r.json"
    rc = cc.run(w.argv("--json", str(out), switch=False))
    res = json.loads(out.read_text())
    assert rc == 1 and res["verdict"] == "NOT INFORMATIVE" and "switch_unknown" in res["not_informative_reasons"]
    assert res["switch"] == {"known": False, "ts": None}
    assert res["coverage"] == 1.0, "the numbers are still computed and printed"


def test_unbuilt_prereg_items_are_listed_in_every_result(w, tmp_path, capsys):
    big(w)
    rc, res, cap = w.result(tmp_path, capsys)
    assert res["clauses"]["not_evaluated"] == ["D", "E", "F", "prereg_sha_check", "allowed_roots_refusal",
                                                "supplement_sessions"]
    assert "prereg_sha_check" in cap.out and res["switch"] == {"known": True, "ts": SWITCH}


def test_routed_share_beside_coverage_all_routed(w, tmp_path, capsys):
    big(w)  # tier_reason "policy" on every row
    rc, res, cap = w.result(tmp_path, capsys)
    rs = res["routed_share"]
    assert (rs["passed"], rs["routed"], rs["routed_share"]) == (240, 240, 1.0)
    assert rs["not_evidence_for_RRTE2"] is False and rc == 0
    assert "routed share: 100.0% of 240 passed" in cap.out and "NOT EVIDENCE" not in cap.out


def test_a_window_of_routing_off_rows_passes_coverage_but_is_not_evidence_for_rrte2(w, tmp_path, capsys):
    for s in range(6):
        for i in range(40):
            m = w.call(f"sess{s}", offset_s=i)
            w.row(m, f"sess{s}", reason="routing_off")
    rc, res, cap = w.result(tmp_path, capsys)
    rs = res["routed_share"]
    assert res["coverage"] == 1.0 and rc == 0, "decision-point coverage counts a recorded reason"
    assert (rs["off"], rs["routed"], rs["off_share"]) == (240, 0, 1.0)
    assert rs["not_evidence_for_RRTE2"] is True and "routing_off/tiers_off" in rs["not_evidence_reason"]
    assert "NOT EVIDENCE FOR R-RTE-2" in cap.out


def test_routed_share_kinds_and_the_90_percent_line(w, tmp_path, capsys):
    rows = ([dict(decision="served")] * 2 + [dict(tier_reason="policy")] * 3 + [dict(tier_reason="advise")] * 2
            + [dict(reason="routing_off")] * 2 + [dict(reason="tiers_off")] + [dict(reason="not_eligible")] * 2)
    for i, fields in enumerate(rows):
        m = w.call("s", offset_s=i)
        w.row(m, "s", **fields)
    _, res, _ = w.result(tmp_path, capsys, "--cap", "0")
    rs = res["routed_share"]
    assert (rs["routed"], rs["advise"], rs["off"], rs["other_reason"]) == (5, 2, 3, 2)
    assert rs["routed_share"] == round(5 / 12, 4) and rs["not_evidence_for_RRTE2"] is False
    for i in range(60):
        m = w.call("s", offset_s=100 + i)
        w.row(m, "s", reason="routing_off")
    _, res, _ = w.result(tmp_path, capsys, "--cap", "0")
    assert res["routed_share"]["off_share"] == round(63 / 72, 4) and res["routed_share"]["not_evidence_for_RRTE2"] is False
    for i in range(60):
        m = w.call("s", offset_s=200 + i)
        w.row(m, "s", reason="routing_off")
    _, res, _ = w.result(tmp_path, capsys, "--cap", "0")
    assert res["routed_share"]["off_share"] >= 0.9 and res["routed_share"]["not_evidence_for_RRTE2"] is True


def test_a_window_of_advise_rows_is_not_evidence_either(w, tmp_path, capsys):
    for s in range(6):
        for i in range(40):
            m = w.call(f"sess{s}", offset_s=i)
            w.row(m, f"sess{s}", tier_reason="advise", authority="advise")
    _, res, _ = w.result(tmp_path, capsys)
    assert res["routed_share"]["advise"] == 240 and res["routed_share"]["not_evidence_for_RRTE2"] is True


def test_the_uncapped_largest_session_rule_alone_makes_the_run_not_informative(w, tmp_path, capsys):
    # capped shares are 40/240; uncapped the whale holds 400/600 = 67%
    for i in range(400):
        w.passed("whale", offset_s=i)
    for s in range(5):
        for i in range(40):
            w.passed(f"small{s}", offset_s=i)
    rc, res, _ = w.result(tmp_path, capsys)
    assert res["largest_session_share"] == round(40 / 240, 4)
    assert res["uncapped"]["largest_session_share"] == round(400 / 600, 4)
    assert (rc, res["verdict"]) == (1, "NOT INFORMATIVE")
    assert "largest_session_share_above_0.60" in res["not_informative_reasons"]
