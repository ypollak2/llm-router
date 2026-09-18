# The arms, run — 18 September 2026

The semantic layer was built and unmeasured. This is the measurement.

Two findings, and the second one is the more useful of the two.

## 1. The semantic layer beats the corrected baseline

```
n = 60 · model = qwen3-coder:30b · seed 7 · budget 2000 tokens · paired
strict scoring (a wrong directory is a wrong answer)

arm                                        correct    acc    p50 s
A   no context at all                         0/60    0.0%     0.7
B   OKF context — the corrected baseline     40/60   66.7%     0.4
C   semantic pack, no traversal              58/60   96.7%     0.3
D   semantic pack, 2 hops                    58/60   96.7%     0.5

C vs B:  +18 of 60 (+30.0 points)
         C wins 18, B wins 0, agree 42
         McNemar exact p = 7.6e-06
```

Every one of the 18 discordant pairs goes the same way. Arm B reproduces the
corrected baseline exactly (40/60), which is the expected replication — same
seed, same questions, same scorer.

C is also *cheaper*: a median of 122 tokens against a 2000-token budget, and
the fastest arm at 0.3s p50.

Source: `scripts/run_semantic_arms.py` at commit `07bc8db`, per-question detail
in `docs/measurements/semantic-arms-n60.json`.

## 2. Traversal adds exactly nothing

**D and C are identical on all 60 questions.** Not "the same total" — the same
answer to every single one. Two hops of graph expansion changed no outcome and
cost 0.2s per query.

The research document set the adoption gate for the graph at *D beats the
strongest corrected non-graph baseline by at least 3 points*, and said plainly
what to do otherwise:

> If graph traversal cannot beat corrected hybrid retrieval under these
> controls, keep the simpler retrieval system. That is a useful outcome, not a
> reason to redesign the benchmark until the graph wins.

So: **ship C, not D.** Traversal stays in the code because the M-track and
impact-shaped questions may yet need it, but it is not on by default and it has
no evidence behind it on this task.

## What this does NOT establish

This is close to a best case for the thing being tested, and the number should
be read with that in front of it.

- **The task is exact symbol lookup**, on symbols the repository defines exactly
  once. That is precisely what an `ast`-built index is for, and precisely where
  lexical matching over prose documents is weakest. The document warns against
  exactly this: *an intentionally graph-heavy diagnostic set alone cannot
  establish general product value.*
- **It is retrieval, not task completion.** Nothing here shows a better patch, a
  fixed bug or a finished task. A system that retrieves better *should* answer
  more of these; whether that converts into work getting done is the M0/M1/M2
  history track, a different experiment on a different task set.
- **One model, one repository, one seed, n=60.** The interval on 58/60 is wide.
  Cross-project generalisation is untested and the document is explicit that
  with very few projects, uncertainty about a new project stays large.
- **The two remaining misses** are `_evaluate` and `_skip` — short, generic
  names that appear everywhere. Retrieval found material for all 60; the model
  still picked wrong on those two.

## What changes as a result

| | Before | After |
|---|---|---|
| Is the layer measured? | no | on retrieval, yes; on task completion, still no |
| Does traversal earn its place? | unknown | **no**, on this task set |
| Default | off | off — this is one benchmark, not a product decision |

The defaults do not change on the strength of one retrieval benchmark. What
changes is that the claim "unmeasured" is no longer accurate for retrieval, and
that the graph half of the design has its first piece of negative evidence.
