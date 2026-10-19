// Mock Pi for the end-of-turn line (P35-3). Loads the REAL extension logic (extensions/lib/summary.mjs, the
// module summary.ts registers), fires `agent_end` the way Pi does and prints what Pi would show.
// Usage: node summary_harness.mjs <session-id|-> ; env: LLM_ROUTER_HOME, PI_HARNESS_UI=1|0.
// Prints one JSON object: {"notices": [...], "stderr": "...", "spawned": [[cmd, args], ...]}.

import { register } from "../extensions/lib/summary.mjs";

const handlers = {};
const pi = { on: (name, fn) => (handlers[name] = fn) };
const notices = [];
const spawned = [];
let stderr = "";
process.stderr.write = (chunk) => { stderr += String(chunk); return true; };

register(pi, {
	spawnFn: (cmd, args) => { spawned.push([cmd, args]); return { on() {}, unref() {} }; },
});

const session = process.argv[2] === "-" ? undefined : process.argv[2];
const ui = process.env.PI_HARNESS_UI === "1";
await handlers.agent_end?.({ type: "agent_end" }, {
	hasUI: ui,
	ui: { notify: (m, kind) => notices.push([m, kind]) },
	sessionManager: { getSessionId: () => session },
});
process.stdout.write(JSON.stringify({ notices, stderr, spawned }) + "\n");
