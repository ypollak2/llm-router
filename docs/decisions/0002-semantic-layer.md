# ADR 0002 — Two semantic layers over one derived index

**Status:** accepted · **Date:** 2026-09-18 · **Decider:** project owner

## Context

OKF gives every routed prompt a slice of the repository. It works: the
corrected baseline measures 0/60 → 40/60 on file-identification questions when
material is retrieved ([the measurement][m], n=60). But it answers one shape of
question — "what does this file define" — and it answers it from documents that
have no notion of the snapshot they described.

Two consequences, and they are different problems.

The first is structural. A document written before a rename keeps asserting the
old shape indefinitely. There is no way to ask which files import a symbol,
what changes if a signature changes, or whether the evidence being quoted is
still true of the code on disk.

The second is that the project's engineering knowledge — why an obvious change
was rejected, which workaround is temporary, what a failure actually looked
like, which conditions made a fix work — lives nowhere a future session can
retrieve it. The library keeps durable facts as flat sentences with a commit and
a date. That records THAT something was decided. It cannot record that a
diagnosis was never reproduced, or that the check which would catch a
recurrence was never written, and those are the sentences that make remembering
an incident worth anything.

A research review ([the blueprint][r], 733 lines, revision 2) examined 28 files
and ran three isolated probes. It found five prerequisite defects and argued
that fixing them first is not optional: until the baseline is correct, a scope
fix or a richer source snippet can collect credit that gets attributed to a
semantic layer.

## Decision

Build two connected per-project layers over one scoped, derived index.

**Layer 1 — project definition.** What the code is: files, entities,
relations, spans, and the hash each was parsed from. Extracted deterministically
with `ast`. Rebuildable from the repository at any time.

**Layer 2 — engineering experience.** Why it became that way: decisions,
problems, attempts, performance observations and feature transitions, each
carrying two timelines and four independent state axes.

They are views over one index, not two systems. Experience records key on
`(path, symbol, commit range)`; the indexer resolves that key to entities once
they exist.

### What follows from this, and why

**OKF stays.** It is the readable, curated, human-editable knowledge. The
SQLite index is derived and disposable — which is precisely what licenses it to
discard aggressively. An entry whose bytes no longer hash to what was parsed is
not evidence; it is a memory of evidence, and presenting the second as the first
is the failure this layer exists to avoid.

**Four state axes, not one status.** `review` is whether a person looked.
`validation` is whether reality did. `applicability` is whether it still holds.
`enforcement` is whether anything actually fails when it is broken. An accepted
decision can have zero empirical validation; a reproduced bug can be fixed while
its prevention lesson stays active; an unresolved issue must stay discoverable
without being presented as an established root cause. Collapse these into one
field and all of them render as "known good", which makes a memory system worse
than no memory system.

**Two timelines.** When a claim applies in the project is not when we learned
it. A cause found today for a bug that shipped in March is `valid_from` March
and `known_from` today. Without the separation you cannot ask what was believed
in April, and you judge April's decision against evidence nobody had.

**Contradictions surface; they are not resolved.** A timestamp is not evidence,
and two maintenance branches hold different valid decisions at the same instant.
Both sides come back with their provenance and the conflict named. Supersession
is an explicit, recorded act.

**Retrieved prose is data.** A lesson body is written by whoever filed it and
read later by a model. It renders inside an explicit untrusted region that says
so, because otherwise filing a bug report is a way to issue instructions.

**Everything is off by default, with a shadow mode that is byte-identical to
off.** Shadow that alters one character confounds every comparison against off
and is a change nobody agreed to, running under a name that says it is not one.

**Model-selection policy does not move.** D vs C measures retrieval; E vs D
measures routing. An arm that changed both would make neither attributable.
There is a test asserting the modes module never mentions the routing symbols.

## Alternatives rejected, with the constraint behind each

A rejected option keeps its reason, because constraints disappear and a
decision inherited without its reasoning is a decision nobody can revisit.

| Rejected | Why | What would change this |
|---|---|---|
| A graph database service | A service to deploy, operate and back up, for a bounded local index that fits in one file with adjacency tables and FTS | Measured graph size or query shape exceeding what SQLite serves comfortably |
| An LLM-extracted whole-repository knowledge graph | Inferred edges, ingestion cost, stale summaries and contamination risk — none of which the structural questions need, and all of which are hard to audit | Evidence that document-level sensemaking is the bottleneck, after the deterministic layer has been measured |
| Graph embeddings / GNN features for routing | Needs labels that do not exist, and adds a training problem before the simpler features have shown any value | Simple structural features demonstrating routing lift first |
| Tree-sitter for the structural extractor | `ast` is in the standard library, exact for Python, and already correct about the definition-versus-mention distinction that motivated this | A second language needing an adapter; tree-sitter is the obvious choice there |
| Raising `MAX_BIO_FACTS` | Postpones saturation by exactly the amount raised; the store of record should not be capped at all | Nothing — the cap belongs on the view, which is where it now is |
| Keeping the lenient-only grounding scorer | Its substring implementation forgave a WRONG directory as readily as an omitted one | Nothing; both rules are now reported separately, so neither argument had to lose |

