"""Programmatic grader for commit_message and pr_description answers (ported from the frozen PREREG-Q37 grader; behaviour identical except one documented fix).

The fix (D-268): an issue ref ``#N`` is also grounded by an ``/issues/N``, ``/pull/N`` or ``/pulls/N`` URL in the context. The Q37 G4 audit found
that the frozen rule false-failed bank item q37-511a487c23de, whose diff carries the number only as an ``issues/18861`` URL.

Enforces ``rubric.LIMITS`` and nothing else; identical for every arm. A failed rule fails the answer; each rule has a name that appears in the
``reasons`` list. Diff-grounding rules (no invented content): every file path named (backticked, or bare with a directory and a known extension)
must have its basename in the diff; every backticked identifier must have each dotted component in the diff (or be a language builtin / well-known
tool); every backticked long flag must be in the diff or a well-known tool flag; no claim that tests were run or passed. Everything else about
content is the judge's job. The module is pure: no I/O, no globals read from files.

An *item* is a mapping with ``stratum`` (``commit_message`` or ``pr_description``), ``diff`` (str), ``files`` (list[str]) and optionally
``messages`` (chat messages whose last entry is the prompt the model saw; used to ground issue refs and URLs).
"""
from __future__ import annotations

from typing import Any

import re
from .rubric import LIMITS

THINK = re.compile(r"<think>.*?</think>", re.S)
PREAMBLE = re.compile(r"^\s*(?:here(?:'s|\s+is|\s+are)\b|sure\b|certainly\b|of course\b|okay\b|below is\b|i(?:'ll|\s+will)\b|commit message\s*:|pull request description\s*:|\*\*commit message|\*\*pull request)", re.I)
EXT = r"py|pyi|md|rst|txt|toml|cfg|ini|yml|yaml|js|jsx|ts|tsx|mjs|cjs|go|rs|c|h|cc|cpp|json|html|css|sh|java|rb"
PATHTOK = re.compile(r"^(?:\.{0,2}/)?[\w@.+-]+(?:/[\w@.+-]+)*\.(?:%s)$" % EXT)
PLAIN_PATH = re.compile(r"(?<![\w/.`@-])((?:[\w@.+-]+/)+[\w@.+-]+\.(?:%s))(?![\w/-])" % EXT)
BT = re.compile(r"`([^`\n]{1,200})`")
IDENT = re.compile(r"^[A-Za-z_$][\w$]*(?:(?:\.|::|->)[A-Za-z_$][\w$]*)*(?:\(.*\))?$")
ATTR_OBJECTS = frozenset("response request resp req res r self cls obj result results data client session app page doc soup node el element window document this ctx context".split())
FRAMEWORKS = frozenset("node vue next nuxt react express three d3 backbone ember angular socket p5 chart moment require mocha jest dayjs".split())
BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+\S")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S")
OK_IDENT = frozenset("""None True False self cls str int float bool list dict set tuple bytes object type len range print isinstance super Exception ValueError TypeError KeyError RuntimeError
AttributeError OSError ImportError NotImplementedError IndexError StopIteration AssertionError null undefined true false string number boolean async await Promise Error console void any never unknown
nil error byte rune interface struct map chan Option Result Some Ok Err String Vec Box Self bool i32 i64 u8 u32 u64 usize isize f32 f64 str pytest python python3 pip uv tox nox ruff mypy pyright git make cargo go
npm npx node pnpm yarn vitest jest tsc clippy rustfmt rustc gofmt flake8 black isort pre-commit sphinx unittest coverage CI README CHANGELOG TODO API URL HTTP HTTPS JSON YAML TOML SQL CLI""".split())
OK_FLAGS = frozenset("--help --version --verbose --quiet --cov --tb --maxfail --no-cov --lf --ff --all --release --check --fix --watch --force --dry-run --workspace --lib --bins --tests --features --all-features --no-default-features --color --debug --strict --config --upgrade --user --no-deps --editable --no-cache --no-input --watch --coverage --run --reporter --silent".split())
GENERIC_ANCHOR = frozenset("module modules class test tests init main name value values type types data file files path args kwargs result results none self return import from string list dict object function method update code docs doc readme changes".split())

