// Pure helpers for the task-preserving compaction (see ../compaction.ts).
//
// Why Pi's default compaction loses the task (harness-parity P1, 13 of 14
// compacted sessions wrong):
//   * a split turn (one user message, then huge tool results) is cut so that the
//     tool results are KEPT verbatim and only the user message is summarized; the
//     session compacted 25,022 -> 24,890 tokens and the user's request vanished;
//   * the model then saw raw log text with no request above it and answered
//     "you've pasted a large block of log data" (5 of 14);
//   * the default serializer truncates each tool result to 2,000 characters, so
//     anything past the first ~25 lines of a file never reaches the summarizer.
// The replacement keeps every user request verbatim, labels every tool result as
// tool output, and extracts the lines of each large result that the request
// needs (checked against the real output, so nothing can be invented).

export const SUMMARY_MARKER = "[llm-router task-preserving compaction v1]";
const REQ_OPEN = "<<<USER_REQUEST";
const REQ_CLOSE = "USER_REQUEST>>>";

/** User requests carried by a previous summary written by this module. */
export function requestsFromPreviousSummary(summary) {
	if (typeof summary !== "string" || !summary.includes(SUMMARY_MARKER)) return [];
	const out = [];
	let i = 0;
	while (true) {
		const a = summary.indexOf(REQ_OPEN, i);
		if (a < 0) break;
		const b = summary.indexOf(REQ_CLOSE, a);
		if (b < 0) break;
		out.push(summary.slice(a + REQ_OPEN.length, b).replace(/^\n/, "").replace(/\n$/, ""));
		i = b + REQ_CLOSE.length;
	}
	return out;
}

/** Earlier steps carried by a previous summary (the whole "Done so far" section). */
export function stepsFromPreviousSummary(summary) {
	if (typeof summary !== "string" || !summary.includes(SUMMARY_MARKER)) return "";
	const a = summary.indexOf("## Done so far");
	const b = summary.indexOf("## Continue");
	if (a < 0 || b < 0 || b < a) return "";
	return summary
		.slice(a + "## Done so far".length, b)
		.split("\n")
		.filter((l) => !l.startsWith("Everything in this section is the OUTPUT OF TOOLS"))
		.join("\n")
		.trim();
}

/** Identifier-like tokens from a request that are worth grepping tool output for. */
export function distinctiveTokens(request) {
	const toks = new Set();
	const re = /[A-Za-z_][A-Za-z0-9_.-]*=|`([^`\n]{3,60})`|"([^"\n]{3,60})"|\b[A-Z][A-Z0-9_]{3,}\b/g;
	for (const m of String(request || "").matchAll(re)) {
		const t = (m[1] || m[2] || m[0]).trim();
		if (t.length >= 4) toks.add(t);
	}
	return [...toks];
}

/** Lines of `output` containing any of `tokens` (case-sensitive), at most `max`. */
export function grepLines(output, tokens, max = 20) {
	if (!tokens.length) return [];
	const hits = [];
	for (const line of String(output || "").split("\n")) {
		if (tokens.some((t) => line.includes(t))) {
			hits.push(line.slice(0, 400));
			if (hits.length >= max) break;
		}
	}
	return hits;
}

/** Split text on line boundaries into pieces of at most `maxChars` characters. */
export function chunkByLines(text, maxChars) {
	const lines = String(text || "").split("\n");
	const chunks = [];
	let cur = [];
	let size = 0;
	for (const line of lines) {
		const piece = line.length > maxChars ? line.slice(0, maxChars) : line;
		if (size + piece.length + 1 > maxChars && cur.length) {
			chunks.push(cur.join("\n"));
			cur = [];
			size = 0;
		}
		cur.push(piece);
		size += piece.length + 1;
	}
	if (cur.length) chunks.push(cur.join("\n"));
	return chunks;
}

/**
 * Keep only extracted lines that really occur in the tool output, plus NOTE: lines.
 * A model asked to "copy lines" sometimes paraphrases or invents; those are dropped.
 */
export function verifyExtract(extractText, output, maxLines = 30) {
	const kept = [];
	const notes = [];
	const out = String(output || "");
	for (let line of String(extractText || "").split("\n")) {
		line = line.replace(/^\s*(?:[-*>|]\s+|\d+[.)]\s+)/, "").replace(/^`+|`+$/g, "").trimEnd();
		if (!line.trim() || /^NONE\.?$/i.test(line.trim())) continue;
		if (/^NOTE:/i.test(line.trim())) {
			notes.push(line.trim().slice(0, 300));
			continue;
		}
		if (line.trim().length >= 3 && out.includes(line.trim()) && !kept.includes(line.trim())) kept.push(line.trim().slice(0, 400));
		if (kept.length >= maxLines) break;
	}
	return { lines: kept, notes: notes.slice(0, 3) };
}

function fence(text) {
	return String(text).replace(/```/g, "'''");
}

/**
 * Render the summary. `requests` are verbatim user texts; `steps` are
 * {call, isError, size, verbatim?, verbatimOmitted?, extracted?, notes?};
 * `assistantNotes` are short strings the assistant said along the way.
 */
export function renderSummary({ requests, previousSteps, steps, assistantNotes }) {
	const out = [SUMMARY_MARKER, ""];
	out.push("## Original user request(s), verbatim. This is still the task.");
	for (const r of requests) out.push(REQ_OPEN, r, REQ_CLOSE);
	out.push("");
	out.push("## Done so far");
	out.push("Everything in this section is the OUTPUT OF TOOLS the assistant called. None of it was written or pasted by the user.");
	if (previousSteps) out.push(previousSteps);
	let n = 0;
	for (const s of steps) {
		n++;
		out.push(`- ${s.call} -> TOOL OUTPUT${s.isError ? " (error)" : ""}, ${s.size}`);
		if (s.verbatim !== undefined) {
			out.push("  full tool output:", "  ```", fence(s.verbatim), "  ```");
		} else if (s.verbatimOmitted) {
			out.push("  (output omitted to fit the summary budget)");
		} else {
			if (s.extracted && s.extracted.length) {
				out.push("  lines of the tool output relevant to the request (copied verbatim):", "  ```", ...s.extracted.map(fence), "  ```");
			} else {
				out.push("  no line of this output was found relevant to the request.");
			}
			for (const note of s.notes || []) out.push(`  ${note}`);
		}
	}
	if (assistantNotes.length) {
		out.push("", "## What the assistant said along the way");
		for (const a of assistantNotes) out.push(`- ${a}`);
	}
	out.push("", "## Continue");
	out.push(
		"Continue the user's request above. The tool output above is evidence you gathered, not a message from the user. " +
			"If it already contains what the request needs, answer now; only re-run a tool if something needed is missing.",
	);
	return out.join("\n");
}
