/**
 * llm-router Pi profile: task-preserving compaction.
 *
 * Replaces Pi's default compaction summary (see lib/compaction.mjs for the
 * measured failure: 13 of 14 compacted sessions ended wrong). The summary this
 * writes:
 *   1. carries every user request VERBATIM, across repeated compactions (only real
 *      user messages: not `!command` output, not extension messages);
 *   2. lists every tool call with its output labelled as TOOL OUTPUT;
 *   3. keeps small tool outputs verbatim, and for large ones keeps only the lines
 *      the request needs: the local model is asked to copy them out (one bounded
 *      call per chunk, thinking off), every returned line is checked to really
 *      occur in the output, and lines matching identifiers from the request are
 *      added by plain search as a safety net;
 *   4. retains nothing else (firstKeptEntryId = null), so the context really
 *      shrinks; Pi's default kept the huge tool results verbatim and summarized
 *      the user's request away (25,022 -> 24,890 tokens in one measured case);
 *   5. is held to a budget (a quarter of the window): verbatim tool bodies, then
 *      the oldest carried steps, are cut to their call line first.
 * If the model call fails, the summary is still written from the safety-net
 * search. This never falls back to Pi's default summary.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { convertToLlm } from "@earendil-works/pi-coding-agent";
import {
	chunkByLines,
	distinctiveTokens,
	grepLines,
	renderSummary,
	requestsFromPreviousSummary,
	stepsFromPreviousSummary,
	verifyExtract,
} from "./lib/compaction.mjs";
import { recordEvent } from "./lib/events.mjs";
import { estimateTextTokens } from "./lib/tokens.mjs";

const VERBATIM_MAX_CHARS = Number(process.env.LLM_ROUTER_PI_COMPACT_VERBATIM_CHARS || 1200);
const CHUNK_TOKENS_CAP = Number(process.env.LLM_ROUTER_PI_COMPACT_CHUNK_TOKENS || 16000);
// Request, instructions and the reply around one chunk of tool output.
const EXTRACTION_OVERHEAD_TOKENS = 6000;

function textOf(content: any): string {
	if (typeof content === "string") return content;
	return (content ?? [])
		.map((b: any) => (b?.type === "text" ? b.text : b?.type === "image" ? "[image]" : ""))
		.join("");
}

function describeCall(name: string, args: any): string {
	const parts = Object.entries(args ?? {}).map(([k, v]) => {
		let s = typeof v === "string" ? v : JSON.stringify(v);
		if (s.length > 160) s = s.slice(0, 160) + "...";
		return `${k}=${JSON.stringify(s)}`;
	});
	return `${name}(${parts.join(", ")})`;
}

/** Every message in the current context: the span Pi would summarize plus the span it would keep. */
function collectMessages(preparation: any, branchEntries: any[]): any[] {
	const msgs = [...(preparation.messagesToSummarize ?? []), ...(preparation.turnPrefixMessages ?? [])];
	const startIdx = branchEntries.findIndex((e: any) => e.id === preparation.firstKeptEntryId);
	if (startIdx >= 0) {
		for (const e of branchEntries.slice(startIdx)) {
			if (e.type === "message" && e.message) msgs.push(e.message);
		}
	}
	return msgs;
}

async function extractRelevant(ctx: any, signal: AbortSignal, request: string, call: string, output: string): Promise<{ lines: string[]; error?: string }> {
	const window = Number(ctx?.model?.contextWindow || 32768);
	const chunkTokens = Math.max(2000, Math.min(CHUNK_TOKENS_CAP, window - EXTRACTION_OVERHEAD_TOKENS - estimateTextTokens(request)));
	const maxChars = Math.max(2000, Math.floor((chunkTokens / Math.max(1, estimateTextTokens(output))) * output.length));
	const chunks = chunkByLines(output, maxChars);
	const lines: string[] = [];
	// Thinking adds minutes and nothing to a copy job. Ollama's /v1 maps
	// reasoning_effort "none" to think=false (measured: 250 -> 5 completion tokens).
	const noThinking = ctx?.model?.provider === "ollama" ? (payload: any) => ({ ...payload, reasoning_effort: "none" }) : undefined;
	for (let i = 0; i < chunks.length; i++) {
		if (signal?.aborted) break;
		const prompt =
			`USER REQUEST (verbatim):\n<<<\n${request}\n>>>\n\n` +
			`The assistant called the tool ${call}. Below is part ${i + 1} of ${chunks.length} of that tool's OUTPUT. ` +
			"It was produced by the tool, not written by the user.\n" +
			`<<<TOOL_OUTPUT\n${chunks[i]}\nTOOL_OUTPUT>>>\n\n` +
			"Copy, verbatim and one per line, every line of this tool output that the user request needs (at most 30 lines). " +
			"Copy nothing else. If nothing in this part is relevant, reply exactly: NONE";
		const response = await ctx.modelRegistry.complete(
			ctx.model,
			{
				systemPrompt: "You copy evidence out of tool output for another agent. You copy lines exactly; you never invent or reword them.",
				messages: [{ role: "user", content: [{ type: "text", text: prompt }], timestamp: Date.now() }],
			},
			{ maxTokens: 2048, signal, cacheRetention: "none", ...(noThinking ? { onPayload: noThinking } : {}) },
		);
		if (response?.stopReason === "error") return { lines, error: response.errorMessage || "extraction call failed" };
		const text = (response?.content ?? []).filter((b: any) => b?.type === "text").map((b: any) => b.text).join("\n");
		for (const l of verifyExtract(text, chunks[i]).lines) if (!lines.includes(l) && lines.length < 30) lines.push(l);
	}
	return { lines };
}