# ---- imperative-mood heuristic for the subject's first word (rubric: "imperative mood")
ED_OK = frozenset("embed seed speed proceed succeed exceed feed need bleed shed shred bed red hundred weed breed indeed sacred naked wicked kindred".split())
ING_OK = frozenset("bring ping string swing sing spring bing cling fling sling wing ring king sting thing during evening morning nothing something anything everything ceiling sibling spring".split())
SS_OK = ()   # words ending in 's' that are fine are detected by the verb test below, so no list is needed
IRREG_PAST = frozenset("made ran took got gave went wrote built found kept held left sent became began brought thought taught caught bought lost sold told said saw broke chose drew drove fell forgot grew hid knew led met paid rose spoke stood threw woke wore won".split())
VERBS = frozenset("""add fix remove update allow make use improve change handle support avoid drop replace move rename ensure prevent enable disable skip show return raise set get clean refactor simplify extend introduce
deprecate document test expose respect preserve validate convert apply pass check run build mark fail stop start create delete load read write parse format sort cache reuse split merge wrap unwrap propagate accept reject report log
warn silence correct reduce increase decrease limit restore revert tighten relax tweak teach let help keep send call import export include exclude detect resolve normalize sanitize escape quote unquote strip trim truncate flush close
open release lock unlock bind register unregister configure initialize reset clear copy sync adapt align rewrite rework reorder reorganize restructure migrate port upgrade downgrade pin unpin require forbid disallow permit treat
consider ignore recognize implement define declare emit fire trigger dispatch render display print raise throw catch wait await retry cancel abort finish complete resume pause rely depend need provide offer expand shrink fold unfold
inline hoist lift extract reuse share isolate guard protect secure harden cover track capture record store save persist serialize deserialize encode decode compress decompress resize reshape sort order group join link unlink mount
unmount attach detach move push pull fetch download upload publish bump tag label name call expect default fallback mirror duplicate dedupe deduplicate separate combine unify standardize generalize specialize optimize speed
accelerate slow throttle debounce batch queue schedule defer delay postpone prefetch preload warm lazy cleanup tidy polish document explain clarify note mention list describe annotate type typecheck lint stub mock patch hotfix""".split())


def strip_think(t: str | None) -> str:
    t = THINK.sub("", t or "")
    return t.split("</think>")[-1] if "</think>" in t else t


def clean(text: str | None) -> tuple[str, list[str]]:
    """-> (text, flags). Removes <think>, surrounding whitespace, and ONE outer code fence that wraps the entire answer (flag outer_fence_removed)."""
    t = strip_think(text).strip()
    flags = []
    lines = t.split("\n")
    if len(lines) >= 3 and re.match(r"^```[\w-]*\s*$", lines[0].strip()) and lines[-1].strip() == "```" and sum(1 for ln in lines if ln.strip().startswith("```")) == 2:
        t = "\n".join(lines[1:-1]).strip()
        flags.append("outer_fence_removed")
    return t, flags


def subject_of(t: str) -> str:
    return next((ln for ln in t.splitlines() if ln.strip()), "")


def first_word(subject: str) -> str:
    s = subject.strip()
    for _ in range(3):   # strip gitmoji, [tag], conventional prefix `fix(scope)!:`, `module.name:` prefixes
        n = s
        s = re.sub(r"^(?::\w+:|[^\w\s]|\[[^\]]*\])+\s*", "", s)
        s = re.sub(r"^[\w./-]+(?:\([^)]*\))?!?:\s+", "", s)
        s = re.sub(r"^[\w./-]+(?:\([^)]*\))?!?:(?=\S)", "", s) if re.match(r"^[a-z]+(?:\([^)]*\))?!?:\S", s) else s
        if s == n:
            break
    m = re.match(r"[A-Za-z][A-Za-z'-]*", s)
    return m.group(0).lower() if m else ""


def non_imperative(w: str) -> bool:
    if not w:
        return False
    if w in IRREG_PAST:
        return True
    if w.endswith("ed") and len(w) > 3 and w not in ED_OK:
        return True
    if w.endswith("ing") and len(w) > 4 and w not in ING_OK:
        return True
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        base = [w[:-1], w[:-2], (w[:-3] + "y") if w.endswith("ies") else ""]
        if any(b in VERBS for b in base):
            return True
    return False


