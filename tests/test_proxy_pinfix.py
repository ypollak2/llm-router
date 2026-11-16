"""PINFIX treatment, compatibility and text-free replay; all backends are injected."""
# ruff: noqa: F811
import json
import random
from pathlib import Path

import pytest

from llm_router.proxy import local_mode
from llm_router.proxy.cache_cost import conversation_key
from llm_router.proxy.loop_guard import LoopGuard
from tests.test_proxy_cls_decide import Env, FakeClassifier, decision_model_on, turn_body
from tests.test_proxy_local_mode import Backend, _call, _ollama, _overrides_readable  # noqa: F401
from tests.test_proxy_pin_per_turn import failing


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ('LLM_ROUTER_PINFIX', 'LLM_ROUTER_PINFIX_K', 'LLM_ROUTER_PINFIX_MAX_FALLBACKS',
                 'LLM_ROUTER_PIN_SCOPE', 'LLM_ROUTER_LOCAL_CLASSIFIER', 'LLM_ROUTER_CLAUDE_MARKER',
                 'LLM_ROUTER_CLASSIFIER_AUTHORITY', 'LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('LLM_ROUTER_LOCAL_EXEMPT', 'none')


def drive(e, count, prompt='repair'):
    for i in range(count):
        e.post(turn_body([prompt], loops=i))


def test_k_and_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    monkeypatch.setenv('LLM_ROUTER_PIN_SCOPE', 'turn')
    e = Env(tmp_path)
    e.app = e.build(failing())
    drive(e, 5)
    rows = e.rows()
    assert [r['decision'] for r in rows] == ['fallback'] * 3 + ['forwarded'] * 2
    assert all('pin_change' not in r['local_mode'] for r in rows[:2])
    assert rows[2]['pin_cause'] == 'validation'
    assert all(r['reason'] == 'pinned_sticky' for r in rows[3:])
    assert all('pin_scope' not in r['local_mode'] and 'ab_arm' not in r for r in rows)


def test_ping_pong_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    monkeypatch.setenv('LLM_ROUTER_PINFIX_MAX_FALLBACKS', '3')
    e = Env(tmp_path)
    bad = failing().replies[0]
    e.app = e.build(Backend(bad, _ollama('ok'), bad, _ollama('ok'), bad))
    drive(e, 7)
    rows = e.rows()
    assert [r['decision'] for r in rows[:5]] == ['fallback', 'served', 'fallback', 'served', 'fallback']
    assert all('pin_change' not in r['local_mode'] for r in rows[:4])
    assert rows[4]['pin_cause'] == rows[5]['pin_cause'] == 'fallback_ceiling'
    assert len(e.backend.calls) == 5


@pytest.mark.parametrize('reason', list(local_mode.STRUCTURAL_REASONS))
def test_structural_pins_immediately(tmp_path, monkeypatch, reason):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    original = local_mode.decide_local
    monkeypatch.setattr(local_mode, 'decide_local', lambda b, s: (
        local_mode.LocalDecision(False, reason) if s.pinned is None else original(b, s)))
    e = Env(tmp_path)
    drive(e, 2)
    assert e.rows()[0]['local_mode']['pin_change'] == 'new->claude'
    assert e.rows()[1]['reason'] == 'pinned_claude'
    assert not e.backend.calls


@pytest.mark.parametrize('reason', ['backend_unhealthy', 'budget_exceeded', 'local_queue_timeout',
                                   'memory_budget', 'cold_over_deadline'])
def test_first_transient_marks_seen(tmp_path, monkeypatch, reason):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    original = local_mode.decide_local
    monkeypatch.setattr(local_mode, 'decide_local', lambda b, s: local_mode.LocalDecision(False, reason))
    e = Env(tmp_path)
    e.post(turn_body(['repair']))
    monkeypatch.setattr(local_mode, 'decide_local', original)
    e.post(turn_body(['repair'], loops=1))
    assert e.rows()[1]['decision'] == 'served'
    assert 'pin_change' not in e.rows()[0]['local_mode']


def test_repeat_counts_with_cap_no_pin(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    monkeypatch.setenv('LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN', '1')
    monkeypatch.setenv('LLM_ROUTER_PINFIX_MAX_FALLBACKS', '2')
    same = _ollama('', [_call('Read', {'file_path': '/w/a'})])
    e = Env(tmp_path)
    e.app = e.build(Backend(*[same] * 5))
    drive(e, 5)
    rows = e.rows()
    assert [r['loop_guard_trigger'] for r in rows if r['reason'] == 'loop_guard'] == ['repeat', 'repeat']
    assert rows[-1]['pin_cause'] == 'fallback_ceiling'


def test_siblings_do_not_share_or_reset_guard(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    e = Env(tmp_path)
    same = _ollama('', [_call('Read', {'file_path': '/w/a'})])
    bad = failing().replies[0]
    e.app = e.build(Backend(same, bad, same, same))
    e.post(turn_body(['sibling-a']))
    e.post(turn_body(['sibling-b']))  # must not reset a's recent signature
    e.post(turn_body(['sibling-a'], loops=1))
    e.post(turn_body(['sibling-b'], loops=1))  # must not inherit a's signature
    assert [r['decision'] for r in e.rows()] == ['served', 'fallback', 'fallback', 'served']
    assert e.rows()[2]['loop_guard_trigger'] == 'repeat'


def test_ab_deterministic_balanced_and_conversation_stable(monkeypatch):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'ab')
    rng = random.Random(20261115)
    keys = [str(rng.getrandbits(128)) for _ in range(1000)]
    arms = [local_mode.pinfix_arm(k) for k in keys]
    assert 450 <= arms.count('T') <= 550
    assert arms == [local_mode.pinfix_arm(k) for k in keys]
    for i in range(30):
        assert conversation_key(turn_body(['same'], loops=i), 'session') == conversation_key(turn_body(['same']), 'session')


@pytest.mark.parametrize('mode', ['ab', 'on', 'off', None], ids=['ab', 'on', 'off', 'unset'])
def test_ledger_rows_carry_arm_only_in_ab(tmp_path, monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv('LLM_ROUTER_PINFIX', raising=False)
    else:
        monkeypatch.setenv('LLM_ROUTER_PINFIX', mode)
    e = Env(tmp_path)
    keys, arms = set(), set()
    for i in range(40):
        prompt = f'ledger-arm-{i}'
        body = turn_body([prompt])
        key = conversation_key(body, '11111111-2222-3333-4444-555555555555')
        keys.add(key)
        drive(e, 3, prompt)
        rows = e.rows()
        assert len(rows) == (i + 1) * 3
        conversation_rows = rows[i * 3:]
        if mode == 'ab':
            assert all(row.get('ab_arm') in {'T', 'C'} for row in conversation_rows)
            row_arms = {row['ab_arm'] for row in conversation_rows}
            assert row_arms == {local_mode.pinfix_arm(key)}
            arms.update(row_arms)
        else:
            assert all('ab_arm' not in row for row in conversation_rows)
    assert len(keys) == 40
    if mode == 'ab':
        assert arms == {'T', 'C'}


def test_control_rows_equal_off_and_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'ab')
    prompt = next(f'c-{i}' for i in range(1000) if local_mode.pinfix_arm(
        conversation_key(turn_body([f'c-{i}']), '11111111-2222-3333-4444-555555555555')) == 'C')
    captured = []
    # Compare every recorded field except clocks and generated identities.
    for mode in ('off', 'garbage', 'ab'):
        monkeypatch.setenv('LLM_ROUTER_PINFIX', mode)
        d = tmp_path / mode
        d.mkdir()
        e = Env(d)
        e.app = e.build(failing())
        drive(e, 3, prompt)
        rows = e.rows()
        for r in rows:
            if mode == 'ab':
                assert r.pop('ab_arm') == 'C'
            else:
                assert 'ab_arm' not in r
            for name in ('ts', 'trace_id', 'task_id', 'phase_ms', 'msg_id', 'upstream_latency_s',
                         'route_latency_s', 'added_latency_s', 'tier_phases_ms', 'tier_decision_s'):
                r.pop(name, None)
        captured.append(rows)
    for other in captured[1:]:
        differences = [(i, k) for i, (a, b) in enumerate(zip(captured[0], other))
                       for k in a.keys() | b.keys() if a.get(k) != b.get(k)]
        assert not differences, differences


@pytest.mark.parametrize('raw,expected', [('', (3, 8)), ('bad', (3, 8)), ('0', (3, 8)),
                                         ('51', (3, 8)), ('1', (1, 1)), ('10', (10, 10))])
def test_limits(monkeypatch, raw, expected):
    monkeypatch.setenv('LLM_ROUTER_PINFIX_K', raw)
    monkeypatch.setenv('LLM_ROUTER_PINFIX_MAX_FALLBACKS', raw)
    assert local_mode.pinfix_limits() == expected


def test_state_bounded_and_success_resets_only_streak(tmp_path):
    lm = local_mode.LocalModeState(kill_path=tmp_path / 'k', max_pins=2)
    lm.pinfix_outcome('a', True)
    lm.pinfix_outcome('a', False)
    assert lm.pinfix_counts['a'] == (0, 1)
    lm.pinfix_outcome('b', True)
    lm.pinfix_outcome('a', True)
    lm.pinfix_outcome('c', True)
    assert set(lm.pinfix_counts) == {'a', 'c'}
    g = LoopGuard(1, 1, max_states=2)
    for key in ('a', 'b', 'c'):
        g.record_served(key, {'content': []})
    assert set(g._sessions) == {'b', 'c'}


@pytest.mark.parametrize('flag', ['LLM_ROUTER_LOCAL_CLASSIFIER', 'LLM_ROUTER_CLAUDE_MARKER',
                                  'LLM_ROUTER_LOCAL_EXEMPT'])
def test_live_interactions(tmp_path, monkeypatch, flag):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    monkeypatch.setenv(flag, 'coordinator_main' if flag == 'LLM_ROUTER_LOCAL_EXEMPT' else 'on')
    if flag == 'LLM_ROUTER_LOCAL_CLASSIFIER':
        decision_model_on(monkeypatch)
        FakeClassifier(monkeypatch, tier='local')
    e = Env(tmp_path)
    e.app = e.build(failing())
    drive(e, 2)
    assert [r['decision'] for r in e.rows()] == ['fallback', 'fallback']


def test_marker_still_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    monkeypatch.setenv('LLM_ROUTER_CLAUDE_MARKER', '1')
    e = Env(tmp_path)
    drive(e, 2, local_mode.CLAUDE_MARKER)
    assert all(r['reason'] == 'claude_marker' for r in e.rows())
    assert not e.backend.calls


def test_session_backstop(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    from llm_router.proxy import server as ps
    original = ps.ProxyConfig
    monkeypatch.setattr(ps, 'ProxyConfig', lambda **kw: original(**kw, loop_max_consecutive=1))
    e = Env(tmp_path)
    for i in range(5):
        e.post(turn_body([f'sibling-{i}']))
    assert [r['decision'] for r in e.rows()] == ['served'] * 4 + ['fallback']
    assert e.rows()[-1]['loop_guard_trigger'] == 'cap'


def test_replay(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    monkeypatch.setenv('LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN', '1')
    source = json.loads((Path(__file__).parent / 'fixtures/proxy/replay-e7d66d-20261115.json').read_text())['rows']
    # Only the 11 observed local outcomes are known. The 19 censored rows receive unique
    # valid candidates to measure eligibility, not to claim counterfactual local success.
    replies = [_ollama('', [_call('Read', {'file_path': f'/w/{i}'})]) for i in range(30)]
    replies[9] = replies[8]  # cap skips backend call 8; next candidate repeats it
    e = Env(tmp_path)
    e.app = e.build(Backend(*replies))
    attempted = []
    for i in range(30):
        before = len(e.backend.calls)
        e.post(turn_body(['repair'], loops=i))
        attempted.append(len(e.backend.calls) > before)
    rows = e.rows()
    assert sum(attempted[i] for i, old in enumerate(source) if old['reason'] == 'pinned_claude') == 17
    assert sum(old['pin_change'] == 'local->claude' for old in source) == 1
    assert [r['reason'] for r in rows[:11]] == [r['reason'] for r in source[:11]]
    assert not any('pin_change' in r['local_mode'] for r in rows)
    assert sum(r['reason'] != 'pinned_sticky' for r, old in zip(rows, source) if old['reason'] == 'pinned_claude') == 19
    print(json.dumps({
        'rows': len(source),
        'sticky_pins_before': sum(old['pin_change'] == 'local->claude' for old in source),
        'sticky_pins_after': sum(r['local_mode'].get('pin_change') == 'local->claude' for r in rows),
        'formerly_pinned_eligible': sum(r['reason'] != 'pinned_sticky' for r, old in zip(rows, source)
                                      if old['reason'] == 'pinned_claude'),
        'formerly_pinned_backend_attempts': sum(attempted[i] for i, old in enumerate(source)
                                              if old['reason'] == 'pinned_claude'),
    }, sort_keys=True))


def test_sibling_fallback_counters_are_independent(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    monkeypatch.setenv('LLM_ROUTER_PINFIX_K', '2')
    e = Env(tmp_path)
    bad = failing().replies[0]
    e.app = e.build(Backend(bad, _ollama('ok'), bad, bad))
    e.post(turn_body(['a']))
    e.post(turn_body(['b']))
    e.post(turn_body(['a'], loops=1))
    e.post(turn_body(['b'], loops=1))
    assert e.rows()[2]['local_mode']['pin_change'] == 'local->claude'
    assert 'pin_change' not in e.rows()[3]['local_mode']


def test_ab_rows_keep_both_arms_stable(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'ab')
    prompts = {}
    for i in range(100):
        prompt = f'arm-{i}'
        arm = local_mode.pinfix_arm(conversation_key(turn_body([prompt]),
                                  '11111111-2222-3333-4444-555555555555'))
        prompts.setdefault(arm, prompt)
    assert set(prompts) == {'T', 'C'}
    from llm_router.proxy import server as ps
    original = ps.ProxyConfig
    monkeypatch.setattr(ps, 'ProxyConfig', lambda **kw: original(**kw, backend_fail_n=100))
    e = Env(tmp_path)
    e.app = e.build(failing())
    for arm, prompt in prompts.items():
        drive(e, 3, prompt)
        assert {r['ab_arm'] for r in e.rows()[-3:]} == {arm}
        assert [r['decision'] for r in e.rows()[-3:]] == (
            ['fallback'] * 3 if arm == 'T' else ['fallback', 'forwarded', 'forwarded'])


@pytest.mark.parametrize('kind', ['side', 'claude_prefix'])
def test_ab_labels_early_and_bypassed_rows(tmp_path, monkeypatch, kind):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'ab')
    e = Env(tmp_path)
    body = turn_body(['claude: review' if kind == 'claude_prefix' else 'review'])
    if kind == 'side':
        body.pop('tools', None)
    e.post(body)
    assert e.rows()[0]['ab_arm'] == local_mode.pinfix_arm(conversation_key(
        body, '11111111-2222-3333-4444-555555555555'))
    assert not e.backend.calls


@pytest.mark.parametrize('limit', ['streak', 'total'])
@pytest.mark.parametrize('neutral', ['policy_kept', 'backend_unhealthy'])
def test_neutral_steps_preserve_failure_counts(tmp_path, monkeypatch, neutral, limit):
    from llm_router import user_rules
    from llm_router.proxy.backend_health import BackendHealth

    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    monkeypatch.setenv('LLM_ROUTER_PINFIX_K', '2' if limit == 'streak' else '10')
    monkeypatch.setenv('LLM_ROUTER_PINFIX_MAX_FALLBACKS', '2' if limit == 'total' else '8')
    e = Env(tmp_path)
    e.app = e.build(failing())
    e.post(turn_body(['repair']))
    with monkeypatch.context() as patch:
        if neutral == 'policy_kept':
            patch.setattr(user_rules, 'allows', lambda model: False)
        else:
            async def refuse(*args):
                return False, None
            patch.setattr(BackendHealth, 'admit', refuse)
        drive(e, 3)
    # Neutral steps neither turn one failure into two nor reset that failure.
    assert len(e.backend.calls) == 1
    assert [r['reason'] for r in e.rows()[1:]] == [neutral] * 3
    assert all('pin_change' not in r['local_mode'] for r in e.rows())
    e.post(turn_body(['repair'], loops=4))
    assert len(e.backend.calls) == 2
    assert e.rows()[-1]['local_mode']['pin_change'] == 'local->claude'


@pytest.mark.parametrize('reason', ['backend_unhealthy', 'budget_exceeded', 'local_queue_timeout',
                                   'memory_budget', 'cold_over_deadline'])
def test_step_five_transient_is_neutral(tmp_path, monkeypatch, reason):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    monkeypatch.setenv('LLM_ROUTER_PINFIX_K', '2')
    e = Env(tmp_path)
    bad = failing().replies[0]
    e.app = e.build(Backend(_ollama('ok'), _ollama('ok'), _ollama('ok'), bad, bad))
    drive(e, 4)
    with monkeypatch.context() as patch:
        patch.setattr(local_mode, 'decide_local', lambda b, s: local_mode.LocalDecision(False, reason))
        e.post(turn_body(['repair'], loops=4))
    assert e.rows()[4]['reason'] == reason
    assert 'pin_change' not in e.rows()[4]['local_mode']
    e.post(turn_body(['repair'], loops=5))
    assert e.rows()[5]['local_mode']['pin_change'] == 'local->claude'
    assert len(e.backend.calls) == 5


def test_queue_refusal_inside_backend_is_neutral(tmp_path, monkeypatch):
    from llm_router.toolkit.local_gate import LocalQueueTimeout

    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    monkeypatch.setenv('LLM_ROUTER_PINFIX_K', '2')
    e = Env(tmp_path)
    e.app = e.build(failing())
    e.post(turn_body(['repair']))
    e.backend.exc = LocalQueueTimeout('queue deadline')
    drive(e, 3)
    assert all(r['reason'] == 'local_queue_timeout' for r in e.rows()[1:])
    assert all('pin_change' not in r['local_mode'] for r in e.rows())
    e.backend.exc = None
    e.post(turn_body(['repair'], loops=4))
    assert e.rows()[-1]['local_mode']['pin_change'] == 'local->claude'


@pytest.mark.parametrize('claude_step', ['failure', 'explicit_prefix', 'control'])
def test_session_backstop_resets_on_claude(tmp_path, monkeypatch, claude_step):
    from llm_router.proxy import server as ps

    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    original = ps.ProxyConfig
    monkeypatch.setattr(ps, 'ProxyConfig', lambda **kw: original(**kw, loop_max_consecutive=1))
    e = Env(tmp_path)
    for i in range(3):
        e.post(turn_body([f'before-{i}']))
    if claude_step == 'failure':
        e.backend.replies = failing().replies
        e.post(turn_body(['failed-sibling']))
        e.backend.replies = [_ollama('ok')]
    elif claude_step == 'explicit_prefix':
        e.post(turn_body(['claude: review']))
    else:
        with monkeypatch.context() as patch:
            patch.setattr(local_mode, 'pinfix_treatment', lambda key: False)
            patch.setattr(local_mode, 'decide_local', lambda b, s: local_mode.LocalDecision(False, 'policy_kept'))
            e.post(turn_body(['control-sibling']))
    for i in range(5):
        e.post(turn_body([f'after-{i}']))
    rows = e.rows()
    assert rows[3]['decision'] != 'served'
    assert [r['decision'] for r in rows[4:]] == ['served'] * 4 + ['fallback']
    assert rows[-1]['loop_guard_trigger'] == 'cap'


def test_long_healthy_session_with_periodic_claude(tmp_path, monkeypatch):
    from llm_router.proxy import server as ps

    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    original = ps.ProxyConfig
    monkeypatch.setattr(ps, 'ProxyConfig', lambda **kw: original(**kw, loop_max_consecutive=1))
    e = Env(tmp_path)
    for i in range(60):
        e.post(turn_body([f'claude: review {i}' if i % 4 == 3 else f'sibling-{i}']))
    rows = e.rows()
    assert sum(r['decision'] == 'served' for r in rows) == 45
    assert sum(r['decision'] == 'forwarded' for r in rows) == 15
    assert not any(r['reason'] == 'loop_guard' for r in rows)


@pytest.mark.parametrize('exc,reason', [(RuntimeError('backend failed'), 'backend_error'),
                                       (TimeoutError('backend timed out'), 'budget_exceeded')])
def test_attempted_backend_failures_count(tmp_path, monkeypatch, exc, reason):
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    monkeypatch.setenv('LLM_ROUTER_PINFIX_K', '1')
    e = Env(tmp_path)
    e.app = e.build(Backend(exc=exc))
    e.post(turn_body(['repair']))
    assert len(e.backend.calls) == 1
    row = e.rows()[0]
    assert row['reason'] == reason
    assert row['local_mode']['pin_change'] == 'local->claude'
    assert '_pinfix_attempted' not in row
