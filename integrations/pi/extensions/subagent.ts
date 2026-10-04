/**
 * llm-router Pi profile: the `subagent` tool (Claude Code's Agent/Task).
 *
 * Measured (harness-parity P2, Agent+handback, Route C 0/2 with Pi's example
 * subagent extension): qwen3.6 either skipped the tool or handed the child a
 * task scoped to the wrong directory ("every .py file under /private/tmp/
 * claude-501/", answer 1453 instead of 3-7), because the child is told only the
 * task text and the model guessed the directory.
 *
 * This tool is a small, local-first rewrite of that example:
 *   * the child ALWAYS runs in the parent's working directory, and the task it
 *     receives starts with that directory and a scope rule; a model-supplied
 *     `cwd` is honoured only when it is inside the working directory;
 *   * one child at a time (a single local model serves both; parallel children
 *     would only queue on Ollama);
 *   * the child gets the profile's safety extensions (paths, cancel, context
 *     guard, compaction) but not `subagent` or `question`, so it cannot recurse
 *     or block on a question nobody sees;
 *   * aborting the parent sends SIGINT to the child's process group (so the child's
 *     own cancel.ts kills its shell commands), then SIGKILL after 3 s; children
 *     still running when this process exits get the same SIGINT.
 * Agent definitions are Markdown files in $PI_CODING_AGENT_DIR/agents
 * (integrations/pi/agent/agents/ ships `worker` and `scout`).
 */

import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { getAgentDir, parseFrontmatter } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { narrowedAncestors, scopedTask } from "./lib/scope.mjs";

interface AgentDef {
	name: string;
	description: string;
	tools?: string[];
	systemPrompt: string;
}

function loadAgents(): AgentDef[] {
	const dir = path.join(getAgentDir(), "agents");
	let names: string[] = [];
	try {
		names = fs.readdirSync(dir).filter((n) => n.endsWith(".md"));
	} catch {
		return [];
	}
	const out: AgentDef[] = [];
	for (const n of names) {
		try {
			const { frontmatter, body } = parseFrontmatter<Record<string, unknown>>(fs.readFileSync(path.join(dir, n), "utf-8"));
			if (typeof frontmatter.name !== "string" || typeof frontmatter.description !== "string") continue;
			const rawTools = frontmatter.tools;
			const tools = (Array.isArray(rawTools) ? rawTools : typeof rawTools === "string" ? rawTools.split(",") : [])
				.map((t) => String(t).trim())
				.filter(Boolean);
			out.push({ name: frontmatter.name, description: frontmatter.description, tools: tools.length ? tools : undefined, systemPrompt: body });
		} catch {
			// One malformed agent file must not hide the others.
		}
	}
	return out;
}

function isInside(child: string, parent: string): boolean {
	const rel = path.relative(parent, child);
	return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
}

function finalText(messages: any[]): string {
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i];
		if (m?.role !== "assistant") continue;
		const t = (m.content ?? []).filter((b: any) => b?.type === "text").map((b: any) => b.text).join("\n").trim();
		if (t) return t;
	}
	return "";
}

const Params = Type.Object({
	task: Type.String({ description: "The complete task for the sub-agent, with everything it needs to know." }),
	agent: Type.Optional(Type.String({ description: 'Agent to use. Default "worker".' })),
	cwd: Type.Optional(Type.String({ description: "Sub-directory of the working directory to run in. Default: the working directory." })),
});

/** Process groups of running sub-agents, so they can be stopped when this process exits. */
const liveChildren = new Set<number>();
let exitHookInstalled = false;

function interruptGroup(pid: number) {
	try {
		// SIGINT, not SIGKILL: the child's own cancel.ts must run to kill ITS shell commands,
		// which live in their own process groups.
		process.kill(-pid, "SIGINT");
	} catch {
		// Already gone.
	}
}

