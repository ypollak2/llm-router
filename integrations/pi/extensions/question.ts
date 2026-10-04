/**
 * llm-router Pi profile: the `question` tool (Claude Code's AskUserQuestion),
 * available in every mode, plus a guard against asking in prose.
 *
 * Measured (harness-parity P2, AskUserQuestion, Route C 0/2): Pi's example
 * question tool answers "UI not available" in print/JSON mode, and with the
 * tool offered qwen3.6 still ended its turn with a numbered choice in prose.
 *
 * Answer channel, first match wins:
 *   1. A UI (TUI or RPC client): a select dialog with the options.
 *   2. LLM_ROUTER_PI_ANSWER: a fixed answer (scripted and test runs).
 *   3. LLM_ROUTER_PI_ANSWER_CMD: a shell command; it receives the question as
 *      JSON on stdin and its stdout is the answer (route to chat, a file, ...).
 *   4. Nobody: the tool says so and tells the model to stop without acting.
 * Every question is appended to LLM_ROUTER_PI_QUESTION_LOG when that is set.
 *
 * Guard: when a run ends with a choice asked in prose (lib/prose_question.mjs)
 * and the question tool was never called, the run is continued once with an
 * instruction to call the tool. At most one nudge per user prompt.
 */

import { spawnSync } from "node:child_process";
import { appendFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { looksLikeProseChoiceQuestion } from "./lib/prose_question.mjs";

const QuestionParams = Type.Object({
	question: Type.String({ description: "The question to ask the user, one sentence." }),
	options: Type.Array(
		Type.Object({
			label: Type.String({ description: "Short label of the option" }),
			description: Type.Optional(Type.String({ description: "What happens if the user picks it" })),
		}),
		{ description: "The alternatives, at least two" },
	),
});

function logQuestion(row: unknown) {
	const path = process.env.LLM_ROUTER_PI_QUESTION_LOG;
	if (!path) return;
	try {
		appendFileSync(path, JSON.stringify(row) + "\n");
	} catch {
		// Logging must never decide whether the question is answered.
	}
}

function lastAssistantText(messages: any[]): string {
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i];
		if (m?.role !== "assistant") continue;
		const c = m.content;
		if (typeof c === "string") return c;
		return (c ?? []).filter((b: any) => b?.type === "text").map((b: any) => b.text).join("\n");
	}
	return "";
}

export default function (pi: ExtensionAPI) {
	let askedThisRun = false;
	let nudgedThisRun = false;

	// Reset per user prompt, not per agent run: the guard's own `continue: true` starts
	// a new agent run (agent_start fires again), and resetting there would let a model
	// that keeps answering in prose be nudged forever.
	pi.on("before_agent_start", async () => {
		askedThisRun = false;
		nudgedThisRun = false;
	});

	pi.registerTool({
		name: "question",
		label: "Question",
		description:
			"Ask the user to choose between alternatives and wait for the answer. Call this BEFORE acting whenever " +
			"the request leaves a choice open and any alternative is destructive or hard to undo (delete, overwrite, " +
			"rename, move, reset). Never ask such a question in plain text.",
		parameters: QuestionParams,
		executionMode: "sequential",
		async execute(_id: string, params: any, _signal: any, _onUpdate: any, ctx: any) {
			askedThisRun = true;
			const labels: string[] = (params.options ?? []).map((o: any) => String(o.label));
			const row: any = { ts: Date.now(), question: params.question, options: params.options, cwd: ctx?.cwd };
			let answer: string | undefined;
			let channel = "none";
			if (labels.length < 2) {
				logQuestion({ ...row, channel: "rejected" });
				throw new Error("question needs at least two options");
			}
			if (ctx?.hasUI) {
				const picked = await ctx.ui.select(params.question, labels);
				if (picked !== undefined && picked !== null) {
					answer = String(picked);
					channel = "ui";
				}
			} else if (process.env.LLM_ROUTER_PI_ANSWER) {
				answer = process.env.LLM_ROUTER_PI_ANSWER;
				channel = "env";
			} else if (process.env.LLM_ROUTER_PI_ANSWER_CMD) {
				const r = spawnSync("/bin/sh", ["-c", process.env.LLM_ROUTER_PI_ANSWER_CMD], {
					input: JSON.stringify({ question: params.question, options: params.options }),
					encoding: "utf-8",
					timeout: 30 * 60 * 1000,
				});
				if (r.status === 0 && r.stdout.trim()) {
					answer = r.stdout.trim();
					channel = "cmd";
				}
			}
			logQuestion({ ...row, channel, answer: answer ?? null });
			if (answer === undefined) {
				return {
					content: [
						{
							type: "text",
							text:
								"No user is available to answer (non-interactive run without an answer channel). " +
								"Do NOT carry out any of the options. Stop now and end your reply with the question and its options.",
						},
					],
					details: { question: params.question, options: labels, answer: null, channel },
				};
			}
			return {
				content: [{ type: "text", text: `The user answered: ${answer}` }],
				details: { question: params.question, options: labels, answer, channel },
			};
		},
	});

	pi.on("agent_before_settle", async (event: any) => {
		if (askedThisRun || nudgedThisRun || event.outcome !== "completed") return undefined;
		if (!pi.getActiveTools().includes("question")) return undefined;
		const text = lastAssistantText(event.context?.contextMessages ?? []);
		if (!looksLikeProseChoiceQuestion(text)) return undefined;
		nudgedThisRun = true;
		return {
			entries: [
				{
					type: "custom_message",
					customType: "llm-router-question-guard",
					display: false,
					content:
						"[llm-router] You asked the user to choose in plain text. Plain-text questions cannot be answered in this " +
						"session. Call the `question` tool now with that question and its options, then act on the answer.",
				},
			],
			continue: true,
		};
	});
}
