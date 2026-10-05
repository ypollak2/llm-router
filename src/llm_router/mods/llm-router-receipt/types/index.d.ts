/** One turn served off Claude, as `llm-router mod receipt` reports it. */
export type Receipt = {
  key: string
  model: string | null
  costUsd: number | null
  savedUsd: number | null
}

/** What the band shows: the receipt, and the press already recorded for it. */
export type Band = { receipt: Receipt; pressed: 'kept' | 'redone' | 'failed' | null }

/** One row of the /router pane. */
export type FeedRow = { ts: number | null; model: string; why: string; outcome: string }

declare module 'claude-code' {
  interface PluginState {
    'llm-router-receipt': { band: Band | null; turnStartMs: number; feed: FeedRow[] }
  }
}
