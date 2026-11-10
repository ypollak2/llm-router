"""P11: LLM_ROUTER_SUMMARY_GATE (default off): extract a chunk's facts from the map input, pin the ones the
summary drops, and keep a chunk verbatim when its facts cannot be pinned.

Behaviour only, mocked backend, no network. Extraction tests are pure; the integration tests drive the
P9 summarizer through the proxy.
"""

from __future__ import annotations

import copy
import json
import time

import pytest

from llm_router.proxy import local_mode, mr_summary, summary_gate
from tests.test_p9_proxy_mr import SUMMARY, MRBackend, _est, _is_map, _warm  # noqa: F401  (fixtures)
from tests.test_proxy_local_mode import _first_call, _overrides_readable, _post, env  # noqa: F401  (fixtures)


def _facts(*texts: str) -> dict[str, list[str]]:
    f = summary_gate.extract("\n".join(texts))
    return {k: list(v) for k, v in f.items()}


def _miss(text: str, summary: str, pinned: str = "") -> dict[str, int]:
    return summary_gate.counts(summary_gate.missing(summary_gate.extract(text), summary + pinned))


# -- extraction ---------------------------------------------------------------------------------------------

def test_paths_are_extracted_and_negatives_are_not():
    f = _facts("edited src/llm_router/proxy/mr_summary.py and /etc/hosts, see ~/notes/a/b.txt.")
    assert f["path"] == ["src/llm_router/proxy/mr_summary.py", "/etc/hosts", "~/notes/a/b.txt"]
    assert _facts("a plain sentence, 3/4 of the way, and/or maybe")["path"] == []


def test_paths_inside_code_fences_and_tool_use_render_count():
    assert _facts("```bash\ncat src/a/b.py\n```")["path"] == ["src/a/b.py"]
    assert _facts('[tool_use Read] {"file_path": "/tmp/x/y.py"}')["path"] == ["/tmp/x/y.py"]


def test_windows_paths_are_not_recognised():
    assert _facts(r"opened C:\Users\me\proj\main.py")["path"] == []  # documented limit


def test_hashes_tickets_and_ids():
    f = _facts("commit 7189d67c fixed ABC-123 via toolu_01ABCDEFGHIJ")
    assert set(f["path"]) == {"7189d67c", "ABC-123", "toolu_01ABCDEFGHIJ"}
    assert _facts("the word deadbeef, digits 1234567, short abc12")["path"] == []  # needs a digit AND a letter, >=7


def test_standards_are_not_tickets():
    text = "charset=UTF-8 SHA-256 SHA-1 ISO-8601 HTTP-2 GPT-4 X-509 TLS-13 PROJ-42"
    assert _facts(text)["path"] == ["PROJ-42"]
    assert _facts("CVE-2024-1234")["path"] == []


def test_error_lines_including_render_prefixes():
    f = _facts("ok\n[tool_result error] ValueError: bad value 3\n  indented ValueError: not at line start\n"
               "FAILED tests/test_a.py::test_x - assert 1 == 2\nerror[E0308]: mismatched types\nfatal: not a git repository\nno problem")
    assert f["error"] == ["valueerror: bad value 3", "failed tests/test_a.py::test_x - assert 1 == 2",
                          "error[e0308]: mismatched types", "fatal: not a git repository"]


def test_error_facts_keep_their_original_case_for_pinning():
    assert summary_gate.extract("ValueError: Bad Thing")["error"] == {"valueerror: bad thing": "ValueError: Bad Thing"}


def test_traceback_contributes_its_terminating_exception_line_and_duplicates_collapse():
    tb = 'Traceback (most recent call last):\n  File "a.py", line 3, in f\n    g()\nKeyError\n'
    assert _facts(tb)["error"] == ["keyerror"]
    assert _facts(tb + tb)["error"] == ["keyerror"]


def test_pytest_e_prefix_counts():
    assert _facts("E   AssertionError: assert 1 == 2")["error"] == ["e assertionerror: assert 1 == 2"]


