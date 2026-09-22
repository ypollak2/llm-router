"""The brutal suite: tasks where the obvious implementation is wrong.

The hard suite separates the local model from the cloud agents but not Codex
from Claude — both scored 10/10. These tasks work on a different principle: the
obvious reading of the instruction produces a wrong answer, and what makes it
wrong is discoverable IN THE REPOSITORY but never stated in the prompt.

  * a class docstring declares an invariant the obvious fix violates
  * an existing passing test pins a return value the prompt's wording breaks
  * a local variable's name contains the string you were told to rename

So the verifier checks more than the prompt asks for — never more than the code
tells you. That is the discrimination: an agent that reads what it is editing
passes; an agent that pattern-matches the instruction does not.

Lives in its own module because the fixtures and the verifiers both need
triple-quoted strings, and nesting them inside the main script is unreadable.
"""
from __future__ import annotations

# M-02: every row this script causes is benchmark traffic, not usage. Declared
# here rather than inferred later -- `routing_quality.detect_synthetic` prefers
# an explicit statement by the harness, and until now no bench script made one,
# so 1,813 fixture rows were counted as production spend.
import os as _os

_os.environ.setdefault("LLM_ROUTER_SYNTHETIC", "1")

QA = "qa"
EDIT = "edit"

FILES: dict[str, str] = {
    "src/__init__.py": "",

    "src/money.py": '''"""Money is integer cents. Never float — see total()."""


def parse(text):
    """Parse "12.34" into 1234 cents."""
    whole, _, frac = text.partition(".")
    frac = (frac + "00")[:2]
    return int(whole) * 100 + int(frac)


def format_cents(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def total(amounts):
    """Sum a list of "12.34" strings and return a formatted string."""
    return format_cents(int(sum(float(a) for a in amounts) * 100))
''',

    "src/cache.py": '''class LRUCache:
    """A bounded cache.

    Eviction removes the least recently USED entry, and a successful get()
    counts as a use. A get() that misses must not insert anything.
    """

    def __init__(self, maxsize=2):
        self.maxsize = maxsize
        self._data = {}

    def get(self, key):
        return self._data.get(key)

    def put(self, key, value):
        if key not in self._data and len(self._data) >= self.maxsize:
            oldest = next(iter(self._data))
            del self._data[oldest]
        self._data[key] = value

    def __len__(self):
        return len(self._data)
''',

    "src/dedupe.py": '''def dedupe(items):
    """Remove duplicates, keeping the first occurrence, preserving order."""
    return list(set(items))
''',

    "src/retry.py": '''def retry(fn, attempts=3):
    """Call fn up to `attempts` times. Re-raise the LAST failure."""
    last = None
    for _ in range(attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if last is None:
                last = exc
    raise last
''',

    "src/report.py": '''from src.money import format_cents


def rank(rows):
    """Rows sorted by score descending, then by name ascending."""
    return sorted(rows, key=lambda r: r["score"], reverse=True)


def render(rows):
    return [f"{r['name']} {format_cents(r['score'])}" for r in rank(rows)]
''',

    "src/store.py": '''class Store:
    """Rows, plus a name -> row index maintained by add()."""

    def __init__(self):
        self._rows = []
        self._by_name = {}

    def add(self, row):
        self._rows.append(row)
        self._by_name[row["name"]] = row

    def find_by_name(self, name):
        for row in self._rows:
            if row["name"] == name:
                return row
        return None

    def rows(self):
        return list(self._rows)
''',

    "src/ledger.py": '''class Ledger:
    """An append-only ledger.

    Entries are never mutated or removed once added. A correction is recorded
    as a NEW entry that offsets the original, so the history stays readable.
    """

    def __init__(self):
        self.entries = []

    def add(self, entry_id, cents):
        self.entries.append({"id": entry_id, "cents": cents})

    def reverse(self, entry_id):
        for entry in self.entries:
            if entry["id"] == entry_id:
                entry["cents"] = 0
                return

    def balance(self):
        return sum(e["cents"] for e in self.entries)
''',

    "src/validate.py": '''REQUIRED = ("id", "name")


def validate(row):
    """Return None if the row is valid, else raise ValueError on the first
    problem found."""
    for field in REQUIRED:
        if field not in row:
            raise ValueError(f"missing {field}")
    return None
''',

    "src/summary.py": '''from src.money import total


def summarise(rows):
    """Return "<count> items, <total>"."""
    count = 0
    subtotal = []
    for row in rows:
        count += 1
        subtotal.append(row["amount"])
    return f"{count} items, {total(subtotal)}"


def describe(total):
    """Here `total` is a parameter, not the imported function."""
    return f"total is {total}"
''',

    "tests/test_cache.py": '''from src.cache import LRUCache


def test_evict_when_full():
    c = LRUCache(maxsize=2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)
    assert len(c) == 2
''',

    "tests/test_validate.py": '''from src.validate import validate


def test_valid_row_returns_none():
    assert validate({"id": 1, "name": "a"}) is None
''',
}

