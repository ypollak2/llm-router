// Append-only record of every intervention the profile makes (KPI guardrail G2:
// every silent-failure class must be recorded, not just prevented).
// `llm-router pi` points LLM_ROUTER_PI_EVENT_LOG at ~/.llm-router/pi/events.jsonl.

import { appendFileSync } from "node:fs";

export function recordEvent(kind, data = {}) {
	const path = process.env.LLM_ROUTER_PI_EVENT_LOG;
	if (!path) return false;
	try {
		appendFileSync(path, JSON.stringify({ ts: new Date().toISOString(), kind, pid: process.pid, ...data }) + "\n");
		return true;
	} catch {
		// The log is evidence, not control flow: failing to write it must not change
		// what the profile does. The caller still acts; only the record is lost.
		return false;
	}
}
