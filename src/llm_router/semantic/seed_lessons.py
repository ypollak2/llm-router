"""The starter set — real incidents from this repository, honestly stated.

A memory layer seeded with invented examples teaches nothing and proves less.
Every record below is something that actually happened in this codebase, with
the file and line it happened at and the commit that addressed it, or an honest
`repair_status` saying it has not been addressed.

The states are the point. Read down the `repair_status` column and you will
find `verified` next to `proposed`, because two of these are still open. A seed
set where everything is fixed and everything is reviewed would demonstrate
exactly the failure mode the four axes exist to prevent: a store in which every
record renders as "known good".

The contradictory pair at the end is deliberate. `applicability.conflicts()`
cannot be shown to work against a set of records that all agree, and ten to
twenty honest starter lessons are unlikely to contradict each other by
accident. These two are real positions that were both held in this repo at
different times, and the conflict is the record.
"""
from __future__ import annotations

from pathlib import Path

from llm_router.semantic.experience import (
    Applicability,
    Decision,
    Enforcement,
    ExperienceStore,
    Lesson,
    RepairStatus,
    Review,
    Validation,
)

# The date this session established each of these. `known_from` is when the
# system learned it; `valid_from` is when it became true of the project, which
# for a defect is when the defect shipped.
_LEARNED = "2026-09-18"