def test_nonzero_exit_codes_only():
    assert _facts("[tool_result error] Exit code 1\nboom")["exit"] == ["1"]
    assert _facts("Exit code 0\nfine")["exit"] == []
    assert _facts("process exited with code 137, then exit status 2")["exit"] == ["137", "2"]
    assert _facts("the exit code is mentioned but has no number")["exit"] == []
    assert _facts("Exit code 3\nExit code 3")["exit"] == ["3"]


def test_test_names():
    f = _facts("tests/test_a.py::TestX::test_one[param-1] PASSED\n____ test_two ____\nFAILED x - test_three broke\nuse test_four in prose")
    assert f["test"] == ["tests/test_a.py::TestX::test_one[param-1]", "test_two", "test_three"]
    assert _facts("a test_helper mention in prose only")["test"] == []


def test_system_reminders_are_not_scanned_and_an_unclosed_one_is_kept():
    assert _facts("<system-reminder>see /a/b/c.py</system-reminder> and /d/e/f.py")["path"] == ["/d/e/f.py"]
    assert _facts("<system-reminder>see /a/b/c.py")["path"] == ["/a/b/c.py"]


# -- the check ----------------------------------------------------------------------------------------------

CHUNK = "Exit code 2\nsrc/a/b.py\nValueError: bad thing\nFAILED tests/test_q.py::test_z"


def test_a_complete_summary_misses_nothing():
    assert _miss(CHUNK, "ran src/a/b.py, got ValueError: bad thing, exit 2; FAILED tests/test_q.py::test_z") == dict.fromkeys(summary_gate.KINDS, 0)


def test_missing_facts_are_counted_per_kind():
    assert _miss(CHUNK, "src/a/b.py tests/test_q.py::test_z") == {"path": 0, "error": 2, "exit": 1, "test": 0}


def test_exit_code_needs_a_whole_exit_word_and_its_number():
    assert _miss("Exit code 1", "see src/foo.py line 1")["exit"] == 1  # "rc" inside "src" is not an exit word
    assert _miss("Exit code 1", "barcode 1, codex 1")["exit"] == 1
    assert _miss("Exit code 12", "exit code 1 only")["exit"] == 1
    assert _miss("Exit code 12", "rc=12")["exit"] == 0


def test_a_longer_or_truncated_path_does_not_satisfy_a_fact():
    assert _miss("/a/b", "/a/bc")["path"] == 1
    assert _miss("/a/b", "/a/b.py")["path"] == 1
    assert _miss("/a/b", "x/a/b")["path"] == 1
    assert _miss("/a/b", "see /a/b. Then")["path"] == 0
    assert _miss("test_one failed\nFAILED x - test_one", "test_one_more")["test"] == 1


def test_pin_block_satisfies_every_missing_fact_verbatim():
    facts = summary_gate.extract(CHUNK)
    miss = summary_gate.missing(facts, "nothing useful")
    block = summary_gate.pin_block(facts, miss)
    assert block.startswith("\npinned:\n") and "ValueError: bad thing" in block and "exit code 2" in block
    assert not any(summary_gate.missing(facts, "nothing useful" + block).values())
    assert summary_gate.pin_block(facts, summary_gate.missing(facts, "")) != ""
    assert summary_gate.pin_block(facts, {}) == ""


def test_whitespace_normalised_and_errors_case_insensitive():
    assert _miss("ValueError:   bad\n thing", "valueerror: bad thing")["error"] == 0


def test_budget(monkeypatch):
    facts = summary_gate.extract(CHUNK)
    assert not summary_gate.over_budget(facts)
    monkeypatch.setenv("LLM_ROUTER_PIN_BUDGET_CHARS", "20")
    assert summary_gate.over_budget(facts)
    monkeypatch.setenv("LLM_ROUTER_PIN_BUDGET_CHARS", "junk")
    assert summary_gate.pin_budget() == summary_gate.DEFAULT_PIN_BUDGET_CHARS == 800


