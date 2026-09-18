# What a context pack actually contains — 18 September 2026

Two numbers from this branch's commit messages were audited and could not be
reproduced by a reader. This file exists so they can be.

The rule this repository already had — a number ships with its `n`, its window
and the file it came from — turns out not to be enough on its own. Both numbers
below are *correct*; both were unreproducible because the **conditions** were
not stated, and a number whose conditions are missing is not a measurement
someone else can check.

## Pack shape for one representative query

```
query    "fix _write_source_concept in src/llm_router/okf.py"
commit   1475c3a (feat/semantic-layer)
index    built over the whole repo immediately before the run
memory   the 15 seeded starter records, freshly seeded
budget   2000 tokens (the default)

status              ok
evidence            20 entities
lessons              2
decision constraints 0
tokens             595 / 2000
omissions            0
suggested checks   tests/okf/test_okf_scope_02_explicit_root.py
                   tests/okf/test_okf_scope_03_verified_writes.py
```

Reproduce:

```bash
python - <<'PY'
import sys, pathlib, json; sys.path.insert(0, "src")
from llm_router.semantic import indexer as ix, pack as P, experience as exp, seed_lessons
b = pathlib.Path("/tmp/pack-idx"); e = pathlib.Path("/tmp/pack-exp")
ix.index(root=".", base=b); seed_lessons.seed(e)
p = P.build("fix _write_source_concept in src/llm_router/okf.py",
            root=".", base=b, experience=exp.ExperienceStore(e))
print(p.retrieval_status, p.retrieved_tokens, "/", p.budget_tokens,
      len(p.evidence), "evidence", len(p.applicable_lessons), "lessons")
PY
```

An audit run that used `llm-router semantic explain` against the user's real
knowledge directory got **254 tokens** instead — correctly, because that store
had not been seeded and the index was built at a different commit. Both numbers
are right for what they measured. The commit message named neither condition,
which is what made the figure unverifiable.

## The router swap's byte delta is NOT a constant

Commit `873a101` said the choke-point swap added "+252 bytes, constant, every
prompt". The first half of that is a snapshot; the second is wrong.

`scripts/shadow_diff_router_injection.py`, 12 router-shaped prompts:

| Working tree | Delta per prompt |
|---|---|
| clean | ~211 bytes |
| dirty (audit run) | ~260 bytes |
| dirty (this run, `1475c3a`) | **+391 bytes** |

`<repo_state>` renders the branch name, the last commit subject and the list of
uncommitted files (`src/llm_router/repo_facts.py`), so its length is a property
of the working tree and changes while you work. Three runs, three numbers.

**What is actually stable, and what the swap rests on:**

- the OKF half is byte-identical in **12 of 12** prompts — retrieval does not
  change, which was the question
- the delta is the **same for every prompt** in any single run — it is a fixed
  overhead, not a per-prompt cost that scales with the query

Those two survive. "+252 bytes" does not, and the script now prints the caveat
itself so the next person cannot quote a magnitude without seeing why they
should not.

## The general lesson, filed as `stated-conditions-011`

An `n` is necessary and not sufficient. A measurement also needs the state it
ran against — which commit, which index, which store, clean or dirty — or the
next person reproduces a different thing and one of you concludes the other was
wrong.
