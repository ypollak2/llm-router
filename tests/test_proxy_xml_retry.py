"""XMLRETRY-1: one same-step retry when Ollama cannot parse the model's tool-call XML.

Evidence: O2 v2 pilot 1 (RUN-pilot1-20261123T134423Z/proxy_calls.jsonl): 3 of 3 local runs had exactly one
fallback with backend_error_kind=xml_parse (2 'unexpected EOF', 1 'closed by'); every other step was served.
Backends are fakes; no network.
"""
import pytest

from tests.test_proxy import _call, _ollama, _post, _req, _rows
from tests.test_proxy_backend_health import _app, policy  # noqa: F401
from llm_router.proxy.translate import from_ollama

XML_ERR = "ollama error: XML syntax error on line 60: unexpected EOF"
GOOD = _ollama("Running.", [_call("Bash", {"command": "ls"})])


class ScriptedBackend:
    """Returns the scripted errors in order, then a good reply."""

    def __init__(self, errs, delay=0.0):
        self.errs, self.calls, self.delay = list(errs), 0, delay

    async def complete(self, body, timeout_s):
        import asyncio
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.errs:
            return None, self.errs.pop(0), {"first_token_s": 0.1}
        m, err = from_ollama(GOOD, body)
        return m, err, {"first_token_s": 0.1}

    async def probe(self, timeout_s):
        return True, "ok"


@pytest.mark.usefixtures("policy")
async def test_one_xml_parse_failure_is_retried_and_served(tmp_path):
    b = ScriptedBackend([XML_ERR])
    await _post(_app(tmp_path, b), _req())
    row = _rows(tmp_path)[-1]
    assert b.calls == 2 and row["decision"] == "served" and row["xml_retry"] == 1
    assert "backend_error_kind" not in row


@pytest.mark.usefixtures("policy")
async def test_two_xml_parse_failures_fall_back_with_the_kind(tmp_path):
    b = ScriptedBackend([XML_ERR, XML_ERR])
    await _post(_app(tmp_path, b), _req())
    row = _rows(tmp_path)[-1]
    assert b.calls == 2 and row["decision"] == "fallback" and row["reason"] == "validation"
    assert row["backend_error_kind"] == "xml_parse" and row["xml_retry"] == 1


@pytest.mark.usefixtures("policy")
@pytest.mark.parametrize("err", ["empty response", "truncated at num_predict", "unknown tool 'x'",
                                 "ollama error: decode() failed: Compute error."])
async def test_other_failures_are_not_retried(tmp_path, err):
    b = ScriptedBackend([err])
    await _post(_app(tmp_path, b), _req())
    row = _rows(tmp_path)[-1]
    assert b.calls == 1 and "xml_retry" not in row and row["decision"] == "fallback"


@pytest.mark.usefixtures("policy")
async def test_no_retry_when_the_budget_cannot_cover_a_second_try(tmp_path):
    b = ScriptedBackend([XML_ERR], delay=0.3)
    await _post(_app(tmp_path, b, step_budget_s=0.5), _req())
    row = _rows(tmp_path)[-1]
    assert b.calls == 1 and "xml_retry" not in row and row["decision"] == "fallback"
    assert row["backend_error_kind"] == "xml_parse"


class OptsBackend(ScriptedBackend):
    def __init__(self, errs):
        super().__init__(errs)
        self.bodies = []

    async def complete(self, body, timeout_s):
        self.bodies.append(body)
        return await super().complete(body, timeout_s)


@pytest.mark.usefixtures("policy")
async def test_retry_changes_sampling_and_first_call_is_unchanged(tmp_path):
    from llm_router.proxy.translate import to_ollama
    b = OptsBackend([XML_ERR])
    await _post(_app(tmp_path, b), _req())
    first, second = b.bodies
    assert "seed" not in first and "temperature" not in first
    o1 = to_ollama(first, "m", num_ctx=1000)["options"]
    o2 = to_ollama(second, "m", num_ctx=1000)["options"]
    assert "seed" not in o1 and "temperature" not in o1
    assert o2["temperature"] == 0.2 and o2["seed"] == 1729 and o1 != o2
    assert _rows(tmp_path)[-1]["xml_retry_first_s"] >= 0


@pytest.mark.usefixtures("policy")
@pytest.mark.parametrize("val", ["0", "off", "false"])
async def test_kill_flag_disables_the_retry(tmp_path, monkeypatch, val):
    monkeypatch.setenv("LLM_ROUTER_XML_RETRY", val)
    b = ScriptedBackend([XML_ERR])
    await _post(_app(tmp_path, b), _req())
    row = _rows(tmp_path)[-1]
    assert b.calls == 1 and "xml_retry" not in row and row["backend_error_kind"] == "xml_parse"
