// Pure-logic tests for the receipt band mod. Run: node --test tests/mods/
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  bandText, clock, parseFeed, parseReceipt, press, pressedText, receiptArgv, redoPrompt,
  safeModel, shouldShow, signalArgv, usd,
} from '../../src/llm_router/mods/llm-router-receipt/hooks/logic.mjs'

const ROUTED = JSON.stringify({ routed: true, key: 'msg_lr1', model: 'ollama/qwen3-coder:30b', cost_usd: 0, saved_usd: 0.04 })

test('render: a turn routed off Claude draws the receipt line', () => {
  const r = parseReceipt(ROUTED)
  assert.equal(bandText(r), 'served by ollama/qwen3-coder:30b · $0.00 · est. saved $0.04')
})

test('render: a turn NOT routed off Claude draws nothing', () => {
  assert.equal(parseReceipt(JSON.stringify({ routed: false })), null)
  assert.equal(shouldShow(null, false), false)
  assert.equal(shouldShow(undefined, false), false)
})

test('render: garbage and keyless receipts are not receipts', () => {
  for (const bad of ['', 'nope', '[]', '{}', '{"routed":true}', '{"routed":true,"key":""}', 'null'])
    assert.equal(parseReceipt(bad), null, bad)
})

test('render: an unknown figure is n/a, never $0.00', () => {
  const r = parseReceipt(JSON.stringify({ routed: true, key: 'k1', model: 'm', cost_usd: null, saved_usd: null }))
  assert.equal(bandText(r), 'served by m · n/a · est. saved n/a')
  assert.equal(usd(NaN), 'n/a')
  assert.equal(usd('3'), 'n/a')
  assert.equal(usd(-0.004), '$0.00')
  assert.equal(usd(-0.5), '-$0.50')
})

test('render: a survey on the band hides the receipt', () => {
  assert.equal(shouldShow({ pressed: null }, true), false)
  assert.equal(shouldShow({ pressed: null }, false), true)
})

test('render: a hostile model name cannot carry escapes or length', () => {
  const m = safeModel('\u001b[31mevil\u001b[0m' + 'x'.repeat(200))
  assert.ok(!m.includes('\u001b'))
  assert.ok(m.length <= 40)
  assert.equal(safeModel(undefined), 'an unknown model')
})

test('keys: k records kept for the receipt key, once', () => {
  const band = { receipt: parseReceipt(ROUTED), pressed: null }
  const first = press(band, 'kept')
  assert.deepEqual(first.record, { key: 'msg_lr1', signal: 'kept' })
  assert.equal(first.band.pressed, 'kept')
  const second = press(first.band, 'kept')
  assert.equal(second.record, null, 'a second press is not a second event')
  const third = press(first.band, 'redone')
  assert.equal(third.record, null, 'a kept receipt cannot also be redone by a press')
})

test('keys: r records redone and builds a redo prompt that quotes nothing', () => {
  const band = { receipt: parseReceipt(ROUTED), pressed: null }
  const step = press(band, 'redone')
  assert.deepEqual(step.record, { key: 'msg_lr1', signal: 'redone' })
  const prompt = redoPrompt(band.receipt)
  assert.ok(prompt.startsWith('claude:'), 'uses the router\'s explicit-Claude prefix')
  assert.ok(prompt.includes('ollama/qwen3-coder:30b'))
  assert.ok(prompt.length < 200)
})

test('keys: no band, no event', () => {
  assert.deepEqual(press(null, 'kept'), { band: null, record: null })
})

test('signal argv carries the key, the signal and the surface and nothing else', () => {
  assert.deepEqual(signalArgv(['llm-router'], 'msg_lr1', 'kept', 'terminal'),
    ['llm-router', 'mod', 'signal', '--key', 'msg_lr1', '--signal', 'kept', '--surface', 'terminal'])
  assert.throws(() => signalArgv(['llm-router'], 'k', 'loved', 'terminal'))
  assert.equal(signalArgv(['x'], 'k', 'kept', 'Termi nal; rm').at(-1), 'unknown')
})

test('receipt argv: seconds, and the session when there is one', () => {
  assert.deepEqual(receiptArgv(['llm-router'], 1500, 'abc'),
    ['llm-router', 'mod', 'receipt', '--since', '1.5', '--session', 'abc'])
  assert.equal(receiptArgv(['llm-router'], 1500, '').includes('--session'), false)
})

test('pressed text never claims a keep counts as used', () => {
  assert.match(pressedText('kept'), /not counted as used/)
  assert.match(pressedText('failed'), /could not record/)
})

test('feed: at most 10 rows, cleaned, unknown time is --:--', () => {
  const rows = Array.from({ length: 15 }, (_, i) => ({ ts: i, model: 'm\u001b', why: 'w', outcome: 'served' }))
  const parsed = parseFeed(JSON.stringify(rows))
  assert.equal(parsed.length, 10)
  assert.equal(parsed[0].model, 'm')
  assert.equal(clock(null), '--:--')
  assert.deepEqual(parseFeed('garbage'), [])
})