def test_flag_default_off_and_on(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_SUMMARY_GATE", raising=False)
    assert not summary_gate.enabled()
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_GATE", "1")
    assert summary_gate.enabled()


def test_extractive_and_the_gate_share_one_path_source():
    sample = "a/b/c.py /x/y/z ~/q/r.txt 7189d67c ABC-123"
    assert [m.group(0) for m in mr_summary._PATH.finditer(sample)] == ["a/b/c.py", "/x/y/z", "~/q/r.txt"]
    assert mr_summary._PATH.pattern == summary_gate.PATH_ABS_SRC + "|" + summary_gate.PATH_REL_SRC


# -- linear time --------------------------------------------------------------------------------------------

@pytest.mark.timing
def test_extraction_is_linear_on_adversarial_text():
    n = 200_000
    cases = ["x" * n, "0123abcd" * (n // 8), "a/b/" * (n // 4), ("var a=function(){return b.c.d(e)};" * 6000)[:n],
             "." * n, "/a" * (n // 2), "_" * n, "Exit code" + " " * n, "test_" * (n // 5), "E " + "x" * n]
    t0 = time.perf_counter()
    for c in cases:
        summary_gate.extract(c)
    assert time.perf_counter() - t0 < 2.0 * len(cases) / 4  # the old _NODE_ID needed ~5 s for ONE 50k-char run
    t0 = time.perf_counter()
    for c in cases[:4]:
        summary_gate.extract(c)
    assert time.perf_counter() - t0 < 0.5


# -- through the proxy --------------------------------------------------------------------------------------

def _session(pairs: int = 12, heavy: bool = True) -> dict:
    """Every round trip names a distinct path and its result carries an error, an exit code and a failing test."""
    body = _first_call()
    msgs = [body["messages"][0]]
    for i in range(pairs):
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"toolu_{i}", "name": "Bash", "input": {"command": f"pytest src/pkg/mod{i}.py"}}]})
        head = f"Exit code 1\nValueError: boom {i}\nFAILED tests/test_m{i}.py::test_case{i} - assert 0\n" if heavy else ""
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": head + ("line %d of output\n" % i) * 400}]})
    body["messages"] = msgs
    return body


def _gate_env(monkeypatch, threshold="20000"):
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_GATE", "1")
    monkeypatch.setenv("LLM_ROUTER_MR_CLOSE_THRESHOLD", threshold)  # two chunks planned, one is enough for the cap


def _summary_texts(sent: dict) -> str:
    return "\n".join(b["text"] for b in sent["messages"][0]["content"] if b.get("type") == "text" and "<chunk-summary" in b["text"])


async def test_tool_heavy_chunks_are_served_with_every_fact_in_summary_or_pinned(env, monkeypatch):  # noqa: F811
    _gate_env(monkeypatch, "36000")
    body = _session(20)
    backend = MRBackend()  # the map reply carries no fact at all
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    mr = env.rows()[-1]["mr_summary"]
    assert env.up.requests == [] and mr["outcome"] == "summarized"
    assert mr["gate_checked"] == len(backend.maps) == mr["chunks"] >= 1 and mr["gate_failed"] == 0 and mr["gate_skipped"] == 0
    assert mr["gate_missing_path"] > 0 and mr["gate_missing_error"] > 0 and mr["gate_missing_exit"] > 0 and mr["gate_missing_test"] > 0
    sent = backend.lives[-1]
    served = _summary_texts(sent)
    checked = 0
    for i in range(mr["chunks"] * 3):  # the first chunks cover pairs 0.. ; every closed pair's facts are in the wrapper
        if "mod%d.py" % i not in json.dumps(sent["messages"][1:]):
            assert "src/pkg/mod%d.py" % i in served and "ValueError: boom %d" % i in served
            assert "tests/test_m%d.py::test_case%d" % (i, i) in served
            checked += 1
    assert checked >= 3
    assert "exit code 1" in served and "pinned:" in served
    assert _est(sent) <= local_mode.prompt_cap_tokens()