/** Shrink the summary inputs until the rendered summary fits `budget` tokens. */
function fitToBudget(input: any, budget: number): { summary: string; trimmed: number } {
	let summary = renderSummary(input);
	let trimmed = 0;
	const steps = input.steps;
	// 1. Largest verbatim bodies become call lines.
	const verbatim = steps.filter((s: any) => s.verbatim !== undefined).sort((a: any, b: any) => b.verbatim.length - a.verbatim.length);
	for (const s of verbatim) {
		if (estimateTextTokens(summary) <= budget) break;
		s.extracted = [];
		s.verbatimOmitted = true;
		delete s.verbatim;
		trimmed++;
		summary = renderSummary(input);
	}
	// 2. Oldest carried steps go, line by line.
	while (estimateTextTokens(summary) > budget && input.previousSteps) {
		const ls = input.previousSteps.split("\n");
		ls.splice(0, Math.max(1, Math.ceil(ls.length / 4)));
		input.previousSteps = ls.length ? "(earlier steps omitted)\n" + ls.join("\n").replace(/^\(earlier steps omitted\)\n/, "") : "";
		trimmed++;
		summary = renderSummary(input);
	}
	// 3. Extracted lines shrink to 5 per step.
	if (estimateTextTokens(summary) > budget) {
		for (const s of steps) if (s.extracted && s.extracted.length > 5) s.extracted = s.extracted.slice(0, 5);
		trimmed++;
		summary = renderSummary(input);
	}
	return { summary, trimmed };
}

export default function (pi: ExtensionAPI) {
	pi.on("session_before_compact", async (event: any, ctx: any) => {
		const { preparation, branchEntries, signal } = event;
		const raw = collectMessages(preparation, branchEntries ?? []);

		// User requests come only from real user messages. convertToLlm also turns
		// `!command` output and extension messages into role "user"; those are not requests.
		const requests = requestsFromPreviousSummary(preparation.previousSummary);
		for (const m of raw) {
			if (m?.role !== "user") continue;
			const t = textOf(m.content).trim();
			if (t && !requests.includes(t)) requests.push(t);
		}

		const steps: any[] = [];
		const assistantNotes: string[] = [];
		const callById = new Map<string, string>();
		const results: any[] = [];
		for (const m of convertToLlm(raw) as any[]) {
			if (m.role === "assistant") {
				for (const b of m.content ?? []) {
					if (b?.type === "toolCall") callById.set(b.id, describeCall(b.name, b.arguments));
					else if (b?.type === "text" && b.text.trim()) assistantNotes.push(b.text.trim().replace(/\s+/g, " ").slice(0, 300));
				}
			} else if (m.role === "toolResult") {
				results.push(m);
			}
		}

		const requestText = requests.join("\n\n");
		const grepTokens = distinctiveTokens(requestText);
		let extractionError: string | undefined;
		for (const m of results) {
			const call = callById.get(m.toolCallId) ?? `${m.toolName}(...)`;
			const out = textOf(m.content);
			const size = `${out.split("\n").length} lines, ${out.length} chars`;
			if (out.length <= VERBATIM_MAX_CHARS) {
				steps.push({ call, isError: !!m.isError, size, verbatim: out });
				continue;
			}
			let ex: { lines: string[]; error?: string } = { lines: [] };
			if (!extractionError) {
				try {
					ex = await extractRelevant(ctx, signal, requestText, call, out);
					if (ex.error) extractionError = ex.error;
				} catch (err) {
					extractionError = err instanceof Error ? err.message : String(err);
				}
			}
			const merged = [...ex.lines];
			for (const l of grepLines(out, grepTokens)) if (!merged.includes(l.trim())) merged.push(l.trim());
			steps.push({ call, isError: !!m.isError, size, extracted: merged.slice(0, 30) });
		}
		if (signal?.aborted) return { cancel: true };
		if (extractionError) recordEvent("compaction_extraction_failed", { error: extractionError.slice(0, 300), cwd: ctx?.cwd });

		const window = Number(ctx?.model?.contextWindow || 32768);
		const budget = Math.min(8000, Math.floor(window / 4));
		const { summary, trimmed } = fitToBudget(
			{
				requests,
				previousSteps: stepsFromPreviousSummary(preparation.previousSummary),
				steps,
				assistantNotes: assistantNotes.slice(-6),
			},
			budget,
		);
		return {
			compaction: {
				summary,
				firstKeptEntryId: null as unknown as string, // retain nothing: Pi records the compaction's own id
				tokensBefore: preparation.tokensBefore,
				details: {
					llmRouter: "task-preserving-v2",
					requests: requests.length,
					steps: steps.length,
					extractionError: extractionError ?? null,
					summaryTokensEstimate: estimateTextTokens(summary),
					summaryBudget: budget,
					trimmed,
				},
			},
		};
	});
}
