// Does a final assistant message ask the user to choose, in prose?
//
// Measured on qwen3.6 in Pi (harness-parity P2, AskUserQuestion 0/2): with the
// question tool offered, the model ended its turn with "Which do you prefer?
// 1. Delete ... 2. Rename ..." instead of calling the tool. A headless caller
// then gets a question nobody can answer.
//
// The test is deliberately narrow, because a false positive costs a correct
// answer (the guard pushes the model to the question tool). It needs an explicit
// ask phrase ("do you want", "should I", "which do you", "you prefer", ...) AND
// the alternatives attached to it:
//   A. the ask line ends with "?" or ":" and a list of at least two items follows;
//   B. the ask line ends with "?" and itself names two alternatives with "or".
// Generic "which"/"let me know" do not count: "Which tests failed? These two: ..."
// and "...Let me know if you have any questions?" are answers, not choices.
// Measured: the first version required a "?" and missed 3 of 20 profile trials that
// ended "do you want me to:\n\n1. ...", which shape A covers.

const LIST_ITEM_LINE = /^\s*(?:\d+[.)]|[-*•]|\(?[a-d]\))\s+\S/;
const ASK_PHRASE =
	/\b(?:do you want|would you like|should i|shall i|which (?:one|option|of the|do you|would you)|(?:do )?you prefer|your (?:choice|preference)|please (?:choose|pick|confirm)|want me to)\b/i;
const OR_CHOICE = /\b(?:or|either)\b/i;

export function looksLikeProseChoiceQuestion(text) {
	if (typeof text !== "string" || !text.trim()) return false;
	const lines = text.trim().split("\n");
	for (let i = 0; i < lines.length; i++) {
		const line = lines[i].trim();
		if (!ASK_PHRASE.test(line)) continue;
		const endsAsk = /[?:]\**\s*$/.test(line);
		if (endsAsk) {
			let items = 0;
			for (const next of lines.slice(i + 1)) if (LIST_ITEM_LINE.test(next)) items++;
			if (items >= 2) return true;
		}
		if (/\?/.test(line) && OR_CHOICE.test(line)) return true;
	}
	return false;
}
