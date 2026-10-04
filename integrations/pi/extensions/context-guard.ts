/**
 * llm-router Pi profile: refuse a prompt that would not fit, instead of letting
 * Ollama truncate it.
 *
 * Ollama does not reject a prompt longer than the loaded context: it drops the
 * OLDEST tokens (the system prompt and the start of the task) and answers about
 * what is left. Measured in Pi (harness-parity P1, 1 of 50 sessions): Ollama
 * logged "n_tokens = 32767, truncated = 1" and Pi was never told.
 *
 * Before every model request this estimates the prompt (lib/tokens.mjs) and, if
 * it exceeds contextWindow - reply reserve, makes the request fail before
 * anything is sent, with an error that Pi classifies as context overflow
 * ("prompt too long; exceeded max context length"). Pi then runs its one
 * compact-and-retry recovery (with compaction.ts, a real shrink); if the retry
 * is still too big the run ends with that explicit error. Nothing is truncated.
 *
 * After every reply it also checks the reported prompt size: a prompt that
 * filled the window is the signature of a truncation the estimate missed, and
 * the reply is turned into the same explicit error rather than used.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { recordEvent } from "./lib/events.mjs";
import { estimatePayloadTokens } from "./lib/tokens.mjs";

// Room kept for the reply. Pi may request more (the model's maxTokens), but reserving all
// of it would refuse prompts that measured fine (a 25k-token single-file read in a 32k
// window). A reply that outgrows the room left makes Ollama shift the context during
// generation; message_end below records every reply where that may have happened.
const REPLY_RESERVE = Number(process.env.LLM_ROUTER_PI_REPLY_RESERVE || 4096);
export const OVERFLOW_TEXT = "prompt too long; exceeded max context length";

/** A payload stand-in that throws when the HTTP client serializes it: the request is never sent. */
function refusingPayload(payload: any, message: string): any {
	return {
		...payload,
		toJSON() {
			throw new Error(message);
		},
	};
}

export default function (pi: ExtensionAPI) {
	pi.on("before_provider_request", async (event: any, ctx: any) => {
		const window = Number(ctx?.model?.contextWindow || 0);
		if (!window) return undefined;
		const budget = window - REPLY_RESERVE;
		const estimate = estimatePayloadTokens(event.payload);
		if (estimate <= budget) return undefined;
		const message =
			`llm-router pre-send check: ${OVERFLOW_TEXT} (estimated ${estimate} prompt tokens > budget ${budget} = ` +
			`context ${window} - reply reserve ${REPLY_RESERVE}); refused instead of letting the server truncate it`;
		recordEvent("presend_refused", { estimate, budget, window, model: ctx?.model?.id, cwd: ctx?.cwd });
		process.stderr.write(`[llm-router] ${message}\n`);
		return refusingPayload(event.payload, message);
	});

	pi.on("message_end", async (event: any, ctx: any) => {
		const m = event.message;
		if (m?.role !== "assistant" || m.stopReason === "error" || m.stopReason === "aborted") return undefined;
		const window = Number(ctx?.model?.contextWindow || 0);
		const input = Number(m.usage?.input || 0) + Number(m.usage?.cacheRead || 0);
		if (!window) return undefined;
		if (input < window - 1) {
			const output = Number(m.usage?.output || 0);
			if (input + output >= window) {
				// Not provably wrong, so the reply is kept; but it is recorded, not silent.
				recordEvent("context_shift_possible", { input, output, window, model: ctx?.model?.id, cwd: ctx?.cwd });
				process.stderr.write(`[llm-router] prompt ${input} + reply ${output} tokens reached the ${window}-token window: the server may have shifted out the start of the context\n`);
			}
			return undefined;
		}
		const message =
			`llm-router post-check: ${OVERFLOW_TEXT} (the server reported ${input} prompt tokens for a ${window}-token ` +
			"window, which means it truncated the prompt); this reply was discarded";
		recordEvent("truncation_detected", { input, window, model: ctx?.model?.id, cwd: ctx?.cwd });
		process.stderr.write(`[llm-router] ${message}\n`);
		return { message: { ...m, content: [], stopReason: "error", errorMessage: message } };
	});
}
