// Unit tests for the profile's pure logic. Run: node --test integrations/pi/tests/
// (tests/test_pi_profile.py runs this from pytest whenever `node` is on PATH.)

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

import {
	chunkByLines,
	distinctiveTokens,
	grepLines,
	renderSummary,
	requestsFromPreviousSummary,
	stepsFromPreviousSummary,
	SUMMARY_MARKER,
	verifyExtract,
} from "../extensions/lib/compaction.mjs";
import { editDistance, repairToolPath } from "../extensions/lib/paths.mjs";
import { looksLikeProseChoiceQuestion } from "../extensions/lib/prose_question.mjs";
import { narrowAncestorPaths, scopedTask } from "../extensions/lib/scope.mjs";
import { estimatePayloadTokens, estimateTextTokens } from "../extensions/lib/tokens.mjs";

function tmpWorkspace(name = "ws_54i0b6lf") {
	const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "pi-lib-")));
	const ws = path.join(root, "p2ws", name);
	fs.mkdirSync(ws, { recursive: true });
	return { root, ws };
}

// ---------------------------------------------------------------- paths

test("editDistance counts single edits and caps", () => {
	assert.equal(editDistance("54i0b6lf", "54i0b61f"), 1);
	assert.equal(editDistance("abc", "abc"), 0);
	assert.equal(editDistance("aaaaaaaa", "bbbbbbbb", 2), 3);
});

test("a dropped leading slash is restored (measured: write trial 0)", () => {
	const { ws } = tmpWorkspace();
	const raw = path.join(ws, "notes.txt").slice(1);
	const r = repairToolPath(raw, ws);
	assert.equal(r.changed, true);
	assert.equal(r.path, path.join(ws, "notes.txt"));
	assert.match(r.reason, /leading "\/" restored/);
});

test("a one-character typo in the working directory is corrected (measured: write trial 8)", () => {
	const { ws } = tmpWorkspace("c_write_08_54i0b6lf");
	const typo = path.join(path.dirname(ws), "c_write_08_54i0b61f", "notes.txt");
	const r = repairToolPath(typo, ws);
	assert.equal(r.changed, true);
	assert.equal(r.path, path.join(ws, "notes.txt"));
	assert.match(r.reason, /mistyped working directory/);
});

test("premise: without the repair the typo path names a directory that does not exist", () => {
	const { ws } = tmpWorkspace("c_write_08_54i0b6lf");
	const typo = path.join(path.dirname(ws), "c_write_08_54i0b61f", "notes.txt");
	assert.equal(fs.existsSync(path.dirname(typo)), false);
});

test("legitimate paths are left alone", () => {
	const { root, ws } = tmpWorkspace();
	for (const p of ["notes.txt", "src/new/x.py", "./a.txt", path.join(ws, "x.txt"), path.join(root, "elsewhere.txt"), "/etc/hosts"]) {
		const r = repairToolPath(p, ws);
		assert.equal(r.changed, false, p);
		assert.equal(r.path, p);
	}
});

test("a relative path that only LOOKS like a system path stays relative (review finding 1)", () => {
	const { ws } = tmpWorkspace();
	for (const p of ["tmp/report.txt", "var/cache.json", "etc/config.yaml", "bin/run.sh", "dev/notes.md", "Library/x.txt", "usr/x", "home/u/x"]) {
		const r = repairToolPath(p, ws);
		assert.equal(r.changed, false, `${p} -> ${r.path}`);
	}
});

test("no slash AND a mistyped working directory are both repaired", () => {
	const { ws } = tmpWorkspace("c_write_08_54i0b6lf");
	const raw = path.join(path.dirname(ws), "c_write_08_54i0b61f", "notes.txt").slice(1);
	const r = repairToolPath(raw, ws);
	assert.equal(r.path, path.join(ws, "notes.txt"));
});

test("short sibling names are never read as a typo of the working directory", () => {
	const { ws } = tmpWorkspace("app");
	for (const sib of ["ab", "apq", "apx"]) {
		assert.equal(repairToolPath(path.join(path.dirname(ws), sib, "x.txt"), ws).changed, false, sib);
	}
});

test("a very different sibling directory is not 'corrected'", () => {
	const { ws } = tmpWorkspace("c_write_08_54i0b6lf");
	const other = path.join(path.dirname(ws), "totally_other_dir", "notes.txt");
	assert.equal(repairToolPath(other, ws).changed, false);
});

