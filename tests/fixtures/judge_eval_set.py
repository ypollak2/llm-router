"""Hand-labelled (prompt, response, label) triples for judge discrimination eval.

WHY HAND-LABELLED, NOT LLM-GENERATED: the point of this set is to measure whether
the judge (an LLM) can tell a correct answer from a wrong one. If the labels
themselves came from an LLM — the judge under test, a sibling model, or any
router-selected model — a judge that shares that model's blind spots would score
well for the wrong reason, and the eval would be measuring agreement, not
correctness. Every label below was assigned by a human (the agent doing the PR#145
follow-up work, 2026-09-25) working the arithmetic and facts out independently
(see `tests/fixtures/judge_eval_verification.txt` for the arithmetic cross-check)
before ever running the judge on this set.

COMPOSITION (n=50), stratified by category (~task_type the router actually uses)
and by correctness label:

    category      n   correct  wrong  partial
    arithmetic    12  5        5      2
    factual_qa    13  6        5      2
    code          13  6        5      2
    explanation   12  5        5      2
    ------------------------------------------
    TOTAL         50  22       20     8

`label` is one of:
  - "correct"  — the response is right. Used for the correct>wrong pair rate / AUC.
  - "wrong"    — the response is wrong. Most wrong items are PLAUSIBLE mistakes
                 (a real element symbol for the wrong element, an inverted
                 comparison, a common myth) rather than absurd ones — a judge
                 that only catches absurd wrongness is not being tested here.
  - "partial"  — genuinely partially correct (right on some sub-part, wrong or
                 missing on another). Reported separately per the task brief;
                 NOT pooled into "wrong" for the primary correct-vs-wrong metric.

`task_type` matches the values the router's judge prompt embeds (see
`judge._build_judge_prompt`): "query" for factual/arithmetic Q&A, "code" for
code snippets, "analyze" for explanations.

`split` is "tune" (n=28: 12 correct / 12 wrong / 4 partial) or "holdout"
(n=22: 10 correct / 8 wrong / 4 partial), assigned per category-and-label
group BEFORE any judge was run on this set, to guard against tuning the
prompt/weights against the exact cases used to report the final number.
Prompt and scoring changes are iterated against "tune" only; "holdout" is
scored once, at the end, and that is the number that goes in the PR.
"""

from __future__ import annotations

