# llm-router — North Star

> **Keep a Claude Pro or Max subscriber's quota for the work that needs Claude.**
> Routine prompts are answered by free local or cheap models, with no API keys and no
> change to how the user works — and every claim of quota saved is one we actually observed.

Source of the goal: `README.md:13-15`, `npm/package.json:4`. The clause about observed
savings comes from the 15.2.0 correction (`CHANGELOG.md:45`, `CHANGELOG.md:83-88`).

---

## What llm-router should do

1. **Preserve subscription quota.** Quota is the unit, not dollars. Success means fewer or
   shorter Claude turns on routine prompts. (`README.md:397-399`)
2. **Serve the flat-rate subscriber first.** Pay-per-token users are better served by a
   proxy; that is not the gap we fill. (`README.md:118-120`)
3. **Require zero API keys and zero workflow change.** Install it, keep using Claude Code
   as before. (`README.md:14-15`)
4. **Route by complexity.** The cheapest capable model takes simple work; Claude is kept for
   the hard work. The router never sends work to a model that cannot do it.
5. **Stay local-first.** Everything runs on the user's machine: no hosted proxy, no
   account, and telemetry never leaves `~/.llm-router/`. (`README.md:408-414`)
6. **Count a routed answer only when it is used.** A draft that was produced but not used
   saved nothing.
7. **Never trade quality for quota.** Routed answers must be as good as Claude's for the
   task. Quality is measured independently (RouterArena, `docs/BACKEND-QUALITY.md`), and
   a quality regression blocks a release.
8. **Tell each user what it did for them.** `llm-router status` and `summary` report
   verified and unverified savings separately, each with its n.
9. **Fail open to Claude, never to a wrong answer.** If a model is slow, missing or
   unusable, Claude answers. Chronically bad models get demoted. (`README.md:411-413`)
10. **Stay the right size.** It is built for individual developers and small teams.
    Org-wide policy, SSO and audit export belong to Chuzom. (`README.md:571-574`)

## How to act

11. **Every number carries its n, window, conditions and source file**, or it does not
    ship. Leaving the claim out is always an option. (`CLAUDE.md:36`)
12. **On a subscription, savings are quota, never dollars.** API-price figures are
    labelled as counterfactuals. (`README.md:397-399`)
13. **Unverified savings are never a headline.** They appear as `+ $X unverified, n=N`,
    and only below the verified figure. (`CHANGELOG.md:83-88`)
14. **Measure on real workload.** Exclude test and benchmark traffic, and prove that the
    filter dropped something. (`CLAUDE.md:22`, `:65`, `:90`)
15. **Claims match the mode.** The default mode is advisory: Claude still takes the turn.
    Turn replacement is claimed only for `LLM_ROUTER_ZERO_CLAUDE=1`, and the README
    headline must say which mode it describes. (`README.md:19-24`)
16. **A routed draft is an unverified hint, never presented as ground truth.** Claude
    decides whether to use it; nothing fabricates what a model "would have said".
17. **Scope safety claims to what was measured.** State what the guard does not stop as
    plainly as what it does. (`README.md:459-463`)
18. **Log every routing decision with its reason.** A non-route has a recorded reason. A
    default is not a classification, and "unknown" must not quietly become the
    unfavourable answer. (`CLAUDE.md:54`, `:222`, `:264`)
19. **Publish negative results.** A failed experiment or a lost benchmark is documented,
    not buried. (`README.md:131-161`)
20. **Ship only through the verified path.** Open a PR to main, run
    `scripts/release/pre-release-verify.sh`, then publish a GitHub Release. Never re-push
    a tag. (`CLAUDE.md:125`)

---

## How we know it is working

| Metric | Role | Today |
|---|---|---|
| Verified share of eligible Claude turns replaced or shortened, on real workload | **Primary**: the North Star number | ~0.66%: 7 of 1,066 gated rows, `usage.db` 2026-09-24 ([audit](AUDIT-2026-09-24.md)); earlier 0 of 1,185 drafts (`CHANGELOG.md:45`) |
| Verified quota saved, with its n | Reported to each user | $0.11 verified (n=7) vs $483.14 unverified (n=46,099), maintainer ledger 2026-09-24 (`CHANGELOG.md:86-88`) |
| Quality of routed answers vs Claude on the same tasks | Guardrail: must not drop | RouterArena and `docs/BACKEND-QUALITY.md` |

## Where the repo currently disagrees with this file

These are open items for the audit, not settled facts.

- **The headline outruns the default mode.** "Spend less of your … plan" (`README.md:13`)
  vs "a draft being produced does *not* by itself mean … quota was saved" (`README.md:19-22`).
- **Dollars vs quota.** The Enterprise section says "local cost savings" (`README.md:573`),
  but point 12 says savings on a subscription are quota.
- **The evidence runs against the goal.** No audited local draft was used before 15.2.0.