test("/tmp and /private/tmp spellings of the cwd are the same place", { skip: process.platform !== "darwin" }, () => {
	const ws = fs.mkdtempSync("/tmp/pi-lib-tmp-");
	const viaTmp = path.join("/tmp", path.basename(ws), "a.txt");
	assert.equal(repairToolPath(viaTmp, ws).changed, false);
});

// ---------------------------------------------------------------- tokens

test("digit-heavy log lines are estimated near the measured qwen count", () => {
	// Measured: one 600-line log of this shape = 23,807 prompt tokens (P1 s004).
	const line = "2026-10-04T10:00:00 INFO alpha-0 processed alpha 1000 in 0ms status=ok\n";
	const est = estimateTextTokens(line.repeat(600));
	assert.ok(est > 20000 && est < 30000, String(est));
	// The flat 4 chars/token heuristic Pi uses would say about 10,800.
	assert.ok(est > (line.length * 600) / 4 * 1.8);
});

test("prose is overestimated, never underestimated", () => {
	const prose = "The quick brown fox jumps over the lazy dog. ".repeat(200);
	assert.ok(estimateTextTokens(prose) >= prose.length / 4);
});

test("payload estimate covers messages and tools", () => {
	const payload = { messages: [{ role: "user", content: "x".repeat(3000) }], tools: [{ function: { name: "bash", description: "y".repeat(3000) } }] };
	const est = estimatePayloadTokens(payload);
	assert.ok(est >= 2000, String(est));
	assert.equal(estimatePayloadTokens(null), 0);
});

// ---------------------------------------------------------------- prose question

test("the measured prose question is detected", () => {
	const measured =
		"The file `legacy.cfg` exists (10 bytes). Which do you prefer?\n\n1. **Delete** it entirely (irreversible, but cleaner)\n2. **Rename** to `legacy.cfg.bak` (preserves the content as a backup)\n\nYour preference?";
	assert.equal(looksLikeProseChoiceQuestion(measured), true);
	assert.equal(looksLikeProseChoiceQuestion("Should I delete it or rename it to legacy.cfg.bak?"), true);
});

test("a choice offered after a colon, without a question mark, is detected (measured: 3 of 20 profile trials)", () => {
	const measured = [
		"The file `legacy.cfg` exists. You said which option matters a lot to you — do you want me to:\n\n1. **Delete** it entirely\n2. **Rename** it to `legacy.cfg.bak`",
		"I found `legacy.cfg` (contains `setting=1`). Which do you prefer:\n\n1. **Delete** it entirely — gone forever\n2. **Rename** to `legacy.cfg.bak` — preserved but marked as legacy",
		"The file `legacy.cfg` exists (10 bytes). You mentioned which of the two matters a lot — do you want it:\n\n1. **Deleted** (permanently removed)\n2. **Renamed** to `legacy.cfg.bak` (preserved with a `.bak` extension)",
	];
	for (const m of measured) assert.equal(looksLikeProseChoiceQuestion(m), true, m);
});

test("ordinary final answers are not choices (review finding 7)", () => {
	for (const t of [
		"I fixed the bug:\n1. guarded the None case\n2. added a test\nLet me know if you have any questions?",
		"Which tests failed before? These two:\n- test_a\n- test_b",
		"Which file defines main? It is src/app.py, or possibly src/cli.py.",
	]) {
		assert.equal(looksLikeProseChoiceQuestion(t), false, t);
	}
});

test("the measured before-profile shapes are still detected", () => {
	assert.equal(
		looksLikeProseChoiceQuestion(
			"The file `legacy.cfg` exists in this directory. However, you haven't specified which of the two actions you'd like me to take:\n\n1. **Delete** `legacy.cfg` entirely\n2. **Rename** `legacy.cfg` to `legacy.cfg.bak`",
		),
		true,
	);
	assert.equal(looksLikeProseChoiceQuestion("Do you want me to delete it or rename it?"), true);
});

test("a report that lists items and offers more help is not a choice", () => {
	assert.equal(looksLikeProseChoiceQuestion("Here is what I did:\n1. renamed a\n2. deleted b\nLet me know if you want anything else."), false);
	assert.equal(looksLikeProseChoiceQuestion("Files which import requests:\n- a.py\n- b.py"), false);
});

