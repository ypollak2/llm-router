// End-of-turn line (P35-3): print the session's statusline snapshot line after each agent run.
//
// `llm-router`'s statusline_snapshot renders the line once into
// <router home>/snapshots/pi-<session>.json; the Codex Stop hook and the status line read the same
// kind of file. This module only READS that file (no llm_router import: this is Node) and starts a
// detached `llm-router statusline --refresh --host pi --session <id>` so the next turn has a fresh line.
// A snapshot that is absent, unreadable or older than 15 minutes prints "n/a": unknown is never a number.

import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export const HOST = "pi";
export const STALE_S = 15 * 60;
export const REFRESH_MIN_S = 10;
export const STAMP_DIR = ".pi-summary-refresh.d"; // F111: one stamp per session (the old single stamp starved a second session)
export const STAMP_KEEP_S = 3600;
export const STAMP_MAX = 64;
export const NA = "n/a";
const PLAIN_ID = /^[A-Za-z0-9._-]{1,128}$/;

/** The last `KEY=value` (or `export KEY=value`) line of a regular file, else "". Only regular files: a FIFO would block. */
export function dotenvValue(file, key) {
	try {
		if (!fs.statSync(file).isFile()) return "";
		let found = "";
		const re = new RegExp(`^\\s*(?:export\\s+)?${key}\\s*=\\s*(.*?)\\s*$`);
		for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
			const m = re.exec(line);
			if (m) found = m[1].replace(/^["']+|["']+$/g, "").trim();
		}
		return found;
	} catch {
		return "";
	}
}

/**
 * The state home, resolved as the Python package does (F111): LLM_ROUTER_HOME from the real environment, else from
 * ~/.llm-router/.env, else from ~/.env, else ~/.llm-router. This module is Node and cannot call the Python
 * env loader, so it reads that one key itself.
 */
export function routerHome(env = process.env, homedir = os.homedir()) {
	const fallback = path.join(homedir, ".llm-router");
	let o = (env.LLM_ROUTER_HOME ?? "").trim();
	if (!o) {
		for (const f of [path.join(fallback, ".env"), path.join(homedir, ".env")]) {
			o = dotenvValue(f, "LLM_ROUTER_HOME");
			if (o) break;
		}
	}
	if (!o) return fallback;
	return o.startsWith("~") ? path.join(homedir, o.slice(1)) : o;
}

export function snapshotPath(home, session) {
	return path.join(home, "snapshots", `${HOST}-${String(session).replace(/[^A-Za-z0-9._-]/g, "_")}.json`);
}

/** The snapshot's pre-rendered line, or "n/a" (no session, no file, unreadable, other schema, stale). */
export function readLine(home, session, nowS = Date.now() / 1000, onProblem = () => {}) {
	if (!session) return NA;
	let data;
	try {
		data = JSON.parse(fs.readFileSync(snapshotPath(home, session), "utf8"));
	} catch (e) {
		if (e?.code !== "ENOENT") onProblem(`snapshot unreadable (${e?.name ?? "error"})`);
		return NA;
	}
	if (data === null || typeof data !== "object" || (data.v ?? 1) !== 1) return NA;
	const at = data.written_at;
	if (typeof at !== "number" || !Number.isFinite(at) || typeof data.line !== "string" || !data.line) return NA;
	const age = nowS - at;
	return age >= 0 && age <= STALE_S ? data.line : NA;
}

/** argv for the detached refresher; LLM_ROUTER_STATUSLINE_REFRESH_CMD (space-separated) replaces the prefix. */
export function refreshArgv(session, env = process.env) {
	if (!PLAIN_ID.test(String(session))) return null;
	const override = (env.LLM_ROUTER_STATUSLINE_REFRESH_CMD ?? "").trim();
	const prefix = override ? override.split(/\s+/) : ["llm-router", "statusline", "--refresh"];
	return [...prefix, "--host", HOST, "--session", String(session)];
}

/** Drop stamps older than STAMP_KEEP_S, then the oldest beyond STAMP_MAX. */
export function cleanStamps(dir, nowS) {
	const entries = [];
	for (const name of fs.readdirSync(dir)) {
		try {
			entries.push([fs.statSync(path.join(dir, name)).mtimeMs / 1000, path.join(dir, name)]);
		} catch {
			// vanished: skip
		}
	}
	entries.sort((a, b) => a[0] - b[0]);
	const fresh = entries.filter((e) => nowS - e[0] <= STAMP_KEEP_S);
	const doomed = [...entries.filter((e) => nowS - e[0] > STAMP_KEEP_S), ...fresh.slice(0, Math.max(0, fresh.length - STAMP_MAX))];
	for (const [, f] of doomed) {
		try {
			fs.unlinkSync(f);
		} catch {
			// already gone
		}
	}
}

/** Start one refresher per session unless that session started one in the last REFRESH_MIN_S seconds. Never waits, never throws. */
export function maybeRefresh(home, session, nowS, { env = process.env, spawnFn = spawn, onProblem = () => {} } = {}) {
	try {
		const argv = refreshArgv(session, env);
		if (!argv) {
			onProblem(`session id ${JSON.stringify(session)} is not a plain id: snapshot not refreshed`);
			return false;
		}
		const stampDir = path.join(home, STAMP_DIR);
		const stamp = path.join(stampDir, `s-${String(session).replace(/[^A-Za-z0-9._-]/g, "_")}`);
		try {
			if (nowS - fs.statSync(stamp).mtimeMs / 1000 < REFRESH_MIN_S) return false;
		} catch {
			// no stamp yet: due
		}
		fs.mkdirSync(stampDir, { recursive: true, mode: 0o700 });
		fs.closeSync(fs.openSync(stamp, "w", 0o600));
		fs.utimesSync(stamp, nowS, nowS);
		try {
			cleanStamps(stampDir, nowS);
		} catch (e) {
			onProblem(`stamp cleanup failed (${e?.name})`);
		}
		const child = spawnFn(argv[0], argv.slice(1), { detached: true, stdio: "ignore" });
		child.on?.("error", (e) => onProblem(`refresh not started (${e?.code ?? e?.name}): is llm-router on PATH?`));
		child.unref?.();
		return true;
	} catch (e) {
		onProblem(`refresh not started (${e?.name}: ${e?.message})`);
		return false;
	}
}

/** Wire the extension: after each agent run print the line (UI notice, else stderr) and refresh for the next. */
export function register(pi, { env = process.env, now = () => Date.now() / 1000, spawnFn = spawn } = {}) {
	const warn = (m) => process.stderr.write(`[llm-router] summary: ${m}\n`);
	pi.on("agent_end", async (_event, ctx) => {
		let session;
		try {
			session = ctx?.sessionManager?.getSessionId?.();
		} catch (e) {
			warn(`no session id (${e?.name})`);
		}
		const home = routerHome(env);
		const t = now();
		const line = readLine(home, session, t, warn);
		if (ctx?.hasUI && ctx.ui?.notify) ctx.ui.notify(line, "info");
		else process.stderr.write(`${line}\n`); // stdout is the model's output in -p / json mode
		maybeRefresh(home, session, t, { env, spawnFn, onProblem: warn });
		return undefined;
	});
}