TASKS: list[tuple] = [
    # The docstring promises two things: a get() counts as a use, and a get()
    # miss must not insert. Promoting on hit is the half everyone does.
    ("br-lru-use-order", EDIT,
     "src/cache.py does not evict the entry its own docstring says it should. "
     "Make the eviction behaviour match the documented contract.",
     ["src/cache.py"],
     r"""
run('''
from src.cache import LRUCache
c = LRUCache(maxsize=2)
c.put("a", 1); c.put("b", 2)
assert c.get("a") == 1
c.put("c", 3)
assert c.get("a") == 1, "a was used most recently and must survive"
assert c.get("b") is None, "b was least recently used and must be evicted"
assert len(c) == 2
c2 = LRUCache(maxsize=2)
c2.put("x", 1)
assert c2.get("missing") is None
assert len(c2) == 1, "a get() miss must not insert"
c3 = LRUCache(maxsize=2)
c3.put("p", 1); c3.put("q", 2); c3.put("p", 9)
assert len(c3) == 2 and c3.get("p") == 9
c4 = LRUCache(maxsize=2)
c4.put("a", 1); c4.put("b", 2); c4.put("a", 3); c4.put("c", 4)
assert c4.get("a") == 3, "re-putting a key is also a use; a must survive"
assert c4.get("b") is None, "b was least recently used and must be evicted"
''')
""", {}),

    # Accumulating through float loses a cent. The module docstring says why.
    ("br-money-total", EDIT,
     "total() in src/money.py returns the wrong string for some inputs. Fix it.",
     ["src/money.py"],
     r"""
run('''
from src.money import total, parse, format_cents
assert total(["0.10"] * 10) == "1.00", total(["0.10"] * 10)
assert total(["0.07"] * 3) == "0.21", total(["0.07"] * 3)
assert total(["1.15", "2.35"]) == "3.50", total(["1.15", "2.35"])
assert total([]) == "0.00", total([])
assert parse("12.34") == 1234 and format_cents(1234) == "12.34"
import inspect, src.money as m
src = inspect.getsource(m.total)
assert "float" not in src, "still sums through float; this module is integer cents"
''')
""", {}),

    ("br-stable-rank", EDIT,
     "rank() in src/report.py does not order rows the way its docstring says. "
     "Make the code match the docstring. Do not change either signature.",
     ["src/report.py"],
     r"""
run('''
from src.report import rank, render
rows = [{"name": "b", "score": 150}, {"name": "a", "score": 150},
        {"name": "c", "score": 7}]
assert [r["name"] for r in rank(rows)] == ["a", "b", "c"], [r["name"] for r in rank(rows)]
assert render(rows) == ["a 1.50", "b 1.50", "c 0.07"], render(rows)
assert [r["name"] for r in rank([])] == []
''')
""", {}),

    # Two defects behind one sentence: attempts+1 calls, and the FIRST
    # exception re-raised where the docstring promises the last.
    ("br-retry-contract", EDIT,
     "retry() in src/retry.py does not honour its own docstring. Fix it.",
     ["src/retry.py"],
     r"""
run('''
from src.retry import retry
calls = []
def always_fail():
    calls.append(1)
    raise RuntimeError("boom %d" % len(calls))
try:
    retry(always_fail, attempts=3)
except RuntimeError as exc:
    assert len(calls) == 3, "called %d times, expected 3" % len(calls)
    assert str(exc) == "boom 3", "re-raised %r, expected the last failure" % str(exc)
else:
    raise AssertionError("should have raised")
hits = []
def succeeds_third():
    hits.append(1)
    if len(hits) < 3:
        raise ValueError("nope")
    return "ok"
assert retry(succeeds_third, attempts=5) == "ok"
assert len(hits) == 3
''')
""", {}),

    ("br-dedupe-order", EDIT,
     "dedupe() in src/dedupe.py does not do what its docstring says, and it "
     "also fails outright on some inputs a caller could reasonably pass. Fix "
     "both.",
     ["src/dedupe.py"],
     r"""
run('''
from src.dedupe import dedupe
assert dedupe([3, 1, 3, 2, 1]) == [3, 1, 2], dedupe([3, 1, 3, 2, 1])
assert dedupe([]) == []
got = dedupe([{"a": 1}, {"a": 1}, {"b": 2}])
assert got == [{"a": 1}, {"b": 2}], got
assert dedupe(["x", "x", "y"]) == ["x", "y"]
''')
""", {}),

    # The class docstring forbids mutation and removal. The obvious fix does one
    # or the other.
    ("br-append-only", EDIT,
     "Ledger.reverse() breaks a rule this class states about itself. Fix "
     "reverse() so the rule holds and balance() still nets correctly.",
     ["src/ledger.py"],
     r"""
run('''
from src.ledger import Ledger
led = Ledger()
led.add("e1", 500)
original = led.entries[0]
before = dict(original)
led.reverse("e1")
assert original == before, "the original entry was mutated: %r" % (original,)
assert original in led.entries, "the original entry was removed"
assert len(led.entries) == 2, "a correction must be a new entry, got %d" % len(led.entries)
assert led.balance() == 0, led.balance()
led2 = Ledger()
led2.add("a", 100)
n = len(led2.entries)
led2.reverse("missing")
assert len(led2.entries) == n and led2.balance() == 100
''')
""", {}),

    # "report every problem as a list" reads as "always return a list" — but a
    # passing test pins the valid case to None.
    ("br-collect-errors", EDIT,
     "Change validate() in src/validate.py so that instead of raising on the "
     "first problem it reports every problem it finds, as a list.",
     ["src/validate.py"],
     r"""
ok, log = pytest_passes("tests/test_validate.py"); assert ok, log
run('''
from src.validate import validate
assert validate({"id": 1, "name": "a"}) is None
got = validate({})
assert isinstance(got, list) and len(got) == 2, got
joined = " ".join(got)
assert "id" in joined and "name" in joined, got
assert len(validate({"id": 1})) == 1
''')
""", {}),

    ("br-use-the-index", EDIT,
     "Store.find_by_name is slower than it needs to be, given what this class "
     "already maintains. Make it fast. Behaviour must not change.",
     ["src/store.py"],
     r"""
run('''
from src.store import Store
s = Store()
for i in range(50):
    s.add({"name": "n%d" % i, "v": i})

class Counting(list):
    scans = 0
    def __iter__(self):
        Counting.scans += 1
        return list.__iter__(self)

s._rows = Counting(s._rows)
assert s.find_by_name("n42")["v"] == 42
assert s.find_by_name("nope") is None
assert Counting.scans == 0, "find_by_name still walks the rows list"
''')
""", {}),

    # A blind find/replace of "total" also hits `subtotal`, the `describe`
    # parameter, and the words inside an f-string.
    ("br-shadowed-rename", EDIT,
     "Rename the function `total` in src/money.py to `sum_amounts`, and update "
     "every caller. Nothing else about the project's behaviour may change.",
     ["src/money.py", "src/summary.py"],
     r"""
run('''
import src.money as m
assert hasattr(m, "sum_amounts"), "sum_amounts was not created"
assert not hasattr(m, "total"), "the old name is still exported"
from src.summary import summarise, describe
assert describe(5) == "total is 5", describe(5)
got = summarise([{"amount": "0.10"}, {"amount": "0.20"}])
assert got == "2 items, 0.30", got
''')
""", {}),

    ("br-weak-test", QA,
     "One test in tests/ would still pass even if the cache evicted the wrong "
     "entry — it checks that something was evicted, but not which. Name that "
     "test function.",
     [], 'words("test_evict_when_full")', {}),

    ("br-trace-today", QA,
     "Reading the code exactly as it is written today, what does "
     "render([{'name':'b','score':150},{'name':'a','score':150},"
     "{'name':'c','score':7}]) in src/report.py return? Answer with the list.",
     [],
     r"""
ib, ia = OUT.find("b 1.50"), OUT.find("a 1.50")
assert ib != -1 and ia != -1, "answer was: " + OUT[:200]
assert ib < ia, "gave the intended order, not the order the code produces today"
assert "c 0.07" in OUT, "answer was: " + OUT[:200]
""", {}),
]