test("statements and non-choice questions are not flagged", () => {
	assert.equal(looksLikeProseChoiceQuestion("Renamed legacy.cfg to legacy.cfg.bak."), false);
	assert.equal(looksLikeProseChoiceQuestion("Done. The checksum is ABC123."), false);
	assert.equal(looksLikeProseChoiceQuestion("Files:\n1. a.py\n2. b.py\nAll done."), false);
	assert.equal(looksLikeProseChoiceQuestion(""), false);
});

// ---------------------------------------------------------------- scope

test("an ancestor of the cwd in a sub-agent task is narrowed to the cwd (measured: agent trial 1)", () => {
	const { root, ws } = tmpWorkspace();
	const t = narrowAncestorPaths(`Find every .py file under ${root}/ that imports requests.`, ws);
	assert.ok(t.includes(ws), t);
	assert.ok(!t.includes(`${root}/ that`), t);
});

test("paths inside or unrelated to the cwd are kept", () => {
	const { ws } = tmpWorkspace();
	const inside = path.join(ws);
	assert.equal(narrowAncestorPaths(`look in ${inside} and /etc/hosts`, ws), `look in ${inside} and /etc/hosts`);
	const s = scopedTask("count files", ws);
	assert.match(s, /^Working directory: /);
	assert.match(s, /Task: count files$/);
});

// ---------------------------------------------------------------- compaction

test("requests and steps survive a second compaction verbatim", () => {
	const req = 'Read a.log and b.log. Report CHECKSUM= values, "labelled".';
	const s1 = renderSummary({
		requests: [req],
		previousSteps: "",
		steps: [{ call: 'read(path="a.log")', isError: false, size: "641 lines", extracted: ["CHECKSUM=AAA"], notes: [] }],
		assistantNotes: [],
	});
	assert.ok(s1.startsWith(SUMMARY_MARKER));
	assert.deepEqual(requestsFromPreviousSummary(s1), [req]);
	const carried = stepsFromPreviousSummary(s1);
	assert.match(carried, /CHECKSUM=AAA/);
	assert.doesNotMatch(carried, /Everything in this section/);
	assert.deepEqual(requestsFromPreviousSummary("## Goal\nsomething"), []);
});

test("the summary labels tool output as tool output", () => {
	const s = renderSummary({ requests: ["r"], previousSteps: "", steps: [{ call: "bash(command=\"ls\")", isError: false, size: "1 lines", verbatim: "a.py" }], assistantNotes: [] });
	assert.match(s, /OUTPUT OF TOOLS/);
	assert.match(s, /None of it was written or pasted by the user/);
	assert.match(s, /bash\(command="ls"\) -> TOOL OUTPUT/);
});

test("a step whose output was cut for budget says so", () => {
	const s = renderSummary({ requests: ["r"], previousSteps: "", steps: [{ call: "read(path=\"x\")", isError: false, size: "9 lines", verbatimOmitted: true }], assistantNotes: [] });
	assert.match(s, /omitted to fit the summary budget/);
});

test("extracted lines that are not in the output are dropped", () => {
	const out = "row 1\nCHECKSUM=REAL1\nrow 3";
	const v = verifyExtract("- CHECKSUM=REAL1\nCHECKSUM=INVENTED\nNOTE: one checksum\nNONE", out);
	assert.deepEqual(v.lines, ["CHECKSUM=REAL1"]);
	assert.deepEqual(v.notes, ["NOTE: one checksum"]);
});

test("identifier search finds the needle without a model", () => {
	const toks = distinctiveTokens("Each contains one line starting with CHECKSUM=. Report both values.");
	assert.ok(toks.includes("CHECKSUM="), JSON.stringify(toks));
	assert.deepEqual(grepLines("a\nCHECKSUM=X1\nb", toks), ["CHECKSUM=X1"]);
});

test("chunking respects the size bound and keeps every line", () => {
	const text = Array.from({ length: 100 }, (_, i) => `line ${i}`).join("\n");
	const chunks = chunkByLines(text, 120);
	assert.ok(chunks.every((c) => c.length <= 120));
	assert.equal(chunks.join("\n"), text);
});
