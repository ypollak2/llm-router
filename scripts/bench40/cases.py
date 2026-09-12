"""40 cases across the categories Claude actually worked in over 5 days.

Weighted by measured token share, not by guesswork:
    images 80.5% | bash 14.3% | text reads 3.1% | edits/other 2.1%

Vision is its own runner (a different model and a different question shape), so
the 32 text cases here cover the operational work and vision.py covers the rest.
Every check is mechanical: a required string, or exact file content.
"""
import fixture

R = fixture.read


def has(*subs):
    return lambda out: all(s.lower() in (out or "").lower() for s in subs)


def any_of(*subs):
    return lambda out: any(s.lower() in (out or "").lower() for s in subs)


def file_is(rel, sub):
    return lambda out: sub in R(rel)


def file_not(rel, sub):
    return lambda out: sub not in R(rel)


# ── A. Shell / inspection — mirrors pytest+grep+sed+cat = 56% of Bash ───────
SHELL = [
 ("run-tests", "Run the test suite with `python3 -m pytest tests -q` and tell me if it passed.",
  any_of("pass", "5 passed", "all", "ok")),
 ("count-tests", "How many test functions are defined under tests/? Answer with the number.",
  has("5")),
 ("find-todo", "Is there any TODO comment anywhere in this project? Answer yes or no.",
  any_of("no", "none", "not")),
 ("list-src", "List the Python files in src/. Give just the filenames.",
  has("config.py", "cache.py", "client.py", "util.py")),
 ("version", "What version is this project, according to pyproject.toml?",
  has("0.4.2")),
 ("changelog-top", "What is the most recent entry in CHANGELOG.md about?",
  any_of("cache eviction", "eviction")),
 ("line-count", "How many lines are in src/client.py? Answer with the number.",
  any_of("27", "28", "26")),
 ("python-version", "What Python version does this project require?",
  has("3.11")),
 ("readme-install", "What command does the README say to use to install?",
  has("pip install")),
 ("grep-sleep", "Which file calls time.sleep? Answer with the path.",
  has("client.py")),
]

# ── B. Code navigation / search ─────────────────────────────────────────────
NAV = [
 ("find-const", "What is the value of MAX_RETRIES and which file defines it?",
  has("3", "config.py")),
 ("find-class", "Which file defines the LRUCache class?", has("cache.py")),
 ("trace-import", "src/client.py imports LRUCache. Which file does it come from?",
  has("cache.py")),
 ("count-importers", "How many files import from src.config? Answer with the number.",
  any_of("2", "two")),
 ("list-methods", "What methods does the LRUCache class have? List their names.",
  has("get", "put", "clear")),
 ("find-default", "What is the default value of the `limit` parameter of truncate()?",
  has("80")),
 ("cache-size", "What is the default cache size, and where does the value come from?",
  has("128", "config")),
 ("timeout-ms", "What does timeout_ms() return for the current configuration? Give the number.",
  has("30000")),
 ("who-uses-cache", "Which file creates an LRUCache instance?", has("client.py")),
 ("public-funcs", "Name the three functions defined in src/util.py.",
  has("slugify", "truncate", "parse_bool")),
]

# ── C. Edits — real file changes, checked by content ────────────────────────
EDITS = [
 ("bump-retries", "Change MAX_RETRIES to 5 in src/config.py. Change nothing else.",
  lambda o: "MAX_RETRIES = 5" in R("src/config.py") and "TIMEOUT_SECONDS = 30" in R("src/config.py")),
 ("enable-debug", "Set DEBUG to True in src/config.py.",
  file_is("src/config.py", "DEBUG = True")),
 ("bump-version", "Bump the version in pyproject.toml to 0.5.0.",
  file_is("pyproject.toml", 'version = "0.5.0"')),
 ("add-constant", "Add a new constant `RETRY_BACKOFF = 2` to src/config.py.",
  file_is("src/config.py", "RETRY_BACKOFF = 2")),
 ("add-docstring", "Add a one-line docstring to the slugify function in src/util.py.",
  lambda o: '"""' in R("src/util.py").split("def slugify")[-1][:180]),
 ("rename-param", "In src/util.py, rename truncate's `limit` parameter to `max_len`, "
                  "updating the body too.",
  lambda o: "max_len" in R("src/util.py") and "limit" not in R("src/util.py")),
 ("add-method", "Add a `size_of(self)` method to LRUCache in src/cache.py that returns "
                "the number of items currently held.",
  lambda o: "def size_of" in R("src/cache.py")),
 ("new-file", "Create src/version.py containing exactly: VERSION = \"0.4.2\"",
  file_is("src/version.py", 'VERSION = "0.4.2"')),
 ("add-import", "Add `import os` as the first import line of src/util.py.",
  file_is("src/util.py", "import os")),
 ("fix-changelog", "Add a new `## 0.5.0` section at the top of CHANGELOG.md, "
                   "above 0.4.2, with a bullet saying `- Add size_of to LRUCache`.",
  lambda o: "0.5.0" in R("CHANGELOG.md")
            and R("CHANGELOG.md").index("0.5.0") < R("CHANGELOG.md").index("0.4.2")),
]

# ── D. Multi-step — needs several tools chained ─────────────────────────────
MULTI = [
 ("const-and-test", "MAX_RETRIES is used somewhere outside config.py. Find where, "
                    "then tell me the file and what it is used for.",
  has("client.py")),
 ("edit-then-verify", "Change CACHE_SIZE to 256, then run `python3 -m pytest tests -q` "
                      "and report whether the tests still pass.",
  lambda o: "CACHE_SIZE = 256" in R("src/config.py")
            and any(w in (o or "").lower() for w in ("pass", "ok"))),
]

TEXT_CASES = (
    [("shell", *c) for c in SHELL]
    + [("nav", *c) for c in NAV]
    + [("edit", *c) for c in EDITS]
    + [("multi", *c) for c in MULTI]
)