def starter_records() -> list:
    return [
        # ── the five prerequisite defects ────────────────────────────────────
        Lesson(
            lesson_id="project-scope-write-001",
            statement=(
                "index_project(root=B) called from inside project A reported B's "
                "store in its summary and wrote A's directory on disk."
            ),
            failure_family="project-scope",
            triggers=["a writer that resolves its own destination",
                      "a long-lived process serving several projects"],
            preconditions=["the requested project differs from the process cwd"],
            exceptions=["a command that only ever operates on its own resolved "
                        "project still needs a clearly scoped boundary"],
            supported_mechanism=(
                "the indexer accepted `root`; _write_source_concept computed "
                "project_knowledge_dir(base=base) with root=None, falling back "
                "to $LLM_ROUTER_PROJECT_ROOT or a .git walk from cwd"
            ),
            suggested_action=(
                "carry an immutable project scope through reads, writers, "
                "enrichment and background jobs — never recover it at the write "
                "boundary"
            ),
            affected_paths=["src/llm_router/okf.py",
                            "src/llm_router/hooks/context-capture.py"],
            affected_symbols=["_write_source_concept", "index_project",
                              "record_session_turn"],
            evidence_ids=["probe:explicit-root-2026-09-17", "commit:7e18e73"],
            check_refs=["tests/okf/test_okf_scope_02_explicit_root.py"],
            review=Review.REVIEWED,
            validation=Validation.REPRODUCED,
            applicability=Applicability.ACTIVE,
            enforcement=Enforcement.ADOPTED_RULE,
            repair_status=RepairStatus.VERIFIED,
            valid_from="2026-08-01",
            known_from=_LEARNED,
        ),
        Lesson(
            lesson_id="unverified-enrichment-002",
            statement=(
                "OKF wrote 'Defines: X' for files it never opened, pairing "
                "files[0] with every symbol name found anywhere in a reply."
            ),
            failure_family="unverified-write",
            triggers=["two independent extractors whose results get paired",
                      "a policy that says 'checkable' rather than 'checked'"],
            supported_mechanism=(
                "_FILE_PAT ran over prompt+response and _SYM_PAT over the "
                "response; neither result was checked against the other or "
                "against the file"
            ),
            suggested_action=(
                "read the file and confirm the definition before asserting it; "
                "count the rejections so 'nothing to record' is distinguishable "
                "from 'everything was invented'"
            ),
            affected_paths=["src/llm_router/okf.py"],
            affected_symbols=["enrich_from_response", "_extract_files_and_symbols"],
            evidence_ids=["commit:d6e2124"],
            check_refs=["tests/okf/test_okf_scope_03_verified_writes.py"],
            review=Review.REVIEWED,
            validation=Validation.REPRODUCED,
            enforcement=Enforcement.ADOPTED_RULE,
            repair_status=RepairStatus.VERIFIED,
            valid_from="2026-08-01",
            known_from=_LEARNED,
        ),
        Lesson(
            lesson_id="substring-scoring-003",
            statement=(
                "The grounding scorer accepted wrong_directory/okf.py as correct "
                "for gold src/llm_router/okf.py, because it substring-matched "
                "the basename."
            ),
            failure_family="measurement",
            triggers=["a lenient scoring rule implemented with `in`"],
            supported_mechanism=(
                "`q['answer'] in text or q['basename'] in text` — the basename "
                "allowance was meant to forgive an OMITTED directory and also "
                "forgave a WRONG one"
            ),
            suggested_action=(
                "extract path tokens, normalise, compare exactly; report strict "
                "and lenient separately, each with its n"
            ),
            affected_paths=["scripts/bench_grounding.py"],
            affected_symbols=["scores", "score"],
            evidence_ids=["commit:84161f1",
                          "Docs/measurements/2026-09-18-grounding-corrected-baseline.md"],
            check_refs=["tests/test_bench_grounding_scoring.py"],
            review=Review.REVIEWED,
            validation=Validation.REPRODUCED,
            enforcement=Enforcement.ADOPTED_RULE,
            repair_status=RepairStatus.VERIFIED,
            valid_from="2026-09-01",
            known_from=_LEARNED,
        ),
        Lesson(
            lesson_id="dead-branch-by-omitted-argument-004",
            statement=(
                "router.py called prepare_prompt without project_dir, so the "
                "code-context branch was unreachable for every routed call and "
                "reported context_source='none' — indistinguishable from a miss."
            ),
            failure_family="silent-dead-branch",
            triggers=["a feature gated on an optional argument",
                      "a caller that has the value and does not pass it"],
            supported_mechanism=(
                "context_prep gates on `task_type in (CODE, ANALYZE) and "
                "project_dir`; the router resolved a usable scope forty lines "
                "later, for OKF only"
            ),
            suggested_action=(
                "a branch that can never run should fail a test, not report the "
                "same value as a legitimate empty result"
            ),
            affected_paths=["src/llm_router/router.py",
                            "src/llm_router/context_prep.py"],
            affected_symbols=["prepare_prompt"],
            evidence_ids=["commit:873a101"],
            check_refs=["tests/test_router_context_seam.py"],
            review=Review.REVIEWED,
            validation=Validation.REPRODUCED,
            enforcement=Enforcement.ADOPTED_RULE,
            repair_status=RepairStatus.VERIFIED,
            valid_from="2026-08-01",
            known_from=_LEARNED,
        ),
        Lesson(
            lesson_id="divergent-scope-conventions-005",
            statement=(
                "Five modules resolved project scope privately with two env var "
                "names and two fallbacks, so running from src/ put OKF at the "
                "repo root and the caches on the subdirectory."
            ),
            failure_family="project-scope",
            triggers=["a second module needing the same question answered"],
            supported_mechanism=(
                "semantic_cache hashed $LLM_ROUTER_PROJECT_DIR or cwd; okf "
                "walked to .git from $LLM_ROUTER_PROJECT_ROOT; result_cache "
                "hashed its caller's raw string into a FILE PATH, which does "
                "not age out the way a TTL'd column does"
            ),
            suggested_action="one resolver, called by everything",
            affected_paths=["src/llm_router/semantic/scope.py",
                            "src/llm_router/result_cache.py",
                            "src/llm_router/semantic_cache.py"],
            affected_symbols=["resolve_scope", "_project_scope", "_get_db_path"],
            evidence_ids=["commit:17ece24"],
            check_refs=["tests/semantic/test_scope_is_one_resolver.py"],
            review=Review.REVIEWED,
            validation=Validation.REPRODUCED,
            enforcement=Enforcement.ADOPTED_RULE,
            repair_status=RepairStatus.VERIFIED,
            valid_from="2026-08-01",
            known_from=_LEARNED,
        ),

        # ── found on the way, and NOT fixed ─────────────────────────────────
        Lesson(
            lesson_id="gateway-scope-via-environ-006",
            statement=(
                "gateway.py sets $LLM_ROUTER_PROJECT_ROOT for the duration of a "
                "request and restores it in a finally, which races the moment "
                "two requests arrive together."
            ),
            failure_family="project-scope",
            triggers=["a function that needs scope and takes no scope argument",
                      "concurrent requests in one process"],
            supported_mechanism=(
                "process environment is global and a request is not; the "
                "restore in `finally` cannot help a second request that read "
                "the variable while the first held it"
            ),
            suggested_action="thread scope through grounding as a value",
            affected_paths=["src/llm_router/gateway.py"],
            affected_symbols=["_resolve_project_scope"],
            evidence_ids=["src/llm_router/gateway.py:396-420"],
            review=Review.REVIEWED,
            validation=Validation.UNTESTED,
            applicability=Applicability.ACTIVE,
            enforcement=Enforcement.ADVISORY,
            repair_status=RepairStatus.PROPOSED,
            valid_from="2026-08-01",
            known_from=_LEARNED,
        ),
        Lesson(
            lesson_id="intent-gate-measured-context-007",
            statement=(
                "The quality-escalation guard measured len(prompt) after "
                "context attachment, so 'say OK' stopped being a short prompt "
                "once attachment became unconditional."
            ),
            failure_family="latent-until-unconditional",
            triggers=["a length threshold applied to an assembled string",
                      "a previously-conditional step becoming unconditional"],
            preconditions=["the gate is asking about user intent, not cost"],
            supported_mechanism=(
                "attachment added a constant ~250 bytes of repo state to every "
                "prompt; five existing tests failed on it and were right to"
            ),
            suggested_action=(
                "what gets SENT is right for cost and token estimates; what the "
                "user MEANT is a separate value, and that is what an intent "
                "gate reads"
            ),
            affected_paths=["src/llm_router/router.py"],
            affected_symbols=["_dispatch_model_loop"],
            evidence_ids=["commit:873a101"],
            check_refs=["tests/test_router_context_seam.py"],
            review=Review.REVIEWED,
            validation=Validation.REPRODUCED,
            enforcement=Enforcement.ADOPTED_RULE,
            repair_status=RepairStatus.VERIFIED,
            valid_from="2026-09-18",
            known_from=_LEARNED,
        ),
        Lesson(
            lesson_id="unknown-recorded-as-ok-008",
            statement=(
                "library-harvest recorded 'ok' whenever it could not determine "
                "an outcome, so unobserved commands became evidence that a fix "
                "succeeded."
            ),
            failure_family="unverified-write",
            triggers=["a two-valued answer to a three-valued question"],
            supported_mechanism=(
                "_outcome's final `return 'ok', 0` was the 'I could not tell' "
                "branch; sealer.is_seal_event seals only on ok, so it was also "
                "deciding that unverified commits were milestones"
            ),
            suggested_action=(
                "unknown is a third answer, and its exit code is None because "
                "0 would be a claim"
            ),
            affected_paths=["src/llm_router/hooks/library-harvest.py",
                            "src/llm_router/library/sealer.py"],
            affected_symbols=["_outcome", "is_seal_event"],
            evidence_ids=["commit:0d62fda"],
            check_refs=["tests/library/test_unknown_is_not_success.py"],
            review=Review.REVIEWED,
            validation=Validation.REPRODUCED,
            enforcement=Enforcement.ADOPTED_RULE,
            repair_status=RepairStatus.VERIFIED,
            valid_from="2026-08-01",
            known_from=_LEARNED,
        ),
        Lesson(
            lesson_id="biography-froze-at-forty-009",
            statement=(
                "Once the biography held 40 bullets, every durable fact learned "
                "afterwards was discarded in silence."
            ),
            failure_family="silent-cap",
            triggers=["a cap on a store of record rather than on a view"],
            supported_mechanism=(
                "add = add[: max(0, MAX_BIO_FACTS - current_count)] evaluates "
                "to an empty slice forever once the document is full"
            ),
            suggested_action=(
                "cap the readable view, not the record; and say on the view "
                "that it is showing a subset"
            ),
            affected_paths=["src/llm_router/library/book_closer.py"],
            affected_symbols=["_merge_biography", "MAX_BIO_FACTS"],
            evidence_ids=["commit:0d62fda"],
            check_refs=["tests/library/test_biography_does_not_freeze_at_forty.py"],
            review=Review.REVIEWED,
            validation=Validation.REPRODUCED,
            enforcement=Enforcement.ADOPTED_RULE,
            repair_status=RepairStatus.VERIFIED,
            valid_from="2026-08-01",
            known_from=_LEARNED,
        ),
        Lesson(
            lesson_id="measure-on-the-target-distribution-010",
            statement=(
                "A rate quoted without its n has been read as a collapse four "
                "times in this repo; days of 21-64 prompts produced 2.5%, 1.6%, "
                "0% and 4.3%, all noise."
            ),
            failure_family="measurement",
            triggers=["reporting a percentage", "comparing two days"],
            supported_mechanism=(
                "small denominators produce large swings, and a workload shift "
                "looks identical to a regression until you check what the user "
                "was doing"
            ),
            suggested_action=(
                "print n beside every rate; below ~50 say 'too few to tell'; "
                "code regressions do not heal themselves, workload shifts do"
            ),
            affected_paths=["scripts/bench_grounding.py", "CLAUDE.md"],
            evidence_ids=["CLAUDE.md"],
            check_refs=["tests/test_bench_grounding_scoring.py"],
            review=Review.REVIEWED,
            validation=Validation.SUPPORTED,
            enforcement=Enforcement.ADOPTED_RULE,
            repair_status=RepairStatus.NONE,
            valid_from="2026-09-13",
            known_from="2026-09-13",
        ),
        Lesson(
            lesson_id="wall-clock-is-not-a-duration-011",
            statement=(
                "macOS Maintenance Sleep advances time.time() and not "
                "time.monotonic(); one benchmark task was recorded at 918.6s of "
                "which 902s was the laptop asleep."
            ),
            failure_family="measurement",
            triggers=["timing anything on this machine",
                      "an unattended benchmark run"],
            suggested_action="time.monotonic() for durations, caffeinate -i for runs",
            affected_paths=["scripts/bench_grounding.py"],
            evidence_ids=["CLAUDE.md"],
            check_refs=["tests/test_bench_grounding_scoring.py"],
            review=Review.REVIEWED,
            validation=Validation.REPRODUCED,
            enforcement=Enforcement.ADOPTED_RULE,
            repair_status=RepairStatus.VERIFIED,
            valid_from="2026-09-13",
            known_from="2026-09-13",
        ),

        # ── decisions ───────────────────────────────────────────────────────
        Decision(
            decision_id="sqlite-for-the-derived-index",
            statement=(
                "The derived index is SQLite in the project's knowledge "
                "directory, not a graph database service."
            ),
            alternatives=["a graph database service",
                          "an LLM-extracted whole-repository knowledge graph",
                          "graph embeddings for routing"],
            rejected_because={
                "a graph database service":
                    "a service to deploy and operate for a local, bounded index; "
                    "reconsider only if measured graph size or query shape "
                    "exceeds adjacency tables plus FTS in one file",
                "an LLM-extracted whole-repository knowledge graph":
                    "inferred edges, ingestion cost, stale summaries and "
                    "contamination risk, none of which the structural questions "
                    "need",
                "graph embeddings for routing":
                    "needs labels that do not exist and adds a training problem "
                    "before the simpler features have shown value",
            },
            consequences=["no service to run", "rebuildable from source",
                          "an embedding index stays optional and later"],
            affected_paths=["src/llm_router/semantic/"],
            review=Review.REVIEWED,
            validation=Validation.UNTESTED,
            enforcement=Enforcement.ADVISORY,
            valid_from=_LEARNED,
            known_from=_LEARNED,
        ),
        Decision(
            decision_id="routing-policy-stays-pinned",
            statement=(
                "Model-selection policy does not change while the context and "
                "memory layers are being measured."
            ),
            alternatives=["expose graph features to the router now"],
            rejected_because={
                "expose graph features to the router now":
                    "changing retrieval and routing in one experiment makes "
                    "neither attributable; the arms exist precisely to separate "
                    "them"
            },
            consequences=["D vs C measures retrieval; E vs D measures routing"],
            affected_paths=["src/llm_router/router.py"],
            review=Review.REVIEWED,
            validation=Validation.UNTESTED,
            enforcement=Enforcement.ADVISORY,
            valid_from=_LEARNED,
            known_from=_LEARNED,
        ),

        # ── a real, deliberate contradiction ────────────────────────────────
        Lesson(
            lesson_id="basename-is-enough-012a",
            statement=(
                "Naming the file is the knowledge under test; the directory is "
                "a formatting preference the question did not ask for."
            ),
            failure_family="measurement",
            triggers=["scoring a file-identification answer"],
            affected_paths=["scripts/bench_grounding.py"],
            affected_symbols=["scores"],
            evidence_ids=["scripts/bench_grounding.py docstring at a535f22"],
            review=Review.DISPUTED,
            validation=Validation.SUPPORTED,
            applicability=Applicability.ACTIVE,
            enforcement=Enforcement.ADVISORY,
            valid_from="2026-09-01",
            known_from="2026-09-01",
        ),
        Lesson(
            lesson_id="exact-path-or-nothing-012b",
            statement=(
                "An answer naming the wrong directory is wrong; only the exact "
                "repo-relative path counts."
            ),
            failure_family="measurement",
            triggers=["scoring a file-identification answer"],
            supported_mechanism=(
                "the lenient rule was implemented as a substring test, so it "
                "forgave a wrong directory as readily as an omitted one"
            ),
            affected_paths=["scripts/bench_grounding.py"],
            affected_symbols=["score"],
            evidence_ids=["commit:84161f1",
                          "Docs/measurements/2026-09-18-grounding-corrected-baseline.md"],
            check_refs=["tests/test_bench_grounding_scoring.py"],
            contradicts=["basename-is-enough-012a"],
            review=Review.REVIEWED,
            validation=Validation.REPRODUCED,
            applicability=Applicability.ACTIVE,
            enforcement=Enforcement.ADOPTED_RULE,
            valid_from=_LEARNED,
            known_from=_LEARNED,
        ),
    ]


def seed(root: Path | str, overwrite: bool = False) -> int:
    """Write the starter set. Returns how many records were written.

    Existing records are left alone unless *overwrite*: a seed that clobbers is
    a seed that silently reverts whatever a person corrected by hand.
    """
    store = ExperienceStore(root)
    written = 0
    for record in starter_records():
        from llm_router.semantic.experience import record_id
        if not overwrite and store.get(record_id(record)) is not None:
            continue
        store.put(record)
        written += 1
    return written
