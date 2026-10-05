import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Band, FeedRow } from '../types'
import {
  bandText,
  clock,
  parseFeed,
  parseReceipt,
  press,
  pressedText,
  receiptArgv,
  redoPrompt,
  shouldShow,
  signalArgv,
} from './logic.mjs'
import { ROUTER_ARGV } from './router-cmd.mjs'

const PANE = 'llm-router-feed'
const band = atom({ plugin: 'llm-router-receipt', key: 'band' } as const, null as Band | null)
const turnStartMs = atom({ plugin: 'llm-router-receipt', key: 'turnStartMs' } as const, 0)
const feed = atom({ plugin: 'llm-router-receipt', key: 'feed' } as const, [] as FeedRow[])

async function record($: EngineInterface, signal: 'kept' | 'redone', surface: string) {
  const current = await read($, band)
  const step = press(current, signal)
  if (!step.record) return
  await update($, band, () => step.band)
  let ok = false
  try {
    const ran = await $.process.run(signalArgv(ROUTER_ARGV, step.record.key, signal, surface), { timeoutMs: 15000 })
    ok = ran.exitCode === 0
  } catch {
    ok = false
  }
  if (!ok) {
    // Back to unpressed, so the person can try again; nothing was recorded.
    await update($, band, b => (b ? { ...b, pressed: null } : b))
    $.ui.toast('llm-router: that press was not recorded; try again')
    return
  }
  if (signal === 'redone' && current) {
    const now = await $.clock.now()
    await update($, turnStartMs, () => now) // the redo turn starts now, never the old receipt
    void $.prompt.submit({ text: redoPrompt(current.receipt) })
  }
}


async function refreshFeed($: EngineInterface, sessionId: string) {
  try {
    const ran = await $.process.run([...ROUTER_ARGV, 'mod', 'feed', '--session', sessionId, '--limit', '10'], {
      timeoutMs: 15000,
    })
    if (ran.exitCode === 0) await update($, feed, () => parseFeed(ran.stdout))
  } catch {
    // no llm-router on this host: the pane says it has nothing
  }
}


export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'router',
      description: 'llm-router: the last 10 routing decisions (time, model, why, outcome)',
    })
    return next(e)
  })

  // The band lasts until the next prompt. The redo prompt this plugin submits
  // from a press passes through here too; record() also moves the turn start
  // itself, so the redo turn can never bring back the old receipt.
  on('prompt.submit', async ($, e, next) => {
    const now = await $.clock.now()
    await update($, turnStartMs, () => now)
    await update($, band, () => null)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (e.agentId !== undefined || e.isAborted) return done
    const since = await read($, turnStartMs)
    if (!since) return done
    const sessionId = await $.session.id()
    try {
      const ran = await $.process.run(receiptArgv(ROUTER_ARGV, since, sessionId), { timeoutMs: 15000 })
      const receipt = ran.exitCode === 0 ? parseReceipt(ran.stdout) : null
      await update($, band, () => (receipt ? { receipt, pressed: null } : null))
    } catch {
      await update($, band, () => null) // no llm-router on this host: no band
    }
    await refreshFeed($, sessionId)
    return done
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const current = await read($, band)
    if (!shouldShow(current, e.props.hasSurvey) || !current) return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    if (current.pressed !== null) {
      return (
        <Box>
          <Text dimColor>llm-router · {pressedText(current.pressed)}</Text>
        </Box>
      )
    }
    return (
      <Box flexDirection="row" flexWrap="wrap" columnGap={2}>
        <Text>llm-router · {bandText(current.receipt)}</Text>
        <Button key="keep" label="keep" hotkey="k" onPress={p => record($, 'kept', p.surface)} />
        <Button key="redo" label="redo on Claude" hotkey="r" onPress={p => record($, 'redone', p.surface)} />
      </Box>
    )
  })

  on('command.run', { command: 'router' }, async $ => {
    await refreshFeed($, await $.session.id())
    await $.ui.open({ id: PANE, title: 'llm-router' })
    return { text: 'llm-router routing feed opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const rows = await read($, feed)
    return (
      <Box flexDirection="column">
        <Text dimColor>routing feed · last {rows.length} · this session</Text>
        {rows.length === 0 && <Text dimColor>No routing decision recorded yet.</Text>}
        {rows.map(r => (
          <Text>
            {clock(r.ts)} {r.model} · {r.why} · {r.outcome}
          </Text>
        ))}
      </Box>
    )
  })
}