## Consequences

**Good.** Exact symbol lookup, import relations and staleness detection the
current store cannot express. Engineering history that survives a session and
states its own uncertainty. An evidence pack that names what it could not find,
so a consumer can tell a thin pack from a complete one. Arms that stamp
themselves, so a measurement has a treatment attached.

**Costs.** A second store to keep fresh. Measured on this repository at
`6b380f9`: 1,264 Python files, 14,425 entities, 2.4s to build cold and 0.08s
when nothing changed. It must be rebuilt after a checkout. More surface: one
new package, four new environment variables, one new CLI command. Experience
records need curation; fifteen seeded honestly is not fifty, and the value of
the layer is a function of whether people keep filing.

**Measured on retrieval, 18 Sep 2026** (`Docs/measurements/2026-09-18-semantic-arms.md`,
n=60, paired): the semantic pack beats the corrected OKF baseline 58/60 against
40/60, +30.0 points, 18 discordant pairs all one way, McNemar exact p=7.6e-06.

**And traversal earns nothing.** Arms C and D gave identical answers on all 60
questions. The blueprint's gate was "D beats the strongest corrected non-graph
baseline by at least 3 points"; it beat it by zero. Per the blueprint's own
instruction, the simpler system is the right one — traversal stays in the code
for the impact-shaped questions it was meant for, off by default and without
evidence.

Read the +30 with its caveats in front of it: exact symbol lookup on
uniquely-defined symbols is close to a best case for an `ast` index and close to
a worst case for lexical matching over prose, and the blueprint explicitly warns
that a graph-favourable diagnostic set cannot establish general product value.

**Still unresolved.** Whether any of it improves TASK OUTCOMES. Retrieval
accuracy is not a finished patch. The M0/M1/M2 history track — recurrence,
lesson applicability, prevention coverage — is scaffolded and not run. The
adoption gate it proposes is +3 points on the relationship-heavy stratum over
the corrected non-graph baseline with a 95% paired interval excluding zero. If
traversal cannot beat corrected hybrid retrieval under those controls, the
simpler system is the right system, and that is a useful outcome rather than a
reason to redesign the benchmark until the graph wins.

**Narrowed from the blueprint, deliberately, and not previously written down.**
An independent review found these cuts unflagged, which is how a cut becomes an
oversight in the retelling:

- §5.3's contract lists `Snapshot`, `Evidence`, `Experience event`,
  `Intervention trace` and `Retrieval trace` as first-class records. None is
  persisted. The index keeps one `meta` row rather than per-generation snapshot
  rows, and retrieval and intervention traces are computed and discarded. That
  is fine for an MVP and fatal for the evaluation track, which needs to replay a
  pack against a recorded treatment — so this is the first thing Phase 5 needs.
- Three of §9's acceptance cases have no implementation: a curated business rule
  conflicting with observed source (record-versus-record conflict *is* handled;
  rule-versus-code is not), preserving source identity when a historical summary
  is regenerated, and reporting partial prevention coverage when a host action
  bypasses the router's observation points. There is no prevention-coverage
  reporting at all.
- `ContextPack` does not implement §6's `id` on evidence items as a stable
  cross-snapshot identifier; ids are positional within one pack.

**A behaviour change worth watching in the field.** `_outcome` returning
`unknown` where it used to return `ok` means any tool response without an
explicit exit code no longer seals a chapter. Most host tool responses probably
carry no exit code, so chapter sealing will become markedly rarer. That is
correct — an unobserved command is not a milestone — but it is a visible drop in
a number someone may have been watching.

**Known and not fixed.** `gateway.py` passes project scope by setting
`$LLM_ROUTER_PROJECT_ROOT` for the duration of a request and restoring it in a
`finally`. That races the moment two requests arrive together — process
environment is global and a request is not. It needs scope threaded through
`grounding` as a value. Recorded as `gateway-scope-via-environ-006` with
`repair_status=proposed`, and exempted by name in the scope guard test rather
than quietly skipped.

[r]: ../RESEARCH_SEMANTIC_LAYER.md
[m]: ../measurements/2026-09-18-grounding-corrected-baseline.md
