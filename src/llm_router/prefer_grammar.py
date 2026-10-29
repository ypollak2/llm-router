"""P2.13 deterministic English + Hebrew grammar for ``llm-router prefer``.

Pure and stdlib-only: no I/O, no ``llm_router.router`` import, so a hook can
import it. ``parse(text)`` turns one natural-language priority into canonical
rule dicts, or refuses with a reason and examples. There is no model call and
no fuzzy matching: only the listed forms are accepted (free-form parsing is a
separate, later item).

Canonical rules (ids and the original text are added by the store)::

    {"kind": "order", "first": T, "then": T}
    {"kind": "ban", "target": T}
    {"kind": "no_paid_api"}
    {"kind": "cap", "target": T, "unit": "day" | "pct", "n": int}
    {"kind": "local_only", "task": "code|query|research|analyze|generate"}

``T`` is ``{"providers": [...], "model": glob | None}``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

TASKS = {
    "code": "code",
    "query": "query", "questions": "query", "question": "query",
    "research": "research",
    "analysis": "analyze", "analyze": "analyze",
    "writing": "generate", "generate": "generate", "generation": "generate",
    "קוד": "code",
    "שאלות": "query", "שאלה": "query",
    "מחקר": "research",
    "ניתוח": "analyze",
    "כתיבה": "generate", "יצירה": "generate",
}

# Providers outside the table below are accepted by their own name. This is a
# static copy of tools/setup._PROVIDER_REGISTRY (that module is not stdlib-only);
# tests/test_prefer_grammar.py pins it against the registry.
OTHER_PROVIDERS = (
    "groq", "deepseek", "perplexity", "mistral", "together", "xai", "cohere",
)

_ANTHROPIC = ["anthropic"]
_GEMINI = ["gemini", "gemini_cli"]


def _t(providers: list[str], model: str | None = None) -> dict:
    return {"providers": list(providers), "model": model}


def _build_targets() -> dict[tuple[str, ...], dict]:
    table: dict[tuple[str, ...], dict] = {}

    def add(names: tuple[str, ...], target: dict) -> None:
        for name in names:
            table[tuple(name.split(" "))] = target

    add(("claude", "anthropic", "קלוד"), _t(_ANTHROPIC))
    for family in ("opus", "sonnet", "haiku"):
        glob = f"claude-{family}*"
        add((family, f"claude {family}", f"anthropic {family}"), _t(_ANTHROPIC, glob))
    add(("אופוס", "קלוד אופוס"), _t(_ANTHROPIC, "claude-opus*"))
    add(("סונט", "קלוד סונט"), _t(_ANTHROPIC, "claude-sonnet*"))
    add(("הייקו", "קלוד הייקו"), _t(_ANTHROPIC, "claude-haiku*"))
    add(("codex", "קודקס"), _t(["codex"]))
    add(("gemini", "ג'מיני", "גמיני"), _t(_GEMINI))
    add(("gemini cli", "gemini-cli", "ג'מיני cli", "גמיני cli"), _t(["gemini_cli"]))
    add(("local", "ollama", "מקומי", "לוקאלי", "לוקל"), _t(["ollama"]))
    add(("openai", "אופןאיי", "אופנאיי"), _t(["openai"]))
    for name in OTHER_PROVIDERS:
        add((name,), _t([name]))
    return table


TARGETS = _build_targets()
KNOWN_PROVIDERS = tuple(sorted({p for t in TARGETS.values() for p in t["providers"]}))
_KNOWN_WORDS = frozenset(w for key in TARGETS for w in key) | frozenset(TASKS)

_HEBREW_PREFIXES = "לבהו"
_BIDI = dict.fromkeys(map(ord, "‎‏‪‫‬‭‮"))
_QUOTES = {ord(c): "'" for c in "׳״‘’"}

EXAMPLES_EN = ("codex before claude", "never claude opus", "cap claude 80%")
EXAMPLES_HE = ("codex לפני claude", "אף פעם לא opus", "הגבל את codex ל-20 ביום")
_MORE_EXAMPLES = ("no paid API", "only local for code", "cap codex 20/day")


@dataclass
class ParseResult:
    rules: list[dict] = field(default_factory=list)
    refusal: str | None = None
    echo: list[str] = field(default_factory=list)


def normalize(text: str) -> str:
    """NFKC, no niqqud / bidi marks, one quote style, one space, lower-case Latin."""
    text = unicodedata.normalize("NFKC", text or "")
    # Maqaf (U+05BE) sits inside the niqqud range but is a hyphen: keep it as one.
    text = text.replace("־", "-")
    text = "".join(ch for ch in text if not "֑" <= ch <= "ׇ")
    text = text.translate(_BIDI).translate(_QUOTES).replace(",", " ")
    text = " ".join(text.split())
    return "".join(ch.lower() if ch.isascii() else ch for ch in text).strip(" .!?")


def _is_hebrew(ch: str) -> bool:
    return "א" <= ch <= "ת"


def _strip_prefix(word: str) -> str:
    """Drop ל/ב/ה/ו (and a hyphen) from a Hebrew word, only if a known word remains."""
    if word in _KNOWN_WORDS or not word or not _is_hebrew(word[0]):
        return word
    cur = word
    for _ in range(2):
        if not cur or cur[0] not in _HEBREW_PREFIXES:
            break
        rest = cur[1:].lstrip("-")
        if rest in _KNOWN_WORDS:
            return rest
        cur = rest
    return word


def _words(segment: str) -> list[str]:
    return [_strip_prefix(w) for w in segment.split(" ") if w]


def _target(segment: str) -> dict | None:
    key = tuple(_words(segment))
    found = TARGETS.get(key)
    return None if found is None else {"providers": list(found["providers"]), "model": found["model"]}


def _task(word: str) -> str | None:
    return TASKS.get(_strip_prefix(word))


def _describe(target: dict) -> str:
    names = "+".join(target["providers"])
    return f"{names} ({target['model']})" if target["model"] else names


def _examples() -> str:
    en = "; ".join(f'"{e}"' for e in EXAMPLES_EN)
    he = "; ".join(f'"{e}"' for e in EXAMPLES_HE)
    return f"English: {en}\n  Hebrew: {he}"


def _refuse(reason: str) -> ParseResult:
    return ParseResult(refusal=f"{reason}\n  Examples:\n  {_examples()}\n"
                       f"  Also: {'; '.join(_MORE_EXAMPLES)}")


def _bad_target(segment: str) -> ParseResult:
    names = sorted({" ".join(k) for k in TARGETS if all(ord(c) < 128 for c in " ".join(k))})
    return _refuse(f"Unknown target {segment!r}. Known names: {', '.join(names)}.")


# ── patterns ─────────────────────────────────────────────────────────────────

_NO_PAID = {
    ("no", "paid", "api"), ("no", "paid", "apis"),
    ("בלי", "api", "בתשלום"), ("בלי", "תשלום"), ("בלי", "api", "משלם"),
}
_CAP_RES = (
    re.compile(r"^cap (?P<x>.+?) (?P<n>\d+)\s*(?P<u>%|/\s*day|per day)$"),
    re.compile(r"^הגבל(?: את)? (?P<x>.+?) ל-?\s*(?P<n>\d+)\s*(?P<u>%|ביום)$"),
    re.compile(r"^מקסימום (?P<n>\d+) ביום ל-?\s*(?P<x>.+)$"),
    re.compile(r"^(?P<x>.+?) עד (?P<n>\d+)\s*(?P<u>%|ביום)$"),
)
_BAN_PREFIXES = (
    "never ", "no ", "don't use ", "do not use ", "dont use ",
    "אף פעם לא ", "לעולם לא ", "בלי ", "לא להשתמש ", "אל תשתמש ",
)
_ONLY_LOCAL = ("only local for ", "רק מקומי ", "רק לוקאלי ", "רק לוקל ")
_ORDER_SEPS = (" before ", " ahead of ", " first then ", " לפני ")


def _parse_cap(text: str) -> ParseResult | None:
    for rx in _CAP_RES:
        m = rx.match(text)
        if not m:
            continue
        groups = m.groupdict()
        n = int(groups["n"])
        unit = "pct" if groups.get("u") == "%" else "day"
        target = _target(groups["x"])
        if target is None:
            return _bad_target(groups["x"])
        if n < 1 or (unit == "pct" and n > 100):
            return _refuse(f"Cap out of range: {n}{'%' if unit == 'pct' else '/day'} "
                           "(per-day needs n >= 1, percent needs 1-100).")
        return ParseResult(rules=[{"kind": "cap", "target": target, "unit": unit, "n": n}])
    return None


def _parse_only_local(text: str) -> ParseResult | None:
    for lead in _ONLY_LOCAL:
        if not text.startswith(lead):
            continue
        rest = text[len(lead):]
        if lead.startswith("רק"):  # Hebrew: the task carries the ל prefix
            if not rest.startswith("ל"):
                return None
            rest = rest[1:].lstrip("-")
        task = TASKS.get(rest)
        if task is None:
            return _refuse(f"Unknown task {rest!r}. Tasks: code, questions, research, analysis, writing.")
        return ParseResult(rules=[{"kind": "local_only", "task": task}])
    return None


def _split_order(text: str) -> list[str] | None:
    padded = f" {text} "
    if text.startswith("prefer "):
        padded = f" {text[len('prefer '):]} "
        seps = (" over ", " to ") + _ORDER_SEPS
    else:
        seps = _ORDER_SEPS
    if not any(s in padded for s in seps):
        return None
    pattern = "|".join(re.escape(s) for s in seps)
    return [seg.strip() for seg in re.split(pattern, padded)]


def _parse_order(text: str) -> ParseResult | None:
    segments = _split_order(text)
    if segments is None:
        return None
    if len(segments) < 2 or not all(segments):
        return _refuse("An order needs a target on both sides, e.g. \"codex before claude\".")
    targets = []
    for seg in segments:
        target = _target(seg)
        if target is None:
            return _bad_target(seg)
        targets.append(target)
    for i, a in enumerate(targets):
        if any(a == b for b in targets[i + 1:]):
            return _refuse("The same target appears twice in one order, e.g. \"claude before claude\".")
    return ParseResult(rules=[{"kind": "order", "first": a, "then": b}
                              for a, b in zip(targets, targets[1:])])


def _parse_ban(text: str) -> ParseResult | None:
    for lead in _BAN_PREFIXES:
        if text.startswith(lead):
            rest = text[len(lead):].strip()
            if not rest:
                return _refuse("A ban needs a target, e.g. \"no gemini\".")
            target = _target(rest)
            if target is None:
                return _bad_target(rest)
            return ParseResult(rules=[{"kind": "ban", "target": target}])
    return None


def _sentence(rule: dict) -> str:
    kind = rule["kind"]
    if kind == "order":
        return f"Prefer {_describe(rule['first'])} before {_describe(rule['then'])}"
    if kind == "ban":
        return f"Never use {_describe(rule['target'])}"
    if kind == "no_paid_api":
        return "Never use a paid API (free providers and a connected Claude subscription stay allowed)"
    if kind == "cap":
        doors = "only calls the router itself sends (MCP llm tools, hook- and SDK-routed calls)"
        claude = ("Claude Code conversation turns, proxy-forwarded ones included, are {}, and a Claude "
                  "cap at the proxy tier decision is not implemented")
        if rule["unit"] == "pct":
            return (f"Cap {_describe(rule['target'])} at {rule['n']}% of quota; {doors} are limited: "
                    + claude.format("not"))
        return (f"Cap {_describe(rule['target'])} at {rule['n']} calls per rolling 24 h; {doors} are "
                "counted and limited: " + claude.format("neither counted nor limited"))
    return f"Use only local models (Ollama on this machine) for {rule['task']} tasks"


def describe(rule: dict) -> str:
    """One English sentence for a canonical rule (also used by the store/CLI)."""
    return _sentence(rule)


def parse(text: str) -> ParseResult:
    """Parse one priority sentence. Never raises; a refusal carries examples."""
    norm = normalize(text)
    if not norm:
        return _refuse("Empty input.")
    if norm.startswith("please "):
        norm = norm[len("please "):]
    result: ParseResult | None = None
    if tuple(norm.split(" ")) in _NO_PAID:
        result = ParseResult(rules=[{"kind": "no_paid_api"}])
    for step in (_parse_cap, _parse_only_local, _parse_order, _parse_ban):
        if result is not None:
            break
        result = step(norm)
    if result is None:
        return _refuse(f"Not a recognised priority: {text.strip()!r}.")
    if result.refusal is None:
        original = " ".join((text or "").split())
        result.echo = [f"{_sentence(r)}   [from: {original}]" for r in result.rules]
    return result