async def test_a_chunk_over_the_pin_budget_is_skipped_without_a_map_call(env, monkeypatch):  # noqa: F811
    _gate_env(monkeypatch, "36000")
    monkeypatch.setenv("LLM_ROUTER_PIN_BUDGET_CHARS", "100")
    body = _session(20)
    backend = MRBackend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    mr = env.rows()[-1]["mr_summary"]
    assert mr["outcome"] == "gate_skipped" and mr["gate_skipped"] == 1 and mr["gate_checked"] == 1 and mr["map_calls"] == 0
    assert backend.maps == [] and [b for b in backend.lives if len(b["messages"]) > 2] == []
    (req,) = env.up.requests
    assert json.loads(req.content)["messages"] == body["messages"]  # Claude gets the full original history


async def test_later_skipped_chunk_stays_verbatim_and_the_wrapper_is_renumbered(env, monkeypatch):  # noqa: F811
    _gate_env(monkeypatch)
    body = _session(12)
    # chunk 1 is light (no facts besides one path), chunk 2 is heavy: the budget sits between them
    for i in range(3):
        body["messages"][2 + 2 * i]["content"][0]["content"] = ("line %d of output\n" % i) * 400
    monkeypatch.setenv("LLM_ROUTER_PIN_BUDGET_CHARS", "200")
    backend = MRBackend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    mr = env.rows()[-1]["mr_summary"]
    assert mr["outcome"] == "summarized" and mr["chunks"] == 1 and mr["gate_skipped"] == 1 and mr["gate_checked"] == 2
    sent = backend.lives[-1]
    assert "n=1 of 1" in _summary_texts(sent)
    kept = json.dumps(sent["messages"][1:])
    assert "mod0.py" not in kept and "mod3.py" in kept and "boom 3" in kept  # the skipped chunk is verbatim
    assert len(backend.maps) == 1


async def test_facts_in_the_cut_part_of_a_block_are_not_required():
    big = "head /a/seen/one\n" + "filler line\n" * 3000 + "/a/never/shown\n" + "filler line\n" * 3000 + "tail /a/seen/two\n"
    msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": big}]}]
    seen = summary_gate.extract(mr_summary.slice_text(msgs))["path"]
    assert "/a/seen/one" in seen and "/a/seen/two" in seen and "/a/never/shown" not in seen


async def test_ledger_has_counts_only_never_fact_text(env, monkeypatch):  # noqa: F811
    _gate_env(monkeypatch, "36000")
    backend = MRBackend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, _session(20))
    row = env.rows()[-1]["mr_summary"]
    gate = {k: v for k, v in row.items() if k.startswith("gate_")}
    assert set(gate) == {"gate_checked", "gate_skipped", "gate_failed"} | {"gate_missing_" + k for k in summary_gate.KINDS}
    assert all(type(v) is int for v in gate.values())
    dumped = json.dumps(env.rows()[-1])
    assert "src/pkg/mod" not in dumped and "boom" not in dumped and "line 0 of output" not in dumped


async def test_flag_off_carries_no_gate_fields(env, monkeypatch):  # noqa: F811
    monkeypatch.delenv("LLM_ROUTER_SUMMARY_GATE", raising=False)
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    backend = MRBackend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, _session(20))
    mr = env.rows()[-1]["mr_summary"]
    assert mr["outcome"] == "summarized" and not [k for k in mr if k.startswith("gate_")]
    assert "pinned:" not in json.dumps(backend.lives[-1])


async def test_gate_without_proxy_mr_does_nothing(env, monkeypatch):  # noqa: F811
    monkeypatch.delenv("LLM_ROUTER_PROXY_MR", raising=False)
    monkeypatch.setenv("LLM_ROUTER_SUMMARY_GATE", "1")
    backend = MRBackend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, _session(20))
    assert backend.maps == [] and "mr_summary" not in env.rows()[-1]