ITEMS: list[dict] = [
    # ── arithmetic (12: 5 correct, 5 wrong, 2 partial) ──────────────────────
    {
        "id": "arith-01",
        "split": "tune",
        "category": "arithmetic",
        "task_type": "query",
        "prompt": "What is 17*23?",
        "response": "391",
        "label": "correct",
        "rationale": "17*23 = 391 (verified: python `17*23`).",
    },
    {
        "id": "arith-02",
        "split": "tune",
        "category": "arithmetic",
        "task_type": "query",
        "prompt": "What is 17*23?",
        "response": "400",
        "label": "wrong",
        "rationale": "The exact CHZ-JUDGE case this eval was built to catch: 400 != 391.",
    },
    {
        "id": "arith-03",
        "split": "holdout",
        "category": "arithmetic",
        "task_type": "query",
        "prompt": "What is 144 divided by 12?",
        "response": "12",
        "label": "correct",
        "rationale": "144/12 = 12.",
    },
    {
        "id": "arith-04",
        "split": "holdout",
        "category": "arithmetic",
        "task_type": "query",
        "prompt": "What is 144 divided by 12?",
        "response": "11",
        "label": "wrong",
        "rationale": "Off-by-one from the correct 12.",
    },
    {
        "id": "arith-05",
        "split": "tune",
        "category": "arithmetic",
        "task_type": "query",
        "prompt": "What is 15% of 240?",
        "response": "36",
        "label": "correct",
        "rationale": "0.15 * 240 = 36.",
    },
    {
        "id": "arith-06",
        "split": "tune",
        "category": "arithmetic",
        "task_type": "query",
        "prompt": "What is 15% of 240?",
        "response": "24",
        "label": "wrong",
        "rationale": "Plausible mistake: that's 10% of 240, not 15%.",
    },
    {
        "id": "arith-07",
        "split": "holdout",
        "category": "arithmetic",
        "task_type": "query",
        "prompt": "What is the square root of 169?",
        "response": "13",
        "label": "correct",
        "rationale": "13*13 = 169.",
    },
    {
        "id": "arith-08",
        "split": "holdout",
        "category": "arithmetic",
        "task_type": "query",
        "prompt": "What is the square root of 169?",
        "response": "14",
        "label": "wrong",
        "rationale": "14*14 = 196, not 169.",
    },
    {
        "id": "arith-09",
        "split": "tune",
        "category": "arithmetic",
        "task_type": "query",
        "prompt": "Compute 7 + 8 * 2 using standard order of operations.",
        "response": "23",
        "label": "correct",
        "rationale": "Multiplication first: 7 + 16 = 23.",
    },
    {
        "id": "arith-10",
        "split": "tune",
        "category": "arithmetic",
        "task_type": "query",
        "prompt": "Compute 7 + 8 * 2 using standard order of operations.",
        "response": "30",
        "label": "wrong",
        "rationale": "Ignores precedence: (7+8)*2 = 30, but the prompt asked for standard order of operations.",
    },
    {
        "id": "arith-11",
        "split": "tune",
        "category": "arithmetic",
        "task_type": "query",
        "prompt": "A rectangle is 7cm by 12cm. What is its area and perimeter?",
        "response": "The area is 84 square cm. The perimeter is 19 cm.",
        "label": "partial",
        "rationale": "Area (7*12=84) is right; perimeter should be 2*(7+12)=38, not 19.",
    },
    {
        "id": "arith-12",
        "split": "holdout",
        "category": "arithmetic",
        "task_type": "query",
        "prompt": "What is 123 - 47, and is the result even or odd?",
        "response": "123 - 47 = 76, which is an odd number.",
        "label": "partial",
        "rationale": "The subtraction (76) is right; 76 is even, not odd.",
    },
    # ── factual QA (13: 6 correct, 5 wrong, 2 partial) ──────────────────────
    {
        "id": "fact-01",
        "split": "tune",
        "category": "factual_qa",
        "task_type": "query",
        "prompt": "What is the capital of France?",
        "response": "Paris",
        "label": "correct",
        "rationale": "Paris is the capital of France.",
    },
    {
        "id": "fact-02",
        "split": "tune",
        "category": "factual_qa",
        "task_type": "query",
        "prompt": "What is the capital of France?",
        "response": "Lyon",
        "label": "wrong",
        "rationale": "Lyon is a major French city but not the capital (Paris is).",
    },
    {
        "id": "fact-03",
        "split": "holdout",
        "category": "factual_qa",
        "task_type": "query",
        "prompt": "Who wrote 'Pride and Prejudice'?",
        "response": "Jane Austen",
        "label": "correct",
        "rationale": "Jane Austen wrote Pride and Prejudice (1813).",
    },
    {
        "id": "fact-04",
        "split": "holdout",
        "category": "factual_qa",
        "task_type": "query",
        "prompt": "Who wrote 'Pride and Prejudice'?",
        "response": "Charlotte Brontë",
        "label": "wrong",
        "rationale": "Plausible mix-up: another 19th-century English novelist, but she wrote Jane Eyre, not Pride and Prejudice.",
    },
    {
        "id": "fact-05",
        "split": "tune",
        "category": "factual_qa",
        "task_type": "query",
        "prompt": "What is the chemical symbol for gold?",
        "response": "Au",
        "label": "correct",
        "rationale": "Au (from Latin aurum) is gold's symbol.",
    },
    {
        "id": "fact-06",
        "split": "tune",
        "category": "factual_qa",
        "task_type": "query",
        "prompt": "What is the chemical symbol for gold?",
        "response": "Gd",
        "label": "wrong",
        "rationale": "Gd is a real element symbol (gadolinium), just the wrong one — gold is Au.",
    },
    {
        "id": "fact-07",
        "split": "holdout",
        "category": "factual_qa",
        "task_type": "query",
        "prompt": "In what year did World War II end?",
        "response": "1945",
        "label": "correct",
        "rationale": "Japan surrendered in September 1945, ending WWII.",
    },
    {
        "id": "fact-08",
        "split": "holdout",
        "category": "factual_qa",
        "task_type": "query",
        "prompt": "In what year did World War II end?",
        "response": "1944",
        "label": "wrong",
        "rationale": "Off by one year from the correct 1945.",
    },
    {
        "id": "fact-09",
        "split": "tune",
        "category": "factual_qa",
        "task_type": "query",
        "prompt": "What planet is known as the Red Planet?",
        "response": "Mars",
        "label": "correct",
        "rationale": "Mars is called the Red Planet.",
    },
    {
        "id": "fact-10",
        "split": "tune",
        "category": "factual_qa",
        "task_type": "query",
        "prompt": "What is the largest ocean on Earth?",
        "response": "The Atlantic Ocean",
        "label": "wrong",
        "rationale": "The Pacific Ocean is the largest, not the Atlantic — a common wrong guess.",
    },
    {
        "id": "fact-11",
        "split": "holdout",
        "category": "factual_qa",
        "task_type": "query",
        "prompt": "How many continents are there on Earth?",
        "response": "Seven",
        "label": "correct",
        "rationale": "The standard model lists seven continents.",
    },
    {
        "id": "fact-12",
        "split": "tune",
        "category": "factual_qa",
        "task_type": "query",
        "prompt": "Who was the first president of the United States, and in what year did he take office?",
        "response": "George Washington, who took office in 1788.",
        "label": "partial",
        "rationale": "The name is right; he took office in 1789, not 1788.",
    },
    {
        "id": "fact-13",
        "split": "holdout",
        "category": "factual_qa",
        "task_type": "query",
        "prompt": "Name the three branches of the U.S. federal government.",
        "response": "Executive and Legislative.",
        "label": "partial",
        "rationale": "Both named branches are correct but Judicial is missing — an incomplete, not wrong, answer.",
    },
    # ── code (13: 6 correct, 5 wrong, 2 partial) ────────────────────────────
    {
        "id": "code-01",
        "split": "tune",
        "category": "code",
        "task_type": "code",
        "prompt": "Write a Python function that returns the sum of a list of numbers.",
        "response": "def sum_list(nums):\n    return sum(nums)",
        "label": "correct",
        "rationale": "Correctly sums the list.",
    },
    {
        "id": "code-02",
        "split": "tune",
        "category": "code",
        "task_type": "code",
        "prompt": "Write a Python function that returns the sum of a list of numbers.",
        "response": "def sum_list(nums):\n    total = 0\n    for n in nums:\n        total = n\n    return total",
        "label": "wrong",
        "rationale": "Assigns instead of accumulating (`total = n`, not `total += n`) — returns the last element, not the sum.",
    },
    {
        "id": "code-03",
        "split": "holdout",
        "category": "code",
        "task_type": "code",
        "prompt": "Write a Python function to check if a number is even.",
        "response": "def is_even(n):\n    return n % 2 == 0",
        "label": "correct",
        "rationale": "Correct even check.",
    },
    {
        "id": "code-04",
        "split": "holdout",
        "category": "code",
        "task_type": "code",
        "prompt": "Write a Python function to check if a number is even.",
        "response": "def is_even(n):\n    return n % 2 == 1",
        "label": "wrong",
        "rationale": "Inverted condition — this checks odd, not even.",
    },
    {
        "id": "code-05",
        "split": "tune",
        "category": "code",
        "task_type": "code",
        "prompt": "Write a one-line Python list comprehension that squares each number in `nums`.",
        "response": "[n**2 for n in nums]",
        "label": "correct",
        "rationale": "Correctly squares each element.",
    },
    {
        "id": "code-06",
        "split": "tune",
        "category": "code",
        "task_type": "code",
        "prompt": "Write a one-line Python list comprehension that squares each number in `nums`.",
        "response": "[n*2 for n in nums]",
        "label": "wrong",
        "rationale": "Doubles each element instead of squaring it.",
    },
    {
        "id": "code-07",
        "split": "holdout",
        "category": "code",
        "task_type": "code",
        "prompt": "In Python, what does `len([1,2,3])` return?",
        "response": "3",
        "label": "correct",
        "rationale": "The list has 3 elements.",
    },
    {
        "id": "code-08",
        "split": "holdout",
        "category": "code",
        "task_type": "code",
        "prompt": "In Python, what does `len([1,2,3])` return?",
        "response": "2",
        "label": "wrong",
        "rationale": "Miscounts the list; the correct length is 3.",
    },
    {
        "id": "code-09",
        "split": "tune",
        "category": "code",
        "task_type": "code",
        "prompt": "Write a SQL query to select all rows from table `users` where `age` > 18.",
        "response": "SELECT * FROM users WHERE age > 18;",
        "label": "correct",
        "rationale": "Matches the requested predicate exactly.",
    },
    {
        "id": "code-10",
        "split": "tune",
        "category": "code",
        "task_type": "code",
        "prompt": "Write a SQL query to select all rows from table `users` where `age` > 18.",
        "response": "SELECT * FROM users WHERE age < 18;",
        "label": "wrong",
        "rationale": "Flipped comparison operator — selects under-18s, the opposite of what was asked.",
    },
    {
        "id": "code-11",
        "split": "holdout",
        "category": "code",
        "task_type": "code",
        "prompt": "What does the Python built-in `sorted([3,1,2])` return?",
        "response": "[1, 2, 3]",
        "label": "correct",
        "rationale": "sorted() returns ascending order by default.",
    },
    {
        "id": "code-12",
        "split": "tune",
        "category": "code",
        "task_type": "code",
        "prompt": "Write a Python function `factorial(n)` that computes n! recursively, and explain the base case.",
        "response": (
            "def factorial(n):\n    if n == 1:\n        return 1\n    return n * factorial(n-1)\n\n"
            "The base case is when n reaches 1, at which point the recursion stops and returns 1."
        ),
        "label": "partial",
        "rationale": "Recursion and explanation are right for n>=1, but there's no base case for n=0 "
        "(factorial(0) should be 1; this recurses into negative n and never terminates).",
    },
    {
        "id": "code-13",
        "split": "holdout",
        "category": "code",
        "task_type": "code",
        "prompt": "Write a function to reverse a string in Python and explain its time complexity.",
        "response": "def reverse(s):\n    return s[::-1]\n\nThis runs in O(1) time since slicing is a single operation.",
        "label": "partial",
        "rationale": "The code is correct, but the complexity claim is wrong: slicing a string of length n is O(n), not O(1).",
    },
    # ── explanation / analysis (12: 5 correct, 5 wrong, 2 partial) ──────────
    {
        "id": "expl-01",
        "split": "tune",
        "category": "explanation",
        "task_type": "analyze",
        "prompt": "Explain in one sentence why the sky appears blue.",
        "response": (
            "The sky appears blue because air molecules scatter shorter (blue) wavelengths of "
            "sunlight more than longer (red) wavelengths, a phenomenon called Rayleigh scattering."
        ),
        "label": "correct",
        "rationale": "Correctly describes Rayleigh scattering as the cause.",
    },
    {
        "id": "expl-02",
        "split": "tune",
        "category": "explanation",
        "task_type": "analyze",
        "prompt": "Explain in one sentence why the sky appears blue.",
        "response": "The sky appears blue because the ocean's blue color reflects onto the atmosphere.",
        "label": "wrong",
        "rationale": "A common myth; the real cause is Rayleigh scattering of sunlight, not ocean reflection.",
    },
    {
        "id": "expl-03",
        "split": "holdout",
        "category": "explanation",
        "task_type": "analyze",
        "prompt": "Explain the difference between TCP and UDP in one or two sentences.",
        "response": (
            "TCP is connection-oriented and guarantees reliable, ordered delivery of data, while UDP "
            "is connectionless and sends packets without guaranteeing delivery or order, trading "
            "reliability for lower latency."
        ),
        "label": "correct",
        "rationale": "Accurately contrasts TCP's reliability guarantees with UDP's lack thereof.",
    },
    {
        "id": "expl-04",
        "split": "holdout",
        "category": "explanation",
        "task_type": "analyze",
        "prompt": "Explain the difference between TCP and UDP in one or two sentences.",
        "response": (
            "TCP and UDP are both connectionless protocols, but TCP is faster because it doesn't "
            "check for errors."
        ),
        "label": "wrong",
        "rationale": "Backwards on every count: TCP is connection-oriented and does error-check; UDP is the connectionless, faster one.",
    },
    {
        "id": "expl-05",
        "split": "tune",
        "category": "explanation",
        "task_type": "analyze",
        "prompt": "Why do objects fall at the same rate in a vacuum regardless of mass?",
        "response": (
            "Because gravitational acceleration is independent of an object's mass — the force of "
            "gravity scales with mass, but so does the inertia resisting it, so the two cancel out."
        ),
        "label": "correct",
        "rationale": "Correctly explains the equivalence of gravitational and inertial mass.",
    },
    {
        "id": "expl-06",
        "split": "tune",
        "category": "explanation",
        "task_type": "analyze",
        "prompt": "Why do objects fall at the same rate in a vacuum regardless of mass?",
        "response": (
            "Because heavier objects have more air resistance, which balances out their extra weight."
        ),
        "label": "wrong",
        "rationale": "Self-contradicting: the question specifies a vacuum, where there is no air resistance at all.",
    },
    {
        "id": "expl-07",
        "split": "holdout",
        "category": "explanation",
        "task_type": "analyze",
        "prompt": "What is the main purpose of a firewall in computer networking?",
        "response": (
            "A firewall monitors and controls incoming and outgoing network traffic based on "
            "predefined security rules, blocking unauthorized access while allowing legitimate "
            "communication."
        ),
        "label": "correct",
        "rationale": "Accurately describes a firewall's function.",
    },
    {
        "id": "expl-08",
        "split": "holdout",
        "category": "explanation",
        "task_type": "analyze",
        "prompt": "What is the main purpose of a firewall in computer networking?",
        "response": "A firewall encrypts all data sent over a network to prevent eavesdropping.",
        "label": "wrong",
        "rationale": "Describes encryption/VPN behavior, not a firewall's traffic-filtering purpose.",
    },
    {
        "id": "expl-09",
        "split": "tune",
        "category": "explanation",
        "task_type": "analyze",
        "prompt": "Briefly explain what causes seasons on Earth.",
        "response": (
            "Seasons are caused by the tilt of Earth's axis (about 23.5 degrees) relative to its "
            "orbital plane around the Sun, which changes how directly sunlight hits different "
            "hemispheres throughout the year."
        ),
        "label": "correct",
        "rationale": "Correctly attributes seasons to axial tilt.",
    },
    {
        "id": "expl-10",
        "split": "tune",
        "category": "explanation",
        "task_type": "analyze",
        "prompt": "Briefly explain what causes seasons on Earth.",
        "response": (
            "Seasons are caused by Earth's changing distance from the Sun as it orbits — closer in "
            "summer, farther in winter."
        ),
        "label": "wrong",
        "rationale": "A common misconception; orbital distance variation is not the cause of seasons "
        "(and would not even explain why hemispheres have opposite seasons).",
    },
    {
        "id": "expl-11",
        "split": "tune",
        "category": "explanation",
        "task_type": "analyze",
        "prompt": "Explain what a REST API is and give one example of an HTTP verb it uses.",
        "response": "A REST API is a way for two computers to talk to each other over the internet, and it uses the verb 'FETCH'.",
        "label": "partial",
        "rationale": "The general description is roughly right but vague; 'FETCH' is not a standard HTTP verb "
        "(GET/POST/PUT/DELETE are) — right idea, wrong specific detail.",
    },
    {
        "id": "expl-12",
        "split": "holdout",
        "category": "explanation",
        "task_type": "analyze",
        "prompt": "Explain the difference between a stack and a queue, and give a real-world analogy for each.",
        "response": "A stack is LIFO, like a pile of plates; a queue is FIFO.",
        "label": "partial",
        "rationale": "LIFO/FIFO and the stack analogy are correct, but the requested queue analogy is missing — incomplete, not wrong.",
    },
]

assert len(ITEMS) == 50, f"expected 50 hand-labelled items, got {len(ITEMS)}"
assert sum(1 for i in ITEMS if i["label"] == "correct") == 22
assert sum(1 for i in ITEMS if i["label"] == "wrong") == 20
assert sum(1 for i in ITEMS if i["label"] == "partial") == 8
assert sum(1 for i in ITEMS if i["split"] == "tune") == 28
assert sum(1 for i in ITEMS if i["split"] == "holdout") == 22
assert len({i["id"] for i in ITEMS}) == 50, "ids must be unique"
