// Path repair for the write and edit tools.
//
// Measured on qwen3.6 in Pi (harness-parity P2, Route C, write 7/9): the model
// writes the file somewhere other than the working directory in two ways.
//   1. It drops the leading slash: "private/tmp/.../ws/notes.txt" is then resolved
//      against cwd and lands in cwd/private/tmp/.../ws/notes.txt.
//   2. It retypes a long absolute cwd and gets one character wrong
//      ("..._54i0b61f" for "..._54i0b6lf"). The write tool creates the missing
//      directory, so the file lands in a fresh sibling directory and the write
//      "succeeds".
// Both are repaired here, conservatively: a path is only rewritten when the
// result is inside the working directory and the original target directory does
// not exist. Anything else is left exactly as the model wrote it.
//
// Pure functions with the filesystem passed in, so they can be tested without Pi.

import * as nodeFs from "node:fs";
import * as nodePath from "node:path";

/** Levenshtein distance, capped: returns cap+1 as soon as the distance exceeds cap. */
export function editDistance(a, b, cap = 3) {
	if (Math.abs(a.length - b.length) > cap) return cap + 1;
	let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
	for (let i = 1; i <= a.length; i++) {
		const cur = [i];
		let rowMin = i;
		for (let j = 1; j <= b.length; j++) {
			const v = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
			cur.push(v);
			if (v < rowMin) rowMin = v;
		}
		if (rowMin > cap) return cap + 1;
		prev = cur;
	}
	return prev[b.length];
}

function defaultFs() {
	return {
		exists: (p) => nodeFs.existsSync(p),
		isDir: (p) => {
			try {
				return nodeFs.statSync(p).isDirectory();
			} catch {
				return false;
			}
		},
		realpath: (p) => {
			try {
				return nodeFs.realpathSync(p);
			} catch {
				return null;
			}
		},
	};
}

/** Real path of `p`, resolving symlinks (/tmp -> /private/tmp) on the longest existing prefix. */
export function canonical(p, fs = defaultFs()) {
	const abs = nodePath.resolve(p);
	let head = abs;
	const tail = [];
	while (true) {
		const real = fs.realpath(head);
		if (real) return tail.length ? nodePath.join(real, ...tail.reverse()) : real;
		const parent = nodePath.dirname(head);
		if (parent === head) return abs;
		tail.push(nodePath.basename(head));
		head = parent;
	}
}

function isInside(child, parent) {
	const rel = nodePath.relative(parent, child);
	return rel === "" || (!rel.startsWith("..") && !nodePath.isAbsolute(rel));
}

/**
 * Decide where a write/edit path should really point.
 * Returns {path, changed, reason}. `path` is what the tool should use.
 */
export function repairToolPath(raw, cwd, fs = defaultFs(), maxSegmentDistance = 2) {
	if (typeof raw !== "string" || raw.trim() === "") return { path: raw, changed: false, reason: "" };
	let p = raw.trim();
	const realCwd = canonical(cwd, fs);

	// Case 1: an absolute path with its leading slash dropped. Only when "/" + path is
	// inside the working directory: "tmp/report.txt" in a project that is about to
	// create tmp/ must stay relative, not become /tmp/report.txt.
	if (!nodePath.isAbsolute(p) && !p.startsWith(".")) {
		const asRel = nodePath.resolve(cwd, p);
		const asAbs = "/" + p;
		if (fs.isDir(nodePath.dirname(asRel))) return { path: raw, changed: false, reason: "" };
		if (isInside(canonical(asAbs, fs), realCwd)) return { path: asAbs, changed: true, reason: 'leading "/" restored' };
		// Both mistakes at once: no slash AND a mistyped working directory.
		const next = repairToolPath(asAbs, cwd, fs, maxSegmentDistance);
		if (next.changed) return { path: next.path, changed: true, reason: `leading "/" restored; ${next.reason}` };
		return { path: raw, changed: false, reason: "" };
	}
	if (!nodePath.isAbsolute(p)) return { path: raw, changed: false, reason: "" };

	const canon = canonical(p, fs);
	if (isInside(canon, realCwd)) {
		// Already inside cwd (possibly via /tmp vs /private/tmp): fine as is, as long as the
		// directory it names exists. If it does not, it may still be a mistyped cwd below.
		if (fs.isDir(nodePath.dirname(canon)) || fs.exists(canon)) return { path: raw, changed: false, reason: "" };
	}
	if (fs.isDir(nodePath.dirname(canon))) return { path: raw, changed: false, reason: "" };

	// Case 2: a mistyped working directory. Find the deepest existing ancestor of the
	// requested path. It must be an ancestor of cwd, and the next missing segment(s) must
	// be near-identical to cwd's own segments at the same depth.
	const segs = canon.split(nodePath.sep).filter(Boolean);
	const cwdSegs = realCwd.split(nodePath.sep).filter(Boolean);
	let depth = 0;
	while (depth < segs.length && fs.isDir(nodePath.sep + segs.slice(0, depth + 1).join(nodePath.sep))) depth++;
	if (depth >= cwdSegs.length) return { path: raw, changed: false, reason: "" };
	for (let i = 0; i < depth; i++) if (segs[i] !== cwdSegs[i]) return { path: raw, changed: false, reason: "" };
	const missing = cwdSegs.length - depth;
	if (segs.length - depth < missing + 1) return { path: raw, changed: false, reason: "" };
	for (let i = depth; i < cwdSegs.length; i++) {
		// Short names get no slack: "ab" or "apq" must not be read as a typo of "app".
		const allowed = cwdSegs[i].length >= 8 ? maxSegmentDistance : cwdSegs[i].length >= 5 ? Math.min(1, maxSegmentDistance) : 0;
		if (editDistance(segs[i], cwdSegs[i], maxSegmentDistance) > allowed) {
			return { path: raw, changed: false, reason: "" };
		}
	}
	const fixed = nodePath.join(realCwd, ...segs.slice(cwdSegs.length));
	return { path: fixed, changed: true, reason: `mistyped working directory corrected (${segs.slice(depth, cwdSegs.length).join("/")})` };
}
