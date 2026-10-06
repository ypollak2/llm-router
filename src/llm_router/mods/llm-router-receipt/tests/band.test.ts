// Engine-level tests: `claude plugin test <this mod>`. No file system, network
// or process is reached: every `$.process.run` is answered by the test's own
// `process.run` hook, which stands where the host would be.
import { expect, mock, test } from 'claude-code/testing'

const ROUTED = JSON.stringify({ routed: true, key: 'msg_lr1', model: 'ollama/qwen3-coder:30b', cost_usd: 0, saved_usd: 0.04 })
const BAND = { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 100 } as any

function host(on: any, receipt: string, prompts: string[] = []) {
  const calls: string[][] = []
  on('process.run', (_$: any, e: any) => {
    calls.push([...e.argv])
    const stdout = e.argv.includes('receipt') ? receipt : e.argv.includes('feed') ? '[]' : '{"recorded":true}'
    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  // The engine's own bottom for the events the tests raise.
  on('prompt.submit', (_$: any, e: any) => {
    prompts.push(e.text)
    return { text: e.text }
  })
  on('turn.complete', (_$: any, e: any) => ({ text: e.answer }))
  on('session.id', () => ({ value: 'sess-1' }))
  // What the engine draws when the mod returns nothing: an empty Box.
  on('ui.render', () => ({ type: 'Box', props: {}, children: [] }))
  return calls
}

async function turn($: any) {
  await $.prompt.submit({ text: 'a question' })
  await $.turn.complete({ answer: 'an answer', durationMs: 5, isAborted: false, turnId: 't1', reason: 'answer' })
}

for (const surface of ['terminal', 'desktop'] as const) {
  test(`${surface}: a turn served off Claude shows the receipt; k records one kept`, async ($, on) => {
    mock.clock(on, { now: 1_790_000_000_000 })
    const calls = host(on, ROUTED)
    await turn($)
    const ui = await $.ui.mount({ plugin: 'llm-router-receipt', surface, component: 'AbovePrompt', props: BAND })
    expect((await ui.find({ type: 'Text', text: /served by ollama\/qwen3-coder:30b · \$0\.00 · est\. saved \$0\.04/ }))).toBeDefined()
    await ui.press({ key: 'keep' })
    await ui.press({ key: 'keep' }).catch(() => undefined)
    const signals = calls.filter(a => a.includes('signal'))
    expect(signals.length).toBe(1)
    expect(signals[0]).toEqual(['llm-router', 'mod', 'signal', '--key', 'msg_lr1', '--signal', 'kept', '--surface', surface])
    await ui.unmount()
  })

  test(`${surface}: a turn not served off Claude shows nothing`, async ($, on) => {
    mock.clock(on, { now: 1_790_000_000_000 })
    host(on, JSON.stringify({ routed: false }))
    await turn($)
    const ui = await $.ui.mount({ plugin: 'llm-router-receipt', surface, component: 'AbovePrompt', props: BAND })
    expect(await ui.find({ type: 'Text', text: /served by/ })).toBeUndefined()
    expect(await ui.find({ key: 'keep' })).toBeUndefined()
    await ui.unmount()
  })

  test(`${surface}: r records one redone and submits a claude: redo prompt`, async ($, on) => {
    mock.clock(on, { now: 1_790_000_000_000 })
    const prompts: string[] = []
    const calls = host(on, ROUTED, prompts)
    await turn($)
    const ui = await $.ui.mount({ plugin: 'llm-router-receipt', surface, component: 'AbovePrompt', props: BAND })
    await ui.press({ key: 'redo' })
    expect(calls.filter(a => a.includes('signal'))).toEqual([
      ['llm-router', 'mod', 'signal', '--key', 'msg_lr1', '--signal', 'redone', '--surface', surface],
    ])
    expect(prompts.some(p => p.startsWith('claude: Please redo your previous answer'))).toBe(true)
    await ui.unmount()
  })

  test(`${surface}: after r, the redo turn asks for receipts from AFTER the press`, async ($, on) => {
    const clock = mock.clock(on, { now: 1_790_000_000_000 })
    const calls = host(on, ROUTED)
    await turn($)
    const ui = await $.ui.mount({ plugin: 'llm-router-receipt', surface, component: 'AbovePrompt', props: BAND })
    await clock.advance(60_000)
    await ui.press({ key: 'redo' })
    await ui.unmount()
    const since = calls.filter(a => a.includes('receipt')).map(a => Number(a[a.indexOf('--since') + 1]))
    await $.turn.complete({ answer: 'redone', durationMs: 5, isAborted: false, turnId: 't2', reason: 'answer' })
    const after = calls.filter(a => a.includes('receipt')).map(a => Number(a[a.indexOf('--since') + 1]))
    expect(after.length).toBe(since.length + 1)
    expect(after[after.length - 1]).toBeGreaterThan(since[0] + 59)
  })

  test(`${surface}: the band expires after the next prompt`, async ($, on) => {
    mock.clock(on, { now: 1_790_000_000_000 })
    host(on, ROUTED)
    await turn($)
    await $.prompt.submit({ text: 'next prompt' })
    const ui = await $.ui.mount({ plugin: 'llm-router-receipt', surface, component: 'AbovePrompt', props: BAND })
    expect(await ui.find({ type: 'Text', text: /served by/ })).toBeUndefined()
    await ui.unmount()
  })
}
