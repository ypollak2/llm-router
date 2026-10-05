// Pure logic of the receipt band: no `$`, no I/O, so it is tested with plain
// `node --test` (tests/mods/receipt_logic.spec.mjs) as well as by the engine.
//
// Nothing here ever carries prompt or answer text: the band shows a model name
// and two dollar figures, and the redo prompt names only the model.

const MODEL_MAX = 40

/** A model name made safe to draw: printable ASCII only, bounded. */
export function safeModel(model) {
  if (typeof model !== 'string') return 'an unknown model'
  const clean = model.replace(/[^\x20-\x7e]/g, '').trim().slice(0, MODEL_MAX)
  return clean || 'an unknown model'
}

/** `$0.04`, `-$0.01`, or `n/a` for an unknown figure (never `$0.00`). */
export function usd(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'n/a'
  const abs = Math.abs(value).toFixed(2)
  return value < 0 && abs !== '0.00' ? `-$${abs}` : `$${abs}`
}

/**
 * The receipt from `llm-router mod receipt` stdout, or null when the turn was
 * not served off Claude or the output is not a receipt.
 */
export function parseReceipt(stdout) {
  let data
  try {
    data = JSON.parse(stdout)
  } catch {
    return null
  }
  if (!data || data.routed !== true || typeof data.key !== 'string' || !data.key) return null
  const num = v => (typeof v === 'number' && Number.isFinite(v) ? v : null)
  return {
    key: data.key,
    model: typeof data.model === 'string' ? data.model : null,
    costUsd: num(data.cost_usd),
    savedUsd: num(data.saved_usd),
  }
}

/** The band's one line of text. */
export function bandText(receipt) {
  return `served by ${safeModel(receipt.model)} · ${usd(receipt.costUsd)} · est. saved ${usd(receipt.savedUsd)}`
}

/** What the band says once a key was pressed for this receipt. */
export function pressedText(pressed) {
  if (pressed === 'kept') return 'kept: recorded (not counted as used; only a passing test counts)'
  if (pressed === 'redone') return 'redo on Claude: recorded, asking Claude to redo it'
  return 'llm-router could not record that press'
}

/**
 * Whether the band draws anything: only for a turn served off Claude, and
 * never over a survey. A null band (no receipt, or expired) draws nothing.
 */
export function shouldShow(band, hasSurvey) {
  return band !== null && band !== undefined && !hasSurvey
}

/** argv for `llm-router mod receipt` over the turn that began at `turnStartMs`. */
export function receiptArgv(routerArgv, turnStartMs, sessionId) {
  const argv = [...routerArgv, 'mod', 'receipt', '--since', String(turnStartMs / 1000)]
  if (typeof sessionId === 'string' && sessionId) argv.push('--session', sessionId)
  return argv
}

/** argv for `llm-router mod signal`: the route key, the signal, the surface. Nothing else. */
export function signalArgv(routerArgv, key, signal, surface) {
  if (signal !== 'kept' && signal !== 'redone') throw new Error(`unknown signal ${signal}`)
  const where = /^[a-z0-9_-]{1,32}$/.test(String(surface)) ? String(surface) : 'unknown'
  return [...routerArgv, 'mod', 'signal', '--key', key, '--signal', signal, '--surface', where]
}

/**
 * The follow-up prompt `r` submits. It starts with `claude:`, the router's own
 * explicit "answer this on Claude" override (proxy/escalation.py and
 * northstar's redo detection both read that prefix), and quotes nothing from
 * the conversation.
 */
export function redoPrompt(receipt) {
  return (
    'claude: Please redo your previous answer yourself, on Claude. ' +
    `It was served by ${safeModel(receipt.model)} and I want Claude's own answer instead.`
  )
}

/**
 * The state change a key press makes. One press per receipt: a band that has
 * already recorded a press ignores the next one, so a double press is still
 * ONE signal event.
 */
export function press(band, signal) {
  if (!band || band.pressed !== null) return { band, record: null }
  return { band: { ...band, pressed: signal }, record: { key: band.receipt.key, signal } }
}

/** `20:44` for an epoch-seconds timestamp; `--:--` when unknown. */
export function clock(tsSeconds) {
  if (typeof tsSeconds !== 'number' || !Number.isFinite(tsSeconds)) return '--:--'
  const d = new Date(tsSeconds * 1000)
  const pad = n => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** The /router pane rows from `llm-router mod feed` stdout (at most 10). */
export function parseFeed(stdout) {
  let rows
  try {
    rows = JSON.parse(stdout)
  } catch {
    return []
  }
  if (!Array.isArray(rows)) return []
  const text = v => (typeof v === 'string' ? v.replace(/[^\x20-\x7e]/g, '').slice(0, MODEL_MAX) : '-')
  return rows.slice(0, 10).map(r => ({
    ts: typeof r?.ts === 'number' ? r.ts : null,
    model: text(r?.model),
    why: text(r?.why),
    outcome: text(r?.outcome),
  }))
}
