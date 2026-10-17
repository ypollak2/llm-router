QUOTA_BALANCED now orders subscriptions with `resource_map.score` in one
`route_and_call` pass using primary/weekly pressure, a reset bonus, marginal
cost and policy priority. Unknown inputs are never scored; unknown pressure
follows measured pressure below 0.95 and precedes measured pressure at or above
0.95. The old `quota_balance` reorder is removed from the router's chain build.
The compatibility `chain_builder.build_chain` and agent-route Codex delegation
also use the score under the quota_balanced profile. Weights and checks are
registered in `docs/prereg/PREREG-P23-quota-score.md`.
