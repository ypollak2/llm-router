/**
 * llm-router Pi profile: print the session's statusline snapshot line after each agent run (P35-3).
 *
 * The line is the same text `llm-router statusline` prints as its second row and the Codex Stop hook
 * prints as its systemMessage: it is rendered once, into snapshots/pi-<session>.json, and read here.
 * The logic is in lib/summary.mjs so `node --test` and the mock harness run the real code.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { register } from "./lib/summary.mjs";

export default function (pi: ExtensionAPI) {
	register(pi);
}
