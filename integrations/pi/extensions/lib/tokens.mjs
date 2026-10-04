// Token estimate for a qwen-class tokenizer, without a tokenizer.
//
// Ollama 0.32 has no tokenize endpoint, so the pre-send check has to estimate.
// A flat chars/token ratio is wrong in BOTH directions on real agent traffic:
// measured on Pi + qwen3.6 sessions (harness-parity P1, 8 prompt deltas of
// 2k-24k tokens), number-heavy logs ran at 1.6-1.8 chars/token, where the
// usual 3.5-4 chars/token heuristic undercounts by 2x. Qwen tokenizes every
// digit separately. Fitting tokens = a*digits + b*other on those deltas gave
// a=1.09, b=0.318 (1/3.14), every delta within 5% of the reported count.
//
// Checked once against Ollama directly: a 640-line log (46,570 chars) was
// 25,247 prompt tokens; this estimate says 25,920 (+2.7%, n=1).
//
// So: digits count one token each, non-ASCII characters one token each (CJK,
// emoji: conservative), everything else 1 per 3.0 chars, times a 1.05 margin.
// It overestimates English prose (about 4 chars/token) by roughly 30%, which
// errs toward refusing slightly early rather than letting Ollama truncate.

export const OTHER_CHARS_PER_TOKEN = 3.0;
export const SAFETY_MARGIN = 1.05;

/** Estimated tokens for a string. */
export function estimateTextTokens(text) {
	if (!text) return 0;
	let digits = 0;
	let nonAscii = 0;
	for (let i = 0; i < text.length; i++) {
		const c = text.charCodeAt(i);
		if (c >= 48 && c <= 57) digits++;
		else if (c > 127) nonAscii++;
	}
	const other = text.length - digits - nonAscii;
	return Math.ceil((digits + nonAscii + other / OTHER_CHARS_PER_TOKEN) * SAFETY_MARGIN);
}

/** Collect every string inside a JSON-like value (keys included). */
function collectStrings(value, out) {
	if (value == null) return;
	if (typeof value === "string") {
		out.push(value);
		return;
	}
	if (typeof value === "number" || typeof value === "boolean") {
		out.push(String(value));
		return;
	}
	if (Array.isArray(value)) {
		for (const v of value) collectStrings(v, out);
		return;
	}
	if (typeof value === "object") {
		for (const [k, v] of Object.entries(value)) {
			// Images are not tokenized as base64 text; a vision model charges a fixed-ish
			// amount per image instead.
			if (k === "url" && typeof v === "string" && v.startsWith("data:image")) {
				out.push("x".repeat(3 * 1200));
				continue;
			}
			out.push(k);
			collectStrings(v, out);
		}
	}
}

/**
 * Estimated prompt tokens of an OpenAI chat-completions payload: messages plus tool
 * declarations (Ollama renders both into the prompt), plus a small per-message
 * template overhead.
 */
export function estimatePayloadTokens(payload) {
	if (!payload || typeof payload !== "object") return 0;
	const parts = [];
	collectStrings(payload.messages ?? [], parts);
	collectStrings(payload.tools ?? [], parts);
	let total = 0;
	for (const s of parts) total += estimateTextTokens(s);
	const nMessages = Array.isArray(payload.messages) ? payload.messages.length : 0;
	return total + 8 * nMessages;
}