# ---- test-run claims
_PURPOSE = re.compile(r"\b(make|ensure|ensures|make sure|so that|so|until|should|must|will|would|verify|check|confirm|assert|if|when|whether|unless|need to|needs to|to)\W*(?:\w+\W+){0,3}$", re.I)
CLAIMS = [re.compile(p, re.I) for p in (
    r"\btests?\s+(?:\w+\s+){0,2}?(?:pass(?:ed|es|ing)?|succeed(?:ed|s)?|are\s+green|is\s+green)\b",
    r"\b\d+\s+(?:tests?\s+)?(?:passed|passing)\b",
    r"\b(?:i|we)(?:'ve|\s+have)?\s+(?:ran|run|tested|verified|confirmed)\b",
    r"\b(?:manually|locally)\s+(?:tested|verified|confirmed)\b|\btested\s+(?:locally|manually)\b|\bverified\s+(?:locally|manually)\b",
    r"\b(?:ci|pipeline|build)\s+(?:is\s+)?(?:passing|green|passes|passed)\b",
    r"^\s*tests?\s*:\s*\d",
)]


def test_claim(text: str) -> str | None:
    for rx in CLAIMS:
        for m in rx.finditer(text):
            pre = text[max(0, m.start() - 40):m.start()]
            if rx is CLAIMS[0] and (_PURPOSE.search(pre.split("\n")[-1]) or re.search(r"\b(to|should|must|will|can|would|could|may|might|not|never)\b", m.group(0), re.I)):
                continue
            return m.group(0)
    return None


# ---- diff grounding
def path_ok(tok: str, dl: str) -> bool:
    core = tok.split("::")[0].strip()
    if core.endswith("/") or not PATHTOK.match(core):
        return True
    base = core.rsplit("/", 1)[-1].lower()
    if "/" not in core:
        stem, ext = base.rsplit(".", 1)
        if ext in ("js", "ts", "c", "h", "json", "html", "css", "go", "rs", "sh", "md", "txt") and (stem in ATTR_OBJECTS or stem in FRAMEWORKS):
            return True
    if "/" in core:
        return core.lstrip("./").lower() in dl
    return base in dl


def codeish(part: str) -> bool:
    """a component that looks like a code identifier (underscore, camelCase/PascalCase, `$`); plain lowercase or ALLCAPS words (`linux`, `utf`, `HTTP`) are ordinary words or values and are not checked"""
    return bool(re.search(r"_|\$|[a-z][A-Z]|[A-Z][a-z]+[A-Z]", part)) or (part[:1].isupper() and part[1:2].islower() and any(c.isupper() for c in part[2:]))


def ident_ok(tok: str, diff: str) -> bool:
    t = re.sub(r"\(.*\)$", "", tok)
    for part in re.split(r"\.|::|->", t):
        if part and codeish(part) and part not in OK_IDENT and part not in diff:
            return False
    return True


def ungrounded(text: str, diff: str) -> list[tuple[str, str]]:
    """-> list of (kind, token). Backticked tokens + plain directory paths; checked against the diff text."""
    dl, out = diff.lower(), []
    for m in BT.finditer(text):
        tok = m.group(1).strip()
        if not tok:
            continue
        core = tok.split("::")[0]
        if PATHTOK.match(core):
            if not path_ok(tok, dl):
                out.append(("path", tok))
            if "::" in tok:
                for p in tok.split("::")[1:]:
                    if IDENT.match(p) and not ident_ok(p, diff):
                        out.append(("symbol", p))
            continue
        if IDENT.match(tok) and " " not in tok.split("(")[0]:
            if not ident_ok(tok, diff):
                out.append(("symbol", tok))
            continue
        for piece in tok.split():
            if piece.startswith("--") and re.match(r"^--[a-z][\w-]+", piece):
                flag = re.match(r"^--[a-z][\w-]+", piece).group(0)
                if flag not in OK_FLAGS and flag not in diff:
                    out.append(("flag", flag))
            elif PATHTOK.match(piece.split("::")[0]) and not path_ok(piece, dl):
                out.append(("path", piece))
    stripped = BT.sub(" ", text)
    for m in PLAIN_PATH.finditer(stripped):
        if not path_ok(m.group(1), dl):
            out.append(("path", m.group(1)))
    return out


def anchors_of(item: dict[str, Any]) -> set[str]:
    a = set()
    for f in item.get("files", []):
        b = f.rsplit("/", 1)[-1].lower()
        a.add(b)
        a.add(b.rsplit(".", 1)[0])
    for m in re.finditer(r"^@@ [^@]+@@ (.+)$", item["diff"], re.M):
        for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", m.group(1)):
            a.add(w.lower())
    for m in re.finditer(r"^[+-]\s*(?:async )?(?:def|class|fn|func|function)\s+([A-Za-z_]\w{3,})", item["diff"], re.M):
        a.add(m.group(1).lower())
    return {x for x in a if x not in GENERIC_ANCHOR}