export default function (pi: ExtensionAPI) {
	// A sub-agent never gets a subagent tool, even if someone loads this file into one.
	if (process.env.LLM_ROUTER_PI_IS_SUBAGENT === "1") return;
	if (!exitHookInstalled) {
		exitHookInstalled = true;
		process.on("exit", () => {
			for (const pid of liveChildren) interruptGroup(pid);
		});
	}
	const agents = loadAgents();
	const agentList = agents.map((a) => `${a.name}: ${a.description}`).join("; ") || "worker";

	pi.registerTool({
		name: "subagent",
		label: "Subagent",
		description:
			"Delegate one self-contained task to a sub-agent with its own context window; you get back only its final answer. " +
			"Use it when the user asks for a sub-agent, and for searches or reads whose raw output you do not need. " +
			`It runs in the current working directory. Agents: ${agentList}.`,
		parameters: Params,
		executionMode: "sequential",
		async execute(_id: string, params: any, signal: AbortSignal | undefined, onUpdate: any, ctx: any) {
			const agentName = params.agent || "worker";
			const agent = agents.find((a) => a.name === agentName);
			if (!agent) throw new Error(`Unknown agent "${agentName}". Available: ${agents.map((a) => a.name).join(", ") || "none"}`);
			const root = ctx.cwd;
			let runCwd = root;
			if (params.cwd) {
				const cand = path.resolve(root, params.cwd);
				if (isInside(fs.existsSync(cand) ? fs.realpathSync(cand) : cand, fs.realpathSync(root)) && fs.existsSync(cand)) runCwd = cand;
			}
			const args = ["--mode", "json", "-p", "--no-session"];
			if (process.env.PI_OFFLINE === "1") args.push("--offline");
			if (ctx.model) args.push("--provider", ctx.model.provider, "--model", ctx.model.id);
			if (agent.tools) args.push("--tools", agent.tools.join(","));
			for (const ext of (process.env.LLM_ROUTER_PI_CHILD_EXTENSIONS ?? "").split(path.delimiter).filter(Boolean)) {
				args.push("-e", ext);
			}
			let tmpDir: string | undefined;
			if (agent.systemPrompt.trim()) {
				tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "llmr-pi-sub-"));
				const p = path.join(tmpDir, "agent.md");
				fs.writeFileSync(p, agent.systemPrompt, { mode: 0o600 });
				args.push("--append-system-prompt", p);
			}
			const narrowed = narrowedAncestors(params.task, runCwd);
			args.push(scopedTask(params.task, runCwd));

			const messages: any[] = [];
			let stderr = "";
			let aborted = false;
			try {
				const code = await new Promise<number>((resolve) => {
					const proc = spawn(process.execPath, [process.argv[1], ...args], {
						cwd: runCwd,
						detached: true,
						stdio: ["ignore", "pipe", "pipe"],
						env: { ...process.env, LLM_ROUTER_PI_IS_SUBAGENT: "1" },
					});
					let buf = "";
					proc.stdout.on("data", (d) => {
						buf += d.toString();
						const lines = buf.split("\n");
						buf = lines.pop() ?? "";
						for (const line of lines) {
							try {
								const ev = JSON.parse(line);
								if (ev.type === "message_end" && ev.message) {
									messages.push(ev.message);
									onUpdate?.({ content: [{ type: "text", text: finalText(messages) || "(sub-agent working...)" }], details: undefined });
								}
							} catch {
								// Non-JSON output lines are not events.
							}
						}
					});
					proc.stderr.on("data", (d) => {
						stderr += d.toString();
					});
					if (proc.pid) liveChildren.add(proc.pid);
					const done = (c: number) => {
						if (proc.pid) liveChildren.delete(proc.pid);
						resolve(c);
					};
					proc.on("close", (c) => done(c ?? 1));
					proc.on("error", () => done(1));
					const kill = () => {
						aborted = true;
						if (!proc.pid) return;
						interruptGroup(proc.pid);
						const t = setTimeout(() => {
							try {
								process.kill(-proc.pid!, "SIGKILL");
							} catch {
								// Exited after SIGINT, as it should.
							}
						}, 3000);
						t.unref();
					};
					if (signal?.aborted) kill();
					else signal?.addEventListener("abort", kill, { once: true });
				});
				if (aborted) throw new Error("Sub-agent was aborted");
				const answer = finalText(messages);
				const last = [...messages].reverse().find((m) => m?.role === "assistant");
				if (code !== 0 || last?.stopReason === "error" || !answer) {
					throw new Error(`Sub-agent "${agentName}" failed (exit ${code}): ${last?.errorMessage || stderr.slice(-500) || "no answer"}`);
				}
				const note = narrowed.length
					? `\n[llm-router] the task named ${narrowed.join(", ")}, above the working directory; the sub-agent searched the working directory instead.`
					: "";
				return {
					content: [{ type: "text", text: answer + note }],
					details: { agent: agentName, cwd: runCwd, turns: messages.filter((m) => m?.role === "assistant").length },
				};
			} finally {
				if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
			}
		},
	});
}
