/**
 * llm-router Pi profile: SIGINT cancels running shell commands.
 *
 * Bug (Pi 0.99.1, print and JSON modes): the bash tool starts every command
 * in its own process group (detached) and kills that group when the run is
 * aborted, and print mode kills tracked children on SIGTERM and SIGHUP. It
 * registers nothing for SIGINT, so Ctrl-C (or `kill -INT`) uses Node's default
 * and exits at once, and the detached command keeps running. Measured in the
 * harness-parity probe (cancel, 0/2): `sleep 31 && touch after.txt` still
 * created after.txt after Pi had exited. Reproduced without a model against a
 * scripted server: integrations/pi/upstream/repro_pi_core.py sigint.
 *
 * Fix, from an extension: on SIGINT, abort the active run. Aborting fires the
 * bash tool's own abort path, which SIGKILLs the command's process group.
 * Then exit with 130 (128 + SIGINT), as Node's default would have.
 * Only in non-interactive modes: the TUI owns Ctrl-C itself.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	let latestCtx: any;
	let installed = false;
	let exiting = false;

	const remember = (_event: unknown, ctx: any) => {
		latestCtx = ctx;
	};
	pi.on("agent_start", remember);
	pi.on("turn_start", remember);
	pi.on("tool_call", remember);

	pi.on("session_start", async (_event, ctx: any) => {
		latestCtx = ctx;
		if (installed || ctx.mode === "tui") return;
		installed = true;
		process.on("SIGINT", () => {
			if (exiting) return;
			exiting = true;
			try {
				latestCtx?.abort?.();
			} catch {
				// Exiting anyway; the abort path is best effort and the exit below is not.
			}
			process.stderr.write("[llm-router] SIGINT: aborted the run and its shell commands\n");
			// Let the abort listeners (which kill the process groups) run first.
			setTimeout(() => process.exit(130), 100);
		});
	});
}