# ---- the two jobs
def grade_commit(t: str, diff: str, L: dict[str, int]) -> list[str]:
    r = []
    first = subject_of(t)
    if PREAMBLE.match(t):
        r.append("preamble")
    if len(first) > L["subject_max_chars"]:
        r.append("subject_too_long")
    if non_imperative(first_word(first)):
        r.append("not_imperative")
    lines = t.splitlines()
    idx = next((i for i, ln in enumerate(lines) if ln.strip()), 0)
    rest = lines[idx + 1:]
    if rest and rest[0].strip():
        r.append("no_blank_after_subject")
    if len(" ".join(rest).split()) > L["body_max_words"]:
        r.append("body_too_long")
    if "```" in t:
        r.append("fence")
    return r


def grade_pr(t: str, diff: str, L: dict[str, int]) -> tuple[list[str], str | None, str | None]:
    r = []
    lines = t.splitlines()
    if PREAMBLE.match(t):
        r.append("preamble")
    in_code, heads = False, []
    for i, ln in enumerate(lines):
        if ln.strip().startswith("```"):
            in_code = not in_code
            continue
        if not in_code and HEADING.match(ln):
            heads.append((i, re.sub(r"^\s*#+\s*|\s*:?\s*$", "", ln).strip().lower(), len(ln) - len(ln.lstrip("# ")) and ln.strip().split()[0]))
    names = [h[1] for h in heads]
    if names != ["summary", "test plan"] or any(h[2] != "##" for h in heads):
        r.append("sections_not_exactly_summary_test_plan")
        return r, None, None
    s_i, p_i = heads[0][0], heads[1][0]
    if any(ln.strip() for ln in lines[:s_i]):
        r.append("text_before_summary")
    summary = "\n".join(lines[s_i + 1:p_i])
    plan = "\n".join(lines[p_i + 1:])
    w = len(summary.split())
    if w < L["summary_min_words"]:
        r.append("summary_too_short")
    if w > L["summary_max_words"]:
        r.append("summary_too_long")
    nb = sum(1 for ln in plan.splitlines() if BULLET.match(ln))
    if nb < L["plan_min_bullets"]:
        r.append("no_plan_bullets")
    if nb > L["plan_max_bullets"]:
        r.append("too_many_plan_bullets")
    return r, summary, plan


def issue_ref_grounded(ref: str, ctx: str) -> bool:
    """Accept a literal issue reference or its issue/pull URL path in context."""
    return "#" + ref in ctx or re.search(r"/(?:issues|pull|pulls)/" + re.escape(ref) + r"(?!\d)", ctx) is not None


def grade_text(item: dict[str, Any], text: str | None) -> dict[str, Any]:
    """Grade one answer. Returns ``valid``, ``ok``, ``reason`` (``accepted`` or comma-joined rule names), ``reasons``, ``flags``, ``ungrounded``, ``test_claim``."""
    t, flags = clean(text)
    job = item["stratum"]
    if not t:
        return {"valid": False, "ok": False, "reason": "empty", "reasons": ["empty"], "flags": flags}
    diff = item["diff"]
    L = LIMITS[job]
    if job == "commit_message":
        reasons = grade_commit(t, diff, L)
        claim_text = t
    else:
        reasons, summary, plan = grade_pr(t, diff, L)
        claim_text = summary if summary is not None else ""
    c = test_claim(claim_text)
    if c:
        reasons.append("test_claim")
    ctx = item["messages"][-1]["content"] if item.get("messages") else diff
    if any(not issue_ref_grounded(ref, ctx) for ref in re.findall(r"(?<![\w&])#(\d{2,})\b", t)):
        reasons.append("ungrounded_issue_ref")
    if any(u.rstrip(".,;:") not in ctx for u in re.findall(r"https?://[^\s)>\]`]+", t)):
        reasons.append("ungrounded_url")
    for kind, tok in ungrounded(t, diff):
        reasons.append(f"ungrounded_{kind}")
    if not (anchors_of(item) & set(re.findall(r"[a-z_][a-z0-9_]{3,}", t.lower().replace("`", " ")))) and not any(a in t.lower() for a in anchors_of(item) if len(a) >= 5):
        flags.append("no_anchor_mention")
    reasons = list(dict.fromkeys(reasons))
    return {"valid": True, "ok": not reasons, "reason": "accepted" if not reasons else ",".join(reasons), "reasons": reasons, "flags": flags,
            "ungrounded": [f"{k}:{v}" for k, v in ungrounded(t, diff)][:6], "test_claim": c}
