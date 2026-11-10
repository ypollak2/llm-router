"""P11: LLM_ROUTER_SUMMARY_GATE (default off) keeps a chunk verbatim when its summary drops a fact.

Behaviour only, mocked backend, no network. Extraction tests are pure; the integration tests drive the
P9 summarizer through the proxy.
"""

from __future__ import annotations

import copy
import json

from llm_router.proxy import local_mode, summary_gate
from tests.test_p9_proxy_mr import SUMMARY, MRBackend, _est, _is_map, _warm  # noqa: F401  (fixtures)
from tests.test_proxy_local_mode import _call, _first_call, _ollama, _overrides_readable, _post, env  # noqa: F401  (fixtures)


def _msgs(*texts: str) -> list[dict]:
    return [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": t}]} for t in texts]


def _facts(*texts: str) -> dict[str, list[str]]:
    return summary_gate.extract_facts(_msgs(*texts))


# -- extraction ---------------------------------------------------------------------------------------------

def test_paths_are_extracted_and_negatives_are_not():
    f = _facts("edited src/llm_router/proxy/mr_summary.py and /etc/hosts, see ~/notes/a/b.txt.")
    assert f["path"] == ["src/llm_router/proxy/mr_summary.py", "/etc/hosts", "~/notes/a/b.txt"]
    assert _facts("a plain sentence, 3/4 of the way, and/or maybe")["path"] == []


def test_paths_inside_code_fences_and_tool_use_inputs_count():
    fenced = _facts("```bash\ncat src/a/b.py\n```")
    assert fenced["path"] == ["src/a/b.py"]
    msg = [{"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "Read", "input": {"file_path": "/tmp/x/y.py"}}]}]
    assert summary_gate.extract_facts(msg)["path"] == ["/tmp/x/y.py"]


def test_windows_paths_are_not_recognised():
    assert _facts(r"opened C:\Users\me\proj\main.py")["path"] == []  # documented limit: the prototype has no pattern


def test_ids_hashes_and_tickets_are_in_the_path_kind():
    f = _facts("commit 7189d67c fixed ABC-123 via toolu_01ABCDEFGHIJ")
    assert set(f["path"]) == {"7189d67c", "ABC-123", "toolu_01ABCDEFGHIJ"}
    assert "deadbeef" not in _facts("the word deadbeef")["path"]


def test_error_lines():
    f = _facts("ok\nValueError: bad value 3\n  indented ValueError: not at line start\nFAILED tests/test_a.py::test_x - assert 1 == 2\n"
               "error[E0308]: mismatched types\nfatal: not a git repository\nno problem here")
    assert f["error"] == ["valueerror: bad value 3", "failed tests/test_a.py::test_x - assert 1 == 2",
                          "error[e0308]: mismatched types", "fatal: not a git repository"]


def test_traceback_contributes_its_terminating_exception_line():
    tb = "Traceback (most recent call last):\n  File \"a.py\", line 3, in f\n    g()\nKeyError\n"
    f = _facts(tb)
    assert f["error"] == ["keyerror"]  # a terminator without "Error:" still counts
    assert _facts(tb + tb)["error"] == ["keyerror"]  # duplicates collapse


def test_pytest_e_prefix_counts():
    assert _facts("E   AssertionError: assert 1 == 2")["error"] == ["e assertionerror: assert 1 == 2"]


def test_nonzero_exit_codes_only():
    assert _facts("Exit code 1\nboom")["exit"] == ["1"]
    assert _facts("Exit code 0\nfine")["exit"] == []
    assert _facts("process exited with code 137, then exit status 2")["exit"] == ["137", "2"]
    assert _facts("the exit code is mentioned but has no number")["exit"] == []
    assert _facts("Exit code 3\nExit code 3")["exit"] == ["3"]


def test_test_names():
    f = _facts("tests/test_a.py::TestX::test_one[param-1] PASSED\n____ test_two ____\nFAILED x - test_three broke\nuse test_four in prose")
    assert f["test"] == ["tests/test_a.py::TestX::test_one[param-1]", "test_two", "test_three"]
    assert _facts("a test_helper mention in prose only")["test"] == []


def test_system_reminders_are_not_scanned():
    m = [{"role": "user", "content": [{"type": "text", "text": "<system-reminder>see /a/b/c.py</system-reminder>"}]}]
    assert summary_gate.extract_facts(m)["path"] == []


# -- the gate -----------------------------------------------------------------------------------------------

CHUNK = _msgs("Exit code 2\nsrc/a/b.py\nValueError: bad thing\nFAILED tests/test_q.py::test_z")


def test_a_complete_summary_passes_and_counts_the_facts():
    summ = "ran src/a/b.py, got ValueError: bad thing, exit 2; FAILED tests/test_q.py::test_z"
    r = summary_gate.check(CHUNK, summ)
    assert r.passed and r.missing_total == 0
    assert r.facts == {"path": 2, "error": 2, "exit": 1, "test": 1}


def test_a_summary_missing_one_path_fails_and_adding_it_passes():
    summ = "ValueError: bad thing, exit code 2; FAILED tests/test_q.py::test_z"
    r = summary_gate.check(CHUNK, summ)
    assert not r.passed and r.missing == {"path": 1, "error": 0, "exit": 0, "test": 0}
    assert summary_gate.check(CHUNK, summ + " in src/a/b.py").passed


def test_missing_exit_code_and_error_and_test_are_counted_per_kind():
    r = summary_gate.check(CHUNK, "src/a/b.py tests/test_q.py::test_z")
    assert r.missing == {"path": 0, "error": 2, "exit": 1, "test": 0}


def test_exit_code_needs_its_number_next_to_an_exit_word():
    assert not summary_gate.check(_msgs("Exit code 12"), "exit code 1 only").passed
    assert summary_gate.check(_msgs("Exit code 12"), "rc=12").passed


def test_pinned_list_satisfies_a_fact():
    summ = "ValueError: bad thing, exit 2; FAILED tests/test_q.py::test_z"
    assert not summary_gate.check(CHUNK, summ).passed
    assert summary_gate.check(CHUNK, summ, pinned=["src/a/b.py"]).passed


def test_whitespace_is_normalised_and_errors_are_case_insensitive():
    assert summary_gate.check(_msgs("ValueError:   bad\n thing"), "valueerror: bad thing").passed


def test_empty_chunk_passes_and_result_has_no_text():
    r = summary_gate.check(_msgs("nothing to see"), "")
    assert r.passed and r.facts == {"path": 0, "error": 0, "exit": 0, "test": 0}
    r2 = summary_gate.check(CHUNK, "")
    assert all(isinstance(v, int) for v in list(r2.facts.values()) + list(r2.missing.values()))
    assert "src/a/b.py" not in repr(r2)


def test_flag_default_off_and_on(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_SUMMARY_GATE", raising=False)
    assert not summary_gate.enabled()
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_GATE", "1")
    assert summary_gate.enabled()


# -- through the proxy --------------------------------------------------------------------------------------

def _session(pairs: int = 20) -> dict:
    """Like the P9 session, but every command names a distinct file path, so each chunk carries path facts."""
    body = _first_call()
    msgs = [body["messages"][0]]
    for i in range(pairs):
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"toolu_{i}", "name": "Bash", "input": {"command": f"cat src/pkg/mod{i}.py"}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": ("line %d of output\n" % i) * 400}]})
    body["messages"] = msgs
    return body


class EchoBackend(MRBackend):
    """The first ``good`` map calls echo every path of their slice; later ones give the path-free SUMMARY."""

    def __init__(self, good: int):
        super().__init__()
        self.good = good

    async def complete(self, body, timeout_s):
        if _is_map(body):
            n = len(self.maps)
            if n < self.good:
                paths = summary_gate.PATH_RE.findall(body["messages"][0]["content"].split("<slice>", 1)[1])
                self.map_reply = SUMMARY.replace("none-1", " ".join(dict.fromkeys(paths)))
            else:
                self.map_reply = SUMMARY
        return await super().complete(body, timeout_s)


async def test_gate_on_complete_summaries_behave_like_p9(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_GATE", "1")
    body = _session()
    backend = EchoBackend(good=99)
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    mr = env.rows()[-1]["mr_summary"]
    assert env.up.requests == [] and mr["outcome"] == "summarized"
    assert mr["gate_checked"] == mr["chunks"] >= 1 and mr["gate_failed"] == 0 and "gate_missing" not in mr


async def test_failed_chunk_stays_verbatim_and_a_still_fitting_turn_is_served_local(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_GATE", "1")
    monkeypatch.setenv("LLM_ROUTER_MR_CLOSE_THRESHOLD", "20000")  # two chunks are planned, one is enough for the cap
    body = _session(12)
    backend = EchoBackend(good=1)  # chunk 1 passes, chunk 2 drops its paths
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    row = env.rows()[-1]["mr_summary"]
    assert row["gate_checked"] == 2 and row["gate_failed"] == 1 and row["gate_missing"]["path"] >= 1
    assert len(backend.maps) == 2  # no retry with a longer summary, no later chunk spent
    assert row["outcome"] == "summarized" and row["chunks"] == 1
    sent = backend.lives[-1]
    texts = [b["text"] for b in sent["messages"][0]["content"] if b.get("type") == "text"]
    assert sum("<chunk-summary" in t for t in texts) == 1 and "n=1 of 1" in "".join(texts)  # the wrapper counts what was kept
    kept = json.dumps(sent["messages"][1:])
    assert "mod0.py" not in kept  # chunk 1 (summarized, gate passed) is gone
    assert "mod3.py" in kept and "mod4.py" in kept  # chunk 2 (gate failed) is verbatim
    assert sent["messages"][-1] == body["messages"][-1] and _est(sent) <= local_mode.prompt_cap_tokens()
    assert env.up.requests == []


async def test_all_chunks_failing_follows_the_existing_still_over_fallback(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_GATE", "1")
    body = _session()
    backend = EchoBackend(good=0)
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    row = env.rows()[-1]["mr_summary"]
    assert row["outcome"] == "still_over" and row["gate_checked"] == row["gate_failed"] == 1 and row["chunks"] == 0
    assert len(backend.maps) == 1
    assert [b for b in backend.lives if len(b["messages"]) > 2] == []  # nothing shortened was served
    (req,) = env.up.requests
    assert json.loads(req.content)["messages"] == body["messages"]  # Claude gets the full original history


async def test_ledger_has_counts_only_never_fact_text(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_GATE", "1")
    backend = EchoBackend(good=0)
    app = env.app(backend)
    await _warm(app)
    await _post(app, _session())
    row = env.rows()[-1]["mr_summary"]
    assert set(row["gate_missing"]) == set(summary_gate.KINDS)
    assert all(isinstance(v, int) for v in row["gate_missing"].values())
    dumped = json.dumps(env.rows()[-1])
    assert "src/pkg/mod" not in dumped and "line 0 of output" not in dumped


async def test_flag_off_carries_no_gate_fields_and_proxy_mr_off_ignores_the_gate(env, monkeypatch):  # noqa: F811
    monkeypatch.delenv("LLM_ROUTER_SUMMARY_GATE", raising=False)
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    backend = EchoBackend(good=0)  # path-free summaries would fail a gate; with it off they are used
    app = env.app(backend)
    await _warm(app)
    await _post(app, _session())
    mr = env.rows()[-1]["mr_summary"]
    assert mr["outcome"] == "summarized" and not [k for k in mr if k.startswith("gate_")]

async def test_gate_without_proxy_mr_does_nothing(env, monkeypatch):  # noqa: F811
    monkeypatch.delenv("LLM_ROUTER_PROXY_MR", raising=False)
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_GATE", "1")
    backend = EchoBackend(good=0)
    app = env.app(backend)
    await _warm(app)
    await _post(app, _session())
    assert backend.maps == [] and "mr_summary" not in env.rows()[-1]
