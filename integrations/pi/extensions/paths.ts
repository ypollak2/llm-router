/**
 * llm-router Pi profile: repair write/edit paths before the tool runs.
 *
 * Measured (harness-parity P2, Route C, write 7/9): qwen3.6 wrote
 * "private/tmp/.../notes.txt" (leading slash dropped, so it landed under cwd)
 * and "/tmp/.../c_write_08_54i0b61f/notes.txt" (one character of a long cwd
 * retyped wrong; the write tool created the missing directory and reported
 * success). The rules live in lib/paths.mjs. A path is only rewritten when the
 * result is inside the working directory and the original target directory does
 * not exist; the tool result then says what was changed, so nothing is silent.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { repairToolPath } from "./lib/paths.mjs";

const PATH_TOOLS = new Set(["write", "edit"]);

export default function (pi: ExtensionAPI) {
	const notes = new Map<string, string>();

	pi.on("tool_call", async (event: any, ctx: any) => {
		if (!PATH_TOOLS.has(event.toolName)) return undefined;
		const input = event.input ?? {};
		const key = typeof input.path === "string" ? "path" : typeof input.file_path === "string" ? "file_path" : null;
		if (!key) return undefined;
		const r = repairToolPath(input[key], ctx.cwd);
		if (r.changed) {
			notes.set(event.toolCallId, `[llm-router] path "${input[key]}" was rewritten to "${r.path}": ${r.reason}.`);
			input[key] = r.path;
		}
		return undefined;
	});

	pi.on("tool_result", async (event: any) => {
		const note = notes.get(event.toolCallId);
		if (!note) return undefined;
		notes.delete(event.toolCallId);
		return { content: [...(event.content ?? []), { type: "text", text: `\n${note}` }] };
	});
}
