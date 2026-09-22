"""R10 — every completion endpoint refuses what it cannot serve.

H-03 found `/v1/chat/completions` accepting a `tools` array, discarding it
before the handler body ran (the field was not declared, so Pydantic dropped
it), and returning a fluent prose answer with `finish_reason: "stop"`. An
OpenAI-compatible client doing function calling received a well-formed answer
to a question it had not asked, with nothing anywhere to indicate that its tool
definitions had been thrown away.

It was fixed on two endpoints. `/v1/responses`, `/api/chat` and `/api/generate`
kept the defect, because the fix was applied call-site by call-site and nothing
enumerated the endpoints. The remediation plan predicted this exactly: *"a new
endpoint must be added or the test fails — this is precisely how /v1/responses
was missed."*

So this file does not test three endpoints. It DISCOVERS every POST route on
the app and requires each one to be classified: either it accepts prompts and
must refuse unservable capabilities, or it is explicitly listed as not a
completion endpoint. A route added later is unclassified and fails here.

WHY REFUSE RATHER THAN IMPLEMENT (carried over from H-03, still true): the
router returns text. `route_and_call` has no tool-call channel and no backend
in the chain is wired for one. Building that is a feature; half-building it
reproduces the same silent wrongness somewhere new. Refusing tells the client
immediately, in its own protocol, that this gateway cannot serve the request.
"""

from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture(scope="module")
def client():
    from llm_router.gateway import app

    # base_url matters: TestClient defaults to Host "testserver", and
    # `_guard_cross_origin` rejects a non-loopback Host with 403 (CHZ-SEC-04,
    # DNS-rebinding). A 403 would mask whether the capability refusal fired at
    # all, so the client has to look like a legitimate local SDK caller.
    return fastapi_testclient.TestClient(
        app, base_url="http://127.0.0.1", raise_server_exceptions=False
    )


def _post_routes():
    from llm_router.gateway import app

    out = []
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        if "POST" in methods:
            out.append(route.path)
    return sorted(set(out))


#: POST routes that do NOT take a prompt, with the reason. Anything not here
#: and not in COMPLETION_ENDPOINTS is unclassified and fails.
NON_COMPLETION_POSTS = {
    "/route": "returns a routing DECISION, executes nothing, so there is no "
              "answer for a discarded capability to corrupt",
    "/ground": "grounding check over text already produced elsewhere",
}

#: Endpoints that accept a prompt and return a model answer. Each must refuse a
#: request carrying a capability it cannot serve.
COMPLETION_ENDPOINTS = {
    "/v1/chat/completions": {
        "model": "x", "messages": [{"role": "user", "content": "hi"}],
    },
    "/v1/messages": {
        "model": "x", "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
    },
    "/v1/responses": {"model": "x", "input": "hi"},
    "/api/chat": {
        "model": "x", "messages": [{"role": "user", "content": "hi"}],
    },
    "/api/generate": {"model": "x", "prompt": "hi"},
}

_TOOLS = [{
    "type": "function",
    "function": {"name": "get_weather", "parameters": {"type": "object"}},
}]


def test_every_post_route_is_classified():
    """The enumerating half. A new endpoint cannot be silently unprotected."""
    unclassified = [
        p for p in _post_routes()
        if p not in COMPLETION_ENDPOINTS and p not in NON_COMPLETION_POSTS
    ]
    assert not unclassified, (
        f"gateway POST route(s) this file does not classify: {unclassified}\n\n"
        "Add it to COMPLETION_ENDPOINTS (and make it refuse unservable "
        "capabilities) or to NON_COMPLETION_POSTS with the reason it takes no "
        "prompt. Applying the fix endpoint by endpoint is how /v1/responses "
        "kept the H-03 defect after /v1/chat/completions was fixed."
    )


def test_the_route_scan_finds_the_endpoints():
    """Anti-vacuity: an empty scan satisfies the classification test."""
    found = set(_post_routes())
    assert len(found) >= 5, f"only {len(found)} POST routes discovered: {found}"
    for known in ("/v1/chat/completions", "/v1/responses", "/api/chat"):
        assert known in found, f"route discovery no longer finds {known}"


@pytest.mark.parametrize("path", sorted(COMPLETION_ENDPOINTS))
def test_a_tool_request_is_refused_not_silently_dropped(client, path):
    body = dict(COMPLETION_ENDPOINTS[path])
    body["tools"] = _TOOLS
    resp = client.post(path, json=body)
    assert resp.status_code == 400, (
        f"{path} returned {resp.status_code} for a request carrying tool "
        "definitions it cannot execute. A 200 here is a fluent answer to a "
        "question the client did not ask, with nothing to say the tools were "
        f"discarded. Body: {resp.text[:300]}"
    )
    detail = resp.json().get("detail", "")
    assert "tool" in detail.lower(), (
        f"{path} refused, but the reason does not mention tools: {detail!r}. "
        "A 400 with an unrelated message is barely better than a wrong 200."
    )


def test_the_refusal_does_not_fire_on_an_ordinary_request():
    """The other half. A rule that refuses everything is not a fix.

    Asserted against the helper, NOT by POSTing a plain prompt to each
    endpoint. The first draft did that, and it worked exactly as designed: the
    request was not refused, so it ROUTED — 14.7s to a live Codex call, a real
    routing_decision row, and a write to the operator's own usage.db. A test
    that proves a refusal did not fire by performing the unrefused action is a
    test that spends money to learn nothing.
    """
    from llm_router.gateway import _refuse_tools_if_present

    # No tools, no tool_choice, and the explicit "none" clients send.
    _refuse_tools_if_present(None, None)
    _refuse_tools_if_present([], None)
    _refuse_tools_if_present(None, "none")


def test_the_refusal_fires_on_each_unservable_shape():
    """Directly, so the parametrised endpoint tests above are the only place
    that needs a request at all."""
    from fastapi import HTTPException

    from llm_router.gateway import _refuse_tools_if_present

    for tools, choice in ((_TOOLS, None), (None, "required"), (None, "auto"),
                          (_TOOLS, "required")):
        with pytest.raises(HTTPException) as exc:
            _refuse_tools_if_present(tools, choice)
        assert exc.value.status_code == 400
        assert "tool" in str(exc.value.detail).lower()


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/messages", "/v1/responses"])
def test_tool_choice_alone_is_refused(client, path):
    """`tool_choice` without `tools` still asks for something unservable."""
    body = dict(COMPLETION_ENDPOINTS[path])
    body["tool_choice"] = "required"
    resp = client.post(path, json=body)
    assert resp.status_code == 400, (
        f"{path} accepted tool_choice={body['tool_choice']!r} with no tools "
        "array. The client is asking to be forced into a tool call and will "
        f"receive prose. Body: {resp.text[:200]}"
    )
