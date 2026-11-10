"""P4: batched observation masking (pure, unwired)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

from llm_router.proxy import history_mask as hm
from llm_router.proxy import translate

BIG = "x" * 3000


def _convo(n_pairs: int, result=None) -> dict:
    msgs = [{"role": "user", "content": "first task"}]
    for i in range(n_pairs):
        msgs.append({"role": "assistant", "content": [
            {"type": "text", "text": f"step {i}"},
            {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": f"ls {i}"}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": result or f"out {i} {BIG}"}]})
    return {"system": "sys", "tools": [], "messages": msgs}


def test_prefix_stable_within_batch():
    outs = {}
    for n in range(0, 40):
        body = _convo(n)
        new, _ = hm.mask_body(body)
        outs[len(body["messages"])] = new["messages"]
    # for every pair of counts sharing a boundary, the masked region is byte-identical
    for a in outs:
        for b in outs:
            if a < b and hm.boundary(a) == hm.boundary(b):
                cut = hm.boundary(a)
                assert json.dumps(outs[a][:cut]) == json.dumps(outs[b][:cut])
    # and across boundaries the earlier prefix is still an append-only prefix
    a, b = outs[21], outs[41]
    assert json.dumps(a[:hm.boundary(21)]) == json.dumps(b[:hm.boundary(21)])


def test_first_and_last_n_untouched_and_input_not_mutated():
    body = _convo(30)
    before = copy.deepcopy(body)
    new, st = hm.mask_body(body, keep_last=10, batch=10)
    assert body == before
    assert new["messages"][0] == body["messages"][0]
    assert new["messages"][-10:] == body["messages"][-10:]
    assert st.masked > 0 and st.chars_saved > 0
    assert st.est_tokens_after < st.est_tokens_before
    assert hm.boundary(len(body["messages"])) <= len(body["messages"]) - 10


def test_short_conversation_is_identity():
    body = _convo(3)
    new, st = hm.mask_body(body)
    assert new is body and st.masked == 0 and st.est_tokens_after == st.est_tokens_before


def test_idempotent():
    new, _ = hm.mask_body(_convo(30))
    again, st = hm.mask_body(new)
    assert again == new and st.masked == 0


def test_placeholder_keeps_paths_errors_exit_code():
    text = ("running\nTraceback (most recent call last):\n  File \"/srv/app/main.py\", line 3\n"
            "ValueError: bad thing in src/pkg/mod.py:42\nprocess exited with code 2\n" + BIG)
    body = _convo(30, result=text)
    new, _ = hm.mask_body(body)
    ph = new["messages"][2]["content"][0]["content"]
    assert ph.startswith(hm.PLACEHOLDER_PREFIX) and "Bash" in ph
    assert "/srv/app/main.py" in ph and "src/pkg/mod.py:42" in ph
    assert "ValueError: bad thing" in ph and "exit code 2" in ph
    assert len(ph) < len(text)


def test_is_error_flag_survives():
    body = _convo(30)
    body["messages"][2]["content"][0]["is_error"] = True
    new, _ = hm.mask_body(body)
    blk = new["messages"][2]["content"][0]
    assert blk["is_error"] is True and "is_error" in blk["content"]


def test_pairing_valid_for_anthropic_and_ollama():
    body = _convo(30)
    new, _ = hm.mask_body(body)
    uses = [b["id"] for m in new["messages"] if isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "tool_use"]
    results = [b["tool_use_id"] for m in new["messages"] if isinstance(m["content"], list)
               for b in m["content"] if b.get("type") == "tool_result"]
    assert uses == results == [f"t{i}" for i in range(30)]
    assert [m["content"] for m in new["messages"] if m["role"] == "assistant"] == \
           [m["content"] for m in body["messages"] if m["role"] == "assistant"]
    o_old = translate.to_ollama_messages(body)
    o_new = translate.to_ollama_messages(new)
    assert len(o_old) == len(o_new)
    for a, b in zip(o_old, o_new):
        assert a["role"] == b["role"] and a.get("tool_name") == b.get("tool_name")
        assert a.get("tool_calls") == b.get("tool_calls")
    assert any(m["role"] == "tool" and m["content"].startswith(hm.PLACEHOLDER_PREFIX) for m in o_new)


def test_list_content_result_masked():
    body = _convo(30)
    body["messages"][2]["content"][0]["content"] = [{"type": "text", "text": BIG}]
    new, _ = hm.mask_body(body)
    assert new["messages"][2]["content"][0]["content"].startswith(hm.PLACEHOLDER_PREFIX)


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_PROXY_MASK", raising=False)
    assert hm.enabled() is False
    monkeypatch.setenv("LLM_ROUTER_PROXY_MASK", "1")
    assert hm.enabled() is True


def test_flag_off_means_not_imported_anywhere():
    src = Path(hm.__file__).resolve().parents[1]
    hits = [str(p.relative_to(src)) for p in src.rglob("*.py")
            if p.name not in ("history_mask.py", "env_registry.py") and "history_mask" in p.read_text(encoding="utf-8")]
    assert hits == []
    code = "import sys, llm_router.proxy.translate, llm_router.proxy.local_mode; print('llm_router.proxy.history_mask' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
