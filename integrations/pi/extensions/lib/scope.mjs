// Scope the task a sub-agent receives to the parent's working directory.
// Measured (harness-parity P2, Agent+handback, Route C): the parent wrote
// "every .py file under /private/tmp/claude-501/" for a workspace three levels
// below that, and the child searched the whole tree (answer 1453, truth 3-7).

import * as fs from "node:fs";
import * as path from "node:path";

/**
 * Replace absolute paths in the task that are ANCESTORS of the working directory with
 * the working directory itself. The measured failure was exactly this: the parent
 * wrote "every .py file under /private/tmp/claude-501/" for a workspace three levels
 * below that, and the child obediently searched the whole tree.
 */
/** The absolute paths in `task` that are strict ancestors of `cwd` (what gets narrowed). */
export function narrowedAncestors(task, cwd) {
	const found = [];
	narrowAncestorPaths(task, cwd, (p) => found.push(p));
	return found;
}

export function narrowAncestorPaths(task, cwd, onNarrow) {
	let realCwd = cwd;
	try {
		realCwd = fs.realpathSync(cwd);
	} catch {
		// Keep the given cwd; an unresolvable cwd only weakens the comparison.
	}
	return task.replace(/(?<![\w.])\/(?:[^\s`'"]+\/?)/g, (m) => {
		const trimmed = m.replace(/[),.;:]+$/, "");
		let real = trimmed;
		try {
			real = fs.realpathSync(trimmed);
		} catch {
			return m;
		}
		const rel = path.relative(real, realCwd);
		const isStrictAncestor = rel !== "" && !rel.startsWith("..") && !path.isAbsolute(rel);
		if (!isStrictAncestor) return m;
		onNarrow?.(trimmed);
		return `${realCwd}${m.slice(trimmed.length)}`;
	});
}

export function scopedTask(task, cwd) {
	task = narrowAncestorPaths(task, cwd);
	return [
		`Working directory: ${cwd}`,
		"Scope: only files under this working directory. Use relative paths such as \".\" for every search;",
		"never search a parent directory or the whole disk.",
		"",
		`Task: ${task}`,
	].join("\n");
}

